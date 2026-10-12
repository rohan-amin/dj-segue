"""`dj-segue annotate`: the draft, key detection, stem activity, the
summary a planner reads, and the page's server (save, re-grid)."""

from __future__ import annotations

import json
import urllib.request

import numpy as np
import pytest
import soundfile as sf

from dj_segue.annotate import (
    Annotation,
    Phrase,
    camelot,
    detect_key,
    draft,
    load,
    phrases_from,
    save,
    stem_activity,
    summary,
)
from dj_segue.annotate.server import AnnotateSession, make_server
from dj_segue.time_math import TrackGrid

SR = 22050


def test_camelot() -> None:
    assert camelot("A minor") == "8A" and camelot("C major") == "8B"
    assert camelot("G minor") == "6A" and camelot("F# major") == "2B"
    assert camelot(None) is None


@pytest.mark.parametrize("tonic,mode,notes", [
    ("C", "major", [[60, 64, 67], [65, 69, 72], [67, 71, 74], [60, 64, 67]]),  # I IV V I
    ("A", "minor", [[57, 60, 64], [62, 65, 69], [64, 68, 71], [57, 60, 64]]),  # i iv V i
])
def test_detects_the_key_of_a_progression(tonic, mode, notes) -> None:
    t = np.arange(int(2 * SR)) / SR
    chords = []
    for chord in notes * 3:
        x = sum(np.sin(2 * np.pi * 440 * 2 ** ((m - 69) / 12) * t) for m in chord)
        chords.append(x * np.exp(-t * 0.5))
    y = np.concatenate(chords).astype(np.float32) * 0.2
    key, conf = detect_key(np.stack([y, y], 1), SR)
    assert key == f"{tonic} {mode}"
    assert conf > 0.3


def test_stem_activity_follows_the_level() -> None:
    grid = TrackGrid(anchor_sec=0.0, bpm=120)  # 2 s bars
    bar = 2 * SR
    x = np.zeros((8 * bar, 2), dtype=np.float32)
    x[2 * bar : 6 * bar] = 0.5  # loud in bars 2–5
    x[6 * bar : 7 * bar] = 0.5 * 10 ** (-15 / 20)  # 15 dB down in bar 6
    act = stem_activity(x, SR, grid, 8)
    assert act[0] == act[1] == act[7] == 0.0
    assert all(act[2:6] == 1.0)
    assert act[6] == pytest.approx(3 / 18, abs=0.02)  # 15 of the 18 dB down


def test_phrases_from_detected_starts() -> None:
    ps = phrases_from([8, 16, 40, 99], 48)
    assert [(p.bar, p.bars) for p in ps] == [(0, 8), (8, 8), (16, 24), (40, 8)]


def _ann(**kw) -> Annotation:
    n = 16
    vocals = [0.9] * 4 + [0.1] * 6 + [0.8] * 6  # no vocals in bars 4–9
    return Annotation(
        audio="audio/x.wav", audio_sha1="0", bpm=120.0, anchor_sec=0.0, duration_sec=32.0,
        key="A minor", key_detected="A minor", key_confidence=0.6,
        phrases=[Phrase(0, 4, "intro"), Phrase(4, 8, "breakdown", "drums only"), Phrase(12, 4, "hook")],
        bars={"energy": [0.5] * n, "bass": [0.5] * n, "vocals": vocals, "drums": [1.0] * n,
              "other": [0.3] * n},
        **kw,
    )


def test_summary_is_what_a_planner_needs() -> None:
    s = summary(_ann(tags=["house"]))
    assert s["camelot"] == "8A" and s["bpm"] == 120.0 and s["bars"] == 16
    assert [p["label"] for p in s["phrases"]] == ["intro", "breakdown", "hook"]
    br = s["phrases"][1]
    assert br["bars"] == "4–11" and br["beats"] == "16–47" and br["note"] == "drums only"
    assert br["vocals"] == pytest.approx((0.1 * 6 + 0.8 * 2) / 8, abs=0.01)
    assert s["no_vocals"] == ["bars 4–9 (beats 16–39)"]


