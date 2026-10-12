"""`dj-segue prepare`: beat analysis, word timing and an annotation draft in
one step; cached, and an existing annotation is never overwritten."""

from __future__ import annotations

import pytest

import dj_segue.words as words_mod
from dj_segue.analyzer import is_fresh
from dj_segue.annotate import annotation_path, load, save
from dj_segue.prepare import prepare
from dj_segue.words import WordTiming
from dj_segue.words.merge import Word


def fake_stems(audio, sr):
    return {"vocals": audio * 0.5, "drums": audio, "bass": audio * 0.5, "other": audio * 0.1}


@pytest.fixture
def track(tmp_path, write_drums, monkeypatch):
    path = tmp_path / "t.wav"
    write_drums(path, 120.0, 0.25, seconds=30.0, first_beat_in_bar=0)
    calls = []

    def words_for(p, whisper=True, log=print):
        calls.append(whisper)
        return WordTiming(p.name, "x", {"id": 9, "synced": True}, whisper,
                          [Word("hi", 1.0, 1.2, 0.9, "lyrics", True, 0)])

    monkeypatch.setattr(words_mod, "words_for", words_for)
    return path, calls


def test_prepares_a_new_track(track, tmp_path) -> None:
    path, calls = track
    folder = tmp_path / "annotations"
    r = prepare(path, folder=folder, stems=fake_stems, log=lambda *_: None)
    assert is_fresh(path)  # the beat analysis sidecar
    assert r["bpm"] == pytest.approx(120, abs=0.5)
    assert r["words"]["words"] == 1 and r["words"]["confirmed"] == 1 and calls == [True]
    assert r["annotation"] == "drafted" and annotation_path(path, folder).exists()
    assert r["sections"] >= 1 and r["labelled"] == 0 and r["cues"] == 0


def test_an_existing_annotation_is_kept(track, tmp_path) -> None:
    path, _ = track
    folder = tmp_path / "annotations"
    prepare(path, folder=folder, stems=fake_stems, log=lambda *_: None)
    ann = load(annotation_path(path, folder))
    ann.phrases[0].label = "intro"
    save(ann, annotation_path(path, folder))
    r = prepare(path, folder=folder, stems=fake_stems, words=False, log=lambda *_: None)
    assert r["annotation"] == "kept" and r["labelled"] == 1 and "words" not in r
    assert load(annotation_path(path, folder)).phrases[0].label == "intro"


def test_missing_word_extras_dont_stop_it(track, tmp_path, monkeypatch) -> None:
    path, _ = track

    def no_words(*a, **k):
        raise ImportError("No module named 'faster_whisper'")

    monkeypatch.setattr(words_mod, "words_for", no_words)
    r = prepare(path, folder=tmp_path / "a", stems=fake_stems, log=lambda *_: None)
    assert "pip install" in r["words_error"] and r["annotation"] == "drafted"
