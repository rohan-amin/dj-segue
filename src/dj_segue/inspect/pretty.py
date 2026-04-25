"""Human-readable plan summary for `dj-segue inspect`."""

from __future__ import annotations

from io import StringIO

from dj_segue.schema.plan import (
    BarPos,
    BeatPos,
    CrossfaderLane,
    CuePos,
    DbKeyframe,
    DeckVolumeLane,
    EqLane,
    PlaySegment,
    Plan,
    SecondPos,
    SilenceSegment,
    StemVolumeLane,
    TransitionSegment,
)
from dj_segue.schema.validator import (
    PlanValidationError,
    position_to_mix_beats,
    resolved_mix_tempo,
    validate_plan,
)


def format_plan(plan: Plan, *, run_validation: bool = True) -> str:
    out = StringIO()
    mix_tempo = resolved_mix_tempo(plan)

    _write_header(out, plan, mix_tempo)
    _write_tracks(out, plan)
    _write_decks(out, plan)
    _write_timeline(out, plan, mix_tempo)
    _write_automation(out, plan, mix_tempo)
    if run_validation:
        _write_validation(out, plan)
    return out.getvalue()


# ---------------------------------------------------------------------------


def _write_header(out: StringIO, plan: Plan, mix_tempo: float) -> None:
    out.write(f"== {plan.meta.mix_name} ==\n")
    out.write(f"schema_version : {plan.schema_version}\n")
    out.write(f"mix_tempo      : {mix_tempo} bpm")
    if plan.meta.mix_tempo is None:
        out.write("  (default: first track's bpm)")
    out.write("\n")
    if plan.meta.author:
        out.write(f"author         : {plan.meta.author}\n")
    if plan.meta.source_prompt:
        out.write(f"source_prompt  : {plan.meta.source_prompt}\n")
    if plan.meta.target_executor:
        out.write(f"target_executor: {plan.meta.target_executor}\n")
    out.write("\n")


def _write_tracks(out: StringIO, plan: Plan) -> None:
    out.write(f"-- tracks ({len(plan.tracks)}) --\n")
    for tid, track in plan.tracks.items():
        key_part = f", key={track.key}" if track.key else ""
        stem_names = sorted(track.stems)
        if stem_names == ["full"]:
            stems_part = f"path={track.stems['full']}"
        else:
            stems_part = f"stems=[{', '.join(stem_names)}]"
        out.write(f"  {tid:<16} bpm={track.bpm}{key_part}  {stems_part}\n")
        if track.cues:
            for cue_name, cue in track.cues.items():
                where = _fmt_cue_position(cue)
                label = f"  ({cue.label})" if cue.label else ""
                out.write(f"    cue {cue_name:<14} @ {where}{label}\n")
    out.write("\n")


def _write_decks(out: StringIO, plan: Plan) -> None:
    out.write(f"-- decks ({len(plan.decks)}) --\n")
    for did in sorted(plan.decks):
        deck = plan.decks[did]
        label = f"  ({deck.label})" if deck.label else ""
        out.write(f"  deck {did}{label}\n")
    out.write("\n")


def _write_timeline(out: StringIO, plan: Plan, mix_tempo: float) -> None:
    out.write(f"-- timeline ({len(plan.timeline)} segments) --\n")
    for i, seg in enumerate(plan.timeline):
        if isinstance(seg, PlaySegment):
            start = _fmt_mix_position(seg.start_at, mix_tempo, default="(after prev on deck)")
            from_ = _fmt_track_position(seg.from_)
            to = _fmt_track_position(seg.to)
            out.write(
                f"  [{i}] play     deck {seg.deck}  track={seg.track}  "
                f"track[{from_} → {to}]  start_at={start}\n"
            )
        elif isinstance(seg, SilenceSegment):
            dur = _fmt_duration(seg.duration)
            out.write(f"  [{i}] silence  deck {seg.deck}  duration={dur}\n")
        elif isinstance(seg, TransitionSegment):
            start = _fmt_mix_position(seg.start_at, mix_tempo)
            dur = _fmt_duration(seg.duration)
            out.write(
                f"  [{i}] transit  {seg.style:<14} "
                f"deck {seg.from_deck} → deck {seg.to_deck}  "
                f"start_at={start}  duration={dur}\n"
            )
    out.write("\n")


def _write_automation(out: StringIO, plan: Plan, mix_tempo: float) -> None:
    out.write(f"-- automation ({len(plan.automation)} lanes) --\n")
    if not plan.automation:
        out.write("  (none)\n\n")
        return
    for i, lane in enumerate(plan.automation):
        if isinstance(lane, DeckVolumeLane):
            head = f"deck_volume   deck {lane.deck}"
        elif isinstance(lane, StemVolumeLane):
            head = f"stem_volume   deck {lane.deck} stem={lane.stem}"
        elif isinstance(lane, EqLane):
            head = f"eq            deck {lane.deck} band={lane.band}"
        elif isinstance(lane, CrossfaderLane):
            head = "crossfader"
        else:  # pragma: no cover
            head = lane.lane
        out.write(
            f"  [{i}] {head}  ({lane.interpolation}, "
            f"{len(lane.keyframes)} kfs)\n"
        )
        for j, kf in enumerate(lane.keyframes):
            at = _fmt_mix_position(kf.at, mix_tempo)
            val = (
                f"{kf.value_db:+g} dB"
                if isinstance(kf, DbKeyframe)
                else f"{kf.value:g}"
            )
            out.write(f"        kf[{j}] @ {at}  →  {val}\n")
    out.write("\n")


def _write_validation(out: StringIO, plan: Plan) -> None:
    out.write("-- validation --\n")
    try:
        validate_plan(plan)
        out.write("  ok\n")
    except PlanValidationError as e:
        out.write(f"  {len(e.issues)} issue(s):\n")
        for issue in e.issues:
            out.write(f"    - {issue}\n")


# ---------------------------------------------------------------------------
# position formatters
# ---------------------------------------------------------------------------


def _fmt_track_position(pos) -> str:
    if isinstance(pos, BeatPos):
        return f"beat {pos.beat:g}"
    if isinstance(pos, BarPos):
        return f"bar {pos.bar:g}"
    if isinstance(pos, SecondPos):
        return f"{pos.second:g}s"
    if isinstance(pos, CuePos):
        return f"cue:{pos.cue}"
    return repr(pos)


def _fmt_mix_position(pos, mix_tempo: float, default: str = "?") -> str:
    if pos is None:
        return default
    raw = _fmt_track_position(pos)
    if isinstance(pos, CuePos):
        return f"{raw} (invalid in mix-time)"
    try:
        beats = position_to_mix_beats(pos, mix_tempo)
    except (ValueError, TypeError):
        return raw
    if isinstance(pos, BeatPos):
        return f"mix-beat {beats:g}"
    return f"{raw}  (= mix-beat {beats:g})"


def _fmt_cue_position(cue) -> str:
    if cue.beat is not None:
        return f"beat {cue.beat:g}"
    if cue.bar is not None:
        return f"bar {cue.bar:g}"
    return f"{cue.second:g}s"


def _fmt_duration(dur) -> str:
    if hasattr(dur, "beats"):
        return f"{dur.beats:g} beats"
    if hasattr(dur, "bars"):
        return f"{dur.bars:g} bars"
    return f"{dur.seconds:g} s"
