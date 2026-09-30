"""Song structure at bar resolution: an energy/bass map, section boundaries,
and breaks. For finding plan positions by ear (`dj-segue scrub`).

Everything is measured per bar of the track's beat grid (bar N = beats
4N..4N+3, bar 0 starting at beat 0 = the first downbeat):

- `energy`, `bass`: per-bar loudness (full band) and low-band (< 150 Hz,
  kick + bass) level, in dB. The most useful map: builds, drops and
  breakdowns show up as plain rises and falls, with no guessing involved.
- `breaks`: short bass dropouts (1–4 bars) with full bass on both sides —
  the fill before a phrase comes back in, a natural entry point.
- `boundaries`: likely section starts, on 4-bar lines: timbre/harmony/level
  novelty (Foote's checkerboard kernel over a bar self-similarity matrix;
  sections ≥ 8 bars), plus 4-bar lines where the bass level steps by
  BASS_STEP_DB and stays there (a breakdown starting or ending), plus break
  ends.

No section *names*: rule-based labels (intro/build/drop) were unreliable on
real tracks, so the map is shown and the listener decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dj_segue.time_math import TrackGrid

SR = 22050
HOP = 512
BASS_HZ = 150.0
BREAK_MAX_BARS = 4
BREAK_CONTEXT_BARS = 4
BREAK_DROP_DB = 6.0  # bass must fall this far below both neighbours
NOVELTY_HALF_WIDTH = 4  # bars on each side of the checkerboard kernel
BOUNDARY_PERCENTILE = 60
MIN_SECTION_BARS = 8
BASS_STEP_DB = 6.0


@dataclass(frozen=True)
class TrackStructure:
    energy_db: np.ndarray  # per bar
    bass_db: np.ndarray  # per bar
    boundaries: list[int]  # bar numbers where a section starts (excluding 0)
    breaks: list[tuple[int, int]]  # [start_bar, end_bar)

    @property
    def n_bars(self) -> int:
        return len(self.energy_db)

    def to_json(self) -> dict:
        return {
            # Overall loudness varies far less than bass (a breakdown drops
            # the bass ~20 dB but the mix only a few), so it gets a tighter range.
            "energy": _unit(self.energy_db, floor_db=12.0),
            "bass": _unit(self.bass_db),
            "boundaries": self.boundaries,
            "breaks": [list(b) for b in self.breaks],
        }


def analyze_structure(audio_path: Path, grid: TrackGrid) -> TrackStructure:
    import librosa

    y, sr = librosa.load(str(audio_path), sr=SR, mono=True)
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    mel = librosa.power_to_db(librosa.feature.melspectrogram(S=S**2, sr=sr))
    frames = {
        "rms": librosa.feature.rms(S=S)[0],
        "bass": S[freqs < BASS_HZ].sum(axis=0),
        "high": S[freqs > 5000].sum(axis=0),
        "mfcc": librosa.feature.mfcc(S=mel, n_mfcc=13),
        "chroma": librosa.feature.chroma_stft(S=S, sr=sr),
    }
    bar_sec = 4 * 60.0 / grid.bpm
    n_bars = int((len(y) / sr - grid.anchor_sec) // bar_sec)
    edges = librosa.time_to_frames(
        [max(0.0, grid.bar_to_seconds(b)) for b in range(n_bars + 1)], sr=sr, hop_length=HOP
    )
    per_bar = {
        k: np.array([v[..., a:max(b, a + 1)].mean(axis=-1) for a, b in zip(edges, edges[1:])])
        for k, v in frames.items()
    }
    energy = _db(per_bar["rms"])
    bass = _db(per_bar["bass"])
    features = np.hstack(
        [per_bar["mfcc"], per_bar["chroma"],
         np.c_[energy, bass, _db(per_bar["high"])] / 3.0]
    )
    breaks = find_breaks(bass)
    return TrackStructure(
        energy_db=energy,
        bass_db=bass,
        boundaries=find_boundaries(features, bass, breaks),
        breaks=breaks,
    )


def find_breaks(bass_db: np.ndarray) -> list[tuple[int, int]]:
    """Runs of 1–BREAK_MAX_BARS bars whose bass sits BREAK_DROP_DB below the
    bars on both sides (median of BREAK_CONTEXT_BARS each)."""
    n = len(bass_db)
    out: list[tuple[int, int]] = []
    a = 1
    while a < n - 1:
        found = None
        for length in range(1, BREAK_MAX_BARS + 1):
            b = a + length
            if b > n - 1:
                break
            before = np.median(bass_db[max(0, a - BREAK_CONTEXT_BARS) : a])
            after = np.median(bass_db[b : b + BREAK_CONTEXT_BARS])
            floor = min(before, after) - BREAK_DROP_DB
            if np.all(bass_db[a:b] < floor):
                found = (a, b)  # keep extending: prefer the whole dropout
            elif found is not None or bass_db[a] >= floor:
                break
        if found:
            out.append(found)
            a = found[1]
        else:
            a += 1
    return out


def find_boundaries(
    features: np.ndarray, bass_db: np.ndarray, breaks: list[tuple[int, int]]
) -> list[int]:
    n = len(features)
    extra = {b for _, b in breaks if 0 < b < n} | set(_bass_steps(bass_db))
    if n < 2 * MIN_SECTION_BARS:
        return sorted(extra)
    nov = _novelty(features, NOVELTY_HALF_WIDTH)
    cand = [(nov[max(0, b - 1) : b + 2].max(), b) for b in range(4, n - 2, 4)]
    cutoff = np.percentile([s for s, _ in cand], BOUNDARY_PERCENTILE)
    picked = sorted(b for s, b in cand if s >= cutoff)
    out: list[int] = []
    last = 0
    for b in picked:
        if b - last >= MIN_SECTION_BARS:
            out.append(b)
            last = b
    return sorted(set(out) | extra)


def _bass_steps(bass_db: np.ndarray) -> list[int]:
    """4-bar lines where the median bass of the 4 bars after differs from
    the 4 bars before by BASS_STEP_DB."""
    n = len(bass_db)
    return [
        b
        for b in range(4, n - 3, 4)
        if abs(np.median(bass_db[b : b + 4]) - np.median(bass_db[b - 4 : b])) >= BASS_STEP_DB
    ]


def _novelty(F: np.ndarray, k: int) -> np.ndarray:
    F = (F - F.mean(axis=0)) / (F.std(axis=0) + 1e-9)
    F = F / (np.linalg.norm(F, axis=1, keepdims=True) + 1e-9)
    S = F @ F.T
    sign = np.r_[-np.ones(k), np.ones(k)]
    w = np.hanning(2 * k + 2)[1:-1]
    kernel = np.outer(sign * w, sign * w)  # + within blocks, - across
    Sp = np.pad(S, k, mode="edge")
    nov = np.array([np.sum(Sp[i : i + 2 * k, i : i + 2 * k] * kernel) for i in range(len(S))])
    return (nov - nov.min()) / (np.ptp(nov) + 1e-9)


def _db(x: np.ndarray) -> np.ndarray:
    return 20 * np.log10(np.asarray(x, dtype=np.float64) + 1e-9)


def _unit(db: np.ndarray, floor_db: float = 30.0) -> list[float]:
    """dB → 0..1 for display: the top `floor_db` of the track's range."""
    top = float(np.max(db)) if len(db) else 0.0
    return [round(float(v), 3) for v in np.clip((db - (top - floor_db)) / floor_db, 0, 1)]
