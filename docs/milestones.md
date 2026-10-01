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

## M4 — Loops  (DONE 2026-09-30, ahead of M3)

**Goal:** Tightening loops and other rhythmic effects. (Pulled ahead of M3 on 2026-09-30: loops don't need stems.)

**Scope in:**
- `loop` segment (schema v0.3) with a `schedule` of (length, repetitions) steps. Decided 2026-09-30 over a `loop_length` automation lane: a lane would make segment timing depend on automation.
- Sample-accurate loop boundaries; click-free seams (3 ms crossfade ending on each boundary).
- Segment `id` + `{"after": id, "offset"}` mix positions, and `target_bpm: "mix"` (schema v0.3; ideas taken from an earlier trigger-based prototype).
- `dj-segue scrub`: a local web page to find plan positions by ear — live beat/bar numbers, a song map (energy/bass per bar, section starts, breaks from `analyzer/structure.py`), seek by beat/bar/section, loops, click, marks.

**Acceptance test:** A track with a 4→2→1→0.5 beat tightening loop, beat-locked to the master clock (`tests/test_m4_loops.py`: every rep starts on its exact sample; stretched loops stay within 4 ms of the mix grid). Real-music demo: `examples/starships_omt.plan.jsonc`.

---

## M4.5 — Tune: adjust transitions, jumps and loops by ear  (PLANNED 2026-09-30)

**Goal:** Try transition, jump and loop options in a local web page, hear them exactly as `render` will produce them, and save the result back into the plan. (Added 2026-09-30 after hand-tuning the Fancy → Like 'em All blend. The plan file stays the source of truth; this is a listening tool, not a full editor.)

**Shape:** `dj-segue tune <plan>` — local server (stdlib, like `scrub`) + one vanilla-JS page. Pick an item (a transition, a jump, or a loop) from a list; the page shows that region of the mix with both decks' waveforms on the mix beat grid.

**Decided with user (2026-09-30):**
- Save edits the plan **in place**: only the changed keys of that one segment's text are replaced/added/removed; every other byte (comments, formatting, other segments) is kept.
- Curves can be **drawn freehand** (schema v0.5, below).
- Scope: transitions, **jump points** and **loop points**.

**Accuracy rule:** the browser never re-implements engine maths. Each edit is sent to the server, which validates it with the real validator and returns either (a) per-deck gain curves sampled every 1 ms from the real compiler (gain-only edits: instant), or (b) a re-rendered region from the native engine (edits that move audio: a jump/loop point, or a window that moves a segment's start; ~1 s for a few bars).

### Phases

**T1 — Transitions, preset curves.**
- Windowed render in the engine (`render(..., window=(a, b))`: compile only the spans that overlap the window) and per-deck pre-gain audio for the region.
- Page: waveforms, beat grid, drawn gain curves; drag out/in window edges (snap to beat; ½/¼ with a modifier); preset curve per side; play from N beats before / loop the region; beat readout.
- In-place JSONC save (position-tracking scanner over the plan text; field-level replace/insert/delete).

**T2 — Windows that move audio.** Dragging a window past the current overlap moves the incoming segment's `start_at` (and `to` of the outgoing one if needed) → region re-render. The page lists later segments with absolute `start_at` that won't follow the change (suggests `after`).

**T3 — Freehand curves (schema v0.5).**
- `curve` may be `{ "points": [[t, gain], …], "smooth": bool }`: t and gain in 0–1 across that side's window; t strictly increasing; an `in` curve starts at 0 and ends at 1, an `out` curve 1 → 0. `smooth: false` → straight lines; `true` → monotone cubic (PCHIP: smooth, never overshoots past its points, so gain stays in 0–1).
- Gains may go up and down inside the window (swells, dips, gated chops). Segments steeper than 2 ms get a 2 ms ramp so nothing clicks.
- Page: pencil — press and drag to paint over the window (the stroke replaces the curve under it, like DAW automation drawing); the stroke is simplified (Ramer–Douglas–Peucker) to a small point list that stays editable — drag/add/delete points; Shift draws straight lines; smooth toggle.
- Compiler/engine: a `points` ramp shape, evaluated vectorised (`np.interp` / PCHIP).

**T4 — Jump points.** A jump = two back-to-back segments on one deck. Drag the outgoing `to` and incoming `from` along track beats (snap); play across the jump with pre/post-roll; region re-render (includes the jump seam).

**T5 — Loop points.** Drag a loop's `from`; edit its `schedule` (length, repetitions) in a small table; play the loop with pre-roll; region re-render.

**T6 — A/B snapshots.** Save the current setting as a snapshot, keep editing, switch between snapshots while playing.

**Acceptance test:** The Fancy → Like 'em All blend reshaped in the page with a hand-drawn `in` curve and a moved jump point; saved plan differs from the original only in those keys (comments intact); `dj-segue render` of the saved plan matches what the page played (same sample-level gains; region audio byte-identical).

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
