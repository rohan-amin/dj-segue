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

## Session: 2026-09-29 → 2026-09-30 (M2, M2.5, M2.6 — general mixing works)

**Milestone:** M2 — Crossfades; M2.5 — Tempo matching (new); M2.6 — Grid robustness (new). All DONE.
**Duration:** one long session (~40 turns)
**Worked on:** Re-scoped the roadmap to general mixing first (M6 planner and M7 Mixxx deferred); crossfades, tempo detection and time-stretch beatmatching, downbeats. First real-music mix (two user MP3s) renders beat-locked and sounded good to the user.

### Roadmap changes (decided with user 2026-09-29)
- Pause M6 (wordplay planner) and M7 (Mixxx executor); get general mixing working first.
- Added M2.5 (tempo matching) and M2.6 (grid robustness, after comparing with Mixxx's analyzer). See docs/milestones.md.
- Unsupported lanes/segments **hard-fail** with NotImplementedError naming the milestone (resolves last session's open question).
- Clipping: stretched/summed peaks exceed 0 dBFS (real mix peaks ~1.86). User chose a **master limiter in M5**; no interim gain cut.

### Completed
- Adopted the beat-grid anchor work (TrackGrid) found uncommitted at session start (origin unlogged), then reworked it (below).
- **M2:** `compiler.py` (engine-agnostic): gain `Envelope`/`Ramp`s from deck_volume/crossfader lanes and crossfade/cut transitions; `timeline_spans` (single place computing segment mix-time + playback rate). `executor/native/curves.py` renders envelopes sample-accurately. Equal-power crossfades (sin/cos) and crossfader law; exponential interpolation = linear-in-dB with -60 dB floor. `inspect` shows transition expansion. `examples/crossfade_mix.plan.jsonc` (3 tracks, 2 crossfades).
- **Validator fix:** M1 counted transitions as deck occupancy, so every real crossfade failed overlap validation. Transitions are now gain-only; new rules: cut needs duration 0, crossfade duration > 0, from≠to deck, no overlapping transitions per deck, per-deck out/in alternation, one crossfader lane max.
- **M2.5:** tempo detection by fitting a fixed grid to onset energy (comb search → weighted least squares; onset-latency compensated). Accuracy on synthetic clicks: ±0.001 BPM, phase ±0.2 ms. librosa's estimate was off (Starships 126.05 vs true 125.00). `track.bpm` optional (auto-detect; a number overrides). `play.target_bpm` time-stretches via the `rubberband` CLI (R3/--fine; onsets within 0.25 ms). `dj-segue preprocess` prints per-track tempo/anchor. `examples/real_mix.plan.jsonc` (needs local audio/).
- **M2.6:** kick-band (<200 Hz) decides beat vs off-beat (loud off-beat hats pulled grid half a beat); BPM rounding to whole/½/⅓/¼ when drift < 10 ms (after Mixxx); downbeats via beat_this (optional) — beat 0 = first downbeat. Verified: trimming 1–3 beats off either real track moves beat 0 correctly (8/8); beat_this agrees with our beat phase (0% half-beat off).
- Real mix render (One More Time 122.88 + Starships 125.00 → 124, 8-bar crossfade): every 16-beat window within ±9 ms of the mix grid. User listened: sounds good.
- Fixed two circular imports (one pre-existing: importing `time_math` first crashed). Validator now imports the `time_math` module, not names; shared constant in `constants.py`.
- `/audio/` (repo root, git-ignored) for local music; README quick start updated.

### Tests
- 175 passed, 0 failed (~25 s; was 5 s — beat_this runs on each analyzed test track).
- New: test_compiler, test_engine_m2, test_m2_acceptance, test_beat_grid (rewritten), test_m25_tempo, test_m26_grid; transition cases in test_validator / test_audio_validator.
- Stretch tests skip without the `rubberband` CLI; the downbeat pipeline test skips without beat_this.
- hello_mix render verified byte-identical to M1 through all changes.

### Schema or interface changes
- **Schema bumped to v0.2** (additive; 0.1 plans still valid): `track.bpm` optional, `play.target_bpm`. Plans declaring 0.1 must give `bpm` and can't use `target_bpm`. New doc `docs/schema-v0.2.md`; v0.1 doc kept (with a track-time clarification added this session).
- **Semantics:** track beat N counts from the track's first downbeat on the fitted grid (not sample 0). `second` positions stay absolute.
- Internal: `validate_plan(plan, grids=None)`, `resolved_mix_tempo(plan, grids)` may return None before preprocessing; `BeatAnalysis.beat_anchor(bpm)` / `grid_anchor(bpm)`; analysis cache schema v4; `PreprocessResult.grids()` raises `TempoNotDetectedError`.

### Dependencies added/removed
- System: `rubberband` CLI (brew install rubberband) — required for `target_bpm`.
- Optional extra `downbeats`: `beat_this` (pulls in torch/torchaudio; model checkpoint auto-downloads once to ~/.cache/torch/hub).
- pyrubberband was tried and **removed** (see notes).

### Open questions
- none blocking. Not built: a metronome/"click on the grid" render option would make checking grids by ear easy — worth considering.

### Next session should
- Start **M3 (stems)**: demucs separation in the preprocessor (torch already installed via beat_this), stem-aware track loading, `stem_volume` lane, `vocal_handoff` transition.
- M5 must include the master limiter (decided this session).

### Notes for future sessions
- **Don't use pyrubberband.** It mutates the `rbargs` dict passed in (`setdefault('--tempo', rate)`), so a shared dict leaked the first segment's rate into every later stretch — a real mix came out at 126 BPM instead of 124. It also writes 16-bit temp files. We call the CLI directly with float temp files.
- The `rubberband` CLI clamps output to ±1.0 even for float WAVs; `stretch.py` scales input to -6 dBFS and back.
- Verify alignment by measuring, not by a single fit: `fit_grid` on a short slice can latch onto a wrong tempo; phase-tracking in 16-beat windows against the mix grid is the reliable check (it's what caught the pyrubberband bug).
- beat_this misreads synthetic accent patterns (hears the accent as a pickup). Test our downbeat *logic* and *consistency* synthetically; judge correctness on real music.
- Import cycle hazard: `time_math` → `schema.plan` → `schema/__init__` → validator. Anything the validator needs from time_math must be looked up via the module at call time; keep librosa out of schema imports (`inspect` must stay light).
- Tracks mastered hot decode above 1.0 and stretching raises peaks 2–4 dB — expect clipping in PCM_16 renders until the M5 limiter.
- User preference: detect values from audio by default; explicit plan values are overrides.

---

## Session: 2026-09-30 (M4 loops, schema v0.3, web scrub)

**Milestone:** M4 — Loops (pulled ahead of M3). Built and verified on real music.
**Duration:** approximately 45 turns
**Worked on:** Tightening loops; schema v0.3; ported a mix from the user's old DJ-as-code prototype; a browser-based beat finder with a song-structure map.

### Roadmap changes (decided with user 2026-09-30)
- M4 (loops) done before M3 (stems): loops don't need stems.
- Loops use a per-segment `schedule`, not a `loop_length` automation lane (a lane would make segment timing depend on automation).
- Direction: make plans easy for non-DJs — eventually "describe the mix in English, get accurate JSON". Hand-editable schema stays the source of truth. The user wants no full GUI for now — just what makes it easy to listen and find beats (→ web scrub).

### Completed
- **Schema v0.3** (additive): `loop` segment with `schedule` [(length, repetitions)…]; optional segment `id`; `{"after": id, "offset"?}` mix positions (resolve at compile time to that segment's end; timeline refs must be earlier segments); `target_bpm: "mix"`; `equal_power` lane interpolation. Old 0.1/0.2 plans valid; 0.3 features rejected in them. Doc: `docs/schema-v0.3.md`.
- **Compiler:** `resolve_timeline` → `Timeline(spans, anchors)`; loop spans carry per-rep `LoopRep`s (each rep's start computed, not accumulated). Anchors threaded into transitions and automation lanes.
- **Engine:** loops render sample-accurately; stretched once per loop region (shorter reps use a prefix); 3 ms linear seam crossfade that *ends* on each boundary, fed from a pre-roll before the loop start (the loop's first transient is untouched).
- **Validator:** ids unique, `after` refs, loop lengths positive, loop region within track, loops in overlap/stem/vocal checks. Inspector shows loops, ids, `after`, `@ mix bpm`.
- **Mixes** (from the old prototype's plan; ideas taken: named segments/after, bpm-match-as-default): `examples/starships_omt.plan.jsonc` (equal-power 8-beat crossfade) and `examples/starships_omt_slowin.plan.jsonc` (OMT fades in over 10 beats vs Starships out over 8 — user: "perfect"). Final positions picked by ear: Starships 100→loop at 108 (4×3, 2×4); OMT enters at its beat 184.
- **`analyzer/structure.py`:** per-bar energy/bass (dB), breaks (1–4 bar bass dropouts ≥ 6 dB below both neighbours), section boundaries (Foote novelty on 4-bar lines, ≥ 8-bar sections, + bass steps ≥ 6 dB, + break ends). No section *names* — rule-based intro/build/drop labels were unreliable on the real tracks.
- **`dj-segue scrub <file>`** is now a local web page (stdlib HTTP server on 127.0.0.1 + `scrub/page.html`, vanilla JS, Web Audio): whole-song map (waveform, energy/bass strips, boundaries, breaks), 8-bar zoom with numbered beats, big beat/bar readout with section/break context, buttons + keys for beat/bar/8-bar/section seeks, go-to-beat, native seamless loops ½–16 beats, click track (second synced buffer), marks (copy as `{ "beat": N }`, also printed in terminal). Paused seeks move silently (user rejected a one-beat preview). The terminal scrub was retired.

### Tests
- 204 passed, 0 failed (~24 s).
- New: `tests/test_m4_loops.py` (schema gating, validation, loop timing, M4 acceptance via a "timecode" track — every rep sample-exact — seam click check, stretched loop on the mix grid, equal_power lanes); `tests/test_scrub.py` (breaks, structure on synthetic audio, server routes/marks).
- The page's JS isn't unit-tested; it was checked in Chrome (muted) — play, seek, loop, marks, readout — and with `node --check`.
- `real_mix` render verified byte-identical to last session's.

### Schema or interface changes
- **Schema bumped to v0.3** (additive), see above.
- Internal: `mix_pos_to_seconds(pos, tempo, anchors=None)`; `resolve_timeline`; `segment_rate(seg, grid, mix_tempo)`; `transition_envelopes` / `deck_volume_envelopes` / `crossfader_envelope` take `anchors`; `Span.kind` includes `"loop"` with `reps`; `position_to_mix_beats(pos, tempo, anchors)`.

### Dependencies added/removed
- none (web scrub uses the stdlib server; the page has no external libraries).

### Open questions
- Transition curve: the `transition` segment has no curve option; custom shapes need `deck_volume` lanes (now incl. `equal_power`). Add `"curve"` to transitions? (offered, not decided)
- "Easy mode" roadmap not yet written into milestones. Proposed order: (1) structure analysis [done in scrub form], (2) transition *recipes* compiled to v0.3 JSON (loop-out, long blend, cut on drop, …), (3) English planner + English edits, (4) later a fuller studio. User to confirm before it goes into `docs/milestones.md`.

### Next session should
- Ask the user which direction: the "easy mode" roadmap (recipes → English planner) or M3 stems. Write the chosen plan into milestones first.
- M5 must still include the master limiter (renders clip; peaks hit 1.0 in PCM_16).

### Notes for future sessions
- Beat numbers from the old prototype don't transfer reliably (its OMT 592 was near the track's end on our grid; 184 was right). Always check positions with `dj-segue scrub`.
- A 4-beat circular-phase check misreads syncopated tracks (Starships alternates ±half-beat readings every 4 beats in the *source*). Compare render vs source, or use 16-beat windows.
- Section boundaries are reliable; labels aren't. Breaks (short bass dropouts) are the best entry-point markers — OMT's 184–192 break is where the user's ear landed.
- Browser checks: audio stays suspended until a real click/key (script calls don't count), and background tabs throttle timers to ~1 s — don't mistake that for a timing bug.
- A structure `energy` strip needs a tighter dB range (12) than bass (30) to be readable.

---

## Session: 2026-09-30 (transition curves, schema v0.4/v0.5, Fancy mix, tune page T1 + T3)

**Milestone:** M4.5 — Tune (new; T1 and T3 done). Also schema v0.4 and v0.5, engine and analyzer fixes.
**Duration:** approximately 60 turns
**Worked on:** Shapeable crossfades (presets, per-side windows, drawn curves); a new real-music mix (Fancy → Like 'em All) that exposed a tempo-detection bug and a jump click; a browser page to tune transitions by ear; render speed.

### Roadmap changes (decided with user 2026-09-30)
- New **M4.5 "Tune"** (docs/milestones.md): a local page to adjust transitions, jump points and loop points by ear. Save edits the plan **in place** (only changed keys; comments kept). Curves can be **drawn freehand**. Phases T1–T6; T1 and T3 done.
- User: "eventually we need to move to real-time rendering". Proposed (not yet decided): a short real-time **spike** before T2/T4/T5 (Rubber Band library streaming from Python, block engine + ring buffer, live == offline output). See *Open questions*.
- The "easy mode vs M3" question from last session wasn't answered; the user steered to tuning tools instead.

### Completed
- **Schema v0.4:** crossfade `curve` (equal_power | linear | exponential) and per-side `out` / `in` overrides (`offset`, `duration`, `curve`). `time_math.transition_windows` is the single place a transition becomes two fade windows (used by compiler, validator, anchors, tune). A transition's `after` anchor = its later side's end. `starships_omt_slowin` ported to one transition (render byte-identical to the old lanes).
- **Default mix tempo** = the first track *on the timeline* (was: first declared in `tracks`; doc and code disagreed). `tempo_track()`; Starships examples dropped their hard-coded 125.
- **Jump seams:** back-to-back segments on a deck whose audio jumps now get the loop seam (3 ms crossfade ending on the boundary, pre-roll before the new `from`), like a quantized hot-cue jump. Plain continuations untouched. Found via an audible click in the Fancy jump.
- **Tempo-level fix:** librosa's coarse tempo locked onto a triplet pulse on Drake's "Fancy" (117.45 for a real 87.55; confirmed by beat_this, bar spacing and online listings). beat_this's beat tempo (when steady) now picks librosa × {½, ⅔, ¾, 1, 4⁄3, 3⁄2, 2}. Other tracks unchanged. Analysis cache id → segue-grid-3.
- **Mixes:** `examples/fancy_jump` (Fancy, 24-bar jump 112 → 208) and `examples/fancy_likeem` (that, into Like 'em All over 32 beats at Fancy +5% = 91.93 BPM; fade-in curve drawn by the user in tune). User: "really good".
- **Engine split:** `render` = `render_decks` → `deck_gains` → `DeckRender.mix` (byte-identical).
- **Stretch cache + parallel stretching:** results cached by hash of input samples, rate, SR, rubberband version (`<project>/.cache/stretch`, git-ignored; `DJ_SEGUE_STRETCH_CACHE` overrides, "off" disables). Segments stretch in parallel threads. Fancy mix: 25 s → 20 s cold, 0.8 s warm; output byte-identical.
- **`dj-segue tune <plan>` (M4.5 T1):** lists the plan's crossfades; both decks' waveforms + gain curves; drag fade windows (beat snap, Alt ¼); preset curve per side; play/loop; Save in place via `schema/jsonc_edit.py`. Every edit goes through the real validator/compiler/engine, so the page plays exactly `render`'s samples (tested byte-identical). Edits that would move audio are refused (T2). Page opens at once with render progress.
- **Schema v0.5 + T3 (drawn curves):** a side's `curve` may be `{points: [[t, gain], …], smooth}` (absolute gains, out 1→0 / in 0→1, points ≥ 2 ms apart, smooth = PCHIP). Page: pencil (D; Shift = straight line), stroke replaces the curve only where drawn, simplified with RDP at 2.5 px; drag points, double-click add/remove, smooth toggle. Saving bumps the plan's `schema_version`.

### Tests
- 261 passed, 0 failed (~45 s).
- New: test_v04_transitions, test_v05_curves, test_jsonc_edit, test_tune, test_stretch_cache; additions to test_m4_loops (jump seam), test_m26_grid (tempo level), test_validator (default tempo track).
- The tune page's JS isn't unit-tested; checked in Chrome (drag, curves, draw, point edit, save, refusal, progress) and with `node --check`.

### Schema or interface changes
- **Schema v0.4** and **v0.5** (both additive). Docs: `docs/schema-v0.4.md`, `docs/schema-v0.5.md`.
- Internal: `transition_windows`/`FadeWindow`; `compiler.Points` ramp shape; `NativeEngine.render_decks(…, on_progress)` / `deck_gains` / `DeckRender`; `tempo_track()`; `analyzer.downbeat.detect_beats` / `beat_tempo` (replaced `detect_downbeats`); `beat.pick_tempo_level`.

### Dependencies added/removed
- none (scipy, already present via librosa, now used directly for PCHIP).

### Open questions
- **Real-time rendering** (user wants it eventually). Proposed: spike first — Rubber Band library streaming (quality vs R3 `--fine`, run-to-run determinism, ratio changes mid-stream), Python block engine filling a ring buffer, live == offline byte-identical; tempo map in `time_math`. Then decide whether T2/T4/T5 build on it. Recorded in `docs/architecture.md` → Open questions (approved).
- Optional: chunked parallel stretching for faster first renders (needs a listening test at chunk joins).
- Small: clearer transition labels in the tune dropdown (track names / beat / id).

### Next session should
- Decide: real-time spike vs. continuing M4.5 (T2 move segments, T4 jump points, T5 loop points, T6 A/B).
- M5 still owes the master limiter (renders clip).

### Notes for future sessions
- The tune server reads `page.html` once at startup — restart it after editing the page (cost a debugging round this session).
- Pointer-capture drags can't be scripted with the click tool beyond a straight line; dispatch `PointerEvent`s from JS for freehand strokes, but do real clicks for playback (audio needs a user gesture).
- Simplify drawn strokes in **pixels**, not normalized units (sub-pixel tolerance kept every wobble). When merging a simplified stroke, take the covered time range from the **raw** stroke.
- A beat grid that looks "right" in `scrub` can still be the wrong tempo (Fancy at 116.7 felt fine because the user picked positions on the same wrong grid). Cross-check unusual tempos with beat_this / online listings.
- Jump points picked on a wrong grid convert by seconds, then round to real downbeats (Fancy 149 → 277 became 112 → 208).
- Stretch time depends on content, not just length (Like 'em All: ~0.077 s per second of audio vs Fancy's ~0.036).

---

## Session: 2026-09-30 (late: drawn-curve render, Bottoms Up plan, echo out planned)

**Milestone:** M4.5 — Tune (testing with a new mix; no code changes)
**Duration:** approximately 10 turns
**Worked on:** Rendering the drawn-curve Fancy → Like 'em All mix; setting up a three-song plan to shape Like 'em All → Bottoms Up in `tune`; echo out added to M5.

### Completed
- Rendered `examples/fancy_likeem.plan.jsonc` (with the drawn fade-in) to `audio/fancy_likeem_drawn.wav` (6:18.76; 0.75 s, stretch cache warm). The earlier `fancy_likeem_fast.wav` predates the drawn curve.
- New plan `examples/fancy_likeem_bottomsup.plan.jsonc`: fancy_likeem with Like 'em All ending at its beat 160 (`id: likeem_end`), Bottoms Up on deck 3 from its beat 0 to 296, starting 32 beats before `likeem_end`; a placeholder 32-beat equal-power crossfade for the user to shape in `tune`. Validates; all three tracks analyzed.
- Bottoms Up analyzed: detected 148.000 (double-time), beat 0 @ 93 ms. Plan declares `bpm: 74`. Mix stays 91.93, so Bottoms Up plays ~24% fast (user's choice over a non-beatmatched short blend or a ~83 BPM compromise).
- `docs/milestones.md`: **echo out** added to M5 (beat-synced delay with feedback, dry cut on a downbeat, decaying tail, optional high-pass sweep; likely `"style": "echo_out"` in a new schema version) with an acceptance case (Like 'em All echoes out into Bottoms Up at native tempo). Removed delay from "Beyond M7".

### Tests
- 261 passed, 0 failed (~44 s). No code changes.

### Schema or interface changes
- none

### Dependencies added/removed
- none

### Open questions
- The user hasn't shaped (or saved) the Like 'em All → Bottoms Up transition yet; the plan still has the placeholder crossfade. Bottoms Up at +24% may sound rushed — revisit after listening (echo out in M5 would allow native tempo).
- Carried over: real-time spike vs. continuing M4.5 (T2/T4/T5/T6).

### Next session should
- Ask whether the user shaped/saved the Bottoms Up transition in `tune`; render it (`dj-segue play <plan> --render-to <wav>`) and commit the plan.
- Then decide real-time spike vs. M4.5 T2 (T2 would let the page lengthen the 32-beat overlap).

### Notes for future sessions
- The render command is `dj-segue play <plan> --render-to <file.wav>`; there is no `render` subcommand.
- Bottoms Up is another wrong tempo level (148 vs. the usual 74), like Fancy — beat_this agreed with the double level here, so `pick_tempo_level` didn't help. Watch for hip-hop tracks detected at 2×.
- Without a declared `bpm`, a too-high detected level would make `target_bpm: "mix"` slow a track down instead of speeding it up — always check the level before beatmatching.

---

## Session: 2026-10-03 → 2026-10-04 (M5 done, M3 stems, tune T7/T8, real-time engine)

**Milestone:** M5 — EQ and filters (done); M3 — Stems (done except the real-music wordplay acceptance); M4.5 T7/T8; R1 — Real-time engine (new, done)
**Duration:** approximately 75 turns
**Worked on:** Rendered Bottoms Up; master limiter; echo out; 3-band EQ; filter knob; stems with vocal handoff and per-stem EQ; all of it in the tune page; a block-based real-time engine that matches offline renders bit for bit.

### Roadmap changes (decided with user)
- M5 before the real-time spike (limiter first, then echo out, then EQ / filter). Then M3 stems, pulling per-stem EQ in from M5.
- Tempo map deferred to the real-time work, then deferred again to R2 (next session) along with live playback in tune.
- The wordplay acceptance for M3 (a real word handoff) is on hold: the user wants word timings first (M6).
- Real-time: user approved that all renders change once (new streaming stretcher) so live == offline.

### Completed
- **Limiter** (`executor/native/limiter.py`): -1 dBFS lookahead, built from finite windows with integer box sums so tune excerpts stay exact. Renders no longer clip. Finding: time-stretching raises peaks 4–7 dB on these heavily mastered tracks (phase), so the limiter works hard on stretched mixes.
- **Schema v0.6 echo out**: `echo: {every, feedback}`, optional `in`; the tail is a loop of the last `every` with pre-boundary seams. `examples/fancy_likeem_echo.plan.jsonc` (Like 'em All → Bottoms Up at 76.22 BPM, 3% over native; the user's call) plus a Fancy bass swap (eq lane). User: "that worked".
- **EQ** (`eq.py`): isolator with LR4 crossovers at 200 Hz / 2.5 kHz (rewritten, not ported; `professional_eq.py`'s shelves were approximations). Fixed filters, automated band gains: no zipper noise. M5 acceptance: a lows kill over 4 beats with the mids intact.
- **Schema v0.7 filter**: one knob per deck (-1 low-pass … +1 high-pass), numba TPT state-variable filter, exactly dry at 0; `echo.highpass` sweep. numba now listed (already present via librosa).
- **Tune T7**: echo outs as items, EQ/filter strips, waveforms after effects, item labels. jsonc_edit gained `append_item` and one-keyframe-per-line formatting.
- **M3 stems / schema v0.8**: path tracks separated automatically (demucs `htdemucs_ft` on MPS, ~40 s per track, cached in `.cache/stems`; "other" absorbs the residual), `stem_volume`, `vocal_handoff` (instrumentals crossfade, vocals swap at `swap_at` with a 10 ms crossover ending on the swap), eq `stem`. `preprocess` separates; `.[stems]` extra.
- **Tune T8** (uncommitted): vocal handoffs as items (drag the swap marker), stem picker for stem volume / EQ strips. The `tune` CLI now opens plans with only echo outs or handoffs.
- **R1 real-time engine** (uncommitted): `stretch.py` is now the Rubber Band library via ctypes (streaming, deterministic, independent of read sizes); `sources.py` (deck/stem audio block by block, seams, loops, seek warm-up), `chain.py` (stems → EQ → filter → volume, stateful), `live.py` (LiveEngine with three cursors, ring-buffer Player, `--watch` reload with 20 ms crossfade). `engine.py` rebuilt on these: `render_decks` → `levels` (now full-length per-deck signals) → `mix`. `play` starts at once; `--from BEAT`; `--watch`. Measured: echo mix 18× real time, stems handoff mix 5×. The stretch cache is gone.

### Tests
- 320 passed, 0 failed (~100 s).
- New: test_limiter, test_v06_echo, test_m5_eq, test_v07_filter, test_m3_stems, test_stretch (replaces test_stretch_cache), test_live; additions to test_tune.
- Key guarantees: live == offline bit for bit (random block sizes; through the real ring buffer + callback); tune excerpts == renders; seek: spectra within 1 dB, clicks within 1 ms.
- Removed: tests that expected EQ / stem_volume / vocal_handoff to be rejected.

### Schema or interface changes
- Schema v0.6 (echo_out), v0.7 (filter lane, echo.highpass), v0.8 (stems for path tracks, eq `stem`, vocal_handoff `swap_at` + shaping). Docs: `docs/schema-v0.6.md` … `v0.8.md`.
- Engine: `Levels` holds per-deck `signals`; `prepare()` / `Prepared`; `DeckRender.prepared`. `play` has `--watch` / `--from`.

### Dependencies added/removed
- Added: `demucs` (optional `.[stems]` extra), `numba` (declared; was transitive). Rubber Band is now used as a library (brew `rubberband` ships it), not the CLI.

### Open questions
- Tempo map (R2), and live playback in tune.
- Memory: full-length deck/stem buffers (~0.6 GB for a 7-minute mix with stems); fine for now.
- `.cache/stretch` deleted (user's call). Without a cache a full offline render of the echo mix takes ~14 s (was <1 s warm) and tune launches wait as long; the user chose to keep it that way for now. A deterministic per-deck cache could come back if it starts to hurt.
- Example WAVs in `audio/` predate the new stretcher (renders changed once, inaudibly).

### Next session should
- **R2 — tempo map + live playback in tune** (the agreed follow-up to R1):
  1. Tempo map: the mix clock can change tempo partway (e.g. Bottoms Up takes over at 74 after the echo out). Schema change (likely `meta.tempo_map` or a per-segment "take the clock"), `time_math` positions through the map, engine and tune follow it. Draft the schema for the user first.
  2. Tune page plays the whole mix live through the LiveEngine (seek on cursor click; edits applied a moment ahead while it plays), instead of re-rendering an excerpt WAV per edit.
- After R2: M6 word timing (Whisper word timestamps on the vocals stem, word cues like `{ "word": "up", "nth": 2 }`) to unblock M3's real wordplay acceptance.
- Not planned unless asked: a `render` subcommand / recording live output (offered; user declined for now), bringing back a stretch cache.

### Notes for future sessions
- Waveform RMS is the wrong measure for anything that restarts a phase vocoder (seeks, reloads): compare spectra and onset timing.
- `( cmd & )` from a non-interactive shell ignores SIGINT: test Ctrl-C via `subprocess.Popen` + `send_signal`.
- Two Chrome browsers are connected; "Browser 2" is on this machine (Browser 1 can't reach localhost).
- The tune server still reads `page.html` once at startup: restart after page edits.
- Equality guarantees rely on identical float order: decks summed in sorted order; stems in the track's stem order; sosfilt with zi is bitwise block-invariant (verified).

---

## Session: 2026-10-04 (R2 tempo map + live tune playback; tune UI; A1 annotate)

**Milestone:** R2 — Tempo map and live playback in tune (done); tune UI pass; A1 — Annotations for planning (started)
**Duration:** approximately 60 turns
**Worked on:** Schema v0.9 tempo map (steps + ramps), the tune page playing the whole mix live, a usability pass on tune, and a new `annotate` tool for describing tracks so transitions can be planned from the descriptions.

### Completed
- **milestones.md fixed:** R2 section; M6 renamed "Word timing and the planner" (word timing first); M3 marked done except its acceptance; later A1 added.
- **Schema v0.9 tempo map** (`583cd67`): `meta.tempo_map: [{at, bpm, ramp?}]` — steps, or ramps linear in time (N beats take 120·N/(b0+b1) s). Mix beats count through it; mix durations count beats from where they start; `target_bpm: "mix"` segments and loops follow it (Warp); numeric target_bpm ignores it; echo `every` uses the tempo just before the cut. `TempoMap` / `Warp` / `mix_duration` in time_math; `resolved_tempo()` in the validator (changes resolve in order; an `after` that moves when the tempo changes is rejected; `after` positions needing grids defer until preprocess). Stretcher takes `rate_at` + `breaks` (varying Rubber Band ratio). Example `examples/fancy_likeem_echo74.plan.jsonc` (Bottoms Up unstretched at 74). Docs: `docs/schema-v0.9.md`.
- **Live playback in tune** (`6fce85a`): `tune/live.py` (TuneLive) owns the sound device: play / stop / jump on a ruler click, gapless loop (next pass built ahead), each validated edit switched in like a `--watch` reload. `Player` gained `jump()` and `playing_at()` (playhead through jumps).
- **Tune UI pass** (`7446227`): tooltips everywhere + canvas hover hints with live values; `?` shortcut sheet; Bass / Mids / Highs; plain-language labels, refusals and a readable change summary (exact text behind a toggle); undo/redo; unsaved marker + confirms; refusal toast; legend; strip scales; arrow-key nudge of the selected handle/keyframe.
- **A1 `dj-segue annotate`** (`9f34a33`, `718213e`): page on scrub's base — grid check (½×/2×, downbeat ±1 beat, ±10 ms), tap 1–9 while playing to mark/label phrases, drag/split/merge (both directions, follows playback, keeps labels/notes), key with a drone check, tags/notes. Draft from analysis: detected section starts, Krumhansl key + confidence, per-bar stem activity (18 dB range below the stem's p95). Saves `annotations/<track>.json`; `summary()` is the planner's view.
- Drafts for Crazy in Love and Pretty Girls Walk in `annotations/` (uncommitted; the user has started annotating Crazy in Love).
- Reviewed the user's Crazy in Love annotation against published structure (see Open questions).

### Tests
- 353 passed, 0 failed (~2 min).
- New: test_v09_tempo_map (17), test_tune_live (4), test_annotate (11); additions to test_tune.
- Verified: plans without a tempo map render bit-identical to before (both real example mixes vs HEAD); live == offline with tempo changes; beats within 1.2 ms after a step, 0.6 ms through a ramp at 44.1 kHz.

### Schema or interface changes
- Schema v0.9 (additive): `meta.tempo_map`. Validation rule 17.
- `Prepared.mix_tempo` / `DeckRender.mix_tempo` are now `TempoMap` (was float); time_math functions take a TempoMap or a bpm.
- `Span.warp`, `CompiledPlay.warp`; `Stretcher(rate_at=, breaks=)`; `Player.pending` is (at, engine, jump).
- New CLI command `annotate`; new package `dj_segue.annotate`.

### Dependencies added/removed
- none

### Open questions
- **Crazy in Love annotation** (user's, in progress): "pre-hook" used where it doesn't precede a hook (6–9, 28–31, 58–59 — the last is a 2-bar horn hit inside Jay-Z's verse); 74–77 looks like the bridge (vocals forward, beat thinner), not a breakdown; several odd lengths (6, 10, 2, 11) where the song moves in 8-bar blocks + 2-bar horn tags; outro 86–96 stays full with vocals (no clean exit). Key: published D minor (7A); detector said G minor (6A). Grid and key not yet marked checked; no tags/notes.
- Pretty Girls Walk not annotated yet (key detected C# minor at only 17% confidence).
- Uncommitted user edits to `examples/fancy_likeem_bottomsup.plan.jsonc` (a reshaped fade-in + highs EQ lane on Fancy, saved from tune) — left alone; ask before committing.
- The browser tool's synthetic key presses/drags didn't reach the annotate page (clicks did; synthetic JS events worked). Real keys untested by me — confirm with the user.

### Next session should
- Finish both annotations with the user (apply the Crazy in Love review; annotate Pretty Girls Walk), commit `annotations/`.
- Then the A1 test: read only the two annotation summaries, propose a transition (where, technique, length, why), write it as a plan, render, and have the user rate it.
- After that: decide whether a transition proposer (candidate pairs + scoring + LLM choice) comes before M6 word timing.

### Notes for future sessions
- **Don't reproduce or quote song lyrics** — output gets blocked by the content filter ("Output blocked by content filtering policy") and the turn dies. Describe sections by function and performer only. genius.com can't be fetched anyway.
- Rubber Band applies a new time ratio to audio it already holds: look the rate up ~start-delay (2048 frames) behind the input at mid-piece; end pieces on tempo steps at `break + lag − CHUNK/2`. Calibrated on click tracks — re-check if CHUNK or Rubber Band options change.
- Real rap/R&B tracks have vocals almost everywhere; stem activity must be relative (18 dB range), or the vocals strip saturates.
- Pages read their HTML once at server start: restart `tune` / `annotate` after editing a page.
- A dirty page with a `beforeunload` guard blocks `tabs_close` in browser tests (a "Leave site?" dialog the user must dismiss) — save or reload before closing test tabs.
- Click into a page before sending keys from the browser tool; clicks made before an item finishes loading get reset.

## Session: 2026-10-11 (A1 annotations, transitions, M6 W1/W2/W4: word timing, word positions, finder; cues; tune markers)

**Milestone:** A1 — Annotations (two more tracks); M6 — Word timing and the planner (W1, W2, W4 done); M4.5 tune (T9 markers, loop edits)
**Duration:** approximately 150 turns
**Worked on:** finished annotating four tracks, built three transitions, then word timing → word positions in plans → a wordplay finder, plus cue points and tune deck markers.

### Completed
- **Annotations** for Crazy in Love, Pretty Girls Walk (`1d061ad`), Everybody (Backstreet's Back) (Extended) and Let's Get It Started (Spike Mix) (`1873791`). New labels `interlude` (0) and `riff` (-).
- **A1 test transitions** planned from the annotations (Pretty Girls Walk → Crazy in Love): `examples/pgw_cil.plan.jsonc` (horn drop on the vocals-only pre-hook bar, echo out) and `examples/pgw_cil_beatswitch.plan.jsonc` (PGW's verse vocal over CIL's looped interlude, by stems). **User rated both B / B+** ("not bad for a first try").
- **Live loop fix** (`38fdbad`): a loop segment rendered its whole span (×4 stems, stretched) inside one live block — a ~1.6 s dropout in tune. Loops now build in the background `LOOP_PREBUILD_SEC` (4 s) ahead. Bit-identical output.
- **renders/** (git-ignored) holds all rendered WAVs; old renders moved there from `audio/`.
- **M6 W1 `dj-segue words`** (`437aeeb`): LRCLIB lyrics (line-timed or plain) force-aligned to the separated vocals with torchaudio's wav2vec2 (no WhisperX: it would downgrade PyTorch), checked against faster-whisper large-v3-turbo (greedy, repetition penalty 1.15, no VAD): lyric words `confirmed` when Whisper heard them within 0.25 s; Whisper-only words in gaps added (ad-libs). Cached per step in `.cache/words/<file sha1>/`. Cross-check vs Whisper's own times: median ~10 ms.
- **Annotate shows lyrics** (`f5b5de8`, `74f30d6`): words lane under the zoomed waveform, follow-along lyrics panel (click a word to jump). User: "the lyrics looked pretty good".
- **M6 W2, schema v0.10 word positions** (`168a3f9`): `{word, nth, edge, offset}` in track positions; `+ "in": <play segment>` in mix positions and a handoff's `swap_at`. Example `examples/pgw_cil_know.plan.jsonc` (handoff on a shared "know", 71 ms into the outgoing word) — **not yet listened to / rated**.
- **Cue points in annotate** (`665b46d`): `m` marks a named cue (snapped; ⇧M exact), stored as seconds in the annotation; any plan can use `{ "cue": name }` without declaring it.
- **LGIS → Everybody** (`4166042`, `53eec1e`): the user's design — play LGIS to its "everybody" cue, loop 4 beats ×4 while Everybody's first 15 beats crossfade in, jump to Everybody's "everybody" pickup (hook on the downbeat); whole mix at 108.06. **User: "that sounded perfect."**
- **Tune T9** (`c9936fb`): deck lanes show segment starts / jumps, loops (×N, a tick per pass) and annotation cues; loop length and passes editable (written to the plan, re-rendered).
- **`dj-segue prepare`** (`25c89df`): beats, stems, words and an annotation draft for new tracks in one step (existing annotations kept).
- **M6 W4 finder `dj-segue find a b`** (`571c985`): same words / phrases, sound-alikes, rhymes (CMU dictionary), ranked by match, prominence, reliability (a cue named after the word counts as verified) and beat-of-bar fit; suggests vocal handoff / loop and jump / echo out. Ranks "everybody" first for LGIS → Everybody at the user's cue, with loop and jump suggested. Across the four tracks it also found "walk" (LGIS ↔ Pretty Girls Walk, a title word) and "Roc"/"rock" (Crazy in Love ↔ Everybody).
- Housekeeping: deleted the unused Whisper large-v3 model (2.9 GB, `~/.cache/huggingface`); milestones M5 note fixed.

### Tests
- 395 passed, 0 failed (~2.5 min). Up from 353.
- New: test_words (12), test_v010_words (12), test_prepare (3), test_finder (7); additions to test_live (loop prebuild), test_annotate (words endpoint, cues ×3), test_tune (markers, loop edit, fade across a jump, 0.10 preview), test_time_math / test_validator (cue fallback).

### Schema or interface changes
- **Schema v0.10** (additive, `docs/schema-v0.10.md`): word positions; rule 18; rule 4 amended (an undeclared cue resolves from the track's annotation, checked after preprocessing); rule 17 (no tempo change at a word).
- `TrackGrid` gained `words` and `cues` (not part of equality); the preprocessor fills them for tracks that need them (`PreprocessResult.words` / `.cues`).
- `resolve_timeline` anchors are an `Anchors` dict with `.clocks` (play segment id → `SegmentClock`, track → mix seconds).
- Annotation JSON gained `cues: [{name, second, note}]`; `summary()` lists cues.
- New CLI commands: `words`, `prepare`, `find`. New packages/modules: `dj_segue.words`, `dj_segue.prepare`, `dj_segue.finder`. Annotate server: `GET /words`; tune server: `POST /loop`.
- Tune's schema version list is derived from `SUPPORTED_SCHEMA_VERSIONS` (it had stopped at 0.9).

### Dependencies added/removed
- New optional extra `words`: `faster-whisper`, `mutagen`, `pronouncing` (CMU dictionary). Uses the existing torchaudio for alignment. Whisper turbo weights (~1.6 GB) download on first use.

### Open questions
- **Keys and grids are unchecked on all four annotated tracks.** Crazy in Love says G minor at "100%" vs published D minor; Pretty Girls Walk 17% confidence. The finder's key notes say "unchecked".
- **M3 acceptance** ("vocals end on a word, the other's start on the same word, drums continuing"): `pgw_cil_know` is the candidate; needs a listen and rating.
- **A1 acceptance**: the user's B / B+ — pass or not? User's call.
- Whisper skips chants (the repetition penalty), so chant sections stay unconfirmed; and word timing can't be corrected by hand yet (the aligned "everybody" in Everybody was 1.3 beats off the user's cue).
- Tune can't yet drag jump points (T4), a loop's `from` (T5), or fades that move audio (T2); no words strip in tune.

### Next session should
- **Templates:** turn a finder candidate into a plan (vocal handoff, loop and jump, echo out on the word), then a **blind proposal** — e.g. "walk" (LGIS → Pretty Girls Walk) or "Roc"/"rock" (Crazy in Love → Everybody) — built without the user's input, for the user to rate.
- Before relying on keys: the user checks grid and key on the four tracks (annotate, Drone).
- Listen to and rate `pgw_cil_know` (M3 acceptance).

### Notes for future sessions
- **A `.beats` sidecar's `grid_anchor_sec` is the raw beat phase, not the plan's beat 0** (beat 0 adds the downbeat shift). `dj-segue preprocess <plan>` prints the real "beat 0 @ … ms"; annotation bar N = plan bar N. Misreading this cost a stuttering render (memory: beats-anchor-is-raw-phase).
- Render to `renders/<plan>.wav` (git-ignored), not a scratch folder.
- **Lyrics never in output**: tools print counts and single words only; lyrics stay in `.cache/words/`. Single cue words in plans are fine.
- Verify a feature in the real pages, not just by loading: tune *loaded* a 0.10 plan but couldn't *preview* one (hard-coded version list). Browser checks caught three page bugs today (cue rows not attached, focus lookup, lyric scroll measuring against the page).
- Whisper: turbo + `repetition_penalty=1.15`, no VAD, greedy — large-v3 with beam search was 15× slower and no better; turbo without the penalty looped (one word ×203); VAD gating skipped whole verses.
- A loop's live render is built ahead (4 s); seeking to <~2 s before a loop can still stall briefly.
- The browser tool's tabs are hidden: `requestAnimationFrame` doesn't run, so call page functions directly when testing.

---

## Session: 2026-10-17

**Milestone:** M6 — Word timing and the planner
**Duration:** approximately 60 turns
**Worked on:** wordplay rules from the user's ratings, loop-and-jump templates, wordplay spots in annotate, three tracks annotated with spots

### Completed
- **Finder rules** (`566634d`), from the user's ratings of `pgw_cil_know` ("odd because the transition happened during vocals") and a blind proposal (C-: "again you are doing a transition mid verse. and the stutter loop was very harsh"):
  - transitions only at a section's first or last bar, in both songs;
  - never mid-line (line edges from lyric lines or a gap of a beat or more); B may enter at its line start if its word follows within 4 beats;
  - the two words heard back to back;
  - words leading into a hook rank higher.
- **Templates** (`src/dj_segue/templates.py`, `566634d`; `dj-segue find --plan N [-o path]`): a candidate becomes a loop-and-jump plan:
  - A plays from a section start 8+ bars before its word and loops a bar from the beat;
  - B's bed is its first K beats (K ≡ entry mod 4), then it jumps to its word or line start, vocals up;
  - tempo: B's if within 4%, else a ramp.
  - It reproduces the user's "perfect" LGIS → Everybody plan.
- **Wordplay spots** (`566634d`, `8a7efd6`): an annotate cue can take a role (out / in / both), a word and an out loop (1–16 beats). Spots override the rules, rank first, and stand in where a word wasn't aligned. `find --spots` shows only pairs at spots.
- **Annotated with spots:**
  - LGIS: "everybody" out, 2-beat loop, bar 40.
  - Everybody: "everybody" in, bar 51.
  - Starships (new, `prepare`): "one more time" out, bar 27.
  - One More Time (new, `prepare`; sections mostly unlabelled): two "one more time" in spots, bars 47 and 131.
- **LGIS → Everybody from the user's spots** (`examples/lgis_e_everybody.plan.jsonc`): **rated A-**. The blind "walk" proposal was C- and was deleted.
- **Starships → One More Time** (`afe5b98`):
  - **Phrase fix:** a phrase a spot names now counts even when every word is common ("one more time" had been dropped as a phrase).
  - **First render "wasn't good":** it entered on a guessed boundary (bar 71), not the user's spot. Re-made as `starships_omt_spot1/2`, one into each spot.
  - **Fade:** the user said "you cut the first song off vs. letting the loop die into the second song". A now crossfades out over the whole bed instead of holding and then dropping 2 beats before B.
- **Fixes:**
  - a word a hair before a bar line counts in that bar;
  - titles drop an "Artist - " prefix;
  - track ids: one-word titles kept whole, apostrophes dropped;
  - the loop no longer runs a pass too many.
- **Housekeeping:**
  - `.DS_Store` untracked (it had been committed in the seed despite `.gitignore`); `uv.lock` ignored;
  - rejected plans and renders deleted (`pgw_cil_know`, `lgis_pgw_walk`).

### Tests
- 410 passed, 0 failed (~2.5 min). Up from 395.
- New: finder rules and spots (test_finder, incl. common-word spot phrases), test_templates (6), wordplay spots in test_annotate.

### Schema or interface changes
- Plan schema: none (still v0.10).
- Annotation cues gained optional `role` (`out` / `in` / `both`), `word` and `loop` (beats, out spots only); plain cues are saved as before.
- `Track.spots`; `find(..., spots_only=)`; new `dj_segue.templates`; `dj-segue find` gained `--spots`, `--plan N`, `--out`.

### Dependencies added/removed
- none

### Open questions
- What kept LGIS → Everybody at A- rather than A? Not answered. Candidates: the 2-beat loop (the perfect plan looped a bar), or the vocals' rise.
- `starships_omt_spot1/2` (re-rendered with the fade) haven't been rated yet.
- An "in" cue that isn't wordplay (a good place to bring a song in, no shared word) has no role. The user's Everybody "intro" spot was made a plain cue instead.
- Keys and grids are still unchecked on all six annotated tracks.

### Next session should
- Listen to and rate `starships_omt_spot1` / `spot2`.
- Finish One More Time's annotation (label sections, check grid and key).
- Spots on more tracks: Bottoms Up and Like 'em All (no annotations yet); Pretty Girls Walk and Crazy in Love (no spots).
- Delete `examples/starships_omt_one_more_time.plan.jsonc` and `renders/starships_omt_one_more_time.wav`. Both are untracked and replaced by the spot plans.

### Notes for future sessions
- **Ask for spots before proposing blind.** The user's ear beats the rules: spots got A-, the blind pick C-. See memory "Wordplay transition rules".
- **Wordplay rules:**
  - never mid-verse in either song;
  - the two words back to back, not overlapping and not far apart;
  - bar loops, not ½-beat stutters;
  - let the loop die into B, don't cut it.
- **After editing an annotation by hand, stop that song's annotate server.** Saving from the open page overwrites the edit. The server also caches `page.html`, so restart it after page changes.
- Check spot words for typos: the user's "one more time 2" (the cue's name typed into the word box) could never match. Annotate's prefill may be worth tightening.
- `prepare` finds no lyrics for files named "Artist - Title" when the artist isn't the singer (Starships by "DJ's International"): words were Whisper only.
