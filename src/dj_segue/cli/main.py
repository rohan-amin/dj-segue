"""dj-segue command-line entry point."""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import ValidationError

from dj_segue.executor.native import NativeEngine
from dj_segue.inspect import format_plan
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
) -> None:
    """Play a plan live, or render it to a WAV file."""
    plan = _load_or_die(plan_path)
    try:
        validate_plan(plan)
    except PlanValidationError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)

    pre = run_preprocess(plan, audio_root)
    durations = {tid: ta.primary.duration_sec for tid, ta in pre.tracks.items()}
    try:
        validate_against_audio(plan, durations)
    except PlanValidationError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)

    engine = NativeEngine()
    if render_to is not None:
        result = engine.render_to_wav(plan, audio_root, render_to)
        typer.echo(
            f"rendered {render_to} ({result.duration_sec:.3f}s, "
            f"{result.samples.shape[0]} samples @ {result.sample_rate}Hz)"
        )
    else:
        result = engine.play_live(plan, audio_root)
        typer.echo(f"played {result.duration_sec:.3f}s of audio")


if __name__ == "__main__":  # pragma: no cover
    app()
