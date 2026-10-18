"""The wordplay finder (M6 W4): where two tracks could hand over on a word.

Given an outgoing track A and an incoming track B, each with word timing
(`dj-segue words`) and ideally an annotation (sections, cues, grid), list
places a wordplay transition could turn on, best first:

  - **same word** in both ("everybody" / "everybody"), or the same phrase;
  - **sound-alike**: pronounced the same, spelled differently ("know" / "no");
  - **rhyme**: same sound from the last stressed vowel on ("body" / "everybody").

Pronunciations come from the CMU dictionary (`pronouncing`); stress is
ignored. Filler and function words ("the", "oh", "yeah") don't count.

Each candidate pairs one occurrence in A with one in B and is scored
(0–1 parts, weighted; see `score`):

  - match: same word / phrase > sound-alike > rhyme;
  - prominence: in a track's title; in a hook / pre-hook / riff (and how often
    the word is sung there); for A, late enough that A has played a while;
  - reliability: Whisper confirmed the word in each track; alignment scores;
  - fit: the word lands on the same beat of the bar in both (a handoff can
    keep both grids in step).

Wordplay is heard as a pair: A sings the word, then B sings it right after
(within a bar or two), never both at once — a swap inside one word merges
the two into one and nothing is heard.

Never mid-section: in both songs the transition sits at a section boundary
— A's word, and where B comes in, in a section's first or last bar (so a
pickup into the next section counts). Looping a mid-verse word doesn't make
it a boundary. (Tracks without annotated sections skip this check.)

**Wordplay spots** — cues you marked in annotate with a role ("out": this
song can leave here; "in": another can come in here; "both") and a word —
override all of that: your ear has judged them. A spot takes the place of
the nearest time its word is sung (or stands in for it if the aligner
missed it), skips the boundary and line checks on its side, and pairs that
use spots rank first (`find(..., spots_only=True)` lists only those).

Never in the middle of vocals: a transition doesn't cut through a sung
line. Each occurrence knows where it sits in its line (`edge`: "start",
"end", "alone" or "mid"; a line ends at a lyric line break or a gap of a beat
or more). B's vocal comes in on its word or at the start of its line — when
the word is sung within a bar of it (`lead`), B can come in there and loop
and jump still works. A's word must end a line, unless A loops it (the loop
lifts the word out of its line).

Per match it keeps the best-scoring pair, and suggests a technique:
  - **loop and jump**: loop A's word while B's intro comes in, then jump B to
    its word, or to the start of its line when the word is sung within a bar
    of it (A's word may sit anywhere in its line);
  - **vocal handoff**: A's word ends a line, B's starts one, and they land on
    the same beat of the bar (within ¼ beat) — B comes in bar-aligned with its
    word a bar after A's, and the vocals swap between the two;
  - **echo out**: A's word ends a line, B's starts one — A echoes out on its
    word, B comes in on its.
A pair where B's word is further into its line fits none, and is left out.
B's word leading into a hook (its pickup, or the hook's first word) counts as
the most prominent place B can come in.

The tempo gap and key relation are reported for the pair (they don't change
the ranking within it). Output never includes lyric lines: single words,
times and positions only.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

STOP = frozenset("""
a an the and or but if so as at by for from in into of off on onto out over to up with without
i me my mine you your yours he him his she her hers it its we us our they them their
is am are was were be been being do does did done have has had will would shall should can could
may might must not no nor this that these those there here what who whom which when where why how
all any some just only very too than then now
oh ooh ah uh uhh huh yeah yea yo hey ay ayy la na mm hmm whoa woah ha haha
im ive id ill youre youve youll youd hes shes its were theyre theyll weve dont doesnt didnt cant wont aint
""".split())
# The aint/dont family stays out: too common to be a deliberate connection.

# Everyday words: they count, but a shared one is rarely a deliberate link, so
# its match is worth less (`COMMON_FACTOR`). A rare word shared is the find.
COMMON = frozenset("""
like get got let know go goes going gone come came coming make made say said see saw seen want need
take took give gave tell told feel felt think thought look looking way time man men girl girls boy boys
baby babe good bad new old one two three more most never ever back right left night day call bring
cause because still keep kept put let find found work round around really something nothing thing
things every much many little big next whole buy show turn run take while yes okay ok
""".split())
COMMON_FACTOR = 0.45
# Words in a file's title that say which edition it is, not what it's called.
EDITION = frozenset("""
extended version mix remix radio edit clean explicit original remastered remaster live acoustic
instrumental feat ft featuring spike club dub vip bonus deluxe single album
""".split())

WEIGHTS = {"match": 0.30, "prominence": 0.30, "reliability": 0.25, "fit": 0.15}
MATCH = {"phrase": 1.0, "same": 0.85, "sound-alike": 0.75, "rhyme": 0.5}
SECTION = {"hook": 1.0, "pre-hook": 0.9, "riff": 0.85, "drop": 0.85, "build": 0.7, "bridge": 0.6,
           "verse": 0.55, "interlude": 0.5, "breakdown": 0.5, "intro": 0.4, "outro": 0.35, "?": 0.5}
HANDOFF_PHASE = 0.25  # beats: the word on the same beat of the bar in both


@dataclass
class Occurrence:
    word: str  # as sung (one word; a phrase's first word)
    start: float  # seconds in the track
    beat: float | None  # on the annotation's grid
    bar: int | None
    section: str
    confirmed: bool | None
    score: float  # alignment score
    nth: int  # which time this word (or phrase) is sung, from 1
    cue: str | None = None  # a cue marked here, named after the word: checked by ear
    edge: str = "alone"  # where it sits in its sung line: "start" | "end" | "alone" | "mid"
    index: int = -1  # in the track's words (a phrase: its first word)
    last: int = -1  # its last word
    line_first: int = -1  # the first word of its sung line
    lead: float = 0.0  # beats from the start of its line to it
    spot: str = ""  # at your wordplay spot: its role, "out" | "in" | "both"
    loop: float | None = None  # an out spot's loop, in beats


@dataclass
class Candidate:
    kind: str  # "phrase" | "same" | "sound-alike" | "rhyme"
    a_text: str
    b_text: str
    a: Occurrence
    b: Occurrence
    score: float
    parts: dict = field(default_factory=dict)
    technique: str = ""
    why: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Track:
    name: str
    title_words: set[str]
    bpm: float | None
    anchor: float | None
    key: str | None
    camelot: str | None
    duration: float
    sections: list[tuple[int, int, str]]  # (first bar, end bar, label)
    words: list  # merge.Word, lyric words when there are lyrics
    counts: dict[str, int]  # normalized word → times sung
    cues: dict[str, float] = field(default_factory=dict)  # marked in the annotation: name → second
    spots: list = field(default_factory=list)  # annotate.Cue with a role: your wordplay spots


def load_track(audio_path: Path, folder: Path = Path("annotations")) -> Track:
    from dj_segue.annotate import annotation_path, camelot, file_sha1, load
    from dj_segue.words import cached, read_tags, words_for
    from dj_segue.words.lrclib import clean_title
    from dj_segue.words.merge import norm

    audio_path = Path(audio_path)
    wt = cached(audio_path) or words_for(audio_path, log=lambda *_: None)
    lyric = [w for w in wt.words if w.source == "lyrics"]
    words = lyric or wt.words
    tags = read_tags(audio_path)
    title = clean_title(tags.get("title") or audio_path.stem)
    ann = None
    path = annotation_path(audio_path, folder)
    if path.exists():
        ann = load(path)
        if ann.audio_sha1 != file_sha1(audio_path):
            ann = None
    spots = [c for c in ann.cues if c.role] if ann else []
    words = with_spots(words, spots, ann.bpm if ann else None)
    counts: dict[str, int] = {}
    for w in words:
        k = norm(w.text)
        counts[k] = counts.get(k, 0) + 1
    return Track(
        name=audio_path.name,
        title_words={norm(t) for t in re.split(r"[\s\-()\[\]]+", title) if norm(t)} - STOP - EDITION,
        bpm=ann.bpm if ann else None,
        anchor=ann.anchor_sec if ann else None,
        key=ann.key if ann else None,
        camelot=camelot(ann.key) if ann else None,
        duration=tags.get("duration") or (ann.duration_sec if ann else 0.0),
        sections=[(p.bar, p.bar + p.bars, p.label) for p in ann.phrases] if ann else [],
        words=words,
        counts=counts,
        cues={c.name: c.second for c in ann.cues} if ann else {},
        spots=spots,
    )


def with_spots(words: list, spots: list, bpm: float | None) -> list:
    """`words`, plus a stand-in (source "spot") for each spot whose word isn't
    sung within CUE_NEAR_BEATS of it — so a spot counts even where the
    aligner missed the word (a chant Whisper skipped)."""
    from dj_segue.words.merge import Word, norm

    near = CUE_NEAR_BEATS * (60.0 / bpm if bpm else 0.5)
    extra = []
    for c in spots:
        toks = [norm(t) for t in c.word.split()]
        if not any(norm(w.text) == toks[0] and abs(w.start - c.second) <= near for w in words):
            extra += [Word(t, c.second + 0.3 * k, c.second + 0.3 * k + 0.25, 1.0, "spot", True, None)
                      for k, t in enumerate(c.word.split())]
    return sorted(words + extra, key=lambda w: w.start) if extra else words


# -- sounds ----------------------------------------------------------------


@lru_cache(maxsize=None)
def phones(word: str) -> str | None:
    import pronouncing

    found = pronouncing.phones_for_word(word)
    return re.sub(r"\d", "", found[0]) if found else None


@lru_cache(maxsize=None)
def rhyme_key(word: str) -> str | None:
    import pronouncing

    found = pronouncing.phones_for_word(word)
    return re.sub(r"\d", "", pronouncing.rhyming_part(found[0])) if found else None


def relation(a: str, b: str) -> str | None:
    """How two normalized words connect, if they do."""
    if a == b:
        return "same"
    pa, pb = phones(a), phones(b)
    if pa and pa == pb:
        return "sound-alike"
    ra, rb = rhyme_key(a), rhyme_key(b)
    if ra and ra == rb and len(ra.split()) >= 2 and not (a.endswith(b) or b.endswith(a)):
        return "rhyme"  # (a one-phone rhyme is too loose; "-ing"/"-s" variants aren't wordplay)
    return None


# -- finding ---------------------------------------------------------------


CUE_NEAR_BEATS = 2.0  # a cue this close to a word, named after it, marks that word
LINE_GAP_BEATS = 1.0  # a silence this long between words ends a sung line
LEAD_MAX_BEATS = 4.0  # B can come in at its line's start when its word follows within this


def _breaks(t: Track, i: int, j: int) -> bool:
    """Does a sung line end between words i and j (i < j)?"""
    if i < 0 or j >= len(t.words):
        return True
    wi, wj = t.words[i], t.words[j]
    gap = LINE_GAP_BEATS * (60.0 / t.bpm if t.bpm else 0.6)
    return (wi.line is not None and wj.line is not None and wi.line != wj.line) or wj.start - wi.end >= gap


def at_boundary(t: Track, beat: float | None) -> bool:
    """Is `beat` in the first or last bar of a section? True when the track
    has no sections to check against."""
    if not t.sections or beat is None:
        return True
    bar = int(round(beat, 2) // 4)  # a word a hair before a bar line is in that bar
    return any(bar in (s, e - 1) for s, e, _ in t.sections)


def line_first(t: Track, i: int) -> int:
    """The first word of the sung line word i is in."""
    while not _breaks(t, i - 1, i):
        i -= 1
    return i


def line_edge(t: Track, first: int, last: int) -> str:
    """Where words first..last sit in their sung line: "start", "end",
    "alone" (both) or "mid"."""
    starts, ends = _breaks(t, first - 1, first), _breaks(t, last, last + 1)
    return "alone" if starts and ends else "start" if starts else "end" if ends else "mid"


def _occurrence(t: Track, i: int, nth: int, n: int = 1) -> Occurrence:
    """Where word i (and the phrase of n words it starts) is; if you marked a cue named after it nearby, there
    instead (your ear beats the aligner)."""
    from dj_segue.words.merge import norm

    w = t.words[i]
    start, cue, spot, loop = w.start, None, "", None
    beat_len = 60.0 / t.bpm if t.bpm else 0.5
    key = norm(w.text)
    text = " ".join(norm(x.text) for x in t.words[i : i + n])
    for c in t.spots:
        if " ".join(norm(x) for x in c.word.split()) == text and abs(c.second - w.start) <= CUE_NEAR_BEATS * beat_len:
            start, cue, spot, loop = c.second, c.name, c.role, c.loop
            break
    else:
        for name, sec in t.cues.items():
            if key and key in norm(name) and abs(sec - w.start) <= CUE_NEAR_BEATS * beat_len:
                start, cue = sec, name
                break
    beat = bar = None
    section = "?"
    if t.bpm and t.anchor is not None:
        beat = (start - t.anchor) * t.bpm / 60.0
        bar = int(round(beat, 2) // 4)
        section = next((lab for a, b, lab in t.sections if a <= bar < b), "?")
    first = line_first(t, i)
    lead = (w.start - t.words[first].start) / beat_len
    return Occurrence(w.text, start, beat, bar, section, w.confirmed, w.score, nth, cue,
                      line_edge(t, i, i + n - 1), i, i + n - 1, first, lead, spot, loop)


def _key_words(t: Track):
    """(normalized word, index, nth) for every word worth matching."""
    from dj_segue.words.merge import norm

    seen: dict[str, int] = {}
    for i, w in enumerate(t.words):
        k = norm(w.text)
        seen[k] = seen.get(k, 0) + 1
        if len(k) >= 3 and k not in STOP and not k.isdigit():
            yield k, i, seen[k]


def _phrases(t: Track, n: int, keep: frozenset = frozenset()):
    """(normalized n-word phrase, index of its first word, nth). Phrases of
    only common words are skipped unless in `keep` (a spot named them)."""
    from dj_segue.words.merge import norm

    keys = [norm(w.text) for w in t.words]
    seen: dict[str, int] = {}
    for i in range(len(keys) - n + 1):
        gram = keys[i : i + n]
        p = " ".join(gram)
        if not all(gram) or (p not in keep and not any(g not in STOP and g not in COMMON for g in gram)) or any(
                t.words[i + k + 1].start - t.words[i + k].start > 1.5 for k in range(n - 1)):
            continue  # a phrase is sung in one breath
        seen[p] = seen.get(p, 0) + 1
        yield p, i, seen[p]


def _spot_key(word: str) -> str:
    from dj_segue.words.merge import norm

    return " ".join(k for k in (norm(w) for w in word.split()) if k)


def out_spot(o: Occurrence) -> bool:
    return o.spot in ("out", "both")


def in_spot(o: Occurrence) -> bool:
    return o.spot in ("in", "both")


def spots_used(c: Candidate) -> int:
    return out_spot(c.a) + in_spot(c.b)


def find(a: Track, b: Track, top: int = 10, spots_only: bool = False) -> list[Candidate]:
    """Candidates for a transition from `a` into `b`, best first (pairs at
    your spots before the rest); one (the best pair) per matching word or
    phrase. `spots_only`: only pairs that use at least one of your spots."""
    best: dict[tuple, Candidate] = {}

    def consider(kind, ka, ia, na, kb, ib, nb):
        c = score(a, b, kind, ka, kb, _occurrence(a, ia, na, len(ka.split())),
                  _occurrence(b, ib, nb, len(kb.split())))
        if not c.technique:
            return  # B's word is mid-line: no transition fits without cutting through vocals
        if spots_only and not spots_used(c):
            return
        key = (kind, ka, kb)
        if key not in best or (spots_used(c), c.score) > (spots_used(best[key]), best[key].score):
            best[key] = c

    bw = list(_key_words(b))
    by_word: dict[str, list] = {}
    for k, i, n in bw:
        by_word.setdefault(k, []).append((i, n))
    rel_cache: dict[tuple[str, str], str | None] = {}
    for ka, ia, na in _key_words(a):
        for kb in by_word:
            rel = rel_cache.setdefault((ka, kb), relation(ka, kb))
            if rel:
                for ib, nb in by_word[kb]:
                    consider(rel, ka, ia, na, kb, ib, nb)
    keep = frozenset(_spot_key(s.word) for s in a.spots + b.spots if s.word)  # your spots' phrases count, however common
    for n in sorted({2, 3} | {len(k.split()) for k in keep if len(k.split()) > 1}):
        pb: dict[str, list] = {}
        for p, i, k in _phrases(b, n, keep):
            pb.setdefault(p, []).append((i, k))
        for p, ia, na in _phrases(a, n, keep):
            for ib, nb in pb.get(p, []):
                consider("phrase", p, ia, na, p, ib, nb)
    # A phrase that scores as well supersedes its own words' "same" match.
    phrases = [c for c in best.values() if c.kind == "phrase"]
    out = [c for c in best.values() if not (
        c.kind == "same" and any(c.a_text in p.a_text.split() and p.score >= c.score for p in phrases))]
    out.sort(key=lambda c: (-spots_used(c), -c.score))
    return out[:top]


def score(a: Track, b: Track, kind: str, ta: str, tb: str, oa: Occurrence, ob: Occurrence) -> Candidate:
    def prominence(t: Track, o: Occurrence, text: str, outgoing: bool) -> float:
        words = text.split()
        title = 1.0 if any(w in t.title_words for w in words) else 0.0
        if (out_spot if outgoing else in_spot)(o):
            return 1.0  # your spot: as prominent as it gets
        section = SECTION.get(o.section, 0.5)
        if not outgoing:  # B comes in best leading into a hook, then in one
            section = 1.0 if into_hook(t, o) else 0.85 * section
        repeats = min(1.0, (t.counts.get(words[0], 1) - 1) / 6)
        late = 1.0
        if outgoing and t.duration:
            late = min(1.0, (o.start / t.duration) / 0.25)  # A should have played a while
        return (0.35 * title + 0.4 * section + 0.25 * repeats) * (0.5 + 0.5 * late)

    def reliable(o: Occurrence) -> float:
        if o.cue is not None:
            return 1.0  # checked by ear
        conf = {True: 1.0, None: 0.6, False: 0.35}[o.confirmed]
        return conf * (0.5 + 0.5 * min(1.0, o.score / 0.5))

    fit, phase_gap = 0.5, None
    if oa.beat is not None and ob.beat is not None:
        d = abs((oa.beat % 4) - (ob.beat % 4))
        phase_gap = min(d, 4 - d)
        fit = max(0.0, 1.0 - phase_gap / 2)
    common = all(w in COMMON for w in (ta.split() + tb.split()) if w not in STOP)
    parts = {
        "match": round(MATCH[kind] * (COMMON_FACTOR if common else 1.0), 3),
        "prominence": round(0.5 * prominence(a, oa, ta, True) + 0.5 * prominence(b, ob, tb, False), 3),
        "reliability": round(reliable(oa) * reliable(ob), 3),
        "fit": round(fit, 3),
    }
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    technique, why = suggest(a, b, oa, ob, phase_gap)
    return Candidate(kind, ta, tb, oa, ob, round(total, 3), parts, technique, why)


def suggest(a: Track, b: Track, oa: Occurrence, ob: Occurrence, phase_gap: float | None) -> tuple[str, str]:
    """The techniques that fit (best first), and why; none ("", reason) when
    either song would change mid-section, or B's word is too far into its line."""
    a_spot, b_spot = out_spot(oa), in_spot(ob)
    b_starts = b_spot or ob.edge in ("start", "alone")
    if not b_starts and ob.lead > LEAD_MAX_BEATS:
        return "", "B's word is in the middle of a sung line"
    if not a_spot and not at_boundary(a, oa.beat):
        return "", "A's word is mid-section"
    if not b_spot and not at_boundary(b, ob.beat if b_starts or ob.beat is None else ob.beat - ob.lead):
        return "", "B would come in mid-section"
    if a_spot or b_spot:
        mine = " and ".join(x for x, y in (("A's out spot", a_spot), ("B's in spot", b_spot)) if y)
        found = [("loop and jump", f"at your spots ({mine}): loop A from its word while B's intro "
                  "comes in, then jump B to its word")]
        if b_spot and oa.edge in ("end", "alone") and not a_spot:
            found.append(("echo out", "A's word ends a line: A echoes out on it; B's word comes in during the echo"))
        return " or ".join(t for t, _ in found), "; ".join(w for _, w in found)
    found = []
    into = into_hook(b, ob)
    into = f", {into}" if into else ""
    a_ends = oa.edge in ("end", "alone")
    loop = ("loop and jump", (f"B's word starts a line{into}: loop A's word while B's intro comes in, "
                              "then jump B to its word") if b_starts else
            (f"B's word comes {ob.lead:.1f} beats into its line{into}: loop A's word while B's intro "
             "comes in, then jump B to the start of that line"))
    if not b_starts:
        return loop  # B can only come in at the start of its line
    if into or not a_ends:
        found.append(loop)  # a hook entry, or the only way out of A mid-line
    if a_ends and phase_gap is not None and phase_gap <= HANDOFF_PHASE:
        found.append(("vocal handoff",
                      f"A's word ends a line and B's starts one, {phase_gap:.2f} beats apart in the bar: "
                      "bring B in bar-aligned with its word a bar after A's, and swap vocals between them"))
    if a_ends:
        found.append(("echo out", "A's word ends a line: A echoes out on it; B's word comes in during the echo"))
        if not into:
            found.append(loop)
    return " or ".join(t for t, _ in found), "; ".join(w for _, w in found)


