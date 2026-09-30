"""Tempo, fixed beat grid, and downbeat detection.

The grid model is a straight line, `t(beat) = anchor + beat * 60 / bpm` — right
for constant-tempo music (most dance music). We fit it directly to onset
energy rather than trusting a beat tracker: librosa's beat tracker slips
between on- and off-beats and its tempo estimate is coarse (it put a 125.00
BPM track at 126.05), and over a 5-minute track a 0.1 BPM error is a
half-beat of drift.

Method:
  1. Onset-strength envelope at a fine hop (~2.9 ms), peak-picked into a
     sparse list of onsets. For each onset we keep its full-band strength and
     its low-band (< 200 Hz, i.e. kick/bass) strength. That list is cached.
  2. Coarse tempo from librosa (good at the neighbourhood, and sets the
     octave — 62 vs 124 BPM).
  3. Comb search over bpm within ±3% of the coarse value: fold onset times by
     the beat period and keep the (bpm, phase) whose folded energy is most
     concentrated. Binned (~1.5 ms), so neighbouring tempos tie.
  4. Half-beat check: a comb can't tell the beat from the off-beat, and loud
     off-beat hi-hats (common in house) pull it half a beat off. Compare the
     low-band energy on the grid vs. the grid shifted half a beat, and take the
     one with the kick on it.
  5. Refine by weighted least squares: onsets within ±8% of a beat get an
     integer beat index k; fit t = anchor + k * period. Iterated.
  6. Round the bpm to a musical value (whole, then 1/2, 1/3, 1/4 BPM) when
     doing so drifts less than ROUND_MAX_DRIFT over the track — removing the
     last fraction of a millisecond of error on tracks produced at e.g. 125.
  7. Downbeats (analyzer/downbeat.py, optional): beat_this votes on which grid
     beat is a bar's "1"; `grid_anchor` moves beat 0 onto it.

`beat_anchor` is the beat phase: the first grid line at or after t = -20 ms,
in [-0.02, period - 0.02). `grid_anchor` is the first *downbeat* at or after
t = -20 ms, in [-0.02, 4 * period - 0.02) — that's beat 0 of the track.

Audio with no clear pulse (e.g. the pure-sine test fixtures) yields
`detected_bpm = None`; a plan must then declare the track's `bpm`, and the
anchor is 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np

from dj_segue.analyzer.downbeat import (
    BEATS_PER_BAR,
    detect_downbeats,
    downbeat_model_id,
    downbeat_offset,
)
from dj_segue.constants import ANCHOR_TOLERANCE

ANALYZER_ID = (
    f"segue-grid-2/librosa-{librosa.__version__}/{downbeat_model_id() or 'no-downbeats'}"
)

HOP = 128  # ~2.9 ms at 44.1 kHz
LOW_BAND_HZ = 200.0  # kick/bass band for the half-beat check
MIN_ONSETS = 16  # fewer peaks than this → no detectable pulse
SEARCH_SPAN = 0.03  # ± fraction of the coarse tempo
COARSE_STEP = 0.005  # bpm
REFINE_TOLERANCE = 0.08  # fraction of a beat an onset may sit off-grid
REFINE_ITERATIONS = 3
HALF_BEAT_MARGIN = 1.5  # off-beat must carry this much more low-band energy to flip
ROUND_STEPS = (1.0, 1 / 2, 1 / 3, 1 / 4)  # bpm; coarsest first
ROUND_MAX_DRIFT = 0.010  # s of accumulated drift over the track's onsets


def _normalize_anchor(anchor: float, span: float) -> float:
    return float((anchor + ANCHOR_TOLERANCE) % span - ANCHOR_TOLERANCE)


@dataclass(frozen=True)
class BeatAnalysis:
    detected_bpm: float | None  # None → no detectable pulse
    grid_anchor_sec: float  # beat phase at detected_bpm (0 when undetected)
    onset_times: np.ndarray  # seconds, sparse peaks
    onset_weights: np.ndarray  # full-band peak strengths (≥ 0)
    onset_low_weights: np.ndarray  # low-band (< 200 Hz) strength at each peak
    downbeat_times: np.ndarray | None  # None → downbeat model unavailable
    sample_rate: int
    n_samples: int

    @property
    def duration_sec(self) -> float:
        return self.n_samples / self.sample_rate

    def beat_anchor(self, bpm: float) -> float:
        """Beat phase at `bpm` (the detected one, or a declared override).

        Uses the cached onsets, so no audio re-analysis. 0 when there are no
        onsets to fit (beat 0 == sample 0).
        """
        if self.detected_bpm is not None and bpm == self.detected_bpm:
            return self.grid_anchor_sec
        if len(self.onset_times) < MIN_ONSETS:
            return 0.0
        anchor, _ = _fit_phase(self.onset_times, self.onset_weights, bpm)
        return _prefer_kick_phase(self.onset_times, self.onset_low_weights, bpm, anchor)

    def grid_anchor(self, bpm: float) -> float:
        """Beat 0 of the track: the first downbeat at or after the file start.

        Falls back to the beat phase when downbeats are unavailable or
        inconclusive.
        """
        anchor = self.beat_anchor(bpm)
        period = 60.0 / bpm
        shift = downbeat_offset(self.downbeat_times, anchor, bpm)
        return _normalize_anchor(anchor + shift * period, BEATS_PER_BAR * period)


def analyze_audio(audio_path: Path) -> BeatAnalysis:
    y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    times, weights, low = detect_onsets(y, sr)
    detected_bpm: float | None = None
    anchor = 0.0
    downbeats: np.ndarray | None = None
    if len(times) >= MIN_ONSETS:
        coarse = float(np.atleast_1d(librosa.feature.tempo(y=y, sr=sr))[0])
        if coarse > 0:
            detected_bpm, anchor = fit_grid(times, weights, low, coarse)
            downbeats = detect_downbeats(y, sr)
    return BeatAnalysis(
        detected_bpm=detected_bpm,
        grid_anchor_sec=anchor,
        onset_times=times,
        onset_weights=weights,
        onset_low_weights=low,
        downbeat_times=downbeats,
        sample_rate=int(sr),
        n_samples=int(len(y)),
    )


def detect_onsets(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(times, full-band weights, low-band weights) of onset peaks."""
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    env = np.maximum(env - np.median(env), 0.0)
    if not np.any(env > 0):
        return np.zeros(0), np.zeros(0), np.zeros(0)
    wait = max(1, int(0.03 * sr / HOP))  # ≥ 30 ms between peaks
    peaks = librosa.util.peak_pick(
        env, pre_max=3, post_max=3, pre_avg=wait, post_avg=wait,
        delta=0.1 * float(env.max()), wait=wait,
    )
    low_env = librosa.onset.onset_strength(
        y=y, sr=sr, hop_length=HOP, n_fft=4096, n_mels=8, fmax=LOW_BAND_HZ
    )
    low_env = np.maximum(low_env - np.median(low_env), 0.0)
    # A kick's low-band flux can peak a frame or two off the broadband peak.
    lo_idx = np.clip(peaks[:, None] + np.arange(-3, 4)[None, :], 0, len(low_env) - 1)
    low = low_env[lo_idx].max(axis=1) if len(peaks) else np.zeros(0)
    # Spectral flux peaks one frame after the attack (it compares frame n with
    # n-1); shift back one hop. Measured on synthetic clicks: +2.7 ms → ~0.
    times = (peaks - 1) * HOP / sr
    return times.astype(np.float64), env[peaks].astype(np.float64), low.astype(np.float64)


