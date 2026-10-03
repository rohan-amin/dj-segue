"""Schema v0.7: the `filter` lane (DJ filter knob) and `echo.highpass`."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from dj_segue.executor.native import NativeEngine
from dj_segue.schema.plan import Plan
from dj_segue.schema.validator import PlanValidationError, validate_plan

SR = 44100
BPM = 120.0
BEAT = SR // 2
BASS_HZ, VOCAL_HZ = 60.0, 1000.0


def _amp(x: np.ndarray, hz: float, start: int, n: int = SR // 2) -> float:
    seg = x[start : start + n]
    t = np.arange(start, start + n) / SR
    return 2 / n * abs(np.sum(seg * np.exp(-2j * np.pi * hz * t)))


def _db(a: float, b: float = 0.25) -> float:
    return 20 * np.log10(a / b)


def _write(root: Path) -> None:
    t = np.arange(30 * SR) / SR
    x = 0.25 * np.sin(2 * np.pi * BASS_HZ * t) + 0.25 * np.sin(2 * np.pi * VOCAL_HZ * t)
    sf.write(root / "mix.wav", x.astype(np.float32), SR, subtype="FLOAT")
    sf.write(root / "other.wav", np.zeros(30 * SR, dtype=np.float32), SR, subtype="FLOAT")


def _filter(*kfs: tuple[float, float], deck: int = 1) -> dict:
    return {"lane": "filter", "deck": deck,
            "keyframes": [{"at": {"beat": b}, "value": v} for b, v in kfs]}


PLAY = {"type": "play", "deck": 1, "track": "mix",
        "from": {"beat": 0}, "to": {"beat": 24}, "start_at": {"beat": 0}}


def _plan(automation: list[dict], timeline: list[dict] | None = None,
          version: str = "0.7") -> Plan:
    return Plan.model_validate({
        "schema_version": version,
        "meta": {"mix_name": "filter", "mix_tempo": BPM},
        "tracks": {"mix": {"path": "mix.wav", "bpm": BPM},
                   "other": {"path": "other.wav", "bpm": BPM}},
        "decks": {"1": {}, "2": {}},
        "timeline": timeline or [PLAY],
        "automation": automation,
    })


def _render(plan: Plan, root: Path) -> np.ndarray:
    _write(root)
    return NativeEngine(limited=False).render(plan, root).samples[:, 0]


# --- schema / validation ----------------------------------------------------


def test_filter_lane_requires_v07() -> None:
    with pytest.raises(ValidationError, match="filter lane requires schema_version 0.7"):
        _plan([_filter((0, 0))], version="0.6")


def test_filter_range_and_one_lane_per_deck() -> None:
    with pytest.raises(PlanValidationError, match="outside -1 to \\+1"):
        validate_plan(_plan([_filter((0, 1.5))]))
    with pytest.raises(PlanValidationError, match="second filter lane"):
        validate_plan(_plan([_filter((0, 0)), _filter((4, -1))]))
    validate_plan(_plan([_filter((0, -1), (8, 1))]))


# --- sound ----------------------------------------------------------------------


def test_centre_is_exactly_dry(tmp_path) -> None:
    dry = _render(_plan([]), tmp_path)
    assert np.array_equal(_render(_plan([_filter((0, 0))]), tmp_path), dry)


def test_low_pass_left_high_pass_right(tmp_path) -> None:
    lp = _render(_plan([_filter((0, -1))]), tmp_path)
    assert _db(_amp(lp, BASS_HZ, 4 * BEAT)) > -2
    assert _db(_amp(lp, VOCAL_HZ, 4 * BEAT)) < -40
    hp = _render(_plan([_filter((0, 1))]), tmp_path)
    assert _db(_amp(hp, BASS_HZ, 4 * BEAT)) < -60
    assert _db(_amp(hp, VOCAL_HZ, 4 * BEAT)) < -35  # 10 kHz cutoff: 1 kHz goes too
    half = _render(_plan([_filter((0, 0.5))]), tmp_path)  # cutoff ~450 Hz
    assert _db(_amp(half, BASS_HZ, 4 * BEAT)) < -30
    assert _db(_amp(half, VOCAL_HZ, 4 * BEAT)) == pytest.approx(0, abs=1)


def test_sweep_through_centre_is_smooth(tmp_path) -> None:
    out = _render(_plan([_filter((2, -1), (10, 1))]), tmp_path)
    t = np.arange(len(out)) / SR
    x = 0.25 * np.sin(2 * np.pi * BASS_HZ * t) + 0.25 * np.sin(2 * np.pi * VOCAL_HZ * t)
    assert np.abs(np.diff(out)).max() < 1.2 * np.abs(np.diff(x)).max()


def test_excerpt_equals_full_render(tmp_path) -> None:
    _write(tmp_path)
    plan = _plan([_filter((2, 0), (6, -1)),
                  {"lane": "eq", "deck": 1, "band": "high",
                   "keyframes": [{"at": {"beat": 0}, "value_db": -6}]}])
    eng = NativeEngine()
    parts = eng.render_decks(plan, tmp_path)
    levels = eng.levels(plan, parts)
    full = eng.render(plan, tmp_path).samples
    for a, b in [(0, 999), (5 * BEAT, 7 * BEAT), (20 * BEAT, 24 * BEAT)]:
        np.testing.assert_array_equal(parts.mix(levels, a, b), full[a:b])


# --- echo.highpass ---------------------------------------------------------------


def _echo_plan(**echo) -> Plan:
    timeline = [
        {"type": "play", "deck": 1, "track": "mix",
         "from": {"beat": 0}, "to": {"beat": 8}, "start_at": {"beat": 0}},
        {"type": "play", "deck": 2, "track": "other",
         "from": {"beat": 0}, "to": {"beat": 16}, "start_at": {"beat": 8}},
        {"type": "transition", "style": "echo_out", "from_deck": 1, "to_deck": 2,
         "start_at": {"beat": 8}, "duration": {"beats": 8},
         "echo": {"every": {"beats": 1}, "feedback": 1.0, **echo}},
    ]
    return _plan([], timeline)


def test_echo_highpass_requires_v07() -> None:
    raw = _echo_plan(highpass=0.5).model_dump(by_alias=True, exclude_unset=True)
    raw["schema_version"] = "0.6"
    with pytest.raises(ValidationError, match="echo.highpass requires schema_version 0.7"):
        Plan.model_validate(raw)


def test_echo_highpass_thins_the_tail(tmp_path) -> None:
    plain = _render(_echo_plan(), tmp_path)
    swept = _render(_echo_plan(highpass=0.8), tmp_path)
    first, late = 8 * BEAT + BEAT // 4, 14 * BEAT + BEAT // 4  # 1st and 7th repeats
    n = BEAT // 2
    # Near the cut the knob is near 0: almost no change.
    assert _db(_amp(swept, BASS_HZ, first, n), _amp(plain, BASS_HZ, first, n)) > -3
    # Late in the tail (knob ~0.7, cutoff ~1.6 kHz) the bass is gone, the
    # mids thinned but there.
    assert _db(_amp(swept, BASS_HZ, late, n), _amp(plain, BASS_HZ, late, n)) < -30
    assert _db(_amp(swept, VOCAL_HZ, late, n), _amp(plain, VOCAL_HZ, late, n)) > -12
