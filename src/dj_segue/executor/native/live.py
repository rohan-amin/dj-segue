"""Real-time playback: the engine run block by block, ahead of the speakers.

`LiveEngine` produces the mix in consecutive blocks with exactly the offline
engine's stages (sources → chains → echo tails → limiter), each carrying its
state from block to block, so played from the top it gives the same samples
as `NativeEngine.render` — whatever the block sizes.

Three cursors keep the stages in order:
    s1  decks processed (sources and chains) up to here
    s2  pre-limiter mix up to s1 − one seam: an echo tail starts a seam
        before its cut and needs the deck's signal up to the cut
    s3  output up to s2 − the limiter's lookahead

`play()` runs a producer thread that keeps a ring buffer ~LEAD_SEC ahead of
the sound card; the audio callback only copies out of it (no locks, no
allocation, no I/O). With `watch`, edits to the plan file are applied while
playing: a new engine is built for the new plan, starting a little ahead of
what's already queued, and the switch is a SWITCH_XF_SEC crossfade. A jump
(`Player.jump`: a seek, or the tune page's loop going round) is the same
crossfade into an engine started somewhere else in the mix.

Starting part-way (`start` > 0, a seek or a reload) warms everything up
from START_WARMUP_SEC earlier, so it sounds right; but only playback from
the top is bit-identical to an offline render (stretchers, filters and echo
captures need their history).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from dj_segue.executor.native.engine import (
    Prepared,
    capture_range,
    echo_tail,
    prepare,
)
from dj_segue.executor.native.limiter import context_samples as limiter_context
from dj_segue.executor.native.limiter import limit
from dj_segue.executor.native.sources import LOOP_SEAM_SEC, read_padded

BLOCK = 2048  # frames per engine step
LEAD_SEC = 0.4  # how far the producer keeps ahead of the sound card
RING_SEC = 2.0
START_WARMUP_SEC = 1.0
SWITCH_XF_SEC = 0.02
SWITCH_AHEAD_SEC = 0.3  # a reload applies this far past what's already queued


class LiveEngine:
    def __init__(self, p: Prepared, *, limited: bool = True, start: int = 0) -> None:
        self.p = p
        self.sample_rate = sr = p.sample_rate
        self.total = p.total_samples
        self.limited = limited
        self.seam = int(round(LOOP_SEAM_SEC * sr))
        self.before, self.after = limiter_context(sr) if limited else (0, 0)
        self.echoes = p.echo_windows()
        start = max(0, min(start, self.total))
        captures = [capture_range(w, sr)[1] for _, w in self.echoes]
        warm = int(round(START_WARMUP_SEC * sr)) + max(captures, default=0)
        w0 = 0 if start == 0 else max(0, start - warm)
        self.start = start

        decks = sorted(p.segments)
        self.sources = {}
        for d in decks:
            if d in p.stem_decks:
                self.sources[d] = {s: p.source(d, s, w0) for s in p.stem_decks[d]}
            else:
                self.sources[d] = p.source(d, None, w0)
        self.chains = {d: p.chain(d) for d in decks}
        echo_decks = {d for d, _ in self.echoes}
        n = self.total
        self.history = {d: np.zeros((n, 2), dtype=np.float32) for d in echo_decks & set(decks)}
        self.dsum = np.zeros((n, 2), dtype=np.float32)  # decks summed, before tails
        self.premix = np.zeros((n, 2), dtype=np.float32)  # + tails: the limiter's input
        self.tails: list = [None] * len(self.echoes)
        self.s1 = self.s2 = self.s3 = w0
        self.spill = np.zeros((0, 2), dtype=np.float32)  # produced, not yet read

    @property
    def position(self) -> int:
        """The mix sample the next `read` starts at."""
        return max(self.start, self.s3 - len(self.spill))

    @property
    def done(self) -> bool:
        return self.s3 >= self.total and not len(self.spill)

    def read(self, n: int) -> np.ndarray:
        """The next n output frames (fewer only at the end of the mix)."""
        parts, got = [self.spill], len(self.spill)
        while got < n and self.s3 < self.total:
            before = self.s3
            chunk = self._step()
            if self.s3 <= self.start:
                continue  # still warming up
            if before < self.start:
                chunk = chunk[self.start - before :]
            parts.append(chunk)
            got += len(chunk)
        y = np.concatenate(parts)
        self.spill = y[n:]
        return y[:n]

    def _step(self) -> np.ndarray:
        """Advance every stage by up to BLOCK frames; return new output."""
        p, sr, total = self.p, self.sample_rate, self.total
        a, b = self.s1, min(self.s1 + BLOCK, total)
        if b > a:
            acc = np.zeros((b - a, 2), dtype=np.float32)
            for d in sorted(self.sources):
                src = self.sources[d]
                audio = ({s: x.read(a, b) for s, x in src.items()} if isinstance(src, dict)
                         else src.read(a, b))
                sig = self.chains[d].process(a, b, audio)
                acc += sig
                if d in self.history:
                    self.history[d][a:b] = sig
            self.dsum[a:b] = acc
            self.s1 = b
        for i, (d, w) in enumerate(self.echoes):
            if self.tails[i] is None and d in self.history:
                start, length = capture_range(w, sr)
                if start + length <= self.s1:
                    self.tails[i] = echo_tail(read_padded(self.history[d], start, length), w, sr)
        s2 = total if self.s1 >= total else max(self.s2, self.s1 - self.seam)
        if s2 > self.s2:
            seg = self.dsum[self.s2:s2].copy()
            for t in self.tails:
                if t is None:
                    continue
                lo, hi = max(self.s2, t.start), min(s2, t.start + len(t.samples))
                if hi > lo:
                    seg[lo - self.s2 : hi - self.s2] += t.samples[lo - t.start : hi - t.start]
            self.premix[self.s2:s2] = seg
            self.s2 = s2
        s3 = total if self.s2 >= total else max(self.s3, self.s2 - self.after)
        if s3 <= self.s3:
            return np.zeros((0, 2), dtype=np.float32)
        lo, hi = max(0, self.s3 - self.before), min(total, s3 + self.after)
        x = self.premix[lo:hi]
        if self.limited:
            x = limit(x, sr)
        out = x[self.s3 - lo : s3 - lo]
        self.s3 = s3
        return out


# ---------------------------------------------------------------------------
# playback
# ---------------------------------------------------------------------------


class _Ring:
    """Single-producer / single-consumer ring of stereo frames. Indices only
    grow; each is written by one thread (assignment of an int is atomic)."""

    def __init__(self, frames: int) -> None:
        self.buf = np.zeros((frames, 2), dtype=np.float32)
        self.w = 0  # producer
        self.r = 0  # consumer
        self.finished = False

    def space(self) -> int:
        return len(self.buf) - (self.w - self.r)

    def queued(self) -> int:
        return self.w - self.r

    def write(self, x: np.ndarray) -> None:
        n, size = len(x), len(self.buf)
        i = self.w % size
        k = min(n, size - i)
        self.buf[i : i + k] = x[:k]
        self.buf[: n - k] = x[k:]
        self.w += n

    def read_into(self, out: np.ndarray) -> int:
        n = min(len(out), self.w - self.r)
        size = len(self.buf)
        i = self.r % size
        k = min(n, size - i)
        out[:k] = self.buf[i : i + k]
        out[k:n] = self.buf[: n - k]
        out[n:] = 0
        self.r += n
        return n


class Player:
    """Plays a LiveEngine, swapping in others while it plays: a new plan
    taking over where the old one is (`schedule`), or a jump elsewhere in
    the mix (`jump`).
    """

    def __init__(self, engine: LiveEngine, *, log=print) -> None:
        self.engine = engine
        self.sr = engine.sample_rate
        self.ring = _Ring(int(RING_SEC * self.sr))
        # (mix sample at which to switch, LiveEngine, jump?) waiting to take over
        self.pending = None
        self.stop = threading.Event()
        self.underruns = 0
        self.log = log
        # (ring frame, mix sample it holds): where playback started and
        # each jump landed, to tell what the card is playing.
        self.marks = [(0, engine.position)]

    # -- producer ---------------------------------------------------------------

    def produce(self) -> None:
        lead = int(LEAD_SEC * self.sr)
        while not self.stop.is_set():
            if self.ring.queued() >= lead:
                time.sleep(0.005)
                continue
            n = min(BLOCK, self.ring.space())
            pending = self.pending
            if pending is not None:
                at, new, jump = pending
                left = at - self.engine.position
                if left <= 0:
                    self._switch(new, jump)
                    continue
                n = min(n, left)
            x = self.engine.read(n)
            if len(x) == 0:
                if self.pending is None:
                    self.ring.finished = True
                    return
                continue
            self.ring.write(x)

    def _switch(self, new: LiveEngine, jump: bool = False) -> None:
        # Building `new` may have taken longer than the head start it was
        # given: skip it forward to where the old engine is.
        if not jump and new.position < self.engine.position:
            new.read(self.engine.position - new.position)
        if jump:
            self.marks.append((self.ring.w, new.position))
        xf = int(SWITCH_XF_SEC * self.sr)
        old = self.engine.read(xf)
        nxt = new.read(xf)
        m = min(len(old), len(nxt))
        t = ((np.arange(m, dtype=np.float32) + 0.5) / max(m, 1))[:, None]
        self.ring.write(old[:m] * (1 - t) + nxt[:m] * t)
        if len(nxt) > m:
            self.ring.write(nxt[m:])
        self.engine = new
        if self.pending is not None and self.pending[1] is new:
            self.pending = None  # (another may have been queued meanwhile)

    def schedule(self, build) -> int:
        """Build a new engine starting a little past what's queued and hand
        it to the producer. Returns the mix sample it takes over at."""
        at = self.engine.position + int(SWITCH_AHEAD_SEC * self.sr)
        self.pending = (at, build(at), False)
        return at

    def jump(self, new: LiveEngine, at: int | None = None) -> None:
        """Crossfade into `new` (started anywhere in the mix) when the
        current engine reaches mix sample `at` — by default, right away."""
        self.pending = (self.engine.position if at is None else at, new, True)

    def playing_at(self, latency: int = 0) -> int:
        """The mix sample the card is playing, `latency` frames after it
        took it from the ring."""
        r = max(0, self.ring.r - latency)
        w, mix = next(m for m in reversed(self.marks) if m[0] <= r)
        return mix + (r - w)

    # -- consumer (the audio callback) -------------------------------------------

    def callback(self, outdata, frames, time_info, status) -> None:
        n = self.ring.read_into(outdata)
        if n < frames:
            if self.ring.finished:
                import sounddevice as sd

                raise sd.CallbackStop
            self.underruns += 1


def play(
    engine: LiveEngine,
    *,
    watch: Path | None = None,
    rebuild=None,
    log=print,
) -> int:
    """Play `engine` through the default output; returns frames played.

    `watch` + `rebuild(start) -> LiveEngine | None`: when the file changes,
    a new engine takes over from SWITCH_AHEAD_SEC past what's queued.
    """
    import sounddevice as sd

    player = Player(engine, log=log)
    producer = threading.Thread(target=player.produce, daemon=True)
    producer.start()
    while player.ring.queued() < int(LEAD_SEC * player.sr) and not player.ring.finished:
        time.sleep(0.01)

    watcher = None
    if watch is not None and rebuild is not None:
        watcher = threading.Thread(
            target=_watch, args=(player, Path(watch), rebuild, log), daemon=True
        )
        watcher.start()
    done = threading.Event()
    with sd.OutputStream(samplerate=player.sr, channels=2, dtype="float32",
                         callback=player.callback, finished_callback=done.set):
        try:
            while not done.wait(0.1):
                pass
        except KeyboardInterrupt:
            pass
    player.stop.set()
    if player.underruns:
        log(f"play: {player.underruns} buffer underruns (audio dropped out)")
    return player.ring.r


def _watch(player: Player, path: Path, rebuild, log) -> None:
    mtime = path.stat().st_mtime
    while not player.stop.is_set() and not player.ring.finished:
        time.sleep(0.25)
        try:
            m = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if m == mtime:
            continue
        mtime = m
        try:
            at = player.schedule(rebuild)
        except Exception as e:  # keep playing the old plan
            log(f"play: not reloaded: {e}")
            continue
        log(f"play: reloaded {path.name}; applies from {at / player.sr:.2f}s")


def live_from_plan(plan, audio_root, grids, *, start: int = 0, limited: bool = True,
                   audio_cache: dict | None = None) -> LiveEngine:
    return LiveEngine(prepare(plan, audio_root, grids, audio_cache=audio_cache),
                      limited=limited, start=start)
