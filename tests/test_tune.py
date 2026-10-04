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


# --- EQ / filter lanes and echo out (v0.7) -------------------------------------

ECHO_PLAN = """// tune echo plan
{
  "schema_version": "0.6",
  "meta": { "mix_name": "e", "mix_tempo": 120 },
  "tracks": {
    "a": { "path": "a.wav", "bpm": 120 },
    "b": { "path": "b.wav", "bpm": 120 }
  },
  "decks": { "1": { "label": "A" }, "2": { "label": "B" } },
  "timeline": [
    { "type": "play", "id": "a_end", "deck": 1, "track": "a",
      "from": { "beat": 0 }, "to": { "beat": 32 }, "start_at": { "beat": 0 } },
    { "type": "play", "deck": 2, "track": "b",
      "from": { "beat": 0 }, "to": { "beat": 48 },
      "start_at": { "after": "a_end", "offset": { "beats": -8 } } },
    { "type": "transition", "style": "echo_out", "from_deck": 1, "to_deck": 2,
      "start_at": { "after": "a_end" }, "duration": { "beats": 4 },
      "echo": { "every": { "beats": 1 }, "feedback": 0.5 } }
  ],
  "automation": [
    // bass out before the echo
    { "lane": "eq", "deck": 1, "band": "low",
      "keyframes": [
        { "at": { "beat": 24 }, "value_db": 0 },
        { "at": { "beat": 28 }, "value_db": -24 }
      ] }
  ]
}
"""


@pytest.fixture
def echo_session(tmp_path):
    rng = np.random.default_rng(2)
    for name in ("a", "b"):
        sf.write(tmp_path / f"{name}.wav", (0.2 * rng.standard_normal(60 * BEAT)).astype(np.float32),
                 SR, subtype="FLOAT")
    plan = tmp_path / "e.plan.jsonc"
    plan.write_text(ECHO_PLAN)
    s = TuneSession(plan, tmp_path)
    s.load()
    return s


def test_lists_the_echo_out_with_its_state(echo_session) -> None:
    s = echo_session
    (it,) = s.items()
    assert it.style == "echo_out"
    d = s.describe(2)
    assert d["style"] == "echo_out"
    st = d["state"]
    assert st["out"]["start"] == st["out"]["end"] == _beats(s, 32)
    assert st["in"] == {"start": _beats(s, 32), "end": _beats(s, 32), "curve": "step"}
    assert st["echo"] == {"end": _beats(s, 36), "every": 1, "feedback": 0.5, "highpass": 0.0}
    assert st["lanes"]["1"]["low"] == [[_beats(s, 24), 0, 0], [_beats(s, 28), -24, 1]]
    assert st["lanes"]["2"]["filter"] is None
    r = s.preview(2, st)
    assert r["ok"] and r["changes"] == {} and r["lane_changes"] == []
    assert r["tail_peaks"] is not None and set(r["peaks"]) == {"1", "2"}
    assert "A → B · echo out" in s.label(it)


def test_echo_edits_save_in_place(echo_session) -> None:
    s = echo_session
    st = s.describe(2)["state"]
    st["echo"].update(end=_beats(s, 40), every=2, feedback=0.6)
    st["in"].update(start=_beats(s, 30), end=_beats(s, 32), curve="linear")
    r = s.preview(2, st)
    assert r["ok"], r
    assert r["changes"] == {
        "duration": {"beats": 8},
        "echo": {"every": {"beats": 2}, "feedback": 0.6},
        "in": {"offset": {"beats": -2}, "duration": {"beats": 2}, "curve": "linear"},
    }
    assert s.save(2, st)["saved"]
    assert '"echo": { "every": { "beats": 2 }, "feedback": 0.6 }' in s.plan_path.read_text()


def test_echo_highpass_bumps_to_v07(echo_session) -> None:
    st = echo_session.describe(2)["state"]
    st["echo"]["highpass"] = 0.8
    r = echo_session.preview(2, st)
    assert r["ok"] and r["plan_changes"] == {"schema_version": "0.7"}


def test_cut_needs_the_repeated_beat_on_the_deck(echo_session) -> None:
    s = echo_session
    st = s.describe(2)["state"]
    st["out"]["start"] = st["out"]["end"] = _beats(s, 34)  # after deck 1 stops
    r = s.preview(2, st)
    assert not r["ok"]


def test_moved_keyframe_keeps_the_rest_of_the_file(echo_session) -> None:
    s = echo_session
    st = s.describe(2)["state"]
    st["lanes"]["1"]["low"][1] = [_beats(s, 30), -18, 1]
    r = s.preview(2, st)
    assert r["ok"] and r["lane_changes"] == ["automation[0] deck 1 low: 2 keyframes"]
    assert s.save(2, st)["saved"]
    assert s.plan_path.read_text() == ECHO_PLAN.replace(
        '{ "at": { "beat": 28 }, "value_db": -24 }', '{ "at": { "beat": 30 }, "value_db": -18 }'
    )


