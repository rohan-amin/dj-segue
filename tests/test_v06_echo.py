"""Schema v0.6: echo_out transitions — schema, validation, windows, render."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from dj_segue.compiler import resolve_timeline
from dj_segue.executor.native import NativeEngine
from dj_segue.executor.native.engine import LOOP_SEAM_SEC
from dj_segue.schema.plan import Plan
from dj_segue.schema.validator import PlanValidationError, validate_plan
from dj_segue.time_math import echo_window, transition_windows

SR = 8000
BPM = 120.0
BEAT = SR // 2  # samples per beat
SEAM = int(round(LOOP_SEAM_SEC * SR))


def _echo(**kw) -> dict:
    seg = {
        "type": "transition", "style": "echo_out", "from_deck": 1, "to_deck": 2,
        "start_at": {"beat": 8}, "duration": {"beats": 4},
        "echo": {"every": {"beats": 1}, "feedback": 0.5},
    }
    seg.update(kw)
    return seg


def _plan(trans: dict | None = None, version: str = "0.6", d2_beats: float = 16,
          extra: list[dict] | None = None) -> Plan:
    return Plan.model_validate({
        "schema_version": version,
        "meta": {"mix_name": "echo", "mix_tempo": BPM},
        "tracks": {
            "ramp": {"path": "ramp.wav", "bpm": BPM},
            "half": {"path": "half.wav", "bpm": BPM},
        },
        "decks": {"1": {}, "2": {}},
        "timeline": [
            {"type": "play", "deck": 1, "track": "ramp",
             "from": {"beat": 0}, "to": {"beat": 8}, "start_at": {"beat": 0}},
            {"type": "play", "deck": 2, "track": "half",
             "from": {"beat": 0}, "to": {"beat": d2_beats}, "start_at": {"beat": 8}},
            trans or _echo(),
            *(extra or []),
        ],
    })


def _write(root: Path) -> None:
    n = 40 * BEAT
    ramp = (np.arange(n, dtype=np.float32) + 1) / n  # distinct value per sample
    sf.write(root / "ramp.wav", ramp, SR, subtype="FLOAT")
    sf.write(root / "half.wav", np.full(n, 0.5, dtype=np.float32), SR, subtype="FLOAT")


def _render(plan: Plan, root: Path) -> np.ndarray:
    _write(root)
    return NativeEngine(limited=False).render(plan, root).samples[:, 0]


# --- schema / validation ----------------------------------------------------


def test_echo_out_needs_echo_and_only_it_takes_it() -> None:
    with pytest.raises(ValidationError, match="need `echo`"):
        _plan(_echo(echo=None))
    with pytest.raises(ValidationError, match="need `echo`"):
        _plan({"type": "transition", "style": "crossfade", "from_deck": 1, "to_deck": 2,
               "start_at": {"beat": 8}, "duration": {"beats": 4},
               "echo": {"every": {"beats": 1}, "feedback": 0.5}})


def test_echo_out_rejects_out_and_curve_but_takes_in() -> None:
    with pytest.raises(ValidationError, match="don't apply to echo_out"):
        _plan(_echo(out={"duration": {"beats": 2}}))
    with pytest.raises(ValidationError, match="don't apply to echo_out"):
        _plan(_echo(curve="linear"))
    _plan(_echo(**{"in": {"duration": {"beats": 2}}}))


def test_echo_out_requires_v06() -> None:
    with pytest.raises(ValidationError, match="requires schema_version 0.6"):
        _plan(version="0.5")


def test_feedback_range() -> None:
    with pytest.raises(ValidationError):
        _plan(_echo(echo={"every": {"beats": 1}, "feedback": 1.5}))


def test_every_must_fit_in_tail() -> None:
    with pytest.raises(PlanValidationError, match="longer than the tail"):
        validate_plan(_plan(_echo(echo={"every": {"beats": 8}, "feedback": 0.5})))
    with pytest.raises(PlanValidationError, match="must be positive"):
        validate_plan(_plan(_echo(duration={"beats": 0})))
    validate_plan(_plan())


# --- windows / anchors --------------------------------------------------------


def test_windows_cut_dry_and_bring_in_on_the_cut() -> None:
    seg = _plan().timeline[2]
    out_w, in_w = transition_windows(seg, BPM)
    assert (out_w.start_sec, out_w.end_sec, out_w.curve) == (4.0, 4.0, "step")
    assert (in_w.start_sec, in_w.end_sec, in_w.curve) == (4.0, 4.0, "step")
    w = echo_window(seg, BPM)
    assert (w.cut_sec, w.every_sec, w.end_sec, w.feedback) == (4.0, 0.5, 6.0, 0.5)


def test_in_override_fades_the_incoming_deck() -> None:
    seg = _plan(_echo(**{"in": {"offset": {"beats": -2}, "duration": {"beats": 2}}})).timeline[2]
    _, in_w = transition_windows(seg, BPM)
    assert (in_w.start_sec, in_w.end_sec, in_w.curve) == (3.0, 4.0, "equal_power")


def test_after_anchor_is_the_tail_end() -> None:
    plan = _plan(_echo(id="echo"))
    assert resolve_timeline(plan, BPM).anchors["echo"] == pytest.approx(6.0)


# --- render -------------------------------------------------------------------


def test_repeats_land_on_the_grid_and_decay(tmp_path) -> None:
    out = _render(_plan(), tmp_path)
    x = lambda i: (i + 1) / (40 * BEAT)  # noqa: E731 — the ramp track's value
    cut, end = 8 * BEAT, 12 * BEAT
    half = 0.5  # deck 2 comes in on the cut

    # Dry before the seam; the echo's first repeat crossfades in over it.
    assert out[cut - SEAM - 1] == pytest.approx(x(cut - SEAM - 1))
    # Each repeat k replays the beat before the cut at feedback**k.
    for k in range(3):  # the 4th repeat sits under the final fade
        for m in (SEAM, BEAT // 2, BEAT - SEAM - 1):
            n = cut + k * BEAT + m
            assert out[n] == pytest.approx(half + 0.5**k * x(cut - BEAT + m), abs=1e-6)
    # Faded to silence by the tail's end; only deck 2 after.
    assert np.allclose(out[end:16 * BEAT], half)


def test_seams_are_smooth(tmp_path) -> None:
    out = _render(_plan(), tmp_path)
    cut = 8 * BEAT
    # The ramp track rises by 1/(40·BEAT) per sample; with deck 2's hard
    # entry (0.5 from the cut) taken out, jumps at seams stay tiny.
    echo = out.copy()
    echo[cut:] -= 0.5
    region = echo[cut - BEAT : 12 * BEAT]
    assert np.abs(np.diff(region)).max() < 0.01


def test_tail_extends_the_mix_past_the_last_play(tmp_path) -> None:
    out = _render(_plan(d2_beats=2), tmp_path)  # deck 2 ends at beat 10
    assert len(out) == 12 * BEAT


def test_excerpt_equals_full_render(tmp_path) -> None:
    _write(tmp_path)
    plan = _plan()
    eng = NativeEngine()
    parts = eng.render_decks(plan, tmp_path)
    gains = eng.deck_gains(plan, parts)
    tails = eng.echo_tails(plan, parts, gains)
    full = eng.render(plan, tmp_path).samples
    for a, b in [(0, 100), (8 * BEAT - 50, 9 * BEAT), (10 * BEAT, 12 * BEAT + 7)]:
        np.testing.assert_array_equal(parts.mix(gains, a, b, tails=tails), full[a:b])
