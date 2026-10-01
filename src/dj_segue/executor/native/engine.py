"""Native audio engine — offline WAV render and live sounddevice playback.

Supported: `play`/`loop`/`silence` segments, `crossfade`/`cut` transitions,
and `deck_volume`/`crossfader` automation. Anything else in a plan (stem_volume,
eq, vocal_handoff, stem-based tracks) raises NotImplementedError naming the
milestone that adds it — the engine never silently ignores part of a plan.

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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from dj_segue.compiler import (
    Span,
    crossfader_envelope,
    crossfader_gains,
    deck_volume_envelopes,
    resolve_timeline,
    transition_envelopes,
)
from dj_segue.executor.base import MixExecutor, RenderResult
from dj_segue.executor.native.curves import render_envelope
from dj_segue.executor.native.stretch import time_stretch
from dj_segue.schema.plan import (
    EqLane,
    Plan,
    StemVolumeLane,
    TransitionSegment,
)
from dj_segue.schema.validator import resolved_mix_tempo
from dj_segue.time_math import TrackGrid


# Loop / jump seam crossfade length (seconds of mix time).
LOOP_SEAM_SEC = 0.003


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
    def render(
        self,
        plan: Plan,
        audio_root: Path,
        grids: dict[str, TrackGrid] | None = None,
    ) -> RenderResult:
        self._check_supported(plan)
        audio_root = Path(audio_root).resolve()
        track_audio, sample_rate = self._load_all_tracks(plan, audio_root)
        mix_tempo = resolved_mix_tempo(plan, grids)
        if mix_tempo is None:
            raise ValueError(
                "mix tempo is unknown: the first track's bpm is auto-detected; "
                "pass the preprocessed grids (or set meta.mix_tempo)"
            )

        timeline = resolve_timeline(plan, mix_tempo, grids)
        compiled = self._compile_play_segments(timeline.spans, sample_rate)
        if not compiled:
            return RenderResult(
                samples=np.zeros((0, 2), dtype=np.float32),
                sample_rate=sample_rate,
            )

        total_samples = max(c.mix_end_sample for c in compiled)

        deck_buffers: dict[int, np.ndarray] = {}
        seam = int(round(LOOP_SEAM_SEC * sample_rate))
        prev_on_deck: dict[int, _CompiledPlay] = {}
        for c in sorted(compiled, key=lambda c: (c.deck, c.mix_start_sample)):
            buf = deck_buffers.setdefault(
                c.deck, np.zeros((total_samples, 2), dtype=np.float32)
            )
            n = c.mix_end_sample - c.mix_start_sample
            prev = prev_on_deck.get(c.deck)
            prev_on_deck[c.deck] = c
            lead = 0
            if prev is not None and _needs_seam(prev, c):
                lead = min(seam, n, prev.mix_end_sample - prev.mix_start_sample)
                buf[c.mix_start_sample - lead : c.mix_start_sample] *= _ramp(
                    lead, rising=False
                )[:, None]
            buf[c.mix_start_sample - lead : c.mix_end_sample] += self._segment_audio(
                track_audio[c.track_id], c, n, sample_rate, lead
            )

        gains = self._deck_gains(
            plan, list(deck_buffers), mix_tempo, sample_rate, total_samples, timeline.anchors
        )
        for deck, buf in deck_buffers.items():
            buf *= gains[deck][:, None]

        mix = np.sum(list(deck_buffers.values()), axis=0).astype(np.float32)
        return RenderResult(samples=mix, sample_rate=sample_rate)

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

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _check_supported(plan: Plan) -> None:
        for i, seg in enumerate(plan.timeline):
            if isinstance(seg, TransitionSegment) and seg.style == "vocal_handoff":
                raise NotImplementedError(
                    f"timeline[{i}]: vocal_handoff transitions are M3 scope (stems); "
                    f"the native engine supports crossfade and cut"
                )
        for i, lane in enumerate(plan.automation):
            if isinstance(lane, StemVolumeLane):
                raise NotImplementedError(
                    f"automation[{i}]: stem_volume lanes are M3 scope (stems)"
                )
            if isinstance(lane, EqLane):
                raise NotImplementedError(
                    f"automation[{i}]: eq lanes are M5 scope (EQ and filters)"
                )

    def _load_all_tracks(
        self, plan: Plan, audio_root: Path
    ) -> tuple[dict[str, np.ndarray], int]:
        loaded: dict[str, np.ndarray] = {}
        rates: set[int] = set()
        for tid, track in plan.tracks.items():
            # M1: only single-source (`full`) tracks are supported.
            if list(track.stems) != ["full"]:
                raise NotImplementedError(
                    f"track {tid!r}: stem-based tracks are M3 scope; "
                    f"v0.1 native engine supports single-source only"
                )
            audio_path = (audio_root / track.stems["full"]).resolve()
            data, sr = sf.read(str(audio_path), dtype="float32", always_2d=True)
            if data.shape[1] == 1:
                data = np.repeat(data, 2, axis=1)  # mono → stereo
            elif data.shape[1] > 2:
                data = data[:, :2]
            loaded[tid] = data
            rates.add(int(sr))
        if len(rates) > 1:
            raise NotImplementedError(
                f"mixed sample rates {sorted(rates)} not supported in M1; "
                f"resample tracks to a common rate (e.g. 44100) first"
            )
        return loaded, rates.pop()

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
