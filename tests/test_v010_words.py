"""Schema v0.10: word positions — where a word is sung (from `dj-segue words`),
in track positions and, with `in`, in mix positions and a handoff's swap."""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from dj_segue.compiler import resolve_timeline
from dj_segue.inspect import format_plan
from dj_segue.schema.plan import Plan, WordPos, word_tracks
from dj_segue.schema.validator import PlanValidationError, validate_plan
from dj_segue.time_math import TempoMap, TrackGrid, handoff_swap, mix_pos_to_seconds, word_sec

SR = 22050
# (normalized word, start, end): "up" three times, "bottoms up" once.
WORDS = (("hey", 0.5, 0.7), ("up", 1.0, 1.2), ("we", 2.0, 2.2), ("go", 2.4, 2.6),
         ("up", 3.0, 3.3), ("bottoms", 4.0, 4.4), ("up", 4.5, 4.8))


def W(**kw) -> WordPos:
    return WordPos.model_validate(kw)


# ---------------------------------------------------------------------------
# finding a word
# ---------------------------------------------------------------------------


def test_nth_edge_and_phrases() -> None:
    g = TrackGrid(0.0, 120.0, words=WORDS)
    assert word_sec(W(word="up"), g) == 1.0
    assert word_sec(W(word="Up!", nth=2), g) == 3.0  # case and punctuation don't matter
    assert word_sec(W(word="up", nth=-1), g) == 4.5  # the last time
    assert word_sec(W(word="up", nth=2, edge="end"), g) == 3.3
    assert word_sec(W(word="bottoms up"), g) == 4.0  # a phrase: its words in a row
    assert word_sec(W(word="bottoms up", edge="end"), g) == 4.8


def test_missing_words_say_why() -> None:
    g = TrackGrid(0.0, 120.0, words=WORDS)
    with pytest.raises(ValueError, match="sung 3 times; nth 4 is out of range"):
        word_sec(W(word="up", nth=4), g)
    with pytest.raises(ValueError, match="sung 0 times"):
        word_sec(W(word="down"), g)
    with pytest.raises(ValueError, match="no word timing"):
        word_sec(W(word="up"), TrackGrid(0.0, 120.0))


def test_nth_zero_is_rejected() -> None:
    with pytest.raises(ValidationError, match="counts from 1"):
        W(word="up", nth=0)


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------


def _raw(timeline, automation=(), version="0.10", tempo_map=None) -> dict:
    meta = {"mix_name": "t", "mix_tempo": 120}
    if tempo_map:
        meta["tempo_map"] = tempo_map
    return {
        "schema_version": version, "meta": meta,
        "tracks": {"a": {"path": "a.wav", "bpm": 120}, "b": {"path": "b.wav", "bpm": 100}},
        "decks": {"1": {}, "2": {}},
        "timeline": list(timeline), "automation": list(automation),
    }


GRIDS = {"a": TrackGrid(0.0, 120.0), "b": TrackGrid(0.0, 100.0, words=WORDS)}


def test_word_positions_need_schema_010() -> None:
    tl = [{"type": "play", "deck": 2, "track": "b", "from": {"word": "up"}, "to": {"second": 5}}]
    with pytest.raises(ValidationError, match="requires schema_version 0.10"):
        Plan.model_validate(_raw(tl, version="0.9"))
    plan = Plan.model_validate(_raw(tl))
    assert word_tracks(plan) == {"b"}


def test_track_word_positions_resolve() -> None:
    plan = Plan.model_validate(_raw([
        {"type": "play", "deck": 2, "track": "b",
         "from": {"word": "up", "nth": 2}, "to": {"word": "bottoms up", "edge": "end"}},
    ]))
    validate_plan(plan, GRIDS)
    span = resolve_timeline(plan, 120.0, GRIDS).spans[0]
    assert span.track_from_sec == 3.0 and span.mix_end_sec == pytest.approx(1.8)


