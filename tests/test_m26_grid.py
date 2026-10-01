"""M2.6 — grid robustness: kick-weighted half-beat choice, BPM rounding, and
downbeat anchoring.

Downbeat *correctness* is a property of the beat_this model on real music and
can't be judged on synthetic audio (it hears our synthetic accent as a pickup).
What we test here is our side: the vote → beat-0 logic, and that the pipeline
is consistent — trimming k beats off the start moves beat 0 by k.
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from dj_segue.analyzer import BeatAnalysis, analyze_audio
from dj_segue.analyzer.beat import _round_bpm
from dj_segue.analyzer.downbeat import downbeat_model_id, downbeat_offset

SR = 44100  # matches conftest's generators

needs_downbeat_model = pytest.mark.skipif(
    downbeat_model_id() is None, reason="beat_this not installed"
)


def _phase_error_ms(anchor: float, offset: float, bpm: float) -> float:
    per = 60.0 / bpm
    return ((anchor - offset + per / 2) % per - per / 2) * 1000


# ---------------------------------------------------------------------------
# Half-beat: the kick defines the beat, not the loudest onset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("bpm", "offset", "hat_amp"), [(124.0, 0.1, 0.8), (124.0, 0.1, 1.5),
                                                         (128.0, 0.25, 0.8)])
def test_loud_offbeat_hats_do_not_pull_grid_half_a_beat(tmp_path, write_drums, bpm, offset,
                                                        hat_amp) -> None:
    write_drums(tmp_path / "d.wav", bpm, offset, hat_amp=hat_amp)
    a = analyze_audio(tmp_path / "d.wav")
    assert a.detected_bpm == pytest.approx(bpm, abs=0.01)
    assert abs(_phase_error_ms(a.grid_anchor_sec, offset, bpm)) < 5.0


def test_declared_bpm_path_also_prefers_the_kick(tmp_path, write_drums) -> None:
    write_drums(tmp_path / "d.wav", 124.0, 0.1)
    a = analyze_audio(tmp_path / "d.wav")
    assert abs(_phase_error_ms(a.beat_anchor(124.0 + 1e-9), 0.1, 124.0)) < 5.0


# ---------------------------------------------------------------------------
# BPM rounding
# ---------------------------------------------------------------------------


def _grid_onsets(bpm: float, anchor: float, n: int) -> tuple[np.ndarray, np.ndarray]:
    t = anchor + np.arange(n) * 60.0 / bpm
    return t, np.ones(n)


def test_rounds_to_whole_bpm_when_drift_is_negligible() -> None:
    t, w = _grid_onsets(125.0, 0.3, 400)  # ~3 minutes at 125
    bpm, anchor = _round_bpm(t, w, 124.9963, 0.3)
    assert bpm == 125.0
    assert anchor == pytest.approx(0.3, abs=1e-6)


def test_does_not_round_a_genuinely_fractional_tempo() -> None:
    t, w = _grid_onsets(122.8786, 0.07, 650)  # One More Time
    bpm, _ = _round_bpm(t, w, 122.8786, 0.07)
    assert bpm == 122.8786


def test_rounds_to_half_bpm_before_finer_steps() -> None:
    t, w = _grid_onsets(87.5, 0.0, 300)
    assert _round_bpm(t, w, 87.4991, 0.0)[0] == 87.5


# ---------------------------------------------------------------------------
# Downbeat vote → beat 0
# ---------------------------------------------------------------------------


def test_offset_is_majority_position_mod_4() -> None:
    per = 0.5
    downs = 0.1 + per * np.array([2, 6, 10, 14, 18, 23])  # five vote 2, one outlier
    assert downbeat_offset(downs, 0.1, 120.0) == 2


def test_offset_is_zero_without_or_with_inconclusive_downbeats() -> None:
    assert downbeat_offset(None, 0.0, 120.0) == 0
    assert downbeat_offset(np.zeros(0), 0.0, 120.0) == 0
    split = 0.5 * np.array([0, 4, 1, 5, 2, 6, 3, 7])  # even split, no majority
    assert downbeat_offset(split, 0.0, 120.0) == 0


def _analysis(beat_anchor: float, downbeats) -> BeatAnalysis:
    empty = np.zeros(0)
    return BeatAnalysis(120.0, beat_anchor, empty, empty, empty, downbeats, SR, SR)


def test_grid_anchor_moves_beat_zero_onto_first_downbeat() -> None:
    per = 0.5
    a = _analysis(0.1, 0.1 + per * np.array([3.0, 7.0, 11.0, 15.0]))
    assert a.grid_anchor(120.0) == pytest.approx(0.1 + 3 * per)


def test_grid_anchor_without_downbeats_is_beat_phase() -> None:
    assert _analysis(0.1, None).grid_anchor(120.0) == pytest.approx(0.1)


def test_grid_anchor_keeps_downbeat_at_file_start_as_beat_zero() -> None:
    # Beat phase a hair before 0, downbeats on it: beat 0 stays there rather
    # than wrapping a whole bar later.
    a = _analysis(-0.004, -0.004 + 2.0 * np.arange(1, 6))
    assert a.grid_anchor(120.0) == pytest.approx(-0.004)


# ---------------------------------------------------------------------------
# Pipeline consistency with the real model
# ---------------------------------------------------------------------------


@needs_downbeat_model
def test_trimming_k_beats_moves_beat_zero_by_k(tmp_path, write_drums) -> None:
    bpm = 124.0
    per = 60.0 / bpm
    write_drums(tmp_path / "full.wav", bpm, 0.05, seconds=40, hat_amp=0.3, first_beat_in_bar=0)
    y, _ = sf.read(tmp_path / "full.wav", dtype="float32")
    full = analyze_audio(tmp_path / "full.wav")
    base = round((full.grid_anchor(full.detected_bpm) - full.grid_anchor_sec) / per) % 4
    for trim in (1, 2, 3):
        cut = int(round((0.05 + trim * per - 0.005) * SR))
        p = tmp_path / f"trim{trim}.wav"
        sf.write(p, y[cut : cut + 30 * SR], SR, subtype="FLOAT")
        a = analyze_audio(p)
        shift = round((a.grid_anchor(a.detected_bpm) - a.grid_anchor_sec) / per) % 4
        assert shift == (base - trim) % 4, f"trim {trim}"


def test_tempo_level_follows_the_reference() -> None:
    from dj_segue.analyzer.beat import pick_tempo_level

    # Drake's "Fancy": librosa 117.45 (a triplet pulse), beat_this ~88.2.
    assert pick_tempo_level(117.45, 88.2) == pytest.approx(117.45 * 0.75)
    assert pick_tempo_level(62.0, 125.0) == pytest.approx(124.0)  # octave
    assert pick_tempo_level(126.05, 125.0) == 126.05  # already right
    assert pick_tempo_level(126.05, None) == 126.05  # no beat_this


def test_beat_tempo_needs_a_steady_pulse() -> None:
    from dj_segue.analyzer.downbeat import beat_tempo

    steady = np.arange(64) * 0.5
    assert beat_tempo(steady) == pytest.approx(120.0)
    jittery = np.cumsum(np.tile([0.5, 0.25, 0.75], 22))
    assert beat_tempo(jittery) is None
    assert beat_tempo(None) is None
