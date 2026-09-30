"""Cross-field validator tests."""

from __future__ import annotations

import pytest

from dj_segue.schema import PlanValidationError, load_plan, validate_plan
from dj_segue.schema.plan import Plan


def _build(data: dict) -> Plan:
    return Plan.model_validate(data)


def test_example_plan_validates(example_plan_path) -> None:
    plan = load_plan(example_plan_path)
    validate_plan(plan)  # no raise


def test_unknown_track_in_play_segment(minimal_plan_data) -> None:
    minimal_plan_data["timeline"][0]["track"] = "ghost"
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert any("ghost" in m for m in exc.value.issues)


def test_undeclared_deck_in_timeline(minimal_plan_data) -> None:
    minimal_plan_data["decks"] = {"1": {}}  # drop deck 2
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert any("deck 2" in m for m in exc.value.issues)


def test_undeclared_deck_in_automation(minimal_plan_data) -> None:
    minimal_plan_data["automation"][0]["deck"] = 3
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert any("deck 3" in m for m in exc.value.issues)


def test_unknown_cue_in_play_from(minimal_plan_data) -> None:
    minimal_plan_data["timeline"][0]["from"] = "no_such_cue"
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert any("no_such_cue" in m for m in exc.value.issues)


def test_cue_not_allowed_in_mix_time_start_at(minimal_plan_data) -> None:
    minimal_plan_data["timeline"][0]["start_at"] = "drop"
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert any("not allowed in mix-time" in m for m in exc.value.issues)


def test_cue_not_allowed_in_automation_at(minimal_plan_data) -> None:
    minimal_plan_data["automation"][0]["keyframes"][0]["at"] = "drop"
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert any("not allowed in mix-time" in m for m in exc.value.issues)


def test_keyframes_must_be_time_ordered(minimal_plan_data) -> None:
    minimal_plan_data["automation"][0]["keyframes"] = [
        {"at": {"beat": 10}, "value": 1.0},
        {"at": {"beat": 5}, "value": 0.0},
    ]
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert any("time-ordered" in m for m in exc.value.issues)


def test_keyframe_ordering_uses_mixed_units(minimal_plan_data) -> None:
    # bar 1 at 4/4 = beat 4, second 0.5 at 120bpm = beat 1.0 — ordered (1, 4) OK
    minimal_plan_data["automation"][0]["keyframes"] = [
        {"at": {"second": 0.5}, "value": 0.0},
        {"at": {"bar": 1}, "value": 1.0},
    ]
    plan = _build(minimal_plan_data)
    validate_plan(plan)  # OK


def test_stem_volume_requires_stem_on_deck_tracks(minimal_plan_data) -> None:
    minimal_plan_data["automation"].append(
        {
            "lane": "stem_volume",
            "deck": 1,
            "stem": "vocals",
            "keyframes": [{"at": {"beat": 0}, "value": 1.0}],
        }
    )
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert any("vocals" in m and "no stem" in m for m in exc.value.issues)


def test_vocal_handoff_requires_vocals_stem(minimal_plan_data) -> None:
    minimal_plan_data["timeline"].append(
        {
            "type": "transition",
            "style": "vocal_handoff",
            "from_deck": 1,
            "to_deck": 2,
            "start_at": {"beat": 30},
            "duration": {"beats": 4},
        }
    )
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert any("vocal_handoff" in m for m in exc.value.issues)


def test_vocal_handoff_passes_when_stems_present(minimal_plan_data) -> None:
    stems = {
        "vocals": "x/v.npy",
        "drums": "x/d.npy",
        "bass": "x/b.npy",
        "other": "x/o.npy",
    }
    for tid in ("a", "b"):
        del minimal_plan_data["tracks"][tid]["path"]
        minimal_plan_data["tracks"][tid]["stems"] = stems
    minimal_plan_data["timeline"].append(
        {
            "type": "transition",
            "style": "vocal_handoff",
            "from_deck": 1,
            "to_deck": 2,
            "start_at": {"beat": 30},
            "duration": {"beats": 4},
        }
    )
    plan = _build(minimal_plan_data)
    validate_plan(plan)


def test_multiple_issues_collected(minimal_plan_data) -> None:
    minimal_plan_data["timeline"][0]["track"] = "missing"
    minimal_plan_data["automation"][0]["deck"] = 9
    plan = _build(minimal_plan_data)
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert len(exc.value.issues) >= 2


# ---------------------------------------------------------------------------
# Transitions (M2)
# ---------------------------------------------------------------------------


def _transition(style: str, beat: float, dur: float, frm: int = 1, to: int = 2) -> dict:
    return {
        "type": "transition", "style": style, "from_deck": frm, "to_deck": to,
        "start_at": {"beat": beat}, "duration": {"beats": dur},
    }


def _issues(data: dict) -> list[str]:
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(_build(data))
    return exc.value.issues


def test_crossfade_example_plan_validates(example_plan_path) -> None:
    validate_plan(load_plan(example_plan_path.parent / "crossfade_mix.plan.jsonc"))


def test_valid_crossfade_and_cut_pass(minimal_plan_data) -> None:
    minimal_plan_data["timeline"] += [
        _transition("crossfade", 28, 4),
        _transition("cut", 40, 0, frm=2, to=1),
    ]
    validate_plan(_build(minimal_plan_data))


def test_cut_with_duration_is_rejected(minimal_plan_data) -> None:
    minimal_plan_data["timeline"].append(_transition("cut", 32, 4))
    assert any("cut is instantaneous" in m for m in _issues(minimal_plan_data))


def test_crossfade_with_zero_duration_is_rejected(minimal_plan_data) -> None:
    minimal_plan_data["timeline"].append(_transition("crossfade", 32, 0))
    assert any("duration must be positive" in m for m in _issues(minimal_plan_data))


def test_transition_to_same_deck_is_rejected(minimal_plan_data) -> None:
    minimal_plan_data["timeline"].append(_transition("crossfade", 32, 4, frm=1, to=1))
    assert any("both 1" in m for m in _issues(minimal_plan_data))


def test_overlapping_transitions_on_a_deck_are_rejected(minimal_plan_data) -> None:
    minimal_plan_data["timeline"] += [
        _transition("crossfade", 28, 8),
        _transition("crossfade", 32, 4, frm=2, to=1),
    ]
    assert any("deck 1" in m and "overlaps" in m for m in _issues(minimal_plan_data))


def test_fading_a_deck_out_twice_is_rejected(minimal_plan_data) -> None:
    minimal_plan_data["timeline"] += [
        _transition("crossfade", 28, 4),
        _transition("crossfade", 40, 4),
    ]
    assert any("faded out" in m for m in _issues(minimal_plan_data))


def test_two_crossfader_lanes_are_rejected(minimal_plan_data) -> None:
    lane = {"lane": "crossfader", "keyframes": [{"at": {"beat": 0}, "value": 0.0}]}
    minimal_plan_data["automation"] += [lane, dict(lane)]
    assert any("only one crossfader" in m for m in _issues(minimal_plan_data))