def fit_grid(
    times: np.ndarray, weights: np.ndarray, low: np.ndarray, coarse_bpm: float
) -> tuple[float, float]:
    """Best (bpm, beat anchor) near `coarse_bpm`. See module docstring."""
    lo, hi = coarse_bpm * (1 - SEARCH_SPAN), coarse_bpm * (1 + SEARCH_SPAN)
    bpm = _best_bpm(times, weights, np.arange(lo, hi, COARSE_STEP))
    anchor, _ = _fit_phase(times, weights, bpm)
    anchor = _prefer_kick_phase(times, low, bpm, anchor)
    period = 60.0 / bpm
    for _ in range(REFINE_ITERATIONS):
        k = np.round((times - anchor) / period)
        on_grid = np.abs(times - (anchor + k * period)) < REFINE_TOLERANCE * period
        if on_grid.sum() < MIN_ONSETS:
            break
        sw = np.sqrt(weights[on_grid])
        A = np.column_stack([np.ones(on_grid.sum()), k[on_grid]]) * sw[:, None]
        (anchor, period), *_ = np.linalg.lstsq(A, times[on_grid] * sw, rcond=None)
    bpm, anchor = _round_bpm(times, weights, 60.0 / period, anchor)
    return bpm, _normalize_anchor(anchor, 60.0 / bpm)


