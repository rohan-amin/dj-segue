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
- **Segment-overlap validation is deferred** to the executor because computing mix-time durations of `play` segments depends on the resolved tempo behavior. Validator currently does *not* catch overlapping segments on the same deck.
- **Track-position-within-track-duration validation is deferred** to the preprocessor (schema rule #6) — needs audio file metadata.

### Decisions resolved this session
- **v0.1 tempo behavior: play segments play at the track's natural bpm. `mix_tempo` is informational (used for converting beat positions in mix-time automation, e.g. `second` → mix-beats).** No tempo-stretching in v0.1. Implication: M2 crossfades will be limited to same-bpm tracks unless we ship a tempo field first. Decided with the user 2026-04-24.

### Next session should
- Begin Session B (M1 part B). Tempo decision is resolved (above): play at natural bpm, no stretching in v0.1.
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
- **Long-term tempo direction (post-v0.1, agreed with user 2026-04-24):** tempo will eventually be a **full automation lane** — meaning tempo can change over the course of a mix (DJ tempo builds, gradual matches, etc.), not just be set per-segment. The intermediate step (a static `play.target_bpm` field, additive minor schema bump) is a reasonable bridge if M2 needs mismatched-bpm crossfades before the automation lane lands. Either way, plan for the automation-lane endpoint when designing the executor's tempo handling — don't bake in assumptions that tempo is a per-segment constant.

---

## Session: 2026-04-24 (M1 part B — preprocessor, native engine, M1 ships)

**Milestone:** M1 — Hello Mix (DONE)
**Duration:** one focused session, continued from M1 part A in the same conversation
**Worked on:** Analyzer, preprocessor, time math, native WAV+live executor, deferred validation rules, M1 acceptance test, CLI completion

### Completed
- `analyzer/beat.py` — librosa-based BPM + beat-time detection. For pure sine fixtures librosa returns bpm=0 and 0 beats (no onsets to lock to), which is correct behavior — the plan's declared `bpm` is the v0.1 source of truth.
- `analyzer/cache.py` — `<audio>.beats` JSON sidecar keyed by audio mtime + analyzer version. Round-trips via `CacheEntry` dataclass; `is_fresh()` does cheap mtime/version checks.
- `preprocessor/pipeline.py` — `preprocess(plan, audio_root) → PreprocessResult`. Idempotent (skips fresh caches). Returns per-track `TrackAnalysis` with stems → BeatAnalysis and the plan's `declared_bpm`.
- `time_math.py` — single source of truth for position/duration → seconds conversions. `mix_pos_to_seconds` (mix-time, rejects cue refs), `track_pos_to_seconds` (track-time, resolves cues via track registry), `duration_to_seconds`. 4/4 hardcoded for bar↔beat.
- `executor/base.py` — `MixExecutor` abstract with `render`, `render_to_wav`, `play_live`. `RenderResult` dataclass carries float32 stereo samples + sample rate.
- `executor/native/engine.py` — compiles each `play` segment to (mix_start_sample, mix_end_sample, track_start_sample, deck), per-deck stereo buffers, vectorized volume curves via `_lane_to_curve` (linear/step/exponential supported; exponential falls back to linear when an endpoint is 0). Mono → stereo by channel duplication. Single sample rate; mismatched track rates raise NotImplementedError. Transitions and stem-based tracks raise NotImplementedError pointing to M2/M3.
- Live audio path: `play_live` calls `sd.play(buffer, blocking=True)` against the pre-rendered mix. Architecture's lock-free callback rule holds because sounddevice's callback only memcpys from our buffer.
- Deferred validation rules added to `schema/validator.py`: `validate_against_audio(plan, durations)` checks track-position-within-track-duration and same-deck segment overlap. Overlap math uses time_math + a per-deck cursor for implicit `start_at`. Both rules now exposed; old validator unchanged.
- CLI: `dj-segue preprocess` and `dj-segue play [--render-to <wav>] [--audio-root <dir>]` are real. Both run `validate_plan`; `play` also runs `validate_against_audio` after preprocessing. Stub messages removed.
- Test audio fixtures unchanged from Session A (still 17s sines at 440/660 Hz).

### Tests
- 69 passed, 0 failed in ~0.16s. Full suite (40 from Session A + 29 new).
- New tests: `test_time_math.py` (10), `test_preprocessor.py` (3), `test_audio_validator.py` (6), `test_m1_acceptance.py` (10).
- M1 acceptance test renders `examples/hello_mix.plan.jsonc` to WAV and asserts: 32-second duration, stereo float32, no clipping, RMS in beat-windows matches modulated-sine math (full-volume = 0.354, fade-in window = 0.204), output is near-silent immediately after the step cut at beat 32, and FFT-bin energy proves track A's 440 Hz dominates the early window while track B's 660 Hz dominates the late window. WAV file integrity round-trips through soundfile.
- End-to-end manual smoke: `dj-segue play examples/hello_mix.plan.jsonc --render-to /tmp/hello.wav` produces a clean 32s 1411200-sample WAV.

### Schema or interface changes
- None to the schema spec. Pydantic models unchanged from Session A.
- Internal Python interface additions (no public guarantees): `MixExecutor` abstract, `validate_against_audio(plan, durations)`, `time_math` helpers, `PreprocessResult` / `TrackAnalysis` / `BeatAnalysis` dataclasses.

### Dependencies added/removed
- Added: `librosa>=0.10` (analyzer), `sounddevice>=0.4` (live audio).
- librosa pulls in numba/scipy/soxr — heavy install (~30s on first run).

### Open questions
- **ASK USER FIRST AT START OF NEXT SESSION:** Should the native engine hard-fail (NotImplementedError) or warn-and-skip when a plan declares an automation lane that's not yet implemented (`eq`, `stem_volume`, `crossfader`)? Today these lanes parse and pass schema validation (Session A), but the renderer silently ignores them — which was harmless for M1's deck_volume-only plan but will quietly mislead users as M2+ plans start using them. User-flagged as the lead question for the M2 kickoff (2026-04-24).
- No other blockers; M1 is shipped.

### Decisions resolved this session
- **Tempo behavior implemented as decided last session:** play segments play at track's natural bpm; mix_tempo only converts mix-time positions to seconds. Confirmed correct by the acceptance test (track_a at 120 BPM plays its 32 declared track-beats in exactly 16 seconds).
- **Live audio is render-then-stream**, not real-time mixing. Architecture's lock-free callback rule is honored trivially because the callback only memcpys finished samples. Real-time mixing in the callback is M2+ scope.
- **Per-channel/per-deck output buffers are kept transient.** They're allocated per render, summed once, then discarded. For M5 EQ we may need to keep per-deck output for filtering; refactor at that point.

### Next session should
- **Lead with the open question above (unsupported-lane behavior); resolve with the user before any code.**
- Then begin M2 (Crossfades and per-deck automation):
  1. Implement `transition` segment compilation (style: crossfade, cut). The compiler should expand transitions into per-deck `deck_volume` automation curves before reaching the renderer (this keeps the renderer simple).
  2. Implement `crossfader` lane and the conversion from crossfader value to per-deck gains (for two-deck setups; multi-deck crossfader is undefined in the schema).
  3. Implement `step` and `exponential` keyframe interpolation tests properly (linear is the only one exercised in M1).
  4. Beat-locked timing: when a transition's `start_at` lands on a beat, audio crossover happens sample-accurately on that beat. Already true in M1's positioning math; add a regression test.
- Likely needs new fixture audio with actual transitions to test against.

### Notes for future sessions
- `time_math.py` is now the single source of truth for time conversions. Don't recompute beat→seconds inline in the engine, validator, or anywhere else; import the helpers. The validator's old `position_to_mix_beats` (mix-beats, not seconds) predates time_math and is only used by the audio-free keyframe-ordering check; consider unifying in a future cleanup.
- Sample-rate handling is deliberately strict in M1: all tracks must share an SR or the engine raises. Resampling is a real M2/M3 concern (especially when stems and source audio mix). librosa.resample or soxr would be the path.
- The renderer pre-allocates one stereo buffer per active deck plus the final mix buffer. For long mixes (>10 minutes) this is fine for M1 but will balloon as deck count grows; the M2+ design should consider mixing in chunks rather than full-length buffer-per-deck.
- Cache invalidation uses mtime within 1ms. If audio files are touched without changing content, caches regenerate. That's a feature (no false-fresh) but means CI flows that copy audio should preserve mtimes.
- `sounddevice` is imported lazily inside `play_live` so headless environments (no PortAudio device) can still import the engine and render to WAV. Do not move the import to module top-level.
- M1 acceptance test does not assert byte-identical WAVs (no golden file). It asserts on properties (duration, RMS in time windows, FFT bin energies). This is more robust to harmless float/PCM rounding differences across platforms but won't catch subtle phase or stereo-balance regressions. Worth adding a perceptual golden in M2 if the test catches drift.

---
