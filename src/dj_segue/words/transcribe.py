"""What Whisper hears in a vocal stem: lines of text with rough times.

faster-whisper, large-v3-turbo, greedy, on the CPU (int8): ~22 s for a
2:20 track. Chosen on real tracks (2026-10-11): without a repetition penalty
turbo looped on chants (one word ×203); voice-activity gating skipped whole
verses; large-v3 with beam search confirmed no more lyric words and took
15× as long. The penalty also stops Whisper hearing real chants (a syllable
repeated), so lyric words in chant sections go unconfirmed. Word times come from
`align`, not Whisper.
"""

from __future__ import annotations

import os
from functools import lru_cache

import numpy as np

MODEL = "large-v3-turbo"
OPTIONS = dict(
    language="en",
    beam_size=1,
    condition_on_previous_text=False,  # one bad line doesn't seed the next
    repetition_penalty=1.15,
    vad_filter=False,
)


def transcribe(audio16k: np.ndarray) -> list[tuple[float, float, str]]:
    segments, _ = _model().transcribe(audio16k.astype(np.float32), **OPTIONS)
    return [(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel

    return WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
