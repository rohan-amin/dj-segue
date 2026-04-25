"""dj-segue command-line entry point."""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import ValidationError

from dj_segue.inspect import format_plan
from dj_segue.schema import (
    PlanValidationError,
    load_plan,
    validate_plan,
)

app = typer.Typer(
    name="dj-segue",
    help="AI-driven DJ system for wordplay transitions.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def inspect(
    plan_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Print a human-readable summary of a plan, then run cross-field validation."""
    try:
        plan = load_plan(plan_path)
    except ValidationError as e:
        typer.echo(f"schema error in {plan_path}:\n{e}", err=True)
        raise typer.Exit(code=2)

    typer.echo(format_plan(plan))
    try:
        validate_plan(plan)
    except PlanValidationError:
        raise typer.Exit(code=1)


@app.command()
def preprocess(
    plan_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Compute and cache analysis for tracks in a plan (BPM, beat grid, ...)."""
    typer.echo("preprocess: not yet implemented (Session B / M1 part 2)", err=True)
    raise typer.Exit(code=2)


@app.command()
def play(
    plan_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    render_to: Path | None = typer.Option(
        None,
        "--render-to",
        help="Render to a WAV file instead of live audio output.",
    ),
) -> None:
    """Play a plan live, or render it to a WAV file."""
    typer.echo("play: not yet implemented (Session B / M1 part 2)", err=True)
    raise typer.Exit(code=2)


if __name__ == "__main__":  # pragma: no cover
    app()
