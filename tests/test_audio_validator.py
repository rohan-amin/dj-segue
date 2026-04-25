"""Audio-aware (deferred) validation tests."""

from __future__ import annotations

import pytest

from dj_segue.schema import PlanValidationError, validate_against_audio
from dj_segue.schema.plan import Plan


def _build(data: dict) -> Plan:
    return Plan.model_validate(data)


def test_passes_when_positions_inside_track_duration(minimal_plan_data) -> None:
    plan = _build(minimal_plan_data)
    # Track lasts 16s (32 beats at 120 bpm). Plan plays beat 0..32. Just fits.
    validate_against_audio(plan, {"a": 16.0, "b": 16.0})


def test_fails_when_to_exceeds_track_duration(minimal_plan_data) -> None:
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_against_audio(plan, {"a": 5.0, "b": 16.0})
    assert any("16.000s" in m and "5.000s" in m for m in exc.value.issues)


def test_detects_overlap_on_same_deck(minimal_plan_data) -> None:
    minimal_plan_data["timeline"].append(
        {
            "type": "play",
            "deck": 1,
            "track": "a",
            "from": {"beat": 0},
            "to": {"beat": 32},
            "start_at": {"beat": 8},  # overlaps the first segment which runs beats 0..32
        }
    )
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_against_audio(plan, {"a": 16.0, "b": 16.0})
    assert any("deck 1" in m and "overlap" in m for m in exc.value.issues)


def test_back_to_back_segments_on_same_deck_are_not_overlap(minimal_plan_data) -> None:
    # First segment ends at beat 32; second starts at beat 32 — adjacency, not overlap.
    minimal_plan_data["timeline"].append(
        {
            "type": "play",
            "deck": 1,
            "track": "a",
            "from": {"beat": 0},
            "to": {"beat": 8},
            "start_at": {"beat": 32},
        }
    )
    plan = _build(minimal_plan_data)
    validate_against_audio(plan, {"a": 16.0, "b": 16.0})


def test_silence_segments_advance_deck_cursor(minimal_plan_data) -> None:
    # Make deck 1's timeline: play [0..32] beats, silence 4 beats, play [0..8] beats.
    # Implicit start_at on the second play should land at beat 36.
    minimal_plan_data["timeline"].append(
        {"type": "silence", "deck": 1, "duration": {"beats": 4}}
    )
    minimal_plan_data["timeline"].append(
        {
            "type": "play",
            "deck": 1,
            "track": "a",
            "from": {"beat": 0},
            "to": {"beat": 8},
        }
    )
    plan = _build(minimal_plan_data)
    validate_against_audio(plan, {"a": 16.0, "b": 16.0})  # no overlap


def test_to_must_be_after_from(minimal_plan_data) -> None:
    minimal_plan_data["timeline"][0]["from"] = {"beat": 32}
    minimal_plan_data["timeline"][0]["to"] = {"beat": 16}
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_against_audio(plan, {"a": 16.0, "b": 16.0})
    assert any("must be after" in m for m in exc.value.issues)
