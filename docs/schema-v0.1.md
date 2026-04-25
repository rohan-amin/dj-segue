# dj-segue Plan Schema — v0.1

**Status:** v0.1 — initial release. Schema is versioned (`schema_version` field). Breaking changes bump the major version; additive changes bump the minor.

**Format:** JSONC (JSON with comments) for source files. Strict JSON for any tooling that needs the spec.

---

## Mental model

A **plan** is a *score*, not a sequence of button presses. It describes the audio that should result, not the gestures that produce it. You can read a plan top-to-bottom and know what the listener hears at any mix-beat.

A plan has four sections:

- `meta` — identity, schema version, optional source description.
- `tracks` — registry of audio sources used in the mix, with metadata and named cue points.
- `decks` — declares the playback channels needed (1 to N).
- `timeline` — what each deck does over the course of the mix.
- `automation` — parameter changes over time (volume, EQ, filter, loop length, etc.).

Anything that happens, happens at a **deterministic mix-time** computable at compile time. There are no event-driven triggers in v0.1. If you find yourself wanting "fire when X happens," that's a v0.2+ feature; for now, everything resolves to a position on the master timeline.

---

## Time and position

### Mix-time
The master clock. Mix-beat 0 is the moment audio output begins. The mix has a tempo, set either explicitly in `meta.mix_tempo` or implicitly by the first track on the timeline.

### Track-time
Each track has its own internal time, in beats from the start of the track file. Independent of mix-time.

### Position specifiers
Anywhere a position is needed, the schema accepts one of:

```jsonc
{ "beat": 64 }              // beat 64 (of mix or track depending on context)
{ "bar":  16 }              // bar 16 (= beat 64 in 4/4)
{ "second": 102.34 }        // wall-clock seconds, used for non-musical positions
{ "cue": "bottom_word" }    // a named cue from the track registry (track context only)
```

Position context is determined by where the position appears in the schema. In a `timeline.segment.from`, the position refers to the *track*. In a `timeline.segment.start_at`, it refers to the *mix*. The schema validator enforces this.

### Durations
Durations use the same flat scalar with a unit suffix:

```jsonc
{ "beats": 4 }              // preferred; portable across BPMs
{ "bars": 1 }
{ "seconds": 1.875 }
```

Beats are strongly preferred. Seconds are an escape hatch.

---

## Top-level structure

```jsonc
{
  "schema_version": "0.1",

  "meta": {
    "mix_name": "Bottom Wordplay Demo",
    "author": "rohan",
    "source_prompt": "mix Started From The Bottom into Middle Child via wordplay on 'bottom'",
    "created_at": "2026-04-25T01:00:00Z",
    "mix_tempo": 86,             // optional; default = first track's BPM
    "target_executor": "native"  // "native" | "mixxx"; informational only
  },

  "tracks": { /* see Tracks */ },
  "decks":  { /* see Decks */ },
  "timeline": [ /* see Timeline */ ],
  "automation": [ /* see Automation */ ]
}
```

---

## Tracks

Top-level dict, keyed by an arbitrary handle (used everywhere else in the plan).

```jsonc
"tracks": {
  "started": {
    // Either single-source...
    "path": "audio/started_from_the_bottom.mp3",
    // ...or stem-based:
    "stems": {
      "vocals": "audio/started/vocals.npy",
      "drums":  "audio/started/drums.npy",
      "bass":   "audio/started/bass.npy",
      "other":  "audio/started/other.npy"
    },

    "bpm": 86,
    "key": "F#m",

    "cues": {
      "intro_drop":  { "beat": 32 },
      "first_verse": { "beat": 64 },
      "bottom_word": { "second": 102.34, "label": "bottom" },
      "outro":       { "bar": 64 }
    }
  }
}
```

**Rules:**
- Exactly one of `path` or `stems` must be present.
- `path` is shorthand for `{ "stems": { "full": "<path>" } }`.
- `bpm` is mandatory in v0.1 (the analyzer fills it; users rarely write it by hand).
- `key` is optional, used for compatibility checks and key-shifting decisions.
- Cue handles are local to the track. `started.bottom_word` and `middle.bottom_word` don't collide.
- Cue positions are track-relative.

---

## Decks

Top-level dict declaring playback channels. Decks are identified by integer keys starting at 1.

```jsonc
"decks": {
  "1": { "label": "main_a" },        // optional human-readable label
  "2": { "label": "main_b" }
}
```

v0.1 supports 1–4 decks. The executor must validate that all decks referenced in `timeline` are declared here.

---

## Timeline

An ordered list of segments. Each segment describes what one deck does over a span of mix-time. Segments on different decks may overlap; segments on the same deck must not overlap.

### Segment types

#### `play` — a track plays on a deck
```jsonc
{
  "type": "play",
  "deck": 1,
  "track": "started",
  "from": "intro_drop",          // track position; or { "beat": 32 }
  "to":   "bottom_word",
  "start_at": { "beat": 0 }      // mix position; defaults to "immediately after previous segment on this deck"
}
```

