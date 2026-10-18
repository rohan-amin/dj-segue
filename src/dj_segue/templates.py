"""Plan templates (M6): a wordplay candidate from the finder → a plan.

**Loop and jump** (the design of examples/lgis_everybody.plan.jsonc):

  1. A plays from a section start some bars before its word, up to the beat
     the word is on (at or just before it).
  2. A loops a bar from that beat — a musical loop with the word at its
     head, not a stutter — dying away while B's bed (B with its vocals down)
     crossfades in over it.
  3. B jumps to its word (or the start of its line, when the word comes
     within a bar of it) as the loop ends, its vocals back up: A's word
     repeating, then B singing it — the pair heard back to back, and no
     vocal cut mid-line.

B's bed is its first K beats, K ≡ B's entry beat (mod 4), so the jump keeps
B's bars in step; when B's entry is 8–16 beats into the track, B simply
plays from the top to it (no jump). Tempo: within 4%, the whole mix runs at
B's tempo; otherwise at A's, ramping to B's over the loop.

The finder only offers candidates at section boundaries in both songs, so
the loop and B's entry line up with the songs' structure. Positions are
written as your cues or words (`{"word", "nth"}`) where they can be, so the
plan reads like the transition it is. No lyric lines are written: single words.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from dj_segue.finder import Candidate, Track

LOOP_BEATS = 4.0  # a bar: musical, not a stutter
LOOP_EARLY = 0.15  # a word this close before a beat loops from that beat
A_LEAD_BARS = 8  # A plays at least this long before its word
BED_MIN, BED_MAX = 8.0, 16.0  # beats of B under the loop
ENTRY_PREROLL = 0.1  # beats before B's entry word, so its attack isn't clipped
B_PLAY_BARS = 16  # B plays at least this long after it comes in, to the end of a hook
SAME_TEMPO = 0.04  # within this, the whole mix runs at B's tempo


def track_id(t: Track) -> str:
    """Initials of the title: "Let's Get It Started" → "lgis"; a one-word
    title whole: "Starships" → "starships"."""
    words = [w.replace("'", "") for w in re.findall(r"[A-Za-z0-9']+", _title(t))]
    words = [w for w in words if w]
    if len(words) == 1:
        return words[0].lower()
    return "".join(w[0] for w in words).lower() or "t"


def schema_nth(t: Track, i: int, n: int) -> int:
    """Which time words i..i+n-1 are sung, counted as a plan's word position
    counts (every run of those words in the track's words)."""
    from dj_segue.words.merge import norm

    keys = [norm(w.text) if w.source != "spot" else None for w in t.words]  # stand-ins aren't sung words
    target = keys[i : i + n]
    return sum(1 for j in range(i + 1) if keys[j : j + n] == target)


def _beat(t: Track, sec: float) -> float:
    return (sec - t.anchor) * t.bpm / 60.0


def loop_start(beat: float) -> float:
    """The beat a loop of the word at `beat` starts on: the beat at or before
    it, or the next one when the word comes just ahead of it (a pickup sung
    a hair early)."""
    return float(math.floor(beat + LOOP_EARLY))


def _fmt(x: float) -> float | int:
    x = round(x, 3)
    return int(x) if x == int(x) else x


def loop_and_jump(a: Track, b: Track, a_path: Path, b_path: Path, c: Candidate) -> str:
    """The plan (JSONC text) for candidate `c` from A into B."""
    from dj_segue.finder import LEAD_MAX_BEATS

    if None in (a.bpm, a.anchor, b.bpm, b.anchor):
        raise ValueError("both tracks need an annotation (grid) for a template")
    ia, ida, idb = c.a, track_id(a), track_id(b)
    if ida == idb:
        ida, idb = ida + "_a", idb + "_b"

    # A: from a section start at least A_LEAD_BARS before its word, to the beat the word
    # is on (your cue, if you marked one: it's snapped to the beat); a bar's loop from there.
    a_word = c.a.beat
    a_pos = {"cue": ia.cue} if ia.cue else {"beat": _fmt(loop_start(a_word))}
    loop_len = ia.loop or LOOP_BEATS
    starts = [s for s, _, _ in a.sections if a_word - 4 * s >= 4 * A_LEAD_BARS]
    a_from = max(starts) if starts else 0

    # B: in at its word, or at the start of its line when the word follows within a bar.
    ob = c.b
    at_line = not ob.spot and ob.edge not in ("start", "alone") and ob.lead <= LEAD_MAX_BEATS
    entry = ob.line_first if at_line else ob.index
    if entry == ob.index and ob.cue:
        b_pos, j = {"cue": ob.cue}, ob.beat
    else:
        n_e = ob.last - ob.index + 1 if entry == ob.index else 1
        text = c.b_text if entry == ob.index else _norm(b.words[entry].text)
        b_pos = {"word": text, "nth": schema_nth(b, entry, n_e),
                 "offset": {"beats": -ENTRY_PREROLL}}
        j = _beat(b, b.words[entry].start) - ENTRY_PREROLL
    j = round(j, 3)  # a cue a hair past the beat is on it (else the loop would run a pass too many)
    straight = BED_MIN <= j <= BED_MAX  # B plays from the top to its entry: no jump
    bed = j if straight else 12.0 + (j % 4)
    reps = math.ceil(bed / loop_len - 1e-6)
    entry_bar = int(j // 4)
    ends = [e for _, e, lab in b.sections if e >= entry_bar + B_PLAY_BARS and lab == "hook"] or \
        [e for _, e, _ in b.sections if e >= entry_bar + B_PLAY_BARS]
    b_to = min(ends) if ends else (max(e for _, e, _ in b.sections) if b.sections else entry_bar + B_PLAY_BARS)

    # Tempo.
    ratio = b.bpm / a.bpm - 1
    same = abs(ratio) <= SAME_TEMPO
    meta = {"mix_name": f"{_title(a)} into {_title(b)}, on \"{c.b_text}\"",
            "source_prompt": f"Template: loop and jump on \"{c.a_text}\" / \"{c.b_text}\" (dj-segue find)",
            "mix_tempo": _fmt(b.bpm if same else a.bpm)}
    if not same:
        meta["tempo_map"] = [{"at": {"after": ida}, "bpm": _fmt(b.bpm), "ramp": {"beats": _fmt(bed)}}]

    loop_id, bed_id = f"{ida}_loop", f"{idb}_bed" if not straight else idb
    timeline = [
        {"type": "play", "id": ida, "deck": 1, "track": ida,
         "from": {"bar": a_from}, "to": a_pos, "start_at": {"beat": 0}, "target_bpm": "mix"},
        {"type": "loop", "id": loop_id, "deck": 1, "track": ida,
         "from": a_pos, "start_at": {"after": ida}, "target_bpm": "mix",
         "schedule": [{"length": {"beats": _fmt(loop_len)}, "repetitions": reps}]},
    ]
    if straight:
        timeline.append({"type": "play", "id": idb, "deck": 2, "track": idb,
                         "from": {"beat": 0}, "to": {"bar": b_to}, "start_at": {"after": ida},
                         "target_bpm": "mix"})
    else:
        timeline += [
            {"type": "play", "id": bed_id, "deck": 2, "track": idb,
             "from": {"beat": 0}, "to": {"beat": _fmt(bed)}, "start_at": {"after": ida}, "target_bpm": "mix"},
            {"type": "play", "id": f"{idb}_main", "deck": 2, "track": idb,
             "from": b_pos, "to": {"bar": b_to}, "target_bpm": "mix"},
        ]
    # A crossfades into B over the whole bed: the loop dies away into B (held up and
    # then dropped, it sounds cut off), gone as B sings the word, so the two don't merge.
    timeline.append({"type": "transition", "style": "crossfade", "from_deck": 1, "to_deck": 2,
                     "start_at": {"after": ida}, "duration": {"beats": _fmt(bed)}})

    # B's vocals up as it reaches its entry (B's beats follow the mix's from where it comes in,
    # so the entry is `bed` mix beats after A's word); quickly at a jump, so none of the bed's leak.
    rise = 0.25 if straight else 0.05
    up = [{"at": {"after": ida, "offset": {"beats": _fmt(bed - rise)}}, "value": 0},
          {"at": {"after": ida, "offset": {"beats": _fmt(bed)}}, "value": 1}]
    automation = [{"lane": "stem_volume", "deck": 2, "stem": "vocals", "keyframes": up}]

    plan = {"schema_version": "0.10", "meta": meta,
            "tracks": {ida: {"path": str(a_path)}, idb: {"path": str(b_path)}},
            "decks": {"1": {"label": ida}, "2": {"label": idb}},
            "timeline": timeline, "automation": automation}

    where_b = (f"the start of its line, {ob.lead:.1f} beats before its \"{c.b_text}\"" if at_line
               else f"its \"{c.b_text}\"")
    head = [
        f"{_title(a)} into {_title(b)}, on \"{c.b_text}\" — loop and jump (dj-segue find → template).",
        "",
        f"  - {_title(a)} plays from bar {a_from} to its \"{c.a_text}\" (bar {c.a.bar}, {c.a.section}"
        f"{'' if ia.cue else f', on beat {_fmt(loop_start(a_word))}'}).",
        f"  - There it loops {'your spot' if ia.spot else 'a bar from the word'} ({_fmt(loop_len)} beats ×{reps}) while "
        f"{_title(b)} comes in under it, vocals down: "
        + (f"its first {_fmt(bed)} beats, straight on to its entry." if straight else
           f"its first {_fmt(bed)} beats (≡ its entry in the bar, so the jump keeps its bars in step)."),
        f"  - As the loop ends, {_title(b)} {'reaches' if straight else 'jumps to'} {where_b} "
        f"(bar {c.b.bar}, {c.b.section}{', ' + _into(b, c) if _into(b, c) else ''}), its vocals up: "
        "the word looped, then sung by the next song.",
        f"  - It plays on to bar {b_to}.",
        ("  - Tempo: the whole mix at " + f"{_title(b)}'s {_fmt(b.bpm)} ({100 * ratio:+.1f}% for {_title(a)})."
         if same else
         f"  - Tempo: {_title(a)}'s {_fmt(a.bpm)}, ramping to {_title(b)}'s {_fmt(b.bpm)} over the loop."),
    ]
    return "\n".join("// " + line if line else "//" for line in head) + "\n\n" + dump(plan) + "\n"


def _norm(text: str) -> str:
    from dj_segue.words.merge import norm

    return norm(text)


def _title(t: Track) -> str:
    from dj_segue.words.lrclib import clean_title

    stem = re.sub(r"^(\d+\s*|[^-]+?\s)-\s+", "", Path(t.name).stem)  # "01 - " or "Artist - "
    return re.sub(r"\s*[(\[][^)\]]*[)\]]", "", clean_title(stem)).strip()


def _into(b: Track, c: Candidate) -> str:
    from dj_segue.finder import into_hook

    return into_hook(b, c.b)


def _flat(obj) -> str:
    if isinstance(obj, dict):
        return "{ " + ", ".join(f"{json.dumps(k)}: {_flat(v)}" for k, v in obj.items()) + " }" if obj else "{}"
    if isinstance(obj, list):
        return "[ " + ", ".join(_flat(v) for v in obj) + " ]" if obj else "[]"
    return json.dumps(obj, ensure_ascii=False)


def dump(obj, indent: int = 0, width: int = 110) -> str:
    """JSON laid out like a hand-written plan: anything that fits on a line
    stays on one; segments and lanes one per line where they fit."""
    flat = _flat(obj)
    if not isinstance(obj, (dict, list)) or indent + len(flat) <= width:
        return flat
    pad, inner = " " * indent, " " * (indent + 2)
    if isinstance(obj, list):
        items = [inner + dump(v, indent + 2, width) for v in obj]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    items = [f"{inner}{json.dumps(k)}: {dump(v, indent + 2, width)}" for k, v in obj.items()]
    return "{\n" + ",\n".join(items) + "\n" + pad + "}"
