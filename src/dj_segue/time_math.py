"""Time conversions: positions/durations ↔ seconds, beats, samples.

All conversions assume 4/4 time (the schema doesn't expose time signature in v0.1).

Mix-time beats go through a `TempoMap` (v0.9): the mix tempo, with steps and
linear ramps (`meta.tempo_map`). Every function taking a mix tempo accepts a
TempoMap or a plain bpm (a constant map). On a constant stretch of the map the
arithmetic is exactly the single-tempo arithmetic, so plans without tempo
changes resolve to the same seconds, bit for bit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
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
    WordPos,
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
    # v0.10: the track's sung words, (normalized text, start s, end s) in
    # order — filled by the preprocessor when the plan uses word positions.
    words: tuple = field(default=(), compare=False, repr=False)
    # Cues marked in the track's annotation (name → second): a plan's cue
    # that the plan doesn't declare is looked up here.
    cues: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def fixed(cls, bpm: float, anchor_sec: float = 0.0) -> "TrackGrid":
        return cls(anchor_sec=anchor_sec, bpm=bpm)

    def beat_to_seconds(self, beat: float) -> float:
        return self.anchor_sec + beat * 60.0 / self.bpm

    def bar_to_seconds(self, bar: float) -> float:
        return self.beat_to_seconds(bar * BEATS_PER_BAR)


@dataclass(frozen=True)
class TempoPiece:
    """One stretch of a TempoMap: from `t0` seconds (mix beat `b0`) the tempo
    moves linearly in time from `bpm0` to `bpm1` over `dur` seconds (`bpm0 ==
    bpm1`: constant). The last piece of a map is constant and endless."""

    t0: float
    b0: float
    bpm0: float
    bpm1: float
    dur: float = math.inf

    @property
    def t1(self) -> float:
        return self.t0 + self.dur

    @property
    def b1(self) -> float:
        return self.b0 + self.beats(self.dur) if self.dur != math.inf else math.inf

    @property
    def constant(self) -> bool:
        return self.bpm0 == self.bpm1

    def bpm(self, t: float) -> float:
        if self.constant:
            return self.bpm0
        return self.bpm0 + (self.bpm1 - self.bpm0) * (t - self.t0) / self.dur

    def beats(self, tau: float) -> float:
        """Beats elapsed `tau` seconds after t0."""
        if self.constant:
            return tau * self.bpm0 / 60.0
        a = (self.bpm1 - self.bpm0) / (2.0 * self.dur)
        return (self.bpm0 * tau + a * tau * tau) / 60.0

    def seconds(self, beats: float) -> float:
        """Seconds after t0 at which `beats` have elapsed."""
        if self.constant:
            return beats * 60.0 / self.bpm0
        a = (self.bpm1 - self.bpm0) / (2.0 * self.dur)
        c = 60.0 * beats
        # a·τ² + bpm0·τ − c = 0, in the form that's stable as a → 0.
        return 2.0 * c / (self.bpm0 + math.sqrt(self.bpm0 * self.bpm0 + 4.0 * a * c))


@dataclass(frozen=True)
class TempoMap:
    """The mix tempo over time (v0.9): constant pieces and linear ramps,
    covering all of mix time. Mix beat 0 is at 0 s; before 0 the first
    piece's tempo extends backwards.

    A ramp is linear in *time*, like moving a pitch fader at a steady speed:
    a ramp from b0 to b1 bpm lasting N beats takes 120·N / (b0 + b1) seconds.
    """

    pieces: tuple[TempoPiece, ...]

    @classmethod
    def fixed(cls, bpm: float) -> "TempoMap":
        return cls((TempoPiece(0.0, 0.0, bpm, bpm),))

    @property
    def start_bpm(self) -> float:
        return self.pieces[0].bpm0

    @property
    def constant(self) -> bool:
        return len(self.pieces) == 1

    def _at_sec(self, t: float) -> TempoPiece:
        for p in reversed(self.pieces):
            if t >= p.t0:
                return p
        return self.pieces[0]

    def _at_beat(self, b: float) -> TempoPiece:
        for p in reversed(self.pieces):
            if b >= p.b0:
                return p
        return self.pieces[0]

    def bpm_at(self, t: float) -> float:
        """The tempo at mix second `t` (at a step: the new tempo)."""
        return self._at_sec(t).bpm(t)

    def bpm_before(self, t: float) -> float:
        """The tempo just before mix second `t` (at a step: the old tempo)."""
        p = self._at_sec(t)
        if t == p.t0 and p is not self.pieces[0]:
            return self.pieces[self.pieces.index(p) - 1].bpm1
        return p.bpm(t)

    def beat_to_sec(self, b: float) -> float:
        p = self._at_beat(b)
        return p.t0 + p.seconds(b - p.b0)

    def sec_to_beat(self, t: float) -> float:
        p = self._at_sec(t)
        return p.b0 + p.beats(t - p.t0)

    def constant_between(self, t0: float, t1: float) -> bool:
        """True when [t0, t1] lies in one constant piece."""
        p = self._at_sec(min(t0, t1))
        return p.constant and max(t0, t1) <= p.t1

    def advance(self, t: float, beats: float) -> float:
        """The mix second `beats` mix beats after `t` (before, if negative)."""
        p = self._at_sec(t)
        if p.constant:
            end = t + beats * 60.0 / p.bpm0
            if (p is self.pieces[0] or end >= p.t0) and end <= p.t1:
                return end
        return self.beat_to_sec(self.sec_to_beat(t) + beats)

    def with_change(self, at: float, bpm: float, ramp_sec: float = 0.0) -> "TempoMap":
        """This map with a change at mix second `at`: a step to `bpm`, or a
        linear ramp to it over `ramp_sec`. `at` must not come before the end
        of the last change."""
        last = self.pieces[-1]
        if at < last.t0:
            raise ValueError(
                f"a tempo change at {at:.3f}s comes before the previous one "
                f"(ends at {last.t0:.3f}s); list changes in time order"
            )
        b_at = last.b0 + last.beats(at - last.t0)
        kept = self.pieces[:-1]
        if at > last.t0:
            kept += (TempoPiece(last.t0, last.b0, last.bpm0, last.bpm0, at - last.t0),)
        if ramp_sec > 0:
            ramp = TempoPiece(at, b_at, last.bpm0, bpm, ramp_sec)
            return TempoMap(kept + (ramp, TempoPiece(ramp.t1, ramp.b1, bpm, bpm)))
        return TempoMap(kept + (TempoPiece(at, b_at, bpm, bpm),))


def as_tempo(tempo: "TempoMap | float") -> TempoMap:
    return tempo if isinstance(tempo, TempoMap) else TempoMap.fixed(float(tempo))


def ramp_seconds(dur: Any, bpm0: float, bpm1: float) -> float:
    """A tempo ramp's length in seconds: `dur` is the mix beats (or bars, or
    seconds) the ramp lasts, at a tempo moving linearly from bpm0 to bpm1."""
    if isinstance(dur, SecondsDur):
        return float(dur.seconds)
    beats = dur.beats if isinstance(dur, BeatsDur) else dur.bars * BEATS_PER_BAR
    return 120.0 * beats / (bpm0 + bpm1)


def mix_duration(start: float, dur: Any, tempo: "TempoMap | float") -> float:
    """A mix-time duration starting at mix second `start`, in seconds. Beats
    and bars count through the tempo map from `start` (negative durations
    count back)."""
    if isinstance(dur, SecondsDur):
        return float(dur.seconds)
    tm = as_tempo(tempo)
    beats = dur.beats if isinstance(dur, BeatsDur) else dur.bars * BEATS_PER_BAR
    if tm.constant_between(start, start + beats * 60.0 / tm.bpm_at(start)):
        return duration_to_seconds(dur, tm.bpm_at(start))
    return tm.advance(start, beats) - start


@dataclass(frozen=True)
class Warp:
    """How a segment following a changing mix tempo (`target_bpm: "mix"`
    over a tempo change) reads its track: track beats advance with mix beats.
    The track is at `track_from_sec` at mix second `mix_start_sec`."""

    tempo: TempoMap
    track_bpm: float
    mix_start_sec: float
    track_from_sec: float

    def track_sec(self, mix_sec: float) -> float:
        tm = self.tempo
        beats = tm.sec_to_beat(mix_sec) - tm.sec_to_beat(self.mix_start_sec)
        return self.track_from_sec + beats * 60.0 / self.track_bpm

    def mix_sec(self, track_sec: float) -> float:
        tm = self.tempo
        beats = (track_sec - self.track_from_sec) * self.track_bpm / 60.0
        return tm.beat_to_sec(tm.sec_to_beat(self.mix_start_sec) + beats)

    def rate_at_track(self, track_sec: float) -> float:
        """Playback rate (track-seconds per mix-second) where the track is
        at `track_sec`."""
        return self.tempo.bpm_at(self.mix_sec(track_sec)) / self.track_bpm

    def steps(self, mix_from: float, mix_to: float) -> list[float]:
        """Track seconds where the rate jumps (tempo steps) between two mix
        seconds."""
        tm = self.tempo
        return [
            self.track_sec(b.t0)
            for a, b in zip(tm.pieces, tm.pieces[1:])
            if mix_from <= b.t0 <= mix_to and a.bpm1 != b.bpm0
        ]

    def starting(self, mix_start_sec: float) -> "Warp":
        """The same warp with the track at `track_from_sec` at another mix
        second (a loop's next repetition)."""
        return Warp(self.tempo, self.track_bpm, mix_start_sec, self.track_from_sec)


def mix_pos_to_seconds(
    pos: Any, mix_tempo: "TempoMap | float", anchors: dict[str, float] | None = None
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
        start = anchors[pos.after]
        offset = 0.0 if pos.offset is None else mix_duration(start, pos.offset, mix_tempo)
        return start + offset
    if isinstance(pos, BeatPos):
        return as_tempo(mix_tempo).beat_to_sec(pos.beat)
    if isinstance(pos, BarPos):
        return as_tempo(mix_tempo).beat_to_sec(pos.bar * BEATS_PER_BAR)
    if isinstance(pos, SecondPos):
        return float(pos.second)
    if isinstance(pos, CuePos):
        raise ValueError(f"Cue references are not valid in mix-time context: {pos!r}")
    if isinstance(pos, WordPos):
        if pos.in_ is None:
            raise ValueError(f'a word in a mix position needs "in": the play segment that sings it ({pos.word!r})')
        clock = getattr(anchors, "clocks", {}).get(pos.in_)
        if clock is None:
            raise ValueError(
                f"word {pos.word!r} in {pos.in_!r}: no earlier play segment with that id "
                f"(or the timeline isn't resolved yet)"
            )
        t = clock.mix_sec(word_sec(pos, clock.grid), pos)
        return t if pos.offset is None else t + mix_duration(t, pos.offset, mix_tempo)
    raise TypeError(f"Unknown position type: {type(pos).__name__}")


class Anchors(dict):
    """Segment id → mix end seconds (what `after` positions need), plus
    `clocks`: play segment id → its SegmentClock (what mix word positions
    need, v0.10)."""

    def __init__(self, *args, **kw) -> None:
        super().__init__(*args, **kw)
        self.clocks: dict[str, SegmentClock] = {}


@dataclass(frozen=True)
class SegmentClock:
    """Where a play segment puts its track in the mix."""

    grid: "TrackGrid | None"
    from_sec: float  # track seconds it plays…
    to_sec: float
    start_sec: float  # …from this mix second
    rate: float
    warp: "Warp | None" = None

    def mix_sec(self, track_sec: float, pos=None) -> float:
        if not self.from_sec - 1e-6 <= track_sec <= self.to_sec + 1e-6:
            what = f"word {pos.word!r} (nth {pos.nth})" if pos is not None else "that position"
            raise ValueError(
                f"{what} is sung at {track_sec:.2f}s in the track, outside the part segment "
                f"{pos.in_ if pos is not None else ''!r} plays ({self.from_sec:.2f}–{self.to_sec:.2f}s)"
            )
        if self.warp is not None:
            return self.warp.mix_sec(track_sec)
        return self.start_sec + (track_sec - self.from_sec) / self.rate


def word_sec(pos: WordPos, grid: "TrackGrid | None") -> float:
    """Track seconds where `pos.word` (a word or phrase) is sung, its `nth`
    time; its start, or with edge "end" the end of its last word."""
    from dj_segue.words.merge import norm

    words = grid.words if grid is not None else ()
    if not words:
        raise ValueError(
            f"word {pos.word!r}: no word timing for this track (run `dj-segue words <audio file>`)"
        )
    target = [norm(t) for t in pos.word.split()]
    target = [t for t in target if t]
    if not target:
        raise ValueError(f"word {pos.word!r} has no letters or digits to match")
    n = len(target)
    hits = [i for i in range(len(words) - n + 1)
            if all(words[i + k][0] == target[k] for k in range(n))]
    idx = pos.nth - 1 if pos.nth > 0 else len(hits) + pos.nth
    if not 0 <= idx < len(hits):
        raise ValueError(
            f"word {pos.word!r} is sung {len(hits)} time{'s' * (len(hits) != 1)}; "
            f"nth {pos.nth} is out of range"
        )
    i = hits[idx]
    return float(words[i][1] if pos.edge == "start" else words[i + n - 1][2])


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
            marked = grid.cues if grid is not None else {}
            if pos.cue in marked:  # marked in the track's annotation
                return float(marked[pos.cue])
            raise ValueError(
                f"cue {pos.cue!r} isn't declared on the track or marked in its annotation; "
                f"available: {sorted(set(track.cues) | set(marked))}"
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
    if isinstance(pos, WordPos):
        if pos.in_ is not None:
            raise ValueError(f'"in" is for mix positions; in a track position, word {pos.word!r} is this track\'s')
        t = word_sec(pos, grid)
        if pos.offset is None:
            return t
        if isinstance(pos.offset, SecondsDur):
            return t + float(pos.offset.seconds)
        return t + duration_to_seconds(pos.offset, resolve_grid(track, grid).bpm)
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
    # A ramp shape name ("equal_power" | "linear" | "exponential" | "step"),
    # or a drawn schema PointCurve (v0.5).
    curve: Any


def transition_windows(
    seg: TransitionSegment, mix_tempo: "TempoMap | float", anchors: dict[str, float] | None = None
) -> tuple[FadeWindow, FadeWindow]:
    """(out, in): the from_deck's fade-out and the to_deck's fade-in.

    A cut is a zero-length step at `start_at`. A crossfade's sides default to
    `start_at` + `duration` with `curve` (equal_power); v0.4 `out` / `in`
    override offset, duration and curve per side.

    An echo_out (v0.6) cuts the dry from_deck at `start_at` (its echo tail is
    `echo_window`); the to_deck comes in on the cut unless `in` gives it a
    fade (`duration` there defaults to 0, not the tail length).

    Durations count mix beats from where they start (v0.9 tempo map): a
    side's `duration` from that side's start.
    """
    start = mix_pos_to_seconds(seg.start_at, mix_tempo, anchors)
    if seg.style == "cut":
        w = FadeWindow(start, start, "step")
        return w, w
    duration = mix_duration(start, seg.duration, mix_tempo)
    curve = seg.curve or "equal_power"
    echo = seg.style == "echo_out"
    if echo:
        duration = 0.0  # the default `in`: a step on the cut

    def side(o) -> FadeWindow:
        s, d, c = start, duration, curve
        if o is not None:
            s += mix_duration(s, o.offset, mix_tempo) if o.offset else 0.0
            d = mix_duration(s, o.duration, mix_tempo) if o.duration else duration
            c = o.curve or curve
        return FadeWindow(s, s + d, "step" if echo and d == 0 else c)

    if seg.style == "echo_out":
        return FadeWindow(start, start, "step"), side(seg.in_)
    return side(seg.out), side(seg.in_)


def handoff_swap(
    seg: TransitionSegment, mix_tempo: "TempoMap | float", anchors: dict[str, float] | None = None
) -> float:
    """A vocal_handoff's vocal switch, in mix seconds (v0.8): `start_at` +
    `swap_at.offset`, by default halfway through `duration`; or (v0.10) when
    a word is sung."""
    if isinstance(seg.swap_at, WordPos):  # v0.10: on a word
        return mix_pos_to_seconds(seg.swap_at, mix_tempo, anchors)
    start = mix_pos_to_seconds(seg.start_at, mix_tempo, anchors)
    if seg.swap_at is None:
        return start + mix_duration(start, seg.duration, mix_tempo) / 2
    return start + mix_duration(start, seg.swap_at.offset, mix_tempo)


@dataclass(frozen=True)
class EchoWindow:
    """An echo_out's tail (v0.6), in mix seconds: repeats of the `every_sec`
    before `cut_sec`, from the cut until `end_sec`."""

    cut_sec: float
    every_sec: float
    end_sec: float
    feedback: float
    highpass: float = 0.0  # v0.7: tail's filter knob at end_sec (0 = none)


def echo_window(
    seg: TransitionSegment, mix_tempo: "TempoMap | float", anchors: dict[str, float] | None = None
) -> EchoWindow:
    """The repeats keep the outgoing tempo (`every` at the tempo just
    before the cut, even when the mix changes tempo on it); the tail's
    `duration` counts mix beats from the cut."""
    cut = mix_pos_to_seconds(seg.start_at, mix_tempo, anchors)
    return EchoWindow(
        cut,
        duration_to_seconds(seg.echo.every, as_tempo(mix_tempo).bpm_before(cut)),
        cut + mix_duration(cut, seg.duration, mix_tempo),
        seg.echo.feedback,
        seg.echo.highpass,
    )
