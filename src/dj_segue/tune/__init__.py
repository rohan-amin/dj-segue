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
progress).

Playback (R2, tune/live.py) is the whole mix, live, through this machine's
speakers: the page sends play / stop / loop, and each edit that validates
is handed to the playing engine, so it's heard within a moment. The
excerpt each preview mixes is what the page draws from (and what tests
compare with a render).

Each deck's lane also shows what plays on it (M4.5 T9): where its segments
start ("→ cue: everybody", a jump), loops with their repetitions, and the
cues marked in the track's annotation. A loop's repetitions and length can
be changed here: that moves audio, so it's written to the plan straight
away and the decks re-render (`set_loop`).

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

from dj_segue.compiler import VOCAL_SWAP_SEC, resolve_timeline
from dj_segue.executor.native import NativeEngine
from dj_segue.executor.native.engine import DeckRender, prepare
from dj_segue.executor.native.live import LiveEngine
from dj_segue.schema import PlanValidationError, jsonc, validate_against_audio, validate_plan
from dj_segue.schema.jsonc_edit import DELETE, append_item, edit_object
from dj_segue.schema.version import SUPPORTED_SCHEMA_VERSIONS
from dj_segue.tune.live import TuneLive
from dj_segue.schema.plan import (
    AfterPos,
    BarPos,
    EqLane,
    FilterLane,
    Plan,
    StemVolumeLane,
    SecondPos,
    TransitionSegment,
)
from dj_segue.time_math import (
    TempoMap,
    TrackGrid,
    echo_window,
    handoff_swap,
    mix_pos_to_seconds,
    transition_windows,
)

