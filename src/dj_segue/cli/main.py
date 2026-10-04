"""dj-segue command-line entry point."""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import ValidationError

from dj_segue.executor.native import NativeEngine
from dj_segue.inspect import format_plan
from dj_segue.preprocessor import PreprocessResult, TempoNotDetectedError, separate_stems
from dj_segue.preprocessor import preprocess as run_preprocess
from dj_segue.schema import (
    PlanValidationError,
    load_plan,
    validate_against_audio,
    validate_plan,
)

app = typer.Typer(
    name="dj-segue",
    help="AI-driven DJ system for wordplay transitions.",
    no_args_is_help=True,
    add_completion=False,
)


def _load_or_die(plan_path: Path):
    try:
        return load_plan(plan_path)
    except ValidationError as e:
        typer.echo(f"schema error in {plan_path}:\n{e}", err=True)
        raise typer.Exit(code=2)


@app.command()
def inspect(
    plan_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Print a human-readable summary of a plan, then run cross-field validation."""
    plan = _load_or_die(plan_path)
    typer.echo(format_plan(plan))
    try:
        validate_plan(plan)
    except PlanValidationError:
        raise typer.Exit(code=1)


@app.command()
def preprocess(
    plan_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    audio_root: Path = typer.Option(
        Path("."),
        "--audio-root",
        help="Base directory for resolving relative audio paths in the plan.",
    ),
) -> None:
    """Compute and cache analysis (.beats sidecar) for tracks in a plan."""
    plan = _load_or_die(plan_path)
    try:
        validate_plan(plan)
    except PlanValidationError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    result = run_preprocess(plan, audio_root)
    typer.echo(
        f"preprocess: {result.analyzed_count} analyzed, {result.cached_count} cached "
        f"({len(result.tracks)} track(s) total)"
    )
    for tid, ta in result.tracks.items():
        detected = ta.primary.detected_bpm
        det = f"{detected:.3f}" if detected is not None else "none"
        if ta.declared_bpm is not None:
            tempo = f"bpm {ta.declared_bpm:g} (declared; detected {det})"
        elif detected is not None:
            tempo = f"bpm {detected:.3f} (detected)"
        else:
            tempo = "bpm ? — no beat detected; set `bpm` in the plan"
        anchor = ""
        if ta.declared_bpm is not None or detected is not None:
            db = "downbeat" if ta.primary.downbeat_times is not None else "first beat; no downbeat model"
            anchor = f", beat 0 @ {ta.grid.anchor_sec * 1000:.1f} ms ({db})"
        typer.echo(f"  {tid:<16} {tempo}{anchor}, {ta.primary.duration_sec:.1f}s")

    def on_track(tid: str, cached: bool) -> None:
        if not cached:
            typer.echo(f"  {tid:<16} separating stems (~40 s; cached after)…")

    separated, cached = separate_stems(plan, audio_root, on_track)
    if separated or cached:
        typer.echo(f"stems: {separated} separated, {cached} cached")


def _grids_or_die(pre: PreprocessResult):
    try:
        return pre.grids()
    except TempoNotDetectedError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)


@app.command()
def play(
    plan_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    render_to: Path | None = typer.Option(
        None,
        "--render-to",
        help="Render to a WAV file instead of live audio output.",
    ),
    audio_root: Path = typer.Option(
        Path("."),
        "--audio-root",
        help="Base directory for resolving relative audio paths in the plan.",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        help="While playing, apply edits to the plan file (e.g. saves from `tune`) "
        "a moment ahead of the playhead.",
    ),
    from_beat: float = typer.Option(
        0.0, "--from", help="Start playing at this mix beat.",
    ),
) -> None:
    """Play a plan live (it starts as it renders), or render it to a WAV file."""
    plan = _load_or_die(plan_path)
    try:
        grids = _ready(plan, audio_root)
    except PlanValidationError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)

    engine = NativeEngine()
    if render_to is not None:
        result = engine.render_to_wav(plan, audio_root, render_to, grids=grids)
        typer.echo(
            f"rendered {render_to} ({result.duration_sec:.3f}s, "
            f"{result.samples.shape[0]} samples @ {result.sample_rate}Hz)"
        )
        return

    from dj_segue.executor.native.engine import prepare
    from dj_segue.executor.native.live import LiveEngine
    from dj_segue.executor.native.live import play as play_live

    cache: dict = {}  # track audio, kept across reloads
    p = prepare(plan, audio_root, grids, audio_cache=cache)
    start = int(round(from_beat * 60.0 / p.mix_tempo * p.sample_rate))
    live = LiveEngine(p, start=start)

    def rebuild(at: int) -> LiveEngine:
        new = load_plan(plan_path)
        new_grids = _ready(new, audio_root)
        return LiveEngine(prepare(new, audio_root, new_grids, audio_cache=cache), start=at)

    typer.echo(f"playing {plan_path.name}" + (" (watching for edits)" if watch else "")
               + " — Ctrl-C to stop")
    frames = play_live(live, watch=plan_path if watch else None, rebuild=rebuild,
                       log=typer.echo)
    typer.echo(f"played {frames / p.sample_rate:.3f}s of audio")


def _ready(plan, audio_root: Path):
    """Validate `plan` (raising PlanValidationError), preprocess it and
    return its grids."""
    validate_plan(plan)
    pre = run_preprocess(plan, audio_root)
    durations = {tid: ta.primary.duration_sec for tid, ta in pre.tracks.items()}
    grids = _grids_or_die(pre)
    # Re-run structural checks with grids: those that need the mix tempo
    # were skipped above if it defaults to an auto-detected bpm.
    validate_plan(plan, grids)
    validate_against_audio(plan, durations, grids)
    separate_stems(plan, audio_root)
    return grids


@app.command()
def scrub(
    audio_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    bpm: float | None = typer.Option(
        None, "--bpm", help="Override the detected tempo (as `bpm` does in a plan)."
    ),
    start_beat: int = typer.Option(0, "--start-beat", help="Beat to start at."),
    port: int = typer.Option(0, "--port", help="Port to serve on (default: any free port)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser."),
) -> None:
    """Open a track in the browser with live beat/bar numbers and a song map,
    to find positions for a plan by ear."""
    from dj_segue.scrub import load_track, serve

    typer.echo(f"analyzing {audio_path.name} …")
    try:
        track = load_track(audio_path, bpm, start_beat)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    marks = serve(track, port, open_browser=not no_browser)
    if marks:
        typer.echo("marked beats: " + ", ".join(str(m) for m in marks))


@app.command()
def tune(
    plan_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    audio_root: Path = typer.Option(
        Path("."),
        "--audio-root",
        help="Base directory for resolving relative audio paths in the plan.",
    ),
    port: int = typer.Option(0, "--port", help="Port to serve on (default: any free port)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser."),
) -> None:
    """Open a plan's transitions in the browser to adjust fade windows and
    curves by ear; Save writes the changes into the plan in place."""
    from dj_segue.tune import STYLES as TUNE_STYLES, TuneSession, serve

    plan = _load_or_die(plan_path)
    if not any(
        getattr(seg, "type", None) == "transition" and seg.style in TUNE_STYLES
        for seg in plan.timeline
    ):
        typer.echo("no crossfade, echo out or vocal handoff in this plan", err=True)
        raise typer.Exit(code=1)
    try:
        session = TuneSession(plan_path, audio_root)
    except (PlanValidationError, TempoNotDetectedError, ValueError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    serve(session, port, open_browser=not no_browser)


if __name__ == "__main__":  # pragma: no cover
    app()
