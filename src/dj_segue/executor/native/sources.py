"""Deck audio before gain, readable block by block.

A `DeckSource` produces one deck's (or one stem of a deck's) audio on the
mix's sample clock, in consecutive `read(a, b)` calls. The offline engine
reads each deck in one call; the live engine reads blocks as it plays. Both
get the same samples: every piece here is deterministic and independent of
how it's read (see `stretch.Stretcher`).

Segments: a play segment reads its track from `from`, time-stretched when
its rate isn't 1. A loop renders its region once (from a pre-roll before the
loop start, stretched) and replays it per repetition.

Seams (loop repetitions, and jumps between back-to-back segments on a deck)
are short linear crossfades placed just *before* the boundary: the outgoing
audio fades out over its last LOOP_SEAM_SEC while the incoming fades in from
a pre-roll of track audio before its start — so the transient on the first
beat is untouched and nothing clicks.

Tempo changes (v0.9): a segment with a `warp` follows the mix tempo, so
it's stretched with a varying rate (`Stretcher(rate_at=…)`); a warped loop
renders each repetition on its own, since each lands on different mix beats.

Seeking (`start` > 0) begins mid-mix, for live playback from a position: a
stretched segment entered part-way is warmed up for SEEK_WARMUP_SEC first,
so it sounds right but isn't bit-identical to playing from the top.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from dj_segue.executor.native.stretch import Stretcher
from dj_segue.time_math import Warp

# Loop / jump seam crossfade length (seconds of mix time).
LOOP_SEAM_SEC = 0.003
# Stretched audio entered part-way (a seek) is run this long before use.
SEEK_WARMUP_SEC = 0.5


@dataclass(frozen=True)
class CompiledPlay:
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
    # Follows a changing mix tempo (v0.9); `rate` is then the rate at the
    # start. The warp's track position is for rep 0 (or the play).
    warp: Warp | None = None


def read_padded(audio: np.ndarray, start: int, n: int) -> np.ndarray:
    """audio[start:start+n], zero-filled wherever that range leaves the track."""
    out = np.zeros((n, audio.shape[1]), dtype=np.float32)
    lo, hi = max(start, 0), min(start + n, audio.shape[0])
    if hi > lo:
        out[lo - start : hi - start] = audio[lo:hi]
    return out


def ramp(n: int, *, rising: bool) -> np.ndarray:
    """Linear fade of n samples; a rising and a falling ramp sum to 1 per sample."""
    t = (np.arange(n, dtype=np.float32) + 0.5) / n
    return t if rising else 1.0 - t


def needs_seam(prev: CompiledPlay, c: CompiledPlay) -> bool:
    """True when `c` starts right as `prev` (same deck) ends and the audio
    jumps. A plain continuation — same track, natural tempo, `c` starting
    where `prev` stopped — needs none."""
    if c.mix_start_sample != prev.mix_end_sample:
        return False
    if prev.rep_starts or c.track_id != prev.track_id or prev.rate != 1.0 or c.rate != 1.0:
        return True
    if prev.warp is not None or c.warp is not None:
        return True
    prev_track_end = prev.track_start_sample + (prev.mix_end_sample - prev.mix_start_sample)
    return c.track_start_sample != prev_track_end


class _Segment:
    """One segment's audio: output index 0 is mix sample mix_start - lead.
    Reads must be consecutive (`read(k, n)` with k = the previous end)."""

    def __init__(
        self, audio: np.ndarray, c: CompiledPlay, lead: int, fade_out: int,
        sample_rate: int, first: int = 0,
    ) -> None:
        self.c, self.lead, self.fade_out = c, lead, fade_out
        self.length = lead + c.mix_end_sample - c.mix_start_sample
        self.audio, self.sr = audio, sample_rate
        self.loop: np.ndarray | None = None
        self.stretch: Stretcher | None = None
        pre_track = int(round(lead * c.rate))
        self.track_start = c.track_start_sample - pre_track
        if c.rep_starts:
            self.loop = self._loop_audio()
        elif c.rate != 1.0 or c.warp is not None:
            warm = min(first, int(round(SEEK_WARMUP_SEC * sample_rate)))
            begin = first - warm  # output index the stretcher starts at
            if c.warp is None:
                self.stretch = Stretcher(
                    lambda s, n: read_padded(audio, s, n),
                    self.track_start + int(round(begin * c.rate)), c.rate, sample_rate,
                )
            else:
                self.stretch = warped_stretcher(
                    audio, c.warp, c.mix_start_sample - lead + begin, sample_rate
                )
            if warm:
                self.stretch.pull(warm)

    def read(self, k: int, n: int) -> np.ndarray:
        """Output samples [k, k + n), with the seam fades applied."""
        if self.loop is not None:
            out = self.loop[k : k + n].copy()
        elif self.stretch is not None:
            out = self.stretch.pull(n)
        else:
            out = read_padded(self.audio, self.track_start + k, n)
        idx = np.arange(k, k + n)
        if self.lead and self.loop is None and k < self.lead:
            m = idx < self.lead
            out[m] *= ramp(self.lead, rising=True)[idx[m]][:, None]
        if self.fade_out and k + n > self.length - self.fade_out:
            m = idx >= self.length - self.fade_out
            out[m] *= ramp(self.fade_out, rising=False)[idx[m] - (self.length - self.fade_out)][:, None]
        return out

    def _loop_audio(self) -> np.ndarray:
        """The whole loop span: every rep replays the region from its start.

        The region (longest rep, plus a pre-roll of `seam` mix samples
        before the loop start) is brought to mix rate once; shorter reps use
        a prefix of it. Rep k>0 is written from `seam` samples before its
        start, fading in over the pre-roll while rep k-1 fades out over the
        same samples, so the two sum to a linear crossfade ending exactly on
        the boundary. With `lead` (≤ seam), rep 0 does the same over the
        first `lead` samples of the output.
        """
        c, sr, lead = self.c, self.sr, self.lead
        n = c.mix_end_sample - c.mix_start_sample
        seam = int(round(LOOP_SEAM_SEC * sr))
        pre_track = int(round(seam * c.rate))
        region_mix = int(np.ceil(c.loop_track_samples / c.rate)) + 2  # rounding slack
        start = c.track_start_sample - pre_track
        src = None
        if c.warp is not None:
            pass  # rendered per rep below
        elif c.rate == 1.0:
            src = read_padded(self.audio, start, seam + region_mix)
        else:
            src = Stretcher(
                lambda s, k: read_padded(self.audio, s, k), start, c.rate, sr
            ).pull(seam + region_mix)
        out = np.zeros((lead + n, self.audio.shape[1]), dtype=np.float32)
        starts = [s - c.mix_start_sample + lead for s in c.rep_starts] + [lead + n]
        for k, (a, b) in enumerate(zip(starts, starts[1:])):
            length = b - a
            if c.warp is not None:
                rep_start = c.rep_starts[k]
                warp = c.warp.starting(rep_start / sr)
                src = warped_stretcher(self.audio, warp, rep_start - seam, sr).pull(
                    seam + length
                )
            # Keep fades inside short reps (not reachable at musical lengths).
            f_in = min(seam, length // 2) if k > 0 else lead
            f_out = min(seam, length // 2) if b < lead + n else 0
            chunk = src[seam - f_in : seam + length].copy()
            if f_in:
                chunk[:f_in] *= ramp(f_in, rising=True)[:, None]
            if f_out:
                chunk[-f_out:] *= ramp(f_out, rising=False)[:, None]
            out[a - f_in : b] += chunk
        return out


def warped_stretcher(audio: np.ndarray, warp: Warp, mix_sample: int, sample_rate: int) -> Stretcher:
    """A stretcher whose output frame 0 is mix sample `mix_sample` and whose
    rate follows `warp`."""
    sr = sample_rate
    start = int(round(warp.track_sec(mix_sample / sr) * sr))
    steps = warp.steps(mix_sample / sr, math.inf)
    return Stretcher(
        lambda s, n: read_padded(audio, s, n), start, 1.0, sr,
        rate_at=lambda frame: warp.rate_at_track(frame / sr),
        breaks=[int(round(t * sr)) for t in steps],
    )


class DeckSource:
    """One deck's audio (or one stem of it): `read(a, b)` for consecutive
    ranges of mix samples, starting at `start`.

    `segments` are the deck's spans in start order, each with its `lead`
    (pre-roll seam); `audio(track_id)` gives the source audio to read.
    """

    def __init__(
        self,
        segments: list[tuple[CompiledPlay, int]],
        audio,
        sample_rate: int,
        start: int = 0,
    ) -> None:
        self.items = []
        for i, (c, lead) in enumerate(segments):
            nxt = segments[i + 1][1] if i + 1 < len(segments) else 0
            self.items.append((c, lead, nxt))
        self.audio, self.sr = audio, sample_rate
        self.pos = start
        self.live: dict[int, tuple[_Segment, int]] = {}  # index → (segment, next read k)

    def read(self, a: int, b: int) -> np.ndarray:
        assert a == self.pos, "DeckSource reads must be consecutive"
        out = np.zeros((b - a, 2), dtype=np.float32)
        for i, (c, lead, fade_out) in enumerate(self.items):
            lo, hi = c.mix_start_sample - lead, c.mix_end_sample
            if hi <= a or lo >= b:
                if hi <= a:
                    self.live.pop(i, None)
                continue
            r0, r1 = max(a, lo), min(b, hi)
            if i not in self.live:
                seg = _Segment(self.audio(c.track_id), c, lead, fade_out, self.sr, r0 - lo)
                self.live[i] = (seg, r0 - lo)
            seg, k = self.live[i]
            assert k == r0 - lo
            out[r0 - a : r1 - a] += seg.read(k, r1 - r0)
            self.live[i] = (seg, r1 - lo)
        self.pos = b
        return out


def deck_segments(compiled: list[CompiledPlay], sample_rate: int) -> dict[int, list]:
    """deck → [(segment, lead), …] in start order; `lead` is the seam
    pre-roll when the audio jumps from the previous segment."""
    seam = int(round(LOOP_SEAM_SEC * sample_rate))
    out: dict[int, list] = {}
    prev_on_deck: dict[int, CompiledPlay] = {}
    for c in sorted(compiled, key=lambda c: (c.deck, c.mix_start_sample)):
        prev = prev_on_deck.get(c.deck)
        prev_on_deck[c.deck] = c
        lead = 0
        if prev is not None and needs_seam(prev, c):
            n = c.mix_end_sample - c.mix_start_sample
            lead = min(seam, n, prev.mix_end_sample - prev.mix_start_sample)
        out.setdefault(c.deck, []).append((c, lead))
    return out
