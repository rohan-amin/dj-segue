"""Merge aligned lyrics with what Whisper heard.

The lyrics are the backbone: their words, at their aligned times. Whisper is
a second opinion:

  - a lyric word is `confirmed` when Whisper heard the same word within
    CONFIRM_SEC of it (each heard word confirms at most one lyric word);
  - a heard word that matches no lyric word and lands where the lyrics have
    nothing (no lyric word within GAP_SEC) is added with source "whisper" —
    ad-libs, mostly; it's never `confirmed`;
  - with no lyrics, every word is Whisper's, unconfirmed.

Without a Whisper pass, `confirmed` is None (unknown) for every word.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from dj_segue.words.align import Aligned

CONFIRM_SEC = 0.25
GAP_SEC = 0.5


@dataclass
class Word:
    text: str
    start: float
    end: float
    score: float  # alignment score, 0–1
    source: str  # "lyrics" | "whisper"
    confirmed: bool | None  # Whisper heard it here too (None: no Whisper pass)
    line: int | None = None  # lyric line index (lyrics only)

    def to_json(self) -> dict:
        d = asdict(self)
        d["start"], d["end"], d["score"] = round(self.start, 3), round(self.end, 3), round(self.score, 3)
        return d


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def merge(lyrics: list[Aligned] | None, heard: list[Aligned] | None) -> list[Word]:
    if not lyrics:
        return [Word(h.text, h.start, h.end, h.score, "whisper", False) for h in heard or []]
    if heard is None:
        return [Word(w.text, w.start, w.end, w.score, "lyrics", None, w.line) for w in lyrics]
    used = [False] * len(heard)
    words = []
    for w in lyrics:
        key, best = norm(w.text), None
        for i, h in enumerate(heard):
            if used[i] or norm(h.text) != key or not key:
                continue
            d = abs(h.start - w.start)
            if d <= CONFIRM_SEC and (best is None or d < best[0]):
                best = (d, i)
        if best is not None:
            used[best[1]] = True
        words.append(Word(w.text, w.start, w.end, w.score, "lyrics", best is not None, w.line))
    for i, h in enumerate(heard):
        if used[i] or not norm(h.text):
            continue
        near = any(w.start - GAP_SEC <= h.start <= w.end + GAP_SEC for w in lyrics)
        if not near:
            words.append(Word(h.text, h.start, h.end, h.score, "whisper", False))
    words.sort(key=lambda w: w.start)
    return words
