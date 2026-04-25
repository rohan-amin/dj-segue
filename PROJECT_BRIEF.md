# PROJECT_BRIEF.md — read this first

You are working on **dj-segue**, an AI-driven DJ system focused on wordplay transitions between songs. This brief is your starting point. It points you to the source-of-truth docs and tells you how to operate in this repo.

---

## Read these in order, before doing anything

1. `docs/architecture.md` — settled architectural decisions. Don't relitigate; if you want to, propose it as an "Open question" first.
2. `docs/schema-v0.1.md` — the plan schema. The schema is the API between layers; respect it.
3. `docs/milestones.md` — what we're building, in what order. Stay in the milestone lane unless instructed otherwise.
4. `docs/SESSION_LOG.md` — what previous sessions did and where they left off.

If any of those files don't exist yet, the project is brand new. Start by reading the other three and the example plan in `examples/hello_mix.plan.jsonc`.

---

## How to start a coding session

**Always run the start-session skill first.** It's at `.claude/skills/start-session/SKILL.md`. It will:

1. Re-read the architecture, schema, milestones, and session log.
2. Confirm the current milestone and the planned task for this session.
3. Surface any blockers or open questions from the prior session.
4. Show you what tests are passing/failing right now.

Don't start coding until the start-session checklist is complete. It takes 60 seconds and saves hours of misalignment.

---

## How to end a coding session

**Always run the end-session skill at the end of every session**, before the user wraps up. It's at `.claude/skills/end-session/SKILL.md`. It will:

1. Update `docs/SESSION_LOG.md` with what was done, what's next, and any open questions.
2. Note any architectural decisions that came up — propose updates to `architecture.md` rather than silently changing behavior.
3. Surface dependency changes, schema changes, or interface changes for the user to review.
4. Make sure tests pass (or document why they don't).

---

## Operating principles for this repo

### Stay in the lane

This project has a clear architecture. New features go through the existing layers (planner → plan → preprocessor → executor). If you're tempted to add a side-channel or a back-door, stop and ask first.

### The schema is the contract

Don't add features that bypass the plan schema. If a feature needs schema support, propose a schema change (which will bump `schema_version`). Don't add fields the schema doesn't declare.

### Headless before live

Every feature must be testable offline (rendering to WAV, asserting on output) before it gets a live-audio code path. If you can't test it without a sound card, it's not done.

### The audio callback is sacred

Lock-free, no allocation, no I/O, no logging. If you find yourself wanting to put any of those in the callback, the design is wrong somewhere upstream. Surface it.

### One source of truth for time

The audio output stream's sample counter. Mix-time = `samples / sample_rate`. Don't introduce parallel clocks.

### Add tests as you go

Every milestone has an acceptance test in `docs/milestones.md`. Implement that test alongside the feature. Add unit tests for non-trivial logic (schema validation, plan compilation, automation interpolation).

### Stop and ask before making cross-cutting changes

If you find yourself touching `executor/native/engine.py`, `executor/base.py`, `schema/plan.py`, and `preprocessor/pipeline.py` in a single change, the change is probably too big. Stop, propose the design, then implement.

### Do not delete or rewrite docs without proposing it first

If a doc seems wrong, surface that to the user with a proposed edit. Don't quietly rewrite settled architecture.

---

## Current milestone

**M1 — Hello Mix.** See `docs/milestones.md` for full scope. The acceptance test is `examples/hello_mix.plan.jsonc` playing correctly in both live and offline modes.

You'll need to:

1. Scaffold the repo per the layout in `docs/architecture.md` (`src/dj_segue/...`).
2. Set up `pyproject.toml` with the pinned dependencies listed in the architecture doc.
3. Implement schema parsing/validation with `pydantic`.
4. Implement the inspector (`dj-segue inspect <plan>`).
5. Implement the preprocessor for BPM/beat detection.
6. Implement the native executor for the `play` segment type and `deck_volume` automation lane.
7. Wire up the CLI (`dj-segue play <plan>`).
8. Write the M1 acceptance test.

Do this in small commits. Run tests as you go. Update `SESSION_LOG.md` at the end.

---

## Anything else

If you're confused about scope or design, **stop and ask the user.** Don't guess. The cost of a clarifying question is one turn; the cost of going down the wrong path is hours of rework.
