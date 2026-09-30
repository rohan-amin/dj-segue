"""`.beats` sidecar cache for analyzer output.

Cache lives next to the audio file: `track_a.wav` → `track_a.wav.beats`.
Keyed by audio mtime + analyzer version so any change invalidates the cache.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from dj_segue.analyzer.beat import ANALYZER_ID, BeatAnalysis

# v2: added grid_anchor_sec (fixed-grid phase origin).
# v3: grid fit from onset peaks; detected_bpm may be null; beat_times replaced
#     by onset_times/onset_weights.
# v4: onset_low_weights (half-beat check) and downbeat_times (may be null).
# Each bump invalidates older caches.
CACHE_SCHEMA_VERSION = 4
CACHE_SUFFIX = ".beats"


@dataclass(frozen=True)
class CacheEntry:
    schema_version: int
    analyzer: str
    audio_path: str
    audio_mtime: float
    audio_sample_rate: int
    audio_n_samples: int
    audio_duration_sec: float
    detected_bpm: float | None
    grid_anchor_sec: float
    onset_times: list[float]
    onset_weights: list[float]
    onset_low_weights: list[float]
    downbeat_times: list[float] | None

    def to_analysis(self) -> BeatAnalysis:
        return BeatAnalysis(
            detected_bpm=self.detected_bpm,
            grid_anchor_sec=self.grid_anchor_sec,
            onset_times=np.asarray(self.onset_times, dtype=np.float64),
            onset_weights=np.asarray(self.onset_weights, dtype=np.float64),
            onset_low_weights=np.asarray(self.onset_low_weights, dtype=np.float64),
            downbeat_times=(
                None
                if self.downbeat_times is None
                else np.asarray(self.downbeat_times, dtype=np.float64)
            ),
            sample_rate=self.audio_sample_rate,
            n_samples=self.audio_n_samples,
        )


def cache_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(audio_path.suffix + CACHE_SUFFIX)


def is_fresh(audio_path: Path) -> bool:
    cp = cache_path(audio_path)
    if not cp.exists() or not audio_path.exists():
        return False
    try:
        entry = load_cache(audio_path)
    except (json.JSONDecodeError, KeyError, TypeError):
        return False
    if entry.schema_version != CACHE_SCHEMA_VERSION:
        return False
    if entry.analyzer != ANALYZER_ID:
        return False
    return abs(entry.audio_mtime - audio_path.stat().st_mtime) < 1e-3


def write_cache(audio_path: Path, analysis: BeatAnalysis) -> Path:
    entry = CacheEntry(
        schema_version=CACHE_SCHEMA_VERSION,
        analyzer=ANALYZER_ID,
        audio_path=str(audio_path),
        audio_mtime=audio_path.stat().st_mtime,
        audio_sample_rate=analysis.sample_rate,
        audio_n_samples=analysis.n_samples,
        audio_duration_sec=analysis.duration_sec,
        detected_bpm=analysis.detected_bpm,
        grid_anchor_sec=analysis.grid_anchor_sec,
        onset_times=[round(float(t), 6) for t in analysis.onset_times],
        onset_weights=[round(float(w), 4) for w in analysis.onset_weights],
        onset_low_weights=[round(float(w), 4) for w in analysis.onset_low_weights],
        downbeat_times=(
            None
            if analysis.downbeat_times is None
            else [round(float(t), 4) for t in analysis.downbeat_times]
        ),
    )
    cp = cache_path(audio_path)
    cp.write_text(json.dumps(asdict(entry), indent=2))
    return cp


def load_cache(audio_path: Path) -> CacheEntry:
    cp = cache_path(audio_path)
    data = json.loads(cp.read_text())
    return CacheEntry(**data)
