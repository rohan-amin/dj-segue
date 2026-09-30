"""Beat-grid tests: tempo/phase detection, cache, TrackGrid math, grid-resolved
positions, preprocessing (declared vs detected bpm), and the engine honoring
the anchor.

Detection is tested on synthetic click tracks with known tempo and phase
(generated per-test, nothing checked in).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from dj_segue.analyzer import BeatAnalysis, analyze_audio
from dj_segue.analyzer.cache import load_cache, write_cache
from dj_segue.executor.native import NativeEngine
from dj_segue.preprocessor import TempoNotDetectedError, preprocess
from dj_segue.schema import load_plan
from dj_segue.schema.plan import BeatPos, CuePos, Plan, SecondPos, Track
from dj_segue.time_math import TrackGrid, track_pos_to_seconds

REPO_ROOT = Path(__file__).resolve().parent.parent
SR = 44100  # matches conftest's click tracks


def _phase_error_ms(anchor: float, offset: float, bpm: float) -> float:
    per = 60.0 / bpm
    return ((anchor - offset + per / 2) % per - per / 2) * 1000


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bpm", "offset"), [(120.0, 0.0), (126.0, 0.2), (123.4, 0.37), (90.0, 0.05), (174.0, 0.1)]
)
def test_detects_tempo_and_phase_of_click_track(tmp_path, write_clicks, bpm, offset) -> None:
    write_clicks(tmp_path / "c.wav", bpm, offset)
    a = analyze_audio(tmp_path / "c.wav")
    assert a.detected_bpm == pytest.approx(bpm, abs=0.01)
    assert abs(_phase_error_ms(a.grid_anchor_sec, offset, bpm)) < 1.0
    if float(bpm).is_integer():  # whole-number tempos snap exactly
        assert a.detected_bpm == bpm


def test_beat_at_file_start_is_beat_zero_not_wrapped(tmp_path, write_clicks) -> None:
    # Detection error can put the anchor a hair before 0; it must stay near 0
    # rather than wrapping to a full beat later (which would renumber beats).
    write_clicks(tmp_path / "c.wav", 120.0, 0.0)
    assert abs(analyze_audio(tmp_path / "c.wav").grid_anchor_sec) < 0.005


def test_sine_fixture_has_no_detectable_tempo() -> None:
    a = analyze_audio(REPO_ROOT / "tests" / "audio" / "sine_120bpm_a.wav")
    assert a.detected_bpm is None
    assert a.grid_anchor_sec == 0.0


def test_beat_anchor_for_declared_bpm_fits_phase_from_cached_onsets(tmp_path, write_clicks) -> None:
    write_clicks(tmp_path / "c.wav", 125.0, 0.3)
    a = analyze_audio(tmp_path / "c.wav")
    assert abs(_phase_error_ms(a.beat_anchor(125.0), 0.3, 125.0)) < 1.0


def test_anchors_without_onsets_are_zero() -> None:
    a = analyze_audio(REPO_ROOT / "tests" / "audio" / "sine_120bpm_a.wav")
    assert a.beat_anchor(120.0) == 0.0 and a.grid_anchor(120.0) == 0.0


# ---------------------------------------------------------------------------
# Cache round-trip
# ---------------------------------------------------------------------------


def test_cache_roundtrips_grid_fields(tmp_path) -> None:
    audio = tmp_path / "x.wav"
    sf.write(audio, np.zeros(SR, dtype=np.float32), SR, subtype="FLOAT")
    analysis = BeatAnalysis(
        detected_bpm=124.5,
        grid_anchor_sec=0.25,
        onset_times=np.asarray([0.25, 0.73]),
        onset_weights=np.asarray([1.0, 0.5]),
        onset_low_weights=np.asarray([0.8, 0.1]),
        downbeat_times=np.asarray([0.25, 2.18]),
        sample_rate=SR,
        n_samples=SR,
    )
    write_cache(audio, analysis)
    back = load_cache(audio).to_analysis()
    assert back.detected_bpm == 124.5
    assert back.grid_anchor_sec == 0.25
    np.testing.assert_allclose(back.onset_times, [0.25, 0.73])
    np.testing.assert_allclose(back.onset_low_weights, [0.8, 0.1])
    np.testing.assert_allclose(back.downbeat_times, [0.25, 2.18])


def test_cache_roundtrips_undetected_bpm(tmp_path) -> None:
    audio = tmp_path / "x.wav"
    sf.write(audio, np.zeros(SR, dtype=np.float32), SR, subtype="FLOAT")
    empty = np.zeros(0)
    write_cache(audio, BeatAnalysis(None, 0.0, empty, empty, empty, None, SR, SR))
    back = load_cache(audio).to_analysis()
    assert back.detected_bpm is None and back.downbeat_times is None


# ---------------------------------------------------------------------------
# TrackGrid math and grid-resolved positions
# ---------------------------------------------------------------------------


def test_trackgrid_beat_and_bar_to_seconds_with_anchor() -> None:
    g = TrackGrid(anchor_sec=1.0, bpm=120.0)
    assert g.beat_to_seconds(4) == pytest.approx(3.0)  # 2 s + 1 s anchor
    assert g.bar_to_seconds(1) == pytest.approx(3.0)


def test_trackgrid_fixed_defaults_to_zero_anchor() -> None:
    assert TrackGrid.fixed(120.0).beat_to_seconds(4) == pytest.approx(2.0)


def _track(bpm: float | None = 120.0, **cues: dict) -> Track:
    return Track.model_validate({"path": "x.wav", "bpm": bpm, "cues": cues})


def test_track_pos_beat_is_shifted_by_anchor() -> None:
    g = TrackGrid(anchor_sec=1.0, bpm=120.0)
    assert track_pos_to_seconds(BeatPos(beat=4), _track(), g) == pytest.approx(3.0)


def test_track_pos_second_is_not_shifted_by_anchor() -> None:
    g = TrackGrid(anchor_sec=1.0, bpm=120.0)
    assert track_pos_to_seconds(SecondPos(second=5.0), _track(), g) == 5.0


def test_track_pos_cue_beat_shifted_cue_second_not() -> None:
    track = _track(drop={"beat": 4}, mark={"second": 5.0})
    g = TrackGrid(anchor_sec=1.0, bpm=120.0)
    assert track_pos_to_seconds(CuePos(cue="drop"), track, g) == pytest.approx(3.0)
    assert track_pos_to_seconds(CuePos(cue="mark"), track, g) == 5.0


def test_track_pos_without_grid_is_zero_anchor_at_declared_bpm() -> None:
    assert track_pos_to_seconds(BeatPos(beat=90), _track(bpm=90.0)) == pytest.approx(60.0)


def test_auto_bpm_track_needs_grid_for_beats_but_not_seconds() -> None:
    track = _track(bpm=None)
    assert track_pos_to_seconds(SecondPos(second=2.5), track) == 2.5
    with pytest.raises(ValueError, match="auto-detected"):
        track_pos_to_seconds(BeatPos(beat=4), track)


# ---------------------------------------------------------------------------
# Preprocess: declared vs detected bpm
# ---------------------------------------------------------------------------


def _one_track_plan(bpm: float | None) -> Plan:
    track = {"path": "c.wav"} | ({"bpm": bpm} if bpm is not None else {})
    return Plan.model_validate(
        {
            "schema_version": "0.2",
            "meta": {"mix_name": "t"},
            "tracks": {"c": track},
            "decks": {"1": {}},
            "timeline": [{"type": "play", "deck": 1, "track": "c",
                          "from": {"beat": 0}, "to": {"beat": 8}}],
        }
    )


def test_preprocess_uses_detected_bpm_when_omitted(tmp_path, write_clicks) -> None:
    write_clicks(tmp_path / "c.wav", 126.0, 0.2)
    g = preprocess(_one_track_plan(None), tmp_path).grids()["c"]
    assert g.bpm == pytest.approx(126.0, abs=0.01)
    assert abs(_phase_error_ms(g.anchor_sec, 0.2, 126.0)) < 1.0


def test_preprocess_declared_bpm_overrides_detection(tmp_path, write_clicks) -> None:
    write_clicks(tmp_path / "c.wav", 126.0, 0.2)
    g = preprocess(_one_track_plan(63.0), tmp_path).grids()["c"]  # e.g. fixing a half-time read
    assert g.bpm == 63.0
    assert abs(_phase_error_ms(g.anchor_sec, 0.2, 63.0 * 2)) < 1.0  # phase still detected


def test_preprocess_without_bpm_or_beats_raises(tmp_path) -> None:
    sf.write(tmp_path / "c.wav", np.zeros(5 * SR, dtype=np.float32), SR, subtype="FLOAT")
    with pytest.raises(TempoNotDetectedError, match="set `bpm`"):
        preprocess(_one_track_plan(None), tmp_path).grids()


def test_preprocess_hello_mix_grids_are_declared_with_zero_anchor() -> None:
    plan = load_plan(REPO_ROOT / "examples" / "hello_mix.plan.jsonc")
    for g in preprocess(plan, REPO_ROOT).grids().values():
        assert g.bpm == 120.0 and g.anchor_sec == 0.0


# ---------------------------------------------------------------------------
# Engine honors the anchor
# ---------------------------------------------------------------------------


def _ramp_plan(tmp_path: Path) -> Plan:
    """One deck over a ramp track (sample i holds value i), so the read offset
    is directly observable in the output samples."""
    sf.write(tmp_path / "ramp.wav", np.arange(4 * SR, dtype=np.float32), SR, subtype="FLOAT")
    return Plan.model_validate(
        {
            "schema_version": "0.1",
            "meta": {"mix_name": "ramp", "mix_tempo": 120},
            "tracks": {"r": {"path": "ramp.wav", "bpm": 120}},
            "decks": {"1": {}},
            "timeline": [{"type": "play", "deck": 1, "track": "r",
                          "from": {"beat": 0}, "to": {"beat": 4}, "start_at": {"beat": 0}}],
        }
    )


def test_engine_no_grid_reads_from_sample_zero(tmp_path) -> None:
    out = NativeEngine().render(_ramp_plan(tmp_path), tmp_path).samples
    assert out.shape[0] == 2 * SR
    assert out[100, 0] == pytest.approx(100.0)


def test_engine_grid_anchor_shifts_track_read_offset(tmp_path) -> None:
    grids = {"r": TrackGrid(anchor_sec=1.0, bpm=120.0)}
    out = NativeEngine().render(_ramp_plan(tmp_path), tmp_path, grids).samples
    assert out[100, 0] == pytest.approx(float(SR + 100))
    assert out.shape[0] == 2 * SR  # span is (to - from); the anchor cancels


def test_engine_negative_anchor_pads_with_silence(tmp_path) -> None:
    grids = {"r": TrackGrid(anchor_sec=-0.01, bpm=120.0)}
    out = NativeEngine().render(_ramp_plan(tmp_path), tmp_path, grids).samples
    lead = int(round(0.01 * SR))
    assert np.all(out[:lead, 0] == 0.0)
    assert out[lead, 0] == pytest.approx(0.0) and out[lead + 5, 0] == pytest.approx(5.0)
