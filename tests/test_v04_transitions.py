"""Schema v0.4: crossfade `curve` and per-side `out` / `in` overrides."""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from dj_segue.compiler import resolve_timeline, transition_envelopes
from dj_segue.executor.native import NativeEngine
from dj_segue.inspect import format_plan
from dj_segue.schema import PlanValidationError, validate_plan
from dj_segue.schema.plan import Plan
from dj_segue.time_math import transition_windows

SR = 44100
BEAT = SR // 2  # samples per beat at 120 bpm (exact)
SEC_PER_BEAT = 0.5


def _plan(timeline: list[dict], *, automation=None, version="0.4") -> Plan:
    return Plan.model_validate(
        {
            "schema_version": version,
            "meta": {"mix_name": "t", "mix_tempo": 120},
            "tracks": {"a": {"path": "a.wav", "bpm": 120}, "b": {"path": "b.wav", "bpm": 120}},
            "decks": {"1": {}, "2": {}},
            "timeline": timeline,
            "automation": automation or [],
        }
    )


def _play(deck, track, frm, to, start) -> dict:
    return {"type": "play", "deck": deck, "track": track,
            "from": {"beat": frm}, "to": {"beat": to}, "start_at": {"beat": start}}


def _xfade(beat=8, dur=4, frm=1, to=2, **extra) -> dict:
    return {"type": "transition", "style": "crossfade", "from_deck": frm, "to_deck": to,
            "start_at": {"beat": beat}, "duration": {"beats": dur}} | extra


PLAYS = [_play(1, "a", 0, 24, 0), _play(2, "b", 0, 24, 0)]


def _windows(seg: dict):
    plan = _plan([seg])
    return transition_windows(plan.timeline[0], 120)


def _beats(w) -> tuple[float, float, str]:
    return (w.start_sec / SEC_PER_BEAT, w.end_sec / SEC_PER_BEAT, w.curve)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_curve_and_sides_parse() -> None:
    seg = _plan([_xfade(curve="linear", out={"offset": {"beats": -2}},
                        **{"in": {"duration": {"beats": 6}, "curve": "exponential"}})]).timeline[0]
    assert seg.curve == "linear"
    assert seg.out.offset.beats == -2
    assert seg.in_.curve == "exponential"


@pytest.mark.parametrize(
    "extra", [{"curve": "linear"}, {"out": {"duration": {"beats": 2}}}, {"in": {"curve": "linear"}}]
)
def test_v04_features_rejected_in_v03(extra) -> None:
    with pytest.raises(ValidationError, match="requires schema_version 0.4"):
        _plan([_xfade(**extra)], version="0.3")


def test_shaping_only_on_crossfades() -> None:
    with pytest.raises(ValidationError, match="only apply to crossfade"):
        _plan([_xfade(dur=0, style="cut", curve="linear")])


def test_unknown_curve_and_side_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        _plan([_xfade(curve="s_curve")])
    with pytest.raises(ValidationError):
        _plan([_xfade(out={"start_at": {"beat": 2}})])


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def test_default_windows_are_the_v03_crossfade() -> None:
    out_w, in_w = _windows(_xfade())
    assert _beats(out_w) == _beats(in_w) == (8, 12, "equal_power")


def test_curve_applies_to_both_sides() -> None:
    out_w, in_w = _windows(_xfade(curve="exponential"))
    assert out_w.curve == in_w.curve == "exponential"


def test_side_overrides() -> None:
    out_w, in_w = _windows(_xfade(
        curve="linear",
        out={"offset": {"beats": -2}, "curve": "equal_power"},
        **{"in": {"offset": {"beats": 1}, "duration": {"bars": 2}}},
    ))
    assert _beats(out_w) == (6, 10, "equal_power")  # duration from the transition
    assert _beats(in_w) == (9, 17, "linear")  # curve from the transition


