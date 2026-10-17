"""Plan templates: a finder candidate → a loop-and-jump plan that validates,
loops A's word, and brings B in at its word or its line's start."""

from __future__ import annotations

import pytest

pytest.importorskip("pronouncing")

from dj_segue.finder import Track, find  # noqa: E402
from dj_segue.schema import load_plan, validate_plan  # noqa: E402
from dj_segue.schema.jsonc import loads  # noqa: E402
from dj_segue.templates import dump, loop_and_jump, loop_start, schema_nth, track_id  # noqa: E402
from dj_segue.words.merge import Word  # noqa: E402

BPM = 120.0  # 0.5 s a beat


def L(text, beat, line, dur=0.3):
    t = beat * 0.5
    return Word(text, t, t + dur * 0.5, 0.9, "lyrics", True, line)


def track(name, words, sections, bpm=BPM):
    counts = {}
    for w in words:
        counts[w.text.lower()] = counts.get(w.text.lower(), 0) + 1
    return Track(name=name, title_words=set(), bpm=bpm, anchor=0.0, key=None, camelot=None, duration=300.0,
                 sections=list(sections), words=words, counts=counts)


A_WORDS = [L("we", 140, 0), L("rocket", 141, 0), L("high", 141.6, 0)]  # "rocket" mid-line, next word ½ beat on
A_SECTIONS = ((0, 8, "intro"), (8, 24, "verse"), (24, 36, "hook"), (36, 40, "outro"))  # "rocket": bar 35


def _plan(tmp_path, a, b, c):
    text = loop_and_jump(a, b, tmp_path / "a.mp3", tmp_path / "b.mp3", c)
    path = tmp_path / "t.plan.jsonc"
    path.write_text(text)
    plan = load_plan(path)
    validate_plan(plan)
    return text, loads(text)


def test_loops_start_on_the_beat_of_the_word() -> None:
    assert loop_start(141.3) == 141.0
    assert loop_start(140.9) == 141.0  # sung a hair early: the beat it's ahead of


def test_track_ids_and_nth() -> None:
    a = track("01 - Let's Get It Started (Spike Mix) [Explicit].mp3", A_WORDS, A_SECTIONS)
    assert track_id(a) == "lgis"
    b = track("b", [L("go", 1, 0), L("go", 2, 0), L("rocket", 3, 0), L("go", 4, 0)], A_SECTIONS)
    assert schema_nth(b, 3, 1) == 3 and schema_nth(b, 2, 1) == 1


def test_jump_to_the_line_start_keeps_b_in_step(tmp_path) -> None:
    """B's word 2 beats into a line late in the track: B's bed is its first
    K beats with K ≡ the entry (mod 4), then B jumps to the line's start."""
    a = track("a", A_WORDS, A_SECTIONS)
    b = track("b", [L("ride", 93, 5), L("the", 94, 5), L("rocket", 95, 5), L("up", 96, 5)],
              ((0, 4, "intro"), (4, 24, "verse"), (24, 40, "hook"), (40, 48, "outro")))
    c = find(a, b)[0]
    assert c.b.lead == pytest.approx(2.0) and "pickup into its hook" in c.why
    text, plan = _plan(tmp_path, a, b, c)
    segs = {s["id"]: s for s in plan["timeline"] if "id" in s}
    assert segs["a"]["to"] == {"beat": 141} == segs["a_loop"]["from"]
    assert segs["a"]["from"] == {"bar": 24}  # the last section start 8+ bars before the word
    assert segs["a_loop"]["schedule"] == [{"length": {"beats": 4}, "repetitions": 4}]  # a bar, not a stutter
    bed = segs["b_bed"]["to"]["beat"]
    assert bed == pytest.approx(12.9) and bed % 4 == pytest.approx((93 - 0.1) % 4)
    assert segs["b_main"]["from"] == {"word": "ride", "nth": 1, "offset": {"beats": -0.1}}
    assert segs["b_main"]["to"] == {"bar": 40}  # through the hook
    assert "tempo_map" not in plan["meta"] and plan["meta"]["mix_tempo"] == 120
    vocals = plan["automation"][0]
    assert vocals["stem"] == "vocals" and [k["value"] for k in vocals["keyframes"]] == [0, 1]
    assert "Lyric" not in text and "ride the rocket" not in text  # single words only


def test_straight_in_and_a_tempo_ramp(tmp_path) -> None:
    """B's word 12 beats in: B plays from the top to it, no jump; a 20%
    tempo gap ramps over the loop."""
    a = track("a", A_WORDS, A_SECTIONS)
    b = track("b", [L("rocket", 12, 0)], ((0, 3, "intro"), (3, 12, "hook")), bpm=100.0)
    c = find(a, b)[0]
    _, plan = _plan(tmp_path, a, b, c)
    ids = [s.get("id") for s in plan["timeline"]]
    assert "b_bed" not in ids and "b" in ids
    assert plan["meta"]["mix_tempo"] == 120
    assert plan["meta"]["tempo_map"][0]["bpm"] == 100


def test_dump_keeps_short_things_on_one_line() -> None:
    out = dump({"at": {"beat": 4}, "value": 1})
    assert out == '{ "at": { "beat": 4 }, "value": 1 }'
    assert dump({"k": "x" * 200}).startswith("{\n")


def test_spots_set_the_loop_and_the_jump(tmp_path) -> None:
    from dj_segue.annotate import Cue
    from dj_segue.finder import with_spots

    spot_a = Cue("lift off", 141 * 0.5, "", "out", "rocket", 8.0)
    spot_b = Cue("rocket in", 95 * 0.5, "", "in", "rocket")
    a = track("a", with_spots(A_WORDS, [spot_a], BPM), A_SECTIONS)
    a.spots = [spot_a]
    bw = [L("ride", 93, 5), L("the", 94, 5), L("rocket", 95, 5)]
    b = track("b", with_spots(bw, [spot_b], BPM), ((0, 4, "intro"), (4, 24, "verse"), (24, 40, "hook")))
    b.spots = [spot_b]
    c = find(a, b, spots_only=True)[0]
    _, plan = _plan(tmp_path, a, b, c)
    segs = {s["id"]: s for s in plan["timeline"] if "id" in s}
    assert segs["a"]["to"] == {"cue": "lift off"} == segs["a_loop"]["from"]
    assert segs["a_loop"]["schedule"][0]["length"] == {"beats": 8}
    assert segs["b_main"]["from"] == {"cue": "rocket in"}  # your spot, not the line's start
