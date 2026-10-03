"""M5 EQ: 3-band isolator on decks with `eq` lanes.

Tracks are sums of sines; each band's level is read by projecting a window
of the output onto the sine's frequency.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from dj_segue.executor.native import NativeEngine
from dj_segue.executor.native.eq import split_bands
from dj_segue.schema.plan import Plan
from dj_segue.schema.validator import PlanValidationError, validate_plan

SR = 44100
BPM = 120.0
BEAT = SR // 2
BASS_HZ, VOCAL_HZ, HAT_HZ = 60.0, 1000.0, 8000.0


def _amp(x: np.ndarray, hz: float, start: int, n: int = SR // 2) -> float:
    """Amplitude of the `hz` sine in x[start:start+n] (whole cycles)."""
    seg = x[start : start + n]
    t = np.arange(start, start + n) / SR
    return 2 / n * abs(np.sum(seg * np.exp(-2j * np.pi * hz * t)))


def _db(a: float, b: float) -> float:
    return 20 * np.log10(a / b)


def _write(root: Path) -> None:
    t = np.arange(30 * SR) / SR
    x = sum(0.2 * np.sin(2 * np.pi * hz * t) for hz in (BASS_HZ, VOCAL_HZ, HAT_HZ))
    sf.write(root / "mix.wav", x.astype(np.float32), SR, subtype="FLOAT")
    sf.write(root / "other.wav", np.zeros(30 * SR, dtype=np.float32), SR, subtype="FLOAT")


def _lane(band: str, *kfs: tuple[float, float], deck: int = 1) -> dict:
    return {"lane": "eq", "deck": deck, "band": band,
            "keyframes": [{"at": {"beat": b}, "value_db": v} for b, v in kfs]}


def _plan(automation: list[dict], timeline: list[dict] | None = None,
          version: str = "0.1") -> Plan:
    return Plan.model_validate({
        "schema_version": version,
        "meta": {"mix_name": "eq", "mix_tempo": BPM},
        "tracks": {"mix": {"path": "mix.wav", "bpm": BPM},
                   "other": {"path": "other.wav", "bpm": BPM}},
        "decks": {"1": {}, "2": {}},
        "timeline": timeline or [
            {"type": "play", "deck": 1, "track": "mix",
             "from": {"beat": 0}, "to": {"beat": 24}, "start_at": {"beat": 0}},
        ],
        "automation": automation,
    })


def _render(plan: Plan, root: Path) -> np.ndarray:
    _write(root)
    return NativeEngine(limited=False).render(plan, root).samples[:, 0]


def test_acceptance_kill_the_lows_over_4_beats_vocals_intact(tmp_path) -> None:
    """M5 acceptance: the outgoing track's lows go over its last 4 beats
    (8 → 12) of a crossfade into deck 2, while the mids (vocals) stay."""
    timeline = [
        {"type": "play", "deck": 1, "track": "mix",
         "from": {"beat": 0}, "to": {"beat": 16}, "start_at": {"beat": 0}},
        {"type": "play", "deck": 2, "track": "other",
         "from": {"beat": 0}, "to": {"beat": 16}, "start_at": {"beat": 8}},
        {"type": "transition", "style": "cut", "from_deck": 1, "to_deck": 2,
         "start_at": {"beat": 14}, "duration": {"beats": 0}},
    ]
    out = _render(_plan([_lane("low", (8, 0), (12, -24))], timeline), tmp_path)

    before, after = 2 * BEAT, 12 * BEAT + BEAT // 4  # after: settled at -24 dB
    assert _db(_amp(out, BASS_HZ, after), _amp(out, BASS_HZ, before)) == pytest.approx(-24, abs=1)
    for hz in (VOCAL_HZ, HAT_HZ):
        assert abs(_db(_amp(out, hz, after), _amp(out, hz, before))) < 0.1
    # Halfway through the kill (linear in dB): about -12 dB.
    mid = 10 * BEAT - SR // 20
    assert _db(_amp(out, BASS_HZ, mid, SR // 10), _amp(out, BASS_HZ, before)) == pytest.approx(
        -12, abs=1.5
    )


def test_each_band_moves_only_its_range(tmp_path) -> None:
    out = _render(_plan([_lane("mid", (0, -24)), _lane("high", (0, 6))]), tmp_path)
    at = 4 * BEAT
    assert _db(_amp(out, BASS_HZ, at), 0.2) == pytest.approx(0, abs=0.2)
    # 1 kHz sits 2.5x below the 2.5 kHz crossover, where the high band still
    # passes it at about -32 dB, so a -24 dB mid cut measures about -19 dB.
    assert -24.5 < _db(_amp(out, VOCAL_HZ, at), 0.2) < -18
    assert _db(_amp(out, HAT_HZ, at), 0.2) == pytest.approx(6, abs=0.2)


def test_flat_eq_keeps_every_level(tmp_path) -> None:
    out = _render(_plan([_lane("low", (0, 0))]), tmp_path)
    for hz in (BASS_HZ, VOCAL_HZ, HAT_HZ):
        assert _db(_amp(out, hz, 4 * BEAT), 0.2) == pytest.approx(0, abs=0.01)


def test_bands_sum_flat() -> None:
    x = np.zeros((1 << 15, 2))
    x[0] = 1.0
    total, _, _ = split_bands(x, SR)
    mag = np.abs(np.fft.rfft(total[:, 0]))
    assert np.allclose(mag, 1.0, atol=1e-5)


def test_only_eq_decks_are_split(tmp_path) -> None:
    _write(tmp_path)
    timeline = [
        {"type": "play", "deck": 1, "track": "mix",
         "from": {"beat": 0}, "to": {"beat": 8}, "start_at": {"beat": 0}},
        {"type": "play", "deck": 2, "track": "other",
         "from": {"beat": 0}, "to": {"beat": 8}, "start_at": {"beat": 0}},
    ]
    parts = NativeEngine().render_decks(_plan([_lane("low", (0, -6), deck=2)], timeline), tmp_path)
    assert set(parts.bands) == {2}


def test_excerpt_equals_full_render(tmp_path) -> None:
    _write(tmp_path)
    plan = _plan([_lane("low", (4, 0), (8, -24)), _lane("high", (6, 0), (10, 6))])
    eng = NativeEngine()
    parts = eng.render_decks(plan, tmp_path)
    gains, eq = eng.deck_gains(plan, parts), eng.deck_eq(plan, parts)
    full = eng.render(plan, tmp_path).samples
    for a, b in [(0, 999), (7 * BEAT, 9 * BEAT), (20 * BEAT, 24 * BEAT)]:
        np.testing.assert_array_equal(parts.mix(gains, a, b, eq=eq), full[a:b])


def test_echo_repeats_what_the_eq_let_through(tmp_path) -> None:
    timeline = [
        {"type": "play", "deck": 1, "track": "mix",
         "from": {"beat": 0}, "to": {"beat": 8}, "start_at": {"beat": 0}},
        {"type": "play", "deck": 2, "track": "other",
         "from": {"beat": 0}, "to": {"beat": 16}, "start_at": {"beat": 8}},
        {"type": "transition", "style": "echo_out", "from_deck": 1, "to_deck": 2,
         "start_at": {"beat": 8}, "duration": {"beats": 4},
         "echo": {"every": {"beats": 1}, "feedback": 0.5}},
    ]
    out = _render(_plan([_lane("low", (4, 0), (6, -24))], timeline, version="0.6"), tmp_path)
    rep = 8 * BEAT + BEAT // 4  # inside the first repeat
    assert _db(_amp(out, BASS_HZ, rep, BEAT // 2), 0.2) < -20
    assert _db(_amp(out, VOCAL_HZ, rep, BEAT // 2), 0.2) == pytest.approx(0, abs=0.2)


def test_eq_range_and_one_lane_per_band() -> None:
    with pytest.raises(PlanValidationError, match="outside -24 to \\+12 dB"):
        validate_plan(_plan([_lane("low", (0, -40))]))
    with pytest.raises(PlanValidationError, match="second eq lane"):
        validate_plan(_plan([_lane("low", (0, 0)), _lane("low", (4, -6))]))
    validate_plan(_plan([_lane("low", (0, -24)), _lane("high", (0, 12))]))
