"""Pydantic schema parsing tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dj_segue.schema import load_plan
from dj_segue.schema.plan import (
    BarPos,
    BeatPos,
    CrossfaderLane,
    CuePos,
    DeckVolumeLane,
    EqLane,
    Plan,
    PlaySegment,
    SecondPos,
    StemVolumeLane,
    TransitionSegment,
)


def test_loads_example_plan(example_plan_path) -> None:
    plan = load_plan(example_plan_path)
    assert plan.schema_version == "0.1"
    assert plan.meta.mix_name == "Hello Mix"
    assert plan.meta.mix_tempo == 120
    assert set(plan.tracks) == {"track_a", "track_b"}
    assert set(plan.decks) == {1, 2}
    assert len(plan.timeline) == 2
    assert all(isinstance(s, PlaySegment) for s in plan.timeline)
    assert len(plan.automation) == 2


def test_path_shorthand_normalizes_to_full_stem(minimal_plan_data) -> None:
    plan = Plan.model_validate(minimal_plan_data)
    assert plan.tracks["a"].stems == {"full": "a.wav"}


def test_path_and_stems_are_mutually_exclusive(minimal_plan_data) -> None:
    minimal_plan_data["tracks"]["a"]["stems"] = {"full": "x.wav"}
    with pytest.raises(ValidationError) as exc:
        Plan.model_validate(minimal_plan_data)
    assert "path" in str(exc.value) and "stems" in str(exc.value)


def test_track_must_have_path_or_stems(minimal_plan_data) -> None:
    minimal_plan_data["tracks"]["a"] = {"bpm": 120}
    with pytest.raises(ValidationError):
        Plan.model_validate(minimal_plan_data)


def test_unsupported_schema_version_rejected(minimal_plan_data) -> None:
    minimal_plan_data["schema_version"] = "9.9"
    with pytest.raises(ValidationError) as exc:
        Plan.model_validate(minimal_plan_data)
    assert "9.9" in str(exc.value)


def test_decks_must_be_in_range(minimal_plan_data) -> None:
    minimal_plan_data["decks"]["7"] = {}
    with pytest.raises(ValidationError):
        Plan.model_validate(minimal_plan_data)


def test_decks_must_be_nonempty(minimal_plan_data) -> None:
    minimal_plan_data["decks"] = {}
    with pytest.raises(ValidationError):
        Plan.model_validate(minimal_plan_data)


def test_extra_fields_rejected_at_top_level(minimal_plan_data) -> None:
    minimal_plan_data["unexpected_top_level"] = 123
    with pytest.raises(ValidationError):
        Plan.model_validate(minimal_plan_data)


def test_cue_position_must_have_exactly_one_of_beat_bar_second(minimal_plan_data) -> None:
    # Both beat and second present
    minimal_plan_data["tracks"]["a"]["cues"]["bad"] = {"beat": 1, "second": 1.0}
    with pytest.raises(ValidationError):
        Plan.model_validate(minimal_plan_data)


def test_cue_string_shorthand_in_play_from(minimal_plan_data) -> None:
    minimal_plan_data["timeline"][0]["from"] = "drop"
    plan = Plan.model_validate(minimal_plan_data)
    assert isinstance(plan.timeline[0].from_, CuePos)
    assert plan.timeline[0].from_.cue == "drop"


def test_position_variants_round_trip(minimal_plan_data) -> None:
    minimal_plan_data["timeline"][0]["from"] = {"bar": 4}
    minimal_plan_data["timeline"][0]["to"] = {"second": 12.5}
    plan = Plan.model_validate(minimal_plan_data)
    assert isinstance(plan.timeline[0].from_, BarPos)
    assert plan.timeline[0].from_.bar == 4
    assert isinstance(plan.timeline[0].to, SecondPos)
    assert plan.timeline[0].to.second == 12.5


def test_transition_segment_parses(minimal_plan_data) -> None:
    minimal_plan_data["timeline"].append(
        {
            "type": "transition",
            "style": "crossfade",
            "from_deck": 1,
            "to_deck": 2,
            "start_at": {"beat": 60},
            "duration": {"beats": 4},
        }
    )
    plan = Plan.model_validate(minimal_plan_data)
    assert isinstance(plan.timeline[-1], TransitionSegment)


def test_eq_lane_uses_value_db_keyframes(minimal_plan_data) -> None:
    minimal_plan_data["automation"].append(
        {
            "lane": "eq",
            "deck": 1,
            "band": "low",
            "keyframes": [
                {"at": {"beat": 0}, "value_db": 0},
                {"at": {"beat": 4}, "value_db": -24},
            ],
        }
    )
    plan = Plan.model_validate(minimal_plan_data)
    eq = plan.automation[-1]
    assert isinstance(eq, EqLane)
    assert eq.keyframes[1].value_db == -24


def test_crossfader_lane_has_no_deck(minimal_plan_data) -> None:
    minimal_plan_data["automation"].append(
        {
            "lane": "crossfader",
            "keyframes": [
                {"at": {"beat": 0}, "value": -1.0},
                {"at": {"beat": 8}, "value": 1.0},
            ],
        }
    )
    plan = Plan.model_validate(minimal_plan_data)
    assert isinstance(plan.automation[-1], CrossfaderLane)


def test_invalid_lane_discriminator_rejected(minimal_plan_data) -> None:
    minimal_plan_data["automation"].append(
        {"lane": "no_such_lane", "deck": 1, "keyframes": []}
    )
    with pytest.raises(ValidationError):
        Plan.model_validate(minimal_plan_data)
