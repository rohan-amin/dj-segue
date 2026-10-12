# dj-segue Plan Schema — v0.10

**Status:** v0.10 — additive over v0.9 (see [Changes from v0.9](#changes-from-v09)); v0.1–v0.9 plans remain valid. Schema is versioned (`schema_version` field). Breaking changes bump the major version; additive changes bump the minor.

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

Anything that happens, happens at a **deterministic mix-time** computable at compile time. There are no event-driven triggers. `{"after": id}` positions (v0.3) *look* like triggers ("when the loop ends") but resolve at compile time like everything else.

---

## Time and position

### Mix-time
The master clock. Mix-beat 0 is the moment audio output begins. The mix has a tempo, set either explicitly in `meta.mix_tempo` or implicitly by the first track on the timeline.

The tempo can change partway (v0.9, `meta.tempo_map`): it steps to a new tempo, or ramps to it over some beats. Mix beats keep counting through every change, so "mix beat 200" is wherever the 200th beat falls with the tempo as it is by then.

### Track-time
Each track has its own internal time, in beats. Independent of mix-time.

Track beats are **grid-relative**. The preprocessor fits each track a fixed beat grid, `t(beat) = anchor_sec + beat × 60 / bpm`:

- `bpm` is the track's detected tempo, or its declared `bpm` when the plan gives one.
- `anchor_sec` is **beat 0: the track's first downbeat** (a bar's "1") at or after the start of the file, with 20 ms tolerance so a downbeat right at the start is beat 0. So beats 0, 4, 8, … are downbeats, and `bar` N = beat 4N.
- The beat phase is fitted to onset energy, with the kick/bass band deciding beat vs. off-beat. Downbeats come from the beat_this model (optional install). Without it, beat 0 is the first grid beat and may be 1–3 beats off a real "1"; shift positions by whole beats to line up phrases.
- Detected tempos snap to a musical value (whole, ½, ⅓, ¼ BPM) when that drifts less than 10 ms over the track.
- `bar` positions and beat/bar cues follow the same grid (4/4).
- `second` positions (and `second` cues) are absolute file time and are never shifted.
- Audio with no detectable beat gets anchor 0 (beat 0 == sample 0) and must declare `bpm`.

### Position specifiers
Anywhere a position is needed, the schema accepts one of:

```jsonc
{ "beat": 64 }              // beat 64 (of mix or track depending on context)
{ "bar":  16 }              // bar 16 (= beat 64 in 4/4)
{ "second": 102.34 }        // wall-clock seconds, used for non-musical positions
{ "cue": "bottom_word" }    // a named cue from the track registry (track context only)
{ "after": "a_loop" }       // v0.3: when segment "a_loop" ends (mix context only)
{ "after": "a_loop", "offset": { "beats": -4 } }   // …4 beats before it ends
{ "word": "know", "nth": 3 }                       // v0.10: the 3rd time "know" is sung (track context)
{ "word": "know", "nth": 2, "in": "cil" }          // …when segment "cil" plays its 2nd "know" (mix context)
```

`after` names a segment `id` (see *Timeline*) and resolves to that segment's mix-time end: a `play`/`loop`/`silence` segment's last sample, a transition's `start_at + duration` (with `out` / `in` overrides, the later side's end). `offset` is a duration and may be negative. Tie positions to segments this way instead of adding up beats by hand: change a loop's repetitions and everything placed `after` it moves with it. In the timeline, `after` must name an **earlier** segment; automation keyframes may name any segment.

### Word positions (v0.10)

Where a word is sung, from the track's word timing (`dj-segue words`: lyrics aligned to the vocals stem, checked against Whisper). The preprocessor loads it for every track a plan's word positions need, timing the track first if it hasn't been.

```jsonc
{ "word": "know" }                                  // the first time "know" is sung
{ "word": "know", "nth": -1 }                       // the last time
{ "word": "bottoms up", "edge": "end" }             // a phrase: its words in a row; "end" = where its last word ends
{ "word": "know", "nth": 3, "offset": { "beats": -8 } }   // 8 beats before it
{ "word": "know", "nth": 2, "in": "cil" }           // mix context: when play segment "cil" sings it
```

