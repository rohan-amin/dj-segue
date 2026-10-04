"""Stem separation (M3): vocals / drums / bass / other, with demucs.

`stems_for(audio, sample_rate)` returns the four stems of a track's audio
(frames × 2, float32). "other" is the track minus the other three, so the
stems sum back to the input to float rounding ("other" absorbs what demucs
misses, ~25 dB down on real music).

Separation is slow (~40 s for a 4-minute track on Apple silicon, plus ~20 s
to load the model once), so results are cached on disk keyed by a hash of
the input samples, the model, demucs's version and our settings: in the
project's `.cache/stems` (git-ignored) when running from a source checkout,
else ~/.cache/dj-segue/stems. DJ_SEGUE_STEM_CACHE overrides the folder
("off" disables the cache).

demucs is an optional install: `pip install -e .[stems]`.
"""

from __future__ import annotations

import hashlib
import os
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

from dj_segue.schema.plan import SEPARATED_STEMS as STEM_NAMES
MODEL = "htdemucs_ft"
CACHE_ENV = "DJ_SEGUE_STEM_CACHE"
# No random time shifts (demucs's default is 1): the same input gives the
# same stems.
SETTINGS = (MODEL, "shifts=0", "overlap=0.25", "other=residual")

_lock = threading.Lock()  # one separation at a time (the model is big)


def stems_for(audio: np.ndarray, sample_rate: int) -> dict[str, np.ndarray]:
    """The four stems of `audio` (frames × channels float32), cached."""
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    folder = _cache_folder(audio, sample_rate)
    if folder is not None and all((folder / f"{n}.npy").exists() for n in STEM_NAMES):
        try:
            return {n: np.load(folder / f"{n}.npy") for n in STEM_NAMES}
        except (OSError, ValueError):
            pass  # unreadable entry: recompute and overwrite
    with _lock:
        stems = _separate(audio, sample_rate)
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
        for name, x in stems.items():
            tmp = folder / f"{name}.{os.getpid()}.tmp.npy"
            np.save(tmp, x)
            os.replace(tmp, folder / f"{name}.npy")
    return stems


def is_cached(audio: np.ndarray, sample_rate: int) -> bool:
    folder = _cache_folder(np.ascontiguousarray(audio, dtype=np.float32), sample_rate)
    return folder is not None and all((folder / f"{n}.npy").exists() for n in STEM_NAMES)


def default_cache_dir() -> Path:
    """`<project>/.cache/stems` in a source checkout, else ~/.cache/dj-segue/stems."""
    project = Path(__file__).resolve().parents[3]  # src/dj_segue/stems/
    if (project / "pyproject.toml").exists() and (project / "src" / "dj_segue").is_dir():
        return project / ".cache" / "stems"
    return Path.home() / ".cache" / "dj-segue" / "stems"


def _cache_folder(audio: np.ndarray, sample_rate: int) -> Path | None:
    root = os.environ.get(CACHE_ENV) or str(default_cache_dir())
    if root.lower() == "off":
        return None
    h = hashlib.blake2b(digest_size=20)
    h.update(repr((audio.shape, sample_rate, SETTINGS, _demucs_version())).encode())
    h.update(audio.tobytes())
    return Path(root).expanduser() / h.hexdigest()


def _demucs_version() -> str:
    try:
        import demucs
    except ImportError:
        return "missing"
    return demucs.__version__


@lru_cache(maxsize=1)
def _separator():
    try:
        import torch
        from demucs.api import Separator
    except ImportError as e:
        raise RuntimeError(
            "stem separation needs demucs: pip install -e '.[stems]'"
        ) from e
    device = "mps" if torch.backends.mps.is_available() else (
        "cuda" if torch.cuda.is_available() else "cpu"
    )
    return Separator(MODEL, device=device, shifts=0, overlap=0.25)


def _separate(audio: np.ndarray, sample_rate: int) -> dict[str, np.ndarray]:
    import torch

    sep = _separator()
    stereo = audio if audio.shape[1] == 2 else np.repeat(audio[:, :1], 2, axis=1)
    _, out = sep.separate_tensor(torch.from_numpy(stereo.T.copy()), sample_rate)
    stems = {}
    for name in ("vocals", "drums", "bass"):
        x = out[name].numpy().T.astype(np.float32)
        if sep.samplerate != sample_rate:
            import soxr

            x = soxr.resample(x, sep.samplerate, sample_rate).astype(np.float32)
        stems[name] = _fit(x, len(audio))
    stems["other"] = (stereo - stems["vocals"] - stems["drums"] - stems["bass"]).astype(np.float32)
    return stems


def _fit(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) >= n:
        return x[:n]
    return np.concatenate([x, np.zeros((n - len(x), x.shape[1]), dtype=x.dtype)])