def test_cut_is_a_step_on_both_sides() -> None:
    out_w, in_w = _windows(_xfade(dur=0, style="cut"))
    assert _beats(out_w) == _beats(in_w) == (8, 8, "step")


def test_after_a_transition_is_its_later_side() -> None:
    plan = _plan([*PLAYS, _xfade(id="x", **{"in": {"duration": {"beats": 10}}})])
    assert resolve_timeline(plan, 120).anchors["x"] == pytest.approx(18 * SEC_PER_BEAT)


def test_envelopes_use_each_sides_window_and_curve() -> None:
    plan = _plan([*PLAYS, _xfade(out={"curve": "linear"}, **{"in": {"duration": {"beats": 10}}})])
    envs = transition_envelopes(plan, 120)
    (out_r,), (in_r,) = envs[1].ramps, envs[2].ramps
    assert (out_r.start_value, out_r.end_value, out_r.shape) == (1.0, 0.0, "linear")
    assert (in_r.start_value, in_r.end_value, in_r.shape) == (0.0, 1.0, "equal_power")
    assert in_r.end_sec == pytest.approx(18 * SEC_PER_BEAT)
    assert envs[1].value_at(10 * SEC_PER_BEAT) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _issues(plan: Plan) -> list[str]:
    with pytest.raises(PlanValidationError) as e:
        validate_plan(plan)
    return e.value.issues


def test_valid_asymmetric_plan_passes() -> None:
    validate_plan(_plan([*PLAYS, _xfade(**{"in": {"duration": {"beats": 10}}})]))


def test_side_duration_must_be_positive() -> None:
    issues = _issues(_plan([*PLAYS, _xfade(out={"duration": {"beats": 0}})]))
    assert any("out fade duration must be positive" in s for s in issues)


def test_overlap_is_checked_per_side() -> None:
    # Deck 2's fade-in runs to beat 18, into the fade back out at beat 16.
    back = _xfade(beat=16, frm=2, to=1)
    issues = _issues(_plan([*PLAYS, _xfade(**{"in": {"duration": {"beats": 10}}}), back]))
    assert any(s.startswith("deck 2:") and "overlaps" in s for s in issues)
    # Deck 1's fade-out ends at beat 12, so deck 1 is fine.
    assert not any(s.startswith("deck 1:") for s in issues)


def test_inspect_shows_overrides() -> None:
    text = format_plan(_plan([*PLAYS, _xfade(curve="linear", **{"in": {"duration": {"beats": 10}}})]))
    assert "curve=linear  in=(duration 10 beats)" in text


# ---------------------------------------------------------------------------
# Render: a shaped transition sounds exactly like the equivalent lanes
# ---------------------------------------------------------------------------


def test_asymmetric_crossfade_renders_like_lanes(tmp_path) -> None:
    rng = np.random.default_rng(0)
    for name in ("a", "b"):
        noise = (0.3 * rng.standard_normal(30 * BEAT)).astype(np.float32)
        sf.write(tmp_path / f"{name}.wav", noise, SR, subtype="FLOAT")

    shaped = _plan([*PLAYS, _xfade(out={"curve": "exponential"},
                                    **{"in": {"offset": {"beats": 1}, "duration": {"beats": 10}}})])
    lanes = _plan(PLAYS, automation=[
        {"lane": "deck_volume", "deck": 1, "interpolation": "exponential",
         "keyframes": [{"at": {"beat": 8}, "value": 1.0}, {"at": {"beat": 12}, "value": 0.0}]},
        {"lane": "deck_volume", "deck": 2, "interpolation": "equal_power",
         "keyframes": [{"at": {"beat": 9}, "value": 0.0}, {"at": {"beat": 19}, "value": 1.0}]},
    ])
    a = NativeEngine().render(shaped, tmp_path).samples
    b = NativeEngine().render(lanes, tmp_path).samples
    np.testing.assert_allclose(a, b, atol=1e-6)
