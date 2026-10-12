"""Plan → gain envelopes. Engine-agnostic; everything here is in mix-seconds.

An `Envelope` is a piecewise function of mix-time: it starts at `initial`, and
each `Ramp` moves it from `start_value` to `end_value` over
[start_sec, end_sec) with a given shape. Between ramps the value holds. A ramp
with start_sec == end_sec is an instant jump (how `step` keyframes and `cut`
transitions are expressed).

It also resolves the timeline into mix-time spans (`resolve_timeline`): where
each play/loop/silence segment sits in the mix, what part of the track it
reads (and, for loops, where each repetition starts), and at what playback
rate — plus the end time of every segment with an `id`, which `{"after": id}`
positions resolve against. Engine and validator both use it, so segment
timing is computed in exactly one place.

Two sources compile into envelopes:
  - automation lanes (`deck_volume`, `crossfader`): keyframes → ramps using the
    lane's interpolation;
  - `transition` segments: per-deck fade-out / fade-in ramps. A deck that is
    the target of a transition is silent (gain 0) before its first fade-in;
    a deck that is only ever faded out starts at gain 1.

Executors turn envelopes into per-sample curves (see executor/native/curves.py)
and multiply them together with any other gains on the deck.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dj_segue.schema.plan import (
    CrossfaderLane,
    DeckVolumeLane,
    EqLane,
    FilterLane,
    FloatKeyframe,
    StemVolumeLane,
    LoopSegment,
    PlaySegment,
    Plan,
    SecondsDur,
    SilenceSegment,
    TransitionSegment,
)
from dj_segue.time_math import (
    Anchors,
    SegmentClock,
    TempoMap,
    TrackGrid,
    Warp,
    as_tempo,
    mix_duration,
    resolve_grid,
    duration_to_seconds,
    mix_pos_to_seconds,
    track_pos_to_seconds,
    FadeWindow,
    echo_window,
    handoff_swap,
    transition_windows,
)

# `equal_power`: a sin/cos fade whose squared gains sum to 1 across a
# crossfade, so loudness doesn't dip. Crossfade transitions use it; lanes can
# too (v0.3).
PresetShape = Literal["linear", "step", "exponential", "equal_power"]


@dataclass(frozen=True)
class Points:
    """A drawn curve (v0.5): (t, gain) pairs over the ramp, t in 0–1.

    The gains are absolute (the ramp's start/end values are its first/last
    points). `smooth` = monotone cubic (PCHIP) through the points, which never
    overshoots them; otherwise straight lines.
    """

    points: tuple[tuple[float, float], ...]
    smooth: bool = False


Shape = PresetShape | Points


@dataclass(frozen=True)
class Ramp:
    start_sec: float
    end_sec: float
    start_value: float
    end_value: float
    shape: Shape


@dataclass(frozen=True)
class Envelope:
    initial: float
    ramps: tuple[Ramp, ...]
    source: str  # human-readable origin, for `inspect`

    def value_at(self, sec: float) -> float:
        """Scalar evaluation. Used by inspect/tests; engines render vectorized."""
        v = self.initial
        for r in self.ramps:
            if sec < r.start_sec:
                break
            if sec >= r.end_sec:
                v = r.end_value
                continue
            t = (sec - r.start_sec) / (r.end_sec - r.start_sec)
            return shape_value(r.shape, r.start_value, r.end_value, t)
        return v


# Floor for exponential gain ramps that touch 0: 10**(-60/20). A fade to 0 is
# computed as a fade to -60 dB, then snaps to exactly 0 at the keyframe.
EXP_FLOOR = 1e-3


def shape_value(shape: Shape, v1: float, v2: float, t):
    """Evaluate a ramp shape at fraction t ∈ [0, 1). Works on floats and numpy arrays."""
    import numpy as np

    if isinstance(shape, Points):
        ts = np.array([p[0] for p in shape.points])
        gs = np.array([p[1] for p in shape.points])
        if shape.smooth:
            from scipy.interpolate import PchipInterpolator

            return PchipInterpolator(ts, gs)(np.clip(t, 0.0, 1.0))
        return np.interp(t, ts, gs)
    if shape == "step":
        return v1 + 0.0 * t
    if shape == "linear":
        return v1 + (v2 - v1) * t
    if shape == "equal_power":
        # Rising: sin; falling: cos. Maps [0,1) onto the quarter circle.
        if v2 >= v1:
            return v1 + (v2 - v1) * np.sin(t * np.pi / 2)
        return v2 + (v1 - v2) * np.cos(t * np.pi / 2)
    if shape == "exponential":
        # Constant ratio per unit time (linear in dB). Values ≥ 0 only; a 0
        # endpoint is treated as -60 dB. Sign-changing ranges (e.g. the
        # crossfader's -1..1) have no exponential meaning → linear.
        if v1 < 0 or v2 < 0:
            return v1 + (v2 - v1) * t
        a = max(v1, EXP_FLOOR)
        b = max(v2, EXP_FLOOR)
        return a * (b / a) ** t
    raise ValueError(f"unknown ramp shape {shape!r}")


# ---------------------------------------------------------------------------
# Timeline spans
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopRep:
    """One pass of a loop: plays track [loop start, loop start + track_len_sec)
    over mix-time [mix_start_sec, mix_end_sec). The next pass starts at mix_end_sec."""

    mix_start_sec: float
    mix_end_sec: float
    track_len_sec: float


@dataclass(frozen=True)
class Span:
    """A play, loop or silence segment resolved to mix-time.

    For `play`: the track is read from `track_from_sec` at `rate` (track-seconds
    per mix-second), so it covers track time
    [track_from_sec, track_from_sec + (mix_end_sec - mix_start_sec) * rate).
    For `loop`: every rep in `reps` restarts at `track_from_sec`.
    Silence spans have track_id None and rate 1.

    A `target_bpm: "mix"` segment that crosses a tempo change (v0.9) has a
    `warp` instead: its rate follows the mix tempo (`rate` is the rate at
    its start). Elsewhere the rate is constant and `warp` is None.
    """

    index: int  # timeline index
    kind: Literal["play", "silence", "loop"]
    deck: int
    mix_start_sec: float
    mix_end_sec: float
    track_id: str | None = None
    track_from_sec: float = 0.0
    rate: float = 1.0
    reps: tuple[LoopRep, ...] = ()
    warp: Warp | None = None


@dataclass(frozen=True)
class Timeline:
    spans: list[Span]
    # segment id → the segment's mix end time (seconds); what `{"after": id}`
    # positions resolve against.
    anchors: dict[str, float]


def segment_rate(
    seg: PlaySegment | LoopSegment, grid: TrackGrid, mix_tempo: TempoMap | float,
    at_sec: float = 0.0,
) -> float:
    """Playback rate for a play/loop segment: target tempo (for "mix", the
    mix tempo at `at_sec`) over the track's tempo."""
    if seg.target_bpm is None:
        return 1.0
    target = as_tempo(mix_tempo).bpm_at(at_sec) if seg.target_bpm == "mix" else seg.target_bpm
    return target / grid.bpm


