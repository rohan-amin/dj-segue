"""`dj-segue tune`: adjust a plan's transitions by ear, then save in place.

A local page (stdlib HTTP server on 127.0.0.1 + `page.html`) shows one
transition at a time: both decks' waveforms over the blend, their gain
curves, and draggable fade windows with a curve per side.

The page never does engine maths. The decks are rendered once before gain
(`NativeEngine.render_decks`); every edit is sent here, validated with the
real validator, compiled to gains by the real engine (`deck_gains`), and
mixed exactly as `render` mixes — the page plays those samples. Saving
rewrites only the transition's changed keys (`schema.jsonc_edit`), keeping
comments and formatting.

The decks render in a background thread after the page opens (it shows
progress); stretched audio is cached on disk, so a second launch is quick.

T1 scope (docs/milestones.md, M4.5): windows inside the decks' existing
overlap, preset curves. An edit that would move audio is refused for now.
"""

from __future__ import annotations

import io
import json
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from pydantic import ValidationError

from dj_segue.compiler import resolve_timeline
from dj_segue.executor.native import NativeEngine
from dj_segue.executor.native.engine import DeckRender
from dj_segue.schema import PlanValidationError, jsonc, validate_against_audio, validate_plan
from dj_segue.schema.jsonc_edit import DELETE, edit_object
from dj_segue.schema.plan import (
    AfterPos,
    BarPos,
    Plan,
    SecondPos,
    TransitionSegment,
)
from dj_segue.time_math import (
    TrackGrid,
    duration_to_seconds,
    mix_pos_to_seconds,
    transition_windows,
)

CURVES = ("equal_power", "linear", "exponential")
CONTEXT_BEATS = 16  # shown either side of a transition
PEAK_BUCKETS = 2400
GAIN_RATE = 100  # Hz, for drawing the gain curves
EPS = 1e-6  # seconds


@dataclass
class Item:
    """One transition being tuned: its region of the mix, in samples."""

    index: int  # into plan.timeline
    start: int
    end: int
    from_deck: int
    to_deck: int


