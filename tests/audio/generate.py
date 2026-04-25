"""Generate the M1 test audio fixtures.

Sine tones at 120 BPM context: 32 beats = 16 seconds. We render 17 seconds
to give a small head-room above the example plan's `to: beat 32` boundary.

Run from the repo root: `python tests/audio/generate.py`. The output WAVs
are checked in so tests don't need to regenerate them.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 44_100
DURATION_SEC = 17.0
AMPLITUDE = 0.5  # leave headroom; -6 dBFS roughly

FIXTURES = {
    "sine_120bpm_a.wav": 440.0,  # A4
    "sine_120bpm_b.wav": 660.0,  # E5 (perfect fifth above A4)
}


def render_sine(freq_hz: float, *, sample_rate: int, duration_sec: float) -> np.ndarray:
    n = int(round(sample_rate * duration_sec))
    t = np.arange(n, dtype=np.float64) / sample_rate
    tone = AMPLITUDE * np.sin(2.0 * np.pi * freq_hz * t)

    # 5 ms cosine ramp on each end to avoid click artifacts.
    ramp_n = int(0.005 * sample_rate)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, ramp_n))
    tone[:ramp_n] *= ramp
    tone[-ramp_n:] *= ramp[::-1]
    return tone.astype(np.float32)


def main() -> None:
    out_dir = Path(__file__).parent
    for filename, freq in FIXTURES.items():
        wav = render_sine(freq, sample_rate=SAMPLE_RATE, duration_sec=DURATION_SEC)
        path = out_dir / filename
        sf.write(path, wav, SAMPLE_RATE, subtype="PCM_16")
        print(f"wrote {path} ({len(wav)} samples, {len(wav)/SAMPLE_RATE:.3f}s, {freq}Hz)")


if __name__ == "__main__":
    main()