- `word`: a word or phrase. Matching ignores case and punctuation ("Don't" = "dont").
- `nth`: which time it's sung, from 1; negative counts from the end. Default 1. Counted over the lyric words (Whisper-only ad-libs don't count); with no lyrics found, over Whisper's words.
- `edge`: `"start"` (default) or `"end"` of the word (of a phrase's last word).
- `offset` (optional): a duration from the word, may be negative — track beats/bars at the track's tempo in a track position, mix beats/bars in a mix position.
- `in`: mix positions only, and required there: the `id` of a **play** segment. The position is when that segment plays the word, through its start, `from`, stretch and any tempo changes. The word must lie within the part of the track the segment plays (the offset may take the position outside it). In the timeline it must name an earlier segment; keyframes and `swap_at` may name any.
- Allowed in track positions (`from`/`to`), `start_at`, keyframe `at` and a vocal_handoff's `swap_at` — not in `meta.tempo_map` (where words land depends on the tempo).

Position context is determined by where the position appears in the schema. In a `timeline.segment.from`, the position refers to the *track*. In a `timeline.segment.start_at`, it refers to the *mix*. The schema validator enforces this.

### Durations
Durations use the same flat scalar with a unit suffix:

```jsonc
{ "beats": 4 }              // preferred; portable across BPMs
{ "bars": 1 }
{ "seconds": 1.875 }
```

Beats are strongly preferred. Seconds are an escape hatch.

A mix-time duration counts mix beats from where it starts (v0.9), so through a tempo change 16 beats is 16 beats, some at each tempo. Negative durations (an `after` position's offset) count back the same way.

---

## Top-level structure

```jsonc
{
  "schema_version": "0.4",

  "meta": {
    "mix_name": "Bottom Wordplay Demo",
    "author": "rohan",
    "source_prompt": "mix Started From The Bottom into Middle Child via wordplay on 'bottom'",
    "created_at": "2026-04-25T01:00:00Z",
    "mix_tempo": 86,             // optional; default = BPM (declared or detected) of the first track on the timeline
    "tempo_map": [               // optional (v0.9): tempo changes, in time order
      { "at": { "beat": 128 }, "bpm": 92, "ramp": { "beats": 16 } },  // ramp 86 → 92 over beats 128–144
      { "at": { "after": "echo" }, "bpm": 74 }                          // step to 74 where the echo out ends
    ],
    "target_executor": "native"  // "native" | "mixxx"; informational only
  },

  "tracks": { /* see Tracks */ },
  "decks":  { /* see Decks */ },
  "timeline": [ /* see Timeline */ ],
  "automation": [ /* see Automation */ ]
}
```

### Tempo map (v0.9)

`meta.tempo_map` lists changes to the mix tempo. `mix_tempo` (or its default) is the tempo before the first one.

- `at`: a mix position (beat, bar, second or `after`) where the change starts. Each change's position is counted with the earlier changes in place.
- `bpm`: the tempo after the change.
- `ramp` (optional): a duration. Without it the tempo steps to `bpm` at `at`; with it, the tempo moves from the previous tempo to `bpm`, starting at `at` and lasting `ramp`. In beats, that's mix beats counted through the ramp; or give seconds.

A ramp is linear in **time**, like moving a pitch fader at a steady speed: a ramp from 86 to 92 bpm lasting 16 beats takes 120 × 16 / (86 + 92) ≈ 10.79 s.

What follows the tempo:

- **Positions and durations** in mix beats or bars (everywhere: `start_at`, keyframes, transition durations and offsets, `after` offsets).
- **`target_bpm: "mix"` segments.** A segment that crosses a change follows it: its track beats advance with the mix's beats, so it speeds up through a ramp, and switches speed at a step, staying beatmatched. One that sits wholly at one tempo is time-stretched to that tempo. Loops too: every repetition starts on its mix beat.
- Not a numeric `target_bpm` or a segment without one: those play at a fixed tempo whatever the mix does.
- An `echo_out`'s repeats keep the outgoing tempo: `echo.every` is measured at the tempo just before the cut, even when the mix changes tempo on the cut. Its tail (`duration`) counts mix beats from the cut.

Typical uses: an echo out into a slower track, with a step where the echo starts so the new track plays at its own tempo (`target_bpm: "mix"`); or a ramp over a blend, to walk the mix from one track's tempo to the next.

```jsonc
"meta": { "mix_name": "…", "mix_tempo": 91.93,
          "tempo_map": [{ "at": { "after": "likeem_end" }, "bpm": 74 }] },
…
{ "type": "play", "deck": 3, "track": "bottomsup", "from": { "beat": 0 }, "to": { "beat": 296 },
  "start_at": { "after": "likeem_end" }, "target_bpm": "mix" }   // 74 from here on
```

An `after` position mustn't depend on the change itself: if applying the change would move it, the plan is rejected. In practice that only happens with an offset in seconds back into a `"mix"` segment that the change slows or speeds up (the segment's end moves, and with it the change). `{"after": id}` itself, or an offset in beats (which counts back through the new tempo), is fine.

---

## Tracks

Top-level dict, keyed by an arbitrary handle (used everywhere else in the plan).

```jsonc
"tracks": {
  "started": {
    // Either single-source...
    "path": "audio/started_from_the_bottom.mp3",
    // ...or your own stems (audio files, or .npy arrays at 44.1 kHz):
    "stems": {
      "vocals": "audio/started/vocals.npy",
      "drums":  "audio/started/drums.npy",
      "bass":   "audio/started/bass.npy",
      "other":  "audio/started/other.npy"
    },

    "bpm": 86,                   // optional: omit to auto-detect
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
- **Stems (v0.8):** a `path` track has the four stems `vocals`, `drums`, `bass` and `other` whenever the plan mixes it by stems — the preprocessor separates them (demucs `htdemucs_ft`; optional install `pip install -e '.[stems]'`) and caches them. `other` is the track minus the other three, so the four sum back to the track. A track with explicit `stems` has exactly those (their sum is the track).
- A deck is **mixed by stems** if it has a `stem_volume` lane, an `eq` lane with a `stem`, or is either side of a `vocal_handoff`. Every track on such a deck must offer the same stems. Other decks play their tracks whole, as before.
- `bpm` is **optional** (v0.2). Omitted → the preprocessor detects it (the normal case). A number overrides detection and is used exactly, which is how you fix a misdetected track (e.g. half/double tempo). The beat phase is detected either way. A track with no `bpm` and no detectable beat is an error at preprocess time.
- `key` is optional, used for compatibility checks and key-shifting decisions.
- Cue handles are local to the track. `started.bottom_word` and `middle.bottom_word` don't collide.
- Cue positions are track-relative.
- **Cues marked in annotate (v0.10):** a `{ "cue": name }` that the plan doesn't declare is looked up in the track's annotation (`annotations/<audio file name>.json`, for that exact file), where `dj-segue annotate` saves the cues you mark (`m`). Those are stored as exact seconds, so moving the annotation's grid never moves them. A cue declared in the plan wins over a marked one of the same name.

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
  "start_at": { "beat": 0 },     // mix position; defaults to "immediately after previous segment on this deck"
  "target_bpm": 86               // optional (v0.2): play at this tempo
}
```

If `start_at` is omitted, the segment begins immediately after the previous segment on the same deck (or at mix-beat 0 if first).

`target_bpm` (v0.2) time-stretches the segment so the track plays at that tempo, pitch preserved. The playback rate is `target_bpm / track bpm`, so a segment covering N track-beats lasts N beats at `target_bpm` in the mix. `"target_bpm": "mix"` (v0.3) means the mix tempo — the usual way to beatmatch a track; with a tempo map (v0.9) the segment follows the tempo as it changes, so N track beats last N mix beats. Omitted → the track plays at its natural tempo.

Any segment may carry an `"id"` (v0.3), unique within the plan, for `after` positions to reference.

#### `loop` — repeat a region of a track (v0.3)
```jsonc
{
  "type": "loop",
  "id": "a_loop",                // optional
  "deck": 1,
  "track": "starships",
  "from": { "beat": 108 },       // loop start (track position); fixed for the whole loop
  "start_at": { "beat": 8 },     // mix position; same default as play
  "target_bpm": "mix",           // optional; same as play
  "schedule": [                  // one or more steps, played in order
    { "length": { "beats": 4 },   "repetitions": 2 },
    { "length": { "beats": 2 },   "repetitions": 2 },
    { "length": { "beats": 1 },   "repetitions": 2 },
    { "length": { "beats": 0.5 }, "repetitions": 2 }
  ]
}
```

Each step plays the first `length` of the loop `repetitions` times; the start stays put and the end moves, so a shrinking schedule is a tightening loop (a single step is a plain loop). `length` is track-time (beats at the track's tempo, or seconds); with `target_bpm` the loop is stretched like a play segment, so a 4-beat loop at `"mix"` lasts 4 mix beats. The segment lasts the sum of length × repetitions (20 beats above), and the next segment on the deck starts where it ends — to continue the track after the loop, add a `play` from the loop start plus its last length (or anywhere else).

Every repetition starts on its exact mix sample (computed from the start, not accumulated). Executors must make the jump back to `from` click-free without moving the loop's first beat; the native engine does a 3 ms crossfade that ends exactly on each boundary.

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

A transition only changes gain. It doesn't start or stop playback, so the decks' `play` segments must cover the transition window. A deck that is the target of a transition is silent until that transition starts.
```jsonc
{
  "type": "transition",
  "style": "crossfade",          // "crossfade" | "cut" | "vocal_handoff" (v0.8) | "echo_out" (v0.6)
  "from_deck": 1,
  "to_deck":   2,
  "start_at":  { "beat": 128 },  // mix position
  "duration":  { "beats": 4 }
}
```

A crossfade can be shaped (v0.4):

```jsonc
{
  "type": "transition",
  "style": "crossfade",
  "from_deck": 1,
  "to_deck":   2,
  "start_at":  { "beat": 128 },
  "duration":  { "beats": 8 },
  "curve": "equal_power",                              // optional: both sides
  "out": { "curve": "exponential" },                   // optional: from_deck's fade-out
  "in":  { "offset": { "beats": 2 }, "duration": { "beats": 10 } }  // optional: to_deck's fade-in
}
```

- `curve`: `"equal_power"` (default), `"linear"` or `"exponential"` — the same shapes as automation interpolation (see *Keyframe rules*).
- `out` / `in` override one side. Each takes any of `offset` (a duration from `start_at`, may be negative), `duration` and `curve`; anything left out comes from the transition. Use them for an asymmetric handoff, e.g. the incoming track fading in slower than the outgoing one fades out.
- Only crossfades take `curve` / `out` / `in`.

A side's curve can also be drawn (v0.5) — a list of points instead of a name:

```jsonc
"in": { "curve": { "points": [[0, 0], [0.6, 0.08], [0.85, 0.5], [1, 1]], "smooth": true } }
```

- Each point is `[t, gain]`. `t` runs 0 → 1 across that side's fade window (strictly increasing; the first is 0, the last 1), so the shape stretches with the window. `gain` is that deck's actual volume, 0–1.
- An `out` curve starts at 1 and ends at 0; an `in` curve starts at 0 and ends at 1. In between it can go anywhere — swells, dips, chops.
- `smooth: false` (default) joins the points with straight lines; `true` draws a smooth curve through them (monotone cubic) that never overshoots them, so the volume never leaves 0–1.
- Points must be at least 2 ms apart in the mix, so the volume can't jump (a click).
- Drawn curves go on a side (`out.curve` / `in.curve`); the transition-level `curve` stays a name.

An echo out (v0.6) leaves a track without overlapping it — so the next one needn't be stretched to match:

```jsonc
{
  "type": "transition",
  "style": "echo_out",
  "from_deck": 2,
  "to_deck":   3,
  "start_at":  { "after": "likeem_end" },          // the dry cut — put it on a downbeat
  "duration":  { "beats": 4 },                     // the echo tail
  "echo": { "every": { "beats": 1 }, "feedback": 0.5, "highpass": 0.8 },  // highpass: v0.7, optional
  "in": { "offset": { "beats": -2 }, "duration": { "beats": 2 } }  // optional
}
```

- At `start_at` the from_deck's dry audio cuts. Its last `every` before the cut (what was audible, after its volume) then repeats every `every`, starting on the cut, so the repeats land on the mix grid.
- The first repeat is at full level; each one after is `feedback` (0–1) times the one before. The tail fades linearly to silence over its last `every`, ending at `start_at + duration`. `every` must be no longer than `duration`.
- `highpass` (v0.7, 0–1, default 0): the tail gets its own filter knob, moving in a straight line from 0 (off) at the cut to `highpass` at the tail's end — the echoes thin out as they fade. Same knob scale as the `filter` lane.
- The repeats ring on after the from_deck's `play` ends; the deck's segment only needs to cover the captured `every` before the cut.
- The to_deck comes in at full volume on the cut. `in` (offset, duration, curve — drawn curves too) gives it a fade instead; there, `duration` defaults to 0 (the cut), not the tail length.
- `curve` and `out` don't apply. An `after` naming an echo_out resolves to the end of its tail (or of its `in`, if later).
- The incoming track can play at its own tempo (no `target_bpm`), but the mix clock doesn't change unless the plan says so: later mix positions still count in mix beats. To make the incoming track's tempo the mix's, add a tempo change at the cut (v0.9, `meta.tempo_map`) and play it at `"target_bpm": "mix"`.

Transitions are *compiled* by the loader into deterministic per-deck volume automations and (where applicable) stem-level operations. The expansion is visible via `dj-segue inspect` for debugging.

- `crossfade`: by default an equal-power (sin/cos) fade over `duration`, so loudness doesn't dip mid-fade.
- `cut`: instantaneous at `start_at`; `duration` must be `{ "beats": 0 }`.
- `echo_out`: a cut on the from_deck plus the echo tail (rendered by the engine, not a gain envelope); the to_deck steps in at `start_at` or follows its `in`.
- `vocal_handoff`: per-stem gain envelopes (above); at deck level the to_deck switches on where the handoff starts and the from_deck off where it ends.
- Transitions touching the same deck must not overlap, and must alternate out/in per deck. With `out` / `in`, each deck is checked against its own side's window.

A vocal handoff (v0.8) is the wordplay transition: the instrumentals crossfade while the vocals switch on a single word.

```jsonc
{
  "type": "transition",
  "style": "vocal_handoff",
  "from_deck": 1,
  "to_deck":   2,
  "start_at":  { "beat": 120 },
  "duration":  { "beats": 8 },                 // the instrumentals' crossfade
  "swap_at":   { "offset": { "beats": 4 } },   // optional: vocals switch here (default: halfway)
  //   or on a word (v0.10): "swap_at": { "word": "know", "nth": 2, "in": "<incoming play segment id>" }
  "curve": "linear"                            // optional, as for crossfades (also `out` / `in`)
}
```

- The drums, bass and other stems of both decks crossfade over the window, shaped like a crossfade (`curve`, `out` / `in`, drawn curves).
- The vocals switch at `start_at + swap_at.offset`, or (v0.10) where a word position puts it — usually the incoming segment's copy of the shared word, so its voice takes over on that word: the outgoing deck's vocals stop and the incoming deck's start. They cross over 10 ms ending on the swap, so the incoming vocal's first word is intact and nothing clicks. `swap_at` must lie within `duration`.
- Outside the handoff the decks are whole: the incoming deck is silent until the handoff starts (its earliest fade or 10 ms before the swap), and the outgoing deck stops when it ends. An `after` naming the handoff resolves to its later end.
- Both decks are mixed by stems; every track on them needs a `vocals` stem (any `path` track has one).

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
    "interpolation": "linear"      // "linear" | "step" | "exponential" | "equal_power" (v0.3)
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
    "stem": "drums",                // optional (v0.8): EQ only this stem of the deck
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
| `eq`           | `deck`, `band` (+ `stem`, v0.8) | dB, –24 to +12 (per band)    |
| `filter` (v0.7)| `deck`                     | knob, –1 (low-pass) … 0 (off) … +1 (high-pass) |
| `crossfader`   | (none)                     | –1.0 (full deck 1) to +1.0 (full deck 2); equal-power law, centre = 0.707 each; decks 1–2 only; at most one lane |

On a deck mixed by stems the chain is: each stem's EQ (an `eq` lane with that `stem`) and volume (`stem_volume` lanes × any vocal handoff) → the stems summed → the deck's EQ → filter → deck volume, crossfader and transitions. Per-stem EQ uses the same isolator as the deck's.

`filter` (v0.7) is a DJ mixer's filter knob. Left of centre is a low-pass whose cutoff slides from 20 kHz (just left of 0) to 60 Hz (–1); right of centre a high-pass from 20 Hz to 10 kHz (+1). The cutoff is exponential in the knob, so a linear move is an even sweep. 12 dB/oct, slightly resonant (Q 0.9). At exactly 0 the deck is untouched; within ±0.05 it blends from dry into the filter, so sweeping through the centre doesn't click. It acts after the EQ and before the deck's volume. At most one lane per deck.

`eq` is a DJ isolator: the deck is split into low / mid / high bands at 200 Hz and 2.5 kHz (Linkwitz–Riley 24 dB/oct), and each lane sets one band's gain. All bands at 0 dB sounds the same as no EQ. At most one lane per deck and band. `linear` interpolation is linear in dB. A band's cut is deepest well inside its range; near a crossover the neighbouring band still passes some of the signal (e.g. a -24 dB mid cut leaves 1 kHz about 19 dB down).

### Keyframe rules

- `at` positions are **mix-time**.
- Keyframes within a lane must be strictly time-ordered.
- The first keyframe sets the starting value; the parameter holds that value for all mix-times before it.
- The last keyframe sets the final value; the parameter holds that value for all mix-times after it.
- `step` interpolation: value jumps at each keyframe.
- `linear` interpolation: linear ramp between keyframes.
- `equal_power` (v0.3): the crossfade transition's curve — rising follows sin, falling follows cos over the quarter circle. A rising and a falling equal-power ramp over the same window keep constant total power; give them different windows for an asymmetric crossfade (or use a crossfade transition's `out` / `in`, v0.4).
- `exponential`: constant ratio per unit time, i.e. linear in dB (natural-sounding volume fades). A 0 endpoint is treated as -60 dB, then snaps to exactly 0 at the keyframe. Ranges that cross zero (the crossfader) fall back to linear.

### Automated parameter values

Any keyframe `value` may itself be a constant or a reference to another automation lane (v0.2+). For v0.1, all keyframe values are constants.

---

## Validation rules (the inspector enforces these)

1. `schema_version` must match a supported version.
2. Every `track` referenced in the timeline must be declared in `tracks`.
3. Every `deck` referenced anywhere must be declared in `decks`.
4. Every `cue` reference must resolve in the relevant track's cue registry, or (v0.10) among the cues marked in its annotation — checked after preprocessing.
5. No two segments on the same deck overlap.
6. Track positions (`from`/`to` in `play` segments) must fall within the track's actual duration.
7. Keyframes within a lane are time-ordered.
8. A lane's `stem` (`stem_volume`, or `eq` with `stem`) must be a stem of every track on its deck: the track's own `stems`, or for a `path` track `vocals` / `drums` / `bass` / `other` (v0.8). Every track on a deck mixed by stems offers the same stems.
9. A `vocal_handoff` transition requires every track on both decks to have a `vocals` stem; its `swap_at` lies within `duration` (v0.8).
10. Segment `id`s are unique. An `after` position names an existing id — in the timeline, an earlier segment's.
11. Loop schedule lengths are positive; `repetitions` is a whole number ≥ 1; a loop's region (`from` + its longest length) lies within the track.
12. `after` is mix-time only; it's a parse error in a track position (`from`/`to`).
13. `curve`, `out` and `in` are crossfade-only (`in` also on `echo_out`); each crossfade side has positive duration.
14. A drawn curve's points start at t=0 and end at t=1, strictly increasing, gains 0–1, from 1 to 0 (`out`) or 0 to 1 (`in`), and at least 2 ms apart in the mix.
15. `echo_out` has `echo` (and only it does); its `duration` is positive, `echo.every` is positive and no longer than `duration`, and `echo.feedback` and `echo.highpass` are 0–1.
16. `eq` values are –24 to +12 dB, one lane per deck, band and stem; `filter` values are –1 to +1, one lane per deck.
17. `tempo_map` changes are in time order, each starting at or after the previous one ends (a ramp's end); `bpm` is positive, a `ramp` positive; `at` is a mix position (not a cue or a word), and an `after` there must not depend on the change itself.
18. Word positions (v0.10): `nth` ≠ 0; `in` only (and always) in mix positions, naming a play segment; after preprocessing, the word is sung at least `nth` times in the track, and a mix word lies within its segment's part of the track.

---

## Worked example

See `examples/hello_mix.plan.jsonc` for a minimal complete plan. It's also the v0.1 acceptance test — if the engine can play it, M1 is done.

---

## Changes from v0.1

- `track.bpm` is optional; omitted → detected by the preprocessor. A declared value overrides detection.
- `play.target_bpm` time-stretches a segment to a tempo.
- Clarified: track beats are counted on the fitted grid (see *Track-time*).
- Clarified transition, crossfader and exponential-interpolation semantics (implemented in M2; valid in v0.1 plans too).

A plan declaring `"schema_version": "0.1"` must give every track a `bpm` and may not use `target_bpm`.

## Changes from v0.2

- `loop` segment with a `schedule` of (length, repetitions) steps — tightening loops.
- Optional segment `id`, and `{"after": id, "offset"?: duration}` mix positions.
- `target_bpm: "mix"`.
- `equal_power` lane interpolation.
- Validation rules 10–12.

A plan declaring `"schema_version": "0.1"` or `"0.2"` may not use these.

## Changes from v0.3

- Crossfade `curve` (`equal_power` | `linear` | `exponential`).
- Per-side `out` / `in` overrides (`offset`, `duration`, `curve`).
- A transition's `after` anchor is its later side's end.
- Validation rule 13.

A plan declaring `"schema_version"` 0.1–0.3 may not use these.

## Changes from v0.4

- Drawn curves on a crossfade side: `curve: { "points": [[t, gain], …], "smooth"?: bool }`.
- Validation rule 14.

A plan declaring `"schema_version"` 0.1–0.4 may not use these. (`dj-segue tune` bumps the version when it saves a drawn curve.)

## Changes from v0.5

- `echo_out` transition style with `echo: { every, feedback }` and an optional `in`.
- Validation rules 13 (amended) and 15.

A plan declaring `"schema_version"` 0.1–0.5 may not use these.

## Changes from v0.6

- `filter` automation lane.
- `echo.highpass` on `echo_out`.
- Validation rules 15 (amended) and 16.

A plan declaring `"schema_version"` 0.1–0.6 may not use these.

## Changes from v0.7

- Stems: `path` tracks get `vocals` / `drums` / `bass` / `other` by separation when the plan mixes them by stems; `stem_volume` lanes and `vocal_handoff` are implemented.
- `eq` lanes take an optional `stem`.
- `vocal_handoff` takes `swap_at`, and `curve` / `out` / `in` like a crossfade.
- Validation rules 8, 9 and 16 amended.

A plan declaring `"schema_version"` 0.1–0.7 may not use `swap_at`, an eq `stem`, or a shaped vocal_handoff.

## Changes from v0.8

- `meta.tempo_map`: the mix tempo steps or ramps partway; mix beats count through it; `target_bpm: "mix"` segments (and loops) follow it.
- Mix-time durations count beats from where they start.
- `echo.every` is measured at the tempo before the cut.
- Validation rule 17.

A plan declaring `"schema_version"` 0.1–0.8 may not use `tempo_map`.

## Changes from v0.9

- Word positions `{"word", "nth", "edge", "offset"}`: in track positions, and with `in` (a play segment) in mix positions and a vocal_handoff's `swap_at`.
- The preprocessor loads word timing (`dj-segue words`) for the tracks word positions need.
- A cue the plan doesn't declare resolves from the cues marked in the track's annotation (rule 4 amended: before preprocessing such a cue passes).
- Validation rules 17 (amended) and 18.

A plan declaring `"schema_version"` 0.1–0.9 may not use word positions.

## Versioning policy

- v0.x: pre-stable; breaking changes allowed at minor bumps with migration notes.
- v1.0: stable; breaking changes require major bump and migration tooling.

Every plan file must declare `schema_version`. The loader rejects unknown versions with a clear error.

---

## What's deliberately *not* in v0.1

These are deferred to later versions to keep v0.1 small and shippable:

- **Continuous loop-length automation** (e.g. a loop that shrinks smoothly). v0.3 loops change length in steps via `schedule`, which keeps segment timing decided by the timeline alone.
- **Effects beyond EQ** (v0.3). Filters, delay, reverb, flanger. (v0.6 adds one fixed use of delay: `echo_out`.)
- **Live triggers / reactive mode** (v0.5+). "Fire when deck B reaches energy threshold X."
- **Key shifting** (v0.3). Will go in `play` segments as `key_shift_semitones`.
- **Stem strategies in transitions beyond `vocal_handoff`** (v0.3).
- **Auto-generated transitions** (planner concern, not schema).
- **Master effects bus and recording-to-file output** (executor concern, not schema, but worth noting).

The schema version field gives us room to grow into these without breaking existing plans.
