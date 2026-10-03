"""Master limiter: keeps the mix's sample peaks at or below LIMIT_CEILING_DB.

A lookahead brickwall built only from finite windows, so each output sample
depends on the input within a fixed distance of it (`context_samples`). That
lets the tune page limit just an excerpt (plus context) and get exactly the
samples a full render has.

    need[n]  = min(1, ceiling / peak[n])            peak = max |x| over channels
    held[m]  = min(need[m - hold .. m + 2w])         hold, then lookahead
    gain[n]  = mean of mean of held over [n - 2w, n] (two w-sample boxes)

Every `held` value averaged into gain[n] comes from a window containing n, so
gain[n] <= need[n]: no sample is left above the ceiling. Gain ramps down over
2w samples before a peak (S-shaped), holds, and ramps back up over 2w.

The boxes sum integers (gain quantized down to 2^-24 steps) so the result
doesn't depend on where the array starts — float running sums would.
Where nothing exceeds the ceiling, gain is exactly 1 and the audio untouched.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import minimum_filter1d

LIMIT_CEILING_DB = -1.0
LIMIT_RAMP_SEC = 0.005  # each of the two smoothing boxes; attack/release = 2×
LIMIT_HOLD_SEC = 0.050

_ONE = 1 << 24  # gain quantum


def _sizes(sample_rate: int) -> tuple[int, int]:
    w = max(1, round(LIMIT_RAMP_SEC * sample_rate))
    hold = round(LIMIT_HOLD_SEC * sample_rate)
    return w, hold


def context_samples(sample_rate: int) -> tuple[int, int]:
    """(before, after): input needed around a range to limit it exactly."""
    w, hold = _sizes(sample_rate)
    return 2 * w + hold, 2 * w


def limit(x: np.ndarray, sample_rate: int) -> np.ndarray:
    """`x` (n, 2) with peaks held to the ceiling. Audio outside `x` counts as
    silence; a caller limiting an excerpt pads it by `context_samples`."""
    ceiling = 10 ** (LIMIT_CEILING_DB / 20)
    peak = np.abs(x).max(axis=1) if len(x) else np.zeros(0)
    if not (peak > ceiling).any():
        return x
    w, hold = _sizes(sample_rate)

    need = np.ones(len(x), dtype=np.float64)
    over = peak > ceiling
    need[over] = ceiling / peak[over]
    q = np.floor(need * _ONE).astype(np.int64)

    held = _window_min(q, hold, 2 * w)
    s1 = _box(held, w, _ONE)  # sum of w values
    s2 = _box(s1, w, w * _ONE)  # sum of w sums
    gain = s2 / (w * w * _ONE)
    return (x * gain[:, None]).astype(np.float32)


def _window_min(v: np.ndarray, before: int, after: int) -> np.ndarray:
    """out[m] = min(v[m - before .. m + after]); outside `v` counts as _ONE."""
    padded = np.concatenate([np.full(before, _ONE), v, np.full(after, _ONE)])
    size = before + after + 1
    # origin -(size // 2) makes output i's window padded[i .. i + size - 1].
    out = minimum_filter1d(padded, size, mode="nearest", origin=-(size // 2))
    return out[: len(v)]


def _box(v: np.ndarray, w: int, fill: int) -> np.ndarray:
    """out[n] = sum(v[n - w + 1 .. n]); before `v` counts as `fill`.
    Integer cumsum, so the result doesn't depend on where `v` starts."""
    p = np.concatenate([np.full(w, fill, dtype=np.int64), v])
    c = np.concatenate([[0], p.cumsum()])  # c[k] = sum(p[:k])
    return c[w + 1 :] - c[1 : len(v) + 1]
