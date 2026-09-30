"""Human-readable plan summary for `dj-segue inspect`."""

from __future__ import annotations

from io import StringIO

from dj_segue.compiler import resolve_timeline, transition_envelopes
from dj_segue.schema.plan import (
    AfterPos,
    BarPos,
    BeatPos,
    CrossfaderLane,
    CuePos,
    DbKeyframe,
    DeckVolumeLane,
    EqLane,
    LoopSegment,
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
    anchors = _anchors(plan, mix_tempo)

    _write_header(out, plan, mix_tempo)
    _write_tracks(out, plan)
    _write_decks(out, plan)
    _write_timeline(out, plan, mix_tempo, anchors)
    _write_transition_expansion(out, plan, mix_tempo, anchors)
    _write_automation(out, plan, mix_tempo, anchors)
    if run_validation:
        _write_validation(out, plan)
    return out.getvalue()


def _anchors(plan: Plan, mix_tempo: float | None) -> dict[str, float] | None:
    """Segment end times for `after` positions; None if timing needs preprocessing."""
    if mix_tempo is None:
        return None
    try:
        return resolve_timeline(plan, mix_tempo).anchors
    except (ValueError, TypeError, KeyError):
        return None


# ---------------------------------------------------------------------------


def _write_header(out: StringIO, plan: Plan, mix_tempo: float | None) -> None:
    out.write(f"== {plan.meta.mix_name} ==\n")
    out.write(f"schema_version : {plan.schema_version}\n")
    if mix_tempo is None:
        out.write("mix_tempo      : auto  (first track's detected bpm; known after preprocess)")
    else:
        out.write(f"mix_tempo      : {mix_tempo:g} bpm")
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
        bpm = "auto" if track.bpm is None else f"{track.bpm:g}"
        out.write(f"  {tid:<16} bpm={bpm}{key_part}  {stems_part}\n")
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


def _write_timeline(
    out: StringIO, plan: Plan, mix_tempo: float | None, anchors: dict[str, float] | None
) -> None:
    out.write(f"-- timeline ({len(plan.timeline)} segments) --\n")
    for i, seg in enumerate(plan.timeline):
        sid = f"  id={seg.id}" if seg.id is not None else ""
        if isinstance(seg, PlaySegment):
            start = _fmt_mix_position(
                seg.start_at, mix_tempo, anchors, default="(after prev on deck)"
            )
            from_ = _fmt_track_position(seg.from_)
            to = _fmt_track_position(seg.to)
            out.write(
                f"  [{i}] play     deck {seg.deck}  track={seg.track}  "
                f"track[{from_} → {to}]  start_at={start}{_fmt_tempo(seg)}{sid}\n"
            )
        elif isinstance(seg, LoopSegment):
            start = _fmt_mix_position(
                seg.start_at, mix_tempo, anchors, default="(after prev on deck)"
            )
            steps = ", ".join(
                f"{_fmt_duration(st.length)} ×{st.repetitions}" for st in seg.schedule
            )
            out.write(
                f"  [{i}] loop     deck {seg.deck}  track={seg.track}  "
                f"from {_fmt_track_position(seg.from_)}  [{steps}]  "
                f"start_at={start}{_fmt_tempo(seg)}{sid}\n"
            )
        elif isinstance(seg, SilenceSegment):
            dur = _fmt_duration(seg.duration)
            out.write(f"  [{i}] silence  deck {seg.deck}  duration={dur}{sid}\n")
        elif isinstance(seg, TransitionSegment):
            start = _fmt_mix_position(seg.start_at, mix_tempo, anchors)
            dur = _fmt_duration(seg.duration)
            out.write(
                f"  [{i}] transit  {seg.style:<14} "
                f"deck {seg.from_deck} → deck {seg.to_deck}  "
                f"start_at={start}  duration={dur}{sid}\n"
            )
    out.write("\n")


def _write_transition_expansion(
    out: StringIO, plan: Plan, mix_tempo: float | None, anchors: dict[str, float] | None
) -> None:
    """Show the per-deck gain ramps that crossfade/cut transitions compile to."""
    if not any(isinstance(s, TransitionSegment) for s in plan.timeline):
        return
    if mix_tempo is None:
        out.write("-- transition expansion --\n  (after preprocess: mix tempo is auto)\n\n")
        return
    uses_after = any(
        isinstance(getattr(seg, "start_at", None), AfterPos) for seg in plan.timeline
    )
    if anchors is None and uses_after:
        out.write(
            "-- transition expansion --\n  (after preprocess: `after` positions "
            "depend on auto-detected track tempos)\n\n"
        )
        return
    try:
        envs = transition_envelopes(plan, mix_tempo, anchors)
    except (ValueError, TypeError) as e:  # e.g. cue ref in start_at
        out.write(f"-- transition expansion --\n  (cannot expand: {e})\n\n")
        return
    out.write("-- transition expansion (per-deck gain) --\n")
    if not envs:
        out.write("  (none compiled; vocal_handoff is stem-level, M3)\n\n")
        return
    beats_per_sec = mix_tempo / 60.0
    for deck in sorted(envs):
        env = envs[deck]
        out.write(f"  deck {deck}: starts at gain {env.initial:g}  ({env.source})\n")
        for r in env.ramps:
            b1 = r.start_sec * beats_per_sec
            b2 = r.end_sec * beats_per_sec
            span = f"beat {b1:g}" if b1 == b2 else f"beat {b1:g} → {b2:g}"
            out.write(
                f"        {span}: {r.start_value:g} → {r.end_value:g}  ({r.shape})\n"
            )
    out.write("\n")


def _write_automation(
    out: StringIO, plan: Plan, mix_tempo: float | None, anchors: dict[str, float] | None
) -> None:
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
            at = _fmt_mix_position(kf.at, mix_tempo, anchors)
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


def _fmt_tempo(seg) -> str:
    if seg.target_bpm is None:
        return ""
    if seg.target_bpm == "mix":
        return "  @ mix bpm"
    return f"  @ {seg.target_bpm:g} bpm"


def _fmt_mix_position(
    pos,
    mix_tempo: float | None,
    anchors: dict[str, float] | None = None,
    default: str = "?",
) -> str:
    if pos is None:
        return default
    if isinstance(pos, AfterPos):
        raw = f"after {pos.after}"
        if pos.offset is not None:
            off = _fmt_duration(pos.offset)
            raw += f" {off}" if off.startswith("-") else f" +{off}"
    else:
        raw = _fmt_track_position(pos)
    if isinstance(pos, CuePos):
        return f"{raw} (invalid in mix-time)"
    try:
        beats = position_to_mix_beats(pos, mix_tempo, anchors)
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