def test_mix_word_positions_follow_the_segment() -> None:
    """`{"word", "in": id}` is when that segment plays the word: through its
    start, its `from` and its stretch (b at 100 bpm played at the mix's 120)."""
    plan = Plan.model_validate(_raw([
        {"type": "play", "id": "bb", "deck": 2, "track": "b", "target_bpm": "mix",
         "from": {"second": 2.0}, "to": {"second": 5.0}, "start_at": {"beat": 8}},
        {"type": "play", "deck": 1, "track": "a", "from": {"beat": 0}, "to": {"beat": 8},
         "start_at": {"word": "up", "nth": 2, "in": "bb"}},
    ], automation=[{"lane": "deck_volume", "deck": 2,
                    "keyframes": [{"at": {"word": "bottoms", "in": "bb"}, "value": 1}]}]))
    validate_plan(plan, GRIDS)
    tl = resolve_timeline(plan, 120.0, GRIDS)
    # bb starts at mix 4.0 s with track 2.0 s, at 1.2× speed: "up" #2 (3.0 s) at 4 + 1/1.2.
    assert tl.spans[1].mix_start_sec == pytest.approx(4.0 + 1.0 / 1.2)
    at = plan.automation[0].keyframes[0].at
    assert mix_pos_to_seconds(at, 120.0, tl.anchors) == pytest.approx(4.0 + 2.0 / 1.2)


def test_mix_word_positions_follow_a_tempo_ramp() -> None:
    plan = Plan.model_validate(_raw([
        {"type": "play", "id": "bb", "deck": 2, "track": "b", "target_bpm": "mix",
         "from": {"second": 0.0}, "to": {"second": 5.0}, "start_at": {"beat": 0}},
    ], tempo_map=[{"at": {"beat": 0}, "bpm": 150, "ramp": {"beats": 8}}]))
    tm = TempoMap.fixed(120).with_change(0.0, 150, 120 * 8 / (120 + 150))
    tl = resolve_timeline(plan, tm, GRIDS)
    t = mix_pos_to_seconds(W(word="go", **{"in": "bb"}), tm, tl.anchors)
    # The warp maps track 2.4 s (track beat 4 at 100 bpm) to mix beat 4.
    assert tm.sec_to_beat(t) == pytest.approx(4.0, abs=1e-6)


def test_a_handoff_swaps_on_a_word() -> None:
    plan = Plan.model_validate(_raw([
        {"type": "play", "deck": 1, "track": "a", "from": {"beat": 0}, "to": {"beat": 16}},
        {"type": "play", "id": "bb", "deck": 2, "track": "b",
         "from": {"second": 0.0}, "to": {"second": 5.0}, "start_at": {"beat": 4}},
        {"type": "transition", "style": "vocal_handoff", "from_deck": 1, "to_deck": 2,
         "start_at": {"beat": 4}, "duration": {"beats": 8},
         "swap_at": {"word": "we", "in": "bb"}},
    ]))
    validate_plan(plan, GRIDS)
    tl = resolve_timeline(plan, 120.0, GRIDS)
    assert handoff_swap(plan.timeline[2], 120.0, tl.anchors) == pytest.approx(2.0 + 2.0)
    assert 'swap_at=word "we" in bb' in format_plan(plan)