class TuneSession:
    def __init__(self, plan_path: Path, audio_root: Path) -> None:
        from dj_segue.preprocessor import preprocess as run_preprocess

        self.plan_path = Path(plan_path)
        self.audio_root = Path(audio_root)
        self.engine = NativeEngine()
        self.text = self.plan_path.read_text()
        self.raw = jsonc.loads(self.text)
        self.plan = Plan.model_validate(self.raw)
        pre = run_preprocess(self.plan, self.audio_root)
        self.grids: dict[str, TrackGrid] = pre.grids()
        self.durations = {tid: ta.primary.duration_sec for tid, ta in pre.tracks.items()}
        validate_plan(self.plan, self.grids)
        self.parts: DeckRender | None = None  # set by load()
        self.progress = {"done": 0, "total": 0, "ready": False, "error": None}
        self.audio: dict[int, bytes] = {}  # preview version → WAV
        self._version = 0
        self._lock = threading.Lock()

    def load(self) -> None:
        """Render the decks (the slow part: time-stretching, cached on disk)."""

        def on_progress(done: int, total: int) -> None:
            self.progress.update(done=done, total=total)

        try:
            self.parts = self.engine.render_decks(
                self.plan, self.audio_root, self.grids, on_progress
            )
            self.progress["ready"] = True
        except Exception as e:  # shown on the page and in the terminal
            self.progress["error"] = str(e)
            raise

    # -- time ----------------------------------------------------------

    @property
    def spb(self) -> float:
        """Seconds per mix beat."""
        return 60.0 / self.parts.mix_tempo

    def _smp(self, sec: float) -> int:
        return int(round(sec * self.parts.sample_rate))

    # -- items ---------------------------------------------------------

    def items(self) -> list[Item]:
        out = []
        anchors = self.parts.timeline.anchors
        for i, seg in enumerate(self.plan.timeline):
            if not isinstance(seg, TransitionSegment) or seg.style != "crossfade":
                continue
            o, n = transition_windows(seg, self.parts.mix_tempo, anchors)
            ctx = CONTEXT_BEATS * self.spb
            start = max(0, self._smp(min(o.start_sec, n.start_sec) - ctx))
            end = min(self.parts.total_samples, self._smp(max(o.end_sec, n.end_sec) + ctx))
            out.append(Item(i, start, end, seg.from_deck, seg.to_deck))
        return out

    def item(self, index: int) -> Item:
        for it in self.items():
            if it.index == index:
                return it
        raise KeyError(index)

    def describe(self, index: int) -> dict:
        """Everything the page draws for one transition."""
        it = self.item(index)
        seg = self.plan.timeline[index]
        o, n = transition_windows(seg, self.parts.mix_tempo, self.parts.timeline.anchors)
        sr = self.parts.sample_rate
        return {
            "index": index,
            "sample_rate": sr,
            "start_sec": it.start / sr,
            "end_sec": it.end / sr,
            "spb": self.spb,
            "zero_sec": o.start_sec,  # beat numbers on the page count from here
            "decks": [
                self._deck_info(it, it.from_deck, "out"),
                self._deck_info(it, it.to_deck, "in"),
            ],
            "state": {
                "out": {"start": o.start_sec, "end": o.end_sec, "curve": _curve_json(o.curve)},
                "in": {"start": n.start_sec, "end": n.end_sec, "curve": _curve_json(n.curve)},
            },
            "segment": self.raw["timeline"][index],
        }

    def _deck_info(self, it: Item, deck: int, side: str) -> dict:
        sr = self.parts.sample_rate
        buf = self.parts.decks[deck][it.start : it.end]
        mono = np.abs(buf).max(axis=1)
        edges = np.linspace(0, len(mono), PEAK_BUCKETS + 1).astype(int)
        peaks = [float(mono[a:b].max()) if b > a else 0.0 for a, b in zip(edges, edges[1:])]
        spans = [
            (max(sp.mix_start_sec, it.start / sr), min(sp.mix_end_sec, it.end / sr), sp.track_id)
            for sp in self.parts.timeline.spans
            if sp.deck == deck and sp.mix_end_sec > it.start / sr and sp.mix_start_sec < it.end / sr
        ]
        return {
            "deck": deck,
            "side": side,
            "label": self.plan.decks[deck].label or f"deck {deck}",
            "peaks": peaks,
            "plays": [{"start": a, "end": b, "track": t} for a, b, t in spans],
        }

    # -- edits ---------------------------------------------------------

    def changes_for(self, index: int, state: dict) -> dict[str, Any]:
        """The transition's keys that `state` changes (DELETE = remove).

        `state` = {"out": {start, end, curve}, "in": {…}} in mix seconds;
        a curve is a preset name or a drawn {"points": [[t, gain], …],
        "smooth": bool}. The out side sets `start_at` / `duration` (in the
        start_at's own form: an `after` stays an `after`); the in side is an
        `in` override relative to it; equal preset curves become one `curve`;
        drawn curves always go on their side.
        """
        seg: TransitionSegment = self.plan.timeline[index]
        raw = self.raw["timeline"][index]
        anchors = self.parts.timeline.anchors
        spb = self.spb
        o, n = state["out"], state["in"]
        for side in (o, n):
            side["curve"] = _clean_curve(side["curve"])
        new: dict[str, Any] = {}

        orig_start = mix_pos_to_seconds(seg.start_at, self.parts.mix_tempo, anchors)
        if abs(o["start"] - orig_start) > EPS:
            new["start_at"] = self._mix_position(seg.start_at, o["start"], anchors)
        out_len = o["end"] - o["start"]
        if abs(out_len - duration_to_seconds(seg.duration, self.parts.mix_tempo)) > EPS:
            new["duration"] = {"beats": _beats(out_len, spb)}

        side_in: dict[str, Any] = {}
        if abs(n["start"] - o["start"]) > EPS:
            side_in["offset"] = {"beats": _beats(n["start"] - o["start"], spb)}
        if abs((n["end"] - n["start"]) - out_len) > EPS:
            side_in["duration"] = {"beats": _beats(n["end"] - n["start"], spb)}
        side_out: dict[str, Any] = {}
        if isinstance(o["curve"], str) and o["curve"] == n["curve"]:
            new["curve"] = o["curve"] if o["curve"] != "equal_power" else DELETE
        else:
            new["curve"] = DELETE
            if o["curve"] != "equal_power":
                side_out["curve"] = o["curve"]
            if n["curve"] != "equal_power":
                side_in["curve"] = n["curve"]
        new["in"] = side_in or DELETE
        new["out"] = side_out or DELETE

        # Only what differs from the file; a duration in bars that still
        # resolves the same stays as written.
        changes = {}
        for key, value in new.items():
            if value is DELETE:
                if key in raw:
                    changes[key] = DELETE
            elif raw.get(key) != value:
                if key == "duration" and "duration" in raw and abs(
                    _dur_beats(raw["duration"], spb) - value["beats"]
                ) < 1e-6:
                    continue
                changes[key] = value
        return changes

    def _mix_position(self, orig, sec: float, anchors: dict[str, float]) -> dict:
        spb = self.spb
        if isinstance(orig, AfterPos):
            off = _beats(sec - anchors[orig.after], spb)
            return {"after": orig.after, **({"offset": {"beats": off}} if off else {})}
        if isinstance(orig, BarPos):
            return {"bar": _beats(sec, spb) / 4}
        if isinstance(orig, SecondPos):
            return {"second": round(sec, 6)}
        return {"beat": _beats(sec, spb)}

    def plan_changes(self, state: dict) -> dict[str, Any]:
        """Top-level keys to change: a drawn curve needs schema 0.5."""
        drawn = any(not isinstance(state[side]["curve"], str) for side in ("out", "in"))
        if drawn and self.raw["schema_version"] in ("0.1", "0.2", "0.3", "0.4"):
            return {"schema_version": "0.5"}
        return {}

    def _candidate(
        self, index: int, changes: dict[str, Any], top: dict[str, Any] | None = None
    ) -> tuple[dict, Plan]:
        raw = json.loads(json.dumps(self.raw))
        raw.update(top or {})
        seg = raw["timeline"][index]
        for key, value in changes.items():
            if value is DELETE:
                seg.pop(key, None)
            else:
                seg[key] = value
        return raw, Plan.model_validate(raw)

    def preview(self, index: int, state: dict) -> dict:
        """Validate an edit, mix its region, and return what the page needs."""
        try:
            changes = self.changes_for(index, state)
            top = self.plan_changes(state)
            raw, plan = self._candidate(index, changes, top)
            validate_plan(plan, self.grids)
            validate_against_audio(plan, self.durations, self.grids)
            self._check_audio_unmoved(plan, index, state)
        except (ValidationError, PlanValidationError, ValueError) as e:
            issues = e.issues if isinstance(e, PlanValidationError) else [str(e)]
            return {"ok": False, "issues": issues}

        it = self.item(index)
        anchors = resolve_timeline(plan, self.parts.mix_tempo, self.grids).anchors
        gains = self.engine.deck_gains(plan, self.parts, anchors)
        mixed = self.parts.mix(gains, it.start, it.end)
        buf = io.BytesIO()
        sf.write(buf, mixed, self.parts.sample_rate, format="WAV", subtype="FLOAT")
        with self._lock:
            self._version += 1
            version = self._version
            self.audio = {version: buf.getvalue()}  # keep only the latest
        step = self.parts.sample_rate // GAIN_RATE
        return {
            "ok": True,
            "version": version,
            "changes": {k: (None if v is DELETE else v) for k, v in changes.items()},
            "plan_changes": top,
            "segment": raw["timeline"][index],
            "gains": {
                str(d): gains[d][it.start : it.end : step].round(4).tolist()
                for d in (it.from_deck, it.to_deck)
            },
            "gain_rate": self.parts.sample_rate / step,
        }

    def _check_audio_unmoved(self, plan: Plan, index: int, state: dict) -> None:
        """T1: an edit may only change gains, inside where both decks play."""
        spans = resolve_timeline(plan, self.parts.mix_tempo, self.grids).spans
        before = [(s.mix_start_sec, s.mix_end_sec) for s in self.parts.timeline.spans]
        after = [(s.mix_start_sec, s.mix_end_sec) for s in spans]
        if any(abs(a - b) > EPS for x, y in zip(before, after) for a, b in zip(x, y)):
            raise ValueError("this edit moves audio (not supported yet: M4.5 T2)")
        it = self.item(index)
        for side, deck in (("out", it.from_deck), ("in", it.to_deck)):
            w = state[side]
            if not any(
                s.deck == deck
                and s.mix_start_sec - EPS <= w["start"]
                and w["end"] <= s.mix_end_sec + EPS
                for s in self.parts.timeline.spans
            ):
                raise ValueError(
                    f"the {side} fade must stay where deck {deck} plays "
                    f"(moving a segment is M4.5 T2)"
                )

    def save(self, index: int, state: dict) -> dict:
        result = self.preview(index, state)
        if not result["ok"]:
            return result
        changes = self.changes_for(index, state)
        top = self.plan_changes(state)
        if changes:
            text = edit_object(self.text, ("timeline", index), changes)
            if top:
                text = edit_object(text, (), top)
            self.plan_path.write_text(text)
            self.text = text
            self.raw = jsonc.loads(text)
            self.plan = Plan.model_validate(self.raw)
        return {"ok": True, "saved": bool(changes), "segment": self.raw["timeline"][index]}


