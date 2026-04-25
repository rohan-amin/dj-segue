"""Given a plan, ensure every audio file referenced has a fresh analysis cache.

Idempotent: skips files whose `.beats` cache is already up-to-date with the
audio's mtime and the current analyzer version.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dj_segue.analyzer import (
    BeatAnalysis,
    analyze_audio,
    is_fresh,
    load_cache,
    write_cache,
)
from dj_segue.schema.plan import Plan


@dataclass(frozen=True)
class TrackAnalysis:
    """Per-track analysis: one entry per stem (single-source tracks have one 'full')."""

    track_id: str
    stems: dict[str, BeatAnalysis]
    declared_bpm: float

    @property
    def primary(self) -> BeatAnalysis:
        """The analysis used for duration/beat-grid: 'full' if present, else the first stem."""
        if "full" in self.stems:
            return self.stems["full"]
        return next(iter(self.stems.values()))


@dataclass(frozen=True)
class PreprocessResult:
    audio_root: Path
    tracks: dict[str, TrackAnalysis]
    cached_count: int  # how many files were already fresh
    analyzed_count: int  # how many we re-ran analysis on


def preprocess(plan: Plan, audio_root: Path) -> PreprocessResult:
    audio_root = Path(audio_root).resolve()
    cached = 0
    analyzed = 0
    out: dict[str, TrackAnalysis] = {}

    for tid, track in plan.tracks.items():
        stem_results: dict[str, BeatAnalysis] = {}
        for stem_name, rel_path in track.stems.items():
            audio = (audio_root / rel_path).resolve()
            if not audio.exists():
                raise FileNotFoundError(
                    f"track {tid!r} stem {stem_name!r}: audio file not found "
                    f"at {audio} (resolved from {rel_path!r} relative to {audio_root})"
                )
            if is_fresh(audio):
                stem_results[stem_name] = load_cache(audio).to_analysis()
                cached += 1
            else:
                analysis = analyze_audio(audio)
                write_cache(audio, analysis)
                stem_results[stem_name] = analysis
                analyzed += 1
        out[tid] = TrackAnalysis(
            track_id=tid, stems=stem_results, declared_bpm=track.bpm
        )

    return PreprocessResult(
        audio_root=audio_root,
        tracks=out,
        cached_count=cached,
        analyzed_count=analyzed,
    )