def test_validation_explains_word_mistakes() -> None:
    def issues(timeline, **kw):
        with pytest.raises(PlanValidationError) as e:
            validate_plan(Plan.model_validate(_raw(timeline, **kw)), GRIDS)
        return "\n".join(e.value.issues)

    bb = {"type": "play", "id": "bb", "deck": 2, "track": "b",
          "from": {"second": 0.0}, "to": {"second": 5.0}}
    assert '"in" is for mix positions' in issues([
        {**bb, "from": {"word": "up", "in": "bb"}}])
    assert 'needs "in"' in issues([bb, {"type": "play", "deck": 1, "track": "a",
                                        "from": {"beat": 0}, "to": {"beat": 4},
                                        "start_at": {"word": "up"}}])
    assert "must name a play segment" in issues([bb, {
        "type": "play", "deck": 1, "track": "a", "from": {"beat": 0}, "to": {"beat": 4},
        "start_at": {"word": "up", "in": "nope"}}])
    assert "nth 9 is out of range" in issues([{**bb, "from": {"word": "up", "nth": 9}}])
    assert "outside the part segment" in issues([
        {**bb, "to": {"second": 2.0}},
        {"type": "play", "deck": 1, "track": "a", "from": {"beat": 0}, "to": {"beat": 4},
         "start_at": {"word": "bottoms", "in": "bb"}}])
    assert "can't be at a word" in issues(
        [bb], tempo_map=[{"at": {"word": "up", "in": "bb"}, "bpm": 100}])


# ---------------------------------------------------------------------------
# end to end: the preprocessor supplies the words, the engine plays from one
# ---------------------------------------------------------------------------


def test_render_starts_on_the_word(tmp_path, monkeypatch) -> None:
    from dj_segue.executor.native import NativeEngine
    from dj_segue.preprocessor import pipeline

    rng = np.random.default_rng(5)
    x = (0.2 * rng.standard_normal((6 * SR, 2))).astype(np.float32)
    sf.write(tmp_path / "b.wav", x, SR, subtype="FLOAT")
    monkeypatch.setattr(pipeline, "load_words", lambda plan, tid, root: WORDS)
    raw = _raw([{"type": "play", "deck": 2, "track": "b",
                 "from": {"word": "go"}, "to": {"word": "go", "edge": "end"}}])
    raw["tracks"] = {"b": {"path": "b.wav", "bpm": 100}}
    plan = Plan.model_validate(raw)
    grids = pipeline.preprocess(plan, tmp_path).grids()  # as `play` and `tune` do
    assert grids["b"].words == WORDS
    out = NativeEngine().render(plan, tmp_path, grids).samples
    a, n = int(round(2.4 * SR)), int(round(0.2 * SR))
    assert len(out) == pytest.approx(n, abs=2)
    np.testing.assert_allclose(out[: n - 2], x[a : a + n - 2], atol=1e-5)


def test_offsets_move_a_word_position() -> None:
    plan = Plan.model_validate(_raw([
        {"type": "play", "id": "bb", "deck": 2, "track": "b",
         "from": {"word": "go", "offset": {"beats": -2}}, "to": {"second": 5.0}},
        {"type": "play", "deck": 1, "track": "a", "from": {"beat": 0}, "to": {"beat": 4},
         "start_at": {"word": "bottoms", "in": "bb", "offset": {"beats": -4}}},
    ]))
    validate_plan(plan, GRIDS)
    tl = resolve_timeline(plan, 120.0, GRIDS)
    assert tl.spans[0].track_from_sec == pytest.approx(2.4 - 2 * 60 / 100)  # track beats at 100
    # "bottoms" (4.0 s) plays at 4.0 − 1.2 = 2.8 s in the mix; 4 mix beats at 120 earlier.
    assert tl.spans[1].mix_start_sec == pytest.approx(2.8 - 2.0)
    assert 'word "go" -2 beats' in format_plan(plan)


def test_a_word_swap_validates_before_preprocessing() -> None:
    """Without grids (no words yet) the swap check waits for preprocessing."""
    plan = Plan.model_validate(_raw([
        {"type": "play", "deck": 1, "track": "a", "from": {"beat": 0}, "to": {"beat": 16}},
        {"type": "play", "id": "bb", "deck": 2, "track": "b",
         "from": {"second": 0.0}, "to": {"second": 5.0}, "start_at": {"beat": 4}},
        {"type": "transition", "style": "vocal_handoff", "from_deck": 1, "to_deck": 2,
         "start_at": {"beat": 4}, "duration": {"beats": 8}, "swap_at": {"word": "we", "in": "bb"}},
    ]))
    validate_plan(plan)
