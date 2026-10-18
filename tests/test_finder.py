"""The wordplay finder: matches (same word, phrase, sound-alike, rhyme),
ranking, cues as verified positions, and the suggested technique."""

from __future__ import annotations

import pytest

pytest.importorskip("pronouncing")

from dj_segue.finder import Track, find, key_relation, relation  # noqa: E402
from dj_segue.words.merge import Word  # noqa: E402

BPM = 120.0  # 0.5 s a beat, 2 s a bar


def W(text, beat, confirmed=True, score=0.9):
    t = beat * 0.5
    return Word(text, t, t + 0.3, score, "lyrics", confirmed, 0)


def pieces(sections):
    """Each section as 2-bar pieces of the same label: every bar is then a
    section's first or last, so the boundary rule doesn't get in the way."""
    return [(b, min(b + 2, e), lab) for s, e, lab in sections for b in range(s, e, 2)]


def track(name, words, sections=((0, 8, "verse"), (8, 16, "hook")), title="", cues=None, whole=False, spots=()):
    from dj_segue.finder import with_spots

    sections = list(sections) if whole else pieces(sections)
    words = with_spots(words, list(spots), BPM)
    counts = {}
    for w in words:
        counts[w.text.lower()] = counts.get(w.text.lower(), 0) + 1
    return Track(name=name, title_words=set(title.split()), bpm=BPM, anchor=0.0, key=None, camelot=None,
                 duration=64.0, sections=sections, words=words, counts=counts, cues=cues or {},
                 spots=list(spots))


def test_relations() -> None:
    assert relation("everybody", "everybody") == "same"
    assert relation("know", "no") == "sound-alike"
    assert relation("body", "everybody") is None  # one is the end of the other: not wordplay
    assert relation("fire", "desire") == "rhyme"
    assert relation("up", "cup") is None  # a one-phone rhyme is too loose
    assert relation("feet", "beat") == "rhyme"


def test_shared_title_word_in_a_hook_ranks_first() -> None:
    a = track("a", [W("go", 4), W("everybody", 37), W("tonight", 44), W("dance", 50)])
    b = track("b", [W("dance", 6), W("tonight", 10), W("everybody", 33)], title="everybody")
    cands = find(a, b)
    assert cands[0].a_text == "everybody" and cands[0].kind == "same"
    assert {c.a_text for c in cands} >= {"tonight", "dance"}
    assert all(c.a_text != "go" for c in cands)  # filler/common words alone don't match themselves


def test_common_words_count_less() -> None:
    a = track("a", [W("like", 36), W("rocket", 40)])
    b = track("b", [W("like", 36), W("rocket", 40)])
    by = {c.a_text: c for c in find(a, b)}
    assert by["rocket"].parts["match"] > by["like"].parts["match"]


def test_a_cue_named_after_the_word_wins() -> None:
    """The weakly aligned, unconfirmed occurrence near a cue you marked is
    taken at the cue, as verified."""
    a = track("a", [W("everybody", 39)])
    b = track("b", [W("everybody", 9), W("everybody", 30.6, confirmed=False, score=0.1)],
              sections=((0, 8, "verse"), (8, 16, "hook")), cues={"everybody": 31 * 0.5})
    c = find(a, b)[0]
    assert c.b.cue == "everybody" and c.b.beat == pytest.approx(31.0) and c.b.nth == 2
    assert "loop and jump" in c.technique  # beat 4 of bar 7, before the hook at bar 8


def test_handoff_when_the_word_lands_on_the_same_beat() -> None:
    a = track("a", [W("rocket", 37.0)])
    b = track("b", [W("rocket", 21.1)], sections=((0, 16, "verse"),))
    c = find(a, b)[0]
    assert c.technique.startswith("vocal handoff") and c.parts["fit"] > 0.9
    a2 = track("a", [W("rocket", 36.0)])
    assert find(a2, b)[0].technique.startswith("echo out")  # a beat apart, no hook to jump to


def test_phrases_are_found_and_beat_their_words() -> None:
    a = track("a", [W("bottoms", 36), W("up", 36.5), W("tonight", 44)])
    b = track("b", [W("bottoms", 20), W("up", 20.5)])
    cands = find(a, b)
    assert cands[0].kind == "phrase" and cands[0].a_text == "bottoms up"


def test_key_relations() -> None:
    assert key_relation("8A", "8A") == "same key"
    assert key_relation("8A", "8B").startswith("relative")
    assert key_relation("8A", "9A").startswith("a fifth")
    assert key_relation("12A", "1A").startswith("a fifth")
    assert key_relation("6A", "12A").startswith("not compatible")


def L(text, beat, line):
    """A lyric word on a given line, sung right after the one before it."""
    t = beat * 0.5
    return Word(text, t, t + 0.45, 0.9, "lyrics", True, line)


def test_never_in_the_middle_of_vocals() -> None:
    """B's word deep in its line fits no technique and is left out; A's word
    mid-line can only be looped; ends and starts of lines allow the rest."""
    mid = [L("they", 15, 0), L("fly", 16, 0), L("past", 17, 0), L("stars", 18, 0), L("then", 19, 0),
           L("ride", 20, 0), L("rocket", 21, 0), L("high", 22, 0)]  # "rocket" 6 beats into its line
    start = [L("rocket", 21, 1), L("ship", 22, 1)]
    end = [L("we", 36, 0), L("rocket", 37, 0)]
    a_mid = track("a", [L("we", 36, 0), L("rocket", 37, 0), L("high", 38, 0)])
    assert find(track("a", end), track("b", mid)) == []
    c = find(a_mid, track("b", start))[0]
    assert (c.a.edge, c.b.edge) == ("mid", "start") and c.technique == "loop and jump"
    c = find(track("a", end), track("b", start))[0]
    assert (c.a.edge, c.b.edge) == ("end", "start")
    assert c.technique == "vocal handoff or echo out or loop and jump"


