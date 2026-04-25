"""Shared test fixtures."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PLAN = REPO_ROOT / "examples" / "hello_mix.plan.jsonc"


@pytest.fixture
def example_plan_path() -> Path:
    return EXAMPLE_PLAN


@pytest.fixture
def minimal_plan_data() -> dict:
    """A small valid plan as a dict, ready to mutate per-test."""
    return deepcopy(
        {
            "schema_version": "0.1",
            "meta": {"mix_name": "minimal", "mix_tempo": 120},
            "tracks": {
                "a": {"path": "a.wav", "bpm": 120, "cues": {"drop": {"beat": 16}}},
                "b": {"path": "b.wav", "bpm": 120},
            },
            "decks": {"1": {}, "2": {}},
            "timeline": [
                {
                    "type": "play",
                    "deck": 1,
                    "track": "a",
                    "from": {"beat": 0},
                    "to": {"beat": 32},
                    "start_at": {"beat": 0},
                },
                {
                    "type": "play",
                    "deck": 2,
                    "track": "b",
                    "from": {"beat": 0},
                    "to": {"beat": 32},
                    "start_at": {"beat": 32},
                },
            ],
            "automation": [
                {
                    "lane": "deck_volume",
                    "deck": 1,
                    "keyframes": [
                        {"at": {"beat": 0}, "value": 1.0},
                        {"at": {"beat": 32}, "value": 0.0},
                    ],
                    "interpolation": "step",
                },
            ],
        }
    )
