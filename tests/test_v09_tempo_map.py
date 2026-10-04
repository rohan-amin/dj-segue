"""Schema v0.9: the tempo map — the mix tempo steps and ramps partway, and
`target_bpm: "mix"` segments follow it."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from dj_segue.compiler import resolve_timeline
from dj_segue.executor.native import NativeEngine
from dj_segue.executor.native.engine import prepare
from dj_segue.executor.native.live import BLOCK, LiveEngine
from dj_segue.inspect import format_plan
from dj_segue.schema.plan import Plan
from dj_segue.schema.validator import PlanValidationError, resolved_tempo, validate_plan
from dj_segue.time_math import TempoMap, mix_duration, mix_pos_to_seconds
from dj_segue.schema.plan import BeatsDur, BeatPos, SecondsDur

SR = 44100  # the beat tests measure to a millisecond: real tracks' rate
TRACK_BPM = 120.0


# ---------------------------------------------------------------------------
# the map
# ---------------------------------------------------------------------------


def test_steps_and_ramps() -> None:
    tm = TempoMap.fixed(120).with_change(4.0, 90)  # step at beat 8
    tm = tm.with_change(tm.beat_to_sec(16), 150, 120 * 16 / (90 + 150))  # 16-beat ramp
    assert tm.sec_to_beat(4.0) == pytest.approx(8)
    assert tm.bpm_at(3.999) == 120 and tm.bpm_at(4.0) == 90
    t16 = 4.0 + 8 * 60 / 90
    assert tm.beat_to_sec(16) == pytest.approx(t16)
    # A linear-in-time ramp lasting 16 beats takes 120·16 / (90 + 150) s.
    assert tm.beat_to_sec(32) == pytest.approx(t16 + 8.0)
    assert tm.bpm_at(t16 + 4.0) == pytest.approx(120)  # halfway
    assert tm.beat_to_sec(40) == pytest.approx(t16 + 8.0 + 8 * 60 / 150)
    for b in np.linspace(-3, 50, 200):
        assert tm.sec_to_beat(tm.beat_to_sec(b)) == pytest.approx(b, abs=1e-9)


def test_constant_stretches_are_single_tempo_arithmetic() -> None:
    """Plans without changes resolve to the same seconds, bit for bit."""
    tm = TempoMap.fixed(91.93)
    for b in (0.0, 1.0, 17.25, 333.0, -4.0):
        assert mix_pos_to_seconds(BeatPos(beat=b), tm) == b * 60.0 / 91.93
        assert mix_duration(12.3, BeatsDur(beats=b), tm) == b * 60.0 / 91.93


def test_durations_count_beats_through_changes() -> None:
    tm = TempoMap.fixed(120).with_change(4.0, 60)  # beat 8: half speed
    # 4 beats from beat 6: 2 at 120 (1 s) + 2 at 60 (2 s).
    assert mix_duration(3.0, BeatsDur(beats=4), tm) == pytest.approx(3.0)
    # …and back: 4 beats before beat 10.
    assert mix_duration(6.0, BeatsDur(beats=-4), tm) == pytest.approx(-3.0)
    assert mix_duration(3.0, SecondsDur(seconds=2), tm) == 2.0


def test_changes_must_be_in_order() -> None:
    tm = TempoMap.fixed(120).with_change(4.0, 90, 2.0)
    with pytest.raises(ValueError, match="before the previous"):
        tm.with_change(5.0, 100)  # inside the ramp


# ---------------------------------------------------------------------------
# schema and validation
# ---------------------------------------------------------------------------


def _raw(changes, timeline=None, version="0.9", **meta) -> dict:
    return {
        "schema_version": version,
        "meta": {"mix_name": "t", "mix_tempo": 120, "tempo_map": changes, **meta},
        "tracks": {"c": {"path": "c.wav", "bpm": TRACK_BPM}},
        "decks": {"1": {}, "2": {}},
        "timeline": timeline or [
            {"type": "play", "id": "a", "deck": 1, "track": "c", "target_bpm": "mix",
             "from": {"beat": 0}, "to": {"beat": 48}, "start_at": {"beat": 0}},
        ],
    }


def test_tempo_map_needs_schema_09() -> None:
    with pytest.raises(ValidationError, match="tempo_map requires schema_version 0.9"):
        Plan.model_validate(_raw([{"at": {"beat": 8}, "bpm": 100}], version="0.8"))


def test_overlapping_changes_are_rejected() -> None:
    plan = Plan.model_validate(_raw([
        {"at": {"beat": 8}, "bpm": 100, "ramp": {"beats": 8}},
        {"at": {"beat": 12}, "bpm": 140},
    ]))
    with pytest.raises(PlanValidationError, match=r"tempo_map: \[1\].*before the previous"):
        validate_plan(plan)


def test_a_change_after_a_segment_that_crosses_it_is_circular() -> None:
    # 4 s before a ends, but a ends later once the tempo drops. (An offset
    # in beats would be fine: it counts back through the new tempo.)
    plan = Plan.model_validate(_raw([{"at": {"after": "a", "offset": {"seconds": -4}}, "bpm": 90}]))
    with pytest.raises(PlanValidationError, match="moves when the tempo changes"):
        validate_plan(plan)


def test_cue_positions_are_rejected() -> None:
    plan = Plan.model_validate(_raw([{"at": "drop", "bpm": 90}]))
    with pytest.raises(PlanValidationError, match="track position"):
        validate_plan(plan)


def test_after_positions_resolve() -> None:
    """The echo-out case: the clock changes where a segment ends."""
    plan = Plan.model_validate(_raw(
        [{"at": {"after": "a"}, "bpm": 74}],
        timeline=[
            {"type": "play", "id": "a", "deck": 1, "track": "c", "target_bpm": "mix",
             "from": {"beat": 0}, "to": {"beat": 16}, "start_at": {"beat": 0}},
            {"type": "play", "deck": 2, "track": "c", "target_bpm": "mix",
             "from": {"beat": 0}, "to": {"beat": 16}, "start_at": {"after": "a"}},
        ],
    ))
    validate_plan(plan)
    tm = resolved_tempo(plan)
    assert tm.bpm_at(8.0) == 74 and tm.bpm_at(7.99) == 120
    spans = resolve_timeline(plan, tm).spans
    assert spans[1].rate == pytest.approx(74 / TRACK_BPM)
    assert spans[1].warp is None  # wholly at 74
    assert spans[1].mix_end_sec == pytest.approx(8.0 + 16 * 60 / 74)


def test_numeric_target_bpm_ignores_the_map() -> None:
    plan = Plan.model_validate(_raw(
        [{"at": {"beat": 4}, "bpm": 60}],
        timeline=[{"type": "play", "deck": 1, "track": "c", "target_bpm": 120,
                   "from": {"beat": 0}, "to": {"beat": 16}, "start_at": {"beat": 0}}],
    ))
    (sp,) = resolve_timeline(plan, resolved_tempo(plan)).spans
    assert sp.warp is None and sp.mix_end_sec == pytest.approx(8.0)


def test_a_mix_segment_across_a_change_follows_it() -> None:
    plan = Plan.model_validate(_raw([{"at": {"beat": 8}, "bpm": 60}]))
    (sp,) = resolve_timeline(plan, resolved_tempo(plan)).spans
    assert sp.warp is not None
    # 8 beats at 120 (4 s) + 40 at 60 (40 s).
    assert sp.mix_end_sec == pytest.approx(44.0)


def test_loop_reps_land_on_mix_beats_through_a_ramp() -> None:
    plan = Plan.model_validate(_raw(
        [{"at": {"beat": 4}, "bpm": 80, "ramp": {"beats": 8}}],
        timeline=[{"type": "loop", "deck": 1, "track": "c", "target_bpm": "mix",
                   "from": {"beat": 8}, "start_at": {"beat": 2},
                   "schedule": [{"length": {"beats": 1}, "repetitions": 12}]}],
    ))
    tm = resolved_tempo(plan)
    (sp,) = resolve_timeline(plan, tm).spans
    starts = [r.mix_start_sec for r in sp.reps]
    assert starts == pytest.approx([tm.beat_to_sec(2 + k) for k in range(12)])
    assert sp.mix_end_sec == pytest.approx(tm.beat_to_sec(14))


def test_inspect_lists_the_changes() -> None:
    plan = Plan.model_validate(_raw([
        {"at": {"beat": 8}, "bpm": 100},
        {"at": {"beat": 16}, "bpm": 140, "ramp": {"beats": 8}},
    ]))
    text = format_plan(plan, run_validation=False)
    assert "beat 8: 100 bpm" in text
    assert "beat 16: ramp 100 → 140 bpm over 8 beats" in text


# ---------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------


def _clicks(root: Path, seconds: float = 40.0) -> None:
    """A click on every beat at TRACK_BPM, from 0 s."""
    x = np.zeros(int(seconds * SR), dtype=np.float32)
    for k in range(int(seconds * TRACK_BPM / 60)):
        i = int(round(k * 60 / TRACK_BPM * SR))
        x[i : i + 150] = np.hanning(300)[150:] * np.sin(np.arange(150) * 0.9) * 0.8
    sf.write(root / "c.wav", np.stack([x, x], 1), SR, subtype="FLOAT")


def _onsets(y: np.ndarray) -> np.ndarray:
    loud = np.flatnonzero(np.abs(y) > 0.3 * np.abs(y).max())
    return loud[np.concatenate([[True], np.diff(loud) > SR // 8])] / SR


CHANGES = [
    {"at": {"beat": 8}, "bpm": 96},  # a step down
    {"at": {"beat": 16}, "bpm": 140, "ramp": {"beats": 16}},  # a ramp up
    {"at": {"beat": 40}, "bpm": 110, "ramp": {"seconds": 4}},  # and down
]


def test_beats_land_on_the_mix_grid_through_steps_and_ramps(tmp_path) -> None:
    _clicks(tmp_path)
    plan = Plan.model_validate(_raw(CHANGES, timeline=[
        {"type": "play", "deck": 1, "track": "c", "target_bpm": "mix",
         "from": {"beat": 0}, "to": {"beat": 60}, "start_at": {"beat": 0}}]))
    validate_plan(plan)
    tm = resolved_tempo(plan)
    y = NativeEngine(limited=False).render(plan, tmp_path).samples[:, 0]
    got = _onsets(y)
    want = np.array([tm.beat_to_sec(k) for k in range(len(got))])
    assert len(got) >= 59
    assert np.abs(got - want).max() < 0.0015  # about what plain stretching manages


def test_live_is_the_offline_render(tmp_path) -> None:
    _clicks(tmp_path)
    plan = Plan.model_validate(_raw(CHANGES, timeline=[
        {"type": "play", "id": "a", "deck": 1, "track": "c", "target_bpm": "mix",
         "from": {"beat": 0}, "to": {"beat": 20}, "start_at": {"beat": 0}},
        {"type": "loop", "deck": 1, "track": "c", "target_bpm": "mix", "from": {"beat": 20},
         "schedule": [{"length": {"beats": 2}, "repetitions": 3},
                      {"length": {"beats": 1}, "repetitions": 4}]},
        {"type": "play", "deck": 2, "track": "c", "target_bpm": "mix",
         "from": {"beat": 0}, "to": {"beat": 40}, "start_at": {"beat": 10}},
        {"type": "transition", "style": "crossfade", "from_deck": 1, "to_deck": 2,
         "start_at": {"beat": 26}, "duration": {"beats": 8}},
    ]))
    validate_plan(plan)
    full = NativeEngine().render(plan, tmp_path).samples
    rng = np.random.default_rng(3)
    eng = LiveEngine(prepare(plan, tmp_path))
    out = []
    while not eng.done:
        out.append(eng.read(int(rng.integers(1, 3 * BLOCK))))
    np.testing.assert_array_equal(np.concatenate(out), full)


def test_seeks_keep_the_beat(tmp_path) -> None:
    """Entered mid-way (mid-ramp, after a step), the beats still land on the
    mix grid — a seek restarts the stretcher, so a little looser than a
    render."""
    _clicks(tmp_path)
    plan = Plan.model_validate(_raw(CHANGES, timeline=[
        {"type": "play", "deck": 1, "track": "c", "target_bpm": "mix",
         "from": {"beat": 0}, "to": {"beat": 50.5}, "start_at": {"beat": 0}}]))
    tm = resolved_tempo(plan)
    for beat in (12.3, 21.3, 44.1):
        start = int(tm.beat_to_sec(beat) * SR)
        eng = LiveEngine(prepare(plan, tmp_path), start=start, limited=False)
        out = []
        while not eng.done:
            out.append(eng.read(BLOCK))
        got = _onsets(np.concatenate(out)[:, 0]) + start / SR
        want = [tm.beat_to_sec(round(tm.sec_to_beat(t))) for t in got]
        assert len(got) > 5
        assert np.abs(got - want).max() < 0.002


def test_an_echo_out_into_a_new_tempo(tmp_path) -> None:
    """The R2 case: deck 1 echoes out and deck 2 takes over at its own
    tempo, with the mix clock moving to it."""
    _clicks(tmp_path)
    plan = Plan.model_validate(_raw(
        [{"at": {"after": "a"}, "bpm": 90}],
        timeline=[
            {"type": "play", "id": "a", "deck": 1, "track": "c", "target_bpm": "mix",
             "from": {"beat": 0}, "to": {"beat": 16}, "start_at": {"beat": 0}},
            {"type": "play", "deck": 2, "track": "c", "target_bpm": "mix",
             "from": {"beat": 0}, "to": {"beat": 16}, "start_at": {"after": "a"}},
            {"type": "transition", "style": "echo_out", "from_deck": 1, "to_deck": 2,
             "start_at": {"after": "a"}, "duration": {"beats": 4},
             "echo": {"every": {"beats": 1}, "feedback": 0.5}},
        ],
    ))
    validate_plan(plan)
    tm = resolved_tempo(plan)
    p = prepare(plan, tmp_path)
    (w,) = [w for _, w in p.echo_windows()]
    assert w.every_sec == pytest.approx(0.5)  # the outgoing tempo: 120
    assert w.end_sec == pytest.approx(8.0 + 4 * 60 / 90)  # the tail counts new beats
    # Deck 2's beats are the mix's beats at 90.
    y = NativeEngine(limited=False).render(plan, tmp_path).samples[:, 0]
    after = _onsets(y[int(8.0 * SR):]) + 8.0
    want = [tm.beat_to_sec(16 + k) for k in range(16)]
    got = [min(after, key=lambda t: abs(t - x)) for x in want]
    # Deck 2 plays at a constant 0.75× (no warp): plain stretching's error.
    assert np.abs(np.array(got) - want).max() < 0.0025
    assert math.isclose(tm.beat_to_sec(17) - tm.beat_to_sec(16), 60 / 90)
