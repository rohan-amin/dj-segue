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


def track(name, words, sections=((0, 8, "verse"), (8, 16, "hook")), title="", cues=None):
    counts = {}
    for w in words:
        counts[w.text.lower()] = counts.get(w.text.lower(), 0) + 1
    return Track(name=name, title_words=set(title.split()), bpm=BPM, anchor=0.0, key=None, camelot=None,
                 duration=64.0, sections=list(sections), words=words, counts=counts, cues=cues or {})


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
    assert c.technique == "vocal handoff" and c.parts["fit"] > 0.9
    a2 = track("a", [W("rocket", 36.0)])
    assert find(a2, b)[0].technique == "echo out"  # a beat apart, no hook to jump to


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
