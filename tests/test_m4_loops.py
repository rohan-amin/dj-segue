"""M4 — loops: schema 0.3 (loop segments, segment ids, `after` positions,
target_bpm "mix"), loop timing in the compiler, loop rendering in the engine,
and the acceptance test (a 4→2→1→0.5 beat tightening loop, beat-locked to the
mix clock).

The stretch test needs the `rubberband` CLI; it skips without it.
"""

from __future__ import annotations

import shutil

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from dj_segue.analyzer.beat import detect_onsets
from dj_segue.compiler import resolve_timeline, transition_envelopes
from dj_segue.executor.native import NativeEngine
from dj_segue.executor.native.engine import LOOP_SEAM_SEC
from dj_segue.inspect import format_plan
from dj_segue.schema import PlanValidationError, validate_against_audio, validate_plan
from dj_segue.schema.plan import Plan

SR = 44100
BEAT = SR // 2  # samples per beat at 120 bpm (exact)

needs_rubberband = pytest.mark.skipif(
    shutil.which("rubberband") is None, reason="rubberband CLI not installed"
)

TIGHTENING = [
    {"length": {"beats": 4}, "repetitions": 2},
    {"length": {"beats": 2}, "repetitions": 2},
    {"length": {"beats": 1}, "repetitions": 2},
    {"length": {"beats": 0.5}, "repetitions": 2},
]
# Mix-beat offsets of each rep's start from the loop start, and the loop's length.
TIGHTENING_REP_BEATS = [0, 4, 8, 10, 12, 13, 14, 14.5]
TIGHTENING_TOTAL = 15


def _plan(timeline: list[dict], *, automation=None, version="0.3", tempo=120, bpm=120) -> Plan:
    return Plan.model_validate(
        {
            "schema_version": version,
            "meta": {"mix_name": "t", "mix_tempo": tempo},
            "tracks": {"a": {"path": "a.wav", "bpm": bpm}, "b": {"path": "b.wav", "bpm": bpm}},
            "decks": {"1": {}, "2": {}},
            "timeline": timeline,
            "automation": automation or [],
        }
    )


def _loop(start=None, frm=8, schedule=TIGHTENING, **extra) -> dict:
    seg = {"type": "loop", "deck": 1, "track": "a", "from": {"beat": frm}, "schedule": schedule}
    if start is not None:
        seg["start_at"] = {"beat": start}
    return seg | extra


def _play(deck, track, frm, to, start=None, **extra) -> dict:
    seg = {"type": "play", "deck": deck, "track": track,
           "from": {"beat": frm}, "to": {"beat": to}}
    if start is not None:
        seg["start_at"] = {"beat": start}
    return seg | extra


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_loop_segment_parses() -> None:
    plan = _plan([_loop(start=0, id="l")])
    seg = plan.timeline[0]
    assert seg.type == "loop" and seg.id == "l"
    assert [s.repetitions for s in seg.schedule] == [2, 2, 2, 2]


@pytest.mark.parametrize(
    "timeline",
    [
        [_loop(start=0)],
        [_play(1, "a", 0, 8, start=0, id="x")],
        [_play(1, "a", 0, 8, start=0, target_bpm="mix")],
        [_play(1, "a", 0, 8, start=0, id="x"),
         {**_play(2, "b", 0, 8), "start_at": {"after": "x"}}],
    ],
)
def test_v03_features_rejected_in_older_versions(timeline) -> None:
    with pytest.raises(ValidationError, match="requires schema_version 0.3"):
        _plan(timeline, version="0.2")


def test_loop_needs_a_schedule_and_whole_repetitions() -> None:
    with pytest.raises(ValidationError):
        _plan([_loop(start=0, schedule=[])])
    with pytest.raises(ValidationError):
        _plan([_loop(start=0, schedule=[{"length": {"beats": 4}, "repetitions": 0}])])