def resolve_timeline(
    plan: Plan,
    mix_tempo: TempoMap | float,
    grids: dict[str, TrackGrid] | None = None,
) -> Timeline:
    """Resolve every segment to mix-time, in timeline order.

    A play/loop/silence segment without `start_at` begins where the previous
    one on its deck ended. Transitions don't occupy decks (they only change
    gain) and aren't spans, but their end times are anchors. An `after`
    position can only name a segment earlier in the timeline. Raises
    ValueError/TypeError on unresolvable positions (e.g. a beat position on
    an auto-bpm track with no grid).

    `mix_tempo` is the tempo map (or a constant bpm). A "mix" segment that
    stays within one constant tempo is timed exactly as with a single mix
    tempo; one that crosses a change gets a `Warp` (its track beats advance
    with mix beats).
    """
    grids = grids or {}
    tempo = as_tempo(mix_tempo)
    spans: list[Span] = []
    anchors = Anchors()  # (+ each play segment's clock, for mix word positions)
    cursor: dict[int, float] = {}

    def start_of(seg) -> float:
        if seg.start_at is not None:
            return mix_pos_to_seconds(seg.start_at, mix_tempo, anchors)
        return cursor.get(seg.deck, 0.0)

    for i, seg in enumerate(plan.timeline):
        if isinstance(seg, TransitionSegment):
            if seg.id is not None:
                # A transition ends when its later side (or echo tail, or
                # vocal swap) does.
                out_w, in_w = transition_windows(seg, mix_tempo, anchors)
                end = max(out_w.end_sec, in_w.end_sec)
                if seg.style == "vocal_handoff":
                    end = max(end, handoff_swap(seg, mix_tempo, anchors))
                if seg.style == "echo_out":
                    end = max(end, echo_window(seg, mix_tempo, anchors).end_sec)
                anchors[seg.id] = end
            continue
        if isinstance(seg, SilenceSegment):
            start = cursor.get(seg.deck, 0.0)
            end = start + mix_duration(start, seg.duration, tempo)
            spans.append(Span(i, "silence", seg.deck, start, end))
        elif isinstance(seg, PlaySegment):
            track = plan.tracks[seg.track]
            grid = grids.get(seg.track)
            from_sec = track_pos_to_seconds(seg.from_, track, grid)
            to_sec = track_pos_to_seconds(seg.to, track, grid)
            start = start_of(seg)
            rate, warp = 1.0, None
            if seg.target_bpm is not None:
                rate = segment_rate(seg, resolve_grid(track, grid), tempo, start)
            length = max(0.0, to_sec - from_sec)
            end = start + length / rate
            if seg.target_bpm == "mix" and not tempo.constant_between(start, end):
                warp = Warp(tempo, resolve_grid(track, grid).bpm, start, from_sec)
                end = warp.mix_sec(from_sec + length)
            spans.append(
                Span(i, "play", seg.deck, start, end, seg.track, from_sec, rate, warp=warp)
            )
            if seg.id is not None:
                anchors.clocks[seg.id] = SegmentClock(grid, from_sec, to_sec, start, rate, warp)
        elif isinstance(seg, LoopSegment):
            track = plan.tracks[seg.track]
            grid = grids.get(seg.track)
            from_sec = track_pos_to_seconds(seg.from_, track, grid)
            start = start_of(seg)
            rate, warp = 1.0, None
            if seg.target_bpm is not None:
                rate = segment_rate(seg, resolve_grid(track, grid), tempo, start)
            lengths = [
                _track_duration_sec(step.length, track, grid)
                for step in seg.schedule
                for _ in range(step.repetitions)
            ]
            reps: list[LoopRep] = []
            t = start
            for track_len in lengths:
                reps.append(LoopRep(t, t + track_len / rate, track_len))
                t += track_len / rate
            if seg.target_bpm == "mix" and not tempo.constant_between(start, t):
                # Each repetition replays the region from `from` on the
                # mix's beats: its own warp, starting at its own start.
                warp = Warp(tempo, resolve_grid(track, grid).bpm, start, from_sec)
                reps, t = [], start
                for track_len in lengths:
                    nxt = warp.starting(t).mix_sec(from_sec + track_len)
                    reps.append(LoopRep(t, nxt, track_len))
                    t = nxt
            end = t
            spans.append(
                Span(i, "loop", seg.deck, start, end, seg.track, from_sec, rate,
                     tuple(reps), warp)
            )
        else:
            continue
        if seg.id is not None:
            anchors[seg.id] = end
        cursor[seg.deck] = end
    return Timeline(spans, anchors)


