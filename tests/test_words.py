"""Word timing (M6 W1): LRCLIB parsing, the lyrics/Whisper merge, gap filling,
and the cached pipeline — with the network, stems and models faked out."""

from __future__ import annotations

import json

import numpy as np
import pytest

import dj_segue.words as words_mod
from dj_segue.words import lrclib, summary, words_for
from dj_segue.words.align import Aligned, _fill_gaps
from dj_segue.words.merge import CONFIRM_SEC, merge, norm


def A(text, start, line=0, score=0.8):
    return Aligned(text, start, start + 0.2, score, line)


# -- LRCLIB --------------------------------------------------------------------


def test_clean_title_drops_edition_tags() -> None:
    assert lrclib.clean_title("Crazy In Love [Clean]") == "Crazy In Love"
    assert lrclib.clean_title("Bottoms Up (feat. Nicki Minaj) [Explicit]") == "Bottoms Up"
    assert lrclib.clean_title("Like 'em All (Radio Version)") == "Like 'em All"
    assert lrclib.clean_title("Love (Remix)") == "Love (Remix)"  # not an edition tag


def test_synced_and_plain_lines() -> None:
    rec = {"syncedLyrics": "[00:01.50] one two\n[00:04.00]\n[01:02.25] three",
           "plainLyrics": "[Verse 1]\none two\n\nthree\n"}
    assert lrclib.synced_lines(rec) == [(1.5, "one two"), (4.0, ""), (62.25, "three")]
    assert lrclib.plain_lines(rec) == ["one two", "three"]


# -- merge ----------------------------------------------------------------------


def test_lyric_words_confirmed_by_whisper_nearby() -> None:
    lyr = [A("Hello,", 1.0), A("there", 1.5), A("friend", 2.0)]
    heard = [A("hello", 1.05), A("there", 1.5 + CONFIRM_SEC + 0.1), A("friend", 2.1)]
    out = merge(lyr, heard)
    assert [(w.text, w.confirmed) for w in out] == [("Hello,", True), ("there", False), ("friend", True)]
    assert all(w.source == "lyrics" for w in out)  # the late "there" isn't in a gap


def test_each_heard_word_confirms_once() -> None:
    out = merge([A("oh", 1.0), A("oh", 1.1)], [A("oh", 1.05)])
    assert sum(w.confirmed for w in out) == 1


def test_whisper_words_fill_gaps_only() -> None:
    lyr = [A("one", 1.0), A("two", 1.4)]
    heard = [A("one", 1.0), A("yeah", 1.5), A("hey", 5.0)]
    out = merge(lyr, heard)
    extra = [(w.text, w.source, w.confirmed) for w in out if w.source == "whisper"]
    assert extra == [("hey", "whisper", False)]  # "yeah" sits inside the lyrics
    assert [w.start for w in out] == sorted(w.start for w in out)


def test_merge_without_one_side() -> None:
    lyr, heard = [A("one", 1.0)], [A("uno", 1.0)]
    assert [(w.source, w.confirmed) for w in merge(lyr, None)] == [("lyrics", None)]
    assert [(w.source, w.confirmed) for w in merge(None, heard)] == [("whisper", False)]


def test_norm_ignores_case_and_punctuation() -> None:
    assert norm("Don't!") == norm("dont") == "dont"


# -- alignment helpers -------------------------------------------------------------


def test_unaligned_words_take_the_gap() -> None:
    times = [(1.0, 1.2, 0.9), None, None, (2.2, 2.4, 0.9), None]
    _fill_gaps(times, 0.0, 3.0)
    assert times[1] == pytest.approx((1.2, 1.7, 0.0))
    assert times[2] == pytest.approx((1.7, 2.2, 0.0))
    assert times[4] == pytest.approx((2.4, 3.0, 0.0))


# -- the pipeline (faked steps) ---------------------------------------------------------


@pytest.fixture
def fake(tmp_path, monkeypatch):
    calls = {"lookup": 0, "align": 0, "transcribe": 0}
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"not really audio")
    monkeypatch.setenv(words_mod.CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.setattr(words_mod, "read_tags", lambda p: {
        "artist": "Someone", "title": "Song [Clean]", "duration": 10.0})

    def lookup(artist, title, album, duration):
        calls["lookup"] += 1
        title = lrclib.clean_title(title)  # as the real lookup does
        assert (artist, title) == ("Someone", "Song")
        return {"id": 7, "artistName": artist, "trackName": title,
                "syncedLyrics": "[00:01.00] la la\n[00:03.00] go now"}

    def align(audio16k, spans):
        calls["align"] += 1
        return [Aligned(w, s0 + 0.3 + 0.4 * i, s0 + 0.5 + 0.4 * i, 0.9, k)
                for k, (s0, _, text) in enumerate(spans) for i, w in enumerate(text.split())]

    def transcribe(audio16k):
        calls["transcribe"] += 1
        return [(0.5, 2.0, "la la"), (6.0, 7.0, "hey")]

    monkeypatch.setattr(lrclib, "lookup", lookup)
    monkeypatch.setattr("dj_segue.words.align.align", align)
    monkeypatch.setattr("dj_segue.words.transcribe.transcribe", transcribe)
    monkeypatch.setattr(words_mod, "_vocals16k", lambda p, log: np.zeros(16000 * 10, np.float32))
    return audio, calls


def test_pipeline_merges_and_caches(fake) -> None:
    audio, calls = fake
    wt = words_for(audio, log=lambda *_: None)
    assert wt.lyrics == {"id": 7, "artist": "Someone", "title": "Song", "synced": True}
    assert [w.text for w in wt.words] == ["la", "la", "go", "now", "hey"]
    assert [w.source for w in wt.words][-1] == "whisper"
    assert summary(wt)["from_lyrics"] == 4 and summary(wt)["whisper_only"] == 1
    # Everything comes from the cache the second time.
    again = words_for(audio, log=lambda *_: None)
    assert [w.to_json() for w in again.words] == [w.to_json() for w in wt.words]
    assert calls == {"lookup": 1, "align": 2, "transcribe": 1}


def test_lyrics_only_then_whisper_adds_the_check(fake) -> None:
    audio, calls = fake
    wt = words_for(audio, whisper=False, log=lambda *_: None)
    assert all(w.confirmed is None for w in wt.words) and not wt.whisper
    wt = words_for(audio, log=lambda *_: None)  # Whisper's pass is added, lyrics reused
    assert wt.whisper and calls["lookup"] == 1 and calls["transcribe"] == 1


def test_a_miss_is_cached_and_whisper_stands_alone(fake, monkeypatch) -> None:
    audio, calls = fake
    monkeypatch.setattr(lrclib, "lookup", lambda *a: calls.__setitem__("lookup", 99) or None)
    wt = words_for(audio, log=lambda *_: None)
    assert wt.lyrics is None and {w.source for w in wt.words} == {"whisper"}
    cached = json.loads(next((words_mod.cache_dir()).glob("*/lrclib.json")).read_text())
    assert cached["record"] is None


def test_summary_has_no_lyrics(fake) -> None:
    audio, _ = fake
    s = json.dumps(summary(words_for(audio, log=lambda *_: None)))
    assert "la" not in s and "go" not in s
