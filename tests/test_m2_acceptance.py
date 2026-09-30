"""M2 acceptance test: render crossfade_mix (3 tracks, 2 crossfades) and assert on output.

Like M1, this asserts properties rather than a byte-identical golden WAV:
duration, frequency dominance per section, both tracks present at the
crossfade midpoint, and constant loudness through each equal-power crossfade.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dj_segue.executor.native import NativeEngine
from dj_segue.schema import load_plan, validate_against_audio, validate_plan

REPO_ROOT = Path(__file__).resolve().parent.parent
SR = 44100
FULL_RMS = 0.5 / np.sqrt(2)  # fixture sines have amplitude 0.5
FREQS = {"a": 440.0, "b": 660.0, "c": 550.0}


def _sec(beat: float) -> float:
    return beat * 0.5  # mix_tempo 120


@pytest.fixture(scope="module")
def mix() -> np.ndarray:
    plan = load_plan(REPO_ROOT / "examples" / "crossfade_mix.plan.jsonc")
    validate_plan(plan)
    validate_against_audio(plan, {t: 17.0 for t in plan.tracks})
    return NativeEngine().render(plan, REPO_ROOT).samples[:, 0].astype(np.float64)


def _window(mix: np.ndarray, beat: float, width_sec: float = 0.25) -> np.ndarray:
    s = int(_sec(beat) * SR)
    return mix[s : s + int(width_sec * SR)]


def _band_energy(x: np.ndarray, freq: float) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    fbins = np.fft.rfftfreq(len(x), 1 / SR)
    return float(spec[np.abs(fbins - freq) < 10].sum())


def _dominant(x: np.ndarray) -> str:
    return max(FREQS, key=lambda k: _band_energy(x, FREQS[k]))


def test_duration_is_80_beats(mix) -> None:
    assert len(mix) == 40 * SR


def test_no_clipping(mix) -> None:
    assert np.max(np.abs(mix)) <= 1.0


@pytest.mark.parametrize(
    ("beat", "track"),
    [(4, "a"), (20, "a"), (36, "b"), (44, "b"), (60, "c"), (76, "c")],
)
def test_expected_track_dominates_each_section(mix, beat, track) -> None:
    assert _dominant(_window(mix, beat)) == track


@pytest.mark.parametrize(("mid_beat", "out", "inn"), [(28, "a", "b"), (52, "b", "c")])
def test_crossfade_midpoint_has_both_tracks_equally(mix, mid_beat, out, inn) -> None:
    w = _window(mix, mid_beat - 0.25, 0.25)  # centered on the midpoint
    e_out, e_in = _band_energy(w, FREQS[out]), _band_energy(w, FREQS[inn])
    assert e_out == pytest.approx(e_in, rel=0.05)


@pytest.mark.parametrize("start_beat", [24, 48])
def test_equal_power_crossfade_keeps_loudness_constant(mix, start_beat) -> None:
    # Sines at different frequencies are uncorrelated, so with g1² + g2² = 1
    # the RMS stays at full level throughout — no mid-fade dip.
    for b in np.arange(start_beat, start_beat + 8, 0.5):
        rms = np.sqrt(np.mean(_window(mix, b) ** 2))
        assert rms == pytest.approx(FULL_RMS, rel=0.03), f"beat {b}"


def test_outgoing_track_fully_gone_after_crossfade(mix) -> None:
    w = _window(mix, 33)
    assert _band_energy(w, FREQS["a"]) < 1e-3 * _band_energy(w, FREQS["b"])
