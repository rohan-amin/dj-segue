# dj-segue

AI-driven DJ system for **wordplay transitions** between songs.

Most DJ software does the easy parts (BPM matching, harmonic mixing, beat-aligned crossfades) and leaves the hard part — choosing *what* to play and *how* to transition — to the human. dj-segue tries the hard part. Specifically: given two songs, find a transition point where a word in one song sets up or echoes a word in the other, then execute the transition cleanly. The bottom-of-the-bottom from "Started From The Bottom" landing on the bottom-of-the-bottom in "Middle Child" is the canonical example.

This is an early-stage project. See `docs/milestones.md` for the roadmap.

---

## Status

Schema v0.5. General mixing works: plans with crossfades, cuts, volume automation, beatmatched tempo changes and tightening loops render sample-accurately (milestones M1–M2.6, M4). Next: stems (M3).

---

## Quick start

```bash
brew install rubberband          # time-stretching (Debian/Ubuntu: apt install rubberband-cli)
pip install -e ".[dev,downbeats]"   # `downbeats` is optional (PyTorch)

dj-segue inspect    examples/crossfade_mix.plan.jsonc   # plan summary + validation
dj-segue preprocess examples/crossfade_mix.plan.jsonc   # analyze tracks (tempo, beat grid)
dj-segue play       examples/crossfade_mix.plan.jsonc   # play through the speakers
dj-segue play       examples/crossfade_mix.plan.jsonc --render-to /tmp/out.wav
dj-segue scrub      audio/some_track.mp3                # find beat numbers by ear
dj-segue tune       examples/fancy_likeem.plan.jsonc    # adjust transitions by ear
```

To mix your own music, put audio files in `audio/` (git-ignored) and point a plan's
track `path`s at them. See `examples/real_mix.plan.jsonc`, `examples/starships_omt.plan.jsonc`
and `docs/schema-v0.5.md`.

`dj-segue scrub <file>` opens the track in your browser (a local page; nothing is
uploaded) to find beat numbers by ear. It shows the whole song as a map — waveform,
energy and bass strips, likely section starts, and amber **breaks** (short bass
dropouts before a phrase, natural entry points) — plus a zoomed 8-bar view with every
beat numbered. The big readout shows the beat you're hearing on the same grid plans
use (beat 0 = first downbeat, bar N = beat 4N). Buttons (and keys) move by beat, bar,
8 bars or section, loop ½–16 beats, toggle a click on the grid, and mark beats to copy
into a plan as `{ "beat": N }`.

`dj-segue tune <plan>` opens a plan's crossfades in the browser: both decks'
waveforms over the blend with their volume curves. Drag either side's fade window
(snaps to beats; Alt for ¼ beats), pick a curve per side or draw your own (**D**
for the pencil; drag, add or remove points), and play or loop the blend. What you hear is rendered by the engine itself, so it's exactly what `play` produces.
**Save** writes only the transition's changed keys into the plan, keeping your
comments and formatting. (Moving where a track starts and jump/loop points are
coming; see M4.5 in `docs/milestones.md`.)

Time-stretched audio is cached in the project's `.cache/stretch` folder (git-ignored;
keyed by the exact input samples, tempo ratio and Rubber Band version), so re-rendering
or re-opening a plan skips the slow part. Delete that folder any time to free space;
`DJ_SEGUE_STRETCH_CACHE=off` disables it, or set it to another folder.

---

## How it works (high level)

```
"english request"  →  [planner]  →  plan.jsonc  →  [executor]  →  audio output
                       LLM + lyrics    score format     native engine
                       + audio analysis                 or Mixxx fallback
```

A **plan** is a score-style JSONC document describing what each deck does over the course of a mix. It's hand-editable for testing and AI-generated for real use. The plan is engine-agnostic — the same plan can run on the native Python audio engine (default) or via a Mixxx bridge (for cross-validation).

See `docs/architecture.md` for the design rationale and `docs/schema-v0.5.md` for the plan format.

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
