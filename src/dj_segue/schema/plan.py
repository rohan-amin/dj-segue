"""Pydantic models for the dj-segue plan schema (v0.1–v0.9).

The shape mirrors `docs/schema-v0.10.md` (the latest). Cross-field rules that pydantic alone
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


class WordPos(BaseModel):
    """v0.10: where a word is sung (from `dj-segue words`). `word` may be a
    phrase ("bottoms up": its words in a row); matching ignores case and
    punctuation. `nth` counts from 1 (negative from the end: -1 = the last
    time). `edge`: the word's start, or the end of its (last) word.

    In a track position (`from`/`to`) it's the word's time in the track. In a
    mix position (`start_at`, keyframe `at`, a handoff's `swap_at`) `in`
    names a play segment, and it's when that segment plays the word.
    `offset` moves it, e.g. `{"beats": -8}`: 8 beats before the word."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    word: str = Field(min_length=1)
    nth: int = 1
    edge: Literal["start", "end"] = "start"
    in_: str | None = Field(default=None, alias="in")
    # A duration from the word (may be negative): track beats/bars at the
    # track's tempo in a track position, mix beats/bars in a mix position.
    offset: "Duration | None" = None

    @model_validator(mode="after")
    def _nth_nonzero(self) -> "WordPos":
        if self.nth == 0:
            raise ValueError("nth counts from 1 (or from -1 for the last); 0 isn't a position")
        return self


def _normalize_position(v: Any) -> Any:
    # `"from": "intro_drop"` is shorthand for `{"cue": "intro_drop"}`.
    if isinstance(v, str):
        return {"cue": v}
    return v


Position = Annotated[
    Union[BeatPos, BarPos, SecondPos, CuePos, WordPos],
    BeforeValidator(_normalize_position),
]


class AfterPos(BaseModel):
    """v0.3, mix-time only: the moment segment `after` ends, plus `offset`
    (which may be negative, e.g. `{"beats": -16}` = 16 beats before the end)."""

    model_config = _FORBID
    after: str
    offset: "Duration | None" = None


# Mix-time positions (start_at, keyframe `at`). Cues stay in the union so the
# validator can report "cue not allowed in mix-time" rather than a parse error.
MixPosition = Annotated[
    Union[BeatPos, BarPos, SecondPos, CuePos, AfterPos, WordPos],
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

AfterPos.model_rebuild()
WordPos.model_rebuild()


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
    # Omitted → detected by the preprocessor (v0.2+). A number overrides
    # detection and is used exactly (the beat phase is still detected).
    bpm: float | None = None
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
    def _bpm_positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError(f"bpm must be positive, got {v}")
        return v


# The stems a `path` track gets from the separator (M3, dj_segue.stems).
SEPARATED_STEMS = ("vocals", "drums", "bass", "other")


def available_stems(track: "Track") -> tuple[str, ...]:
    """The stems a track can be mixed by: its own `stems`, or for a `path`
    track (stems == {"full": …}) the four the separator produces."""
    if list(track.stems) == ["full"]:
        return SEPARATED_STEMS
    return tuple(track.stems)


class Deck(BaseModel):
    model_config = _FORBID
    label: str | None = None


class TempoChange(BaseModel):
    """v0.9: a change in the mix tempo at `at`: a step to `bpm`, or with
    `ramp`, a ramp to it (linear in time) that starts at `at` and lasts
    `ramp` (mix beats counted through the ramp, or seconds)."""

    model_config = _FORBID
    at: MixPosition
    bpm: float = Field(gt=0)
    ramp: Duration | None = None


class Meta(BaseModel):
    model_config = _FORBID
    mix_name: str
    author: str | None = None
    source_prompt: str | None = None
    created_at: str | None = None
    mix_tempo: float | None = None  # the starting tempo, with a tempo_map
    tempo_map: list[TempoChange] = Field(default_factory=list)  # v0.9, in time order
    target_executor: Literal["native", "mixxx"] | None = None


# ---------------------------------------------------------------------------
# Timeline segments
# ---------------------------------------------------------------------------


# v0.2: time-stretch a segment so the track plays at this tempo (pitch
# preserved). v0.3: "mix" = the mix tempo. Omitted → the track's natural tempo.
TargetBpm = Annotated[float, Field(gt=0)] | Literal["mix"]


class PlaySegment(BaseModel):
    model_config = _FORBID
    type: Literal["play"]
    id: str | None = None  # v0.3: referenced by `{"after": id}` positions
    deck: int
    track: str
    from_: Position = Field(alias="from")
    to: Position
    start_at: MixPosition | None = None
    target_bpm: TargetBpm | None = None


class SilenceSegment(BaseModel):
    model_config = _FORBID
    type: Literal["silence"]
    id: str | None = None
    deck: int
    duration: Duration


class LoopStep(BaseModel):
    """`repetitions` passes over the first `length` (track-time) of the loop."""

    model_config = _FORBID
    length: Duration
    repetitions: int = Field(ge=1)


class LoopSegment(BaseModel):
    """v0.3: repeat a region of a track that starts at `from`.

    The start stays fixed; each schedule step sets the loop length, so a
    shrinking schedule is a tightening loop. The segment lasts the sum of
    length × repetitions (converted to mix-time by the playback rate).
    """

    model_config = _FORBID
    type: Literal["loop"]
    id: str | None = None
    deck: int
    track: str
    from_: Position = Field(alias="from")
    start_at: MixPosition | None = None
    target_bpm: TargetBpm | None = None
    schedule: list[LoopStep] = Field(min_length=1)


TransitionStyle = Literal["crossfade", "cut", "vocal_handoff", "echo_out"]  # echo_out: v0.6

# v0.4: the gain curve of a crossfade (default "equal_power").
TransitionCurve = Literal["equal_power", "linear", "exponential"]


class PointCurve(BaseModel):
    """v0.5: a drawn curve for one side of a crossfade.

    `points` are [t, gain] pairs: t runs 0 → 1 across that side's window
    (strictly increasing, first 0, last 1); gain is that deck's actual volume,
    0–1. An `out` curve starts at 1 and ends at 0, an `in` curve the reverse;
    in between it may go anywhere. `smooth`: false joins the points with
    straight lines, true with a monotone cubic (PCHIP) that never overshoots
    them.
    """

    model_config = _FORBID
    points: list[tuple[float, float]] = Field(min_length=2)
    smooth: bool = False

    @model_validator(mode="after")
    def _check_points(self) -> "PointCurve":
        ts = [t for t, _ in self.points]
        if ts[0] != 0 or ts[-1] != 1:
            raise ValueError("curve points must start at t=0 and end at t=1")
        if any(b <= a for a, b in zip(ts, ts[1:])):
            raise ValueError("curve point times must strictly increase")
        if any(not 0 <= g <= 1 for _, g in self.points):
            raise ValueError("curve gains must be within 0–1")
        return self


class TransitionSide(BaseModel):
    """v0.4: override one side (`out` or `in`) of a crossfade. Unset fields
    come from the transition; `offset` is from its `start_at` (may be negative).
    v0.5: `curve` may be a drawn PointCurve."""

    model_config = _FORBID
    offset: Duration | None = None
    duration: Duration | None = None
    curve: TransitionCurve | PointCurve | None = None


class Echo(BaseModel):
    """v0.6: an echo_out's delay. The last `every` of the outgoing deck's
    audio before the cut repeats every `every`; the first repeat is at full
    level and each one after it `feedback` times the one before."""

    model_config = _FORBID
    every: Duration
    feedback: float = Field(ge=0, le=1)
    # v0.7: the tail's high-pass sweeps from off at the cut to this filter
    # knob position (0–1) at the tail's end. 0 = no sweep.
    highpass: float = Field(default=0.0, ge=0, le=1)


class SwapAt(BaseModel):
    """v0.8: where a vocal_handoff switches vocals, from its `start_at`."""

    model_config = _FORBID
    offset: Duration


class TransitionSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["transition"]
    id: str | None = None
    style: TransitionStyle
    from_deck: int
    to_deck: int
    start_at: MixPosition
    duration: Duration
    curve: TransitionCurve | None = None  # v0.4; crossfade only
    out: TransitionSide | None = None  # v0.4: the from_deck's fade
    in_: TransitionSide | None = Field(default=None, alias="in")  # v0.4: the to_deck's fade
    echo: Echo | None = None  # v0.6; echo_out only
    # v0.8; vocal_handoff only (default: halfway). v0.10: or on a word.
    swap_at: SwapAt | WordPos | None = None

    @model_validator(mode="after")
    def _shaping_is_crossfade_only(self) -> "TransitionSegment":
        for name, side, start in (("out", self.out, 1), ("in", self.in_, 0)):
            if side is not None and isinstance(side.curve, PointCurve):
                pts = side.curve.points
                if pts[0][1] != start or pts[-1][1] != 1 - start:
                    raise ValueError(
                        f"a drawn `{name}` curve must go from {start} to {1 - start}"
                    )
        if self.swap_at is not None and self.style != "vocal_handoff":
            raise ValueError("only vocal_handoff transitions take `swap_at`")
        if (self.style == "echo_out") != (self.echo is not None):
            raise ValueError("echo_out transitions need `echo`, and only they take it")
        if self.style == "echo_out":
            used = [k for k, v in (("curve", self.curve), ("out", self.out)) if v is not None]
            if used:
                raise ValueError(f"{', '.join(used)} don't apply to echo_out (only `in`)")
        elif self.style not in ("crossfade", "vocal_handoff"):
            used = [
                k for k, v in (("curve", self.curve), ("out", self.out), ("in", self.in_))
                if v is not None
            ]
            if used:
                raise ValueError(
                    f"{', '.join(used)} only apply to crossfade and vocal_handoff transitions"
                )
        return self


Segment = Annotated[
    Union[PlaySegment, SilenceSegment, LoopSegment, TransitionSegment],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Automation lanes
# ---------------------------------------------------------------------------


# "equal_power" (v0.3): sin/cos quarter-wave, as used by crossfade transitions.
Interpolation = Literal["linear", "step", "exponential", "equal_power"]


class FloatKeyframe(BaseModel):
    model_config = _FORBID
    at: MixPosition
    value: float


class DbKeyframe(BaseModel):
    model_config = _FORBID
    at: MixPosition
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
    stem: str | None = None  # v0.8: EQ one stem of the deck (None = the whole deck)
    keyframes: list[DbKeyframe]
    interpolation: Interpolation = "linear"


class FilterLane(BaseModel):
    """v0.7: a deck's filter knob, -1 (low-pass, only lows) … 0 (off) … +1
    (high-pass, only highs)."""

    model_config = _FORBID
    lane: Literal["filter"]
    deck: int
    keyframes: list[FloatKeyframe]
    interpolation: Interpolation = "linear"


class CrossfaderLane(BaseModel):
    model_config = _FORBID
    lane: Literal["crossfader"]
    keyframes: list[FloatKeyframe]
    interpolation: Interpolation = "linear"


AutomationLane = Annotated[
    Union[DeckVolumeLane, StemVolumeLane, EqLane, FilterLane, CrossfaderLane],
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
                f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS, key=lambda x: tuple(map(int, x.split('.'))))}"
            )
        return v

    @model_validator(mode="after")
    def _gate_versioned_features(self) -> "Plan":
        v = self.schema_version
        problems: list[str] = []
        if v == "0.1":
            problems += [
                f"tracks.{tid}: bpm is required in schema 0.1 (optional from 0.2)"
                for tid, t in self.tracks.items()
                if t.bpm is None
            ] + [
                f"timeline[{i}]: target_bpm requires schema_version 0.2"
                for i, seg in enumerate(self.timeline)
                if isinstance(seg, PlaySegment) and seg.target_bpm is not None
            ]
        if v in ("0.1", "0.2"):
            problems += [
                f"{where}: {what} requires schema_version 0.3"
                for where, what in _v03_features(self)
            ]
        if v in ("0.1", "0.2", "0.3", "0.4"):
            problems += [
                f"timeline[{i}]: a drawn `{name}` curve requires schema_version 0.5"
                for i, seg in enumerate(self.timeline)
                if isinstance(seg, TransitionSegment)
                for name, side in (("out", seg.out), ("in", seg.in_))
                if side is not None and isinstance(side.curve, PointCurve)
            ]
        if tuple(int(x) for x in v.split(".")) < (0, 10):
            problems += [f"{where}: a word position requires schema_version 0.10"
                         for where, _, _ in word_positions(self)]
        if v in ("0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8") and self.meta.tempo_map:
            problems.append("meta.tempo_map requires schema_version 0.9")
        if v in ("0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7"):
            problems += [
                f"timeline[{i}]: swap_at requires schema_version 0.8"
                for i, seg in enumerate(self.timeline)
                if isinstance(seg, TransitionSegment) and seg.swap_at is not None
            ] + [
                f"automation[{i}]: an eq lane's `stem` requires schema_version 0.8"
                for i, lane in enumerate(self.automation)
                if isinstance(lane, EqLane) and lane.stem is not None
            ] + [
                f"timeline[{i}]: vocal_handoff {what} requires schema_version 0.8"
                for i, seg in enumerate(self.timeline)
                if isinstance(seg, TransitionSegment) and seg.style == "vocal_handoff"
                for what, val in (("curve", seg.curve), ("out", seg.out), ("in", seg.in_))
                if val is not None
            ]
        if v in ("0.1", "0.2", "0.3", "0.4", "0.5", "0.6"):
            problems += [
                f"automation[{i}]: the filter lane requires schema_version 0.7"
                for i, lane in enumerate(self.automation)
                if isinstance(lane, FilterLane)
            ] + [
                f"timeline[{i}]: echo.highpass requires schema_version 0.7"
                for i, seg in enumerate(self.timeline)
                if isinstance(seg, TransitionSegment)
                and seg.echo is not None
                and "highpass" in seg.echo.model_fields_set
            ]
        if v in ("0.1", "0.2", "0.3", "0.4", "0.5"):
            problems += [
                f"timeline[{i}]: echo_out requires schema_version 0.6"
                for i, seg in enumerate(self.timeline)
                if isinstance(seg, TransitionSegment) and seg.style == "echo_out"
            ]
        if v in ("0.1", "0.2", "0.3"):
            problems += [
                f"timeline[{i}]: transition {what} requires schema_version 0.4"
                for i, seg in enumerate(self.timeline)
                if isinstance(seg, TransitionSegment)
                for what, val in (("curve", seg.curve), ("out", seg.out), ("in", seg.in_))
                if val is not None
            ]
        if problems:
            raise ValueError("; ".join(problems))
        return self

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


def _v03_features(plan: "Plan"):
    """(where, what) for every use of a schema-0.3 feature in the plan."""
    for i, seg in enumerate(plan.timeline):
        where = f"timeline[{i}]"
        if isinstance(seg, LoopSegment):
            yield where, "the loop segment"
            continue
        if seg.id is not None:
            yield where, "segment id"
        if isinstance(seg, PlaySegment) and seg.target_bpm == "mix":
            yield where, 'target_bpm "mix"'
        if isinstance(getattr(seg, "start_at", None), AfterPos):
            yield where, "an `after` position"
    for i, lane in enumerate(plan.automation):
        if lane.interpolation == "equal_power":
            yield f"automation[{i}]", 'interpolation "equal_power"'
        for j, kf in enumerate(lane.keyframes):
            if isinstance(kf.at, AfterPos):
                yield f"automation[{i}].keyframes[{j}]", "an `after` position"


def word_positions(plan: "Plan"):
    """(where, WordPos, "track" | "mix") for every word position (v0.10)."""
    for i, seg in enumerate(plan.timeline):
        where = f"timeline[{i}]"
        for name in ("from_", "to"):
            pos = getattr(seg, name, None)
            if isinstance(pos, WordPos):
                yield f"{where}.{name.rstrip('_')}", pos, "track"
        if isinstance(getattr(seg, "start_at", None), WordPos):
            yield f"{where}.start_at", seg.start_at, "mix"
        if isinstance(getattr(seg, "swap_at", None), WordPos):
            yield f"{where}.swap_at", seg.swap_at, "mix"
    for i, lane in enumerate(plan.automation):
        for j, kf in enumerate(lane.keyframes):
            if isinstance(kf.at, WordPos):
                yield f"automation[{i}].keyframes[{j}].at", kf.at, "mix"
    for i, change in enumerate(plan.meta.tempo_map or []):
        if isinstance(change.at, WordPos):
            yield f"meta.tempo_map[{i}].at", change.at, "mix"


def word_tracks(plan: "Plan") -> set[str]:
    """Tracks whose word timing the plan needs: those a track word position
    is in, and those of the play segments a mix word position names."""
    by_id = {seg.id: seg for seg in plan.timeline if getattr(seg, "id", None)}
    out = set()
    for i, seg in enumerate(plan.timeline):
        for name in ("from_", "to"):
            if isinstance(getattr(seg, name, None), WordPos):
                out.add(seg.track)
    for _, pos, ctx in word_positions(plan):
        if ctx == "mix" and pos.in_ in by_id and hasattr(by_id[pos.in_], "track"):
            out.add(by_id[pos.in_].track)
    return out & set(plan.tracks)


def load_plan(path: str | Path) -> Plan:
    """Read and parse a JSONC plan file. Raises pydantic ValidationError on schema issues."""
    p = Path(path)
    data = jsonc.loads(p.read_text())
    return Plan.model_validate(data)
