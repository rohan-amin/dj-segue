"""Native engine M2 behavior: transitions, crossfader, gain stacking, beat-locking,
and hard failure on features the engine doesn't implement yet.

Tracks here are constant-value (DC) FLOAT WAVs, so the output sample *is* the
applied gain times the track value — gains can be asserted exactly.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from dj_segue.executor.native import NativeEngine
from dj_segue.schema.plan import Plan

SR = 44100
BPM = 128.0  # non-integer samples per beat (20671.875) — exercises rounding


def _beat_sample(beat: float) -> int:
    return int(round(beat * 60.0 / BPM * SR))


def _dc(path: Path, value: float, seconds: float = 20.0) -> None:
    sf.write(path, np.full(int(seconds * SR), value, dtype=np.float32), SR, subtype="FLOAT")


def _plan(tmp_path: Path, timeline: list[dict], automation: list[dict] | None = None,
          tracks: dict | None = None) -> Plan:
    _dc(tmp_path / "one.wav", 1.0)
    _dc(tmp_path / "half.wav", 0.5)
    return Plan.model_validate(
        {
            "schema_version": "0.1",
            "meta": {"mix_name": "m2", "mix_tempo": BPM},
            "tracks": tracks or {
                "one": {"path": "one.wav", "bpm": BPM},
                "half": {"path": "half.wav", "bpm": BPM},
            },
            "decks": {"1": {}, "2": {}},
            "timeline": timeline,
            "automation": automation or [],
        }
    )


def _play(deck: int, track: str, start: float = 0, length: float = 32) -> dict:
    return {"type": "play", "deck": deck, "track": track,
            "from": {"beat": 0}, "to": {"beat": length}, "start_at": {"beat": start}}


def _trans(style: str, beat: float, dur: float, frm: int = 1, to: int = 2) -> dict:
    return {"type": "transition", "style": style, "from_deck": frm, "to_deck": to,
            "start_at": {"beat": beat}, "duration": {"beats": dur}}


def _render(plan: Plan, root: Path) -> np.ndarray:
    return NativeEngine().render(plan, root).samples[:, 0]


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def test_cut_is_sample_accurate_on_the_beat(tmp_path) -> None:
    plan = _plan(tmp_path, [_play(1, "one"), _play(2, "half"), _trans("cut", 17, 0)])
    out = _render(plan, tmp_path)
    n = _beat_sample(17)
    assert out[n - 1] == 1.0  # deck 1 only
    assert out[n] == 0.5  # deck 2 only, from exactly that sample
    assert np.all(out[:n] == 1.0) and np.all(out[n:] == 0.5)


def test_crossfade_endpoints_and_equal_power_midpoint(tmp_path) -> None:
    plan = _plan(tmp_path, [_play(1, "one"), _play(2, "half"), _trans("crossfade", 8, 4)])
    out = _render(plan, tmp_path)
    assert out[_beat_sample(8) - 1] == pytest.approx(1.0)
    mid = np.sqrt(0.5)
    assert out[_beat_sample(10)] == pytest.approx(mid * 1.0 + mid * 0.5, abs=1e-4)
    assert out[_beat_sample(12)] == pytest.approx(0.5)


def test_incoming_deck_is_silent_before_its_transition(tmp_path) -> None:
    plan = _plan(tmp_path, [_play(1, "one"), _play(2, "half"), _trans("crossfade", 8, 4)])
    out = _render(plan, tmp_path)
    # deck 2 is playing from beat 0 but contributes nothing until beat 8.
    assert np.all(out[: _beat_sample(8)] == 1.0)


def test_play_segment_starts_sample_accurately_on_beat(tmp_path) -> None:
    plan = _plan(tmp_path, [_play(2, "half", start=17, length=4)])
    out = _render(plan, tmp_path)
    n = _beat_sample(17)
    assert out[n - 1] == 0.0
    assert out[n] == 0.5


# ---------------------------------------------------------------------------
# Crossfader and gain stacking
# ---------------------------------------------------------------------------


def test_crossfader_lane_moves_gain_between_decks(tmp_path) -> None:
    lane = {"lane": "crossfader", "interpolation": "step",
            "keyframes": [{"at": {"beat": 0}, "value": -1.0},
                          {"at": {"beat": 8}, "value": 1.0}]}
    plan = _plan(tmp_path, [_play(1, "one"), _play(2, "half")], [lane])
    out = _render(plan, tmp_path)
    assert out[_beat_sample(8) - 1] == pytest.approx(1.0)
    assert out[_beat_sample(8)] == pytest.approx(0.5, abs=1e-6)


def test_crossfader_center_is_equal_power(tmp_path) -> None:
    lane = {"lane": "crossfader", "keyframes": [{"at": {"beat": 0}, "value": 0.0}]}
    plan = _plan(tmp_path, [_play(1, "one"), _play(2, "half")], [lane])
    out = _render(plan, tmp_path)
    assert out[1000] == pytest.approx(np.sqrt(0.5) * 1.5, abs=1e-6)


def test_deck_volume_multiplies_with_transition(tmp_path) -> None:
    vol = {"lane": "deck_volume", "deck": 2, "interpolation": "step",
           "keyframes": [{"at": {"beat": 0}, "value": 0.5}]}
    plan = _plan(tmp_path, [_play(1, "one"), _play(2, "half"), _trans("cut", 4, 0)], [vol])
    out = _render(plan, tmp_path)
    assert out[_beat_sample(4)] == pytest.approx(0.25)


def test_exponential_deck_volume_fade(tmp_path) -> None:
    vol = {"lane": "deck_volume", "deck": 1, "interpolation": "exponential",
           "keyframes": [{"at": {"beat": 0}, "value": 1.0},
                         {"at": {"beat": 8}, "value": 0.0}]}
    plan = _plan(tmp_path, [_play(1, "one")], [vol])
    out = _render(plan, tmp_path)
    # Linear-in-dB to a -60 dB floor: halfway is -30 dB.
    assert out[_beat_sample(4)] == pytest.approx(10 ** (-30 / 20), rel=1e-3)
    assert out[_beat_sample(8)] == 0.0  # snaps to exactly 0 at the keyframe


# ---------------------------------------------------------------------------
# Unsupported features hard-fail
# ---------------------------------------------------------------------------


def test_eq_lane_is_rejected(tmp_path) -> None:
    eq = {"lane": "eq", "deck": 1, "band": "low",
          "keyframes": [{"at": {"beat": 0}, "value_db": -6}]}
    with pytest.raises(NotImplementedError, match="M5"):
        _render(_plan(tmp_path, [_play(1, "one")], [eq]), tmp_path)


def test_stem_volume_lane_is_rejected(tmp_path) -> None:
    sv = {"lane": "stem_volume", "deck": 1, "stem": "vocals",
          "keyframes": [{"at": {"beat": 0}, "value": 0.0}]}
    with pytest.raises(NotImplementedError, match="M3"):
        _render(_plan(tmp_path, [_play(1, "one")], [sv]), tmp_path)


def test_vocal_handoff_is_rejected(tmp_path) -> None:
    plan = _plan(tmp_path, [_play(1, "one"), _play(2, "half"), _trans("vocal_handoff", 8, 4)])
    with pytest.raises(NotImplementedError, match="vocal_handoff"):
        _render(plan, tmp_path)
