"""Master limiter (M5): ceiling, locality, and excerpt == full render."""

from __future__ import annotations

import numpy as np

from dj_segue.executor.native.engine import DeckRender
from dj_segue.executor.native.limiter import (
    LIMIT_CEILING_DB,
    _sizes,
    context_samples,
    limit,
)

SR = 1000  # small rate keeps the brute-force reference cheap
CEIL = 10 ** (LIMIT_CEILING_DB / 20)


def _loud(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.uniform(-0.5, 0.5, (n, 2))
    for i in rng.choice(n, 12, replace=False):
        x[i : i + rng.integers(1, 40)] *= rng.uniform(2, 4)
    return x.astype(np.float32)


def _reference_gain(x: np.ndarray) -> np.ndarray:
    w, hold = _sizes(SR)
    peak = np.abs(x).max(axis=1)
    need = np.minimum(1.0, CEIL / np.maximum(peak, 1e-12))
    n = len(x)
    held = np.array([need[max(0, m - hold) : m + 2 * w + 1].min() for m in range(n)])
    pad = lambda v: np.concatenate([np.ones(w), v])  # noqa: E731
    box = lambda v: np.array([pad(v)[k + 1 : k + w + 1].mean() for k in range(n)])  # noqa: E731
    return box(box(held))


def test_quiet_mix_is_untouched():
    x = np.full((500, 2), 0.5, dtype=np.float32)
    assert limit(x, SR) is x


def test_peaks_held_to_ceiling():
    x = _loud(3000)
    y = limit(x, SR)
    assert np.abs(y).max() <= CEIL * (1 + 1e-6)
    assert np.all(np.abs(y) <= np.abs(x) + 1e-7)


def test_matches_reference_gain():
    x = _loud(1500, seed=1)
    gain = limit(x, SR)[:, 0] / x[:, 0]
    ref = _reference_gain(x)
    ok = np.abs(x[:, 0]) > 1e-3
    np.testing.assert_allclose(gain[ok], ref[ok], atol=1e-6)


def test_effect_is_local():
    w, hold = _sizes(SR)
    x = np.full((2000, 2), 0.3, dtype=np.float32)
    x[1000] = 2.0
    y = limit(x, SR)
    changed = np.nonzero(np.any(y != x, axis=1))[0]
    assert changed.min() >= 1000 - 2 * w
    assert changed.max() <= 1000 + hold + 2 * w
    assert np.abs(y[1000]).max() <= CEIL * (1 + 1e-6)


def test_excerpt_equals_full_mix():
    n = 4000
    decks = {1: _loud(n, 2), 2: _loud(n, 3)}
    gains = {1: np.linspace(1, 0, n), 2: np.linspace(0, 1, n)}
    parts = DeckRender(decks=decks, sample_rate=SR, total_samples=n, mix_tempo=120.0, timeline=None)
    full = parts.mix(gains)
    before, after = context_samples(SR)
    for start, end in [(0, 300), (1234, 2345), (n - 50, n), (before + 1, n - after - 1)]:
        np.testing.assert_array_equal(parts.mix(gains, start, end), full[start:end])
