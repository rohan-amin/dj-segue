"""M2.5 — tempo matching: time-stretch, schema 0.2, span timing, and the
acceptance test (two tracks at different tempos, beatmatched through a
crossfade).

Needs the `rubberband` CLI for the stretch tests; they skip without it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from dj_segue.analyzer.beat import detect_onsets
from dj_segue.compiler import timeline_spans
from dj_segue.executor.native import NativeEngine
from dj_segue.executor.native.stretch import time_stretch
from dj_segue.preprocessor import preprocess
from dj_segue.schema import validate_against_audio, validate_plan
from dj_segue.schema.plan import Plan
from dj_segue.time_math import TrackGrid

SR = 44100  # matches conftest's click tracks

needs_rubberband = pytest.mark.skipif(
    shutil.which("rubberband") is None, reason="rubberband CLI not installed"
)


def _plan(data: dict) -> Plan:
    base = {"schema_version": "0.2", "meta": {"mix_name": "t"}, "decks": {"1": {}, "2": {}},
            "automation": []}
    return Plan.model_validate(base | data)


def _play(deck, track, frm, to, start=None, target=None) -> dict:
    seg = {"type": "play", "deck": deck, "track": track,
           "from": {"beat": frm}, "to": {"beat": to}}
    if start is not None:
        seg["start_at"] = {"beat": start}
    if target is not None:
        seg["target_bpm"] = target
    return seg


def _click_onsets(y: np.ndarray) -> np.ndarray:
    return detect_onsets(y, SR)[0]


# ---------------------------------------------------------------------------
# Time-stretch
# ---------------------------------------------------------------------------


@needs_rubberband
def test_stretch_places_clicks_at_new_tempo(tmp_path, write_clicks) -> None:
    write_clicks(tmp_path / "c.wav", 126.0, 0.1, seconds=20)
    y, _ = sf.read(tmp_path / "c.wav", dtype="float32", always_2d=True)
    rate = 123.0 / 126.0
    out = time_stretch(y, SR, rate)
    assert out.shape[0] == pytest.approx(y.shape[0] / rate, abs=2)
    onsets = _click_onsets(out[:, 0])
    per = 60.0 / 123.0
    expected = 0.1 / rate + per * np.round((onsets - 0.1 / rate) / per)
    assert np.max(np.abs(onsets - expected)) < 0.004


@needs_rubberband
def test_consecutive_stretches_use_their_own_rates(tmp_path) -> None:
    # Regression: pyrubberband mutated a shared args dict, so the first call's
    # rate leaked into every later call in the same process.
    y = np.zeros((SR, 2), dtype=np.float32)
    y[::4410] = 0.5
    assert time_stretch(y, SR, 2.0).shape[0] == pytest.approx(SR / 2, abs=2)
    assert time_stretch(y, SR, 0.5).shape[0] == pytest.approx(SR * 2, abs=2)


@needs_rubberband
def test_stretch_preserves_levels_above_full_scale() -> None:
    # 32-bit float temp files: no 16-bit quantization or clipping at 0 dBFS.
    t = np.arange(SR) / SR
    y = (1.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)[:, None].repeat(2, 1)
    assert np.max(np.abs(time_stretch(y, SR, 0.9))) > 1.2


def test_stretch_rate_one_is_identity() -> None:
    y = np.ones((10, 2), dtype=np.float32)
    assert time_stretch(y, SR, 1.0) is y


# ---------------------------------------------------------------------------
# Schema 0.2
# ---------------------------------------------------------------------------


def test_v02_allows_auto_bpm_and_target_bpm() -> None:
    plan = _plan({"tracks": {"a": {"path": "a.wav"}}, "timeline": [_play(1, "a", 0, 8, target=124)]})
    assert plan.tracks["a"].bpm is None
    assert plan.timeline[0].target_bpm == 124


def test_v01_requires_bpm() -> None:
    with pytest.raises(ValidationError, match="bpm is required in schema 0.1"):
        Plan.model_validate({"schema_version": "0.1", "meta": {"mix_name": "t"},
                             "tracks": {"a": {"path": "a.wav"}}, "decks": {"1": {}},
                             "timeline": []})


def test_v01_rejects_target_bpm() -> None:
    with pytest.raises(ValidationError, match="target_bpm requires schema_version 0.2"):
        Plan.model_validate({"schema_version": "0.1", "meta": {"mix_name": "t"},
                             "tracks": {"a": {"path": "a.wav", "bpm": 120}},
                             "decks": {"1": {}},
                             "timeline": [_play(1, "a", 0, 8, target=124)]})


def test_target_bpm_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _plan({"tracks": {"a": {"path": "a.wav"}}, "timeline": [_play(1, "a", 0, 8, target=0)]})


# ---------------------------------------------------------------------------
# Span timing and validation with tempo
# ---------------------------------------------------------------------------


def test_span_of_stretched_segment() -> None:
    plan = _plan({"meta": {"mix_name": "t", "mix_tempo": 120},
                  "tracks": {"a": {"path": "a.wav"}},
                  "timeline": [_play(1, "a", 0, 32, start=0, target=120)]})
    grids = {"a": TrackGrid(anchor_sec=0.1, bpm=125.0)}
    (sp,) = timeline_spans(plan, 120.0, grids)
    assert sp.rate == pytest.approx(120 / 125)
    assert sp.track_from_sec == pytest.approx(0.1)
    # 32 track-beats played at 120 bpm take 16 s of mix time.
    assert sp.mix_end_sec - sp.mix_start_sec == pytest.approx(16.0)


def test_validate_plan_defers_tempo_checks_until_grids() -> None:
    plan = _plan({"tracks": {"a": {"path": "a.wav"}, "b": {"path": "b.wav"}},
                  "timeline": [_play(1, "a", 0, 32, start=0), _play(2, "b", 0, 32, start=24),
                               {"type": "transition", "style": "crossfade", "from_deck": 1,
                                "to_deck": 2, "start_at": {"beat": 24},
                                "duration": {"beats": 8}}]})
    validate_plan(plan)  # mix tempo unknown: timing checks skipped, no error
    grids = {"a": TrackGrid(0.0, 124.0), "b": TrackGrid(0.0, 124.0)}
    validate_plan(plan, grids)
    validate_against_audio(plan, {"a": 60.0, "b": 60.0}, grids)


def test_overlap_check_uses_stretched_length() -> None:
    # At natural tempo (125) the first segment ends before beat 32 of the mix
    # (120 bpm); slowed to 120 it runs to exactly 32 — and into the next.
    plan = _plan({"meta": {"mix_name": "t", "mix_tempo": 120},
                  "tracks": {"a": {"path": "a.wav"}},
                  "timeline": [_play(1, "a", 0, 32, start=0, target=110),
                               _play(1, "a", 0, 4, start=31)]})
    from dj_segue.schema import PlanValidationError

    with pytest.raises(PlanValidationError, match="overlaps"):
        validate_against_audio(plan, {"a": 60.0}, {"a": TrackGrid(0.0, 125.0)})


def test_engine_requires_grids_for_auto_tempo(tmp_path, write_clicks) -> None:
    write_clicks(tmp_path / "a.wav", 120.0, 0.0, seconds=5)
    plan = _plan({"tracks": {"a": {"path": "a.wav"}}, "timeline": [_play(1, "a", 0, 4)]})
    with pytest.raises(ValueError, match="mix tempo is unknown"):
        NativeEngine().render(plan, tmp_path)


# ---------------------------------------------------------------------------
# Acceptance: two tempos, beatmatched through a crossfade
# ---------------------------------------------------------------------------

MIX_BPM = 123.0


@pytest.fixture(scope="module")
def beatmatched(tmp_path_factory, write_clicks):
    if shutil.which("rubberband") is None:
        pytest.skip("rubberband CLI not installed")
    root = tmp_path_factory.mktemp("m25")
    write_clicks(root / "slow.wav", 120.0, 0.13, seconds=40)
    write_clicks(root / "fast.wav", 126.0, 0.31, seconds=40)
    plan = _plan({
        "meta": {"mix_name": "beatmatch", "mix_tempo": MIX_BPM},
        "tracks": {"slow": {"path": "slow.wav"}, "fast": {"path": "fast.wav"}},
        "timeline": [
            _play(1, "slow", 0, 48, start=0, target=MIX_BPM),
            _play(2, "fast", 0, 48, start=32, target=MIX_BPM),
            {"type": "transition", "style": "crossfade", "from_deck": 1, "to_deck": 2,
             "start_at": {"beat": 32}, "duration": {"beats": 16}},
        ],
    })
    grids = preprocess(plan, root).grids()
    validate_plan(plan, grids)
    durations = {t: 40.0 for t in plan.tracks}
    validate_against_audio(plan, durations, grids)
    out = NativeEngine().render(plan, root, grids).samples[:, 0]
    return out, grids


def test_detected_source_tempos(beatmatched) -> None:
    _, grids = beatmatched
    assert grids["slow"].bpm == pytest.approx(120.0, abs=0.01)
    assert grids["fast"].bpm == pytest.approx(126.0, abs=0.01)


def test_mix_length_is_80_mix_beats(beatmatched) -> None:
    out, _ = beatmatched
    assert out.shape[0] == pytest.approx(80 * 60 / MIX_BPM * SR, abs=1)


@pytest.mark.parametrize(("name", "b1", "b2"), [("slow solo", 1, 31), ("crossfade", 33, 47),
                                                 ("fast solo", 49, 79)])
def test_every_click_lands_on_the_mix_grid(beatmatched, name, b1, b2) -> None:
    out, _ = beatmatched
    per = 60.0 / MIX_BPM
    onsets = _click_onsets(out)
    region = onsets[(onsets > b1 * per) & (onsets < b2 * per)]
    assert len(region) >= (b2 - b1) - 2, f"{name}: missing clicks"
    offsets = (region + per / 2) % per - per / 2
    # Beat 0 of each track (its first click) is placed on a mix beat, so every
    # click — from either deck — should sit on the mix grid.
    assert np.max(np.abs(offsets)) < 0.005, f"{name}: max offset {np.max(np.abs(offsets))*1000:.1f} ms"
