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

Stretching is slow (several seconds per minute of audio), so results are
cached on disk, keyed by a hash of the input samples, the rate, the sample
rate, the rubberband version and our settings. The cache lives in the
project's `.cache/stretch` (git-ignored) when running from a source checkout,
else ~/.cache/dj-segue/stretch. Override with DJ_SEGUE_STRETCH_CACHE (a
folder, or "off" to disable). Delete the folder any time to reclaim space.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf

HEADROOM = 0.5  # input peak fed to rubberband (-6 dBFS)


ARGS = ("--quiet", "--fine")
CACHE_ENV = "DJ_SEGUE_STRETCH_CACHE"


def time_stretch(audio: np.ndarray, sample_rate: int, rate: float) -> np.ndarray:
    """Play `audio` (frames × channels, float32) `rate` times faster.

    rate > 1 speeds up (shorter output), rate < 1 slows down. Output has
    ≈ len(audio) / rate frames; callers trim/pad to the exact length they need.
    """
    if rate <= 0:
        raise ValueError(f"rate must be positive, got {rate}")
    if rate == 1.0:
        return audio
    exe = _rubberband()
    cache = _cache_file(exe, audio, sample_rate, rate)
    if cache is not None and cache.exists():
        try:
            return np.load(cache)
        except (OSError, ValueError):
            pass  # unreadable entry: recompute and overwrite
    out = _run_rubberband(exe, audio, sample_rate, rate)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.stem}.{os.getpid()}.{id(out)}.tmp.npy")
        np.save(tmp, out)
        os.replace(tmp, cache)  # atomic: concurrent writers can't leave a torn file
    return out


def default_cache_dir() -> Path:
    """`<project>/.cache/stretch` in a source checkout, else ~/.cache/dj-segue/stretch."""
    project = Path(__file__).resolve().parents[4]  # src/dj_segue/executor/native/
    if (project / "pyproject.toml").exists() and (project / "src" / "dj_segue").is_dir():
        return project / ".cache" / "stretch"
    return Path.home() / ".cache" / "dj-segue" / "stretch"


def _rubberband() -> str:
    exe = shutil.which("rubberband")
    if exe is None:
        raise RuntimeError(
            "time-stretching needs the `rubberband` CLI "
            "(macOS: brew install rubberband; Debian/Ubuntu: apt install rubberband-cli)"
        )
    return exe


@lru_cache(maxsize=4)
def _rubberband_version(exe: str) -> str:
    r = subprocess.run([exe, "--version"], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def _cache_file(exe: str, audio: np.ndarray, sample_rate: int, rate: float) -> Path | None:
    root = os.environ.get(CACHE_ENV) or str(default_cache_dir())
    if root.lower() == "off":
        return None
    a = np.ascontiguousarray(audio, dtype=np.float32)
    h = hashlib.blake2b(digest_size=20)
    h.update(repr((a.shape, sample_rate, float(rate), HEADROOM, ARGS)).encode())
    h.update(_rubberband_version(exe).encode())
    h.update(a.tobytes())
    return Path(root).expanduser() / f"{h.hexdigest()}.npy"


def _run_rubberband(exe: str, audio: np.ndarray, sample_rate: int, rate: float) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    gain = peak / HEADROOM if peak > 0 else 1.0
    with tempfile.TemporaryDirectory(prefix="dj-segue-rb-") as tmp:
        src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.wav"
        sf.write(src, audio / gain, sample_rate, subtype="FLOAT")
        subprocess.run(
            [exe, *ARGS, "--tempo", repr(float(rate)), str(src), str(dst)],
            check=True,
            capture_output=True,
        )
        out, _ = sf.read(dst, dtype="float32", always_2d=True)
    return out * np.float32(gain)
