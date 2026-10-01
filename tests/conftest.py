"""Shared test fixtures."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True, scope="session")
def _stretch_cache_in_tmp(tmp_path_factory):
    """Keep test stretches out of the user's ~/.cache."""
    mp = pytest.MonkeyPatch()
    mp.setenv("DJ_SEGUE_STRETCH_CACHE", str(tmp_path_factory.mktemp("stretch-cache")))
    yield
    mp.undo()

EXAMPLE_PLAN = REPO_ROOT / "examples" / "hello_mix.plan.jsonc"
SR = 44100


def _write_clicks(path: Path, bpm: float, offset: float, seconds: float = 30.0) -> None:
    """Noise-burst clicks at `offset + k * 60/bpm` — a track with a known grid."""
    y = np.zeros(int(seconds * SR), dtype=np.float32)
    rng = np.random.default_rng(0)
    burst = (rng.standard_normal(441) * np.exp(-np.arange(441) / 60)).astype(np.float32) * 0.8
    t = offset
    while t < seconds - 0.02:
        s = int(round(t * SR))
        y[s : s + 441] += burst
        t += 60.0 / bpm
    sf.write(path, y, SR, subtype="FLOAT")


def _write_drums(
    path: Path,
    bpm: float,
    offset: float,
    seconds: float = 30.0,
    hat_amp: float = 0.8,
    first_beat_in_bar: int | None = None,
) -> None:
    """Kick on every beat (starting at `offset`), noise hi-hat on every off-beat.

    `hat_amp` 0.8 makes the off-beat hats louder broadband than the kick — the
    case that pulls a naive grid half a beat off. With `first_beat_in_bar` set,
    each bar's 1 also gets a bass note and crash; the file starts on that beat
    of the bar (0 = starts on the 1).
    """
    rng = np.random.default_rng(0)
    per = 60.0 / bpm
    y = np.zeros(int(seconds * SR), dtype=np.float32)
    tk = np.arange(int(0.12 * SR)) / SR
    kick = (np.sin(2 * np.pi * (55 + 60 * np.exp(-tk * 40)) * tk) * np.exp(-tk * 25) * 0.6).astype(np.float32)
    hn = int(0.03 * SR)
    hat = np.diff(np.r_[0, rng.standard_normal(hn)]) * np.exp(-np.arange(hn) / SR * 150)
    hat = (hat / np.abs(hat).max() * hat_amp).astype(np.float32)
    tb = np.arange(int(per * 3 * SR)) / SR
    bass = (np.sin(2 * np.pi * 41.2 * tb) * np.exp(-tb * 1.5) * 0.4).astype(np.float32)
    cn = SR
    crash = (np.diff(np.r_[0, rng.standard_normal(cn)]) * np.exp(-np.arange(cn) / SR * 4) * 0.15).astype(np.float32)

    def add(sig: np.ndarray, at: float) -> None:
        s = int(round(at * SR))
        e = min(len(y), s + len(sig))
        if s < e:
            y[s:e] += sig[: e - s]

    k, t = 0, offset
    while t < seconds:
        add(kick, t)
        add(hat, t + per / 2)
        if first_beat_in_bar is not None and (k + first_beat_in_bar) % 4 == 0:
            add(bass, t)
            add(crash, t)
        k += 1
        t += per
    sf.write(path, y, SR, subtype="FLOAT")


@pytest.fixture(scope="session")
def write_drums():
    """Factory: write_drums(path, bpm, offset, seconds=30, hat_amp=0.8, first_beat_in_bar=None)."""
    return _write_drums


@pytest.fixture(scope="session")
def write_clicks():
    """Factory: write_clicks(path, bpm, offset, seconds=30) → click-track WAV."""
    return _write_clicks


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