def test_new_filter_lane_is_appended_and_bumps_the_schema(echo_session, tmp_path) -> None:
    s = echo_session
    st = s.describe(2)["state"]
    # New keyframes are written like the transition's start_at (after a_end).
    st["lanes"]["1"]["filter"] = [[_beats(s, 24), 0, None], [_beats(s, 32), 0.5, None]]
    r = s.preview(2, st)
    assert r["ok"], r
    assert r["plan_changes"] == {"schema_version": "0.7"}
    assert r["lane_changes"] == ["new lane: deck 1 filter, 2 keyframes"]
    page, _ = sf.read(io.BytesIO(s.audio[r["version"]]), dtype="float32")
    it = s.item(2)
    assert s.save(2, st)["saved"]
    text = s.plan_path.read_text()
    assert """    { "lane": "filter", "deck": 1,
      "keyframes": [
        { "at": { "after": "a_end", "offset": { "beats": -8 } }, "value": 0 },
        { "at": { "after": "a_end" }, "value": 0.5 }
      ] }
  ]""" in text
    assert '"schema_version": "0.7"' in text
    # What the page played is what a render of the saved plan produces.
    fresh = TuneSession(s.plan_path, tmp_path)
    full = NativeEngine().render(fresh.plan, tmp_path, fresh.grids).samples
    np.testing.assert_array_equal(page, full[it.start : it.end])


def test_lane_values_are_checked(echo_session) -> None:
    st = echo_session.describe(2)["state"]
    st["lanes"]["2"]["mid"] = [[1.0, -40, None]]
    r = echo_session.preview(2, st)
    assert not r["ok"] and "outside -24" in r["issues"][0]


# --- vocal handoff and stem lanes (v0.8) ----------------------------------------

HANDOFF_PLAN = """{
  "schema_version": "0.7",
  "meta": { "mix_name": "h", "mix_tempo": 120 },
  "tracks": {
    "a": { "stems": { "vocals": "av.wav", "drums": "ad.wav" }, "bpm": 120 },
    "b": { "stems": { "vocals": "bv.wav", "drums": "bd.wav" }, "bpm": 120 },
    "c": { "path": "c.wav", "bpm": 120 }
  },
  "decks": { "1": {}, "2": {}, "3": {} },
  "timeline": [
    { "type": "play", "deck": 1, "track": "a",
      "from": { "beat": 0 }, "to": { "beat": 32 }, "start_at": { "beat": 0 } },
    { "type": "play", "deck": 2, "track": "b",
      "from": { "beat": 0 }, "to": { "beat": 32 }, "start_at": { "beat": 8 } },
    { "type": "transition", "style": "vocal_handoff", "from_deck": 1, "to_deck": 2,
      "start_at": { "beat": 16 }, "duration": { "beats": 8 } },
    { "type": "play", "deck": 3, "track": "c",
      "from": { "beat": 0 }, "to": { "beat": 40 }, "start_at": { "beat": 0 } }
  ]
}
"""


@pytest.fixture
def handoff_session(tmp_path):
    rng = np.random.default_rng(3)
    for name in ("av", "ad", "bv", "bd", "c"):
        sf.write(tmp_path / f"{name}.wav", (0.1 * rng.standard_normal(60 * BEAT)).astype(np.float32),
                 SR, subtype="FLOAT")
    plan = tmp_path / "h.plan.jsonc"
    plan.write_text(HANDOFF_PLAN)
    s = TuneSession(plan, tmp_path)
    s.load()
    return s


def test_handoff_item_and_swap_edit(handoff_session, tmp_path) -> None:
    s = handoff_session
    (it,) = s.items()
    assert it.style == "vocal_handoff" and "vocal handoff" in s.label(it)
    d = s.describe(2)
    assert d["state"]["swap"] == _beats(s, 20)  # halfway
    assert d["stems"] == {"1": ["vocals", "drums"], "2": ["vocals", "drums"]}
    assert "vol@vocals" in d["state"]["lanes"]["1"]
    r = s.preview(2, d["state"])
    assert r["ok"] and r["changes"] == {} and set(r["vocal_gains"]) == {"1", "2"}

    st = d["state"]
    st["swap"] = _beats(s, 22)
    r = s.preview(2, st)
    assert r["ok"], r
    assert r["changes"] == {"swap_at": {"offset": {"beats": 6}}}
    assert r["plan_changes"] == {"schema_version": "0.8"}
    page, _ = sf.read(io.BytesIO(s.audio[r["version"]]), dtype="float32")
    assert s.save(2, st)["saved"]
    fresh = TuneSession(s.plan_path, tmp_path)
    full = NativeEngine().render(fresh.plan, tmp_path, fresh.grids).samples
    np.testing.assert_array_equal(page, full[it.start : it.end])


def test_swap_outside_the_window_is_refused(handoff_session) -> None:
    st = handoff_session.describe(2)["state"]
    st["swap"] = _beats(handoff_session, 26)
    assert not handoff_session.preview(2, st)["ok"]


def test_stem_lanes_save_and_play_exactly(handoff_session, tmp_path) -> None:
    s = handoff_session
    st = s.describe(2)["state"]
    st["lanes"]["1"]["vol@vocals"] = [[_beats(s, 4), 1, None], [_beats(s, 8), 0.5, None]]
    st["lanes"]["2"]["low@drums"] = [[_beats(s, 10), -12, None]]
    r = s.preview(2, st)
    assert r["ok"], r
    assert r["lane_changes"] == ["new lane: deck 1 vocals volume, 2 keyframes",
                                 "new lane: deck 2 drums low, 1 keyframes"]
    assert r["plan_changes"] == {"schema_version": "0.8"}
    page, _ = sf.read(io.BytesIO(s.audio[r["version"]]), dtype="float32")
    it = s.item(2)
    assert s.save(2, st)["saved"]
    text = s.plan_path.read_text()
    assert '"lane": "stem_volume", "deck": 1, "stem": "vocals"' in text
    assert '"lane": "eq", "deck": 2, "band": "low", "stem": "drums"' in text
    fresh = TuneSession(s.plan_path, tmp_path)
    full = NativeEngine().render(fresh.plan, tmp_path, fresh.grids).samples
    np.testing.assert_array_equal(page, full[it.start : it.end])
