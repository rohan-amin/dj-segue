"""Inspector pretty-printer tests."""

from __future__ import annotations

from dj_segue.inspect import format_plan
from dj_segue.schema import load_plan
from dj_segue.schema.plan import Plan


def test_inspector_renders_example_plan(example_plan_path) -> None:
    plan = load_plan(example_plan_path)
    out = format_plan(plan)

    # Header
    assert "Hello Mix" in out
    assert "schema_version : 0.1" in out
    assert "120" in out  # mix tempo

    # Tracks section
    assert "track_a" in out and "track_b" in out
    assert "bpm=120" in out

    # Decks section
    assert "deck 1" in out and "deck_a" in out
    assert "deck 2" in out and "deck_b" in out

    # Timeline mix-time resolution
    assert "mix-beat 0" in out
    assert "mix-beat 32" in out

    # Validation passed
    assert "validation" in out
    assert "ok" in out


def test_inspector_reports_validation_issues(minimal_plan_data) -> None:
    minimal_plan_data["timeline"][0]["track"] = "missing"
    plan = Plan.model_validate(minimal_plan_data)
    out = format_plan(plan)
    assert "issue" in out
    assert "missing" in out


def test_inspector_can_skip_validation(minimal_plan_data) -> None:
    minimal_plan_data["timeline"][0]["track"] = "missing"
    plan = Plan.model_validate(minimal_plan_data)
    out = format_plan(plan, run_validation=False)
    assert "validation" not in out


def test_inspector_renders_all_segment_types(minimal_plan_data) -> None:
    minimal_plan_data["timeline"].append(
        {"type": "silence", "deck": 1, "duration": {"beats": 4}}
    )
    minimal_plan_data["timeline"].append(
        {
            "type": "transition",
            "style": "crossfade",
            "from_deck": 1,
            "to_deck": 2,
            "start_at": {"beat": 100},
            "duration": {"beats": 4},
        }
    )
    plan = Plan.model_validate(minimal_plan_data)
    out = format_plan(plan, run_validation=False)
    assert "play" in out and "silence" in out and "transit" in out
    assert "crossfade" in out


def test_inspector_renders_all_automation_lanes(minimal_plan_data) -> None:
    minimal_plan_data["automation"].extend(
        [
            {
                "lane": "eq",
                "deck": 1,
                "band": "low",
                "keyframes": [
                    {"at": {"beat": 0}, "value_db": 0},
                    {"at": {"beat": 4}, "value_db": -24},
                ],
            },
            {
                "lane": "crossfader",
                "keyframes": [
                    {"at": {"beat": 0}, "value": -1.0},
                    {"at": {"beat": 8}, "value": 1.0},
                ],
            },
        ]
    )
    plan = Plan.model_validate(minimal_plan_data)
    out = format_plan(plan, run_validation=False)
    assert "deck_volume" in out
    assert "eq" in out and "low" in out and "dB" in out
    assert "crossfader" in out