def timeline_spans(
    plan: Plan,
    mix_tempo: TempoMap | float,
    grids: dict[str, TrackGrid] | None = None,
) -> list[Span]:
    """The play/loop/silence spans of `resolve_timeline`."""
    return resolve_timeline(plan, mix_tempo, grids).spans


def _track_duration_sec(dur, track, grid: TrackGrid | None) -> float:
    """A track-time duration (beats/bars at the track's tempo) in track seconds."""
    if isinstance(dur, SecondsDur):
        return float(dur.seconds)
    return duration_to_seconds(dur, resolve_grid(track, grid).bpm)


# ---------------------------------------------------------------------------
# Automation lanes
# ---------------------------------------------------------------------------


def keyframes_to_envelope(
    keyframes: list[FloatKeyframe],
    interpolation: str,
    mix_tempo: TempoMap | float,
    source: str,
    *,
    default: float = 1.0,
    anchors: dict[str, float] | None = None,
) -> Envelope:
    if not keyframes:
        return Envelope(initial=default, ramps=(), source=source)
    pts = [
        (mix_pos_to_seconds(kf.at, mix_tempo, anchors), float(kf.value))
        for kf in keyframes
    ]
    ramps: list[Ramp] = []
    for (s1, v1), (s2, v2) in zip(pts, pts[1:]):
        if interpolation == "step":
            # Hold v1 until s2, then jump.
            ramps.append(Ramp(s2, s2, v1, v2, "step"))
        else:
            ramps.append(Ramp(s1, s2, v1, v2, interpolation))  # type: ignore[arg-type]
    return Envelope(initial=pts[0][1], ramps=tuple(ramps), source=source)


