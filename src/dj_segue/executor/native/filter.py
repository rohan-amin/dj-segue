"""DJ filter knob (schema v0.7 `filter` lane): low-pass left, high-pass right.

Knob position p in -1..+1, per sample:
    p < 0  low-pass,  cutoff FILTER_LP_TOP_HZ → FILTER_LP_BOTTOM_HZ as p → -1
    p > 0  high-pass, cutoff FILTER_HP_BOTTOM_HZ → FILTER_HP_TOP_HZ as p → +1
The cutoff is exponential in p, so a linear knob move is an even sweep.
Within FILTER_DEAD_ZONE of 0 the output blends from dry into the filter, so
p = 0 is exactly the input and crossing the centre doesn't click.

The filter is a 2-pole (12 dB/oct) topology-preserving-transform state
variable filter (Zavalishin), with Q = FILTER_Q. Its coefficients are
recomputed every sample and it stays stable and click-free under that
modulation. Compiled with numba; one pass per channel.
"""

from __future__ import annotations

import numba
import numpy as np

FILTER_LP_TOP_HZ = 20000.0
FILTER_LP_BOTTOM_HZ = 60.0
FILTER_HP_BOTTOM_HZ = 20.0
FILTER_HP_TOP_HZ = 10000.0
FILTER_Q = 0.9
FILTER_DEAD_ZONE = 0.05


def apply_filter(x: np.ndarray, knob: np.ndarray, sample_rate: int) -> np.ndarray:
    """`x` (n, channels) through the filter, knob position per sample (n,)."""
    out = np.empty(x.shape, dtype=np.float32)
    knob = np.ascontiguousarray(knob, dtype=np.float64)
    for ch in range(x.shape[1]):
        out[:, ch] = _svf(
            np.ascontiguousarray(x[:, ch], dtype=np.float64), knob, float(sample_rate),
            FILTER_LP_TOP_HZ, FILTER_LP_BOTTOM_HZ, FILTER_HP_BOTTOM_HZ, FILTER_HP_TOP_HZ,
            FILTER_Q, FILTER_DEAD_ZONE,
        )
    return out


@numba.njit(cache=True)
def _svf(x, knob, sr, lp_top, lp_bottom, hp_bottom, hp_top, q, dead):  # pragma: no cover
    n = x.shape[0]
    y = np.empty(n)
    k = 1.0 / q
    max_hz = 0.49 * sr
    ic1 = 0.0
    ic2 = 0.0
    for i in range(n):
        p = knob[i]
        if p < 0.0:
            hz = lp_top * (lp_bottom / lp_top) ** (-p)
        else:
            hz = hp_bottom * (hp_top / hp_bottom) ** p
        if hz > max_hz:
            hz = max_hz
        g = np.tan(np.pi * hz / sr)
        a1 = 1.0 / (1.0 + g * (g + k))
        a2 = g * a1
        a3 = g * a2
        v3 = x[i] - ic2
        v1 = a1 * ic1 + a2 * v3
        v2 = ic2 + a2 * ic1 + a3 * v3
        ic1 = 2.0 * v1 - ic1
        ic2 = 2.0 * v2 - ic2
        if p == 0.0:
            y[i] = x[i]
            continue
        wet = v2 if p < 0.0 else x[i] - k * v1 - v2
        mix = min(1.0, abs(p) / dead)
        y[i] = x[i] + mix * (wet - x[i])
    return y
