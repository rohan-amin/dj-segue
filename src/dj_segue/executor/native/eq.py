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

`BandSplitter` does the split block by block, carrying the filters' state;
its output is bitwise the same as one `split_bands` call over the whole
signal (the live engine relies on that).
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

EQ_LOW_HZ = 200.0
EQ_HIGH_HZ = 2500.0


class _LR4:
    """Linkwitz–Riley 4th order (a 2nd-order Butterworth applied twice),
    stateful across calls."""

    def __init__(self, hz: float, btype: str, sample_rate: int, channels: int = 2) -> None:
        self.sos = butter(2, hz, btype=btype, fs=sample_rate, output="sos")
        self.zi = [np.zeros((self.sos.shape[0], 2, channels)) for _ in range(2)]

    def __call__(self, x: np.ndarray) -> np.ndarray:
        for k in range(2):
            x, self.zi[k] = sosfilt(self.sos, x, axis=0, zi=self.zi[k])
        return x


class BandSplitter:
    """`split_bands`, one block at a time."""

    def __init__(self, sample_rate: int) -> None:
        sr = sample_rate
        self.lo = _LR4(EQ_LOW_HZ, "low", sr)
        self.lo_lp, self.lo_hp = _LR4(EQ_HIGH_HZ, "low", sr), _LR4(EQ_HIGH_HZ, "high", sr)
        self.hp = _LR4(EQ_LOW_HZ, "high", sr)
        self.hp_hp, self.hp_lp = _LR4(EQ_HIGH_HZ, "high", sr), _LR4(EQ_HIGH_HZ, "low", sr)

    def process(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(sum, low, high) of the next block of `x` (n, 2), float32."""
        x = x.astype(np.float64)
        lo = self.lo(x)
        low = self.lo_lp(lo) + self.lo_hp(lo)
        del lo
        hp = self.hp(x)
        del x
        high = self.hp_hp(hp)
        total = low + self.hp_lp(hp) + high
        return total.astype(np.float32), low.astype(np.float32), high.astype(np.float32)


def split_bands(x: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(sum, low, high) of `x` (n, 2), float32. mid = sum − low − high."""
    return BandSplitter(sample_rate).process(x)


def apply_eq(x: np.ndarray, gains, bands) -> np.ndarray:
    """x (already the band sum) with its bands scaled by per-sample (low,
    mid, high) gains; all the same length."""
    g_low, g_mid, g_high = (g[:, None] for g in gains)
    low, high = bands
    return x * g_mid + low * (g_low - g_mid) + high * (g_high - g_mid)
