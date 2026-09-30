"""Offline time-stretching (tempo change, pitch preserved) via Rubber Band.

Uses the R3 ("--fine") engine: measured on click tracks, onsets land within
~0.25 ms of their ideal stretched position and output length is exact to the
sample, which is what beat-locked mixing needs.

Calls the `rubberband` command-line tool directly (macOS: `brew install
rubberband`) through 32-bit float temp files. We don't use pyrubberband: it
writes 16-bit temp files (quantizing, and clipping anything over 0 dBFS) and
mutates the args dict passed to it, which leaked one segment's tempo into the
next.

The rubberband CLI also clamps its output to ±1.0 even for float files, and
stretching can overshoot the input peak, so we scale the input into
[-HEADROOM, HEADROOM] and scale the result back (exact: the process is linear).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

HEADROOM = 0.5  # input peak fed to rubberband (-6 dBFS)


def time_stretch(audio: np.ndarray, sample_rate: int, rate: float) -> np.ndarray:
    """Play `audio` (frames × channels, float32) `rate` times faster.

    rate > 1 speeds up (shorter output), rate < 1 slows down. Output has
    ≈ len(audio) / rate frames; callers trim/pad to the exact length they need.
    """
    if rate <= 0:
        raise ValueError(f"rate must be positive, got {rate}")
    if rate == 1.0:
        return audio
    exe = shutil.which("rubberband")
    if exe is None:
        raise RuntimeError(
            "time-stretching needs the `rubberband` CLI "
            "(macOS: brew install rubberband; Debian/Ubuntu: apt install rubberband-cli)"
        )
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    gain = peak / HEADROOM if peak > 0 else 1.0
    with tempfile.TemporaryDirectory(prefix="dj-segue-rb-") as tmp:
        src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.wav"
        sf.write(src, audio / gain, sample_rate, subtype="FLOAT")
        subprocess.run(
            [exe, "--quiet", "--fine", "--tempo", repr(float(rate)), str(src), str(dst)],
            check=True,
            capture_output=True,
        )
        out, _ = sf.read(dst, dtype="float32", always_2d=True)
    return out * np.float32(gain)
