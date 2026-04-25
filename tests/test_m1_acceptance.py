"""M1 acceptance test: render hello_mix and assert on the output.

Per architecture.md: integration tests render plans to WAV and assert on output
(sample counts, RMS levels, expected silence ranges, frequency content).
No tests require a sound card.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from dj_segue.executor.native import NativeEngine
from dj_segue.schema import load_plan

REPO_ROOT = Path(__file__).resolve().parent.parent
SR = 44100  # all M1 fixtures are 44.1kHz


@pytest.fixture(scope="module")
def rendered_hello_mix(tmp_path_factory):
    plan = load_plan(REPO_ROOT / "examples" / "hello_mix.plan.jsonc")
    out_path = tmp_path_factory.mktemp("m1") / "hello.wav"
    result = NativeEngine().render_to_wav(plan, REPO_ROOT, out_path)
    return result, out_path


# ---------------------------------------------------------------------------
# Shape and dtype
# ---------------------------------------------------------------------------


def test_total_duration_is_32_seconds(rendered_hello_mix) -> None:
    result, _ = rendered_hello_mix
    # Last play segment ends at mix-beat 64 (track_b plays 32 track-beats
    # starting at mix-beat 32). At mix_tempo 120, that's 32 seconds exactly.
    assert result.samples.shape == (32 * SR, 2)
    assert result.sample_rate == SR
    assert result.duration_sec == pytest.approx(32.0, abs=1.0 / SR)


def test_output_is_stereo_float32(rendered_hello_mix) -> None:
    result, _ = rendered_hello_mix
    assert result.samples.dtype == np.float32
    assert result.samples.ndim == 2
    assert result.samples.shape[1] == 2


def test_no_clipping(rendered_hello_mix) -> None:
    result, _ = rendered_hello_mix
    peak = float(np.abs(result.samples).max())
    assert peak <= 1.0, f"clipping: peak={peak}"
    assert peak >= 0.4, f"output suspiciously quiet: peak={peak}"


# ---------------------------------------------------------------------------
# RMS in beat-windows
# ---------------------------------------------------------------------------


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def test_track_a_window_full_volume(rendered_hello_mix) -> None:
    """Beats 0..32 (samples 0..705_600): track A at full volume."""
    result, _ = rendered_hello_mix
    rms = _rms(result.samples[: 16 * SR])
    # 0.5-amplitude sine → RMS = 0.5 / sqrt(2) ≈ 0.354
    assert rms == pytest.approx(0.354, abs=0.02)


def test_fadein_window_partial_volume(rendered_hello_mix) -> None:
    """Beats 32..36 (samples 705_600..793_800): track B linear fade-in."""
    result, _ = rendered_hello_mix
    rms = _rms(result.samples[16 * SR : 18 * SR])
    # Linear ramp 0→1 modulating a 0.5-amplitude sine:
    # RMS = (0.5/√2) * √(∫ t² dt from 0 to 1) = 0.354 * √(1/3) ≈ 0.204
    assert rms == pytest.approx(0.204, abs=0.02)


def test_track_b_window_full_volume(rendered_hello_mix) -> None:
    """Beats 36..64 (samples 793_800..1_411_200): track B at full volume."""
    result, _ = rendered_hello_mix
    rms = _rms(result.samples[18 * SR : 32 * SR])
    assert rms == pytest.approx(0.354, abs=0.02)


def test_track_a_silent_immediately_after_step_cut(rendered_hello_mix) -> None:
    """At sample 705_700 (100 samples past mix-beat 32), track A's step
    automation has cut to 0 and track B is barely above 0 in the fade-in.
    Output should be near-silent."""
    result, _ = rendered_hello_mix
    sample = result.samples[16 * SR + 100]
    assert float(np.abs(sample).max()) < 0.01


# ---------------------------------------------------------------------------
# Frequency content (the hard assertion: right track in right window)
# ---------------------------------------------------------------------------


def _bin_magnitudes(window: np.ndarray, freqs_hz: list[float]) -> list[float]:
    spectrum = np.abs(np.fft.rfft(window))
    freqs = np.fft.rfftfreq(len(window), d=1.0 / SR)
    return [float(spectrum[int(np.argmin(np.abs(freqs - f)))]) for f in freqs_hz]


def test_track_a_dominates_early_window(rendered_hello_mix) -> None:
    """In [1s, 5s) only track A (440 Hz) should be playing — not track B (660 Hz)."""
    result, _ = rendered_hello_mix
    window = result.samples[SR : 5 * SR, 0]
    mag_440, mag_660 = _bin_magnitudes(window, [440.0, 660.0])
    assert mag_440 > 50 * mag_660, f"A/B FFT bin ratio = {mag_440 / max(mag_660, 1e-9):.1f}"


def test_track_b_dominates_late_window(rendered_hello_mix) -> None:
    """In [20s, 25s) only track B (660 Hz) should be playing — not track A (440 Hz)."""
    result, _ = rendered_hello_mix
    window = result.samples[20 * SR : 25 * SR, 0]
    mag_440, mag_660 = _bin_magnitudes(window, [440.0, 660.0])
    assert mag_660 > 50 * mag_440, f"B/A FFT bin ratio = {mag_660 / max(mag_440, 1e-9):.1f}"


# ---------------------------------------------------------------------------
# WAV file integrity
# ---------------------------------------------------------------------------


def test_wav_file_is_readable_and_matches(rendered_hello_mix) -> None:
    result, out_path = rendered_hello_mix
    data, sr = sf.read(out_path)
    assert sr == SR
    assert data.shape == (32 * SR, 2)
    # PCM_16 quantization → not bit-identical to float, but very close in RMS.
    assert _rms(data) == pytest.approx(_rms(result.samples), abs=0.005)
