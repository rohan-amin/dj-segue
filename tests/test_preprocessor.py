"""Preprocessor pipeline tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from dj_segue.preprocessor import preprocess
from dj_segue.schema import load_plan

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_preprocess_hello_mix_idempotent() -> None:
    plan = load_plan(REPO_ROOT / "examples" / "hello_mix.plan.jsonc")
    first = preprocess(plan, REPO_ROOT)
    assert set(first.tracks) == {"track_a", "track_b"}
    for ta in first.tracks.values():
        assert ta.primary.sample_rate == 44100
        assert ta.primary.duration_sec == pytest.approx(17.0, abs=0.01)
    # Second call should be all-cached.
    second = preprocess(plan, REPO_ROOT)
    assert second.analyzed_count == 0
    assert second.cached_count == 2


def test_preprocess_missing_audio_errors(minimal_plan_data, tmp_path) -> None:
    minimal_plan_data["tracks"]["a"]["path"] = "no_such_file.wav"
    minimal_plan_data["tracks"]["b"]["path"] = "no_such_file.wav"
    from dj_segue.schema.plan import Plan

    plan = Plan.model_validate(minimal_plan_data)
    with pytest.raises(FileNotFoundError, match="no_such_file"):
        preprocess(plan, tmp_path)


def test_preprocess_declared_bpm_passed_through() -> None:
    plan = load_plan(REPO_ROOT / "examples" / "hello_mix.plan.jsonc")
    result = preprocess(plan, REPO_ROOT)
    assert result.tracks["track_a"].declared_bpm == 120.0
    assert result.tracks["track_b"].declared_bpm == 120.0
