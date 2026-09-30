"""Render compiler Envelopes to per-sample float32 gain curves."""

from __future__ import annotations

import numpy as np

from dj_segue.compiler import Envelope, shape_value


def render_envelope(env: Envelope, sample_rate: int, total_samples: int) -> np.ndarray:
    """Sample-accurate curve: a ramp at mix-second s starts at round(s * sr).

    Ramps are applied in start order; each fills its own span and holds its
    end value to the end of the buffer, so later ramps overwrite the hold.
    Ramp fractions are computed from the unclipped span, so a ramp that starts
    before sample 0 or runs past the end is still evaluated at the right phase.
    """
    out = np.full(total_samples, env.initial, dtype=np.float32)
    for r in sorted(env.ramps, key=lambda r: r.start_sec):
        s1 = int(round(r.start_sec * sample_rate))
        s2 = int(round(r.end_sec * sample_rate))
        if s2 > s1:
            a, b = max(0, s1), min(s2, total_samples)
            if b > a:
                t = (np.arange(a, b, dtype=np.float64) - s1) / (s2 - s1)
                out[a:b] = shape_value(r.shape, r.start_value, r.end_value, t)
        hold_from = max(0, min(s2, total_samples))
        out[hold_from:] = r.end_value
    return out
