"""`dj-segue scrub`: song-structure analysis and the local web server.
(The page's playback logic runs in the browser and isn't unit-tested.)"""

from __future__ import annotations

import json
import threading
import urllib.request

import numpy as np
import pytest
import soundfile as sf

from dj_segue.analyzer.structure import analyze_structure, find_breaks
from dj_segue.scrub import ScrubTrack, make_server
from dj_segue.analyzer.structure import TrackStructure
from dj_segue.time_math import TrackGrid

SR = 44100


def test_find_breaks_marks_short_bass_dropouts_only() -> None:
    bass = np.full(40, 50.0)
    bass[14:16] = 30.0  # 2-bar break
    bass[24:32] = 30.0  # 8-bar breakdown: a section, not a break
    assert find_breaks(bass) == [(14, 16)]


def test_small_bass_dips_are_not_breaks() -> None:
    bass = np.full(40, 50.0)
    bass[10] = 47.0
    assert find_breaks(bass) == []


def _kick_track(path, bpm=120.0, bars=32, silent_bars=(), seconds=None):
    per = 60.0 / bpm
    n = int((seconds or bars * 4 * per + 1) * SR)
    y = np.zeros(n, dtype=np.float32)
    t = np.arange(int(0.15 * SR)) / SR
    kick = (np.sin(2 * np.pi * 50 * t) * np.exp(-t * 20) * 0.8).astype(np.float32)
    rng = np.random.default_rng(0)
    hat = (rng.standard_normal(int(0.02 * SR)) * 0.1).astype(np.float32)
    for beat in range(bars * 4):
        s = int(round(beat * per * SR))
        y[s : s + len(hat)] += hat[: n - s]
        if beat // 4 not in silent_bars:
            y[s : s + len(kick)] += kick[: n - s]
    sf.write(path, y, SR, subtype="FLOAT")


def test_structure_finds_a_break_in_audio(tmp_path) -> None:
    _kick_track(tmp_path / "k.wav", silent_bars={14, 15})
    st = analyze_structure(tmp_path / "k.wav", TrackGrid(anchor_sec=0.0, bpm=120.0))
    assert st.n_bars == 32
    assert st.breaks == [(14, 16)]
    assert 16 in st.boundaries  # the bass comes back: a section start
    j = st.to_json()
    assert len(j["energy"]) == 32 and 0 <= min(j["bass"]) and max(j["bass"]) == 1


@pytest.fixture
def server(tmp_path):
    audio = tmp_path / "t.wav"
    sf.write(audio, np.zeros(SR, dtype=np.float32), SR)
    track = ScrubTrack(
        audio_path=audio,
        grid=TrackGrid(anchor_sec=0.25, bpm=120.0),
        duration_sec=1.0,
        structure=TrackStructure(np.zeros(2), np.zeros(2), [1], [(1, 2)]),
        start_beat=3,
    )
    marks: list[int] = []
    srv = make_server(track, on_mark=marks.append)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", audio, marks
    srv.shutdown()
    srv.server_close()


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.headers.get("Content-Type"), r.read()


def test_server_serves_page_analysis_and_audio(server) -> None:
    base, audio, _ = server
    ctype, page = _get(base + "/")
    assert ctype.startswith("text/html") and b"<canvas" in page
    _, body = _get(base + "/analysis")
    a = json.loads(body)
    assert a["bpm"] == 120.0 and a["anchor_sec"] == 0.25 and a["start_beat"] == 3
    assert a["bars"]["boundaries"] == [1] and a["bars"]["breaks"] == [[1, 2]]
    ctype, data = _get(base + "/audio")
    assert ctype == "audio/x-wav" or ctype.startswith("audio/")
    assert data == audio.read_bytes()


def test_server_records_marks(server) -> None:
    base, _, marks = server
    req = urllib.request.Request(base + "/mark", data=b'{"beat": 184}', method="POST")
    urllib.request.urlopen(req).read()
    assert marks == [184]
    bad = urllib.request.Request(base + "/mark", data=b"nope", method="POST")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(bad)
