"""BPM and beat-grid detection.

For real music librosa works well; for the M1 sine-wave fixtures it returns
something nonsensical (no onsets to lock to). The plan's declared `bpm` is
the source of truth in v0.1 — the analyzer's job here is to fill the cache
with file metadata and a best-effort tempo estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np


ANALYZER_ID = f"librosa-{librosa.__version__}"


@dataclass(frozen=True)
class BeatAnalysis:
    detected_bpm: float
    beat_times: np.ndarray  # seconds
    sample_rate: int
    n_samples: int

    @property
    def duration_sec(self) -> float:
        return self.n_samples / self.sample_rate


def analyze_audio(audio_path: Path) -> BeatAnalysis:
    y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    bpm = float(np.atleast_1d(tempo)[0])
    return BeatAnalysis(
        detected_bpm=bpm,
        beat_times=np.asarray(beats, dtype=np.float64),
        sample_rate=int(sr),
        n_samples=int(len(y)),
    )
