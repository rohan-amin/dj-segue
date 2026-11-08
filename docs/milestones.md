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

**T1 — Transitions, preset curves.**  (DONE 2026-09-30)
- Windowed render in the engine (`render(..., window=(a, b))`: compile only the spans that overlap the window) and per-deck pre-gain audio for the region.
- Page: waveforms, beat grid, drawn gain curves; drag out/in window edges (snap to beat; ½/¼ with a modifier); preset curve per side; play from N beats before / loop the region; beat readout.
- In-place JSONC save (position-tracking scanner over the plan text; field-level replace/insert/delete).

**T2 — Windows that move audio.** Dragging a window past the current overlap moves the incoming segment's `start_at` (and `to` of the outgoing one if needed) → region re-render. The page lists later segments with absolute `start_at` that won't follow the change (suggests `after`).

**T3 — Freehand curves (schema v0.5).**  (DONE 2026-09-30)
- `curve` may be `{ "points": [[t, gain], …], "smooth": bool }`: t and gain in 0–1 across that side's window; t strictly increasing; an `in` curve starts at 0 and ends at 1, an `out` curve 1 → 0. `smooth: false` → straight lines; `true` → monotone cubic (PCHIP: smooth, never overshoots past its points, so gain stays in 0–1).
- Gains may go up and down inside the window (swells, dips, gated chops). Segments steeper than 2 ms get a 2 ms ramp so nothing clicks.
- Page: pencil — press and drag to paint over the window (the stroke replaces the curve under it, like DAW automation drawing); the stroke is simplified (Ramer–Douglas–Peucker) to a small point list that stays editable — drag/add/delete points; Shift draws straight lines; smooth toggle.
- Compiler/engine: a `points` ramp shape, evaluated vectorised (`np.interp` / PCHIP).

**T4 — Jump points.** A jump = two back-to-back segments on one deck. Drag the outgoing `to` and incoming `from` along track beats (snap); play across the jump with pre/post-roll; region re-render (includes the jump seam).

**T5 — Loop points.** Drag a loop's `from`; edit its `schedule` (length, repetitions) in a small table; play the loop with pre-roll; region re-render. **Partly done 2026-10-11 (T9 below):** a single-step loop's length and passes are edited on the page (written to the plan at once, decks re-render). Dragging `from` still open.

**T9 — What plays on each deck.**  (DONE 2026-10-11) Deck lanes show segment starts / jumps ("→ cue: everybody"), loops (shaded, ×N, a tick per pass) and the cues marked in each track's annotation. Fades may run across back-to-back segments (a jump). Fixed: tune couldn't preview schema-0.10 plans.

**T7 — EQ, filter and echo out.**  (DONE 2026-10-03)
- Echo outs are items: drag the cut (tail and incoming fade move with it) and the tail's end; `every` / feedback / high-pass controls; the incoming deck's hard entry drags into a fade (`in`).
- Two strips under the decks edit their `eq` (low / mid / high) and `filter` lanes over the region: click adds a keyframe (beat snap, Alt ¼), drag moves, double-click removes. Untouched keyframes keep their text, moved ones their position's form, new ones follow the transition's `start_at` form; new lanes are appended to `automation`. Saving bumps `schema_version` when the edit needs it.
- Deck waveforms show the audio after EQ, filter and volume; the echo tail is drawn over the outgoing deck. Item labels name the decks, kind and mix beat.

**T8 — Stems.**  (DONE 2026-10-04)
- Vocal handoffs are items: their instrumental fades edit like a crossfade's; the vocal swap is a marker to drag (written as `swap_at`; "Halfway" resets it). Lanes show the instrumentals' gain, the vocals' as a dashed line.
- A stem picker next to the lane buttons: on a deck mixed by stems, the strips edit that stem's volume (`stem_volume`) and EQ (`eq` + `stem`). A deck not mixed by stems says so (making it one needs a re-render: add a stem lane in the plan and reopen).

**T10 — Mashups (no transition).**  (PROPOSED 2026-11-07) Tune only opens plans with a crossfade / echo out / vocal handoff, shown ±16 beats. A stems mashup (`examples/weekend_voyager.plan.jsonc`) has none: open the whole mix, or a chosen section, with the stem strips editable anywhere. Not yet agreed.

**T6 — A/B snapshots.** Save the current setting as a snapshot, keep editing, switch between snapshots while playing.

**Acceptance test:** The Fancy → Like 'em All blend reshaped in the page with a hand-drawn `in` curve and a moved jump point; saved plan differs from the original only in those keys (comments intact); `dj-segue render` of the saved plan matches what the page played (same sample-level gains; region audio byte-identical).

---

## M3 — Stems  (DONE 2026-10-03 except the acceptance test)

