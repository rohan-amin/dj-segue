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

CACHE_SCHEMA_VERSION = 1
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
    detected_bpm: float
    beat_times: list[float]

    def to_analysis(self) -> BeatAnalysis:
        return BeatAnalysis(
            detected_bpm=self.detected_bpm,
            beat_times=np.asarray(self.beat_times, dtype=np.float64),
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
        beat_times=[float(t) for t in analysis.beat_times],
    )
    cp = cache_path(audio_path)
    cp.write_text(json.dumps(asdict(entry), indent=2))
    return cp


def load_cache(audio_path: Path) -> CacheEntry:
    cp = cache_path(audio_path)
    data = json.loads(cp.read_text())
    return CacheEntry(**data)
