"""`dj-segue tune`: previews match `render`, edits validate, saves are in place."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest
import soundfile as sf

from dj_segue.executor.native import NativeEngine
from dj_segue.tune import TuneSession, make_server

SR = 44100
BEAT = SR // 2  # 120 bpm

PLAN = """// tune test plan
{
  "schema_version": "0.4",
  "meta": { "mix_name": "t", "mix_tempo": 120 },
  "tracks": {
    "a": { "path": "a.wav", "bpm": 120 },
    "b": { "path": "b.wav", "bpm": 120 }
  },
  "decks": { "1": {}, "2": {} },
  "timeline": [
    { "type": "play", "id": "a_end", "deck": 1, "track": "a",
      "from": { "beat": 0 }, "to": { "beat": 48 }, "start_at": { "beat": 0 } },
    { "type": "play", "deck": 2, "track": "b",
      "from": { "beat": 0 }, "to": { "beat": 48 },
      "start_at": { "after": "a_end", "offset": { "beats": -32 } } },  // b comes in

    // the blend
    { "type": "transition", "style": "crossfade", "from_deck": 1, "to_deck": 2,
      "start_at": { "after": "a_end", "offset": { "beats": -16 } },
      "duration": { "beats": 16 } }
  ]
}
"""


@pytest.fixture
def session(tmp_path):
    rng = np.random.default_rng(1)
    for name in ("a", "b"):
        sf.write(tmp_path / f"{name}.wav", (0.2 * rng.standard_normal(60 * BEAT)).astype(np.float32),
                 SR, subtype="FLOAT")
    plan = tmp_path / "t.plan.jsonc"
    plan.write_text(PLAN)
    s = TuneSession(plan, tmp_path)
    s.load()
    return s


def _beats(s: TuneSession, b: float) -> float:
    return b * s.spb


def test_lists_the_crossfade(session) -> None:
    (it,) = session.items()
    assert it.index == 2 and (it.from_deck, it.to_deck) == (1, 2)
    d = session.describe(2)
    assert d["state"]["out"] == {"start": _beats(session, 32), "end": _beats(session, 48),
                                 "curve": "equal_power"}
    assert d["zero_sec"] == _beats(session, 32)


def test_unchanged_state_changes_nothing(session) -> None:
    r = session.preview(2, session.describe(2)["state"])
    assert r["ok"] and r["changes"] == {}


def test_asymmetric_edit_keeps_the_after_form(session) -> None:
    st = session.describe(2)["state"]
    st["in"]["start"] = _beats(session, 24)
    st["in"]["curve"] = "linear"
    r = session.preview(2, st)
    assert r["ok"], r
    assert r["changes"] == {"in": {"offset": {"beats": -8}, "duration": {"beats": 24}, "curve": "linear"}}
    st["out"]["start"] = _beats(session, 40)
    r = session.preview(2, st)
    assert r["changes"]["start_at"] == {"after": "a_end", "offset": {"beats": -8}}
    assert r["changes"]["duration"] == {"beats": 8}


def test_same_curve_both_sides_is_one_key(session) -> None:
    st = session.describe(2)["state"]
    st["out"]["curve"] = st["in"]["curve"] = "exponential"
    assert session.preview(2, st)["changes"] == {"curve": "exponential"}


def test_fade_outside_the_deck_is_refused(session) -> None:
    st = session.describe(2)["state"]
    st["in"]["start"] = _beats(session, 8)  # deck 2 only starts at beat 16
    r = session.preview(2, st)
    assert not r["ok"] and "deck 2" in r["issues"][0]


def test_preview_audio_is_what_render_produces_after_save(session, tmp_path) -> None:
    st = session.describe(2)["state"]
    st["in"]["start"], st["in"]["curve"] = _beats(session, 20), "exponential"
    r = session.preview(2, st)
    page, _ = sf.read(io.BytesIO(session.audio[r["version"]]), dtype="float32")
    it = session.item(2)  # the region the page played (saving widens it)
    assert session.save(2, st)["saved"]
    fresh = TuneSession(session.plan_path, tmp_path)
    full = NativeEngine().render(fresh.plan, tmp_path, fresh.grids).samples
    np.testing.assert_array_equal(page, full[it.start : it.end])


def test_save_only_touches_the_transition(session) -> None:
    st = session.describe(2)["state"]
    st["out"]["curve"] = st["in"]["curve"] = "linear"
    session.save(2, st)
    text = session.plan_path.read_text()
    assert text == PLAN.replace(
        '"duration": { "beats": 16 } }',
        '"duration": { "beats": 16 },\n      "curve": "linear" }',
    )


def test_status_while_rendering(tmp_path, session) -> None:
    fresh = TuneSession(session.plan_path, tmp_path)  # not loaded yet
    server = make_server(fresh)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert json.load(urllib.request.urlopen(base + "/status"))["ready"] is False
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/items")
        assert e.value.code == 503
        fresh.load()
        st = json.load(urllib.request.urlopen(base + "/status"))
        assert st["ready"] is True and st["done"] == st["total"] == 2
    finally:
        server.shutdown()
        server.server_close()


def test_http_round_trip(session) -> None:
    server = make_server(session)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        items = json.load(urllib.request.urlopen(base + "/items"))
        assert items["items"][0]["index"] == 2
        assert b"<canvas" in urllib.request.urlopen(base + "/").read()
        st = json.load(urllib.request.urlopen(base + "/item/2"))["state"]
        req = urllib.request.Request(base + "/preview", json.dumps({"index": 2, "state": st}).encode(),
                                     {"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req))
        wav = urllib.request.urlopen(f"{base}/audio/{r['version']}").read()
        assert wav[:4] == b"RIFF"
    finally:
        server.shutdown()
        server.server_close()


def test_drawn_curve_saves_and_bumps_the_schema(session) -> None:
    st = session.describe(2)["state"]
    st["in"]["curve"] = {"points": [[0, 0], [0.5, 0.1], [1, 1]], "smooth": True}
    r = session.preview(2, st)
    assert r["ok"], r
    assert r["changes"] == {"in": {"curve": {"points": [[0, 0], [0.5, 0.1], [1, 1]], "smooth": True}}}
    assert r["plan_changes"] == {"schema_version": "0.5"}
    session.save(2, st)
    text = session.plan_path.read_text()
    assert text == PLAN.replace('"schema_version": "0.4"', '"schema_version": "0.5"').replace(
        '"duration": { "beats": 16 } }',
        '"duration": { "beats": 16 },\n'
        '      "in": { "curve": { "points": [[0, 0], [0.5, 0.1], [1, 1]], "smooth": true } } }',
    )
    assert session.describe(2)["state"]["in"]["curve"]["smooth"] is True
