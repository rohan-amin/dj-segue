"""Time conversion tests."""

from __future__ import annotations

import pytest

from dj_segue.schema.plan import (
    BarPos,
    BarsDur,
    BeatPos,
    BeatsDur,
    CuePos,
    Cue,
    SecondPos,
    SecondsDur,
    Track,
)
from dj_segue.time_math import (
    duration_to_seconds,
    mix_pos_to_seconds,
    track_pos_to_seconds,
)


def _track(bpm: float = 120.0, **cues: dict) -> Track:
    return Track.model_validate(
        {"path": "x.wav", "bpm": bpm, "cues": cues}
    )


def test_mix_pos_beat_at_120_bpm() -> None:
    # beat 60 at 120 bpm = 30 seconds
    assert mix_pos_to_seconds(BeatPos(beat=60), 120.0) == pytest.approx(30.0)


def test_mix_pos_bar_uses_4_beats_per_bar() -> None:
    assert mix_pos_to_seconds(BarPos(bar=2), 120.0) == pytest.approx(4.0)


def test_mix_pos_second_passes_through() -> None:
    assert mix_pos_to_seconds(SecondPos(second=12.5), 120.0) == 12.5


def test_mix_pos_cue_is_invalid() -> None:
    with pytest.raises(ValueError, match="Cue references"):
        mix_pos_to_seconds(CuePos(cue="x"), 120.0)


def test_track_pos_uses_track_bpm_not_mix() -> None:
    track = _track(bpm=90.0)
    # beat 90 at 90 bpm = 60 seconds; would be 45 at mix tempo 120 (irrelevant)
    assert track_pos_to_seconds(BeatPos(beat=90), track) == pytest.approx(60.0)


def test_track_pos_resolves_cue_via_registry() -> None:
    track = _track(bpm=120.0, drop={"beat": 16}, label_only={"second": 5.5})
    assert track_pos_to_seconds(CuePos(cue="drop"), track) == pytest.approx(8.0)
    assert track_pos_to_seconds(CuePos(cue="label_only"), track) == 5.5


def test_track_pos_unknown_cue_errors() -> None:
    track = _track(bpm=120.0, drop={"beat": 16})
    with pytest.raises(ValueError, match="not declared"):
        track_pos_to_seconds(CuePos(cue="ghost"), track)


def test_duration_beats() -> None:
    assert duration_to_seconds(BeatsDur(beats=8), 120.0) == pytest.approx(4.0)


def test_duration_bars() -> None:
    assert duration_to_seconds(BarsDur(bars=2), 120.0) == pytest.approx(4.0)


def test_duration_seconds() -> None:
    assert duration_to_seconds(SecondsDur(seconds=1.875), 120.0) == 1.875
