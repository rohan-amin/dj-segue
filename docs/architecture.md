# dj-segue Architecture

This document records architectural decisions that have been *settled*. Don't relitigate these without good reason; if you find yourself wanting to, add a section "Open questions" at the bottom and we'll discuss before changing the doc.

---

## Mission

dj-segue is an AI-driven DJ system that performs **wordplay transitions** — finding and executing transitions between songs based on lyrical content, not just BPM and key. The core thesis: an LLM with access to time-aligned lyrics, audio analysis, and a structured plan format can identify and execute wordplay transitions that human DJs find clever.

Most of the value is in the **planning intelligence**, not the audio engine. The audio engine exists to make plans audible with high fidelity and zero friction.

---

## Layers

```
┌──────────────────────────────────────────────┐
│ Intent (English, free-form)                  │  human input or LLM seed
└──────────────────────────────────────────────┘
                  │
                  ▼  [planner]
┌──────────────────────────────────────────────┐
│ Plan (JSONC, score-style)                    │  source of truth, versioned, hand-editable
└──────────────────────────────────────────────┘
                  │
                  ▼  [preprocessor]
┌──────────────────────────────────────────────┐
│ Resolved plan + cached track analysis        │  stem files, beat grids, keys, etc.
└──────────────────────────────────────────────┘
                  │
                  ▼  [executor]
┌──────────────────────────────────────────────┐
│ Sample-accurate audio output (or WAV file)   │
└──────────────────────────────────────────────┘
```

Each layer has a clean interface. Each is independently testable. The plan format is the abstraction barrier that makes executor pluggability work.

---

## Settled decisions

### Plan format is score-style, not gesture-style.

A plan describes *what the listener hears*, not *what a DJ's fingers do*. You can read a plan and answer "what's audible at mix-beat 200" by inspection.

Anti-pattern (gesture-style, what we are *not* doing):
> "press load, press play, at beat 104 press bpm-match, when first loop ends start crossfade…"

This is a state machine the executor must simulate to know audio outcomes. Debugging requires mental simulation. Hard to inspect, hard to test.

Score-style (what we *are* doing):
> "deck 1 plays track A from beat 0 to beat 200; deck 1 volume = 1.0 from beat 0 to 196 then ramps to 0 by beat 200; deck 2 plays track B starting at mix-beat 196…"

The executor compiles this to a sample-accurate event list at load time. No runtime mystery.

### Single source of authoritative time.

One clock: the audio output stream's sample counter. Mix-time = `samples_rendered / sample_rate`. Each deck's playhead is derived from mix-time and the deck's start position + tempo.

No `time.time()`, no `time.monotonic()`, no separate "beat manager" clocks anywhere in the engine's timing decisions. Wall-clock time is for log timestamps only.

### Lock-free audio callback.

The audio callback path holds no locks, allocates no memory, performs no I/O, emits no log lines. State the callback reads is either immutable for the buffer's duration or written by one thread and read by one thread via a single-producer/single-consumer queue.

Locks live on the control thread. If we ever need cross-thread state communication into the callback, it goes through a lock-free ring buffer or atomic — never a mutex.

### Preprocess before play. No surprises during playback.

Before any audio plays, every track in the plan must have:
- BPM and beat grid (`<track>.beats` JSON cache)
- Detected key (`<track>.beats` JSON cache, same file)
- Stems separated and saved as `.npy` (if stems are referenced)
- Cue points: declared in the plan, or marked in the track's annotation (`annotations/<track>.json`, `dj-segue annotate`)
- Word timing, for plans with word positions (`.cache/words/<file sha1>/`: LRCLIB lyrics aligned to the vocals stem, checked against Whisper)

The preprocessor populates caches if missing; the player verifies they exist and aborts cleanly if not. No `time.sleep(0.3)` waits for analysis to complete.

### Executor pluggability.

Two executor implementations target the same plan format:

- **Native** (default): a Python-side audio engine using `sounddevice`, Rubber Band, `pedalboard`. Sample-accurate, supports headless WAV output for testing. Owns the v0.1 milestone.
- **Mixxx** (fallback): the existing MIDI-bridge executor, kept for cross-validation. Loses precision for sub-beat operations but useful as a sanity check.

Plans are engine-agnostic. The executor abstract base class lives in `src/dj_segue/executor/base.py` and pins the interface.

### Deterministic; no live triggers in v0.1.

Every event resolves to a sample-accurate mix-time at compile time. There are no `on_loop_complete` or `on_deck_beat` triggers. If you need branching, write multiple plans and choose between them at the planner layer.

(Reactive mode is a v0.5+ feature with its own design pass.)

### Headless-first.

The engine must support rendering a plan to a WAV file without using a real audio device. This is non-negotiable; it's what makes the engine testable. Live audio output is one mode of the executor; offline rendering is another.

### Real-time: one engine, block by block; live == offline.  (settled 2026-10-04)

Live playback computes the mix a few hundred ms ahead of the speakers. Every stage of the native engine — deck sources (time-stretch, seams, loops), deck chains (stems, EQ, filter, volume), echo tails, the limiter — carries its state between blocks, so running it block by block (live) and over the whole mix at once (offline) gives **bitwise identical** audio, whatever the block sizes. Tests enforce this.

