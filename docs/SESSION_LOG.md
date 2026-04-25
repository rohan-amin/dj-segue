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

## Session: 2026-04-24 (M1 part A — schema + inspect path)

**Milestone:** M1 — Hello Mix (split into A: schema/inspect, B: preprocessor/executor/play)
**Duration:** one focused session
**Worked on:** Package scaffold, JSONC + pydantic schema, cross-field validator, inspector, CLI, test fixtures, schema-layer test suite

### Completed
- Scaffolded `src/dj_segue/{schema,inspect,cli}/` per architecture.md (other module dirs deferred to Session B to avoid empty stubs).
- `pyproject.toml` (hatchling, Python 3.11+, console script `dj-segue = dj_segue.cli.main:app`). M1 deps installed: pydantic v2, typer, soundfile, numpy. Deferred to Session B: librosa, sounddevice, pyrubberband, pedalboard.
- `schema/jsonc.py` — `//` and `/* */` comment stripper that respects string literals and preserves newlines (so JSON parse errors keep accurate line numbers). Trailing commas not supported.
- `schema/plan.py` — full v0.1 pydantic models. Position discriminated union (BeatPos/BarPos/SecondPos/CuePos) with bare-string-as-cue shorthand. Duration types. Track with path↔stems normalization (path becomes `{"full": path}`). Decks coerced from string keys to int 1–4. All three segment types and all four automation lanes. EQ uses `value_db` keyframes; others use `value`. Schema-version pin against `version.py`. All models `extra="forbid"`.
- `schema/validator.py` — six cross-field rules: track refs in timeline, deck refs (timeline + automation, crossfader skipped), cue refs (only valid in track-time, rejected in mix-time positions), stem refs (stem_volume lane requires stem on every track that plays on that deck), keyframe time-ordering (mix-tempo-aware, handles mixed beat/bar/second units), vocal_handoff requires `vocals` stem on tracks of both decks. Returns all issues as a list rather than failing fast.
- `inspect/pretty.py` — readable summary with mix-time resolution; tracks/decks/timeline/automation sections; embedded validation result.
- `cli/main.py` — typer app with `inspect` (working), `preprocess`/`play` (stubbed, exit 2 with message).
- `tests/audio/generate.py` + checked-in WAVs `sine_120bpm_a.wav` (440Hz) and `sine_120bpm_b.wav` (660Hz, perfect-fifth above), 17s mono 44.1kHz PCM_16 with 5ms cosine ramps to avoid clicks.
- `.gitignore` for venvs, caches, `.beats`/`.cue` sidecars, stem `.npy` outputs.
- 40 unit tests across test_jsonc, test_schema, test_validator, test_inspect.

### Tests
- 40 passed, 0 failed in ~0.07s.
- End-to-end smoke: `dj-segue inspect examples/hello_mix.plan.jsonc` renders the plan and reports validation `ok` with exit 0.

### Schema or interface changes
- None. Schema v0.1 implemented as written; no doc edits.

### Dependencies added/removed
- Added (in pyproject.toml): pydantic>=2,<3, typer>=0.12, numpy>=1.26, soundfile>=0.12, pytest>=8 (dev).
- Build backend: hatchling.

### Open questions
- **v0.1 tempo behavior is underspecified.** Schema-doc says `mix_tempo` defaults to first track's bpm but doesn't say whether `play` segments tempo-match to mix_tempo or play at original bpm. The example plan dodges this (both tracks at 120, mix_tempo 120). Session B's executor will need to pick one; my read is "play at original bpm" for v0.1 (key/tempo shifting deferred to v0.3 per schema doc). Surface to user before implementing.
- **Segment-overlap validation is deferred** to the executor because computing mix-time durations of `play` segments depends on the tempo decision above. Validator currently does *not* catch overlapping segments on the same deck.
- **Track-position-within-track-duration validation is deferred** to the preprocessor (schema rule #6) — needs audio file metadata.

### Next session should
- Resolve the tempo question with the user, then begin Session B:
  1. Implement preprocessor: BPM/beat-grid via librosa, write `<track>.beats` JSON sidecar, idempotent (skip if cache fresh).
  2. Implement native executor for `play` segments + `deck_volume` automation. Headless (WAV render) first, then live audio via sounddevice.
  3. Wire up `dj-segue preprocess` and `dj-segue play [--render-to]`.
  4. Add the deferred validation rules (overlap, track-position-within-duration) once audio metadata is available.
  5. M1 acceptance test: render `examples/hello_mix.plan.jsonc` to WAV and assert on sample counts + RMS in expected windows.

### Notes for future sessions
- Schema discriminated unions: I used `Field(discriminator="type")` for segments and `Field(discriminator="lane")` for automation lanes (both have a literal tag field). Position is a smart union without a tag field — pydantic resolves it by trying variants with `extra="forbid"`. Worked cleanly; keep this pattern.
- The `from` field on PlaySegment is aliased (`from_` in Python because `from` is a keyword). When dumping with `model_dump()` the field is `from_`; use `model_dump(by_alias=True)` if you need round-trip JSON.
- Track normalization happens in a `model_validator(mode="before")` on Track — input dict gets `path` rewritten to `stems={"full": path}`. Post-validation, `track.stems` is always a dict and `track.path` doesn't exist on the model. Tests rely on this.
- `inspect/` package shadows the stdlib `inspect` name. Inside this package, use absolute imports (`import inspect as stdlib_inspect`) if you ever need the stdlib module. Hasn't bitten anything yet.
- Test audio fixtures are checked in (~3 MB total). If they ever grow, move to git-lfs or a download script — but for sine waves of this size, in-tree is fine.

---
