"""Live playback for the tune page: the whole mix through this machine's
speakers, with edits heard as it plays (R2).

The page sends play (from a mix position), stop, and the loop region; the
server plays through the real-time engine (executor/native/live.py). While
it plays:

  - every edit that validates (`TuneSession.preview`) takes over like a
    `play --watch` reload: an engine for the edited plan starts a little
    past what's queued, with a short crossfade;
  - play from somewhere else (a click on the ruler) jumps there — the same
    crossfade, into an engine started at the new position;
  - with a loop region, an engine for its start is built ahead of time and
    jumped to when playback reaches its end.

The page polls `state()` for the playhead. What it hears is ~LEAD_SEC
behind what's been produced, so an edit or a jump is heard about half a
second after it's made.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from dj_segue.executor.native.live import LEAD_SEC, LiveEngine, Player
from dj_segue.schema.plan import Plan

# The loop's next pass is built this long before playback reaches its end.
LOOP_PREBUILD_SEC = 1.0
# A loop whose end playback passed by up to this much (an edit took the
# slot) still jumps back.
LOOP_LATE_SEC = 2.0


class TuneLive:
    """`build(plan, start_sample)` makes a LiveEngine; `open_stream(player)`
    starts audio output pulling `player.callback` and returns an object with
    `close()` and `latency` (seconds) — sounddevice by default."""

    def __init__(
        self,
        build: Callable[[Plan, int], LiveEngine],
        plan: Plan,
        sample_rate: int,
        open_stream: Callable | None = None,
    ) -> None:
        self.build = build
        self.plan = plan
        self.sr = sample_rate
        self.open_stream = open_stream or _sounddevice_stream
        self.player: Player | None = None
        self.stream = None
        self.loop: tuple[int, int] | None = None  # mix samples [start, end)
        self._lock = threading.RLock()
        self._monitor: threading.Thread | None = None

    @property
    def playing(self) -> bool:
        p = self.player
        return p is not None and not (p.ring.finished and p.ring.r >= p.ring.w)

    def set_plan(self, plan: Plan) -> None:
        """The plan to play from now on (each validated edit)."""
        with self._lock:
            self.plan = plan
            if self.playing:
                self.player.schedule(lambda at: self.build(plan, at))

    def play(self, at: int) -> None:
        """Play from mix sample `at` (jumping there if already playing)."""
        with self._lock:
            at = max(0, at)
            if self.playing:
                self.player.jump(self.build(self.plan, at))
                return
            self.stop()
            player = Player(self.build(self.plan, at), log=lambda *_: None)
            threading.Thread(target=player.produce, daemon=True).start()
            lead = int(LEAD_SEC * self.sr)
            while player.ring.queued() < lead and not player.ring.finished:
                time.sleep(0.005)
            self.player = player
            self.stream = self.open_stream(player)
            self._monitor = threading.Thread(target=self._watch_loop, args=(player,), daemon=True)
            self._monitor.start()

    def stop(self) -> None:
        with self._lock:
            if self.player is not None:
                self.player.stop.set()
            if self.stream is not None:
                self.stream.close()
            self.player, self.stream = None, None

    def set_loop(self, region: tuple[int, int] | None) -> None:
        with self._lock:
            self.loop = region if region and region[1] > region[0] else None
            if self.player is not None and self.player.pending and self.player.pending[2]:
                self.player.pending = None  # a jump back to the old loop

    def state(self) -> dict:
        """{"playing": bool, "position": mix seconds being heard, or None}."""
        with self._lock:
            if not self.playing:
                return {"playing": False, "position": None}
            latency = int(round(getattr(self.stream, "latency", 0.0) * self.sr))
            return {"playing": True, "position": self.player.playing_at(latency) / self.sr}

    # -- the loop ---------------------------------------------------------

    def _due(self, player: Player):
        """(loop, plan) when the loop's next pass should be built now."""
        if self.player is not player or self.loop is None or player.pending is not None:
            return None  # (an edit or a jump is on its way)
        start, end = self.loop
        pos = player.engine.position
        if end - int(LOOP_PREBUILD_SEC * self.sr) <= pos < end + int(LOOP_LATE_SEC * self.sr):
            return self.loop, self.plan
        return None

    def _watch_loop(self, player: Player) -> None:
        while not player.stop.is_set() and not player.ring.finished:
            time.sleep(0.02)
            with self._lock:
                due = self._due(player)
            if due is None:
                continue
            (start, end), plan = due
            new = self.build(plan, start)  # outside the lock: it takes a moment
            with self._lock:
                now = self._due(player)
                if now and now[0] == due[0] and now[1] is plan:  # nothing changed meanwhile
                    player.jump(new, at=max(end, player.engine.position))


def _sounddevice_stream(player: Player):
    import sounddevice as sd

    stream = sd.OutputStream(samplerate=player.sr, channels=2, dtype="float32",
                             callback=player.callback)
    stream.start()
    return stream
