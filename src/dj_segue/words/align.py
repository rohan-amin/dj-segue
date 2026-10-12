"""Forced alignment of known text to a vocal stem: when each word is sung.

torchaudio's wav2vec2 (WAV2VEC2_ASR_BASE_960H, the model WhisperX uses for
English) gives per-frame letter probabilities (20 ms frames) over the whole
stem, computed in overlapping chunks. Each span's text is then forced onto
its frames (CTC forced alignment), so every word gets a start, an end and a
score: the mean probability of its letters over the frames each letter is
detected, 0–1. (WhisperX's score also counts the blank frames after each
letter, so it reads higher for the same alignment.)

Text is reduced to what the model spells (A–Z and '); a word with nothing
left (a number, a symbol) gets the gap between its neighbours and score 0.
A span too short for its text is spread evenly, score 0.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

SAMPLE_RATE = 16000
FRAME_SEC = 0.02  # wav2vec2's frame hop (320 samples)
CHUNK_SEC = 30.0  # emissions are computed in chunks this long…
CONTEXT_SEC = 1.0  # …each with this much audio either side for context

_lock = threading.Lock()


@dataclass
class Aligned:
    text: str  # as written
    start: float  # seconds in the audio
    end: float
    score: float  # 0–1
    line: int  # index of the span it came from


def to_mono_16k(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    import librosa

    x = audio.mean(axis=1) if audio.ndim == 2 else audio
    return librosa.resample(x.astype(np.float32), orig_sr=sample_rate, target_sr=SAMPLE_RATE)


def align(audio16k: np.ndarray, spans: list[tuple[float, float, str]]) -> list[Aligned]:
    """Words of each (start, end, text) span, timed within it."""
    emission = emissions(audio16k)
    labels = _model()[1]
    index = {c: i for i, c in enumerate(labels)}
    out: list[Aligned] = []
    for line, (s0, s1, text) in enumerate(spans):
        words = text.split()
        if not words:
            continue
        f0 = max(0, int(s0 / FRAME_SEC))
        f1 = min(len(emission), int(np.ceil(s1 / FRAME_SEC)))
        out += _align_span(emission[f0:f1], f0 * FRAME_SEC, words, index, line)
    return out


def _align_span(em, offset: float, words: list[str], index: dict, line: int) -> list[Aligned]:
    import torch
    import torchaudio.functional as F

    spelled = [re.sub(r"[^A-Z']", "", w.upper()) for w in words]
    chars = "|".join(s for s in spelled if s)
    targets = [index[c] for c in chars]
    times: list[tuple[float, float, float] | None] = [None] * len(words)
    repeats = sum(a == b for a, b in zip(targets, targets[1:]))
    if targets and len(em) >= len(targets) + repeats:  # CTC needs a frame per letter
        try:
            path, scores = F.forced_align(
                torch.from_numpy(em)[None], torch.tensor([targets], dtype=torch.int32), blank=0
            )
            spans = F.merge_tokens(path[0], scores[0].exp())
        except RuntimeError:
            spans = None
        if spans is not None and len(spans) == len(targets):
            k = 0  # walk the letter spans, word by word ("|" separates)
            for wi, s in enumerate(spelled):
                if not s:
                    continue
                mine = spans[k : k + len(s)]
                k += len(s) + 1  # skip the "|"
                times[wi] = (
                    offset + mine[0].start * FRAME_SEC,
                    offset + mine[-1].end * FRAME_SEC,
                    float(np.mean([t.score for t in mine])),
                )
    if all(t is None for t in times):  # couldn't align: spread evenly
        t0, n = offset, len(words)
        step = len(em) * FRAME_SEC / n
        times = [(t0 + i * step, t0 + (i + 1) * step, 0.0) for i in range(n)]
    _fill_gaps(times, offset, offset + len(em) * FRAME_SEC)
    return [Aligned(w, a, b, sc, line) for w, (a, b, sc) in zip(words, times)]


def _fill_gaps(times: list, start: float, end: float) -> None:
    """Unaligned words take the gap between their aligned neighbours."""
    i = 0
    while i < len(times):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(times) and times[j] is None:
            j += 1
        a = times[i - 1][1] if i > 0 else start
        b = times[j][0] if j < len(times) else end
        step = max(b - a, 0.0) / (j - i)
        for k in range(i, j):
            times[k] = (a + (k - i) * step, a + (k - i + 1) * step, 0.0)
        i = j


def emissions(audio16k: np.ndarray) -> np.ndarray:
    """Log letter probabilities per 20 ms frame (frames × labels), float32."""
    import torch

    model = _model()[0]
    n = len(audio16k)
    frames = n // 320
    chunk, ctx = int(CHUNK_SEC * SAMPLE_RATE), int(CONTEXT_SEC * SAMPLE_RATE)
    out = []
    with _lock, torch.inference_mode():
        for a in range(0, n, chunk):
            b = min(n, a + chunk)
            lo, hi = max(0, a - ctx), min(n, b + ctx)
            x = torch.from_numpy(np.ascontiguousarray(audio16k[lo:hi], dtype=np.float32))[None]
            em, _ = model(x)
            em = torch.log_softmax(em[0], dim=-1).numpy()
            skip = (a - lo) // 320
            out.append(em[skip : skip + (b - a) // 320])
    em = np.concatenate(out) if out else np.zeros((0, len(_model()[1])), np.float32)
    if len(em) < frames:  # chunk rounding: pad with blanks
        pad = np.full((frames - len(em), em.shape[1]), -30.0, np.float32)
        pad[:, 0] = 0.0
        em = np.concatenate([em, pad])
    return em[:frames].astype(np.float32)


@lru_cache(maxsize=1)
def _model():
    import torchaudio

    bundle = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H
    return bundle.get_model().eval(), bundle.get_labels()
