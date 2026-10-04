"""Native audio engine — offline WAV render and live sounddevice playback.

Supports every segment, transition and automation lane in schema v0.8.

EQ (eq.py): a deck with `eq` lanes is split once into three bands; the lanes
scale the bands per sample.

Stems (M3): a deck with a stem lane or a vocal_handoff is mixed by stems.
Each segment is rendered per stem (a `path` track's stems come from the
separator, dj_segue.stems; explicit `stems` are loaded — a `.npy` stem is
taken as 44.1 kHz). Per sample, each stem gets its own EQ and volume
(stem_volume lanes × vocal_handoff fades), then the stems are summed and
the deck carries on as any other: its EQ, filter and volume. Decks not
mixed by stems play the track as before.

Single sample rate (mismatched tracks error out). A play segment with
`target_bpm` is time-stretched (pitch preserved) by target / track tempo;
otherwise the track plays at its natural tempo. mix_tempo converts mix-time
positions to seconds.

Loops: each repetition starts at its own rounded mix sample (no drift), and
every seam is a short linear crossfade placed just *before* the boundary —
the outgoing pass fades out over its last LOOP_SEAM_SEC while the incoming
pass fades in from a pre-roll of track audio just before the loop start — so
the transient on the loop's first beat is untouched and there's no click.

Jumps get the same seam: when a segment starts exactly where the previous one
on its deck ends, and the audio doesn't simply carry on (another track or
position, or a stretched segment), the previous segment fades out over its
last LOOP_SEAM_SEC while the new one fades in from a pre-roll of track audio
just before its `from`.

Per-deck gain = product of: transition envelope × deck_volume lanes ×
crossfader gain (decks 1–2 only). All come from dj_segue.compiler.

Echo out (v0.6): the dry from_deck cuts at `start_at` and its last `every`
before the cut repeats like a loop — same pre-boundary seams, the first one
crossfading out of the dry audio — each repeat `feedback` times the last,
with a linear fade over the final `every` to silence at the tail's end. The
repeats are taken after the deck's gain (what was audible) and mixed as
separate `Tail`s, so they ring on after the deck stops.

The summed mix goes through the master limiter (limiter.py), which holds
sample peaks to -1 dBFS and leaves mixes below that untouched.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import soundfile as sf

from dj_segue.compiler import (
    Span,
    Timeline,
    crossfader_envelope,
    crossfader_gains,
    deck_volume_envelopes,
    eq_decks,
    eq_envelope,
    eq_stems,
    stem_decks,
    stem_envelopes,
    filter_decks,
    filter_envelope,
    resolve_timeline,
    transition_envelopes,
)
from dj_segue.executor.base import MixExecutor, RenderResult
from dj_segue.executor.native.curves import render_envelope
from dj_segue.executor.native.eq import split_bands
from dj_segue.executor.native.filter import apply_filter
from dj_segue.executor.native.limiter import context_samples as limiter_context
from dj_segue.executor.native.limiter import limit
from dj_segue.executor.native.stretch import time_stretch
from dj_segue.schema.plan import (
    LoopSegment,
    Plan,
    PlaySegment,
    TransitionSegment,
)
from dj_segue.schema.validator import resolved_mix_tempo
from dj_segue.time_math import EchoWindow, TrackGrid, echo_window


# Loop / jump seam crossfade length (seconds of mix time).
LOOP_SEAM_SEC = 0.003


@dataclass(frozen=True)
class Tail:
    """Audio laid on the mix outside any deck's gain (an echo_out's repeats)."""

    start: int  # mix sample of samples[0]
    samples: np.ndarray  # (n, 2) float32


# deck → (low, mid, high) per-sample linear gains (eq.py).
EqGains = dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]


@dataclass(frozen=True)
class Levels:
    """What the plan's transitions and automation do to the decks' audio.
    Cheap to recompute from a DeckRender (the tune page does, per edit)."""

    gains: dict[int, np.ndarray]  # deck → per-sample volume
    eq: EqGains = field(default_factory=dict)
    # Decks with a filter lane: their whole post-EQ, post-filter audio (the
    # filter has memory, so it runs over the full deck, not per excerpt).
    filtered: dict[int, np.ndarray] = field(default_factory=dict)
    tails: tuple[Tail, ...] = ()
    # Decks mixed by stems: the stems mixed (after their EQ and volume), the
    # whole deck's audio before its own EQ — its band sum if it has EQ, with
    # (low, high) in `premix_bands`.
    premix: dict[int, np.ndarray] = field(default_factory=dict)
    premix_bands: dict[int, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)


@dataclass(frozen=True)
class DeckRender:
    """Each deck's audio before gain, on the mix's sample clock."""

    decks: dict[int, np.ndarray]  # deck → (total_samples, 2) float32
    sample_rate: int
    total_samples: int
    mix_tempo: float
    timeline: Timeline
    # Decks with EQ: their `decks` entry is the band sum; these are the
    # (low, high) bands (eq.split_bands).
    bands: dict[int, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    # Decks mixed by stems (M3): each stem's audio, on the mix clock. A stem
    # with its own EQ holds its band sum, with (low, high) in `stem_bands`.
    stems: dict[int, dict[str, np.ndarray]] = field(default_factory=dict)
    stem_bands: dict[tuple[int, str], tuple[np.ndarray, np.ndarray]] = field(
        default_factory=dict
    )

    def equalized(self, deck: int, levels: Levels, lo: int, hi: int) -> np.ndarray:
        """`deck`'s audio over [lo, hi) (inside the mix) after its stems'
        mix and its EQ."""
        if deck in levels.premix:
            x, bands = levels.premix[deck][lo:hi], levels.premix_bands.get(deck)
        else:
            x, bands = self.decks[deck][lo:hi], self.bands.get(deck)
        if deck in levels.eq:
            x = _apply_eq(x, levels.eq[deck], bands, lo, hi)
        return x

    def signal(self, deck: int, levels: Levels, start: int, end: int) -> np.ndarray:
        """What `deck` contributes to the mix over [start, end) — after EQ,
        filter and gain — zero outside the mix."""
        out = np.zeros((end - start, 2), dtype=np.float32)
        lo, hi = max(0, start), min(self.total_samples, end)
        if hi <= lo:
            return out
        if deck in levels.filtered:
            x = levels.filtered[deck][lo:hi]
        else:
            x = self.equalized(deck, levels, lo, hi)
        out[lo - start : hi - start] = x * levels.gains[deck][lo:hi, None]
        return out

    def mix(
        self,
        levels: Levels,
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
        out = np.zeros((hi - lo, 2), dtype=np.float32)
        for deck in self.decks:
            out += self.signal(deck, levels, lo, hi)
        for t in levels.tails:
            a, b = max(lo, t.start), min(hi, t.start + len(t.samples))
            if b > a:
                out[a - lo : b - lo] += t.samples[a - t.start : b - t.start]
        if limited:
            out = limit(out, self.sample_rate)
        return out[start - lo : end - lo]


@dataclass(frozen=True)
class _CompiledPlay:
    """A play or loop span in samples. A play is a loop with one rep."""

    deck: int
    mix_start_sample: int
    mix_end_sample: int  # exclusive
    track_start_sample: int  # may be slightly negative (grid anchor tolerance)
    track_id: str
    rate: float  # track-seconds per mix-second; 1.0 = natural tempo
    # Loops only: each rep's mix start sample (absolute), and the longest
    # rep's length in track samples.
    rep_starts: tuple[int, ...] = ()
    loop_track_samples: int = 0


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
    ) -> "DeckRender":
        """Each deck's audio before gain (volume, transitions, crossfader).

        `on_progress(done, total)` is called as segments finish (from worker
        threads).

        `render` = `render_decks` → `levels` → `DeckRender.mix`; the tune
        page uses the same steps so it plays exactly what `render` produces.
        """
        audio_root = Path(audio_root).resolve()
        track_audio, sample_rate = self._load_all_tracks(plan, audio_root)
        by_stems = stem_decks(plan)
        track_stems = self._load_stems(plan, audio_root, track_audio, sample_rate, by_stems)
        mix_tempo = resolved_mix_tempo(plan, grids)
        if mix_tempo is None:
            raise ValueError(
                "mix tempo is unknown: the first track's bpm is auto-detected; "
                "pass the preprocessed grids (or set meta.mix_tempo)"
            )

        timeline = resolve_timeline(plan, mix_tempo, grids)
        compiled = self._compile_play_segments(timeline.spans, sample_rate)
        total_samples = max((c.mix_end_sample for c in compiled), default=0)
        for w in _echo_windows(plan, mix_tempo, timeline.anchors):
            total_samples = max(total_samples, int(round(w.end_sec * sample_rate)))

        # Seams first (they depend on each deck's previous segment), then all
        # segments' audio at once — stretching runs in rubberband processes,
        # so threads overlap them — then assemble in order.
        seam = int(round(LOOP_SEAM_SEC * sample_rate))
        ordered = sorted(compiled, key=lambda c: (c.deck, c.mix_start_sample))
        leads: list[int] = []
        prev_on_deck: dict[int, _CompiledPlay] = {}
        for c in ordered:
            prev = prev_on_deck.get(c.deck)
            prev_on_deck[c.deck] = c
            lead = 0
            if prev is not None and _needs_seam(prev, c):
                n = c.mix_end_sample - c.mix_start_sample
                lead = min(seam, n, prev.mix_end_sample - prev.mix_start_sample)
            leads.append(lead)

        # One job per segment — per stem on a deck mixed by stems (each stem
        # is stretched on its own, with the same seams).
        jobs = []
        for c, lead in zip(ordered, leads):
            if c.deck in by_stems:
                jobs += [(c, lead, stem) for stem in track_stems[c.track_id]]
            else:
                jobs.append((c, lead, None))

        done = 0
        if on_progress is not None:
            on_progress(0, len(jobs))

        def segment(job: tuple[_CompiledPlay, int, str | None]) -> np.ndarray:
            nonlocal done
            c, lead, stem = job
            src = track_audio[c.track_id] if stem is None else track_stems[c.track_id][stem]
            audio = self._segment_audio(
                src, c, c.mix_end_sample - c.mix_start_sample, sample_rate, lead,
            )
            if on_progress is not None:
                with progress_lock:
                    done += 1
                    on_progress(done, len(jobs))
            return audio

        progress_lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=max(1, os.cpu_count() or 1)) as pool:
            audios = list(pool.map(segment, jobs))

        buffers: dict[tuple[int, str | None], np.ndarray] = {}
        for (c, lead, stem), audio in zip(jobs, audios):
            buf = buffers.setdefault(
                (c.deck, stem), np.zeros((total_samples, 2), dtype=np.float32)
            )
            if lead:
                buf[c.mix_start_sample - lead : c.mix_start_sample] *= _ramp(
                    lead, rising=False
                )[:, None]
            buf[c.mix_start_sample - lead : c.mix_end_sample] += audio

        deck_buffers = {d: b for (d, stem), b in buffers.items() if stem is None}
        stems: dict[int, dict[str, np.ndarray]] = {}
        for (d, stem), b in buffers.items():
            if stem is not None:
                stems.setdefault(d, {})[stem] = b
        for d, per_stem in stems.items():
            deck_buffers[d] = sum(per_stem.values())  # display; levels mixes the stems
        stem_bands: dict[tuple[int, str], tuple[np.ndarray, np.ndarray]] = {}
        for d, stem in sorted(eq_stems(plan)):
            if stem in stems.get(d, {}):
                total, low, high = split_bands(stems[d][stem], sample_rate)
                stems[d][stem] = total
                stem_bands[(d, stem)] = (low, high)

        # Whole-deck EQ bands now for plain decks; a deck mixed by stems is
        # split in `levels`, after its stems are mixed.
        bands: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        for deck in sorted(eq_decks(plan) & set(deck_buffers) - set(stems)):
            total, low, high = split_bands(deck_buffers[deck], sample_rate)
            deck_buffers[deck] = total
            bands[deck] = (low, high)
        return DeckRender(
            deck_buffers, sample_rate, total_samples, mix_tempo, timeline, bands,
            stems, stem_bands,
        )

    def deck_gains(
        self, plan: Plan, parts: "DeckRender", anchors: dict[str, float] | None = None
    ) -> dict[int, np.ndarray]:
        """Per-sample gain for each of `parts`' decks under `plan`'s
        transitions and automation. `anchors` defaults to `parts`' own."""
        return self._deck_gains(
            plan,
            list(parts.decks),
            parts.mix_tempo,
            parts.sample_rate,
            parts.total_samples,
            parts.timeline.anchors if anchors is None else anchors,
        )

    def deck_eq(
        self, plan: Plan, parts: "DeckRender", anchors: dict[str, float] | None = None
    ) -> EqGains:
        """Per-sample (low, mid, high) gains for each deck `parts` split into
        bands. A band without a lane stays at 0 dB."""
        anchors = parts.timeline.anchors if anchors is None else anchors
        decks = set(parts.bands) | (eq_decks(plan) & set(parts.stems))
        return {deck: _eq_gains(plan, deck, parts, anchors) for deck in sorted(decks)}

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
        # Imported lazily so the WAV-render path doesn't require a sound device.
        import sounddevice as sd

        result = self.render(plan, audio_root, grids)
        sd.play(result.samples, samplerate=result.sample_rate, blocking=True)
        return result

    def levels(
        self, plan: Plan, parts: "DeckRender", anchors: dict[str, float] | None = None
    ) -> Levels:
        """Volume, EQ, filter and echo tails under `plan` (`anchors` default
        to `parts`' own). `render` = `render_decks` → `levels` → `mix`."""
        anchors = parts.timeline.anchors if anchors is None else anchors
        sr, n = parts.sample_rate, parts.total_samples
        gains = self.deck_gains(plan, parts, anchors)
        eq = self.deck_eq(plan, parts, anchors)

        premix: dict[int, np.ndarray] = {}
        premix_bands: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        for deck, per_stem in sorted(parts.stems.items()):
            x = np.zeros((n, 2), dtype=np.float32)
            for stem, audio in per_stem.items():
                if (deck, stem) in parts.stem_bands:
                    audio = _apply_eq(
                        audio, _eq_gains(plan, deck, parts, anchors, stem),
                        parts.stem_bands[(deck, stem)], 0, n,
                    )
                g = np.ones(n, dtype=np.float32)
                for env in stem_envelopes(plan, deck, stem, parts.mix_tempo, anchors):
                    g *= render_envelope(env, sr, n)
                x += audio * g[:, None]
            if deck in eq:
                total, low, high = split_bands(x, sr)
                premix[deck], premix_bands[deck] = total, (low, high)
            else:
                premix[deck] = x

        levels = Levels(gains, eq, premix=premix, premix_bands=premix_bands)
        filtered = {}
        for deck in sorted(filter_decks(plan) & set(parts.decks)):
            env = filter_envelope(plan, deck, parts.mix_tempo, anchors)
            knob = render_envelope(env, sr, n)
            filtered[deck] = apply_filter(parts.equalized(deck, levels, 0, n), knob, sr)
        levels = replace(levels, filtered=filtered)
        tails = tuple(
            _echo_tail(parts, deck, levels, w)
            for deck, w in _echo_windows(plan, parts.mix_tempo, anchors, with_deck=True)
            if deck in parts.decks
        )
        return replace(levels, tails=tails)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _load_all_tracks(
        self, plan: Plan, audio_root: Path
    ) -> tuple[dict[str, np.ndarray], int]:
        """Each track's audio (frames × 2): its file, or the sum of its
        explicit stems."""
        self._explicit_stems: dict[str, dict[str, np.ndarray]] = {}
        loaded: dict[str, np.ndarray] = {}
        rates: set[int] = set()
        for tid, track in plan.tracks.items():
            parts = {}
            for name, rel in track.stems.items():
                data, sr = _read_audio((audio_root / rel).resolve())
                parts[name] = data
                rates.add(sr)
            if list(parts) == ["full"]:
                loaded[tid] = parts["full"]
            else:
                n = max(len(x) for x in parts.values())
                parts = {k: _fit_length(x, n) for k, x in parts.items()}
                self._explicit_stems[tid] = parts
                loaded[tid] = sum(parts.values())
        if len(rates) > 1:
            raise NotImplementedError(
                f"mixed sample rates {sorted(rates)} not supported in M1; "
                f"resample tracks to a common rate (e.g. 44100) first"
            )
        return loaded, rates.pop()

    def _load_stems(
        self,
        plan: Plan,
        audio_root: Path,
        track_audio: dict[str, np.ndarray],
        sample_rate: int,
        decks: set[int],
    ) -> dict[str, dict[str, np.ndarray]]:
        """Stems of every track played on a deck mixed by stems: its own, or
        separated (cached; the preprocessor normally did it already)."""
        tids = sorted({
            seg.track for seg in plan.timeline
            if isinstance(seg, (PlaySegment, LoopSegment)) and seg.deck in decks
        })
        out = {}
        for tid in tids:
            if tid in self._explicit_stems:
                out[tid] = self._explicit_stems[tid]
            else:
                from dj_segue.stems import stems_for

                out[tid] = stems_for(track_audio[tid], sample_rate)
        return out

    @staticmethod
    def _compile_play_segments(spans: list[Span], sample_rate: int) -> list[_CompiledPlay]:
        def smp(sec: float) -> int:
            return int(round(sec * sample_rate))

        return [
            _CompiledPlay(
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

    # Extra track audio fed to the stretcher past the segment end, so its
    # end-of-input handling doesn't color the last samples we keep.
    _STRETCH_TAIL_SEC = 0.25

    def _segment_audio(
        self, audio: np.ndarray, c: _CompiledPlay, n: int, sample_rate: int, lead: int = 0
    ) -> np.ndarray:
        """`lead` + `n` mix samples of the segment's audio, stretched if rate != 1.

        The first `lead` samples are a pre-roll of the track audio before
        `from`, fading in (a jump seam); the segment itself follows exactly.
        Reads outside the track (a slightly negative start from the grid
        anchor, or running past the end) come back as silence.
        """
        if c.rep_starts:
            return self._loop_audio(audio, c, n, sample_rate, lead)
        pre_track = int(round(lead * c.rate))
        out = self._mix_rate_audio(
            audio, c.track_start_sample - pre_track, lead + n, c.rate, sample_rate
        )
        if lead:
            out[:lead] *= _ramp(lead, rising=True)[:, None]
        return out

    def _mix_rate_audio(
        self, audio: np.ndarray, track_start: int, n: int, rate: float, sample_rate: int
    ) -> np.ndarray:
        """`n` mix samples of track audio from `track_start`, played at `rate`."""
        if rate == 1.0:
            return _read_padded(audio, track_start, n)
        tail = int(self._STRETCH_TAIL_SEC * sample_rate)
        n_in = int(np.ceil(n * rate)) + tail
        src = _read_padded(audio, track_start, n_in)
        return _fit_length(time_stretch(src, sample_rate, rate), n)

    def _loop_audio(
        self, audio: np.ndarray, c: _CompiledPlay, n: int, sample_rate: int, lead: int = 0
    ) -> np.ndarray:
        """Render a loop span: every rep replays the loop from its start.

        The loop region (longest rep, plus a pre-roll of `seam` mix samples
        before the loop start) is brought to mix rate once; shorter reps use
        a prefix of it. Rep k>0 is written from `seam` samples before its
        start, fading in over the pre-roll while rep k-1 fades out over the
        same samples, so the two sum to a linear crossfade ending exactly on
        the boundary. With `lead` (≤ seam), rep 0 does the same over the
        first `lead` samples of the output.
        """
        seam = int(round(LOOP_SEAM_SEC * sample_rate))
        pre_track = int(round(seam * c.rate))
        region_mix = int(np.ceil(c.loop_track_samples / c.rate)) + 2  # rounding slack
        src = self._mix_rate_audio(
            audio, c.track_start_sample - pre_track, seam + region_mix, c.rate, sample_rate
        )
        out = np.zeros((lead + n, audio.shape[1]), dtype=np.float32)
        starts = [s - c.mix_start_sample + lead for s in c.rep_starts] + [lead + n]
        for k, (a, b) in enumerate(zip(starts, starts[1:])):
            length = b - a
            # Keep fades inside short reps (not reachable at musical lengths).
            f_in = min(seam, length // 2) if k > 0 else lead
            f_out = min(seam, length // 2) if b < lead + n else 0
            chunk = src[seam - f_in : seam + length].copy()
            if f_in:
                chunk[:f_in] *= _ramp(f_in, rising=True)[:, None]
            if f_out:
                chunk[-f_out:] *= _ramp(f_out, rising=False)[:, None]
            out[a - f_in : b] += chunk
        return out

    def _deck_gains(
        self,
        plan: Plan,
        decks: list[int],
        mix_tempo: float,
        sample_rate: int,
        total_samples: int,
        anchors: dict[str, float],
    ) -> dict[int, np.ndarray]:
        transitions = transition_envelopes(plan, mix_tempo, anchors)
        xfade_env = crossfader_envelope(plan, mix_tempo, anchors)
        xfade = None
        if xfade_env is not None:
            pos = render_envelope(xfade_env, sample_rate, total_samples)
            g1, g2 = crossfader_gains(pos)
            xfade = {1: g1.astype(np.float32), 2: g2.astype(np.float32)}

        gains: dict[int, np.ndarray] = {}
        for deck in decks:
            envs = deck_volume_envelopes(plan, deck, mix_tempo, anchors)
            if deck in transitions:
                envs.append(transitions[deck])
            g = np.ones(total_samples, dtype=np.float32)
            for env in envs:
                g *= render_envelope(env, sample_rate, total_samples)
            if xfade is not None and deck in xfade:
                g *= xfade[deck]
            gains[deck] = g
        return gains


def _eq_gains(
    plan: Plan, deck: int, parts: DeckRender, anchors: dict[str, float], stem: str | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(low, mid, high) per-sample linear gains of a deck's (or stem's) EQ;
    a band without a lane stays at 0 dB."""
    out = []
    for band in ("low", "mid", "high"):
        env = eq_envelope(plan, deck, band, parts.mix_tempo, anchors, stem)
        if env is None:
            out.append(np.ones(parts.total_samples, dtype=np.float32))
        else:
            db = render_envelope(env, parts.sample_rate, parts.total_samples)
            out.append((10.0 ** (db / 20.0)).astype(np.float32))
    return tuple(out)


def _apply_eq(x, gains, bands, lo: int, hi: int) -> np.ndarray:
    """x (already the band sum) with its bands scaled: see eq.py."""
    g_low, g_mid, g_high = (g[lo:hi, None] for g in gains)
    low, high = (b[lo:hi] for b in bands)
    return x * g_mid + low * (g_low - g_mid) + high * (g_high - g_mid)


def _echo_windows(plan: Plan, mix_tempo: float, anchors: dict[str, float], with_deck=False):
    for seg in plan.timeline:
        if isinstance(seg, TransitionSegment) and seg.style == "echo_out":
            w = echo_window(seg, mix_tempo, anchors)
            yield (seg.from_deck, w) if with_deck else w


def _echo_tail(parts: DeckRender, deck: int, levels: Levels, w: EchoWindow) -> Tail:
    """Repeats of the `every` before the cut, as a loop would play them.

    The tail starts one seam before the cut: there it fades in the first
    repeat's pre-roll and subtracts a rising ramp of the dry audio, which
    (with the dry gain stepping to 0 at the cut) fades the dry out — the
    same pre-boundary crossfade as a loop seam.
    """
    sr = parts.sample_rate
    seam = int(round(LOOP_SEAM_SEC * sr))
    cut = int(round(w.cut_sec * sr))
    end = int(round(w.end_sec * sr))
    every = int(round(w.every_sec * sr))
    reps = []  # each repeat's first mix sample, then the tail's end
    while (r := cut + int(round(len(reps) * w.every_sec * sr))) < end:
        reps.append(r)
    reps.append(end)

    # What the deck played (after EQ, filter and gain) from one seam before
    # the captured `every` up to the cut.
    cap = parts.signal(deck, levels, cut - every - seam, cut)

    out = np.zeros((end - (cut - seam), 2), dtype=np.float64)
    base = cut - seam
    rise = _ramp(seam, rising=True)[:, None]
    out[:seam] -= cap[every:] * rise  # dry fades out into the first repeat
    level = 1.0
    for a, b in zip(reps, reps[1:]):
        length = b - a
        chunk = _read_padded(cap, 0, seam + length).astype(np.float64)
        chunk[:seam] *= rise
        f_out = min(seam, length // 2) if b < end else 0
        if f_out:
            chunk[-f_out:] *= _ramp(f_out, rising=False)[:, None]
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


def _needs_seam(prev: _CompiledPlay, c: _CompiledPlay) -> bool:
    """True when `c` starts right as `prev` (same deck) ends and the audio
    jumps. A plain continuation — same track, natural tempo, `c` starting
    where `prev` stopped — needs none."""
    if c.mix_start_sample != prev.mix_end_sample:
        return False
    if prev.rep_starts or c.track_id != prev.track_id or prev.rate != 1.0 or c.rate != 1.0:
        return True
    prev_track_end = prev.track_start_sample + (prev.mix_end_sample - prev.mix_start_sample)
    return c.track_start_sample != prev_track_end


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


def _read_padded(audio: np.ndarray, start: int, n: int) -> np.ndarray:
    """audio[start:start+n], zero-filled wherever that range leaves the track."""
    out = np.zeros((n, audio.shape[1]), dtype=np.float32)
    lo, hi = max(start, 0), min(start + n, audio.shape[0])
    if hi > lo:
        out[lo - start : hi - start] = audio[lo:hi]
    return out


def _ramp(n: int, *, rising: bool) -> np.ndarray:
    """Linear fade of n samples; a rising and a falling ramp sum to 1 per sample."""
    t = (np.arange(n, dtype=np.float32) + 0.5) / n
    return t if rising else 1.0 - t


def _fit_length(x: np.ndarray, n: int) -> np.ndarray:
    if x.shape[0] >= n:
        return x[:n]
    return np.concatenate([x, np.zeros((n - x.shape[0], x.shape[1]), dtype=x.dtype)])
