"""Compiler tests: ramp shapes, keyframe → envelope, transition expansion, crossfader law."""

from __future__ import annotations

import numpy as np
import pytest

from dj_segue.compiler import (
    EXP_FLOOR,
    Envelope,
    Ramp,
    crossfader_envelope,
    crossfader_gains,
    keyframes_to_envelope,
    shape_value,
    transition_envelopes,
)
from dj_segue.executor.native.curves import render_envelope
from dj_segue.schema.plan import FloatKeyframe, Plan

SR = 1000  # small rate keeps sample math readable


def _kf(beat: float, value: float) -> FloatKeyframe:
    return FloatKeyframe.model_validate({"at": {"beat": beat}, "value": value})


def _plan(timeline: list[dict], automation: list[dict] | None = None) -> Plan:
    return Plan.model_validate(
        {
            "schema_version": "0.1",
            "meta": {"mix_name": "t", "mix_tempo": 60},  # 1 beat = 1 s
            "tracks": {"a": {"path": "a.wav", "bpm": 60}},
            "decks": {"1": {}, "2": {}, "3": {}},
            "timeline": timeline,
            "automation": automation or [],
        }
    )


def _xfade(frm: int, to: int, beat: float, dur: float, style: str = "crossfade") -> dict:
    return {
        "type": "transition", "style": style, "from_deck": frm, "to_deck": to,
        "start_at": {"beat": beat}, "duration": {"beats": dur},
    }


# ---------------------------------------------------------------------------
# shape_value
# ---------------------------------------------------------------------------


def test_linear_midpoint() -> None:
    assert shape_value("linear", 0.0, 1.0, 0.5) == pytest.approx(0.5)


def test_step_holds_start_value() -> None:
    assert shape_value("step", 1.0, 0.0, 0.99) == 1.0


def test_equal_power_sums_to_unit_power() -> None:
    t = np.linspace(0, 1, 101)
    out = shape_value("equal_power", 1.0, 0.0, t)
    inn = shape_value("equal_power", 0.0, 1.0, t)
    np.testing.assert_allclose(out**2 + inn**2, 1.0, atol=1e-12)
    assert inn[50] == pytest.approx(np.sqrt(0.5))


def test_exponential_is_linear_in_db() -> None:
    # 1.0 → 0.01 is 0 dB → -40 dB; halfway is -20 dB = 0.1.
    assert shape_value("exponential", 1.0, 0.01, 0.5) == pytest.approx(0.1)


def test_exponential_to_zero_uses_floor() -> None:
    # A fade to 0 is a fade to -60 dB: halfway is -30 dB.
    assert shape_value("exponential", 1.0, 0.0, 0.5) == pytest.approx(np.sqrt(EXP_FLOOR))


def test_exponential_sign_change_falls_back_to_linear() -> None:
    assert shape_value("exponential", -1.0, 1.0, 0.25) == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# keyframes → envelope → curve
# ---------------------------------------------------------------------------


def test_step_keyframes_jump_at_next_keyframe() -> None:
    env = keyframes_to_envelope([_kf(0, 1.0), _kf(2, 0.0)], "step", 60.0, "t")
    curve = render_envelope(env, SR, 3 * SR)
    assert curve[2 * SR - 1] == 1.0
    assert curve[2 * SR] == 0.0


def test_linear_keyframes_hold_before_first_and_after_last() -> None:
    env = keyframes_to_envelope([_kf(1, 0.0), _kf(2, 1.0)], "linear", 60.0, "t")
    curve = render_envelope(env, SR, 3 * SR)
    assert curve[0] == 0.0
    assert curve[SR + SR // 2] == pytest.approx(0.5)
    assert curve[-1] == 1.0


def test_ramp_starting_before_zero_keeps_phase() -> None:
    # Ramp spans -1s..1s; at sample 0 we're halfway, not at the start.
    env = Envelope(initial=0.0, ramps=(Ramp(-1.0, 1.0, 0.0, 1.0, "linear"),), source="t")
    curve = render_envelope(env, SR, 2 * SR)
    assert curve[0] == pytest.approx(0.5)


def test_value_at_matches_rendered_curve() -> None:
    env = keyframes_to_envelope(
        [_kf(0, 1.0), _kf(1, 0.2), _kf(2, 0.2), _kf(3, 1.0)], "exponential", 60.0, "t"
    )
    curve = render_envelope(env, SR, 4 * SR)
    for sec in (0.0, 0.3, 1.5, 2.7, 3.5):
        assert curve[int(sec * SR)] == pytest.approx(env.value_at(sec), rel=1e-5)


# ---------------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------------


def test_crossfade_expands_to_opposing_equal_power_ramps() -> None:
    envs = transition_envelopes(_plan([_xfade(1, 2, 4, 2)]), 60.0)
    assert envs[1].initial == 1.0 and envs[2].initial == 0.0
    assert envs[1].ramps == (Ramp(4.0, 6.0, 1.0, 0.0, "equal_power"),)
    assert envs[2].ramps == (Ramp(4.0, 6.0, 0.0, 1.0, "equal_power"),)


def test_cut_expands_to_instant_jump() -> None:
    envs = transition_envelopes(_plan([_xfade(1, 2, 4, 0, style="cut")]), 60.0)
    assert envs[1].ramps == (Ramp(4.0, 4.0, 1.0, 0.0, "step"),)
    assert envs[1].value_at(3.999) == 1.0
    assert envs[1].value_at(4.0) == 0.0


def test_back_and_forth_transitions_chain_per_deck() -> None:
    envs = transition_envelopes(_plan([_xfade(2, 1, 8, 1), _xfade(1, 2, 2, 1)]), 60.0)
    # Sorted by time regardless of timeline order: deck 1 out @2, in @8.
    assert [r.start_sec for r in envs[1].ramps] == [2.0, 8.0]
    assert envs[1].value_at(5.0) == 0.0
    assert envs[1].value_at(10.0) == 1.0


def test_untouched_deck_has_no_transition_envelope() -> None:
    envs = transition_envelopes(_plan([_xfade(1, 2, 4, 2)]), 60.0)
    assert 3 not in envs


def test_vocal_handoff_is_not_compiled_here() -> None:
    assert transition_envelopes(_plan([_xfade(1, 2, 4, 2, "vocal_handoff")]), 60.0) == {}


# ---------------------------------------------------------------------------
# crossfader
# ---------------------------------------------------------------------------


def test_crossfader_law_endpoints_and_center() -> None:
    g1, g2 = crossfader_gains(np.array([-1.0, 0.0, 1.0]))
    np.testing.assert_allclose(g1, [1.0, np.sqrt(0.5), 0.0], atol=1e-12)
    np.testing.assert_allclose(g2, [0.0, np.sqrt(0.5), 1.0], atol=1e-12)


def test_no_crossfader_lane_means_none() -> None:
    assert crossfader_envelope(_plan([]), 60.0) is None


def test_crossfader_lane_without_keyframes_defaults_to_center() -> None:
    plan = _plan([], [{"lane": "crossfader", "keyframes": []}])
    assert crossfader_envelope(plan, 60.0).initial == 0.0
