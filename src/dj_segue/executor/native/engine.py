"""Native audio engine — offline WAV render and live sounddevice playback.

M1 scope: `play` segments + `deck_volume` automation. Single sample rate
(taken from the first loaded track; mismatched tracks error out). Tracks
play at their natural bpm; mix_tempo is informational and used only to
convert mix-time positions to seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from dj_segue.executor.base import MixExecutor, RenderResult
from dj_segue.schema.plan import (
    DeckVolumeLane,
    FloatKeyframe,
    PlaySegment,
    Plan,
    SilenceSegment,
    TransitionSegment,
)
from dj_segue.schema.validator import resolved_mix_tempo
from dj_segue.time_math import mix_pos_to_seconds, track_pos_to_seconds


@dataclass(frozen=True)
class _CompiledPlay:
    deck: int
    mix_start_sample: int
    mix_end_sample: int  # exclusive
    track_start_sample: int
    track_id: str


class NativeEngine(MixExecutor):
    def render(self, plan: Plan, audio_root: Path) -> RenderResult:
        for seg in plan.timeline:
            if isinstance(seg, TransitionSegment):
                raise NotImplementedError(
                    "transition segments are M2 scope; native engine v0.1 "
                    "supports `play` and `silence` only"
                )

        audio_root = Path(audio_root).resolve()
        track_audio, sample_rate = self._load_all_tracks(plan, audio_root)
        mix_tempo = resolved_mix_tempo(plan)

        compiled = self._compile_play_segments(plan, sample_rate, mix_tempo)
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
            audio = track_audio[c.track_id]
            n = c.mix_end_sample - c.mix_start_sample
            chunk = audio[c.track_start_sample : c.track_start_sample + n]
            if chunk.shape[0] < n:
                # Track ran out — pad with silence (audio-aware validation
                # would normally have caught this).
                pad = np.zeros((n - chunk.shape[0], 2), dtype=np.float32)
                chunk = np.concatenate([chunk, pad], axis=0)
            buf[c.mix_start_sample : c.mix_end_sample] += chunk

        for deck, buf in deck_buffers.items():
            curve = self._volume_curve(plan, deck, mix_tempo, sample_rate, total_samples)
            buf *= curve[:, None]

        mix = np.sum(list(deck_buffers.values()), axis=0).astype(np.float32)
        return RenderResult(samples=mix, sample_rate=sample_rate)

    def render_to_wav(
        self, plan: Plan, audio_root: Path, out_path: Path
    ) -> RenderResult:
        result = self.render(plan, audio_root)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(out_path, result.samples, result.sample_rate, subtype="PCM_16")
        return result

    def play_live(self, plan: Plan, audio_root: Path) -> RenderResult:
        # Imported lazily so the WAV-render path doesn't require a sound device.
        import sounddevice as sd

        result = self.render(plan, audio_root)
        sd.play(result.samples, samplerate=result.sample_rate, blocking=True)
        return result

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

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
        self, plan: Plan, sample_rate: int, mix_tempo: float
    ) -> list[_CompiledPlay]:
        compiled: list[_CompiledPlay] = []
        deck_cursor: dict[int, float] = {}
        for seg in plan.timeline:
            if isinstance(seg, SilenceSegment):
                # Silence advances the cursor without rendering anything.
                from dj_segue.time_math import duration_to_seconds

                end = deck_cursor.get(seg.deck, 0.0) + duration_to_seconds(
                    seg.duration, mix_tempo
                )
                deck_cursor[seg.deck] = end
                continue
            if not isinstance(seg, PlaySegment):
                continue
            track = plan.tracks[seg.track]
            from_sec = track_pos_to_seconds(seg.from_, track)
            to_sec = track_pos_to_seconds(seg.to, track)
            duration_sec = max(0.0, to_sec - from_sec)
            if seg.start_at is not None:
                start_sec = mix_pos_to_seconds(seg.start_at, mix_tempo)
            else:
                start_sec = deck_cursor.get(seg.deck, 0.0)
            end_sec = start_sec + duration_sec
            compiled.append(
                _CompiledPlay(
                    deck=seg.deck,
                    mix_start_sample=int(round(start_sec * sample_rate)),
                    mix_end_sample=int(round(end_sec * sample_rate)),
                    track_start_sample=int(round(from_sec * sample_rate)),
                    track_id=seg.track,
                )
            )
            deck_cursor[seg.deck] = end_sec
        return compiled

    def _volume_curve(
        self,
        plan: Plan,
        deck: int,
        mix_tempo: float,
        sample_rate: int,
        total_samples: int,
    ) -> np.ndarray:
        lanes = [
            l for l in plan.automation
            if isinstance(l, DeckVolumeLane) and l.deck == deck
        ]
        if not lanes:
            return np.ones(total_samples, dtype=np.float32)
        # If multiple lanes target the same deck, multiply them together.
        out = np.ones(total_samples, dtype=np.float32)
        for lane in lanes:
            out *= self._lane_to_curve(
                lane.keyframes, lane.interpolation, mix_tempo, sample_rate, total_samples
            )
        return out

    def _lane_to_curve(
        self,
        keyframes: list[FloatKeyframe],
        interpolation: str,
        mix_tempo: float,
        sample_rate: int,
        total_samples: int,
    ) -> np.ndarray:
        if not keyframes:
            return np.ones(total_samples, dtype=np.float32)
        kf_samples = [
            int(round(mix_pos_to_seconds(kf.at, mix_tempo) * sample_rate))
            for kf in keyframes
        ]
        kf_values = [float(kf.value) for kf in keyframes]
        out = np.empty(total_samples, dtype=np.float32)
        # Pre-first: hold the first value.
        out[: max(0, kf_samples[0])] = kf_values[0]
        # Between consecutive keyframes.
        for (s1, v1), (s2, v2) in zip(
            zip(kf_samples, kf_values), zip(kf_samples[1:], kf_values[1:])
        ):
            s1c = max(0, min(s1, total_samples))
            s2c = max(0, min(s2, total_samples))
            n = s2c - s1c
            if n <= 0:
                continue
            if interpolation == "step":
                out[s1c:s2c] = v1
            elif interpolation == "linear":
                out[s1c:s2c] = np.linspace(v1, v2, num=n, endpoint=False, dtype=np.float32)
            elif interpolation == "exponential":
                if v1 == 0.0 or v2 == 0.0 or (v1 < 0) != (v2 < 0):
                    out[s1c:s2c] = np.linspace(v1, v2, num=n, endpoint=False, dtype=np.float32)
                else:
                    t = np.linspace(0.0, 1.0, num=n, endpoint=False, dtype=np.float64)
                    out[s1c:s2c] = (v1 * (v2 / v1) ** t).astype(np.float32)
            else:
                out[s1c:s2c] = v1
        # Post-last: hold the last value.
        last_s = min(total_samples, max(0, kf_samples[-1]))
        out[last_s:] = kf_values[-1]
        return out
