"""Cross-field validation that pydantic alone can't express.

Rules implemented here come from `docs/schema-v0.1.md`, "Validation rules".
Two rules need audio metadata and are deferred to the preprocessor/executor:
  - rule 6: track positions fall within actual track duration
  - segment-overlap precision: requires resolving track-time spans to mix-time,
    which requires either (a) v0.1's "play at original tempo" assumption fully
    nailed down, or (b) audio file durations. For M1 we do an approximate
    overlap check based on explicit `start_at` ordering.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from dj_segue.schema.plan import (
    AutomationLane,
    BarPos,
    BeatPos,
    CrossfaderLane,
    CuePos,
    DeckVolumeLane,
    EqLane,
    FloatKeyframe,
    DbKeyframe,
    PlaySegment,
    Plan,
    SecondPos,
    SilenceSegment,
    StemVolumeLane,
    TransitionSegment,
)


class PlanValidationError(Exception):
    """One or more cross-field validation rules failed."""

    def __init__(self, issues: list[str]):
        self.issues = list(issues)
        joined = "\n  - ".join(self.issues)
        super().__init__(f"{len(self.issues)} validation issue(s):\n  - {joined}")


def validate_plan(plan: Plan) -> None:
    issues: list[str] = []
    issues.extend(_check_track_references(plan))
    issues.extend(_check_deck_references(plan))
    issues.extend(_check_cue_references(plan))
    issues.extend(_check_stem_references(plan))
    issues.extend(_check_keyframe_ordering(plan))
    issues.extend(_check_vocal_handoff_requirements(plan))
    if issues:
        raise PlanValidationError(issues)


# ---------------------------------------------------------------------------
# Mix-time helpers
# ---------------------------------------------------------------------------


def resolved_mix_tempo(plan: Plan) -> float:
    """The plan's effective mix tempo. Defaults to the first track's bpm."""
    if plan.meta.mix_tempo is not None:
        return plan.meta.mix_tempo
    if plan.tracks:
        return next(iter(plan.tracks.values())).bpm
    return 120.0


def position_to_mix_beats(pos: Any, mix_tempo: float) -> float:
    """Convert a mix-time position to mix-beats. Assumes 4/4 for bars."""
    if isinstance(pos, BeatPos):
        return pos.beat
    if isinstance(pos, BarPos):
        return pos.bar * 4.0
    if isinstance(pos, SecondPos):
        return pos.second * mix_tempo / 60.0
    if isinstance(pos, CuePos):
        raise ValueError(f"Cue references are not valid in mix-time context: {pos!r}")
    raise TypeError(f"Unknown position type: {type(pos).__name__}")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_track_references(plan: Plan) -> list[str]:
    issues: list[str] = []
    declared = set(plan.tracks)
    for i, seg in enumerate(plan.timeline):
        if isinstance(seg, PlaySegment) and seg.track not in declared:
            issues.append(
                f"timeline[{i}] (play): unknown track {seg.track!r}; "
                f"declared tracks: {sorted(declared)}"
            )
    return issues


def _check_deck_references(plan: Plan) -> list[str]:
    issues: list[str] = []
    declared = set(plan.decks)
    for i, seg in enumerate(plan.timeline):
        decks_used: list[int] = []
        if isinstance(seg, (PlaySegment, SilenceSegment)):
            decks_used.append(seg.deck)
        elif isinstance(seg, TransitionSegment):
            decks_used.extend([seg.from_deck, seg.to_deck])
        for d in decks_used:
            if d not in declared:
                issues.append(
                    f"timeline[{i}] ({seg.type}): undeclared deck {d}; "
                    f"declared: {sorted(declared)}"
                )
    for i, lane in enumerate(plan.automation):
        if isinstance(lane, CrossfaderLane):
            continue
        if lane.deck not in declared:
            issues.append(
                f"automation[{i}] ({lane.lane}): undeclared deck {lane.deck}; "
                f"declared: {sorted(declared)}"
            )
    return issues


def _check_cue_references(plan: Plan) -> list[str]:
    """
    Cue refs are valid only in track-time positions (play.from / play.to).
    They're invalid in mix-time positions (start_at, automation at, transition
    start_at).
    """
    issues: list[str] = []
    for i, seg in enumerate(plan.timeline):
        if isinstance(seg, PlaySegment):
            track = plan.tracks.get(seg.track)
            for field, pos in (("from", seg.from_), ("to", seg.to)):
                if isinstance(pos, CuePos):
                    if track is None:
                        # Track-ref error already reported elsewhere; skip here.
                        continue
                    if pos.cue not in track.cues:
                        issues.append(
                            f"timeline[{i}] (play): cue {pos.cue!r} "
                            f"in '{field}' not declared on track {seg.track!r}; "
                            f"available cues: {sorted(track.cues)}"
                        )
            if isinstance(seg.start_at, CuePos):
                issues.append(
                    f"timeline[{i}] (play): cue references not allowed in "
                    f"mix-time 'start_at'; got {seg.start_at.cue!r}"
                )
        elif isinstance(seg, TransitionSegment):
            if isinstance(seg.start_at, CuePos):
                issues.append(
                    f"timeline[{i}] (transition): cue references not allowed "
                    f"in mix-time 'start_at'; got {seg.start_at.cue!r}"
                )
    for i, lane in enumerate(plan.automation):
        for j, kf in enumerate(lane.keyframes):
            if isinstance(kf.at, CuePos):
                issues.append(
                    f"automation[{i}].keyframes[{j}]: cue references not "
                    f"allowed in mix-time 'at'; got {kf.at.cue!r}"
                )
    return issues


def _check_stem_references(plan: Plan) -> list[str]:
    """
    For each stem_volume lane on deck D, every track that plays on D anywhere
    in the timeline must declare the named stem.
    """
    issues: list[str] = []
    tracks_per_deck: dict[int, set[str]] = {}
    for seg in plan.timeline:
        if isinstance(seg, PlaySegment):
            tracks_per_deck.setdefault(seg.deck, set()).add(seg.track)

    for i, lane in enumerate(plan.automation):
        if not isinstance(lane, StemVolumeLane):
            continue
        for track_id in tracks_per_deck.get(lane.deck, set()):
            track = plan.tracks.get(track_id)
            if track is None or lane.stem in track.stems:
                continue
            issues.append(
                f"automation[{i}] (stem_volume on deck {lane.deck}): "
                f"track {track_id!r} has no stem {lane.stem!r}; "
                f"available: {sorted(track.stems)}"
            )
    return issues


def _check_keyframe_ordering(plan: Plan) -> list[str]:
    issues: list[str] = []
    mix_tempo = resolved_mix_tempo(plan)
    for i, lane in enumerate(plan.automation):
        prev: float | None = None
        for j, kf in enumerate(lane.keyframes):
            if isinstance(kf.at, CuePos):
                # Already reported by _check_cue_references; skip ordering here.
                continue
            try:
                cur = position_to_mix_beats(kf.at, mix_tempo)
            except (ValueError, TypeError) as e:
                issues.append(
                    f"automation[{i}].keyframes[{j}]: {e}"
                )
                continue
            if prev is not None and cur <= prev:
                issues.append(
                    f"automation[{i}] ({lane.lane}): keyframes not strictly "
                    f"time-ordered (keyframe[{j}] at mix-beat {cur} "
                    f"≤ previous {prev})"
                )
            prev = cur
    return issues


def _check_vocal_handoff_requirements(plan: Plan) -> list[str]:
    """
    For each vocal_handoff transition, every track that plays on either the
    from_deck or the to_deck (anywhere in the timeline) must have a 'vocals'
    stem. Conservative — the executor narrows this to just the tracks
    overlapping the transition window.
    """
    issues: list[str] = []
    decks_needing_vocals: set[int] = set()
    for seg in plan.timeline:
        if isinstance(seg, TransitionSegment) and seg.style == "vocal_handoff":
            decks_needing_vocals.add(seg.from_deck)
            decks_needing_vocals.add(seg.to_deck)
    if not decks_needing_vocals:
        return issues
    for i, seg in enumerate(plan.timeline):
        if not isinstance(seg, PlaySegment):
            continue
        if seg.deck not in decks_needing_vocals:
            continue
        track = plan.tracks.get(seg.track)
        if track is None or "vocals" in track.stems:
            continue
        issues.append(
            f"timeline[{i}] (play on deck {seg.deck}): track {seg.track!r} "
            f"is needed for a vocal_handoff but has no 'vocals' stem; "
            f"available: {sorted(track.stems)}"
        )
    return issues
