"""Plan → gain envelopes. Engine-agnostic; everything here is in mix-seconds.

An `Envelope` is a piecewise function of mix-time: it starts at `initial`, and
each `Ramp` moves it from `start_value` to `end_value` over
[start_sec, end_sec) with a given shape. Between ramps the value holds. A ramp
with start_sec == end_sec is an instant jump (how `step` keyframes and `cut`
transitions are expressed).

It also resolves the timeline into mix-time spans (`timeline_spans`): where
each play/silence segment sits in the mix, what part of the track it reads,
and at what playback rate. Engine and validator both use it, so segment
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
    FloatKeyframe,
    PlaySegment,
    Plan,
    SilenceSegment,
    TransitionSegment,
)
from dj_segue.time_math import (
    TrackGrid,
    resolve_grid,
    duration_to_seconds,
    mix_pos_to_seconds,
    track_pos_to_seconds,
)

# `equal_power` is internal-only (not a schema interpolation): a sin/cos fade
# whose squared gains sum to 1 across a crossfade, so loudness doesn't dip.
Shape = Literal["linear", "step", "exponential", "equal_power"]


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
class Span:
    """A play or silence segment resolved to mix-time.

    For `play`: the track is read from `track_from_sec` at `rate` (track-seconds
    per mix-second), so it covers track time
    [track_from_sec, track_from_sec + (mix_end_sec - mix_start_sec) * rate).
    Silence spans have track_id None and rate 1.

    Rate is constant per segment for now. When tempo becomes an automation
    lane, this is where a mix-time → track-time map replaces the constant.
    """

    index: int  # timeline index
    kind: Literal["play", "silence"]
    deck: int
    mix_start_sec: float
    mix_end_sec: float
    track_id: str | None = None
    track_from_sec: float = 0.0
    rate: float = 1.0


def segment_rate(seg: PlaySegment, grid: TrackGrid) -> float:
    """Playback rate for a play segment: target tempo over the track's tempo."""
    if seg.target_bpm is None:
        return 1.0
    return seg.target_bpm / grid.bpm


def timeline_spans(
    plan: Plan,
    mix_tempo: float,
    grids: dict[str, TrackGrid] | None = None,
) -> list[Span]:
    """Resolve every play/silence segment to mix-time, in timeline order.

    A segment without `start_at` begins where the previous one on its deck
    ended. Transitions don't occupy decks (they only change gain) and are
    not included. Raises ValueError/TypeError on unresolvable positions (e.g.
    a beat position on an auto-bpm track with no grid).
    """
    grids = grids or {}
    spans: list[Span] = []
    cursor: dict[int, float] = {}
    for i, seg in enumerate(plan.timeline):
        if isinstance(seg, SilenceSegment):
            start = cursor.get(seg.deck, 0.0)
            end = start + duration_to_seconds(seg.duration, mix_tempo)
            spans.append(Span(i, "silence", seg.deck, start, end))
        elif isinstance(seg, PlaySegment):
            track = plan.tracks[seg.track]
            grid = grids.get(seg.track)
            from_sec = track_pos_to_seconds(seg.from_, track, grid)
            to_sec = track_pos_to_seconds(seg.to, track, grid)
            rate = 1.0
            if seg.target_bpm is not None:
                rate = segment_rate(seg, resolve_grid(track, grid))
            if seg.start_at is not None:
                start = mix_pos_to_seconds(seg.start_at, mix_tempo)
            else:
                start = cursor.get(seg.deck, 0.0)
            end = start + max(0.0, to_sec - from_sec) / rate
            spans.append(Span(i, "play", seg.deck, start, end, seg.track, from_sec, rate))
        else:
            continue
        cursor[seg.deck] = end
    return spans


# ---------------------------------------------------------------------------
# Automation lanes
# ---------------------------------------------------------------------------


def keyframes_to_envelope(
    keyframes: list[FloatKeyframe],
    interpolation: str,
    mix_tempo: float,
    source: str,
    *,
    default: float = 1.0,
) -> Envelope:
    if not keyframes:
        return Envelope(initial=default, ramps=(), source=source)
    pts = [(mix_pos_to_seconds(kf.at, mix_tempo), float(kf.value)) for kf in keyframes]
    ramps: list[Ramp] = []
    for (s1, v1), (s2, v2) in zip(pts, pts[1:]):
        if interpolation == "step":
            # Hold v1 until s2, then jump.
            ramps.append(Ramp(s2, s2, v1, v2, "step"))
        else:
            ramps.append(Ramp(s1, s2, v1, v2, interpolation))  # type: ignore[arg-type]
    return Envelope(initial=pts[0][1], ramps=tuple(ramps), source=source)


def deck_volume_envelopes(plan: Plan, deck: int, mix_tempo: float) -> list[Envelope]:
    return [
        keyframes_to_envelope(
            lane.keyframes, lane.interpolation, mix_tempo, f"automation[{i}] deck_volume"
        )
        for i, lane in enumerate(plan.automation)
        if isinstance(lane, DeckVolumeLane) and lane.deck == deck
    ]


def crossfader_envelope(plan: Plan, mix_tempo: float) -> Envelope | None:
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


def transition_envelopes(plan: Plan, mix_tempo: float) -> dict[int, Envelope]:
    """Expand every crossfade/cut transition into per-deck gain envelopes.

    `vocal_handoff` is stem-level (M3) and is not compiled here; executors that
    can't handle it must reject the plan before rendering.
    """
    per_deck: dict[int, list[tuple[Ramp, str]]] = {}
    for i, seg in enumerate(plan.timeline):
        if not isinstance(seg, TransitionSegment) or seg.style == "vocal_handoff":
            continue
        start = mix_pos_to_seconds(seg.start_at, mix_tempo)
        if seg.style == "cut":
            end, shape = start, "step"
        else:
            end = start + duration_to_seconds(seg.duration, mix_tempo)
            shape = "equal_power"
        label = f"timeline[{i}] {seg.style}"
        per_deck.setdefault(seg.from_deck, []).append((Ramp(start, end, 1.0, 0.0, shape), label))
        per_deck.setdefault(seg.to_deck, []).append((Ramp(start, end, 0.0, 1.0, shape), label))

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