def _prefer_kick_phase(times: np.ndarray, low: np.ndarray, bpm: float, anchor: float) -> float:
    """Return `anchor` or `anchor + half a beat`, whichever the kick sits on."""
    period = 60.0 / bpm

    def low_energy_on(a: float) -> float:
        d = (times - a + period / 2) % period - period / 2
        return float(low[np.abs(d) < REFINE_TOLERANCE * period].sum())

    here, half = low_energy_on(anchor), low_energy_on(anchor + period / 2)
    if half > HALF_BEAT_MARGIN * here:
        return _normalize_anchor(anchor + period / 2, period)
    return anchor


def _round_bpm(
    times: np.ndarray, weights: np.ndarray, bpm: float, anchor: float
) -> tuple[float, float]:
    """Snap bpm to the coarsest musical step whose drift over the onsets is tiny,
    re-fitting the anchor at the new tempo. Unchanged if none qualifies."""
    period = 60.0 / bpm
    k = np.round((times - anchor) / period)
    on_grid = np.abs(times - (anchor + k * period)) < REFINE_TOLERANCE * period
    if on_grid.sum() < MIN_ONSETS:
        return bpm, anchor
    beats_spanned = float(k[on_grid].max() - k[on_grid].min())
    for step in ROUND_STEPS:
        cand = round(bpm / step) * step
        drift = beats_spanned * abs(60.0 / cand - period)
        if drift <= ROUND_MAX_DRIFT:
            new_period = 60.0 / cand
            resid = times[on_grid] - k[on_grid] * new_period
            return float(cand), float(np.average(resid, weights=weights[on_grid]))
    return bpm, anchor


def _best_bpm(times: np.ndarray, weights: np.ndarray, candidates: np.ndarray) -> float:
    scores = [_fit_phase(times, weights, b)[1] for b in candidates]
    return float(candidates[int(np.argmax(scores))])


def _fit_phase(times: np.ndarray, weights: np.ndarray, bpm: float) -> tuple[float, float]:
    """(anchor_sec, concentration) for a fixed bpm.

    Folds onsets by the period into ~1.5 ms bins, smooths, takes the peak bin,
    then refines with a weighted circular mean of the onsets near that peak.
    """
    period = 60.0 / bpm
    nbins = max(8, int(round(period / 0.0015)))
    phase = (times % period) / period  # [0, 1)
    hist = np.bincount((phase * nbins).astype(int) % nbins, weights=weights, minlength=nbins)
    kernel = np.ones(5) / 5
    smooth = np.convolve(np.r_[hist[-2:], hist, hist[:2]], kernel, mode="valid")
    k = int(np.argmax(smooth))
    concentration = float(smooth[k] / max(weights.sum(), 1e-12) * nbins)

    center = (k + 0.5) / nbins
    d = (phase - center + 0.5) % 1.0 - 0.5  # signed distance, wraps
    near = np.abs(d) < 0.05  # ±5% of a beat (~24 ms at 125 BPM)
    if np.any(near):
        center += float(np.average(d[near], weights=weights[near] + 1e-12))
    return _normalize_anchor((center % 1.0) * period, period), concentration
