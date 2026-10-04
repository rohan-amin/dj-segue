"""Native audio engine — offline WAV render (live playback: live.py).

Supports every segment, transition and automation lane in schema v0.8.

    prepare      plan → tracks, stems, timeline, each deck's segments
    sources.py   each deck's (or stem's) audio before gain, stretched with
                 seams and loops
    chain.py     each deck's stems / EQ / filter / volume
    echo_tail    echo outs' repeats
    limiter.py   the master limiter

The offline engine runs each stage over the whole mix at once
(`render_decks` → `levels` → `DeckRender.mix`; the tune page re-runs the
cheap last two per edit). The live engine (live.py) runs the same stages
block by block; every stage carries its state between blocks, so live and
offline produce identical samples.

EQ (eq.py): a deck with `eq` lanes is split into three bands; the lanes
scale the bands per sample. Offline, plain decks are split once in
`render_decks` (the split doesn't depend on the automation).

Stems (M3): a deck with a stem lane or a vocal_handoff is mixed by stems.
Each segment is rendered per stem (a `path` track's stems come from the
separator, dj_segue.stems; explicit `stems` are loaded — a `.npy` stem is
taken as 44.1 kHz). Decks not mixed by stems play the track whole.

Single sample rate (mismatched tracks error out). A play segment with
`target_bpm` is time-stretched (pitch preserved) by target / track tempo;
otherwise the track plays at its natural tempo. mix_tempo converts mix-time
positions to seconds.

Echo out (v0.6): the dry from_deck cuts at `start_at` and its last `every`
before the cut repeats like a loop — same pre-boundary seams, the first one
crossfading out of the dry audio — each repeat `feedback` times the last,
with a linear fade over the final `every` to silence at the tail's end. The
repeats are taken after the deck's chain (what was audible) and mixed as
separate `Tail`s, so they ring on after the deck stops.

The summed mix goes through the master limiter (limiter.py), which holds
sample peaks to -1 dBFS and leaves mixes below that untouched.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from dj_segue.compiler import (
    Span,
    Timeline,
    eq_decks,
    eq_stems,
    resolve_timeline,
    stem_decks,
)
from dj_segue.executor.base import MixExecutor, RenderResult
from dj_segue.executor.native.chain import DeckChain
from dj_segue.executor.native.eq import split_bands
from dj_segue.executor.native.filter import apply_filter
from dj_segue.executor.native.limiter import context_samples as limiter_context
from dj_segue.executor.native.limiter import limit
from dj_segue.executor.native.sources import (
    LOOP_SEAM_SEC,
    CompiledPlay,
    DeckSource,
    deck_segments,
    ramp,
    read_padded,
)
from dj_segue.schema.plan import LoopSegment, Plan, PlaySegment, TransitionSegment
from dj_segue.schema.validator import resolved_mix_tempo
from dj_segue.time_math import EchoWindow, TrackGrid, echo_window

__all__ = ["LOOP_SEAM_SEC", "DeckRender", "Levels", "NativeEngine", "Tail", "prepare"]


@dataclass(frozen=True)
class Tail:
    """Audio laid on the mix outside any deck's chain (an echo_out's repeats)."""

    start: int  # mix sample of samples[0]
    samples: np.ndarray  # (n, 2) float32


@dataclass(frozen=True)
class Prepared:
    """A plan ready to render: everything but the audio processing."""

    plan: Plan
    sample_rate: int
    mix_tempo: float
    timeline: Timeline
    total_samples: int
    segments: dict[int, list[tuple[CompiledPlay, int]]]  # deck → [(segment, lead)]
    track_audio: dict[str, np.ndarray]
    track_stems: dict[str, dict[str, np.ndarray]]  # tracks on decks mixed by stems
    stem_decks: dict[int, tuple[str, ...]]  # deck → its stems

    def source(self, deck: int, stem: str | None = None, start: int = 0) -> DeckSource:
        if stem is None:
            audio = self.track_audio.__getitem__
        else:
            audio = lambda tid: self.track_stems[tid][stem]  # noqa: E731
        return DeckSource(self.segments.get(deck, []), audio, self.sample_rate, start)

    def chain(self, deck: int, anchors: dict[str, float] | None = None) -> DeckChain:
        return DeckChain(
            self.plan, deck, self.mix_tempo, self.sample_rate,
            self.timeline.anchors if anchors is None else anchors,
            self.stem_decks.get(deck, ()),
        )

    def echo_windows(self, anchors: dict[str, float] | None = None):
        """[(from_deck, EchoWindow)] for each echo_out."""
        anchors = self.timeline.anchors if anchors is None else anchors
        return list(_echo_windows(self.plan, self.mix_tempo, anchors))


def prepare(
    plan: Plan,
    audio_root: Path,
    grids: dict[str, TrackGrid] | None = None,
    audio_cache: dict | None = None,
) -> Prepared:
    """`audio_cache` (path → (audio, rate)) skips re-reading files; the live
    player keeps one across reloads."""
    audio_root = Path(audio_root).resolve()
    track_audio, explicit_stems, sample_rate = _load_tracks(plan, audio_root, audio_cache)
    mix_tempo = resolved_mix_tempo(plan, grids)
    if mix_tempo is None:
        raise ValueError(
            "mix tempo is unknown: the first track's bpm is auto-detected; "
            "pass the preprocessed grids (or set meta.mix_tempo)"
        )
    timeline = resolve_timeline(plan, mix_tempo, grids)
    compiled = _compile_play_segments(timeline.spans, sample_rate)
    total = max((c.mix_end_sample for c in compiled), default=0)
    for _, w in _echo_windows(plan, mix_tempo, timeline.anchors):
        total = max(total, int(round(w.end_sec * sample_rate)))

    by_stems = stem_decks(plan)
    tids = sorted({
        seg.track for seg in plan.timeline
        if isinstance(seg, (PlaySegment, LoopSegment)) and seg.deck in by_stems
    })
    track_stems = {}
    for tid in tids:
        if tid in explicit_stems:
            track_stems[tid] = explicit_stems[tid]
        else:
            from dj_segue.stems import stems_for

            track_stems[tid] = stems_for(track_audio[tid], sample_rate)
    segments = deck_segments(compiled, sample_rate)
    deck_stems = {}
    for deck in by_stems:
        names = [tuple(track_stems[c.track_id]) for c, _ in segments.get(deck, [])]
        if names:
            deck_stems[deck] = names[0]
    return Prepared(
        plan, sample_rate, mix_tempo, timeline, total, segments, track_audio, track_stems,
        deck_stems,
    )


@dataclass(frozen=True)
class DeckRender:
    """Each deck's audio before its chain, on the mix's sample clock."""

    decks: dict[int, np.ndarray]  # deck → (total_samples, 2) float32
    sample_rate: int
    total_samples: int
    mix_tempo: float
    timeline: Timeline
    # Decks with whole-deck EQ (not mixed by stems): their `decks` entry is
    # the band sum; these are the (low, high) bands (eq.split_bands).
    bands: dict[int, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    # Decks mixed by stems (M3): each stem's audio. A stem with its own EQ
    # holds its band sum, with (low, high) in `stem_bands`.
    stems: dict[int, dict[str, np.ndarray]] = field(default_factory=dict)
    stem_bands: dict[tuple[int, str], tuple[np.ndarray, np.ndarray]] = field(
        default_factory=dict
    )
    prepared: Prepared | None = None

    def signal(self, deck: int, levels: "Levels", start: int, end: int) -> np.ndarray:
        """What `deck` contributes to the mix over [start, end) — zero
        outside the mix."""
        return read_padded(levels.signals[deck], start, end - start)

    def mix(
        self,
        levels: "Levels",
        start: int = 0,
        end: int | None = None,
        *,
        limited: bool = True,
    ) -> np.ndarray:
        """Sum of the decks under `levels`, plus its echo tails, over samples
        [start, end), through the master limiter — the same samples a full
        render has."""
        end = self.total_samples if end is None else end
        before, after = limiter_context(self.sample_rate) if limited else (0, 0)
        lo, hi = max(0, start - before), min(self.total_samples, end + after)
        out = premix(levels.signals, levels.tails, lo, hi)
        if limited:
            out = limit(out, self.sample_rate)
        return out[start - lo : end - lo]


def premix(signals: dict[int, np.ndarray], tails, lo: int, hi: int) -> np.ndarray:
    """The decks' signals plus echo tails over [lo, hi), before the limiter.
    `signals[deck]` is indexed by mix sample; decks are summed in key order."""
    out = np.zeros((hi - lo, 2), dtype=np.float32)
    for deck in sorted(signals):
        out += signals[deck][lo:hi]
    for t in tails:
        a, b = max(lo, t.start), min(hi, t.start + len(t.samples))
        if b > a:
            out[a - lo : b - lo] += t.samples[a - t.start : b - t.start]
    return out


@dataclass(frozen=True)
class Levels:
    """What the plan's transitions and automation make of the decks: each
    deck's signal into the mix, and the echo tails. Cheap to recompute from
    a DeckRender (the tune page does, per edit)."""

    gains: dict[int, np.ndarray]  # deck → per-sample volume (for display)
    signals: dict[int, np.ndarray] = field(default_factory=dict)  # deck → (total, 2)
    tails: tuple[Tail, ...] = ()
    stem_gains: dict[int, dict[str, np.ndarray]] = field(default_factory=dict)  # display


class NativeEngine(MixExecutor):
    def __init__(self, *, limited: bool = True) -> None:
        # limited=False skips the master limiter (tests that assert raw gains).
        self.limited = limited

    def render(
        self,
        plan: Plan,
        audio_root: Path,
        grids: dict[str, TrackGrid] | None = None,
    ) -> RenderResult:
        parts = self.render_decks(plan, audio_root, grids)
        if not parts.decks:
            return RenderResult(
                samples=np.zeros((0, 2), dtype=np.float32),
                sample_rate=parts.sample_rate,
            )
        levels = self.levels(plan, parts)
        return RenderResult(
            samples=parts.mix(levels, limited=self.limited),
            sample_rate=parts.sample_rate,
        )

    def render_decks(
        self,
        plan: Plan,
        audio_root: Path,
        grids: dict[str, TrackGrid] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> DeckRender:
        """Each deck's audio before its chain.

        `on_progress(done, total)` is called as decks (and stems) finish
        (from worker threads).

        `render` = `render_decks` → `levels` → `DeckRender.mix`; the tune
        page uses the same steps so it plays exactly what `render` produces.
        """
        p = prepare(plan, audio_root, grids)
        sr, total = p.sample_rate, p.total_samples
        jobs = [(d, None) for d in sorted(p.segments) if d not in p.stem_decks]
        jobs += [(d, s) for d, stems in sorted(p.stem_decks.items()) for s in stems]

        done = 0
        progress_lock = threading.Lock()
        if on_progress is not None:
            on_progress(0, len(jobs))

        def run(job: tuple[int, str | None]) -> np.ndarray:
            nonlocal done
            out = p.source(*job).read(0, total)
            if on_progress is not None:
                with progress_lock:
                    done += 1
                    on_progress(done, len(jobs))
            return out

        # The stretcher releases the GIL, so threads overlap the streams.
        with ThreadPoolExecutor(max_workers=max(1, os.cpu_count() or 1)) as pool:
            audios = dict(zip(jobs, pool.map(run, jobs)))

        decks = {d: a for (d, s), a in audios.items() if s is None}
        stems: dict[int, dict[str, np.ndarray]] = {}
        for (d, s), a in audios.items():
            if s is not None:
                stems.setdefault(d, {})[s] = a
        for d, per_stem in stems.items():
            decks[d] = sum(per_stem.values())  # display; `levels` mixes the stems
        stem_bands = {}
        for d, s in sorted(eq_stems(plan)):
            if s in stems.get(d, {}):
                stems[d][s], *b = split_bands(stems[d][s], sr)
                stem_bands[(d, s)] = tuple(b)
        bands = {}
        for d in sorted(eq_decks(plan) & set(decks) - set(stems)):
            decks[d], *b = split_bands(decks[d], sr)
            bands[d] = tuple(b)
        return DeckRender(
            decks, sr, total, p.mix_tempo, p.timeline, bands, stems, stem_bands, p
        )

    def levels(
        self, plan: Plan, parts: DeckRender, anchors: dict[str, float] | None = None
    ) -> Levels:
        """Each deck's signal into the mix, and the echo tails, under `plan`
        (`anchors` default to `parts`' own)."""
        anchors = parts.timeline.anchors if anchors is None else anchors
        n, sr = parts.total_samples, parts.sample_rate
        gains, signals, stem_gains = {}, {}, {}
        for deck in sorted(parts.decks):
            stems = tuple(parts.stems.get(deck, {}))
            chain = DeckChain(plan, deck, parts.mix_tempo, sr, anchors, stems)
            if stems:
                sb = {s: b for (d, s), b in parts.stem_bands.items() if d == deck}
                signals[deck] = chain.process(0, n, parts.stems[deck], stem_bands=sb)
                stem_gains[deck] = {s: chain.stem_gains(s, 0, n) for s in stems}
            else:
                signals[deck] = chain.process(0, n, parts.decks[deck], parts.bands.get(deck))
            gains[deck] = chain.gains(0, n)
        tails = tuple(
            echo_tail(read_padded(signals[deck], *capture_range(w, sr)), w, sr)
            for deck, w in _echo_windows(plan, parts.mix_tempo, anchors)
            if deck in signals
        )
        return Levels(gains, signals, tails, stem_gains)

    def ensure_stem_bands(self, plan: Plan, parts: DeckRender) -> None:
        """Split any stem a plan newly EQs into bands (the tune page adds
        stem EQ lanes after `render_decks`; a render would do this there)."""
        for d, stem in sorted(eq_stems(plan)):
            if stem in parts.stems.get(d, {}) and (d, stem) not in parts.stem_bands:
                total, low, high = split_bands(parts.stems[d][stem], parts.sample_rate)
                parts.stems[d][stem] = total
                parts.stem_bands[(d, stem)] = (low, high)

    def render_to_wav(
        self,
        plan: Plan,
        audio_root: Path,
        out_path: Path,
        grids: dict[str, TrackGrid] | None = None,
    ) -> RenderResult:
        result = self.render(plan, audio_root, grids)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(out_path, result.samples, result.sample_rate, subtype="PCM_16")
        return result

    def play_live(
        self,
        plan: Plan,
        audio_root: Path,
        grids: dict[str, TrackGrid] | None = None,
    ) -> RenderResult:
        """Play through the speakers as it renders (live.py)."""
        from dj_segue.executor.native.live import LiveEngine, play

        engine = LiveEngine(prepare(plan, audio_root, grids), limited=self.limited)
        frames = play(engine)
        return RenderResult(
            samples=np.zeros((frames, 2), dtype=np.float32), sample_rate=engine.sample_rate
        )


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def _load_tracks(plan: Plan, audio_root: Path, cache: dict | None = None):
    """(track audio, explicit stems, sample rate). A track with explicit
    stems is their sum."""
    cache = {} if cache is None else cache
    explicit: dict[str, dict[str, np.ndarray]] = {}
    loaded: dict[str, np.ndarray] = {}
    rates: set[int] = set()
    for tid, track in plan.tracks.items():
        parts = {}
        for name, rel in track.stems.items():
            path = (audio_root / rel).resolve()
            if path not in cache:
                cache[path] = _read_audio(path)
            data, sr = cache[path]
            parts[name] = data
            rates.add(sr)
        if list(parts) == ["full"]:
            loaded[tid] = parts["full"]
        else:
            n = max(len(x) for x in parts.values())
            parts = {k: read_padded(x, 0, n) for k, x in parts.items()}
            explicit[tid] = parts
            loaded[tid] = sum(parts.values())
    if len(rates) > 1:
        raise NotImplementedError(
            f"mixed sample rates {sorted(rates)} not supported; "
            f"resample tracks to a common rate (e.g. 44100) first"
        )
    return loaded, explicit, rates.pop()


def _read_audio(path: Path) -> tuple[np.ndarray, int]:
    """An audio file (or a .npy array at 44.1 kHz) as frames × 2 float32."""
    if path.suffix == ".npy":
        data, sr = np.load(path).astype(np.float32), 44100
        data = data if data.ndim == 2 else data[:, None]
    else:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)  # mono → stereo
    elif data.shape[1] > 2:
        data = data[:, :2]
    return data, int(sr)


