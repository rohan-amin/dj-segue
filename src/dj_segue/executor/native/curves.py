"""Render compiler Envelopes to per-sample float32 gain curves."""

from __future__ import annotations

import numpy as np

from dj_segue.compiler import Envelope, shape_value


def render_envelope(env: Envelope, sample_rate: int, total_samples: int) -> np.ndarray:
    """Sample-accurate curve over samples [0, total_samples)."""
    return render_envelope_range(env, sample_rate, 0, total_samples)


def render_envelope_range(env: Envelope, sample_rate: int, start: int, end: int) -> np.ndarray:
    """The curve over mix samples [start, end): a ramp at mix-second s starts
    at round(s * sr).

    Ramps are applied in start order; each fills its own span and holds its
    end value from then on, so later ramps overwrite the hold. Ramp fractions
    are computed from the unclipped span, so every sample gets the same value
    whatever range it's rendered in.
    """
    out = np.full(end - start, env.initial, dtype=np.float32)
    for r in sorted(env.ramps, key=lambda r: r.start_sec):
        s1 = int(round(r.start_sec * sample_rate))
        s2 = int(round(r.end_sec * sample_rate))
        if s2 > s1:
            a, b = max(start, s1), min(s2, end)
            if b > a:
                t = (np.arange(a, b, dtype=np.float64) - s1) / (s2 - s1)
                out[a - start : b - start] = shape_value(r.shape, r.start_value, r.end_value, t)
        hold_from = max(start, min(s2, end))
        out[hold_from - start :] = r.end_value
    return out