def test_a_gap_or_a_line_break_ends_a_line() -> None:
    from dj_segue.finder import line_edge

    t = track("t", [L("one", 0, 0), L("two", 1, 0), L("three", 4, 0), L("four", 5, 1)])
    assert [line_edge(t, i, i) for i in range(4)] == ["start", "end", "alone", "alone"]
    assert line_edge(t, 0, 1) == "alone"  # a phrase: its first word starts, its last ends


def test_b_comes_in_at_its_line_start_when_the_word_follows_closely() -> None:
    a = track("a", [L("we", 36, 0), L("rocket", 37, 0), L("high", 38, 0)])
    b = track("b", [L("ride", 19, 0), L("the", 20, 0), L("rocket", 21, 0), L("ship", 22, 0)])
    c = find(a, b)[0]
    assert c.b.edge == "mid" and c.b.lead == pytest.approx(2.0)
    assert c.technique == "loop and jump" and "start of that line" in c.why


def test_b_leading_into_a_hook_ranks_first() -> None:
    a = track("a", [L("rocket", 39, 0)])
    b = track("b", [L("rocket", 7, 0), L("rocket", 31, 1)])  # #2 is the pickup into the hook at bar 8
    c = find(a, b)[0]
    assert c.b.nth == 2 and "pickup into its hook" in c.why


def test_never_mid_section_in_either_song() -> None:
    """A loop doesn't excuse a mid-verse word: A's word and B's way in must
    sit in a section's first or last bar."""
    verse = ((0, 8, "intro"), (8, 24, "verse"), (24, 32, "hook"))
    b = track("b", [L("rocket", 95, 0)], whole=True, sections=verse)  # bar 23: the verse's last bar
    assert find(track("a", [L("rocket", 60, 0)], whole=True, sections=verse), b) == []  # A at bar 15
    assert find(track("a", [L("rocket", 93, 0)], whole=True, sections=verse), b)  # A at bar 23
    mid_b = track("b", [L("rocket", 61, 0)], whole=True, sections=verse)
    assert find(track("a", [L("rocket", 93, 0)], whole=True, sections=verse), mid_b) == []


def Spot(name, beat, role, word, loop=None):
    from dj_segue.annotate import Cue

    return Cue(name, beat * 0.5, "", role, word, loop)


VERSE = ((0, 8, "intro"), (8, 24, "verse"), (24, 32, "hook"))


def test_your_spots_beat_the_rules_and_rank_first() -> None:
    """Spots mid-verse (which the rules would refuse) pair up, rank above a
    better-scoring automatic match, and carry the loop length."""
    a = track("a", [L("we", 59, 0), L("rocket", 60, 0), L("go", 61, 0), L("tonight", 93, 1)], whole=True,
              sections=VERSE, spots=[Spot("lift off", 60, "out", "rocket", 8)])
    b = track("b", [L("ride", 59, 0), L("the", 60, 0), L("rocket", 61, 0), L("tonight", 95, 1)], whole=True,
              sections=VERSE, spots=[Spot("rocket in", 61, "in", "rocket")])
    assert find(a, b, spots_only=False)  # the automatic "tonight" is there too…
    c = find(a, b)[0]
    assert c.a_text == "rocket" and (c.a.spot, c.b.spot) == ("out", "in") and c.a.loop == 8
    assert c.technique == "loop and jump" and "your spots" in c.why
    assert [x.a_text for x in find(a, b, spots_only=True)] == ["rocket"]
    # An "in" spot on the outgoing side isn't an out spot: the rules apply again there.
    a_in = track("a", a.words, whole=True, sections=VERSE, spots=[Spot("lift off", 60, "in", "rocket")])
    assert find(a_in, track("b", b.words, whole=True, sections=VERSE), spots_only=True) == []


def test_a_spot_stands_in_where_the_word_wasnt_aligned() -> None:
    a = track("a", [L("tonight", 93, 1)], whole=True, sections=VERSE, spots=[Spot("chant", 60, "out", "rocket")])
    b = track("b", [L("rocket", 95, 0)], whole=True, sections=VERSE)
    c = find(a, b, spots_only=True)[0]
    assert c.a.spot == "out" and c.a.beat == pytest.approx(60.0) and c.a.cue == "chant"


def test_a_spot_keeps_a_phrase_of_common_words() -> None:
    """"one more time" is all common words, which the finder skips as a
    phrase, unless a spot names it (Starships → One More Time)."""
    sung = [L("one", 40, 0), L("more", 41, 0), L("time", 42, 0)]
    b = track("b", [L("one", 60, 3), L("more", 61, 3), L("time", 62, 3)], sections=VERSE)
    assert not [c for c in find(track("a", sung, sections=VERSE), b) if c.kind == "phrase"]
    a = track("a", sung, sections=VERSE, spots=[Spot("omt", 40, "out", "One More  Time")])
    c = find(a, b, spots_only=True)[0]
    assert (c.kind, c.a_text, c.a.spot) == ("phrase", "one more time", "out")