def _compile_play_segments(spans: list[Span], sample_rate: int) -> list[CompiledPlay]:
    def smp(sec: float) -> int:
        return int(round(sec * sample_rate))

    return [
        CompiledPlay(
            deck=sp.deck,
            mix_start_sample=smp(sp.mix_start_sec),
            mix_end_sample=smp(sp.mix_end_sec),
            track_start_sample=smp(sp.track_from_sec),
            track_id=sp.track_id,  # type: ignore[arg-type]
            rate=sp.rate,
            rep_starts=tuple(smp(r.mix_start_sec) for r in sp.reps),
            loop_track_samples=max((smp(r.track_len_sec) for r in sp.reps), default=0),
        )
        for sp in spans
        if sp.kind in ("play", "loop")
    ]


# ---------------------------------------------------------------------------
# echo out
# ---------------------------------------------------------------------------


def _echo_windows(plan: Plan, mix_tempo: float, anchors: dict[str, float]):
    """(from_deck, EchoWindow) for each echo_out, in timeline order."""
    for seg in plan.timeline:
        if isinstance(seg, TransitionSegment) and seg.style == "echo_out":
            yield seg.from_deck, echo_window(seg, mix_tempo, anchors)


def capture_range(w: EchoWindow, sample_rate: int) -> tuple[int, int]:
    """(start, length) of the deck signal an echo repeats: from one seam
    before the captured `every` up to the cut."""
    seam = int(round(LOOP_SEAM_SEC * sample_rate))
    cut = int(round(w.cut_sec * sample_rate))
    every = int(round(w.every_sec * sample_rate))
    return cut - every - seam, every + seam


