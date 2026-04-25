"""Pydantic models for the dj-segue plan schema (v0.1).

The shape mirrors `docs/schema-v0.1.md`. Cross-field rules that pydantic alone
can't express (deck overlap, cue resolution, etc.) live in `validator.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from dj_segue.schema import jsonc
from dj_segue.schema.version import SUPPORTED_SCHEMA_VERSIONS

_FORBID = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Position and duration specifiers
# ---------------------------------------------------------------------------


class BeatPos(BaseModel):
    model_config = _FORBID
    beat: float


class BarPos(BaseModel):
    model_config = _FORBID
    bar: float


class SecondPos(BaseModel):
    model_config = _FORBID
    second: float


class CuePos(BaseModel):
    model_config = _FORBID
    cue: str


def _normalize_position(v: Any) -> Any:
    # `"from": "intro_drop"` is shorthand for `{"cue": "intro_drop"}`.
    if isinstance(v, str):
        return {"cue": v}
    return v


Position = Annotated[
    Union[BeatPos, BarPos, SecondPos, CuePos],
    BeforeValidator(_normalize_position),
]


class BeatsDur(BaseModel):
    model_config = _FORBID
    beats: float


class BarsDur(BaseModel):
    model_config = _FORBID
    bars: float


class SecondsDur(BaseModel):
    model_config = _FORBID
    seconds: float


Duration = Union[BeatsDur, BarsDur, SecondsDur]


# ---------------------------------------------------------------------------
# Tracks, decks, meta
# ---------------------------------------------------------------------------


class Cue(BaseModel):
    """A named position within a track. Stored under tracks.<id>.cues."""

    model_config = _FORBID
    beat: float | None = None
    bar: float | None = None
    second: float | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _exactly_one_position(self) -> "Cue":
        present = [k for k in ("beat", "bar", "second") if getattr(self, k) is not None]
        if len(present) != 1:
            raise ValueError(
                f"Cue must specify exactly one of beat/bar/second; got {present}"
            )
        return self

    def as_position(self) -> Position:
        if self.beat is not None:
            return BeatPos(beat=self.beat)
        if self.bar is not None:
            return BarPos(bar=self.bar)
        return SecondPos(second=self.second)  # type: ignore[arg-type]


class Track(BaseModel):
    model_config = _FORBID

    stems: dict[str, str]
    bpm: float
    key: str | None = None
    cues: dict[str, Cue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_path_shorthand(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        has_path = "path" in data
        has_stems = "stems" in data
        if has_path and has_stems:
            raise ValueError("Track may have 'path' or 'stems', not both")
        if not has_path and not has_stems:
            raise ValueError("Track must have 'path' or 'stems'")
        if has_path:
            data = {**data, "stems": {"full": data["path"]}}
            del data["path"]
        return data

    @field_validator("bpm")
    @classmethod
    def _bpm_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"bpm must be positive, got {v}")
        return v


class Deck(BaseModel):
    model_config = _FORBID
    label: str | None = None


class Meta(BaseModel):
    model_config = _FORBID
    mix_name: str
    author: str | None = None
    source_prompt: str | None = None
    created_at: str | None = None
    mix_tempo: float | None = None
    target_executor: Literal["native", "mixxx"] | None = None


# ---------------------------------------------------------------------------
# Timeline segments
# ---------------------------------------------------------------------------


class PlaySegment(BaseModel):
    model_config = _FORBID
    type: Literal["play"]
    deck: int
    track: str
    from_: Position = Field(alias="from")
    to: Position
    start_at: Position | None = None


class SilenceSegment(BaseModel):
    model_config = _FORBID
    type: Literal["silence"]
    deck: int
    duration: Duration


TransitionStyle = Literal["crossfade", "cut", "vocal_handoff"]


class TransitionSegment(BaseModel):
    model_config = _FORBID
    type: Literal["transition"]
    style: TransitionStyle
    from_deck: int
    to_deck: int
    start_at: Position
    duration: Duration


Segment = Annotated[
    Union[PlaySegment, SilenceSegment, TransitionSegment],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Automation lanes
# ---------------------------------------------------------------------------


Interpolation = Literal["linear", "step", "exponential"]


class FloatKeyframe(BaseModel):
    model_config = _FORBID
    at: Position
    value: float


class DbKeyframe(BaseModel):
    model_config = _FORBID
    at: Position
    value_db: float


class DeckVolumeLane(BaseModel):
    model_config = _FORBID
    lane: Literal["deck_volume"]
    deck: int
    keyframes: list[FloatKeyframe]
    interpolation: Interpolation = "linear"


class StemVolumeLane(BaseModel):
    model_config = _FORBID
    lane: Literal["stem_volume"]
    deck: int
    stem: str
    keyframes: list[FloatKeyframe]
    interpolation: Interpolation = "linear"


EqBand = Literal["low", "mid", "high"]


class EqLane(BaseModel):
    model_config = _FORBID
    lane: Literal["eq"]
    deck: int
    band: EqBand
    keyframes: list[DbKeyframe]
    interpolation: Interpolation = "linear"


class CrossfaderLane(BaseModel):
    model_config = _FORBID
    lane: Literal["crossfader"]
    keyframes: list[FloatKeyframe]
    interpolation: Interpolation = "linear"


AutomationLane = Annotated[
    Union[DeckVolumeLane, StemVolumeLane, EqLane, CrossfaderLane],
    Field(discriminator="lane"),
]


# ---------------------------------------------------------------------------
# Top-level plan
# ---------------------------------------------------------------------------


class Plan(BaseModel):
    model_config = _FORBID
    schema_version: str
    meta: Meta
    tracks: dict[str, Track]
    decks: dict[int, Deck]
    timeline: list[Segment]
    automation: list[AutomationLane] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if v not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"Unsupported schema_version {v!r}; "
                f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        return v

    @field_validator("decks", mode="before")
    @classmethod
    def _coerce_deck_keys(cls, v: Any) -> Any:
        if isinstance(v, dict):
            try:
                return {int(k): val for k, val in v.items()}
            except (TypeError, ValueError) as e:
                raise ValueError(f"Deck keys must be integers: {e}") from e
        return v

    @field_validator("decks", mode="after")
    @classmethod
    def _check_deck_range(cls, v: dict[int, Deck]) -> dict[int, Deck]:
        if not v:
            raise ValueError("Plan must declare at least one deck")
        if len(v) > 4:
            raise ValueError(f"v0.1 supports at most 4 decks; got {len(v)}")
        for k in v:
            if not 1 <= k <= 4:
                raise ValueError(f"Deck keys must be in 1..4; got {k}")
        return v


def load_plan(path: str | Path) -> Plan:
    """Read and parse a JSONC plan file. Raises pydantic ValidationError on schema issues."""
    p = Path(path)
    data = jsonc.loads(p.read_text())
    return Plan.model_validate(data)