**Goal:** Vocal-aware transitions.

**Scope in:**
- Stem-aware track loading (4 `.npy` files per track).
- Per-stem volume automation lane.
- `transition` style `"vocal_handoff"`.
- Stem separation in the preprocessor (demucs).

**Done 2026-10-03 (schema v0.8):** automatic separation of `path` tracks (demucs `htdemucs_ft`, `.[stems]` extra; cached in `.cache/stems`; "other" absorbs the residual so stems sum to the track) instead of hand-made `.npy` files; explicit `stems` still work. `stem_volume` lanes; `vocal_handoff` (instrumentals crossfade, vocals swap at `swap_at` with a 10 ms crossover); per-stem EQ (`eq` lane `stem`, pulled in from M5). Only decks mixed by stems render by stem.

**Acceptance test** (on hold 2026-10-04 until word timings exist; see M6)**:** A wordplay-style transition where one track's vocals end on a word and the other track's vocals start on the same word, with drums continuing under the transition.

---

## R1 — Real-time engine  (DONE 2026-10-04)

Pulled forward from the architecture's open question. One engine, run block by block: live playback is computed ~0.4 s ahead of the speakers and is bitwise identical to an offline render (tested: random block sizes, ring buffer through the real callback). Rubber Band library (streaming, ctypes) replaces the CLI; the stretch cache is gone. `play` starts at once; `--from BEAT`; `--watch` applies plan edits while playing (new engine ~0.3 s past what's queued, 20 ms crossfade). All renders changed once (inaudibly) with the new stretcher.

---

## R2 — Tempo map and live playback in tune  (DONE 2026-10-04)

**Goal:** The mix clock can change tempo partway through, and the tune page plays the whole mix live.

**Scope in:**
- Tempo map: the mix tempo changes partway (e.g. Bottoms Up takes over at 74 after the echo out). Schema change (likely `meta.tempo_map` or a per-segment "take the clock"; draft for the user first); `time_math` positions through the map; engine and tune follow it.
- Tune page plays the whole mix through the LiveEngine: seek on cursor click, edits applied a moment ahead while it plays, in place of re-rendering an excerpt WAV per edit.

**Acceptance test:** Like 'em All echoes out into Bottoms Up, and the mix clock moves to 74 BPM so Bottoms Up plays at native tempo for the rest of the mix. In tune, dragging a transition while the mix plays is heard within a moment, with no gap.

**Tempo map done 2026-10-04 (schema v0.9):** `meta.tempo_map` with steps and ramps (linear in time); mix beats count through it; `target_bpm: "mix"` segments and loops follow it (Rubber Band with a changing ratio; beats within 1.2 ms after a step, 0.6 ms through a ramp at 44.1 kHz); live == offline still bit for bit; plans without a map render unchanged. Tune counts beats through the map. Acceptance mix: `examples/fancy_likeem_echo74.plan.jsonc` (Bottoms Up unstretched at 74).

**Live playback in tune done 2026-10-04:** the page plays the whole mix through this machine's speakers (`tune/live.py`): play / stop / jump on a ruler click, loop the blend (a gapless jump back, built ahead), and each validated edit is heard ~0.3 s past what's queued (about half a second after the edit). The per-edit excerpt is still mixed, for drawing and tests.

---

## M5 — EQ and filters

**Goal:** Frequency-domain DJ moves.

