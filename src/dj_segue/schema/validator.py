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
    FilterLane,
    FloatKeyframe,
    DbKeyframe,
    LoopSegment,
    PlaySegment,
    Plan,
    SecondPos,
    SilenceSegment,
    StemVolumeLane,
    TransitionSegment,
    available_stems,
    WordPos,
    word_positions,
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
    issues: list[str] = []
    try:
        mix_tempo = resolved_tempo(plan, grids)
    except (ValueError, TypeError, KeyError) as e:
        issues.append(f"meta.tempo_map: {e}")
        mix_tempo = None
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
    issues.extend(_check_eq_lanes(plan))
    issues.extend(_check_filter_lanes(plan))
    issues.extend(_check_word_positions(plan, mix_tempo, grids))
    if issues:
        raise PlanValidationError(issues)


def _try_anchors(
    plan: Plan, mix_tempo: "time_math.TempoMap | None", grids: dict[str, time_math.TrackGrid] | None
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
    """The plan's effective mix tempo: `meta.mix_tempo`, else the bpm of the
    first track on the timeline (declared, or detected via `grids`). None when
    it defaults to an auto-detected bpm and no grids are given (i.e. before
    preprocessing)."""
    if plan.meta.mix_tempo is not None:
        return plan.meta.mix_tempo
    tid = tempo_track(plan)
    if tid is None:
        return 120.0
    track = plan.tracks[tid]
    if track.bpm is not None:
        return track.bpm
    if grids and tid in grids:
        return grids[tid].bpm
    return None


def resolved_tempo(
    plan: Plan, grids: dict[str, time_math.TrackGrid] | None = None
) -> "time_math.TempoMap | None":
    """The plan's mix tempo map (v0.9): `resolved_mix_tempo` to start, then
    each `meta.tempo_map` change. None when it isn't known yet: the
    starting tempo is auto-detected (as `resolved_mix_tempo`), or a change
    at an `after` position needs track grids that weren't given.

    Changes resolve in order, each with the changes before it in place, so
    a change's beat position counts through the earlier ones. An `after`
    position needs the timeline, which itself depends on the tempo: it's
    resolved with the map so far, and the final map must put it in the same
    place (otherwise the change would move itself, and it's an error).
    Raises ValueError for changes out of order or overlapping a ramp.
    """
    start = resolved_mix_tempo(plan, grids)
    if start is None:
        return None
    tm = time_math.TempoMap.fixed(start)
    changes = plan.meta.tempo_map
    if not changes:
        return tm
    from dj_segue.compiler import resolve_timeline  # compiler imports schema.plan

    def at_sec(change, tempo) -> float:
        if isinstance(change.at, CuePos):
            raise ValueError(f"cue {change.at.cue!r} is a track position; use a mix position")
        if isinstance(change.at, WordPos):
            raise ValueError("a tempo change can't be at a word (where words land depends on "
                             "the tempo); use a beat or `after` position")
        anchors = None
        if isinstance(change.at, AfterPos):
            try:
                anchors = resolve_timeline(plan, tempo, grids).anchors
            except (ValueError, TypeError, KeyError):
                if not grids:
                    raise _NeedsGrids from None
                raise
        return time_math.mix_pos_to_seconds(change.at, tempo, anchors)

    starts = []
    for k, change in enumerate(changes):
        try:
            at = at_sec(change, tm)
        except _NeedsGrids:
            return None
        starts.append(at)
        ramp = 0.0
        if change.ramp is not None:
            ramp = time_math.ramp_seconds(change.ramp, tm.bpm_at(at), change.bpm)
            if ramp <= 0:
                raise ValueError(f"[{k}]: ramp must be positive (omit it for a step)")
        try:
            tm = tm.with_change(at, change.bpm, ramp)
        except ValueError as e:
            raise ValueError(f"[{k}]: {e}") from None
    for k, change in enumerate(changes):
        if isinstance(change.at, AfterPos):
            if abs(at_sec(change, tm) - starts[k]) > 1e-6:
                raise ValueError(
                    f"[{k}]: its `after` position moves when the tempo changes "
                    f"(segment {change.at.after!r} ends after this change, so "
                    f"it depends on it); use a beat position"
                )
    return tm


class _NeedsGrids(Exception):
    """The timeline can't be resolved before preprocessing."""


def tempo_track(plan: Plan) -> str | None:
    """The track that sets the default mix tempo: the first one a play or
    loop segment uses, in timeline order (else the first declared track)."""
    for seg in plan.timeline:
        if isinstance(seg, (PlaySegment, LoopSegment)) and seg.track in plan.tracks:
            return seg.track
    return next(iter(plan.tracks), None)


def position_to_mix_beats(
    pos: Any, mix_tempo: "time_math.TempoMap | float", anchors: dict[str, float] | None = None
) -> float:
    """Convert a mix-time position to mix-beats. Assumes 4/4 for bars.
    `after` positions need `anchors` (and the tempo)."""
    if isinstance(pos, (AfterPos, WordPos)):
        sec = time_math.mix_pos_to_seconds(pos, mix_tempo, anchors)
        return time_math.as_tempo(mix_tempo).sec_to_beat(sec)
    if isinstance(pos, BeatPos):
        return pos.beat
    if isinstance(pos, BarPos):
        return pos.bar * 4.0
    if isinstance(pos, SecondPos):
        return time_math.as_tempo(mix_tempo).sec_to_beat(pos.second)
    if isinstance(pos, CuePos):
        raise ValueError(f"Cue references are not valid in mix-time context: {pos!r}")
    raise TypeError(f"Unknown position type: {type(pos).__name__}")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_word_positions(
    plan: Plan,
    mix_tempo: "time_math.TempoMap | None",
    grids: dict[str, time_math.TrackGrid] | None,
) -> list[str]:
    """v0.10 word positions: `in` only (and always) in mix positions, naming a
    play segment; with grids (after preprocessing), each word resolves."""
    issues: list[str] = []
    found = list(word_positions(plan))
    if not found:
        return issues
    kinds = {seg.id: seg.type for seg in plan.timeline if getattr(seg, "id", None)}
    for where, pos, ctx in found:
        if where.startswith("meta.tempo_map"):
            continue  # reported with the tempo map
        if ctx == "track" and pos.in_ is not None:
            issues.append(f'{where}: "in" is for mix positions; a track word is in its own track')
        elif ctx == "mix" and pos.in_ is None:
            issues.append(f'{where}: a mix word position needs "in": the play segment that sings {pos.word!r}')
        elif ctx == "mix" and kinds.get(pos.in_) != "play":
            what = "no segment" if pos.in_ not in kinds else f"a {kinds[pos.in_]} segment"
            issues.append(f'{where}: "in": {pos.in_!r} names {what}; it must name a play segment')
    if issues or grids is None or mix_tempo is None:
        return issues
    from dj_segue.compiler import resolve_timeline  # compiler imports schema.plan

    for i, seg in enumerate(plan.timeline):  # track words (from / to)
        for name in ("from_", "to"):
            pos = getattr(seg, name, None)
            if isinstance(pos, WordPos):
                try:
                    time_math.word_sec(pos, grids.get(seg.track))
                except ValueError as e:
                    issues.append(f"timeline[{i}].{name.rstrip('_')}: {e}")
    if issues:
        return issues
    try:
        anchors = resolve_timeline(plan, mix_tempo, grids).anchors
    except (ValueError, TypeError, KeyError) as e:
        if any(w.endswith("start_at") for w, _, c in found if c == "mix"):
            issues.append(f"timeline: {e}")
        return issues
    for where, pos, ctx in found:
        if ctx == "mix" and not where.startswith("meta.tempo_map"):
            try:
                time_math.mix_pos_to_seconds(pos, mix_tempo, anchors)
            except ValueError as e:
                issues.append(f"{where}: {e}")
    return issues


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
    Every stem a lane names (stem_volume, or eq with `stem`) on deck D must
    be available on every track that plays on D: the track's own `stems`, or
    for a `path` track the four the separator makes (v0.8). And every track
    on a deck that's mixed by stems offers the same stems.
    """
    issues: list[str] = []
    tracks_per_deck = _tracks_per_deck(plan)
    for i, lane in enumerate(plan.automation):
        stem = lane.stem if isinstance(lane, (StemVolumeLane, EqLane)) else None
        if stem is None:
            continue
        for track_id in sorted(tracks_per_deck.get(lane.deck, set())):
            track = plan.tracks.get(track_id)
            if track is None or stem in available_stems(track):
                continue
            issues.append(
                f"automation[{i}] ({lane.lane} on deck {lane.deck}): "
                f"track {track_id!r} has no stem {stem!r}; "
                f"available: {sorted(available_stems(track))}"
            )
    for deck in sorted(stem_decks(plan)):
        sets = {
            tid: available_stems(plan.tracks[tid])
            for tid in tracks_per_deck.get(deck, set()) if tid in plan.tracks
        }
        if len({tuple(sorted(v)) for v in sets.values()}) > 1:
            issues.append(
                f"deck {deck} is mixed by stems, but its tracks have different stems: "
                + ", ".join(f"{t}: {sorted(v)}" for t, v in sorted(sets.items()))
            )
    return issues


def _tracks_per_deck(plan: Plan) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for seg in plan.timeline:
        if isinstance(seg, (PlaySegment, LoopSegment)):
            out.setdefault(seg.deck, set()).add(seg.track)
    return out


def stem_decks(plan: Plan) -> set[int]:
    """Decks mixed by stems: a stem lane, or either side of a vocal_handoff."""
    decks = {
        lane.deck for lane in plan.automation
        if isinstance(lane, StemVolumeLane) or (isinstance(lane, EqLane) and lane.stem)
    }
    for seg in plan.timeline:
        if isinstance(seg, TransitionSegment) and seg.style == "vocal_handoff":
            decks |= {seg.from_deck, seg.to_deck}
    return decks


def _check_keyframe_ordering(
    plan: Plan, mix_tempo: "time_math.TempoMap | None", anchors: dict[str, float] | None = None
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

    try:
        mix_tempo = resolved_tempo(plan, grids)
    except (ValueError, TypeError, KeyError) as e:
        return [f"meta.tempo_map: {e}"]
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
    stem — any `path` track does (v0.8: separated automatically).
    """
    issues: list[str] = []
    decks_needing_vocals: set[int] = set()
    for seg in plan.timeline:
        if isinstance(seg, TransitionSegment) and seg.style == "vocal_handoff":
            decks_needing_vocals.add(seg.from_deck)
            decks_needing_vocals.add(seg.to_deck)
    for i, seg in enumerate(plan.timeline):
        if not isinstance(seg, (PlaySegment, LoopSegment)):
            continue
        if seg.deck not in decks_needing_vocals:
            continue
        track = plan.tracks.get(seg.track)
        if track is None or "vocals" in available_stems(track):
            continue
        issues.append(
            f"timeline[{i}] ({seg.type} on deck {seg.deck}): track {seg.track!r} "
            f"is needed for a vocal_handoff but has no 'vocals' stem; "
            f"available: {sorted(track.stems)}"
        )
    return issues


# Closest two points of a drawn curve may be, in mix time (v0.5).
MIN_POINT_GAP_SEC = 0.002


def _check_transitions(
    plan: Plan, mix_tempo: "time_math.TempoMap | None", anchors: dict[str, float] | None = None
) -> list[str]:
    """
    Transition sanity (audio-free; all positions are mix-time):
      - from_deck and to_deck differ;
      - `cut` has zero duration (it's instantaneous — write {"beats": 0});
        `crossfade` has positive duration, on each side (v0.4 `out` / `in`);
      - `echo_out` (v0.6) has a positive tail, and `echo.every` fits in it;
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
            start = time_math.mix_pos_to_seconds(seg.start_at, mix_tempo, anchors)
            duration = time_math.mix_duration(start, seg.duration, mix_tempo)
            out_w, in_w = time_math.transition_windows(seg, mix_tempo, anchors)
        except (ValueError, TypeError):
            # cue-in-mix-time is reported elsewhere; an `after` that can't be
            # resolved yet is re-checked once grids are available.
            continue
        if seg.style == "cut" and duration != 0:
            issues.append(
                f'{label}: cut is instantaneous; duration must be {{"beats": 0}}'
            )
        if seg.style == "vocal_handoff":
            try:
                swap = time_math.handoff_swap(seg, mix_tempo, anchors)
            except (ValueError, TypeError):
                swap = None  # a word swap before preprocessing: re-checked with grids
            if swap is not None and not start - 1e-9 <= swap <= start + duration + 1e-9:
                issues.append(f"{label}: swap_at must be within the transition's duration")
        if seg.style == "echo_out":
            every = time_math.echo_window(seg, mix_tempo, anchors).every_sec
            if duration <= 0:
                issues.append(f"{label}: duration (the echo tail) must be positive")
            if every <= 0:
                issues.append(f"{label}: echo.every must be positive")
            elif every > duration + 1e-9:
                issues.append(
                    f"{label}: echo.every is longer than the tail (duration); "
                    f"no repeat would finish"
                )
        if seg.style != "cut":
            for name, w in (("out", out_w), ("in", in_w)):
                if w.curve == "step":
                    continue  # echo_out's dry cut, or an `in` with no fade
                if w.end_sec - w.start_sec <= 0:
                    issues.append(f"{label}: {name} fade duration must be positive")
                elif not isinstance(w.curve, str):
                    # A drawn curve: a jump faster than MIN_POINT_GAP_SEC clicks.
                    span = w.end_sec - w.start_sec
                    ts = [t for t, _ in w.curve.points]
                    gap = min(b - a for a, b in zip(ts, ts[1:])) * span
                    if gap < MIN_POINT_GAP_SEC - 1e-9:
                        issues.append(
                            f"{label}: drawn {name} curve has points {gap * 1000:.2f} ms "
                            f"apart; keep them at least {MIN_POINT_GAP_SEC * 1000:g} ms "
                            f"apart so the volume can't jump (a click)"
                        )
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


# The `eq` lane's range (schema "Lane types").
EQ_MIN_DB, EQ_MAX_DB = -24.0, 12.0


def _check_eq_lanes(plan: Plan) -> list[str]:
    """EQ values within EQ_MIN_DB..EQ_MAX_DB; one lane per deck, band and
    stem (v0.8; no stem = the whole deck)."""
    issues: list[str] = []
    seen: dict[tuple[int, str, str | None], int] = {}
    for i, lane in enumerate(plan.automation):
        if not isinstance(lane, EqLane):
            continue
        key = (lane.deck, lane.band, lane.stem)
        if key in seen:
            what = f" stem {lane.stem!r}" if lane.stem else ""
            issues.append(
                f"automation[{i}]: a second eq lane for deck {lane.deck} band "
                f"{lane.band!r}{what} (first is automation[{seen[key]}])"
            )
        seen.setdefault(key, i)
        for j, kf in enumerate(lane.keyframes):
            if not EQ_MIN_DB <= kf.value_db <= EQ_MAX_DB:
                issues.append(
                    f"automation[{i}].keyframes[{j}]: eq value_db {kf.value_db:g} is "
                    f"outside {EQ_MIN_DB:g} to {EQ_MAX_DB:+g} dB"
                )
    return issues


def _check_filter_lanes(plan: Plan) -> list[str]:
    """Filter knob values within -1..+1; one filter lane per deck (v0.7)."""
    issues: list[str] = []
    seen: dict[int, int] = {}
    for i, lane in enumerate(plan.automation):
        if not isinstance(lane, FilterLane):
            continue
        if lane.deck in seen:
            issues.append(
                f"automation[{i}]: a second filter lane for deck {lane.deck} "
                f"(first is automation[{seen[lane.deck]}])"
            )
        seen.setdefault(lane.deck, i)
        for j, kf in enumerate(lane.keyframes):
            if not -1.0 <= kf.value <= 1.0:
                issues.append(
                    f"automation[{i}].keyframes[{j}]: filter value {kf.value:g} is "
                    f"outside -1 to +1"
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