- Time-stretching uses the Rubber Band **library** in real-time mode (R3 "finer" engine) through ctypes, fed in pieces the stretcher asks for, so its output doesn't depend on how it's read. Onset accuracy ≈ the offline CLI it replaced (≤ 0.3 ms); ~25× real time per stereo stream. The stretch cache is gone (not needed at that speed).
- `play` runs a producer thread that keeps a lock-free SPSC ring buffer ~0.4 s ahead; the sounddevice callback only copies out of it.
- Plans can change while playing (`play --watch`): a new engine for the new plan starts ~0.3 s past what's queued (warmed up from 1 s earlier) and takes over with a 20 ms crossfade. Starting part-way (a seek, a reload) sounds right but isn't bit-identical to playing from the top (stretchers and filters restart; spectra match within ~0.5 dB after a beat, beats land within 0.4 ms).
- Measured: the Fancy → Like 'em All → Bottoms Up mix runs 18× real time; with two decks mixed by stems, 5×.
- The mix tempo can change (schema v0.9 tempo map, 2026-10-04): a `"mix"` segment across a change is stretched with a varying ratio, set before each piece of input from the rate about the stretcher's start delay back (Rubber Band applies a new ratio to audio it already holds), with pieces ending on tempo steps. Beats: ≤ 1.2 ms after a step, ≤ 0.6 ms through a ramp (44.1 kHz). Still deterministic and read-size independent.
- The tune page plays through the same engine (R2, 2026-10-04): the server owns the sound device; the page sends play / stop / loop and polls the playhead; each validated edit takes over like a `--watch` reload, and a jump (seek, loop) is the same crossfade into an engine started elsewhere.

---

## Module layout

```
src/dj_segue/
├── schema/
│   ├── plan.py          # pydantic models for the plan format
│   ├── validator.py     # extra validation beyond pydantic (overlap checks, etc.)
│   └── version.py       # schema version constants and migration hooks
├── analyzer/
│   ├── beat.py          # BPM + beat grid (librosa or essentia, TBD)
│   ├── key.py           # key detection
│   └── cache.py         # .beats / .cue file I/O
├── stems/
│   └── separator.py     # demucs wrapper
├── preprocessor/
│   └── pipeline.py      # given a plan, ensure all caches are warm
├── executor/
│   ├── base.py          # MixExecutor abstract base class
│   ├── native/
│   │   ├── engine.py    # the audio engine
│   │   ├── deck.py      # per-deck playback state
│   │   ├── mixer.py     # multi-deck stem-aware mixing
│   │   ├── eq.py        # 3-band EQ (port of professional_eq.py)
│   │   └── ringbuffer.py# lock-free ring buffer (port of ring_buffer.py)
│   └── mixxx/
│       └── bridge.py    # MIDI bridge to Mixxx (later milestone)
├── annotate/            # track annotations (sections, key, cues, wordplay spots) + the annotate page
├── words/               # word timing: lrclib, align (wav2vec2), transcribe (Whisper), merge
├── tune/                # the tune page (transitions by ear; live playback)
├── scrub/               # the scrub page (find beats by ear)
├── prepare.py           # a new track ready in one step (beats, stems, words, annotation)
├── finder.py            # wordplay candidates between two tracks (first piece of the planner)
├── templates.py         # a finder candidate → a plan (loop and jump)
├── inspect/
│   └── pretty.py        # human-readable plan summaries
├── cli/
│   └── main.py          # `dj-segue plan|inspect|preprocess|play`
└── planner/
    └── ...              # M6+; LLM-driven plan generation

tests/
├── plans/               # golden plan files
├── audio/               # tiny test audio (1-2s clips)
├── golden/              # rendered WAVs for regression
└── ...

examples/
├── hello_mix.plan.jsonc
└── ...
```

---

## Development discipline

### Testing strategy

- **Unit tests** for analyzers, schema validation, plan-to-event compilation.
- **Integration tests** that render plans to WAV files and assert on output (sample counts, RMS levels, expected silence ranges, etc.). The `tests/golden/` dir stores reference WAVs; tests assert byte-identical or perceptually-close (configurable per test).
- **No tests that require a sound card.** `pytest` runs in CI on a headless box.

### Logging

- `logging` standard lib, namespace `dj_segue.<module>`.
- Levels: DEBUG for engine internals; INFO for user-facing milestones; WARNING for recoverable issues; ERROR for failures.
- **No logging in the audio callback.** Callback writes to a debug ring buffer if needed; control thread drains it.

### Dependencies

Pinned in `pyproject.toml`. Approximate set:

| Use case             | Library                    |
|----------------------|----------------------------|
| Audio I/O            | `sounddevice`              |
| File decode          | `soundfile`                |
| Analysis             | `librosa` (M1), maybe `essentia` later |
| Time-stretch         | Rubber Band library (system install), streaming, via ctypes |
| Effects (EQ/filter)  | `pedalboard` (Spotify) and/or `scipy.signal` |
| Stem separation      | `demucs` (optional, behind a flag) |
| Word timing          | `faster-whisper`, `mutagen`, `pronouncing` (optional `words` extra); alignment via `torchaudio` |
| Schema validation    | `pydantic` v2             |
| CLI                  | `typer`                    |
| Tests                | `pytest`, `pytest-asyncio`|

### Python version

Python 3.11+. We use match statements, `typing.Self`, modern type hints throughout.

---

## Open questions (revisit before locking)

**Memory.** Offline, every deck (and stem) is held full-length; the live engine also keeps full-length mix buffers. Fine for a 10-minute mix; a long set would want rolling buffers.
