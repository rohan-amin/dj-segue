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

Per match it keeps the best-scoring pair, and suggests a technique:
  - **vocal handoff**: beats in the bar line up (within ¼ beat) — B comes in
    bar-aligned and the vocals swap on the word;
  - **loop and jump**: B's word starts a hook or its pickup — loop A's bar
    with the word while B's intro comes in, then jump B to its word;
  - **echo out**: otherwise — A echoes out on its word, B comes in on its.

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
    )


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


def _occurrence(t: Track, i: int, nth: int) -> Occurrence:
    """Where word i is; if you marked a cue named after it nearby, there
    instead (your ear beats the aligner)."""
    from dj_segue.words.merge import norm

    w = t.words[i]
    start, cue = w.start, None
    beat_len = 60.0 / t.bpm if t.bpm else 0.5
    key = norm(w.text)
    for name, sec in t.cues.items():
        if key and key in norm(name) and abs(sec - w.start) <= CUE_NEAR_BEATS * beat_len:
            start, cue = sec, name
            break
    beat = bar = None
    section = "?"
    if t.bpm and t.anchor is not None:
        beat = (start - t.anchor) * t.bpm / 60.0
        bar = int(beat // 4 + 1e-6)
        section = next((lab for a, b, lab in t.sections if a <= bar < b), "?")
    return Occurrence(w.text, start, beat, bar, section, w.confirmed, w.score, nth, cue)


def _key_words(t: Track):
    """(normalized word, index, nth) for every word worth matching."""
    from dj_segue.words.merge import norm

    seen: dict[str, int] = {}
    for i, w in enumerate(t.words):
        k = norm(w.text)
        seen[k] = seen.get(k, 0) + 1
        if len(k) >= 3 and k not in STOP and not k.isdigit():
            yield k, i, seen[k]


def _phrases(t: Track, n: int):
    """(normalized n-word phrase, index of its first word, nth)."""
    from dj_segue.words.merge import norm

    keys = [norm(w.text) for w in t.words]
    seen: dict[str, int] = {}
    for i in range(len(keys) - n + 1):
        gram = keys[i : i + n]
        if not all(gram) or not any(g not in STOP and g not in COMMON for g in gram) or any(
                t.words[i + k + 1].start - t.words[i + k].start > 1.5 for k in range(n - 1)):
            continue  # a phrase is sung in one breath
        p = " ".join(gram)
        seen[p] = seen.get(p, 0) + 1
        yield p, i, seen[p]


def find(a: Track, b: Track, top: int = 10) -> list[Candidate]:
    """Candidates for a transition from `a` into `b`, best first; one (the
    best pair) per matching word or phrase."""
    best: dict[tuple, Candidate] = {}

    def consider(kind, ka, ia, na, kb, ib, nb):
        c = score(a, b, kind, ka, kb, _occurrence(a, ia, na), _occurrence(b, ib, nb))
        key = (kind, ka, kb)
        if key not in best or c.score > best[key].score:
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
    for n in (2, 3):
        pb: dict[str, list] = {}
        for p, i, k in _phrases(b, n):
            pb.setdefault(p, []).append((i, k))
        for p, ia, na in _phrases(a, n):
            for ib, nb in pb.get(p, []):
                consider("phrase", p, ia, na, p, ib, nb)
    # A phrase that scores as well supersedes its own words' "same" match.
    phrases = [c for c in best.values() if c.kind == "phrase"]
    out = [c for c in best.values() if not (
        c.kind == "same" and any(c.a_text in p.a_text.split() and p.score >= c.score for p in phrases))]
    out.sort(key=lambda c: -c.score)
    return out[:top]


def score(a: Track, b: Track, kind: str, ta: str, tb: str, oa: Occurrence, ob: Occurrence) -> Candidate:
    def prominence(t: Track, o: Occurrence, text: str, outgoing: bool) -> float:
        words = text.split()
        title = 1.0 if any(w in t.title_words for w in words) else 0.0
        section = SECTION.get(o.section, 0.5)
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
    technique, why = suggest(b, oa, ob, phase_gap)
    return Candidate(kind, ta, tb, oa, ob, round(total, 3), parts, technique, why)


def suggest(b: Track, oa: Occurrence, ob: Occurrence, phase_gap: float | None) -> tuple[str, str]:
    """The techniques that fit (best first), and why."""
    found = []
    if phase_gap is not None and phase_gap <= HANDOFF_PHASE:
        found.append(("vocal handoff",
                      f"the word falls {phase_gap:.2f} beats apart in the bar in both: bring B in "
                      "bar-aligned and swap vocals on it"))
    if ob.bar is not None and ob.beat is not None and b.sections:
        starts = {a: lab for a, _, lab in b.sections}
        pickup = ob.beat % 4 >= 2 and starts.get(ob.bar + 1) in ("hook", "pre-hook", "drop")
        opener = ob.beat % 4 < 1 and starts.get(ob.bar) in ("hook", "pre-hook", "drop")
        if pickup or opener:
            found.append(("loop and jump",
                          f"B's word {'is the pickup into' if pickup else 'opens'} its "
                          f"{starts.get(ob.bar + 1) if pickup else starts.get(ob.bar)}: loop A's bar with "
                          "the word while B's intro comes in, then jump B to its word"))
    if not found:
        found.append(("echo out", "A echoes out on its word; B comes in on its word"))
    return " or ".join(t for t, _ in found), "; ".join(w for _, w in found)


def summary_line(c: Candidate) -> str:
    def where(o: Occurrence) -> str:
        pos = f"bar {o.bar} beat {o.beat % 4 + 1:.2f}" if o.beat is not None else f"{o.start:.1f}s"
        mark = "⚑" if o.cue else "✓" if o.confirmed else ("?" if o.confirmed is False else "")
        return f"{pos} {o.section} #{o.nth}{mark}"

    match = c.a_text if c.a_text == c.b_text else f"{c.a_text} / {c.b_text}"
    return (f"{c.score:.2f}  {c.kind:<11} {match!r:<24} A: {where(c.a):<28} B: {where(c.b):<28} → {c.technique}")


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