def test_after_is_not_a_track_position() -> None:
    with pytest.raises(ValidationError):
        _plan([{**_play(1, "a", 0, 8, start=0), "from": {"after": "x"}}])


def test_target_bpm_only_accepts_mix_as_a_word() -> None:
    with pytest.raises(ValidationError):
        _plan([_play(1, "a", 0, 8, start=0, target_bpm="fast")])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _issues(plan: Plan) -> list[str]:
    with pytest.raises(PlanValidationError) as e:
        validate_plan(plan)
    return e.value.issues


def test_valid_loop_plan_passes() -> None:
    validate_plan(_plan([_play(1, "a", 0, 8, start=0), _loop(id="l"),
                         {**_play(2, "b", 0, 8), "start_at": {"after": "l"}}]))


def test_after_unknown_id() -> None:
    (issue,) = _issues(_plan([{**_play(1, "a", 0, 8), "start_at": {"after": "nope"}}]))
    assert "no segment has that id" in issue


def test_after_must_reference_an_earlier_segment() -> None:
    (issue,) = _issues(_plan([
        {**_play(1, "a", 0, 8), "start_at": {"after": "later"}},
        _play(2, "b", 0, 8, start=0, id="later"),
    ]))
    assert "earlier segment" in issue


def test_duplicate_ids() -> None:
    (issue,) = _issues(_plan([_play(1, "a", 0, 8, start=0, id="x"),
                              _play(2, "b", 0, 8, start=0, id="x")]))
    assert "already used" in issue


def test_non_positive_loop_length() -> None:
    (issue,) = _issues(_plan([_loop(start=0, schedule=[{"length": {"beats": 0}, "repetitions": 1}])]))
    assert "length must be positive" in issue


def test_automation_after_unknown_id() -> None:
    lane = {"lane": "deck_volume", "deck": 1,
            "keyframes": [{"at": {"after": "ghost"}, "value": 1.0}]}
    (issue,) = _issues(_plan([_loop(start=0)], automation=[lane]))
    assert "ghost" in issue


def test_loop_past_track_end() -> None:
    plan = _plan([_loop(start=0, frm=60)])  # loop covers beats 60–64 = 30–32 s
    with pytest.raises(PlanValidationError, match="loop covers"):
        validate_against_audio(plan, {"a": 31.0, "b": 31.0})
    validate_against_audio(plan, {"a": 33.0, "b": 33.0})


def test_segment_after_a_loop_does_not_overlap_it() -> None:
    # Implicit start: the play begins where the loop ends.
    validate_against_audio(
        _plan([_loop(start=0), _play(1, "a", 12, 16)]), {"a": 60.0, "b": 60.0}
    )
    plan = _plan([_loop(start=0), _play(1, "a", 12, 16, start=10)])
    with pytest.raises(PlanValidationError, match="overlaps"):
        validate_against_audio(plan, {"a": 60.0, "b": 60.0})


# ---------------------------------------------------------------------------
# Compiler timing
# ---------------------------------------------------------------------------


def test_loop_rep_timing() -> None:
    tl = resolve_timeline(_plan([_loop(start=4, id="l")]), 120.0)
    (sp,) = tl.spans
    assert sp.kind == "loop" and sp.track_from_sec == pytest.approx(4.0)
    starts = [r.mix_start_sec for r in sp.reps]
    assert starts == pytest.approx([(4 + b) * 0.5 for b in TIGHTENING_REP_BEATS])
    assert sp.mix_end_sec == pytest.approx((4 + TIGHTENING_TOTAL) * 0.5)
    assert tl.anchors == {"l": pytest.approx(sp.mix_end_sec)}


def test_loop_lengths_are_track_beats_scaled_by_rate() -> None:
    # Track at 100 bpm stretched to the 120 bpm mix: a 4-beat loop lasts 4 mix beats.
    plan = _plan([_loop(start=0, schedule=[{"length": {"beats": 4}, "repetitions": 3}],
                        target_bpm="mix")], bpm=100)
    (sp,) = resolve_timeline(plan, 120.0).spans
    assert sp.rate == pytest.approx(1.2)
    assert sp.mix_end_sec == pytest.approx(12 * 0.5)