If `start_at` is omitted, the segment begins immediately after the previous segment on the same deck (or at mix-beat 0 if first).

#### `silence` — a deck is silent
```jsonc
{
  "type": "silence",
  "deck": 1,
  "duration": { "beats": 4 }
}
```

Used to pad a deck's timeline. Rarely needed; gaps in a deck's segment list imply silence.

#### `transition` — high-level sugar for a multi-deck handoff
```jsonc
{
  "type": "transition",
  "style": "crossfade",          // "crossfade" | "cut" | "vocal_handoff"
  "from_deck": 1,
  "to_deck":   2,
  "start_at":  { "beat": 128 },  // mix position
  "duration":  { "beats": 4 }
}
```

Transitions are *compiled* by the loader into deterministic per-deck volume automations and (where applicable) stem-level operations. The expansion is visible via `dj-segue inspect` for debugging.

For `style: "vocal_handoff"` (stem-aware), tracks must have stems available; the compiler swaps vocal stems at the boundary while crossfading other stems over `duration`.

---

## Automation

Time-varying parameter changes. A flat list of *lanes*, each targeting one parameter on one deck (and optionally one stem).

```jsonc
"automation": [
  {
    "lane": "deck_volume",
    "deck": 1,
    "keyframes": [
      { "at": { "beat": 124 }, "value": 1.0 },
      { "at": { "beat": 128 }, "value": 0.0 }
    ],
    "interpolation": "linear"      // "linear" | "step" | "exponential"
  },
  {
    "lane": "stem_volume",
    "deck": 1,
    "stem": "vocals",
    "keyframes": [
      { "at": { "beat": 100 }, "value": 1.0 },
      { "at": { "beat": 104 }, "value": 0.0 }
    ],
    "interpolation": "linear"
  },
  {
    "lane": "eq",
    "deck": 1,
    "band": "low",                  // "low" | "mid" | "high"
    "keyframes": [
      { "at": { "beat": 120 }, "value_db":  0 },
      { "at": { "beat": 124 }, "value_db": -24 }
    ],
    "interpolation": "linear"
  }
]
```

### Lane types (v0.1)

| `lane`         | Required fields            | Value semantics                   |
|----------------|----------------------------|-----------------------------------|
| `deck_volume`  | `deck`                     | linear gain, 0.0–1.0              |
| `stem_volume`  | `deck`, `stem`             | linear gain, 0.0–1.0              |
| `eq`           | `deck`, `band`             | dB, –24 to +12 (per band)         |
| `crossfader`   | (none)                     | –1.0 (full deck 1) to +1.0 (full deck 2) |

### Keyframe rules

- `at` positions are **mix-time**.
- Keyframes within a lane must be strictly time-ordered.
- The first keyframe sets the starting value; the parameter holds that value for all mix-times before it.
- The last keyframe sets the final value; the parameter holds that value for all mix-times after it.
- `step` interpolation: value jumps at each keyframe.
- `linear` interpolation: linear ramp between keyframes.
- `exponential`: exponential ramp (useful for volume fades that sound natural).

### Automated parameter values

Any keyframe `value` may itself be a constant or a reference to another automation lane (v0.2+). For v0.1, all keyframe values are constants.

---

## Validation rules (the inspector enforces these)

1. `schema_version` must match a supported version.
2. Every `track` referenced in the timeline must be declared in `tracks`.
3. Every `deck` referenced anywhere must be declared in `decks`.
4. Every `cue` reference must resolve in the relevant track's cue registry.
5. No two segments on the same deck overlap.
6. Track positions (`from`/`to` in `play` segments) must fall within the track's actual duration.
7. Keyframes within a lane are time-ordered.
8. `stem` references must exist in the track's `stems` dict.
9. A `vocal_handoff` transition requires both source and target tracks to have a `vocals` stem.

---

## Worked example

See `examples/hello_mix.plan.jsonc` for a minimal complete plan. It's also the v0.1 acceptance test — if the engine can play it, M1 is done.

---

## Versioning policy

- v0.x: pre-stable; breaking changes allowed at minor bumps with migration notes.
- v1.0: stable; breaking changes require major bump and migration tooling.

Every plan file must declare `schema_version`. The loader rejects unknown versions with a clear error.

---

## What's deliberately *not* in v0.1

These are deferred to later versions to keep v0.1 small and shippable:

- **Loops** (v0.2). Tightening loops require length-as-automation, which requires lanes that can target structural parameters. Real, but not yet.
- **Effects beyond EQ** (v0.3). Filters, delay, reverb, flanger.
- **Live triggers / reactive mode** (v0.5+). "Fire when deck B reaches energy threshold X."
- **Key shifting** (v0.3). Will go in `play` segments as `key_shift_semitones`.
- **Stem strategies in transitions beyond `vocal_handoff`** (v0.3).
- **Auto-generated transitions** (planner concern, not schema).
- **Master effects bus and recording-to-file output** (executor concern, not schema, but worth noting).

The schema version field gives us room to grow into these without breaking existing plans.