**Scope in:**
- 3-band EQ per deck and per stem (port `professional_eq.py`). **Per deck done 2026-10-03** (`executor/native/eq.py`; rewritten rather than ported: fixed LR4 crossovers at 200 Hz / 2.5 kHz, automated band gains, so no coefficient updates or zipper noise). Per stem: done with M3 (v0.8).
- Lowpass/highpass filter automation lane. **Done 2026-10-03 (schema v0.7):** `filter` lane (one knob per deck, -1..+1), numba state-variable filter; also `echo.highpass`.
- Crossfaded coefficient updates (no zipper noise).
- Master limiter on the mix output, so stretched/summed peaks above 0 dBFS don't hard-clip (decided 2026-09-30; no interim gain cut). **Done 2026-10-03:** `executor/native/limiter.py`, -1 dBFS sample-peak ceiling, ~10 ms lookahead/release, 50 ms hold; built from finite windows so tune excerpts stay byte-identical to renders.
- **Echo out** (added 2026-09-30, pulled from "Beyond M7"): a beat-synced delay with feedback on the outgoing deck; the dry signal cuts on a downbeat and the echo tail decays over N beats, optionally through a high-pass sweep (the filter lane above). Lets a mix leave a track without beat-overlap, so big tempo gaps (Like 'em All 93 → Bottoms Up 74) needn't be stretched. Likely `"style": "echo_out"` on a transition (echo length in beats, feedback, tail length) in a new schema version; deterministic, beat-locked parameters. Tune page shows and adjusts the tail. Until then, the nearest substitute is a tightening loop roll + fade. **Done 2026-10-03 (schema v0.6):** `style: "echo_out"` with `echo: {every, feedback}` and optional `in`; acceptance mix `examples/fancy_likeem_echo.plan.jsonc`. High-pass sweep: `echo.highpass` (v0.7). Showing/adjusting the tail in tune: done in T7.

**Acceptance test** (lows kill: done 2026-10-03, `tests/test_m5_eq.py` + a Fancy bass-swap demo; echo out: done)**:** A transition that kills the lows on the outgoing track over the last 4 beats while keeping vocals intact. Plus: Like 'em All echoes out (1-beat echo, tail over ~4 beats) into Bottoms Up at its native tempo; the echo repeats land on the mix beat grid.

---

## A1 — Annotations for planning  (STARTED 2026-10-04)

**Goal:** Describe tracks well enough that transitions can be chosen from the descriptions: phrases (start bar, length, label, note), the key, tags, and per-bar stem activity, checked by a human.

**Done 2026-10-04:** `dj-segue annotate` (a page on scrub's base): grid check, tap-to-phrase while listening, labels and notes, key with a drone check, tags; a draft from analysis (detected section starts, Krumhansl key, stem activity per bar from the separator); `annotations/<track>.json`; `summary()` is what a planner reads (phrases with energy and stems, no-vocal runs, breaks, key with Camelot).

**Acceptance test:** With two tracks annotated (Crazy in Love, Pretty Girls Walk), a transition chosen from the two annotations alone sounds like one a DJ would use.

---

## M6 — Word timing and the planner  (word timing next after R2, agreed 2026-10-04; planner DEFERRED 2026-09-29)

**Goal:** Word-level timing for every track, then AI-generated plans from English.

**Progress (2026-10-11):**
- **W1 done:** `dj-segue words` — LRCLIB lyrics aligned to the vocals stem (torchaudio wav2vec2), checked against Whisper (faster-whisper large-v3-turbo); merged words, `confirmed` where Whisper heard them; cached in `.cache/words/`. Annotate shows a words lane and a follow-along lyrics panel.
- **W2 done:** schema v0.10 word positions `{ "word", "nth", "edge", "offset" }` (track), `+ "in": segment` (mix, `swap_at`). Its example (`pgw_cil_know`, a handoff on a shared "know") was rated odd (it switched mid-vocals) and removed; `tests/test_v010_words.py` covers word positions.
- **Cues and tune markers (2026-10-11):** annotate marks named cues (`m`), which any plan can use as `{ "cue": name }`. Tune shows each deck's segments (jumps "→ cue: …"), loops (×N, a tick per pass; length and passes editable, written to the plan and re-rendered) and annotation cues.
- **Finder (W4) done 2026-10-11:** `dj-segue find a b` — same words / phrases, sound-alikes, rhymes (CMU dictionary), ranked by match, prominence (title, hook), reliability (Whisper, or a cue marked by ear) and beat-of-bar fit; suggests vocal handoff / loop and jump / echo out. Ranks "everybody" first for LGIS → Everybody, at the user's own cue, with loop-and-jump among the suggestions.
- `dj-segue prepare` gets a new track ready in one step.
- **Wordplay rules, templates and spots (2026-10-17):** the finder only offers transitions at a section's first or last bar in *both* songs, never mid-line, with the two words heard back to back. `dj-segue find --plan N` writes a loop-and-jump plan (A loops a bar from its word and dies away over B's bed; B jumps to its word or line start). Annotate cues can be **wordplay spots** (out / in / both, a word, an out loop of 1–16 beats); spots override the rules and rank first. A blind proposal (LGIS → Pretty Girls Walk on "walk") was rated **C-** (mid-verse, harsh ½-beat stutter); a plan from the user's own spots (LGIS → Everybody) **A-**.
- Next: rate Starships → One More Time (`starships_omt_spot1/2`); spots on more tracks; tune words strips.

**Scope in:**
- **Word timing first** (unblocks M3's acceptance test): Whisper word timestamps on the vocals stem, cached per track; plan word cues such as `{ "word": "up", "nth": 2 }` that resolve to a time.
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
- Effects beyond EQ (reverb, flanger; echo/delay moved to M5)
- Key shifting at the segment level
- Plan diffing and remixing
- Web UI for plan visualization
- Real-time stem separation (currently offline-only)
- Real-time tempo control (planned tempo changes move to R2's tempo map; this means changing tempo live)

These are not on the v0.x roadmap but are good ideas for v1.x.
