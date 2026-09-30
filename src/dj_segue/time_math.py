"""Time conversions: positions/durations ↔ seconds, beats, samples.

All conversions assume 4/4 time (the schema doesn't expose time signature in v0.1).
For v0.1: tracks play at their natural bpm; mix_tempo is informational and used
only for converting mix-time positions to seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dj_segue.constants import ANCHOR_TOLERANCE  # noqa: F401  (re-exported)

from dj_segue.schema.plan import (
    AfterPos,
    BarPos,
    BarsDur,
    BeatPos,
    BeatsDur,
    CuePos,
    SecondPos,
    SecondsDur,
    Track,
    TransitionSegment,
)

BEATS_PER_BAR = 4  # 4/4 assumption


@dataclass(frozen=True)
class TrackGrid:
    """A fixed (constant-tempo) beat grid for one track.

    The grid is the function `beat -> seconds`. In v0.1 it's a straight line:

        t(beat) = anchor_sec + beat * 60 / bpm

    `anchor_sec` is the grid's phase origin — the time of beat 0, taken from
    audio analysis (the first detected beat). `bpm` is the plan's *declared*
    tempo, which remains the v0.1 source of truth; only the anchor comes from
    detection. With anchor 0 this reduces to the M1 behavior (beat 0 == sample
    0), so an absent grid and `TrackGrid.fixed(bpm)` are interchangeable.

    Beat/bar positions are grid-relative (anchored); `second` positions are
    absolute track-file time and are never shifted by the anchor.

    This is the v0.1 grid shape. A future "flex" grid would swap the linear
    formula for interpolation over a per-beat timestamp list; because every
    positioning caller goes through TrackGrid, that change stays local here.
    """

    anchor_sec: float
    bpm: float

    @classmethod
    def fixed(cls, bpm: float, anchor_sec: float = 0.0) -> "TrackGrid":
        return cls(anchor_sec=anchor_sec, bpm=bpm)

    def beat_to_seconds(self, beat: float) -> float:
        return self.anchor_sec + beat * 60.0 / self.bpm

    def bar_to_seconds(self, bar: float) -> float:
        return self.beat_to_seconds(bar * BEATS_PER_BAR)


def mix_pos_to_seconds(
    pos: Any, mix_tempo: float, anchors: dict[str, float] | None = None
) -> float:
    """Resolve a mix-time position to seconds. Cue refs are invalid here.

    `anchors` maps segment id → the segment's mix end time in seconds
    (compiler.resolve_timeline builds it); `{"after": id}` positions need it.
    """
    if isinstance(pos, AfterPos):
        if anchors is None or pos.after not in anchors:
            raise ValueError(
                f"position after {pos.after!r} is unresolved: no earlier segment "
                f"with that id (or the timeline isn't resolved yet)"
            )
        offset = 0.0 if pos.offset is None else duration_to_seconds(pos.offset, mix_tempo)
        return anchors[pos.after] + offset
    if isinstance(pos, BeatPos):
        return pos.beat * 60.0 / mix_tempo
    if isinstance(pos, BarPos):
        return pos.bar * BEATS_PER_BAR * 60.0 / mix_tempo
    if isinstance(pos, SecondPos):
        return float(pos.second)
    if isinstance(pos, CuePos):
        raise ValueError(f"Cue references are not valid in mix-time context: {pos!r}")
    raise TypeError(f"Unknown position type: {type(pos).__name__}")


def track_pos_to_seconds(
    pos: Any, track: Track, grid: TrackGrid | None = None
) -> float:
    """Resolve a track-time position to seconds within the track.

    `grid` is the track's resolved beat grid (from preprocessing). When omitted,
    a zero-anchor grid at the track's declared bpm is used — i.e. beat 0 maps to
    sample 0, the M1 behavior. Pass a grid to honor the detected downbeat anchor.
    Without a grid, a track whose bpm is auto-detected can only resolve
    `second` positions.
    """
    if isinstance(pos, SecondPos):
        return float(pos.second)
    if isinstance(pos, CuePos):
        cue = track.cues.get(pos.cue)
        if cue is None:
            raise ValueError(
                f"cue {pos.cue!r} not declared on track; "
                f"available: {sorted(track.cues)}"
            )
        if cue.second is not None:
            return float(cue.second)
        if cue.beat is not None:
            return resolve_grid(track, grid).beat_to_seconds(cue.beat)
        return resolve_grid(track, grid).bar_to_seconds(cue.bar)  # type: ignore[arg-type]
    if isinstance(pos, BeatPos):
        return resolve_grid(track, grid).beat_to_seconds(pos.beat)
    if isinstance(pos, BarPos):
        return resolve_grid(track, grid).bar_to_seconds(pos.bar)
    raise TypeError(f"Unknown position type: {type(pos).__name__}")


def resolve_grid(track: Track, grid: TrackGrid | None) -> TrackGrid:
    if grid is not None:
        return grid
    if track.bpm is None:
        raise ValueError(
            "track bpm is auto-detected; beat/bar positions need the "
            "preprocessed grid (run preprocess and pass its grids)"
        )
    return TrackGrid.fixed(track.bpm)


def duration_to_seconds(dur: Any, tempo: float) -> float:
    """Resolve a duration. `tempo` is the contextual tempo (mix or track)."""
    if isinstance(dur, BeatsDur):
        return dur.beats * 60.0 / tempo
    if isinstance(dur, BarsDur):
        return dur.bars * BEATS_PER_BAR * 60.0 / tempo
    if isinstance(dur, SecondsDur):
        return float(dur.seconds)
    raise TypeError(f"Unknown duration type: {type(dur).__name__}")


@dataclass(frozen=True)
class FadeWindow:
    """One deck's side of a transition, in mix seconds."""

    start_sec: float
    end_sec: float
    curve: str  # a ramp shape: "equal_power" | "linear" | "exponential" | "step"


def transition_windows(
    seg: TransitionSegment, mix_tempo: float, anchors: dict[str, float] | None = None
) -> tuple[FadeWindow, FadeWindow]:
    """(out, in): the from_deck's fade-out and the to_deck's fade-in.

    A cut is a zero-length step at `start_at`. A crossfade's sides default to
    `start_at` + `duration` with `curve` (equal_power); v0.4 `out` / `in`
    override offset, duration and curve per side.
    """
    start = mix_pos_to_seconds(seg.start_at, mix_tempo, anchors)
    if seg.style == "cut":
        w = FadeWindow(start, start, "step")
        return w, w
    duration = duration_to_seconds(seg.duration, mix_tempo)
    curve = seg.curve or "equal_power"

    def side(o) -> FadeWindow:
        if o is None:
            return FadeWindow(start, start + duration, curve)
        s = start + (duration_to_seconds(o.offset, mix_tempo) if o.offset else 0.0)
        d = duration_to_seconds(o.duration, mix_tempo) if o.duration else duration
        return FadeWindow(s, s + d, o.curve or curve)

    return side(seg.out), side(seg.in_)
