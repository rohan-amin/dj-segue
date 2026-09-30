"""Native audio engine — offline WAV render and live sounddevice playback.

Supported: `play`/`silence` segments, `crossfade`/`cut` transitions, and
`deck_volume`/`crossfader` automation. Anything else in a plan (stem_volume,
eq, vocal_handoff, stem-based tracks) raises NotImplementedError naming the
milestone that adds it — the engine never silently ignores part of a plan.

Single sample rate (mismatched tracks error out). A play segment with
`target_bpm` is time-stretched (pitch preserved) by target / track tempo;
otherwise the track plays at its natural tempo. mix_tempo converts mix-time
positions to seconds.

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
    timeline_spans,
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


@dataclass(frozen=True)
class _CompiledPlay:
    deck: int
    mix_start_sample: int
    mix_end_sample: int  # exclusive
    track_start_sample: int  # may be slightly negative (grid anchor tolerance)
    track_id: str
    rate: float  # track-seconds per mix-second; 1.0 = natural tempo


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

        compiled = self._compile_play_segments(plan, sample_rate, mix_tempo, grids)
        if not compiled:
            return RenderResult(
                samples=np.zeros((0, 2), dtype=np.float32),
                sample_rate=sample_rate,
            )

        total_samples = max(c.mix_end_sample for c in compiled)

        deck_buffers: dict[int, np.ndarray] = {}
        for c in compiled:
            buf = deck_buffers.setdefault(
                c.deck, np.zeros((total_samples, 2), dtype=np.float32)
            )
            n = c.mix_end_sample - c.mix_start_sample
            buf[c.mix_start_sample : c.mix_end_sample] += self._segment_audio(
                track_audio[c.track_id], c, n, sample_rate
            )

        gains = self._deck_gains(
            plan, list(deck_buffers), mix_tempo, sample_rate, total_samples
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

    def _compile_play_segments(
        self,
        plan: Plan,
        sample_rate: int,
        mix_tempo: float,
        grids: dict[str, TrackGrid] | None = None,
    ) -> list[_CompiledPlay]:
        return [
            _CompiledPlay(
                deck=sp.deck,
                mix_start_sample=int(round(sp.mix_start_sec * sample_rate)),
                mix_end_sample=int(round(sp.mix_end_sec * sample_rate)),
                track_start_sample=int(round(sp.track_from_sec * sample_rate)),
                track_id=sp.track_id,  # type: ignore[arg-type]
                rate=sp.rate,
            )
            for sp in timeline_spans(plan, mix_tempo, grids)
            if sp.kind == "play"
        ]

    # Extra track audio fed to the stretcher past the segment end, so its
    # end-of-input handling doesn't color the last samples we keep.
    _STRETCH_TAIL_SEC = 0.25

    def _segment_audio(
        self, audio: np.ndarray, c: _CompiledPlay, n: int, sample_rate: int
    ) -> np.ndarray:
        """Exactly `n` mix samples of the segment's audio, stretched if rate != 1.

        Reads outside the track (a slightly negative start from the grid
        anchor, or running past the end) come back as silence.
        """
        if c.rate == 1.0:
            return _read_padded(audio, c.track_start_sample, n)
        tail = int(self._STRETCH_TAIL_SEC * sample_rate)
        n_in = int(np.ceil(n * c.rate)) + tail
        src = _read_padded(audio, c.track_start_sample, n_in)
        return _fit_length(time_stretch(src, sample_rate, c.rate), n)

    def _deck_gains(
        self,
        plan: Plan,
        decks: list[int],
        mix_tempo: float,
        sample_rate: int,
        total_samples: int,
    ) -> dict[int, np.ndarray]:
        transitions = transition_envelopes(plan, mix_tempo)
        xfade_env = crossfader_envelope(plan, mix_tempo)
        xfade = None
        if xfade_env is not None:
            pos = render_envelope(xfade_env, sample_rate, total_samples)
            g1, g2 = crossfader_gains(pos)
            xfade = {1: g1.astype(np.float32), 2: g2.astype(np.float32)}

        gains: dict[int, np.ndarray] = {}
        for deck in decks:
            envs = deck_volume_envelopes(plan, deck, mix_tempo)
            if deck in transitions:
                envs.append(transitions[deck])
            g = np.ones(total_samples, dtype=np.float32)
            for env in envs:
                g *= render_envelope(env, sample_rate, total_samples)
            if xfade is not None and deck in xfade:
                g *= xfade[deck]
            gains[deck] = g
        return gains


def _read_padded(audio: np.ndarray, start: int, n: int) -> np.ndarray:
    """audio[start:start+n], zero-filled wherever that range leaves the track."""
    out = np.zeros((n, audio.shape[1]), dtype=np.float32)
    lo, hi = max(start, 0), min(start + n, audio.shape[0])
    if hi > lo:
        out[lo - start : hi - start] = audio[lo:hi]
    return out


def _fit_length(x: np.ndarray, n: int) -> np.ndarray:
    if x.shape[0] >= n:
        return x[:n]
    return np.concatenate([x, np.zeros((n - x.shape[0], x.shape[1]), dtype=x.dtype)])
