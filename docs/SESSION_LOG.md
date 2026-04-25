# dj-segue Session Log

Append-only log of coding sessions. Most recent at the bottom.

This log is the durable bridge between sessions. The `start-session` skill reads it; the `end-session` skill appends to it. Don't edit historical entries.

---

## Session: 2026-04-25 (project seed, pre-coding)

**Milestone:** Pre-M1 (project bootstrapping)
**Duration:** N/A — design conversation, no code written
**Worked on:** Architecture, schema v0.1, milestones, project brief, session skills

### Completed
- Settled architectural decisions: score-style plans, single sample-clock, lock-free callback, executor pluggability, headless-first testing, preprocess-before-play.
- Drafted `docs/schema-v0.1.md` with full plan format spec.
- Drafted `docs/architecture.md` with pinned decisions.
- Drafted `docs/milestones.md` with M1–M7 roadmap.
- Drafted `PROJECT_BRIEF.md` to orient Claude Code at session start.
- Created example plan `examples/hello_mix.plan.jsonc` as the M1 acceptance test.
- Created `start-session` and `end-session` Claude Code skills.

### Tests
- None yet (no code written).

### Schema or interface changes
- Schema v0.1 created (this is the starting version).

### Dependencies added/removed
- None yet. Dependency list is documented in `docs/architecture.md` for M1 implementation.

### Open questions
- None blocking M1.

### Next session should
- Run start-session skill.
- Begin M1 (Hello Mix). Specifically:
  1. Scaffold `src/dj_segue/...` per the layout in architecture.md.
  2. Create `pyproject.toml` with M1 dependencies.
  3. Implement schema parsing/validation (`pydantic`).
  4. Implement `dj-segue inspect`.
  5. Implement BPM/beat preprocessor.
  6. Implement native executor for `play` segments + `deck_volume` automation.
  7. Wire up CLI.
  8. Write M1 acceptance test.
- Generate two short test audio files for the example plan (`tests/audio/sine_120bpm_a.wav` and `tests/audio/sine_120bpm_b.wav`) — sine waves at different frequencies so the deck switch is audibly obvious.

### Notes for future sessions
- The previous project (`dj-gemini`, files reviewed during design) had three failure modes to actively avoid: (1) event-graph trigger model in JSON, (2) parallel clock systems with mid-flight refactors, (3) per-deck schedulers competing with engine-level scheduling. We've chosen a flat timeline-first design specifically to dodge all three. Don't reintroduce them.
- The native executor will eventually port `professional_eq.py` and `ring_buffer.py` from the previous project (those files were solid). Defer that to M5.
- Stems are optional; tracks can be single-source (`path`) or stem-based (`stems`). The native engine treats single-source as a one-stem case named `full`.

---