CURVES = ("equal_power", "linear", "exponential")
STYLES = ("crossfade", "echo_out", "vocal_handoff")
PARAMS = ("low", "mid", "high", "filter")  # the lanes the page edits per deck
# Schema versions, oldest first (for bumping a plan to what an edit needs).
VERSIONS = tuple(sorted(SUPPORTED_SCHEMA_VERSIONS, key=lambda v: tuple(map(int, v.split(".")))))
CONTEXT_BEATS = 16  # shown either side of a transition
PEAK_BUCKETS = 2400
GAIN_RATE = 100  # Hz, for drawing the gain curves
EPS = 1e-6  # seconds
# Segments this close are back to back; a fade may run up to this past where
# a deck plays (cue times are stored to 0.1 ms, so not exactly on a beat).
JOIN_SEC = 0.001


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
        self.live: TuneLive | None = None  # set by load()
        self._audio_cache: dict = {}  # track audio for live engines
        self._cues: dict[str, dict] = {}  # track → cues marked in its annotation

    def load(self) -> None:
        """Render the decks (the slow part: time-stretching, cached on disk)."""

        def on_progress(done: int, total: int) -> None:
            self.progress.update(done=done, total=total)

        try:
            self.parts = self.engine.render_decks(
                self.plan, self.audio_root, self.grids, on_progress
            )
            self.live = TuneLive(self._live_engine, self.plan, self.parts.sample_rate)
            self.progress["ready"] = True
        except Exception as e:  # shown on the page and in the terminal
            self.progress["error"] = str(e)
            raise

    def _live_engine(self, plan: Plan, start: int) -> LiveEngine:
        p = prepare(plan, self.audio_root, self.grids, audio_cache=self._audio_cache)
        return LiveEngine(p, start=start)

    # -- time ----------------------------------------------------------

    @property
    def tempo(self) -> TempoMap:
        return self.parts.mix_tempo

    def _beats(self, a: float, b: float) -> float:
        """Mix beats from second `a` to `b` (through the tempo map), rounded
        as written to the plan."""
        return _round_beats(self.tempo.sec_to_beat(b) - self.tempo.sec_to_beat(a))

    def _dur_beats(self, raw: dict, start: float) -> float:
        """A plan duration (as written) in mix beats, counted from `start`."""
        if "beats" in raw:
            return raw["beats"]
        if "bars" in raw:
            return raw["bars"] * 4
        return self.tempo.sec_to_beat(start + raw["seconds"]) - self.tempo.sec_to_beat(start)

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
            if seg.style == "vocal_handoff":
                last = max(last, handoff_swap(seg, self.parts.mix_tempo, anchors))
            first = min(o.start_sec, n.start_sec)
            start = max(0, self._smp(self.tempo.advance(first, -CONTEXT_BEATS)))
            end = min(self.parts.total_samples, self._smp(self.tempo.advance(last, CONTEXT_BEATS)))
            out.append(Item(i, start, end, seg.from_deck, seg.to_deck, seg.style))
        return out

    def deck_name(self, deck: int) -> str:
        return self.plan.decks[deck].label or f"deck {deck}"

    def label(self, it: Item) -> str:
        """`Like 'em All → Bottoms Up · echo out at beat 344` style."""
        seg = self.plan.timeline[it.index]
        start = mix_pos_to_seconds(seg.start_at, self.parts.mix_tempo, self.parts.timeline.anchors)
        kind = {"echo_out": "echo out", "vocal_handoff": "vocal handoff"}.get(it.style, "crossfade")
        return (f"{self.deck_name(it.from_deck)} → {self.deck_name(it.to_deck)} · {kind} "
                f"at beat {round(self.tempo.sec_to_beat(start), 1):g}")

    def detail(self, it: Item) -> str:
        """Where the item is in the plan file (the page's tooltip)."""
        return (f"timeline[{it.index}] in {self.plan_path.name}: deck {it.from_deck} → "
                f"deck {it.to_deck}; beats are mix beats from the start of the mix")

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
        if seg.style == "vocal_handoff":
            state["swap"] = handoff_swap(seg, self.parts.mix_tempo, anchors)
        if seg.style == "echo_out":
            w = echo_window(seg, self.parts.mix_tempo, anchors)
            state["echo"] = {
                "end": w.end_sec,
                "every": _round_beats(w.every_sec * self.tempo.bpm_before(w.cut_sec) / 60.0),
                "feedback": w.feedback,
                "highpass": w.highpass,
            }
        return {
            "index": index,
            "style": seg.style,
            "sample_rate": sr,
            "start_sec": it.start / sr,
            "end_sec": it.end / sr,
            "zero_sec": o.start_sec,  # beat numbers on the page count from here
            # Mix second of every quarter beat over the item, from quarter
            # beat `grid_first` (counted from zero_sec): the page's beat
            # numbers and snapping follow the tempo map through it.
            **self._grid(it, o.start_sec),
            "decks": [
                self._deck_info(it, it.from_deck, "out"),
                self._deck_info(it, it.to_deck, "in"),
            ],
            "state": state,
            "segment": self.raw["timeline"][index],
            # Decks mixed by stems → their stems (the page's stem lanes).
            "stems": {str(d): list(self.parts.stems.get(d, {}))
                      for d in (it.from_deck, it.to_deck)},
        }

    def _grid(self, it: Item, zero: float) -> dict:
        tm, sr = self.tempo, self.parts.sample_rate
        z = tm.sec_to_beat(zero)
        q0 = int(np.floor((tm.sec_to_beat(it.start / sr) - z) * 4)) - 1
        q1 = int(np.ceil((tm.sec_to_beat(it.end / sr) - z) * 4)) + 1
        return {
            "grid_first": q0,
            "grid": [round(tm.beat_to_sec(z + q / 4), 6) for q in range(q0, q1 + 1)],
        }

    def _lane_index(self, deck: int, param: str) -> int | None:
        kind, band, stem = _param(param)
        for i, lane in enumerate(self.plan.automation):
            if getattr(lane, "deck", None) != deck:  # the crossfader has no deck
                continue
            if kind == "filter" and isinstance(lane, FilterLane):
                return i
            if kind == "eq" and isinstance(lane, EqLane) and lane.band == band and lane.stem == stem:
                return i
            if kind == "vol" and isinstance(lane, StemVolumeLane) and lane.stem == stem:
                return i
        return None

    def _params(self, deck: int) -> list[str]:
        """The lanes the page can edit on `deck`: EQ and filter, and on a
        deck mixed by stems each stem's volume and EQ."""
        out = list(PARAMS)
        for stem in self.parts.stems.get(deck, {}):
            out += [f"{p}@{stem}" for p in ("vol", "low", "mid", "high")]
        return out

    def _lane_state(self, decks) -> dict:
        """deck → param → [[mix sec, value, keyframe index], …] or None.
        Params: "low" / "mid" / "high" / "filter", and "vol@<stem>",
        "low@<stem>" … on decks mixed by stems."""
        anchors = self.parts.timeline.anchors
        out: dict[str, dict] = {}
        for deck in decks:
            out[str(deck)] = {}
            for param in self._params(deck):
                i = self._lane_index(deck, param)
                if i is None:
                    out[str(deck)][param] = None
                    continue
                lane = self.plan.automation[i]
                out[str(deck)][param] = [
                    [
                        mix_pos_to_seconds(kf.at, self.parts.mix_tempo, anchors),
                        kf.value_db if _param(param)[0] == "eq" else kf.value,
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
            "segments": self._segments(it, deck),
            "cues": self._cue_marks(it, deck),
        }

    # -- what plays on a deck (T9) ------------------------------------------

    def _deck_spans(self, it: Item, deck: int):
        sr = self.parts.sample_rate
        return [sp for sp in self.parts.timeline.spans
                if sp.deck == deck and sp.kind != "silence"
                and sp.mix_end_sec > it.start / sr and sp.mix_start_sec < it.end / sr]

    def _segments(self, it: Item, deck: int) -> list[dict]:
        """Each play / loop on the deck in view: where it starts and ends in
        the mix, where in its track it starts, and a loop's repetitions."""
        from dj_segue.inspect.pretty import _fmt_track_position

        out = []
        for sp in self._deck_spans(it, deck):
            seg = self.plan.timeline[sp.index]
            d = {
                "index": sp.index, "kind": sp.kind, "id": seg.id,
                "start": sp.mix_start_sec, "end": sp.mix_end_sec,
                "from": _fmt_track_position(seg.from_),
            }
            if sp.kind == "loop":
                steps = self.raw["timeline"][sp.index]["schedule"]
                d["reps"] = [r.mix_start_sec for r in sp.reps]
                d["schedule"] = [
                    {"beats": _track_beats(st["length"], self.grids.get(sp.track_id)),
                     "repetitions": st["repetitions"]}
                    for st in steps
                ]
            out.append(d)
        return out

    def _cue_marks(self, it: Item, deck: int) -> list[dict]:
        """The track's annotation cues where the deck plays them (a loop's
        first pass)."""
        out = []
        for sp in self._deck_spans(it, deck):
            cues = self._marked_cues(sp.track_id)
            if not cues:
                continue
            if sp.kind == "loop":
                t0, t1 = sp.track_from_sec, sp.track_from_sec + sp.reps[0].track_len_sec
                to_mix = lambda c, sp=sp: sp.mix_start_sec + (c - sp.track_from_sec) / sp.rate  # noqa: E731
            elif sp.warp is not None:
                t0, t1 = sp.track_from_sec, sp.warp.track_sec(sp.mix_end_sec)
                to_mix = sp.warp.mix_sec
            else:
                t0 = sp.track_from_sec
                t1 = t0 + (sp.mix_end_sec - sp.mix_start_sec) * sp.rate
                to_mix = lambda c, sp=sp: sp.mix_start_sec + (c - sp.track_from_sec) / sp.rate  # noqa: E731
            for name, sec in cues.items():
                at = float(to_mix(sec))
                if t0 - EPS <= sec <= t1 + EPS and not any(
                        c["name"] == name and abs(c["at"] - at) < 1e-3 for c in out):
                    out.append({"name": name, "at": at})  # (once at a boundary)
        return out

    def _marked_cues(self, track_id: str) -> dict:
        if track_id not in self._cues:
            from dj_segue.annotate import annotation_cues

            track = self.plan.tracks[track_id]
            cues = {}
            if "full" in track.stems:
                audio = (self.audio_root / track.stems["full"]).resolve()
                cues = (annotation_cues(audio, self.audio_root / "annotations")
                        or annotation_cues(audio) or {})
            self._cues[track_id] = cues
        return self._cues[track_id]

    def set_loop(self, index: int, beats: float, repetitions: int) -> dict:
        """Change a single-step loop's length (track beats) and repetitions:
        written to the plan file at once (it moves audio), then the decks
        re-render. The page reloads when they're ready."""
        seg = self.plan.timeline[index]
        raw = self.raw["timeline"][index]
        if seg.type != "loop" or len(raw["schedule"]) != 1:
            return {"ok": False, "issues": ["Only a loop with one step can be changed here; edit the plan for the rest."]}
        if repetitions < 1 or beats <= 0:
            return {"ok": False, "issues": ["A loop needs at least one repetition and a positive length."]}
        step = {"length": {"beats": _num(beats)}, "repetitions": int(repetitions)}
        text = edit_object(self.text, ("timeline", index), {"schedule": [step]})
        try:
            raw2 = jsonc.loads(text)
            plan = Plan.model_validate(raw2)
            validate_plan(plan, self.grids)
            validate_against_audio(plan, self.durations, self.grids)
        except (ValidationError, PlanValidationError, ValueError) as e:
            issues = e.issues if isinstance(e, PlanValidationError) else [str(e)]
            return {"ok": False, "issues": issues}
        if self.live is not None:
            self.live.stop()
        self.plan_path.write_text(text)
        self.text, self.raw, self.plan = text, raw2, plan
        self.progress = {"done": 0, "total": 0, "ready": False, "error": None}
        threading.Thread(target=self._reload, daemon=True).start()
        return {"ok": True}

    def _reload(self) -> None:
        try:
            self.load()
        except Exception:
            pass  # reported through progress["error"]

    # -- edits ---------------------------------------------------------

    def changes_for(self, index: int, state: dict) -> dict[str, Any]:
        """The transition's keys that `state` changes (DELETE = remove)."""
        style = self.plan.timeline[index].style
        if style == "echo_out":
            return self._diff(index, self._echo_keys(index, state))
        keys = self._crossfade_keys(index, state)
        if style == "vocal_handoff":
            # `swap_at` relative to the (possibly moved) start; left out while
            # it's the default (halfway) and the file has none.
            o = state["out"]
            offset = state["swap"] - o["start"]
            if ("swap_at" in self.raw["timeline"][index]
                    or abs(offset - (o["end"] - o["start"]) / 2) > EPS):
                keys["swap_at"] = {"offset": {"beats": self._beats(o["start"], state["swap"])}}
            else:
                keys["swap_at"] = DELETE
        return self._diff(index, keys)

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
        o, n = state["out"], state["in"]
        for side in (o, n):
            side["curve"] = _clean_curve(side["curve"])
        new: dict[str, Any] = {}

        orig_start = mix_pos_to_seconds(seg.start_at, self.parts.mix_tempo, anchors)
        if abs(o["start"] - orig_start) > EPS:
            new["start_at"] = self._mix_position(seg.start_at, o["start"], anchors)
        out_beats = self._beats(o["start"], o["end"])
        if abs(out_beats - self._dur_beats(raw["duration"], orig_start)) > 1e-6:
            new["duration"] = {"beats": out_beats}

        side_in: dict[str, Any] = {}
        if abs(n["start"] - o["start"]) > EPS:
            side_in["offset"] = {"beats": self._beats(o["start"], n["start"])}
        if abs(self._beats(n["start"], n["end"]) - out_beats) > 1e-6:
            side_in["duration"] = {"beats": self._beats(n["start"], n["end"])}
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
        cut, n, e = state["out"]["start"], state["in"], state["echo"]
        new: dict[str, Any] = {}
        if abs(cut - mix_pos_to_seconds(seg.start_at, self.parts.mix_tempo, anchors)) > EPS:
            new["start_at"] = self._mix_position(seg.start_at, cut, anchors)
        new["duration"] = {"beats": self._beats(cut, e["end"])}

        echo = dict(raw["echo"])
        if abs(self._dur_beats(echo["every"], cut) - float(e["every"])) > 1e-6:
            echo["every"] = {"beats": _num(e["every"])}
        echo["feedback"] = _num(e["feedback"])
        if float(e.get("highpass", 0)) > 0:
            echo["highpass"] = _num(e["highpass"])
        else:
            echo.pop("highpass", None)
        new["echo"] = echo

        side_in: dict[str, Any] = {}
        if abs(n["start"] - cut) > EPS:
            side_in["offset"] = {"beats": self._beats(cut, n["start"])}
        if n["end"] - n["start"] > EPS:
            side_in["duration"] = {"beats": self._beats(n["start"], n["end"])}
            curve = _clean_curve(n["curve"] if n["curve"] != "step" else "equal_power")
            if curve != "equal_power":
                side_in["curve"] = curve
        new["in"] = side_in or DELETE
        return new

    def _diff(self, index: int, new: dict[str, Any]) -> dict[str, Any]:
        """Only what differs from the file; a duration in bars that still
        resolves the same stays as written."""
        raw = self.raw["timeline"][index]
        seg = self.plan.timeline[index]
        start = mix_pos_to_seconds(seg.start_at, self.tempo, self.parts.timeline.anchors)
        changes = {}
        for key, value in new.items():
            if value is DELETE:
                if key in raw:
                    changes[key] = DELETE
            elif raw.get(key) != value:
                if key == "duration" and "duration" in raw and abs(
                    self._dur_beats(raw["duration"], start) - value["beats"]
                ) < 1e-6:
                    continue
                changes[key] = value
        return changes

    def _mix_position(self, orig, sec: float, anchors: dict[str, float]) -> dict:
        if isinstance(orig, AfterPos):
            off = self._beats(anchors[orig.after], sec)
            return {"after": orig.after, **({"offset": {"beats": off}} if off else {})}
        if isinstance(orig, BarPos):
            return {"bar": self._beats(0.0, sec) / 4}
        if isinstance(orig, SecondPos):
            return {"second": round(sec, 6)}
        return {"beat": self._beats(0.0, sec)}

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
                if param not in self._params(deck):
                    kind, _, stem = _param(param)
                    if stem is not None:
                        raise ValueError(
                            f"{self.deck_name(deck)} isn't split into stems in this session. "
                            f"Add a stem lane for it to the plan and reopen tune."
                        )
                    raise ValueError(f"unknown lane {param!r}")
                kind, band, stem = _param(param)
                i = self._lane_index(deck, param)
                if not kfs:
                    if i is not None:
                        raise ValueError("A lane needs at least one keyframe.")
                    continue
                value_key = "value_db" if kind == "eq" else "value"
                raw_kfs = self.raw["automation"][i]["keyframes"] if i is not None else []
                out = []
                for sec, value, j in sorted(kfs, key=lambda k: k[0]):
                    value = round(float(value), 1 if kind == "eq" else 2)
                    if j is not None and i is not None:
                        orig = self.plan.automation[i].keyframes[j]
                        orig_sec = mix_pos_to_seconds(orig.at, self.parts.mix_tempo, anchors)
                        at = (raw_kfs[j]["at"] if abs(sec - orig_sec) < EPS
                              else self._mix_position(orig.at, sec, anchors))
                    else:
                        at = self._mix_position(template, sec, anchors)
                    out.append({"at": at, value_key: _num(value)})
                if i is None:
                    if kind == "filter":
                        lane = {"lane": "filter", "deck": deck}
                    elif kind == "vol":
                        lane = {"lane": "stem_volume", "deck": deck, "stem": stem}
                    else:
                        lane = {"lane": "eq", "deck": deck, "band": band}
                        if stem:
                            lane["stem"] = stem
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
        shaped = index is not None and self.plan.timeline[index].style == "vocal_handoff" and any(
            v is not DELETE and k in ("swap_at", "curve", "out", "in")
            for k, v in self.changes_for(index, state).items()
        )
        if shaped or any(lane.get("stem") and lane["lane"] == "eq" for lane in new_lanes):
            need = "0.8"
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

        if self.live is not None:
            self.live.set_plan(plan)  # heard in a moment, if playing
        it = self.item(index)
        anchors = resolve_timeline(plan, self.parts.mix_tempo, self.grids).anchors
        with self._lock:
            self.engine.ensure_stem_bands(plan, self.parts)
        levels = self.engine.levels(plan, self.parts, anchors)
        gains = dict(levels.gains)
        vocal_gains = None
        if it.style == "vocal_handoff":
            # Draw what the windows shape: the instrumentals' gain (and the
            # vocals' as a second line).
            sg = levels.stem_gains
            vocal_gains = {}
            for d in (it.from_deck, it.to_deck):
                inst = next(s for s in sg[d] if s != "vocals")
                gains[d] = levels.gains[d] * sg[d][inst]
                vocal_gains[str(d)] = (levels.gains[d] * sg[d]["vocals"])[
                    it.start : it.end : self.parts.sample_rate // GAIN_RATE].round(4).tolist()
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
            "vocal_gains": vocal_gains,
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
            raise ValueError(
                "That would move where a track plays in the mix, which tune can't do yet."
            )
        it = self.item(index)
        echo = state.get("echo") if it.style == "echo_out" else None
        for side, deck in (("out", it.from_deck), ("in", it.to_deck)):
            w = dict(state[side])
            if side == "out" and echo is not None:
                # The echo repeats the `every` before the cut.
                w["start"] -= float(echo["every"]) * 60.0 / self.tempo.bpm_before(w["start"])
            if not any(a - JOIN_SEC <= w["start"] and w["end"] <= b + JOIN_SEC
                       for a, b in self._playing(deck)):
                what = ("The beat the echo repeats" if side == "out" and echo
                        else f"The fade-{side}")
                raise ValueError(
                    f"{what} has to stay within the part where {self.deck_name(deck)} plays."
                )
        if it.style == "vocal_handoff":
            swap = float(state["swap"])
            for deck, t in ((it.from_deck, swap), (it.to_deck, swap - VOCAL_SWAP_SEC)):
                if not any(a - JOIN_SEC <= t <= b + JOIN_SEC for a, b in self._playing(deck)):
                    raise ValueError(
                        f"The vocal swap has to be where {self.deck_name(deck)} plays."
                    )
        if echo is not None and self._smp(float(echo["end"])) > self.parts.total_samples:
            raise ValueError("The echo tail can't run past the end of the mix.")

    def _playing(self, deck: int) -> list[tuple[float, float]]:
        """Where the deck plays, mix seconds: back-to-back segments (a jump,
        a loop) joined into one stretch."""
        out: list[list[float]] = []
        for sp in sorted((s for s in self.parts.timeline.spans
                          if s.deck == deck and s.kind != "silence"), key=lambda s: s.mix_start_sec):
            if out and sp.mix_start_sec <= out[-1][1] + JOIN_SEC:
                out[-1][1] = max(out[-1][1], sp.mix_end_sec)
            else:
                out.append([sp.mix_start_sec, sp.mix_end_sec])
        return [(a, b) for a, b in out]

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
            if self.live is not None:
                self.live.set_plan(self.plan)
        return {"ok": True, "saved": saved, "segment": self.raw["timeline"][index]}


def _param(param: str) -> tuple[str, str | None, str | None]:
    """A page lane key → (kind, band, stem): "low" → eq low of the deck,
    "low@drums" → eq low of its drums, "vol@vocals" → stem_volume,
    "filter" → filter."""
    name, _, stem = param.partition("@")
    if name == "filter":
        return "filter", None, None
    if name == "vol":
        return "vol", None, stem
    return "eq", name, stem or None


def _peaks(x: np.ndarray) -> list[float]:
    mono = np.abs(x).max(axis=1)
    edges = np.linspace(0, len(mono), PEAK_BUCKETS + 1).astype(int)
    return [round(float(mono[a:b].max()), 4) if b > a else 0.0 for a, b in zip(edges, edges[1:])]


def _lane_lines(raw: dict, edits: dict[int, list], new_lanes: list[dict]) -> list[str]:
    """The lane edits, one line each, for the page."""
    def what(lane: dict) -> str:
        name = "volume" if lane["lane"] == "stem_volume" else lane.get("band", lane["lane"])
        return f"{lane['stem']} {name}" if lane.get("stem") else name

    lines = []
    for i, kfs in sorted(edits.items()):
        lane = raw["automation"][i]
        lines.append(f"automation[{i}] deck {lane['deck']} {what(lane)}: {len(kfs)} keyframes")
    for lane in new_lanes:
        lines.append(
            f"new lane: deck {lane['deck']} {what(lane)}, {len(lane['keyframes'])} keyframes"
        )
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


def _track_beats(dur: dict, grid) -> float:
    """A schedule length (as written) in track beats."""
    if "beats" in dur:
        return dur["beats"]
    if "bars" in dur:
        return dur["bars"] * 4
    return dur["seconds"] * grid.bpm / 60.0


def _num(x: float) -> float:
    v = round(float(x), 4)
    return int(v) if v.is_integer() else v


def _round_beats(beats: float) -> float:
    b = round(beats, 4)
    return int(b) if float(b).is_integer() else b


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
                        "detail": session.detail(it),
                    }
                    for it in session.items()
                ]
                self._json({"plan": session.plan_path.name, "items": items})
            elif path.startswith("/item/"):
                try:
                    self._json(session.describe(int(path.rsplit("/", 1)[1])))
                except (ValueError, KeyError):
                    self.send_error(HTTPStatus.NOT_FOUND)
            elif path == "/live/state":
                self._json(session.live.state())
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
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path.startswith("/live/"):
                    self._live(body)
                    return
                index, state = int(body["index"]), body["state"]
            except (ValueError, KeyError, TypeError):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if self.path == "/loop":  # {index, state: {beats, repetitions}}
                self._json(session.set_loop(index, float(state["beats"]), int(state["repetitions"])))
            elif self.path == "/preview":
                self._json(session.preview(index, state))
            elif self.path == "/save":
                self._json(session.save(index, state))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _live(self, body: dict) -> None:
            """/live/play {at: mix sec}, /live/stop, /live/loop {start, end}
            (mix seconds; null = no loop)."""
            live, sr = session.live, session.parts.sample_rate
            if self.path == "/live/play":
                live.play(int(round(float(body["at"]) * sr)))
            elif self.path == "/live/stop":
                live.stop()
            elif self.path == "/live/loop":
                region = body.get("region")
                live.set_loop(None if region is None else
                              (int(round(float(region[0]) * sr)), int(round(float(region[1]) * sr))))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(live.state())

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
        if session.live is not None:
            session.live.stop()
        server.server_close()
