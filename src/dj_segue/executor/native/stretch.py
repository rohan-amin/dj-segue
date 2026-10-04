"""Time-stretching (tempo change, pitch preserved), streaming, via the Rubber
Band library.

`Stretcher` wraps librubberband's C API (through ctypes) in real-time mode
with the R3 ("finer") engine: it reads track audio from a start position
and returns mix-rate audio in whatever pieces you ask for. Measured on click
tracks, onsets land within ~0.3 ms of their ideal stretched position (as
with the offline `--fine` CLI it replaces), and it runs ~25× real time per
stereo stream on Apple silicon.

Deterministic, and independent of how it's read: input is fed in pieces the
stretcher itself asks for (`samples_required`), whatever the caller pulls,
so reading 1 sample at a time or the whole segment at once gives the same
samples. That's what lets the live engine and an offline render produce
identical audio.

Needs the Rubber Band library: macOS `brew install rubberband`
(Debian/Ubuntu: `apt install librubberband-dev`). DJ_SEGUE_RUBBERBAND may
point at the shared library if it isn't found.
"""

from __future__ import annotations

import ctypes as C
import ctypes.util
import os
from collections.abc import Callable
from functools import lru_cache

import numpy as np

_REALTIME = 0x00000001
_THREADING_NEVER = 0x00010000
_CHANNELS_TOGETHER = 0x10000000
_ENGINE_FINER = 0x20000000
OPTIONS = _REALTIME | _THREADING_NEVER | _CHANNELS_TOGETHER | _ENGINE_FINER

CHUNK = 1024  # most input frames fed per process() call
_FP = C.POINTER(C.c_float)


@lru_cache(maxsize=1)
def _lib() -> C.CDLL:
    candidates = [
        os.environ.get("DJ_SEGUE_RUBBERBAND"),
        ctypes.util.find_library("rubberband"),
        "/opt/homebrew/opt/rubberband/lib/librubberband.dylib",
        "/usr/local/opt/rubberband/lib/librubberband.dylib",
        "librubberband.so.3",
        "librubberband.so",
    ]
    for path in filter(None, candidates):
        try:
            lib = C.CDLL(path)
            break
        except OSError:
            continue
    else:
        raise RuntimeError(
            "time-stretching needs the Rubber Band library "
            "(macOS: brew install rubberband; Debian/Ubuntu: apt install librubberband-dev); "
            "set DJ_SEGUE_RUBBERBAND to its path if it's installed somewhere unusual"
        )
    lib.rubberband_new.restype = C.c_void_p
    lib.rubberband_new.argtypes = [C.c_uint, C.c_uint, C.c_int, C.c_double, C.c_double]
    lib.rubberband_delete.argtypes = [C.c_void_p]
    lib.rubberband_set_max_process_size.argtypes = [C.c_void_p, C.c_uint]
    for name in ("get_samples_required", "get_preferred_start_pad", "get_start_delay"):
        fn = getattr(lib, f"rubberband_{name}")
        fn.argtypes, fn.restype = [C.c_void_p], C.c_uint
    lib.rubberband_process.argtypes = [C.c_void_p, C.POINTER(_FP), C.c_uint, C.c_int]
    lib.rubberband_available.argtypes, lib.rubberband_available.restype = [C.c_void_p], C.c_int
    lib.rubberband_retrieve.argtypes = [C.c_void_p, C.POINTER(_FP), C.c_uint]
    lib.rubberband_retrieve.restype = C.c_uint
    return lib


class Stretcher:
    """Mix-rate audio from `read(start, n)` (track frames × 2, float32),
    starting at track frame `start`, played `rate` times faster.

    `pull(n)` returns the next n output frames. Output frame k corresponds
    to track time start + k·rate (the stretcher's own delay is removed).
    Reads past the track's ends come back as silence from `read`.
    """

    def __init__(
        self, read: Callable[[int, int], np.ndarray], start: int, rate: float, sample_rate: int
    ) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        lib = _lib()
        self._lib = lib
        self._read = read
        self._pos = start
        self._st = lib.rubberband_new(sample_rate, 2, OPTIONS, 1.0 / rate, 1.0)
        lib.rubberband_set_max_process_size(self._st, CHUNK)
        self._chunks: list[np.ndarray] = []  # output not yet pulled
        self._have = 0
        self._obuf = [np.zeros(4 * CHUNK, dtype=np.float32) for _ in range(2)]
        pad = lib.rubberband_get_preferred_start_pad(self._st)
        self._skip = lib.rubberband_get_start_delay(self._st)
        if pad:
            self._process(np.zeros((pad, 2), dtype=np.float32))

    def __del__(self) -> None:
        st = getattr(self, "_st", None)
        if st:
            self._lib.rubberband_delete(st)
            self._st = None

    def pull(self, n: int) -> np.ndarray:
        while self._have < n:
            need = self._lib.rubberband_get_samples_required(self._st) or CHUNK
            need = min(need, CHUNK)
            self._process(self._read(self._pos, need))
            self._pos += need
        y = self._chunks[0] if len(self._chunks) == 1 else np.concatenate(self._chunks)
        out, rest = y[:n], y[n:]
        self._chunks, self._have = ([rest] if len(rest) else []), len(rest)
        return out

    def _process(self, x: np.ndarray) -> None:
        chans = [np.ascontiguousarray(x[:, c], dtype=np.float32) for c in range(2)]
        ptrs = (_FP * 2)(*[c.ctypes.data_as(_FP) for c in chans])
        self._lib.rubberband_process(self._st, ptrs, len(x), 0)
        got = []
        while (avail := self._lib.rubberband_available(self._st)) > 0:
            optrs = (_FP * 2)(*[b.ctypes.data_as(_FP) for b in self._obuf])
            k = self._lib.rubberband_retrieve(self._st, optrs, min(avail, len(self._obuf[0])))
            got.append(np.stack([self._obuf[0][:k], self._obuf[1][:k]], axis=1))  # copies
        if not got:
            return
        y = np.concatenate(got)
        if self._skip:
            drop = min(self._skip, len(y))
            y, self._skip = y[drop:], self._skip - drop
        if len(y):
            self._chunks.append(y)
            self._have += len(y)


def time_stretch(audio: np.ndarray, sample_rate: int, rate: float) -> np.ndarray:
    """`audio` played `rate` times faster: round(len / rate) frames."""
    if rate == 1.0:
        return audio

    def read(start: int, n: int) -> np.ndarray:
        out = np.zeros((n, 2), dtype=np.float32)
        lo, hi = max(start, 0), min(start + n, len(audio))
        if hi > lo:
            out[lo - start : hi - start] = audio[lo:hi]
        return out

    return Stretcher(read, 0, rate, sample_rate).pull(int(round(len(audio) / rate)))
