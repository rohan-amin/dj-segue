# dj-segue Milestones

Each milestone produces a working program and a demoable test mix. Don't skip ahead — earlier milestones de-risk later ones.

---

## M1 — Hello Mix (the spine)

**Goal:** Prove the architecture end-to-end with the smallest possible feature set.

**Deliverable:** Running `dj-segue play examples/hello_mix.plan.jsonc` plays two short tracks back-to-back through the speakers, with a hard cut between them, no overlap. Same plan, run as `dj-segue play examples/hello_mix.plan.jsonc --render-to /tmp/hello.wav`, produces a sample-accurate WAV file.

**Scope in:**
- Plan loader (JSONC parsing, `pydantic` validation, schema-version check).
- Inspector (`dj-segue inspect` prints human-readable plan summary).
- Preprocessor (computes BPM/beat grid for tracks, populates `.beats` cache; no stem separation in M1).
- Native executor: load audio via `soundfile`, schedule sample-accurate deck switches, mix to stereo output, write to `sounddevice` stream OR WAV file.
- Two-deck capability with the `play` segment type only.
- One automation lane: `deck_volume` with linear interpolation.
- No transitions, loops, EQ, stems.

**Acceptance test:** `examples/hello_mix.plan.jsonc` plays correctly in both live and offline modes; rendered WAV passes a golden-file comparison.

**Estimated effort:** 2–3 focused sessions.

---

## M2 — Crossfades and per-deck automation  (DONE 2026-09-29)

**Goal:** Real DJ transitions.

**Scope in:**
- `transition` segment type, `style: "crossfade"` and `style: "cut"`.
- Compiler that expands transitions into per-deck volume automation.
- All `interpolation` modes (linear, step, exponential).
- Beat-locked timing: when `start_at` is on a beat, audio crossover happens sample-accurately on that beat.
- `crossfader` lane (two-deck gain law).
- Unsupported lanes/segments hard-fail with `NotImplementedError` naming the milestone that adds them — never silently ignored (decided 2026-09-29).

**Acceptance test:** A 3-track mix with two crossfades sounds right; rendered WAV matches golden.

---

## M2.5 — Tempo matching  (DONE 2026-09-29)

**Goal:** Beatmatch tracks with different BPMs — required for general mixing. (Added 2026-09-29.)

**Scope in:**
- `play.target_bpm` (additive minor schema bump, v0.2): time-stretch a segment to a target tempo via the `rubberband` CLI (R3 engine). Pitch preserved.
- `track.bpm` optional (v0.2): detected by default by fitting a fixed grid to onset energy; a declared value overrides.
- Beat grids anchored to the detected beat phase (`TrackGrid`), so beat positions line up with real audio.
- Real-music fixtures (local, git-ignored `audio/`) for integration testing.
- Executor tempo handling designed for the eventual tempo automation lane — don't bake in "tempo is a per-segment constant".

**Acceptance test:** Two real tracks at different BPMs crossfade with beats audibly aligned through the transition.

---

## M2.6 — Grid robustness  (DONE 2026-09-30)

**Goal:** Beat grids that are right on real music, including which beat is a bar's "1". (Added 2026-09-30, after comparing with Mixxx's analyzer.)

**Scope in:**
- Half-beat disambiguation: the kick/bass band decides beat vs. off-beat (loud off-beat hi-hats pulled the grid half a beat off).
- BPM rounding to musical values (whole, ½, ⅓, ¼) when drift over the track stays under 10 ms (after Mixxx's `roundBpmWithinRange`).
- Downbeat detection via beat_this (optional `downbeats` extra); beat 0 = first downbeat.

**Acceptance test:** Loud-hat drum patterns lock to the kick; whole-number tempos detect exactly; trimming k beats off a track's start moves beat 0 by k (verified 8/8 on two real tracks).

---

## M3 — Stems

**Goal:** Vocal-aware transitions.

**Scope in:**
- Stem-aware track loading (4 `.npy` files per track).
- Per-stem volume automation lane.
- `transition` style `"vocal_handoff"`.
- Stem separation in the preprocessor (demucs).

**Acceptance test:** A wordplay-style transition where one track's vocals end on a word and the other track's vocals start on the same word, with drums continuing under the transition.

---

## M4 — Loops

**Goal:** Tightening loops and other rhythmic effects.

**Scope in:**
- `loop` segment type with `length_beats` and `repetitions` parameters.
- Length-as-automation for tightening loops.
- Sample-accurate loop boundaries.

**Acceptance test:** A track with a 4→2→1→0.5 beat tightening loop, beat-locked to the master clock.

---

## M5 — EQ and filters

**Goal:** Frequency-domain DJ moves.

**Scope in:**
- 3-band EQ per deck and per stem (port `professional_eq.py`).
- Lowpass/highpass filter automation lane.
- Crossfaded coefficient updates (no zipper noise).
- Master limiter on the mix output, so stretched/summed peaks above 0 dBFS don't hard-clip (decided 2026-09-30; no interim gain cut).

**Acceptance test:** A transition that kills the lows on the outgoing track over the last 4 beats while keeping vocals intact.

---

## M6 — The planner  (DEFERRED 2026-09-29: general mixing first)

**Goal:** AI-generated plans from English.

**Scope in:**
- Lyrics ingestion (LRCLIB or Genius-with-alignment).
- Wordplay candidate detection (exact, phonetic, rhyme, semantic).
- LLM-based candidate ranking with tool use.
- Output: a valid plan that the executor can play.

**Acceptance test:** Given two well-known songs and the prompt "find a clever wordplay transition", the planner produces at least one plan that a human DJ rates as "interesting" rather than "forced".

---

## M7 — Mixxx fallback executor  (DEFERRED 2026-09-29)

**Goal:** Validate the architecture by porting the existing Mixxx bridge as an alternative executor.

**Scope in:**
- Implement `MixExecutor` for Mixxx, using the existing `DjSegue.js` MIDI bridge.
- Map plan operations to Mixxx ControlObject changes.
- Document the precision and feature gaps vs. native.

**Acceptance test:** The same plan that runs on native also runs on Mixxx, with documented quality differences.

---

## Beyond M7

- Reactive mode (live triggers, conditional branches)
- Effects beyond EQ (delay, reverb, flanger)
- Key shifting at the segment level
- Plan diffing and remixing
- Web UI for plan visualization
- Real-time stem separation (currently offline-only)
- Real-time tempo control (currently fixed-tempo)

These are not on the v0.x roadmap but are good ideas for v1.x.