def into_hook(b: Track, ob: Occurrence) -> str:
    """"the pickup into its hook" / "opening its hook" when B's word leads
    into a hook (pre-hook, drop), else ""."""
    if ob.bar is None or ob.beat is None or not b.sections:
        return ""
    starts = {a: lab for a, _, lab in b.sections}
    if ob.beat % 4 >= 2 and starts.get(ob.bar + 1) in ("hook", "pre-hook", "drop"):
        return f"the pickup into its {starts[ob.bar + 1]}"
    if ob.beat % 4 < 1 and starts.get(ob.bar) in ("hook", "pre-hook", "drop"):
        return f"opening its {starts[ob.bar]}"
    return ""


def summary_line(c: Candidate) -> str:
    def where(o: Occurrence) -> str:
        rb = round(o.beat, 2) if o.beat is not None else None  # 107.999 reads bar 27 beat 1.00, not bar 26 beat 5.00
        pos = f"bar {int(rb // 4)} beat {rb % 4 + 1:.2f}" if rb is not None else f"{o.start:.1f}s"
        mark = f"◆{o.spot}" if o.spot else "⚑" if o.cue else "✓" if o.confirmed else ("?" if o.confirmed is False else "")
        return f"{pos} {o.section} #{o.nth}{mark} {o.edge}"

    match = c.a_text if c.a_text == c.b_text else f"{c.a_text} / {c.b_text}"
    return (f"{c.score:.2f}  {c.kind:<11} {match!r:<24} A: {where(c.a):<34} B: {where(c.b):<34} → {c.technique}")