def test_after_positions_with_offset_and_transitions() -> None:
    plan = _plan([
        _loop(start=0, id="l"),  # ends at mix-beat 15
        {**_play(2, "b", 0, 32), "start_at": {"after": "l", "offset": {"beats": -3}}},
        {"type": "transition", "style": "crossfade", "from_deck": 1, "to_deck": 2,
         "start_at": {"after": "l", "offset": {"beats": -3}}, "duration": {"beats": 3},
         "id": "x"},
        {**_play(1, "a", 0, 4), "start_at": {"after": "x"}},
    ])
    tl = resolve_timeline(plan, 120.0)
    assert tl.spans[1].mix_start_sec == pytest.approx(12 * 0.5)
    assert tl.anchors["x"] == pytest.approx(15 * 0.5)
    assert tl.spans[2].mix_start_sec == pytest.approx(15 * 0.5)
    env = transition_envelopes(plan, 120.0, tl.anchors)[1]
    assert env.ramps[0].start_sec == pytest.approx(6.0)


def test_inspect_shows_loops_and_after() -> None:
    text = format_plan(_plan([_loop(start=0, id="l", target_bpm="mix"),
                              {**_play(2, "b", 0, 8), "start_at": {"after": "l"}}]))
    assert "loop     deck 1" in text
    assert "4 beats ×2, 2 beats ×2, 1 beats ×2, 0.5 beats ×2" in text
    assert "after l  (= mix-beat 15)" in text
    assert "@ mix bpm" in text


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _timecode(path, seconds=40.0) -> np.ndarray:
    """A track whose sample value is its own position: y[n] = n / len."""
    n = int(seconds * SR)
    y = (np.arange(n, dtype=np.float64) / n).astype(np.float32)
    sf.write(path, y, SR, subtype="FLOAT")
    return y


def test_m4_acceptance_tightening_loop_is_sample_accurate(tmp_path) -> None:
    """Acceptance: 4→2→1→0.5 beat tightening loop, beat-locked to the mix clock.

    Deck 1 plays track beats 0–8, then loops from beat 8. Every rep must start
    on the exact mix sample of its beat and replay the track from the exact
    loop-start sample; the only deviation allowed is the seam fade just
    before each boundary. Afterwards the track resumes from beat 10 (a jump,
    so it gets a seam too).
    """
    y = _timecode(tmp_path / "a.wav")
    _timecode(tmp_path / "b.wav")
    plan = _plan([_play(1, "a", 0, 8, start=0), _loop(id="l"), _play(1, "a", 10, 14)])
    out = NativeEngine().render(plan, tmp_path).samples[:, 0]

    loop_start = 8 * BEAT
    seam = int(round(LOOP_SEAM_SEC * SR))
    assert out.shape[0] == (8 + TIGHTENING_TOTAL + 4) * BEAT

    np.testing.assert_array_equal(out[:loop_start], y[:loop_start])
    bounds = [loop_start + int(b * BEAT) for b in TIGHTENING_REP_BEATS]
    loop_end = loop_start + TIGHTENING_TOTAL * BEAT
    for a, b in zip(bounds, bounds[1:] + [loop_end]):
        clean = b - seam  # the last rep's seam is the jump to beat 10
        np.testing.assert_array_equal(out[a:clean], y[loop_start : loop_start + clean - a])
    np.testing.assert_array_equal(out[loop_end:], y[10 * BEAT : 14 * BEAT])