def eq_envelope(
    plan: Plan,
    deck: int,
    band: str,
    mix_tempo: TempoMap | float,
    anchors: dict[str, float] | None = None,
    stem: str | None = None,
) -> Envelope | None:
    """A deck's (or one of its stems', v0.8) EQ band gain in dB (0 = flat),
    or None without a lane for it. `linear` interpolation is linear in dB.
    One lane per deck, band and stem (validator)."""
    for i, lane in enumerate(plan.automation):
        if (isinstance(lane, EqLane) and lane.deck == deck and lane.band == band
                and lane.stem == stem):
            return keyframes_to_envelope(
                [FloatKeyframe(at=kf.at, value=kf.value_db) for kf in lane.keyframes],
                lane.interpolation,
                mix_tempo,
                f"automation[{i}] eq {band}",
                default=0.0,
                anchors=anchors,
            )
    return None


def filter_envelope(
    plan: Plan, deck: int, mix_tempo: TempoMap | float, anchors: dict[str, float] | None = None
) -> Envelope | None:
    """A deck's filter knob position (-1..+1, 0 = off), or None without a
    filter lane. One lane per deck (validator)."""
    for i, lane in enumerate(plan.automation):
        if isinstance(lane, FilterLane) and lane.deck == deck:
            return keyframes_to_envelope(
                lane.keyframes, lane.interpolation, mix_tempo,
                f"automation[{i}] filter", default=0.0, anchors=anchors,
            )
    return None


def filter_decks(plan: Plan) -> set[int]:
    return {lane.deck for lane in plan.automation if isinstance(lane, FilterLane)}


def eq_decks(plan: Plan) -> set[int]:
    """Decks with whole-deck EQ."""
    return {lane.deck for lane in plan.automation if isinstance(lane, EqLane) and not lane.stem}


def eq_stems(plan: Plan) -> set[tuple[int, str]]:
    """(deck, stem) pairs with their own EQ (v0.8)."""
    return {(lane.deck, lane.stem) for lane in plan.automation
            if isinstance(lane, EqLane) and lane.stem}


def deck_volume_envelopes(
    plan: Plan, deck: int, mix_tempo: TempoMap | float, anchors: dict[str, float] | None = None
) -> list[Envelope]:
    return [
        keyframes_to_envelope(
            lane.keyframes,
            lane.interpolation,
            mix_tempo,
            f"automation[{i}] deck_volume",
            anchors=anchors,
        )
        for i, lane in enumerate(plan.automation)
        if isinstance(lane, DeckVolumeLane) and lane.deck == deck
    ]


def crossfader_envelope(
    plan: Plan, mix_tempo: TempoMap | float, anchors: dict[str, float] | None = None
) -> Envelope | None:
    """The crossfader *position* envelope (-1..+1), or None if the plan has no
    crossfader lane. Multiple crossfader lanes are rejected by the validator."""
    for i, lane in enumerate(plan.automation):
        if isinstance(lane, CrossfaderLane):
            return keyframes_to_envelope(
                lane.keyframes,
                lane.interpolation,
                mix_tempo,
                f"automation[{i}] crossfader",
                default=0.0,
                anchors=anchors,
            )
    return None


def crossfader_gains(position):
    """Equal-power crossfader law: -1 → (1, 0), 0 → (√½, √½), +1 → (0, 1).

    Returns (deck1_gain, deck2_gain). Works on floats and numpy arrays. Only
    decks 1 and 2 sit on the crossfader; decks 3–4 are unaffected (the schema
    leaves multi-deck crossfading undefined).
    """
    import numpy as np

    theta = (np.clip(position, -1.0, 1.0) + 1.0) * np.pi / 4
    return np.cos(theta), np.sin(theta)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def _shape(curve) -> Shape:
    """A FadeWindow curve as a ramp shape: preset names pass through; a drawn
    (schema PointCurve) curve becomes Points."""
    if isinstance(curve, str):
        return curve  # type: ignore[return-value]
    return Points(tuple((float(t), float(g)) for t, g in curve.points), curve.smooth)


# A vocal_handoff's vocals cross over this long, ending on the swap (so the
# incoming vocal's first word is untouched), like a loop seam.
VOCAL_SWAP_SEC = 0.010


@dataclass(frozen=True)
class Handoff:
    """A vocal_handoff in mix seconds: the instrumentals' fades, the vocal
    swap, and the span it all covers."""

    out: FadeWindow
    in_: FadeWindow
    swap_sec: float
    start_sec: float
    end_sec: float


def handoff_windows(
    seg: TransitionSegment, mix_tempo: TempoMap | float, anchors: dict[str, float] | None = None
) -> Handoff:
    out_w, in_w = transition_windows(seg, mix_tempo, anchors)
    swap = handoff_swap(seg, mix_tempo, anchors)
    return Handoff(
        out_w, in_w, swap,
        min(in_w.start_sec, out_w.start_sec, swap - VOCAL_SWAP_SEC),
        max(out_w.end_sec, in_w.end_sec, swap),
    )


