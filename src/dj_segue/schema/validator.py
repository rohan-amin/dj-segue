"""Cross-field validation that pydantic alone can't express.

Rules implemented here come from `docs/schema-v0.3.md`, "Validation rules".
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
    AfterPos,
    AutomationLane,
    BarPos,
    BeatPos,
    CrossfaderLane,
    CuePos,
    DeckVolumeLane,
    EqLane,
    FloatKeyframe,
    DbKeyframe,
    LoopSegment,
    PlaySegment,
    Plan,
    SecondPos,
    SilenceSegment,
    StemVolumeLane,
    TransitionSegment,
)
from dj_segue.constants import ANCHOR_TOLERANCE

# Module import (not `from ... import names`): time_math imports schema.plan,
# which initializes this package, which imports this module — so time_math
# may be only partially initialized here. Names are looked up at call time.
from dj_segue import time_math


class PlanValidationError(Exception):
    """One or more cross-field validation rules failed."""

    def __init__(self, issues: list[str]):
        self.issues = list(issues)
        joined = "\n  - ".join(self.issues)
        super().__init__(f"{len(self.issues)} validation issue(s):\n  - {joined}")


def validate_plan(plan: Plan, grids: dict[str, time_math.TrackGrid] | None = None) -> None:
    """Audio-free structural validation.

    `grids` (from preprocessing) is only needed to resolve the mix tempo when
    it defaults to an auto-detected track bpm. Without it, checks that need
    the tempo to compare positions are skipped; callers that render (the
    `play` CLI) re-run validation with grids after preprocessing.
    """
    mix_tempo = resolved_mix_tempo(plan, grids)
    issues: list[str] = []
    issues.extend(_check_track_references(plan))
    issues.extend(_check_deck_references(plan))
    issues.extend(_check_cue_references(plan))
    issues.extend(_check_segment_ids(plan))
    issues.extend(_check_loop_schedules(plan))
    issues.extend(_check_stem_references(plan))
    anchors = _try_anchors(plan, mix_tempo, grids)
    issues.extend(_check_keyframe_ordering(plan, mix_tempo, anchors))
    issues.extend(_check_vocal_handoff_requirements(plan))
    issues.extend(_check_transitions(plan, mix_tempo, anchors))
    issues.extend(_check_single_crossfader(plan))
    if issues:
        raise PlanValidationError(issues)


def _try_anchors(
    plan: Plan, mix_tempo: float | None, grids: dict[str, time_math.TrackGrid] | None
) -> dict[str, float] | None:
    """Segment-id → mix end seconds, or None when timing can't be resolved yet
    (auto tempo before preprocessing). Checks that need an unresolvable
    `after` position are skipped and re-run once grids are available."""
    if mix_tempo is None:
        return None
    from dj_segue.compiler import resolve_timeline  # compiler imports schema.plan

    try:
        return resolve_timeline(plan, mix_tempo, grids).anchors
    except (ValueError, TypeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Mix-time helpers
# ---------------------------------------------------------------------------


def resolved_mix_tempo(
    plan: Plan, grids: dict[str, time_math.TrackGrid] | None = None
) -> float | None:
    """The plan's effective mix tempo: `meta.mix_tempo`, else the first track's
    bpm (declared, or detected via `grids`). None when it defaults to an
    auto-detected bpm and no grids are given (i.e. before preprocessing)."""
    if plan.meta.mix_tempo is not None:
        return plan.meta.mix_tempo
    if not plan.tracks:
        return 120.0
    tid, track = next(iter(plan.tracks.items()))
    if track.bpm is not None:
        return track.bpm
    if grids and tid in grids:
        return grids[tid].bpm
    return None


def position_to_mix_beats(
    pos: Any, mix_tempo: float, anchors: dict[str, float] | None = None
) -> float:
    """Convert a mix-time position to mix-beats. Assumes 4/4 for bars.
    `after` positions need `anchors` (and the tempo)."""
    if isinstance(pos, AfterPos):
        return time_math.mix_pos_to_seconds(pos, mix_tempo, anchors) * mix_tempo / 60.0
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
        if isinstance(seg, (PlaySegment, LoopSegment)) and seg.track not in declared:
            issues.append(
                f"timeline[{i}] ({seg.type}): unknown track {seg.track!r}; "
                f"declared tracks: {sorted(declared)}"
            )
    return issues


def _check_deck_references(plan: Plan) -> list[str]:
    issues: list[str] = []
    declared = set(plan.decks)
    for i, seg in enumerate(plan.timeline):
        decks_used: list[int] = []
        if isinstance(seg, (PlaySegment, LoopSegment, SilenceSegment)):
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
        if isinstance(seg, (PlaySegment, LoopSegment)):
            track = plan.tracks.get(seg.track)
            fields = [("from", seg.from_)]
            if isinstance(seg, PlaySegment):
                fields.append(("to", seg.to))
            for field, pos in fields:
                if isinstance(pos, CuePos):
                    if track is None:
                        # Track-ref error already reported elsewhere; skip here.
                        continue
                    if pos.cue not in track.cues:
                        issues.append(
                            f"timeline[{i}] ({seg.type}): cue {pos.cue!r} "
                            f"in '{field}' not declared on track {seg.track!r}; "
                            f"available cues: {sorted(track.cues)}"
                        )
            if isinstance(seg.start_at, CuePos):
                issues.append(
                    f"timeline[{i}] ({seg.type}): cue references not allowed in "
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


def _check_segment_ids(plan: Plan) -> list[str]:
    """
    Segment ids are unique. An `after` position names a segment id; in the
    timeline it must be an *earlier* segment (timing resolves in one pass,
    so no cycles), in automation any segment.
    """
    issues: list[str] = []
    seen: dict[str, int] = {}
    for i, seg in enumerate(plan.timeline):
        start_at = getattr(seg, "start_at", None)
        if isinstance(start_at, AfterPos) and start_at.after not in seen:
            later = any(s.id == start_at.after for s in plan.timeline[i:])
            issues.append(
                f"timeline[{i}] ({seg.type}): start_at after {start_at.after!r}: "
                + (
                    "must name an earlier segment"
                    if later
                    else f"no segment has that id; ids: {sorted(seen)}"
                )
            )
        if seg.id is not None:
            if seg.id in seen:
                issues.append(
                    f"timeline[{i}] ({seg.type}): id {seg.id!r} already used by "
                    f"timeline[{seen[seg.id]}]"
                )
            else:
                seen[seg.id] = i
    for i, lane in enumerate(plan.automation):
        for j, kf in enumerate(lane.keyframes):
            if isinstance(kf.at, AfterPos) and kf.at.after not in seen:
                issues.append(
                    f"automation[{i}].keyframes[{j}]: after {kf.at.after!r}: "
                    f"no segment has that id; ids: {sorted(seen)}"
                )
    return issues


def _check_loop_schedules(plan: Plan) -> list[str]:
    issues: list[str] = []
    for i, seg in enumerate(plan.timeline):
        if not isinstance(seg, LoopSegment):
            continue
        for j, step in enumerate(seg.schedule):
            value = next(iter(step.length.model_dump().values()))
            if value <= 0:
                issues.append(
                    f"timeline[{i}] (loop): schedule[{j}] length must be positive"
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
        if isinstance(seg, (PlaySegment, LoopSegment)):
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


def _check_keyframe_ordering(
    plan: Plan, mix_tempo: float | None, anchors: dict[str, float] | None = None
) -> list[str]:
    issues: list[str] = []
    for i, lane in enumerate(plan.automation):
        if mix_tempo is None and any(
            isinstance(kf.at, (SecondPos, AfterPos)) for kf in lane.keyframes
        ):
            continue  # ordering mixed units needs the tempo; re-checked after preprocess
        if anchors is None and any(isinstance(kf.at, AfterPos) for kf in lane.keyframes):
            continue  # `after` needs the resolved timeline; re-checked after preprocess
        prev: float | None = None
        for j, kf in enumerate(lane.keyframes):
            if isinstance(kf.at, CuePos):
                # Already reported by _check_cue_references; skip ordering here.
                continue
            if isinstance(kf.at, AfterPos) and kf.at.after not in anchors:  # type: ignore[operator]
                continue  # unknown id: reported by _check_segment_ids
            try:
                cur = position_to_mix_beats(kf.at, mix_tempo, anchors)
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


def validate_against_audio(
    plan: Plan,
    durations: dict[str, float],
    grids: dict[str, time_math.TrackGrid] | None = None,
) -> None:
    """
    Run audio-aware validation that requires preprocessing output:
      - rule 5: no two segments on the same deck overlap
      - rule 6: track positions in play segments fall within track duration
    `durations` maps track_id → audio duration in seconds (typically taken
    from the preprocessor's TrackAnalysis.primary.duration_sec).
    `grids` maps track_id → resolved beat grid (PreprocessResult.grids()). It's
    required for tracks whose bpm is auto-detected; with it, rule 6 honors the
    anchor, and rule 5 uses each track's real tempo for time-stretched spans.
    """
    issues: list[str] = []
    issues.extend(_check_track_position_bounds(plan, durations, grids))
    issues.extend(_check_no_deck_overlap(plan, grids))
    if issues:
        raise PlanValidationError(issues)


def _check_track_position_bounds(
    plan: Plan,
    durations: dict[str, float],
    grids: dict[str, time_math.TrackGrid] | None = None,
) -> list[str]:
    grids = grids or {}
    issues: list[str] = []
    for i, seg in enumerate(plan.timeline):
        if not isinstance(seg, (PlaySegment, LoopSegment)):
            continue
        track = plan.tracks.get(seg.track)
        if track is None or seg.track not in durations:
            continue
        dur_sec = durations[seg.track]
        grid = grids.get(seg.track)
        try:
            from_sec = time_math.track_pos_to_seconds(seg.from_, track, grid)
            if isinstance(seg, LoopSegment):
                issues.extend(_check_loop_bounds(i, seg, track, grid, from_sec, dur_sec))
                continue
            to_sec = time_math.track_pos_to_seconds(seg.to, track, grid)
        except (ValueError, TypeError) as e:
            issues.append(f"timeline[{i}] ({seg.type}): {e}")
            continue
        # A grid anchor may sit up to ANCHOR_TOLERANCE before sample 0, so beat 0
        # can resolve slightly negative; the engine pads that with silence.
        if from_sec < -ANCHOR_TOLERANCE or from_sec > dur_sec:
            issues.append(
                f"timeline[{i}] (play): track {seg.track!r} 'from' resolves to "
                f"{from_sec:.3f}s but track is only {dur_sec:.3f}s long"
            )
        if to_sec < -ANCHOR_TOLERANCE or to_sec > dur_sec:
            issues.append(
                f"timeline[{i}] (play): track {seg.track!r} 'to' resolves to "
                f"{to_sec:.3f}s but track is only {dur_sec:.3f}s long"
            )
        if to_sec <= from_sec:
            issues.append(
                f"timeline[{i}] (play): 'to' ({to_sec:.3f}s) must be after "
                f"'from' ({from_sec:.3f}s)"
            )
    return issues


def _check_loop_bounds(i, seg, track, grid, from_sec: float, dur_sec: float) -> list[str]:
    from dj_segue.compiler import _track_duration_sec

    longest = max(_track_duration_sec(st.length, track, grid) for st in seg.schedule)
    end_sec = from_sec + longest
    if from_sec < -ANCHOR_TOLERANCE or end_sec > dur_sec:
        return [
            f"timeline[{i}] (loop): track {seg.track!r} loop covers "
            f"{from_sec:.3f}–{end_sec:.3f}s but track is only {dur_sec:.3f}s long"
        ]
    return []


def _check_no_deck_overlap(
    plan: Plan, grids: dict[str, time_math.TrackGrid] | None = None
) -> list[str]:
    """
    Resolve play/loop/silence segments to mix-time spans (compiler.timeline_spans),
    group by deck, and flag any overlap. Transitions are not occupancies:
    they're gain changes laid over decks that are already playing (a crossfade
    needs both decks' play segments to span the window). Transition-vs-
    transition overlap is checked separately in `_check_transitions`.
    """
    from dj_segue.compiler import timeline_spans  # compiler imports schema.plan

    mix_tempo = resolved_mix_tempo(plan, grids)
    if mix_tempo is None:
        return ["mix tempo is unknown: first track's bpm is auto-detected but no grids were given"]
    try:
        spans = timeline_spans(plan, mix_tempo, grids)
    except (ValueError, TypeError, KeyError) as e:
        return [f"timeline: {e}"]

    issues: list[str] = []
    by_deck: dict[int, list] = {}
    for sp in spans:
        by_deck.setdefault(sp.deck, []).append(sp)
    for deck, items in by_deck.items():
        items.sort(key=lambda sp: sp.mix_start_sec)
        for a, b in zip(items, items[1:]):
            if b.mix_start_sec < a.mix_end_sec - 1e-6:  # tolerance for float compare
                issues.append(
                    f"deck {deck}: timeline[{a.index}] ({a.kind}) (mix-time "
                    f"{a.mix_start_sec:.3f}–{a.mix_end_sec:.3f}s) overlaps "
                    f"timeline[{b.index}] ({b.kind}) (mix-time "
                    f"{b.mix_start_sec:.3f}–{b.mix_end_sec:.3f}s)"
                )
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
        if not isinstance(seg, (PlaySegment, LoopSegment)):
            continue
        if seg.deck not in decks_needing_vocals:
            continue
        track = plan.tracks.get(seg.track)
        if track is None or "vocals" in track.stems:
            continue
        issues.append(
            f"timeline[{i}] ({seg.type} on deck {seg.deck}): track {seg.track!r} "
            f"is needed for a vocal_handoff but has no 'vocals' stem; "
            f"available: {sorted(track.stems)}"
        )
    return issues


def _check_transitions(
    plan: Plan, mix_tempo: float | None, anchors: dict[str, float] | None = None
) -> list[str]:
    """
    Transition sanity (audio-free; all positions are mix-time):
      - from_deck and to_deck differ;
      - `cut` has zero duration (it's instantaneous — write {"beats": 0});
        `crossfade` has positive duration, on each side (v0.4 `out` / `in`);
      - transitions touching the same deck don't overlap in time;
      - per deck, transitions alternate out/in: a deck can't be faded out
        twice without being faded back in (or vice versa).
    """
    issues: list[str] = []
    # deck → [(start, end, direction, label)]; direction -1 = out, +1 = in
    per_deck: dict[int, list[tuple[float, float, int, str]]] = {}
    for i, seg in enumerate(plan.timeline):
        if not isinstance(seg, TransitionSegment):
            continue
        label = f"timeline[{i}] ({seg.style})"
        if seg.from_deck == seg.to_deck:
            issues.append(f"{label}: from_deck and to_deck are both {seg.from_deck}")
            continue
        if mix_tempo is None:
            continue  # timing checks need the tempo; re-checked after preprocess
        try:
            duration = time_math.duration_to_seconds(seg.duration, mix_tempo)
            out_w, in_w = time_math.transition_windows(seg, mix_tempo, anchors)
        except (ValueError, TypeError):
            # cue-in-mix-time is reported elsewhere; an `after` that can't be
            # resolved yet is re-checked once grids are available.
            continue
        if seg.style == "cut" and duration != 0:
            issues.append(
                f'{label}: cut is instantaneous; duration must be {{"beats": 0}}'
            )
        if seg.style != "cut":
            for name, w in (("out", out_w), ("in", in_w)):
                if w.end_sec - w.start_sec <= 0:
                    issues.append(f"{label}: {name} fade duration must be positive")
        per_deck.setdefault(seg.from_deck, []).append((out_w.start_sec, out_w.end_sec, -1, label))
        per_deck.setdefault(seg.to_deck, []).append((in_w.start_sec, in_w.end_sec, +1, label))

    for deck, items in sorted(per_deck.items()):
        items.sort()
        for (s1, e1, d1, l1), (s2, e2, d2, l2) in zip(items, items[1:]):
            if s2 < e1 - 1e-6:
                issues.append(
                    f"deck {deck}: {l1} (mix-time {s1:.3f}–{e1:.3f}s) overlaps "
                    f"{l2} (mix-time {s2:.3f}–{e2:.3f}s)"
                )
            elif d1 == d2:
                verb = "out" if d1 < 0 else "in"
                issues.append(
                    f"deck {deck}: faded {verb} by {l1} and again by {l2} "
                    f"with no opposite transition in between"
                )
    return issues


def _check_single_crossfader(plan: Plan) -> list[str]:
    idxs = [i for i, l in enumerate(plan.automation) if isinstance(l, CrossfaderLane)]
    if len(idxs) > 1:
        return [
            f"automation{idxs}: only one crossfader lane is allowed "
            f"(there is one physical crossfader)"
        ]
    return []
