"""M3 stems (schema v0.8): stem volume, vocal handoff, per-stem EQ, the
separator's cache, and stem decks inside the rest of the chain."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from dj_segue.compiler import VOCAL_SWAP_SEC
from dj_segue.executor.native import NativeEngine
from dj_segue.schema.plan import Plan
from dj_segue.schema.validator import PlanValidationError, validate_plan

SR = 8000
BPM = 120.0
BEAT = SR // 2
STEMS = ("vocals", "drums", "bass", "other")
# Constant stems whose sums identify every combination of gains.
A = {"vocals": 0.1, "drums": 0.02, "bass": 0.003, "other": 0.0004}
B = {"vocals": 0.5, "drums": 0.06, "bass": 0.007, "other": 0.0008}


def _write_dc(root: Path, name: str, values: dict[str, float], seconds: float = 20) -> dict:
    out = {}
    for stem, v in values.items():
        path = f"{name}_{stem}.wav"
        sf.write(root / path, np.full(int(seconds * SR), v, dtype=np.float32), SR, subtype="FLOAT")
        out[stem] = path
    return out


def _play(deck: int, track: str, start: float = 0, length: float = 32) -> dict:
    return {"type": "play", "deck": deck, "track": track,
            "from": {"beat": 0}, "to": {"beat": length}, "start_at": {"beat": start}}


def _plan(root: Path, timeline: list[dict], automation: list[dict] | None = None,
          tracks: dict | None = None, version: str = "0.8") -> Plan:
    if tracks is None:
        tracks = {"a": {"stems": _write_dc(root, "a", A), "bpm": BPM},
                  "b": {"stems": _write_dc(root, "b", B), "bpm": BPM}}
    return Plan.model_validate({
        "schema_version": version,
        "meta": {"mix_name": "stems", "mix_tempo": BPM},
        "tracks": tracks,
        "decks": {"1": {}, "2": {}},
        "timeline": timeline,
        "automation": automation or [],
    })


def _render(plan: Plan, root: Path) -> np.ndarray:
    return NativeEngine(limited=False).render(plan, root).samples[:, 0]


def _handoff(**kw) -> dict:
    seg = {"type": "transition", "style": "vocal_handoff", "from_deck": 1, "to_deck": 2,
           "start_at": {"beat": 8}, "duration": {"beats": 8}}
    seg.update(kw)
    return seg


# --- stem volume -----------------------------------------------------------------


def test_stem_volume_mutes_one_stem(tmp_path) -> None:
    lane = {"lane": "stem_volume", "deck": 1, "stem": "vocals",
            "keyframes": [{"at": {"beat": 4}, "value": 1}, {"at": {"beat": 8}, "value": 0}]}
    out = _render(_plan(tmp_path, [_play(1, "a")], [lane]), tmp_path)
    full, inst = sum(A.values()), sum(A.values()) - A["vocals"]
    assert out[2 * BEAT] == pytest.approx(full, abs=1e-6)
    assert out[6 * BEAT] == pytest.approx(inst + 0.5 * A["vocals"], abs=1e-6)
    assert out[10 * BEAT] == pytest.approx(inst, abs=1e-6)


# --- vocal handoff ---------------------------------------------------------------


def test_vocal_handoff(tmp_path) -> None:
    out = _render(_plan(tmp_path, [_play(1, "a"), _play(2, "b"), _handoff()]), tmp_path)
    inst = lambda d: sum(v for k, v in d.items() if k != "vocals")  # noqa: E731
    swap = 12 * BEAT  # halfway through beats 8–16
    fade = int(round(VOCAL_SWAP_SEC * SR))
    eq = np.sqrt(0.5)  # equal power at the window's middle

    assert out[6 * BEAT] == pytest.approx(sum(A.values()), abs=1e-6)  # before: A only
    # Just before the swap: A's vocals, both instrumentals crossfading.
    n = swap - fade - 1
    t = (n - 8 * BEAT + 0.0) / (8 * BEAT)
    assert out[n] == pytest.approx(
        A["vocals"] + inst(A) * np.cos(t * np.pi / 2) + inst(B) * np.sin(t * np.pi / 2), abs=1e-4)
    # On the swap: B's vocals, instrumentals at the middle of the fade.
    assert out[swap] == pytest.approx(B["vocals"] + eq * (inst(A) + inst(B)), abs=1e-4)
    # After the window: B only.
    assert out[17 * BEAT] == pytest.approx(sum(B.values()), abs=1e-6)


def test_swap_at_moves_the_vocal_switch(tmp_path) -> None:
    out = _render(_plan(tmp_path, [_play(1, "a"), _play(2, "b"),
                                   _handoff(swap_at={"offset": {"beats": 2}})]), tmp_path)
    # Beat 11: past the swap (beat 10) — B's vocals, not A's.
    t = 3 / 8
    inst = lambda d: sum(v for k, v in d.items() if k != "vocals")  # noqa: E731
    assert out[11 * BEAT] == pytest.approx(
        B["vocals"] + inst(A) * np.cos(t * np.pi / 2) + inst(B) * np.sin(t * np.pi / 2), abs=1e-4)


def test_handoff_curve_shapes_the_instrumentals(tmp_path) -> None:
    out = _render(_plan(tmp_path, [_play(1, "a"), _play(2, "b"),
                                   _handoff(curve="linear")]), tmp_path)
    inst = lambda d: sum(v for k, v in d.items() if k != "vocals")  # noqa: E731
    assert out[14 * BEAT] == pytest.approx(
        B["vocals"] + 0.25 * inst(A) + 0.75 * inst(B), abs=1e-4)


def test_vocals_cross_without_a_click(tmp_path) -> None:
    out = _render(_plan(tmp_path, [_play(1, "a"), _play(2, "b"), _handoff()]), tmp_path)
    swap = 12 * BEAT
    seg = out[swap - 200 : swap + 200]
    # The vocal change (0.4) spreads over the swap fade, not one sample.
    assert np.abs(np.diff(seg)).max() < 0.4 / (VOCAL_SWAP_SEC * SR) * 1.5


# --- per-stem EQ -------------------------------------------------------------------


def _write_sines(root: Path) -> dict:
    t = np.arange(20 * SR) / SR
    sines = {"vocals": 1000.0, "drums": 60.0, "bass": 60.0, "other": 3000.0}
    out = {}
    for stem, hz in sines.items():
        amp = 0.2 if stem in ("vocals", "drums") else 0.0
        sf.write(root / f"s_{stem}.wav", (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32),
                 SR, subtype="FLOAT")
        out[stem] = f"s_{stem}.wav"
    return out


def _amp(x: np.ndarray, hz: float, start: int, n: int = SR // 2) -> float:
    seg = x[start : start + n]
    t = np.arange(start, start + n) / SR
    return 2 / n * abs(np.sum(seg * np.exp(-2j * np.pi * hz * t)))


def test_stem_eq_touches_only_its_stem(tmp_path) -> None:
    tracks = {"s": {"stems": _write_sines(tmp_path), "bpm": BPM}}
    drums_low = {"lane": "eq", "deck": 1, "band": "low", "stem": "drums",
                 "keyframes": [{"at": {"beat": 0}, "value_db": -24}]}
    vocals_low = {"lane": "eq", "deck": 1, "band": "low", "stem": "vocals",
                  "keyframes": [{"at": {"beat": 0}, "value_db": -24}]}
    out = _render(_plan(tmp_path, [_play(1, "s")], [drums_low], tracks), tmp_path)
    assert 20 * np.log10(_amp(out, 60, 4 * BEAT) / 0.2) == pytest.approx(-24, abs=1)
    assert 20 * np.log10(_amp(out, 1000, 4 * BEAT) / 0.2) == pytest.approx(0, abs=0.1)
    out = _render(_plan(tmp_path, [_play(1, "s")], [vocals_low], tracks), tmp_path)
    assert 20 * np.log10(_amp(out, 60, 4 * BEAT) / 0.2) == pytest.approx(0, abs=0.1)


def test_deck_eq_and_filter_apply_after_the_stems(tmp_path) -> None:
    tracks = {"s": {"stems": _write_sines(tmp_path), "bpm": BPM}}
    lanes = [
        {"lane": "stem_volume", "deck": 1, "stem": "vocals",
         "keyframes": [{"at": {"beat": 0}, "value": 0.5}]},
        {"lane": "eq", "deck": 1, "band": "low",
         "keyframes": [{"at": {"beat": 0}, "value_db": -24}]},
    ]
    out = _render(_plan(tmp_path, [_play(1, "s")], lanes, tracks), tmp_path)
    assert 20 * np.log10(_amp(out, 60, 4 * BEAT) / 0.2) == pytest.approx(-24, abs=1)
    assert 20 * np.log10(_amp(out, 1000, 4 * BEAT) / 0.2) == pytest.approx(-6, abs=0.1)


def test_excerpt_equals_full_render(tmp_path) -> None:
    tracks = {"s": {"stems": _write_sines(tmp_path), "bpm": BPM},
              "b": {"stems": _write_dc(tmp_path, "b", B), "bpm": BPM}}
    plan = _plan(tmp_path, [_play(1, "s"), _play(2, "b"), _handoff()], [
        {"lane": "eq", "deck": 1, "band": "high", "stem": "vocals",
         "keyframes": [{"at": {"beat": 2}, "value_db": -6}]},
        {"lane": "eq", "deck": 1, "band": "low",
         "keyframes": [{"at": {"beat": 4}, "value_db": 0}, {"at": {"beat": 8}, "value_db": -24}]},
        {"lane": "filter", "deck": 2,
         "keyframes": [{"at": {"beat": 8}, "value": -0.5}, {"at": {"beat": 16}, "value": 0}]},
    ], tracks)
    eng = NativeEngine()
    parts = eng.render_decks(plan, tmp_path)
    levels = eng.levels(plan, parts)
    full = eng.render(plan, tmp_path).samples
    for a, b in [(0, 999), (11 * BEAT, 13 * BEAT), (15 * BEAT, 20 * BEAT)]:
        np.testing.assert_array_equal(parts.mix(levels, a, b), full[a:b])


def test_decks_without_stem_features_are_unchanged(tmp_path) -> None:
    """Deck 2 isn't mixed by stems: adding a stem lane on deck 1 doesn't
    change a sample of it."""
    t = np.arange(20 * SR) / SR
    sf.write(tmp_path / "x.wav", (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), SR,
             subtype="FLOAT")
    tracks = {"a": {"stems": _write_dc(tmp_path, "a", A), "bpm": BPM},
              "x": {"path": "x.wav", "bpm": BPM}}
    timeline = [_play(1, "a"), _play(2, "x")]
    lane = {"lane": "stem_volume", "deck": 1, "stem": "vocals",
            "keyframes": [{"at": {"beat": 0}, "value": 0}]}
    eng = NativeEngine(limited=False)
    with_stems = eng.render_decks(_plan(tmp_path, timeline, [lane], tracks), tmp_path)
    without = eng.render_decks(_plan(tmp_path, timeline, [], tracks), tmp_path)
    assert np.array_equal(with_stems.decks[2], without.decks[2])
    assert 1 in with_stems.stems and 2 not in with_stems.stems


# --- path tracks: the separator and its cache ---------------------------------------


def test_path_tracks_are_separated_and_cached(tmp_path, monkeypatch) -> None:
    import dj_segue.stems.separator as sep

    calls = []

    def fake(audio, sr):  # a deterministic stand-in for demucs
        calls.append(len(audio))
        return {"vocals": audio * 0.5, "drums": audio * 0.25, "bass": audio * 0.0,
                "other": audio * 0.25}

    monkeypatch.setattr(sep, "_separate", fake)
    monkeypatch.setenv(sep.CACHE_ENV, str(tmp_path / "cache"))
    sf.write(tmp_path / "x.wav", np.full(20 * SR, 0.4, dtype=np.float32), SR, subtype="FLOAT")
    tracks = {"x": {"path": "x.wav", "bpm": BPM}}
    lane = {"lane": "stem_volume", "deck": 1, "stem": "vocals",
            "keyframes": [{"at": {"beat": 0}, "value": 0}]}
    plan = _plan(tmp_path, [_play(1, "x")], [lane], tracks)
    validate_plan(plan)
    out = _render(plan, tmp_path)
    assert out[4 * BEAT] == pytest.approx(0.2, abs=1e-6)  # vocals (half) muted
    assert len(calls) == 1
    _render(plan, tmp_path)
    assert len(calls) == 1  # second render: from the cache


# --- validation ----------------------------------------------------------------------


def test_swap_must_be_inside_the_window(tmp_path) -> None:
    with pytest.raises(PlanValidationError, match="swap_at must be within"):
        validate_plan(_plan(tmp_path, [_play(1, "a"), _play(2, "b"),
                                       _handoff(swap_at={"offset": {"beats": 9}})]))


def test_v08_gates(tmp_path) -> None:
    with pytest.raises(ValidationError, match="swap_at requires schema_version 0.8"):
        _plan(tmp_path, [_play(1, "a"), _play(2, "b"),
                         _handoff(swap_at={"offset": {"beats": 2}})], version="0.7")
    with pytest.raises(ValidationError, match="`stem` requires schema_version 0.8"):
        _plan(tmp_path, [_play(1, "a")], [
            {"lane": "eq", "deck": 1, "band": "low", "stem": "drums",
             "keyframes": [{"at": {"beat": 0}, "value_db": -6}]}], version="0.7")
    with pytest.raises(ValidationError, match="only vocal_handoff"):
        _plan(tmp_path, [_play(1, "a"), _play(2, "b"),
                         {**_handoff(style="crossfade"), "swap_at": {"offset": {"beats": 2}}}])


def test_stem_deck_tracks_need_the_same_stems(tmp_path) -> None:
    tracks = {"a": {"stems": _write_dc(tmp_path, "a", A), "bpm": BPM},
              "c": {"stems": _write_dc(tmp_path, "c", {"vocals": 0.1, "rest": 0.2}), "bpm": BPM}}
    timeline = [_play(1, "a", 0, 8), _play(1, "c", 8, 8), _play(2, "a", 0, 32)]
    lane = {"lane": "stem_volume", "deck": 1, "stem": "vocals",
            "keyframes": [{"at": {"beat": 0}, "value": 1}]}
    with pytest.raises(PlanValidationError, match="different stems"):
        validate_plan(_plan(tmp_path, timeline, [lane], tracks))