def test_save_and_load_round_trip(tmp_path) -> None:
    a = _ann()
    save(a, tmp_path / "a.json")
    b = load(tmp_path / "a.json")
    assert b == a


# ---------------------------------------------------------------------------
# with audio: the draft and the server
# ---------------------------------------------------------------------------


def fake_stems(audio, sr):
    """Vocals in the first half only; drums throughout."""
    v = audio.copy()
    v[len(v) // 2 :] = 0
    return {"vocals": v, "drums": audio, "bass": audio * 0.5, "other": audio * 0.1}


@pytest.fixture
def track(tmp_path, write_drums):
    path = tmp_path / "t.wav"
    write_drums(path, 120.0, 0.25, seconds=40.0, first_beat_in_bar=0)
    return path


def test_draft(track) -> None:
    ann = draft(track, "t.wav", stems=fake_stems)
    assert ann.bpm == pytest.approx(120, abs=0.5)
    assert ann.anchor_sec == pytest.approx(0.25, abs=0.02)
    assert set(ann.bars) == {"energy", "bass", "vocals", "drums", "other"}
    n = ann.n_bars
    assert len(ann.bars["vocals"]) == n and n >= 18
    assert ann.bars["vocals"][2] > 0.8 and ann.bars["vocals"][n - 2] == 0.0
    assert ann.phrases[0].bar == 0 and sum(p.bars for p in ann.phrases) == n
    assert ann.key and camelot(ann.key)


def _post(url, path, body):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


@pytest.fixture
def served(track, tmp_path):
    import threading

    ann = draft(track, "t.wav", stems=fake_stems)
    session = AnnotateSession(ann, track, tmp_path / "annotations" / "t.json", stems=fake_stems)
    server = make_server(session)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield session, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_save_writes_the_edits(served) -> None:
    session, url = served
    got = json.loads(urllib.request.urlopen(url + "/annotation").read())
    assert got["summary"]["bars"] == session.ann.n_bars and len(got["keys"]) == 24
    res = _post(url, "/save", {
        "phrases": [{"bar": 0, "label": "intro"}, {"bar": 8, "label": "hook", "note": "big"},
                    {"bar": 4, "label": "verse"}, {"bar": 8, "label": "drop"}],
        "key": "D minor", "key_checked": True, "grid_checked": True,
        "tags": ["test", " ", "drums"], "notes": "n",
    })
    assert res["ok"]
    saved = load(session.out_path)
    assert [(p.bar, p.label) for p in saved.phrases] == [(0, "intro"), (4, "verse"), (8, "hook")]
    assert saved.phrases[-1].bars == saved.n_bars - 8
    assert saved.key == "D minor" and saved.key_checked and saved.grid_checked
    assert saved.tags == ["test", "drums"]


def test_bad_edits_are_refused(served) -> None:
    _, url = served
    assert not _post(url, "/save", {"phrases": [{"bar": 0, "label": "chorus?"}]})["ok"]
    assert not _post(url, "/save", {"phrases": [], "key": "H major"})["ok"]


def test_regrid_keeps_phrases_in_time(served) -> None:
    session, url = served
    _post(url, "/save", {"phrases": [{"bar": 0, "label": "intro"}, {"bar": 8, "label": "hook"}]})
    res = _post(url, "/grid", {"bpm": 60.0, "anchor_sec": session.ann.anchor_sec})  # half tempo
    assert res["ok"] and res["bpm"] == 60.0
    ann = session.ann
    assert [(p.bar, p.label) for p in ann.phrases] == [(0, "intro"), (4, "hook")]
    assert len(ann.bars["vocals"]) == ann.n_bars


def test_words_come_from_the_words_cache(served, tmp_path, monkeypatch) -> None:
    from dj_segue import words as words_mod
    from dj_segue.annotate import file_sha1

    session, url = served
    monkeypatch.setenv(words_mod.CACHE_ENV, str(tmp_path / "words"))
    assert json.loads(urllib.request.urlopen(url + "/words").read()) == {"available": False}
    folder = tmp_path / "words" / file_sha1(session.audio_path)
    folder.mkdir(parents=True)
    (folder / "words.json").write_text(json.dumps({
        "version": words_mod.VERSION, "audio": "t.wav", "audio_sha1": "x",
        "lyrics": {"id": 1, "artist": "a", "title": "t", "synced": True}, "whisper": True,
        "words": [{"text": "hi", "start": 1.0, "end": 1.2, "score": 0.9, "source": "lyrics",
                   "confirmed": True, "line": 0}],
    }))
    session._words = None  # (the session keeps what it read)
    got = json.loads(urllib.request.urlopen(url + "/words").read())
    assert got["available"] and got["words"][0]["start"] == 1.0
    assert got["summary"]["confirmed"] == 1


def test_cues_save_and_reach_the_planner(served) -> None:
    session, url = served
    res = _post(url, "/save", {"phrases": [{"bar": 0, "label": "intro"}],
                               "cues": [{"name": " drop ", "second": 4.25, "note": "big"},
                                        {"name": "early", "second": 1.0}]})
    assert res["ok"]
    saved = load(session.out_path)
    assert [(c.name, c.second) for c in saved.cues] == [("early", 1.0), ("drop", 4.25)]  # trimmed, in order
    cue = summary(saved)["cues"][1]
    assert cue["name"] == "drop" and cue["note"] == "big"
    assert cue["beat"] == pytest.approx((4.25 - saved.anchor_sec) * saved.bpm / 60, abs=0.01)
    # Mistakes are refused, and nothing changes.
    for bad in ([{"name": "a", "second": 1}, {"name": "a", "second": 2}],
                [{"name": "", "second": 1}], [{"name": "late", "second": 1e6}]):
        assert not _post(url, "/save", {"phrases": [], "cues": bad})["ok"]
    assert len(load(session.out_path).cues) == 2


def test_cues_stay_put_when_the_grid_moves(served) -> None:
    session, url = served
    _post(url, "/save", {"phrases": [], "cues": [{"name": "x", "second": 3.0}]})
    _post(url, "/grid", {"bpm": session.ann.bpm, "anchor_sec": session.ann.anchor_sec + 0.1})
    assert [c.second for c in load(session.out_path).cues] == [3.0]


def test_a_plan_finds_cues_marked_in_the_annotation(track, tmp_path) -> None:
    import shutil

    from dj_segue.annotate import Cue, annotation_path
    from dj_segue.compiler import resolve_timeline
    from dj_segue.preprocessor import preprocess
    from dj_segue.schema.plan import Plan
    from dj_segue.schema.validator import PlanValidationError, validate_plan

    root = tmp_path / "proj"
    (root / "audio").mkdir(parents=True)
    audio = root / "audio" / "t.wav"
    shutil.copy(track, audio)
    ann = draft(audio, "audio/t.wav", stems=fake_stems)
    ann.cues = [Cue("in_here", 2.0), Cue("out_here", 6.5)]
    save(ann, annotation_path(audio, root / "annotations"))

    def plan(frm, to):
        return Plan.model_validate({
            "schema_version": "0.10", "meta": {"mix_name": "t", "mix_tempo": 120},
            "tracks": {"t": {"path": "audio/t.wav", "bpm": 120, "cues": {"declared": {"second": 1.5}}}},
            "decks": {"1": {}},
            "timeline": [{"type": "play", "deck": 1, "track": "t", "from": frm, "to": to}],
        })

    p = plan({"cue": "in_here"}, "out_here")
    validate_plan(p)  # before preprocessing an undeclared cue may be the annotation's
    grids = preprocess(p, root).grids()
    validate_plan(p, grids)
    span = resolve_timeline(p, 120.0, grids).spans[0]
    assert span.track_from_sec == 2.0 and span.mix_end_sec == pytest.approx(4.5)
    # A plan's own cue still works; a name in neither is an error that lists both kinds.
    p = plan("declared", "nope")
    grids = preprocess(p, root).grids()
    with pytest.raises(PlanValidationError, match="declared.*in_here.*out_here|marked in its annotation"):
        validate_plan(p, grids)
