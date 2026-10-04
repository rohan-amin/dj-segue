"""Live playback in tune (R2): the whole mix through the real-time engine,
edits switched in while it plays, a loop, and the playhead."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
import soundfile as sf

from dj_segue.executor.native import NativeEngine
from dj_segue.executor.native.engine import prepare
from dj_segue.executor.native.live import SWITCH_AHEAD_SEC, LiveEngine
from dj_segue.schema.plan import Plan
from dj_segue.tune.live import TuneLive

SR = 22050
BEAT = SR // 2  # 120 bpm
FRAMES = 512


def _plan(root, deck2_volume: float = 1.0) -> Plan:
    rng = np.random.default_rng(5)
    t = np.arange(40 * SR) / SR
    a = 0.4 * np.sin(2 * np.pi * 220 * t) + 0.1 * rng.standard_normal(len(t))
    b = 0.4 * np.sin(2 * np.pi * 330 * t)
    for name, x in (("a", a), ("b", b)):
        sf.write(root / f"{name}.wav", np.stack([x, x], 1).astype(np.float32), SR, subtype="FLOAT")
    return Plan.model_validate({
        "schema_version": "0.9",
        "meta": {"mix_name": "live", "mix_tempo": 120},
        "tracks": {"a": {"path": "a.wav", "bpm": 120}, "b": {"path": "b.wav", "bpm": 120}},
        "decks": {"1": {}, "2": {}},
        "timeline": [
            {"type": "play", "deck": 1, "track": "a",
             "from": {"beat": 0}, "to": {"beat": 32}, "start_at": {"beat": 0}},
            {"type": "play", "deck": 2, "track": "b",
             "from": {"beat": 0}, "to": {"beat": 32}, "start_at": {"beat": 16}},
            {"type": "transition", "style": "crossfade", "from_deck": 1, "to_deck": 2,
             "start_at": {"beat": 24}, "duration": {"beats": 8}},
        ],
        "automation": [{"lane": "deck_volume", "deck": 2,
                        "keyframes": [{"at": {"beat": 0}, "value": deck2_volume}]}],
    })


class FakeCard:
    """Pulls the player's callback at 4× real time, keeping what it got."""

    latency = 0.0

    def __init__(self, player) -> None:
        import sounddevice as sd

        self.player, self.got, self.closed = player, [], False
        self.done = threading.Event()

        def run() -> None:
            while not self.closed:
                buf = np.zeros((FRAMES, 2), dtype=np.float32)
                try:
                    player.callback(buf, FRAMES, None, None)
                except sd.CallbackStop:
                    self.got.append(buf[: player.ring.r - FRAMES * len(self.got)])
                    break
                self.got.append(buf)
                time.sleep(FRAMES / SR / 4)
            self.done.set()

        threading.Thread(target=run, daemon=True).start()

    def close(self) -> None:
        self.closed = True

    def audio(self) -> np.ndarray:
        return np.concatenate(self.got)


@pytest.fixture
def setup(tmp_path):
    plan = _plan(tmp_path)
    cards = []

    def build(p: Plan, start: int) -> LiveEngine:
        return LiveEngine(prepare(p, tmp_path), start=start)

    def open_stream(player):
        cards.append(FakeCard(player))
        return cards[-1]

    return tmp_path, plan, TuneLive(build, plan, SR, open_stream), cards


def test_plays_the_mix_from_the_top(setup) -> None:
    root, plan, live, cards = setup
    live.play(0)
    assert cards[0].done.wait(30)
    full = NativeEngine().render(plan, root).samples
    out = cards[0].audio()
    assert not live.playing and live.state() == {"playing": False, "position": None}
    np.testing.assert_array_equal(out, full)


def test_an_edit_while_playing_is_heard(setup) -> None:
    root, plan, live, cards = setup
    quiet = _plan(root, deck2_volume=0.25)
    live.play(0)
    while live.player.engine.position < 20 * BEAT:
        time.sleep(0.005)
    queued = live.player.engine.position
    live.set_plan(quiet)  # what a validated preview does
    assert cards[0].done.wait(30)
    out = cards[0].audio()
    old = NativeEngine().render(plan, root).samples
    new = NativeEngine().render(quiet, root).samples
    at = int(np.argmax(np.any(out != old, axis=1)))
    assert queued + int(SWITCH_AHEAD_SEC * SR) <= at < queued + int(0.6 * SR)
    np.testing.assert_array_equal(out[:at], old[:at])
    late = slice(at + SR // 10, at + SR // 10 + 4 * BEAT)
    np.testing.assert_allclose(out[late], new[late], atol=1e-6)


def test_the_loop_goes_round(setup) -> None:
    root, plan, live, cards = setup
    start, end = 20 * BEAT, 26 * BEAT
    live.set_loop((start, end))
    live.play(start)
    seen, prev = [], None  # where the playhead was after each time round
    deadline = time.time() + 20
    while len(seen) < 3 and time.time() < deadline:
        pos = int(live.state()["position"] * SR)
        if prev is None or pos < prev - BEAT:
            seen.append(pos)
        prev = pos
        time.sleep(0.01)
    live.stop()
    assert len(seen) >= 3, "the loop didn't come round twice"
    for pos in seen[1:]:
        assert start <= pos < start + BEAT // 2
    # Each pass started at the loop's start, one loop length apart.
    marks = cards[0].player.marks
    assert all(m == start for _, m in marks)
    assert {b - a for (a, _), (b, _) in zip(marks[1:], marks[2:])} == {end - start}


def test_play_while_playing_jumps(setup) -> None:
    root, plan, live, cards = setup
    live.play(0)
    time.sleep(0.2)
    live.play(28 * BEAT)
    deadline = time.time() + 5
    while time.time() < deadline:
        pos = live.state()["position"]
        if pos is not None and pos * SR >= 28 * BEAT:
            break
        time.sleep(0.01)
    else:
        pytest.fail("never got to the jump")
    assert len(cards) == 1  # the same stream: a jump, not a restart
    live.stop()
    assert not live.playing
