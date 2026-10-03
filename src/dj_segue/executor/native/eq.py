"""3-band DJ EQ (isolator style): fixed crossovers, automated band gains.

A deck with `eq` lanes is split once into low / mid / high bands with
Linkwitz–Riley 24 dB/oct crossovers at EQ_LOW_HZ and EQ_HIGH_HZ:

    low  = AP_high(LP_low(x))      (all-passed so its phase matches the others)
    mid  = LP_high(HP_low(x))
    high = HP_high(HP_low(x))

LR4 low- and high-pass outputs sum to an all-pass, so low + mid + high has a
flat magnitude response: with every band at 0 dB the deck sounds unchanged
(only phase-shifted). Automation then only scales the bands sample by
sample — the filters never change, so there's no zipper noise and nothing to
crossfade, and the split can be computed once (render_decks) while the gains
are cheap to recompute (the tune page).

To save memory the engine keeps the band sum and two bands; the mix is
    sum·g_mid + low·(g_low − g_mid) + high·(g_high − g_mid).
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

EQ_LOW_HZ = 200.0
EQ_HIGH_HZ = 2500.0


def _lr4(x: np.ndarray, hz: float, btype: str, sample_rate: int) -> np.ndarray:
    """Linkwitz–Riley 4th order: a 2nd-order Butterworth applied twice."""
    sos = butter(2, hz, btype=btype, fs=sample_rate, output="sos")
    return sosfilt(sos, sosfilt(sos, x, axis=0), axis=0)


def split_bands(x: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(sum, low, high) of `x` (n, channels), float32. mid = sum − low − high."""
    x = x.astype(np.float64)
    lo = _lr4(x, EQ_LOW_HZ, "low", sample_rate)
    low = _lr4(lo, EQ_HIGH_HZ, "low", sample_rate) + _lr4(lo, EQ_HIGH_HZ, "high", sample_rate)
    del lo
    hp = _lr4(x, EQ_LOW_HZ, "high", sample_rate)
    del x
    high = _lr4(hp, EQ_HIGH_HZ, "high", sample_rate)
    total = low + _lr4(hp, EQ_HIGH_HZ, "low", sample_rate) + high
    return total.astype(np.float32), low.astype(np.float32), high.astype(np.float32)
