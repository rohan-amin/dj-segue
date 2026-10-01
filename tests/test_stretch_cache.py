"""The stretch cache returns exactly what rubberband produced, without rerunning it."""

from __future__ import annotations

import shutil

import numpy as np
import pytest

from dj_segue.executor.native import stretch

pytestmark = pytest.mark.skipif(shutil.which("rubberband") is None, reason="needs rubberband")

SR = 44100


def _audio(seed: int) -> np.ndarray:
    return (0.3 * np.random.default_rng(seed).standard_normal((SR, 2))).astype(np.float32)


def test_second_stretch_comes_from_the_cache(monkeypatch) -> None:
    a = _audio(0)
    first = stretch.time_stretch(a, SR, 1.05)

    def boom(*args, **kwargs):
        raise AssertionError("rubberband ran again")

    monkeypatch.setattr(stretch, "_run_rubberband", boom)
    np.testing.assert_array_equal(stretch.time_stretch(a, SR, 1.05), first)


def test_key_depends_on_samples_and_rate(monkeypatch) -> None:
    exe = stretch._rubberband()
    a = _audio(1)
    b = a.copy()
    b[100, 0] += 1e-6
    keys = {
        stretch._cache_file(exe, a, SR, 1.05),
        stretch._cache_file(exe, b, SR, 1.05),
        stretch._cache_file(exe, a, SR, 1.06),
        stretch._cache_file(exe, a, 48000, 1.05),
    }
    assert len(keys) == 4


def test_cache_can_be_turned_off(monkeypatch) -> None:
    monkeypatch.setenv("DJ_SEGUE_STRETCH_CACHE", "off")
    assert stretch._cache_file(stretch._rubberband(), _audio(2), SR, 1.05) is None


def test_default_cache_is_in_the_project_checkout() -> None:
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    assert stretch.default_cache_dir() == repo / ".cache" / "stretch"
