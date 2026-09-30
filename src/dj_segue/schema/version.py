"""Schema version registry.

0.1 — initial release.
0.2 — additive: `track.bpm` optional (omitted → detected by the preprocessor);
      `play.target_bpm` (time-stretch a segment to a tempo). 0.1 plans remain
      valid; the 0.2 features are rejected in plans that declare 0.1.
"""

from __future__ import annotations

CURRENT_SCHEMA_VERSION = "0.2"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"0.1", "0.2"})
