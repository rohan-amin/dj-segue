"""Schema v0.5: drawn curves (`{points, smooth}`) on a crossfade side."""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from dj_segue.compiler import Points, shape_value, transition_envelopes
from dj_segue.executor.native import NativeEngine
from dj_segue.inspect import format_plan
from dj_segue.schema import PlanValidationError, validate_plan
from dj_segue.schema.plan import Plan

SR = 44100
BEAT = SR // 2
SPB = 0.5

DIP_IN = {"points": [[0, 0], [0.5, 0.1], [0.75, 0.6], [1, 1]]}


def _plan(in_curve=None, out_curve=None, version="0.5") -> Plan:
    xf = {"type": "transition", "style": "crossfade", "from_deck": 1, "to_deck": 2,
          "start_at": {"beat": 8}, "duration": {"beats": 8}}
    if in_curve is not None:
        xf["in"] = {"curve": in_curve}
    if out_curve is not None:
        xf["out"] = {"curve": out_curve}
    return Plan.model_validate({
        "schema_version": version,
        "meta": {"mix_name": "t", "mix_tempo": 120},
        "tracks": {"a": {"path": "a.wav", "bpm": 120}, "b": {"path": "b.wav", "bpm": 120}},
        "decks": {"1": {}, "2": {}},
        "timeline": [
            {"type": "play", "deck": 1, "track": "a", "from": {"beat": 0}, "to": {"beat": 24},
             "start_at": {"beat": 0}},
            {"type": "play", "deck": 2, "track": "b", "from": {"beat": 0}, "to": {"beat": 24},
             "start_at": {"beat": 0}},
            xf,
        ],
    })


# -- schema -----------------------------------------------------------------


def test_drawn_curve_parses() -> None:
    seg = _plan(DIP_IN).timeline[2]
    assert seg.in_.curve.points[1] == (0.5, 0.1)
    assert seg.in_.curve.smooth is False


def test_drawn_curve_needs_v05() -> None:
    with pytest.raises(ValidationError, match="requires schema_version 0.5"):
        _plan(DIP_IN, version="0.4")


@pytest.mark.parametrize(
    "points, match",
    [
        ([[0, 0], [0.5, 1]], "start at t=0 and end at t=1"),
        ([[0, 0], [0.6, 0.5], [0.4, 0.6], [1, 1]], "strictly increase"),
        ([[0, 0], [0.5, 1.2], [1, 1]], "within 0–1"),
        ([[0, 0.2], [1, 1]], "from 0 to 1"),
    ],
)
def test_bad_points_rejected(points, match) -> None:
    with pytest.raises(ValidationError, match=match):
        _plan({"points": points})


def test_out_curve_must_fall() -> None:
    with pytest.raises(ValidationError, match="`out` curve must go from 1 to 0"):
        _plan(out_curve={"points": [[0, 0], [1, 1]]})


def test_transition_level_curve_stays_a_preset() -> None:
    with pytest.raises(ValidationError):
        Plan.model_validate({**_plan().model_dump(by_alias=True, exclude_none=True),
                             "timeline": [{"type": "transition", "style": "crossfade",
                                           "from_deck": 1, "to_deck": 2,
                                           "start_at": {"beat": 8}, "duration": {"beats": 8},
                                           "curve": DIP_IN}]})


# -- evaluation -------------------------------------------------------------


def test_straight_points() -> None:
    shape = Points(((0.0, 0.0), (0.5, 0.1), (1.0, 1.0)))
    np.testing.assert_allclose(shape_value(shape, 0, 1, np.array([0, 0.25, 0.5, 0.75])),
                               [0, 0.05, 0.1, 0.55])


def test_smooth_points_pass_through_and_never_overshoot() -> None:
    shape = Points(((0.0, 0.0), (0.3, 0.9), (0.6, 0.9), (1.0, 1.0)), smooth=True)
    t = np.linspace(0, 1, 1001)
    g = shape_value(shape, 0, 1, t)
    assert g.min() >= 0 and g.max() <= 1
    np.testing.assert_allclose(shape_value(shape, 0, 1, np.array([0.3, 0.6])), [0.9, 0.9])
    # Flat between two equal points (monotone cubic), not a bump.
    assert np.all(np.abs(g[(t >= 0.3) & (t <= 0.6)] - 0.9) < 1e-9)


def test_envelope_uses_the_drawn_curve() -> None:
    env = transition_envelopes(_plan(DIP_IN), 120)[2]
    assert env.value_at(8 * SPB) == 0
    assert env.value_at(12 * SPB) == pytest.approx(0.1)  # t = 0.5
    assert env.value_at(14 * SPB) == pytest.approx(0.6)  # t = 0.75


# -- validation, inspect, render ---------------------------------------------


def test_points_closer_than_2ms_rejected() -> None:
    # 8 beats = 4 s; 0.0004 of it = 1.6 ms.
    plan = _plan({"points": [[0, 0], [0.5, 0], [0.5004, 1], [1, 1]]})
    with pytest.raises(PlanValidationError) as e:
        validate_plan(plan)
    assert any("1.60 ms apart" in s for s in e.value.issues)
    validate_plan(_plan({"points": [[0, 0], [0.5, 0], [0.5005, 1], [1, 1]]}))  # 2 ms: fine


def test_inspect_summarizes_drawn_curves() -> None:
    assert "in=(drawn: 4 points, straight)" in format_plan(_plan(DIP_IN))


def test_drawn_curve_renders_as_its_gains(tmp_path) -> None:
    sf.write(tmp_path / "a.wav", np.zeros(30 * BEAT, np.float32), SR, subtype="FLOAT")
    sf.write(tmp_path / "b.wav", np.full(30 * BEAT, 0.5, np.float32), SR, subtype="FLOAT")
    out = NativeEngine().render(_plan(DIP_IN), tmp_path).samples[:, 0]
    start = 8 * BEAT
    for t, g in ((0.5, 0.1), (0.75, 0.6)):
        assert out[start + int(t * 8 * BEAT)] == pytest.approx(0.5 * g, abs=1e-4)
    assert out[17 * BEAT] == pytest.approx(0.5)
