"""Time conversions: positions/durations ↔ seconds, beats, samples.

All conversions assume 4/4 time (the schema doesn't expose time signature in v0.1).
For v0.1: tracks play at their natural bpm; mix_tempo is informational and used
only for converting mix-time positions to seconds.
"""

from __future__ import annotations

from typing import Any

from dj_segue.schema.plan import (
    BarPos,
    BarsDur,
    BeatPos,
    BeatsDur,
    CuePos,
    SecondPos,
    SecondsDur,
    Track,
)

BEATS_PER_BAR = 4  # 4/4 assumption


def mix_pos_to_seconds(pos: Any, mix_tempo: float) -> float:
    """Resolve a mix-time position to seconds. Cue refs are invalid here."""
    if isinstance(pos, BeatPos):
        return pos.beat * 60.0 / mix_tempo
    if isinstance(pos, BarPos):
        return pos.bar * BEATS_PER_BAR * 60.0 / mix_tempo
    if isinstance(pos, SecondPos):
        return float(pos.second)
    if isinstance(pos, CuePos):
        raise ValueError(f"Cue references are not valid in mix-time context: {pos!r}")
    raise TypeError(f"Unknown position type: {type(pos).__name__}")


def track_pos_to_seconds(pos: Any, track: Track) -> float:
    """Resolve a track-time position to seconds within the track."""
    if isinstance(pos, CuePos):
        cue = track.cues.get(pos.cue)
        if cue is None:
            raise ValueError(
                f"cue {pos.cue!r} not declared on track; "
                f"available: {sorted(track.cues)}"
            )
        if cue.beat is not None:
            return cue.beat * 60.0 / track.bpm
        if cue.bar is not None:
            return cue.bar * BEATS_PER_BAR * 60.0 / track.bpm
        return float(cue.second)  # type: ignore[arg-type]
    if isinstance(pos, BeatPos):
        return pos.beat * 60.0 / track.bpm
    if isinstance(pos, BarPos):
        return pos.bar * BEATS_PER_BAR * 60.0 / track.bpm
    if isinstance(pos, SecondPos):
        return float(pos.second)
    raise TypeError(f"Unknown position type: {type(pos).__name__}")


def duration_to_seconds(dur: Any, tempo: float) -> float:
    """Resolve a duration. `tempo` is the contextual tempo (mix or track)."""
    if isinstance(dur, BeatsDur):
        return dur.beats * 60.0 / tempo
    if isinstance(dur, BarsDur):
        return dur.bars * BEATS_PER_BAR * 60.0 / tempo
    if isinstance(dur, SecondsDur):
        return float(dur.seconds)
    raise TypeError(f"Unknown duration type: {type(dur).__name__}")