def echo_tail(cap: np.ndarray, w: EchoWindow, sample_rate: int) -> Tail:
    """Repeats of the `every` before the cut, as a loop would play them;
    `cap` is the deck's signal over `capture_range(w)`.

    The tail starts one seam before the cut: there it fades in the first
    repeat's pre-roll and subtracts a rising ramp of the dry audio, which
    (with the dry gain stepping to 0 at the cut) fades the dry out — the
    same pre-boundary crossfade as a loop seam.
    """
    sr = sample_rate
    seam = int(round(LOOP_SEAM_SEC * sr))
    cut = int(round(w.cut_sec * sr))
    end = int(round(w.end_sec * sr))
    every = int(round(w.every_sec * sr))
    reps = []  # each repeat's first mix sample, then the tail's end
    while (r := cut + int(round(len(reps) * w.every_sec * sr))) < end:
        reps.append(r)
    reps.append(end)

    out = np.zeros((end - (cut - seam), 2), dtype=np.float64)
    base = cut - seam
    rise = ramp(seam, rising=True)[:, None]
    out[:seam] -= cap[every:] * rise  # dry fades out into the first repeat
    level = 1.0
    for a, b in zip(reps, reps[1:]):
        length = b - a
        chunk = read_padded(cap, 0, seam + length).astype(np.float64)
        chunk[:seam] *= rise
        f_out = min(seam, length // 2) if b < end else 0
        if f_out:
            chunk[-f_out:] *= ramp(f_out, rising=False)[:, None]
        out[a - seam - base : b - base] += chunk * level
        level *= w.feedback
    if w.highpass > 0:
        # The tail's own filter knob: off through the dry crossfade, then a
        # straight move to `highpass` at the tail's end.
        knob = np.zeros(len(out))
        knob[seam:] = np.linspace(0.0, w.highpass, len(out) - seam)
        out = apply_filter(out, knob, sr).astype(np.float64)
    fade = min(every, end - cut)
    if fade:
        out[len(out) - fade :] *= _fade_to_zero(fade)[:, None]
    return Tail(base, out.astype(np.float32))


def _fade_to_zero(n: int) -> np.ndarray:
    """Linear 1 → 0 over n samples, reaching 0 just after the last."""
    return 1.0 - (np.arange(n, dtype=np.float64) + 1) / n
