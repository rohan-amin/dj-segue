"""`dj-segue tune`: adjust a plan's transitions by ear, then save in place.

A local page (stdlib HTTP server on 127.0.0.1 + `page.html`) shows one
transition at a time: both decks' waveforms over the blend, their gain
curves, and draggable fade windows with a curve per side.

The page never does engine maths. The decks are rendered once before gain
(`NativeEngine.render_decks`); every edit is sent here, validated with the
real validator, compiled to levels by the real engine (`levels`), and
mixed exactly as `render` mixes — the page plays those samples. Saving
rewrites only the transition's changed keys (`schema.jsonc_edit`), keeping
comments and formatting.

The decks render in a background thread after the page opens (it shows
progress); stretched audio is cached on disk, so a second launch is quick.

Items are crossfades and echo outs. For each, the page also edits the two
decks' EQ (low / mid / high) and filter lanes over the region: keyframes the
page didn't touch keep their exact text; moved ones keep their position's
form (an `after` stays an `after`); new ones are written like the
transition's `start_at`. New lanes are appended to `automation`.

T1 scope (docs/milestones.md, M4.5): windows inside the decks' existing
overlap. An edit that would move audio is refused for now.
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
from dj_segue.schema.jsonc_edit import DELETE, append_item, edit_object
from dj_segue.schema.plan import (
    AfterPos,
    BarPos,
    EqLane,
    FilterLane,
    Plan,
    SecondPos,
    TransitionSegment,
)
from dj_segue.time_math import (
    TrackGrid,
    duration_to_seconds,
    echo_window,
    mix_pos_to_seconds,
    transition_windows,
)

CURVES = ("equal_power", "linear", "exponential")
STYLES = ("crossfade", "echo_out")
PARAMS = ("low", "mid", "high", "filter")  # the lanes the page edits per deck
VERSIONS = ("0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7")
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
    style: str = "crossfade"


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
            if not isinstance(seg, TransitionSegment) or seg.style not in STYLES:
                continue
            o, n = transition_windows(seg, self.parts.mix_tempo, anchors)
            last = max(o.end_sec, n.end_sec)
            if seg.style == "echo_out":
                last = max(last, echo_window(seg, self.parts.mix_tempo, anchors).end_sec)
            ctx = CONTEXT_BEATS * self.spb
            start = max(0, self._smp(min(o.start_sec, n.start_sec) - ctx))
            end = min(self.parts.total_samples, self._smp(last + ctx))
            out.append(Item(i, start, end, seg.from_deck, seg.to_deck, seg.style))
        return out

    def label(self, it: Item) -> str:
        """`Like 'em All → Bottoms Up · echo out at beat 344` style."""
        name = lambda d: self.plan.decks[d].label or f"deck {d}"  # noqa: E731
        seg = self.plan.timeline[it.index]
        start = mix_pos_to_seconds(seg.start_at, self.parts.mix_tempo, self.parts.timeline.anchors)
        kind = "echo out" if it.style == "echo_out" else "crossfade"
        return (f"{name(it.from_deck)} → {name(it.to_deck)} · {kind} at mix beat "
                f"{round(start / self.spb, 1):g} (timeline[{it.index}])")

    def item(self, index: int) -> Item:
        for it in self.items():
            if it.index == index:
                return it
        raise KeyError(index)

    def describe(self, index: int) -> dict:
        """Everything the page draws for one transition."""
        it = self.item(index)
        seg = self.plan.timeline[index]
        anchors = self.parts.timeline.anchors
        o, n = transition_windows(seg, self.parts.mix_tempo, anchors)
        sr = self.parts.sample_rate
        state = {
            "out": {"start": o.start_sec, "end": o.end_sec, "curve": _curve_json(o.curve)},
            "in": {"start": n.start_sec, "end": n.end_sec, "curve": _curve_json(n.curve)},
            "lanes": self._lane_state((it.from_deck, it.to_deck)),
        }
        if seg.style == "echo_out":
            w = echo_window(seg, self.parts.mix_tempo, anchors)
            state["echo"] = {
                "end": w.end_sec,
                "every": _beats(w.every_sec, self.spb),
                "feedback": w.feedback,
                "highpass": w.highpass,
            }
        return {
            "index": index,
            "style": seg.style,
            "sample_rate": sr,
            "start_sec": it.start / sr,
            "end_sec": it.end / sr,
            "spb": self.spb,
            "zero_sec": o.start_sec,  # beat numbers on the page count from here
            "decks": [
                self._deck_info(it, it.from_deck, "out"),
                self._deck_info(it, it.to_deck, "in"),
            ],
            "state": state,
            "segment": self.raw["timeline"][index],
        }

    def _lane_index(self, deck: int, param: str) -> int | None:
        for i, lane in enumerate(self.plan.automation):
            if param == "filter" and isinstance(lane, FilterLane) and lane.deck == deck:
                return i
            if isinstance(lane, EqLane) and lane.deck == deck and lane.band == param:
                return i
        return None

    def _lane_state(self, decks) -> dict:
        """deck → param → [[mix sec, value, keyframe index], …] or None."""
        anchors = self.parts.timeline.anchors
        out: dict[str, dict] = {}
        for deck in decks:
            out[str(deck)] = {}
            for param in PARAMS:
                i = self._lane_index(deck, param)
                if i is None:
                    out[str(deck)][param] = None
                    continue
                lane = self.plan.automation[i]
                out[str(deck)][param] = [
                    [
                        mix_pos_to_seconds(kf.at, self.parts.mix_tempo, anchors),
                        kf.value if param == "filter" else kf.value_db,
                        j,
                    ]
                    for j, kf in enumerate(lane.keyframes)
                ]
        return out

    def _deck_info(self, it: Item, deck: int, side: str) -> dict:
        sr = self.parts.sample_rate
        peaks = _peaks(self.parts.decks[deck][it.start : it.end])
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
        """The transition's keys that `state` changes (DELETE = remove)."""
        if self.plan.timeline[index].style == "echo_out":
            return self._diff(index, self._echo_keys(index, state))
        return self._diff(index, self._crossfade_keys(index, state))

    def _crossfade_keys(self, index: int, state: dict) -> dict[str, Any]:
        """A crossfade's keys for `state` (DELETE = absent).

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
        return new

    def _echo_keys(self, index: int, state: dict) -> dict[str, Any]:
        """An echo_out's keys for `state`: the cut (`out.start`), the tail's
        end (`echo.end`), its every / feedback / highpass, and an `in` fade
        relative to the cut (none = the to_deck comes in on the cut)."""
        seg: TransitionSegment = self.plan.timeline[index]
        raw = self.raw["timeline"][index]
        anchors = self.parts.timeline.anchors
        spb = self.spb
        cut, n, e = state["out"]["start"], state["in"], state["echo"]
        new: dict[str, Any] = {}
        if abs(cut - mix_pos_to_seconds(seg.start_at, self.parts.mix_tempo, anchors)) > EPS:
            new["start_at"] = self._mix_position(seg.start_at, cut, anchors)
        new["duration"] = {"beats": _beats(e["end"] - cut, spb)}

        echo = dict(raw["echo"])
        if abs(_dur_beats(echo["every"], spb) - float(e["every"])) > 1e-6:
            echo["every"] = {"beats": _num(e["every"])}
        echo["feedback"] = _num(e["feedback"])
        if float(e.get("highpass", 0)) > 0:
            echo["highpass"] = _num(e["highpass"])
        else:
            echo.pop("highpass", None)
        new["echo"] = echo

        side_in: dict[str, Any] = {}
        if abs(n["start"] - cut) > EPS:
            side_in["offset"] = {"beats": _beats(n["start"] - cut, spb)}
        if n["end"] - n["start"] > EPS:
            side_in["duration"] = {"beats": _beats(n["end"] - n["start"], spb)}
            curve = _clean_curve(n["curve"] if n["curve"] != "step" else "equal_power")
            if curve != "equal_power":
                side_in["curve"] = curve
        new["in"] = side_in or DELETE
        return new

    def _diff(self, index: int, new: dict[str, Any]) -> dict[str, Any]:
        """Only what differs from the file; a duration in bars that still
        resolves the same stays as written."""
        raw = self.raw["timeline"][index]
        spb = self.spb
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

    def lane_changes(self, index: int, state: dict) -> tuple[dict[int, list], list[dict]]:
        """(edits, new_lanes) for the EQ / filter lanes in `state`:
        automation index → its new `keyframes`, and lanes to append.

        `state["lanes"]` = deck → param → [[mix sec, value, kf index | None], …]
        (None = no lane). Keyframes the page left alone keep their exact
        text; moved ones keep their position's form; new ones are written
        like the transition's `start_at`.
        """
        edits: dict[int, list] = {}
        new_lanes: list[dict] = []
        anchors = self.parts.timeline.anchors
        template = self.plan.timeline[index].start_at
        for deck_key, params in (state.get("lanes") or {}).items():
            deck = int(deck_key)
            for param, kfs in params.items():
                if param not in PARAMS:
                    raise ValueError(f"unknown lane {param!r}")
                i = self._lane_index(deck, param)
                if not kfs:
                    if i is not None:
                        raise ValueError(f"deck {deck} {param}: a lane needs a keyframe")
                    continue
                value_key = "value" if param == "filter" else "value_db"
                raw_kfs = self.raw["automation"][i]["keyframes"] if i is not None else []
                out = []
                for sec, value, j in sorted(kfs, key=lambda k: k[0]):
                    value = round(float(value), 2 if param == "filter" else 1)
                    if j is not None and i is not None:
                        orig = self.plan.automation[i].keyframes[j]
                        orig_sec = mix_pos_to_seconds(orig.at, self.parts.mix_tempo, anchors)
                        at = (raw_kfs[j]["at"] if abs(sec - orig_sec) < EPS
                              else self._mix_position(orig.at, sec, anchors))
                    else:
                        at = self._mix_position(template, sec, anchors)
                    out.append({"at": at, value_key: _num(value)})
                if i is None:
                    lane = {"lane": "filter", "deck": deck} if param == "filter" else {
                        "lane": "eq", "deck": deck, "band": param}
                    new_lanes.append({**lane, "keyframes": out})
                elif out != raw_kfs:
                    edits[i] = out
        return edits, new_lanes

    def plan_changes(
        self, state: dict, index: int | None = None, new_lanes: list[dict] = ()
    ) -> dict[str, Any]:
        """Top-level keys to change: the schema version the edit needs
        (drawn curves 0.5; a filter lane or echo.highpass 0.7)."""
        need = "0.1"
        if any(isinstance(state[side]["curve"], dict) for side in ("out", "in")):
            need = "0.5"
        if any(lane["lane"] == "filter" for lane in new_lanes) or float(
            (state.get("echo") or {}).get("highpass", 0)
        ) > 0:
            need = "0.7"
        if VERSIONS.index(need) > VERSIONS.index(self.raw["schema_version"]):
            return {"schema_version": need}
        return {}

    def _edit(self, index: int, state: dict):
        """Everything an edit changes: (segment changes, plan changes, lane
        edits, new lanes)."""
        changes = self.changes_for(index, state)
        edits, new_lanes = self.lane_changes(index, state)
        top = self.plan_changes(state, index, new_lanes)
        return changes, top, edits, new_lanes

    def _candidate(
        self,
        index: int,
        changes: dict[str, Any],
        top: dict[str, Any] | None = None,
        edits: dict[int, list] | None = None,
        new_lanes: list[dict] = (),
    ) -> tuple[dict, Plan]:
        raw = json.loads(json.dumps(self.raw))
        raw.update(top or {})
        seg = raw["timeline"][index]
        for key, value in changes.items():
            if value is DELETE:
                seg.pop(key, None)
            else:
                seg[key] = value
        for i, kfs in (edits or {}).items():
            raw["automation"][i]["keyframes"] = kfs
        if new_lanes:
            raw.setdefault("automation", []).extend(new_lanes)
        return raw, Plan.model_validate(raw)

    def preview(self, index: int, state: dict) -> dict:
        """Validate an edit, mix its region, and return what the page needs."""
        try:
            changes, top, edits, new_lanes = self._edit(index, state)
            raw, plan = self._candidate(index, changes, top, edits, new_lanes)
            validate_plan(plan, self.grids)
            validate_against_audio(plan, self.durations, self.grids)
            self._check_audio_unmoved(plan, index, state)
        except (ValidationError, PlanValidationError, ValueError) as e:
            issues = e.issues if isinstance(e, PlanValidationError) else [str(e)]
            return {"ok": False, "issues": issues}

        it = self.item(index)
        anchors = resolve_timeline(plan, self.parts.mix_tempo, self.grids).anchors
        levels = self.engine.levels(plan, self.parts, anchors)
        gains = levels.gains
        mixed = self.parts.mix(levels, it.start, it.end)
        buf = io.BytesIO()
        sf.write(buf, mixed, self.parts.sample_rate, format="WAV", subtype="FLOAT")
        with self._lock:
            self._version += 1
            version = self._version
            self.audio = {version: buf.getvalue()}  # keep only the latest
        step = self.parts.sample_rate // GAIN_RATE
        decks = (it.from_deck, it.to_deck)
        tail = np.zeros((it.end - it.start, 2), dtype=np.float32)
        for t in levels.tails:
            a, b = max(it.start, t.start), min(it.end, t.start + len(t.samples))
            if b > a:
                tail[a - it.start : b - it.start] += t.samples[a - t.start : b - t.start]
        return {
            "ok": True,
            "version": version,
            "changes": {k: (None if v is DELETE else v) for k, v in changes.items()},
            "plan_changes": top,
            "lane_changes": _lane_lines(raw, edits, new_lanes),
            "segment": raw["timeline"][index],
            "gains": {
                str(d): gains[d][it.start : it.end : step].round(4).tolist() for d in decks
            },
            "gain_rate": self.parts.sample_rate / step,
            # What each deck sounds like after EQ, filter and volume; and the
            # echo tail (drawn over the outgoing deck).
            "peaks": {
                str(d): _peaks(self.parts.signal(d, levels, it.start, it.end)) for d in decks
            },
            "tail_peaks": _peaks(tail) if levels.tails else None,
        }

    def _check_audio_unmoved(self, plan: Plan, index: int, state: dict) -> None:
        """T1: an edit may only change gains, inside where both decks play."""
        spans = resolve_timeline(plan, self.parts.mix_tempo, self.grids).spans
        before = [(s.mix_start_sec, s.mix_end_sec) for s in self.parts.timeline.spans]
        after = [(s.mix_start_sec, s.mix_end_sec) for s in spans]
        if any(abs(a - b) > EPS for x, y in zip(before, after) for a, b in zip(x, y)):
            raise ValueError("this edit moves audio (not supported yet: M4.5 T2)")
        it = self.item(index)
        echo = state.get("echo") if it.style == "echo_out" else None
        for side, deck in (("out", it.from_deck), ("in", it.to_deck)):
            w = dict(state[side])
            if side == "out" and echo is not None:
                # The echo repeats the `every` before the cut.
                w["start"] -= float(echo["every"]) * self.spb
            if not any(
                s.deck == deck
                and s.mix_start_sec - EPS <= w["start"]
                and w["end"] <= s.mix_end_sec + EPS
                for s in self.parts.timeline.spans
            ):
                what = "the echo's repeated beat" if side == "out" and echo else f"the {side} fade"
                raise ValueError(
                    f"{what} must stay where deck {deck} plays "
                    f"(moving a segment is M4.5 T2)"
                )
        if echo is not None and self._smp(float(echo["end"])) > self.parts.total_samples:
            raise ValueError("the echo tail can't run past the end of the mix here")

    def save(self, index: int, state: dict) -> dict:
        result = self.preview(index, state)
        if not result["ok"]:
            return result
        changes, top, edits, new_lanes = self._edit(index, state)
        text = self.text
        if changes:
            text = edit_object(text, ("timeline", index), changes)
        for i, kfs in edits.items():
            text = edit_object(text, ("automation", i), {"keyframes": kfs})
        if new_lanes and "automation" not in self.raw:
            text = edit_object(text, (), {"automation": []})
        for lane in new_lanes:
            text = append_item(text, ("automation",), lane)
        if top and text != self.text:
            text = edit_object(text, (), top)
        saved = text != self.text
        if saved:
            self.plan_path.write_text(text)
            self.text = text
            self.raw = jsonc.loads(text)
            self.plan = Plan.model_validate(self.raw)
        return {"ok": True, "saved": saved, "segment": self.raw["timeline"][index]}


def _peaks(x: np.ndarray) -> list[float]:
    mono = np.abs(x).max(axis=1)
    edges = np.linspace(0, len(mono), PEAK_BUCKETS + 1).astype(int)
    return [round(float(mono[a:b].max()), 4) if b > a else 0.0 for a, b in zip(edges, edges[1:])]


def _lane_lines(raw: dict, edits: dict[int, list], new_lanes: list[dict]) -> list[str]:
    """The lane edits, one line each, for the page."""
    lines = []
    for i, kfs in sorted(edits.items()):
        lane = raw["automation"][i]
        what = lane.get("band", lane["lane"])
        lines.append(f"automation[{i}] deck {lane['deck']} {what}: {len(kfs)} keyframes")
    for lane in new_lanes:
        what = lane.get("band", lane["lane"])
        lines.append(f"new lane: deck {lane['deck']} {what}, {len(lane['keyframes'])} keyframes")
    return lines


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
                        "label": session.label(it),
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
