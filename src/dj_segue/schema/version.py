"""Schema version registry.

0.1 — initial release.
0.2 — additive: `track.bpm` optional (omitted → detected by the preprocessor);
      `play.target_bpm` (time-stretch a segment to a tempo). 0.1 plans remain
      valid; the 0.2 features are rejected in plans that declare 0.1.
0.3 — additive: `loop` segments (with a tightening `schedule`); segment `id`s
      and `{"after": id}` mix positions; `target_bpm: "mix"`. Rejected in
      plans that declare 0.1 or 0.2.
0.4 — additive: crossfade `curve`, and per-side `out` / `in` overrides
      (offset, duration, curve). Rejected in plans that declare 0.1–0.3.
0.5 — additive: drawn curves on a crossfade side (`curve: {points, smooth}`).
      Rejected in plans that declare 0.1–0.4.
0.6 — additive: `echo_out` transitions (`echo: {every, feedback}`; optional
      `in`). Rejected in plans that declare 0.1–0.5.
0.7 — additive: `filter` automation lane (DJ filter knob, -1..+1) and
      `echo.highpass` (a high-pass sweep on the echo tail). Rejected in plans
      that declare 0.1–0.6.
"""

from __future__ import annotations

CURRENT_SCHEMA_VERSION = "0.7"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7"})