def test_loop_seams_do_not_click(tmp_path) -> None:
    # A 440 Hz sine doesn't complete whole cycles in a beat, so a hard jump
    # back to the loop start would be a waveform discontinuity (a click).
    t = np.arange(40 * SR) / SR
    sine = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sf.write(tmp_path / "a.wav", sine, SR, subtype="FLOAT")
    sf.write(tmp_path / "b.wav", sine, SR, subtype="FLOAT")
    out = NativeEngine().render(_plan([_loop(start=0, frm=0.3)]), tmp_path).samples[:, 0]
    max_slope = 0.5 * 2 * np.pi * 440 / SR  # largest step a clean sine takes
    assert np.max(np.abs(np.diff(out))) < 1.5 * max_slope


@needs_rubberband
def test_stretched_loop_stays_on_the_mix_grid(tmp_path, write_clicks) -> None:
    # Clicks every beat at 126 bpm, looped and stretched to the 120 bpm mix:
    # every click (including each rep's restart) lands on the mix's ½-beat grid.
    write_clicks(tmp_path / "a.wav", 126.0, 0.0, seconds=30)
    write_clicks(tmp_path / "b.wav", 126.0, 0.0, seconds=30)
    # Starts at mix-beat 2: onset detection can't see a click at sample 0.
    plan = _plan([_loop(start=2, frm=16, target_bpm="mix")], bpm=126)
    out = NativeEngine().render(plan, tmp_path).samples[:, 0]
    onsets = detect_onsets(out, SR)[0]
    half = 0.25  # half a beat at 120 bpm, in seconds
    off_grid = np.abs(onsets - half * np.round(onsets / half))
    assert np.max(off_grid) < 0.004
    rep_starts = (2 + np.array(TIGHTENING_REP_BEATS)) * 0.5
    nearest = np.min(np.abs(onsets[None, :] - rep_starts[:, None]), axis=1)
    assert np.max(nearest) < 0.004  # a click at every rep restart


def test_equal_power_lane_interpolation() -> None:
    from dj_segue.compiler import deck_volume_envelopes

    lane = {"lane": "deck_volume", "deck": 1, "interpolation": "equal_power",
            "keyframes": [{"at": {"beat": 0}, "value": 0.0}, {"at": {"beat": 4}, "value": 1.0}]}
    plan = _plan([_loop(start=0)], automation=[lane])
    (env,) = deck_volume_envelopes(plan, 1, 120.0)
    assert env.value_at(1.0) == pytest.approx(np.sin(np.pi / 4))  # midpoint ≈ 0.707
    with pytest.raises(ValidationError, match="requires schema_version 0.3"):
        _plan([_play(1, "a", 0, 8, start=0)], automation=[lane], version="0.2")


def test_jump_between_plays_gets_a_seam(tmp_path) -> None:
    """A jump (play → play from elsewhere) crossfades over the seam before the
    boundary; the new segment is exact from its first sample. A plain
    continuation is untouched."""
    y = _timecode(tmp_path / "a.wav")
    _timecode(tmp_path / "b.wav")
    seam = int(round(LOOP_SEAM_SEC * SR))
    jump = _plan([_play(1, "a", 0, 4, start=0), _play(1, "a", 20, 24)])
    out = NativeEngine().render(jump, tmp_path).samples[:, 0]
    b = 4 * BEAT
    np.testing.assert_array_equal(out[: b - seam], y[: b - seam])
    np.testing.assert_array_equal(out[b:], y[20 * BEAT : 24 * BEAT])
    # Over the seam, the old audio fades out while the pre-roll fades in.
    t = (np.arange(seam) + 0.5) / seam
    blend = y[b - seam : b] * (1 - t) + y[20 * BEAT - seam : 20 * BEAT] * t
    np.testing.assert_allclose(out[b - seam : b], blend, atol=1e-6)

    cont = _plan([_play(1, "a", 0, 4, start=0), _play(1, "a", 4, 8)])
    out = NativeEngine().render(cont, tmp_path).samples[:, 0]
    np.testing.assert_array_equal(out, y[: 8 * BEAT])
