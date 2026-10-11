"""Real-time engine: played from the top it's the offline render, sample for
sample, whatever the block sizes; seeks and live plan switches sound right."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from dj_segue.executor.native import NativeEngine
from dj_segue.executor.native.engine import prepare
from dj_segue.executor.native.live import (
    BLOCK,
    SWITCH_AHEAD_SEC,
    LiveEngine,
    Player,
)
from dj_segue.schema.plan import Plan

SR = 22050
BPM = 120.0
BEAT = SR // 2


def _write(root: Path) -> dict:
    rng = np.random.default_rng(7)
    t = np.arange(40 * SR) / SR
    def tone(hz, amp):  # noqa: E306
        return amp * np.sin(2 * np.pi * hz * t)
    files = {
        "a.wav": tone(110, 0.5) + 0.3 * rng.standard_normal(len(t)),
        "bv.wav": tone(800, 0.4),
        "bd.wav": tone(60, 0.6) + 0.2 * rng.standard_normal(len(t)),
    }
    for name, x in files.items():
        sf.write(root / name, np.stack([x, x * 0.9], 1).astype(np.float32), SR, subtype="FLOAT")
    return {"a": {"path": "a.wav", "bpm": BPM},
            "b": {"stems": {"vocals": "bv.wav", "drums": "bd.wav"}, "bpm": BPM}}


def _plan(root: Path, **over) -> Plan:
    raw = {
        "schema_version": "0.8",
        "meta": {"mix_name": "live", "mix_tempo": 126},
        "tracks": _write(root),
        "decks": {"1": {}, "2": {}, "3": {}},
        "timeline": [
            # stretched, then a jump (seam), then a loop
            {"type": "play", "id": "a1", "deck": 1, "track": "a", "target_bpm": "mix",
             "from": {"beat": 0}, "to": {"beat": 16}, "start_at": {"beat": 0}},
            {"type": "play", "deck": 1, "track": "a", "target_bpm": "mix",
             "from": {"beat": 32}, "to": {"beat": 40}},
            {"type": "loop", "deck": 1, "track": "a", "from": {"beat": 8},
             "schedule": [{"length": {"beats": 1}, "repetitions": 4}]},
            # stems, handed off to with a vocal swap
            {"type": "play", "deck": 2, "track": "b",
             "from": {"beat": 0}, "to": {"beat": 40}, "start_at": {"beat": 20}},
            {"type": "transition", "style": "vocal_handoff", "from_deck": 1, "to_deck": 2,
             "start_at": {"beat": 22}, "duration": {"beats": 6}},
            # a third deck that echoes out
            {"type": "play", "id": "c", "deck": 3, "track": "a",
             "from": {"beat": 0}, "to": {"beat": 12}, "start_at": {"beat": 30}},
            {"type": "transition", "style": "echo_out", "from_deck": 3, "to_deck": 1,
             "start_at": {"after": "c"}, "duration": {"beats": 4},
             "echo": {"every": {"beats": 1}, "feedback": 0.6, "highpass": 0.7}},
        ],
        "automation": [
            {"lane": "eq", "deck": 1, "band": "low",
             "keyframes": [{"at": {"beat": 4}, "value_db": 0}, {"at": {"beat": 12}, "value_db": -24}]},
            {"lane": "eq", "deck": 2, "band": "high", "stem": "drums",
             "keyframes": [{"at": {"beat": 20}, "value_db": 6}]},
            {"lane": "eq", "deck": 2, "band": "mid",
             "keyframes": [{"at": {"beat": 30}, "value_db": -6}]},
            {"lane": "filter", "deck": 3,
             "keyframes": [{"at": {"beat": 32}, "value": -0.6}, {"at": {"beat": 40}, "value": 0.4}]},
            {"lane": "stem_volume", "deck": 2, "stem": "drums",
             "keyframes": [{"at": {"beat": 34}, "value": 1}, {"at": {"beat": 38}, "value": 0.3}]},
        ],
    }
    raw.update(over)
    return Plan.model_validate(raw)


def _rel_db(a: np.ndarray, b: np.ndarray) -> float:
    """Level of a − b relative to b, in dB."""
    err = np.sqrt(np.mean((a - b) ** 2)) / np.sqrt(np.mean(b ** 2))
    return 20 * np.log10(max(float(err), 1e-12))


def _live(engine: LiveEngine, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = []
    while not engine.done:
        out.append(engine.read(int(rng.integers(1, 3 * BLOCK))))
    return np.concatenate(out)


@pytest.fixture
def offline(tmp_path):
    plan = _plan(tmp_path)
    return plan, NativeEngine().render(plan, tmp_path).samples


def test_live_is_the_offline_render(tmp_path, offline) -> None:
    plan, full = offline
    assert np.abs(full).max() > 0.85  # the limiter is working too
    live = _live(LiveEngine(prepare(plan, tmp_path)))
    assert live.shape == full.shape
    np.testing.assert_array_equal(live, full)


def test_block_sizes_dont_matter(tmp_path, offline) -> None:
    plan, full = offline
    a = _live(LiveEngine(prepare(plan, tmp_path)), seed=1)
    b = _live(LiveEngine(prepare(plan, tmp_path)), seed=2)
    np.testing.assert_array_equal(a, b)
    np.testing.assert_array_equal(a, full)


def _fake_card(player: Player, *, on_position=None) -> np.ndarray:
    """Pull 512-frame buffers through the real callback, waiting (as a real
    card's timing would) until a buffer is queued; `on_position(frames
    played)` is called before each one."""
    import sounddevice as sd

    threading.Thread(target=player.produce, daemon=True).start()
    got, played = [], 0
    while True:
        while player.ring.queued() < 512 and not player.ring.finished:
            threading.Event().wait(0.001)
        if on_position is not None:
            on_position(played)
        buf = np.zeros((512, 2), dtype=np.float32)
        try:
            player.callback(buf, 512, None, None)
        except sd.CallbackStop:
            got.append(buf[: player.ring.r - played])
            break
        got.append(buf)
        played += 512
    return np.concatenate(got)


def test_player_ring_buffer_delivers_every_frame(tmp_path, offline) -> None:
    """The producer thread and the audio callback, with a fake sound card."""
    plan, full = offline
    player = Player(LiveEngine(prepare(plan, tmp_path)))
    out = _fake_card(player)
    assert player.underruns == 0
    np.testing.assert_array_equal(out, full)


def test_seek_sounds_like_the_render(tmp_path, offline) -> None:
    plan, full = offline
    start = 8 * BEAT  # mid-way through a stretched segment, under an EQ ramp
    eng = LiveEngine(prepare(plan, tmp_path), start=start)
    live = _live(eng)
    assert len(live) == len(full) - start
    ref = full[start:]
    assert not np.array_equal(live, ref)  # a stretcher really restarted mid-way…
    # …so the phases differ, but not what you hear: compare spectra.
    def spectrum(x):
        n = 1024
        frames = x[: len(x) // n * n, 0].reshape(-1, n) * np.hanning(n)
        return np.abs(np.fft.rfft(frames, axis=1)).sum(0)

    k = 4 * BEAT
    db = 20 * np.log10((spectrum(live[:k]) + 1e-9) / (spectrum(ref[:k]) + 1e-9))
    assert np.median(np.abs(db)) < 1.0


def test_seek_keeps_the_beat(tmp_path) -> None:
    """Clicks on a stretched track entered mid-way land where the render
    puts them."""
    x = np.zeros(SR * 30, dtype=np.float32)
    for k in range(60):
        i = int(k * 0.5 * SR)
        x[i : i + 150] = np.hanning(300)[150:] * np.sin(np.arange(150) * 0.9) * 0.8
    sf.write(tmp_path / "c.wav", np.stack([x, x], 1), SR, subtype="FLOAT")
    plan = Plan.model_validate({
        "schema_version": "0.8", "meta": {"mix_name": "c", "mix_tempo": 126},
        "tracks": {"c": {"path": "c.wav", "bpm": 120}}, "decks": {"1": {}},
        "timeline": [{"type": "play", "deck": 1, "track": "c", "target_bpm": "mix",
                      "from": {"beat": 0}, "to": {"beat": 40}, "start_at": {"beat": 0}}]})
    full = NativeEngine(limited=False).render(plan, tmp_path).samples[:, 0]
    start = int(10.3 * 60 / 126 * SR)
    live = _live(LiveEngine(prepare(plan, tmp_path), start=start, limited=False))[:, 0]

    def onsets(y):
        loud = np.flatnonzero(np.abs(y) > 0.3 * np.abs(y).max())
        return loud[np.concatenate([[True], np.diff(loud) > SR // 5])]

    a, b = onsets(live), onsets(full[start:])
    assert len(a) == len(b) > 20
    assert np.abs(a - b).max() / SR < 0.001


def test_switch_plays_the_new_plan(tmp_path) -> None:
    old = _plan(tmp_path)
    new = _plan(tmp_path, automation=[
        {"lane": "stem_volume", "deck": 2, "stem": "vocals",
         "keyframes": [{"at": {"beat": 0}, "value": 0}]}])
    old_full = NativeEngine().render(old, tmp_path).samples
    new_full = NativeEngine().render(new, tmp_path).samples
    player = Player(LiveEngine(prepare(old, tmp_path)))
    switch = {}

    def at_beat_30(played: int) -> None:
        if played >= 30 * BEAT and not switch:
            switch["queued"] = player.engine.position
            switch["at"] = player.schedule(lambda s: LiveEngine(prepare(new, tmp_path), start=s))

    out = _fake_card(player, on_position=at_beat_30)
    # The switch lands where it was scheduled, or a little later if the
    # producer got there first; find it as the first sample off the old plan.
    sched = switch["at"]
    assert sched >= switch["queued"] + int(SWITCH_AHEAD_SEC * SR)
    at = int(np.argmax(np.any(out != old_full, axis=1)))
    assert sched <= at < sched + int(0.5 * SR)
    assert len(out) == len(old_full)
    # Up to the switch: exactly the old plan.
    np.testing.assert_array_equal(out[:at], old_full[:at])
    # After it (and its crossfade): the new plan, vocals gone.
    span = slice(at + int(0.05 * SR), at + int(0.05 * SR) + 4 * BEAT)
    assert _rel_db(out[span], new_full[span]) < -30
    assert _rel_db(out[span], old_full[span]) > -15  # and audibly not the old one


def test_loop_is_built_before_it_plays() -> None:
    """A loop renders its whole span at once; read block by block, it's built
    in the background ahead of its start, and sounds the same."""
    from dj_segue.executor.native.sources import LOOP_PREBUILD_SEC, CompiledPlay, DeckSource

    rng = np.random.default_rng(3)
    audio = rng.standard_normal((10 * SR, 2)).astype(np.float32)
    start = int((LOOP_PREBUILD_SEC + 1) * SR)
    rep = BEAT
    loop = CompiledPlay(deck=1, mix_start_sample=start, mix_end_sample=start + 4 * rep,
                        track_start_sample=SR, track_id="a", rate=1.0,
                        rep_starts=tuple(start + k * rep for k in range(4)),
                        loop_track_samples=rep)
    end = loop.mix_end_sample
    whole = DeckSource([(loop, 0)], lambda _: audio, SR).read(0, end)

    src = DeckSource([(loop, 0)], lambda _: audio, SR)
    blocks, pos = [], 0
    while pos < end:
        n = min(BLOCK, end - pos)
        if pos < start <= pos + n:
            assert 0 in src.ahead  # started building before its first block
        blocks.append(src.read(pos, pos + n))
        pos += n
    assert not src.ahead
    np.testing.assert_array_equal(np.concatenate(blocks), whole)