def _curve_json(curve) -> Any:
    """A FadeWindow curve for the page: a preset name, or a drawn curve dict."""
    if isinstance(curve, str):
        return curve
    return {"points": [list(p) for p in curve.points], "smooth": curve.smooth}


def _clean_curve(curve: Any) -> Any:
    """Check a curve from the page; round drawn points to 4 decimals."""
    if isinstance(curve, str):
        if curve not in CURVES:
            raise ValueError(f"unknown curve {curve!r}")
        return curve
    if not isinstance(curve, dict) or not isinstance(curve.get("points"), list):
        raise ValueError("a drawn curve needs a `points` list")
    points = [[_num(t), _num(g)] for t, g in curve["points"]]
    out: dict[str, Any] = {"points": points}
    if curve.get("smooth"):
        out["smooth"] = True
    return out


def _num(x: float) -> float:
    v = round(float(x), 4)
    return int(v) if v.is_integer() else v


def _beats(sec: float, spb: float) -> float:
    b = round(sec / spb, 4)
    return int(b) if float(b).is_integer() else b


def _dur_beats(raw: dict, spb: float) -> float:
    if "beats" in raw:
        return raw["beats"]
    if "bars" in raw:
        return raw["bars"] * 4
    return raw["seconds"] / spb


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def make_server(session: TuneSession, port: int = 0) -> ThreadingHTTPServer:
    page = resources.files(__package__).joinpath("page.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/":
                self._send(page, "text/html; charset=utf-8")
            elif path == "/status":
                self._json({"plan": session.plan_path.name, **session.progress})
            elif not session.progress["ready"]:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "still rendering")
            elif path == "/items":
                items = [
                    {
                        "index": it.index,
                        "label": f"timeline[{it.index}]: deck {it.from_deck} → deck {it.to_deck}",
                    }
                    for it in session.items()
                ]
                self._json({"plan": session.plan_path.name, "items": items})
            elif path.startswith("/item/"):
                try:
                    self._json(session.describe(int(path.rsplit("/", 1)[1])))
                except (ValueError, KeyError):
                    self.send_error(HTTPStatus.NOT_FOUND)
            elif path.startswith("/audio/"):
                body = session.audio.get(int(path.rsplit("/", 1)[1]))
                if body is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    self._send(body, "audio/wav")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not session.progress["ready"]:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "still rendering")
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                index, state = int(body["index"]), body["state"]
            except (ValueError, KeyError, TypeError):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if self.path == "/preview":
                self._json(session.preview(index, state))
            elif self.path == "/save":
                self._json(session.save(index, state))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _json(self, obj: Any) -> None:
            self._send(json.dumps(obj).encode(), "application/json")

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(session: TuneSession, port: int = 0, open_browser: bool = True) -> None:
    import webbrowser

    server = make_server(session, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"tune: {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()

    def load() -> None:
        try:
            session.load()
            print("tune: ready", flush=True)
        except Exception as e:
            print(f"tune: rendering failed: {e}", flush=True)

    if not session.progress["ready"]:
        threading.Thread(target=load, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