def stem_decks(plan: Plan) -> set[int]:
    from dj_segue.schema.validator import stem_decks as _stem_decks

    return _stem_decks(plan)


def stem_envelopes(
    plan: Plan, deck: int, stem: str, mix_tempo: TempoMap | float, anchors: dict[str, float] | None = None
) -> list[Envelope]:
    """Gain envelopes for one stem of a deck (multiplied together): its
    stem_volume lanes, and each vocal_handoff on the deck.

    In a handoff the outgoing deck's instrumental stems fade over `out`
    and its vocals stop at the swap; the incoming deck's instrumentals fade
    in over `in` and its vocals start at the swap. Outside a handoff's span
    its envelopes are 1 (the deck-level gain switches the deck on and off).
    """
    envs = [
        keyframes_to_envelope(
            lane.keyframes, lane.interpolation, mix_tempo,
            f"automation[{i}] stem_volume {stem}", anchors=anchors,
        )
        for i, lane in enumerate(plan.automation)
        if isinstance(lane, StemVolumeLane) and lane.deck == deck and lane.stem == stem
    ]
    for i, seg in enumerate(plan.timeline):
        if not (isinstance(seg, TransitionSegment) and seg.style == "vocal_handoff"):
            continue
        if deck not in (seg.from_deck, seg.to_deck):
            continue
        h = handoff_windows(seg, mix_tempo, anchors)
        vocal_lo = h.swap_sec - VOCAL_SWAP_SEC
        if deck == seg.from_deck:
            fade = (Ramp(vocal_lo, h.swap_sec, 1.0, 0.0, "linear") if stem == "vocals" else
                    Ramp(h.out.start_sec, h.out.end_sec, 1.0, 0.0, _shape(h.out.curve)))
            ramps = (fade, Ramp(h.end_sec, h.end_sec, 0.0, 1.0, "step"))
        else:
            fade = (Ramp(vocal_lo, h.swap_sec, 0.0, 1.0, "linear") if stem == "vocals" else
                    Ramp(h.in_.start_sec, h.in_.end_sec, 0.0, 1.0, _shape(h.in_.curve)))
            ramps = (Ramp(h.start_sec, h.start_sec, 1.0, 0.0, "step"), fade)
        envs.append(Envelope(1.0, ramps, f"timeline[{i}] vocal_handoff {stem}"))
    return envs


def transition_envelopes(
    plan: Plan, mix_tempo: TempoMap | float, anchors: dict[str, float] | None = None
) -> dict[int, Envelope]:
    """Expand every transition into per-deck gain envelopes.

    `anchors` (Timeline.anchors) is needed when a transition starts at an
    `after` position.

    A vocal_handoff (v0.8) shapes its decks by stem (`stem_envelopes`); at
    deck level the incoming deck just switches on where the handoff begins
    and the outgoing one off where it ends.
    """
    per_deck: dict[int, list[tuple[Ramp, str]]] = {}
    for i, seg in enumerate(plan.timeline):
        if not isinstance(seg, TransitionSegment):
            continue
        label = f"timeline[{i}] {seg.style}"
        if seg.style == "vocal_handoff":
            h = handoff_windows(seg, mix_tempo, anchors)
            per_deck.setdefault(seg.from_deck, []).append(
                (Ramp(h.end_sec, h.end_sec, 1.0, 0.0, "step"), label)
            )
            per_deck.setdefault(seg.to_deck, []).append(
                (Ramp(h.start_sec, h.start_sec, 0.0, 1.0, "step"), label)
            )
            continue
        out_w, in_w = transition_windows(seg, mix_tempo, anchors)
        per_deck.setdefault(seg.from_deck, []).append(
            (Ramp(out_w.start_sec, out_w.end_sec, 1.0, 0.0, _shape(out_w.curve)), label)
        )
        per_deck.setdefault(seg.to_deck, []).append(
            (Ramp(in_w.start_sec, in_w.end_sec, 0.0, 1.0, _shape(in_w.curve)), label)
        )

    out: dict[int, Envelope] = {}
    for deck, items in per_deck.items():
        items.sort(key=lambda it: it[0].start_sec)
        ramps = tuple(r for r, _ in items)
        out[deck] = Envelope(
            initial=ramps[0].start_value,
            ramps=ramps,
            source=", ".join(label for _, label in items),
        )
    return out