def pair_notes(a: Track, b: Track) -> list[str]:
    from dj_segue.annotate import camelot

    notes = []
    if a.bpm and b.bpm:
        notes.append(f"tempo: A {a.bpm:.2f} → B {b.bpm:.2f} bpm ({100 * (b.bpm / a.bpm - 1):+.1f}%)")
    if a.camelot and b.camelot:
        notes.append(f"keys: A {a.key} ({a.camelot}) → B {b.key} ({b.camelot}): {key_relation(a.camelot, b.camelot)}"
                     " (unchecked keys are guesses)")
    return notes


def key_relation(ca: str, cb: str) -> str:
    na, la, nb, lb = int(ca[:-1]), ca[-1], int(cb[:-1]), cb[-1]
    step = min((na - nb) % 12, (nb - na) % 12)
    if (na, la) == (nb, lb):
        return "same key"
    if na == nb:
        return "relative major/minor (compatible)"
    if la == lb and step == 1:
        return "a fifth apart (compatible)"
    if la == lb and step == 2:
        return "two steps (an energy lift; overlap briefly)"
    return "not compatible: avoid long harmonic overlaps"


def to_json(cands: list[Candidate], a: Track, b: Track) -> str:
    return json.dumps({"a": a.name, "b": b.name, "notes": pair_notes(a, b),
                       "candidates": [c.to_json() for c in cands]}, indent=1)
