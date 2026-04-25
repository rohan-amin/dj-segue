# dj-segue

AI-driven DJ system for **wordplay transitions** between songs.

Most DJ software does the easy parts (BPM matching, harmonic mixing, beat-aligned crossfades) and leaves the hard part — choosing *what* to play and *how* to transition — to the human. dj-segue tries the hard part. Specifically: given two songs, find a transition point where a word in one song sets up or echoes a word in the other, then execute the transition cleanly. The bottom-of-the-bottom from "Started From The Bottom" landing on the bottom-of-the-bottom in "Middle Child" is the canonical example.

This is an early-stage project. See `docs/milestones.md` for the roadmap.

---

## Status

Pre-v0.1. Currently building Milestone 1 (the basic audio engine spine).

---

## Quick start

> Coming once M1 is implemented. Approximately:
>
> ```bash
> pip install -e .
> dj-segue inspect  examples/hello_mix.plan.jsonc
> dj-segue preprocess examples/hello_mix.plan.jsonc
> dj-segue play examples/hello_mix.plan.jsonc
> dj-segue play examples/hello_mix.plan.jsonc --render-to /tmp/out.wav
> ```

---

## How it works (high level)

```
"english request"  →  [planner]  →  plan.jsonc  →  [executor]  →  audio output
                       LLM + lyrics    score format     native engine
                       + audio analysis                 or Mixxx fallback
```

A **plan** is a score-style JSONC document describing what each deck does over the course of a mix. It's hand-editable for testing and AI-generated for real use. The plan is engine-agnostic — the same plan can run on the native Python audio engine (default) or via a Mixxx bridge (for cross-validation).

See `docs/architecture.md` for the design rationale and `docs/schema-v0.1.md` for the plan format.

---

## Repository layout

```
docs/                 — design docs (architecture, schema, milestones, session log)
examples/             — example plans, including the M1 acceptance test
src/dj_segue/         — the implementation (created during M1)
tests/                — unit + integration tests, golden WAVs
.claude/skills/       — Claude Code session-management skills
PROJECT_BRIEF.md      — orientation for Claude Code at start of any coding session
```

---

## For contributors / Claude Code sessions

Read `PROJECT_BRIEF.md` before doing anything. It points you at the docs you need and explains the operating principles for the repo.

Sessions begin and end with the skills in `.claude/skills/`. They keep the docs honest and prevent drift between sessions.

---

## License

TBD. Likely MIT or Apache-2.
