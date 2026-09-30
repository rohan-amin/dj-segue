"""Downbeat detection via beat_this (CPJKU, 2024) — which beat is a bar's "1".

beat_this is a neural beat/downbeat tracker. We use only its downbeat times:
the fixed grid from analyzer/beat.py already places beats more precisely than
its 50 fps frames. Each downbeat votes for a grid position mod 4, and the
winner says how far to shift beat 0 so it lands on a downbeat.

Optional: needs `pip install beat_this` (pulls in PyTorch). Without it,
`detect_downbeats` returns None and beat 0 stays on the first grid beat.
The model checkpoint is downloaded once on first use (~cached by torch hub).
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

CHECKPOINT = "final0"
# The winning position must hold at least this share of the downbeat votes,
# or we don't trust it (e.g. a track with shifting meter or no clear bars).
MIN_VOTE_SHARE = 0.5
BEATS_PER_BAR = 4


def downbeat_model_id() -> str | None:
    """Identifier folded into the analysis cache key; None if unavailable."""
    try:
        from importlib.metadata import version

        return f"beat_this-{version('beat_this')}-{CHECKPOINT}"
    except Exception:
        return None


@lru_cache(maxsize=1)
def _model():
    from beat_this.inference import Audio2Beats

    return Audio2Beats(checkpoint_path=CHECKPOINT, device="cpu")


def detect_downbeats(y: np.ndarray, sr: int) -> np.ndarray | None:
    """Downbeat times in seconds, or None if beat_this isn't installed."""
    if downbeat_model_id() is None:
        return None
    _beats, downbeats = _model()(np.asarray(y, dtype=np.float32), sr)
    return np.asarray(downbeats, dtype=np.float64)


def downbeat_offset(downbeats: np.ndarray | None, anchor: float, bpm: float) -> int:
    """How many beats (0–3) after `anchor` the first downbeat falls, by vote.

    Each detected downbeat is snapped to its nearest grid beat k; the most
    common k mod 4 wins. Returns 0 when there's nothing to go on.
    """
    if downbeats is None or len(downbeats) == 0:
        return 0
    period = 60.0 / bpm
    k = np.round((downbeats - anchor) / period).astype(int) % BEATS_PER_BAR
    votes = np.bincount(k, minlength=BEATS_PER_BAR)
    best = int(np.argmax(votes))
    if votes[best] < MIN_VOTE_SHARE * votes.sum():
        return 0
    return best
