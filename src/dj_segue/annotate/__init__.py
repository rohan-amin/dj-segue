"""`dj-segue annotate`: describe a track well enough to plan transitions from.

A local page (like `scrub`) where you check the beat grid, mark the
track's phrases (tap a number key at each section start while it plays),
label them, check the key by ear, and add notes. It starts from a draft:
the grid from analysis, phrases from the detected section starts, the
key from a chroma profile, and per-bar activity of each stem (vocals,
drums, bass, other — from the separator), which needs no editing.

The result is one JSON file per track in `annotations/` (see
`Annotation`). `summary()` turns it into the compact description a
planner reads: phrases with their energy and what's playing, the key, and
instrumental runs (where a mix can go in or out without vocals clashing).

Cues are named points you mark (`m` on the page): stored as exact seconds,
so re-gridding never moves them. A plan's `{"cue": name}` finds them when
the plan doesn't declare that cue itself.

Bars are on the annotation's own grid: bar N starts at beat 4N, beat 0 =
the first downbeat — the numbers a plan uses (with the annotation's bpm
declared on the track, if it differs from the detected one).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from dj_segue.time_math import TrackGrid

VERSION = 1
LABELS = ("intro", "verse", "pre-hook", "hook", "breakdown", "build", "drop", "outro", "bridge", "interlude", "riff", "?")
STEMS = ("vocals", "drums", "bass", "other")
# Stem activity spans this many dB below a stem's loud bars (0 at the
# bottom, 1 at the top): narrow enough that a quieter verse reads lower
# than a full hook (on real tracks vocals are rarely absent, just softer).
ACTIVITY_RANGE_DB = 18.0
# A stem counts as playing in a bar above this activity (0–1).
ACTIVE = 0.35
# Instrumental runs shorter than this aren't worth listing.
MIN_RUN_BARS = 4

KEY_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
# Camelot wheel numbers: major keys (B) and minor keys (A), by tonic.
CAMELOT_MAJOR = {0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7}
CAMELOT_MINOR = {9: 8, 4: 9, 11: 10, 6: 11, 1: 12, 8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7}
# Krumhansl–Kessler key profiles.
PROFILE_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
PROFILE_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


@dataclass
class Phrase:
    bar: int  # first bar
    bars: int  # length
    label: str = "?"
    note: str = ""


@dataclass
class Cue:
    """A named point in the track, marked on the page."""

    name: str
    second: float  # exact file time (on a beat if it was snapped)
    note: str = ""


@dataclass
class Annotation:
    audio: str  # path as given (relative to the project, usually)
    audio_sha1: str
    bpm: float
    anchor_sec: float  # time of beat 0
    duration_sec: float
    key: str | None = None  # e.g. "A minor"
    key_detected: str | None = None
    key_confidence: float = 0.0
    grid_checked: bool = False
    key_checked: bool = False
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    phrases: list[Phrase] = field(default_factory=list)
    # Per bar, 0–1 (automatic): energy, bass (low band), and each stem.
    bars: dict[str, list[float]] = field(default_factory=dict)
    breaks: list[list[int]] = field(default_factory=list)  # [first bar, end bar)
    suggestions: list[int] = field(default_factory=list)  # detected section starts (bars)
    cues: list[Cue] = field(default_factory=list)  # in time order
    version: int = VERSION

    @property
    def grid(self) -> TrackGrid:
        return TrackGrid(anchor_sec=self.anchor_sec, bpm=self.bpm)

    @property
    def n_bars(self) -> int:
        return len(self.bars.get("energy", []))

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Annotation":
        data = dict(data)
        data["phrases"] = [Phrase(**p) for p in data.get("phrases", [])]
        data["cues"] = [Cue(**c) for c in data.get("cues", [])]
        return cls(**data)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


def annotation_path(audio_path: Path, folder: Path) -> Path:
    return Path(folder) / f"{Path(audio_path).stem}.json"


def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save(ann: Annotation, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ann.to_json(), indent=1) + "\n")


def load(path: Path) -> Annotation:
    return Annotation.from_json(json.loads(Path(path).read_text()))


# ---------------------------------------------------------------------------
# the draft
# ---------------------------------------------------------------------------


def draft(audio_path: Path, audio_name: str, *, bpm: float | None = None, stems=None) -> Annotation:
    """An annotation from analysis alone. `stems(audio, sr) -> {name: audio}`
    defaults to the separator (cached; ~40 s per track the first time)."""
    from dj_segue.scrub import load_track

    track = load_track(audio_path, bpm)
    ann = Annotation(
        audio=audio_name,
        audio_sha1=file_sha1(audio_path),
        bpm=track.grid.bpm,
        anchor_sec=track.grid.anchor_sec,
        duration_sec=track.duration_sec,
    )
    audio, sr = _read(audio_path)
    key, conf = detect_key(audio, sr)
    ann.key = ann.key_detected = key
    ann.key_confidence = round(conf, 3)
    measure(ann, audio_path, audio, sr, stems)
    ann.phrases = phrases_from(ann.suggestions, ann.n_bars)
    return ann


def measure(ann: Annotation, audio_path: Path, audio=None, sr=None, stems=None) -> None:
    """(Re)compute the automatic per-bar data on the annotation's grid."""
    from dj_segue.analyzer.structure import analyze_structure

    if audio is None:
        audio, sr = _read(audio_path)
    structure = analyze_structure(audio_path, ann.grid)
    s = structure.to_json()
    ann.bars = {"energy": _r(s["energy"]), "bass": _r(s["bass"])}
    n = len(s["energy"])
    if stems is None:
        from dj_segue.stems import stems_for as stems
    for name, x in stems(audio, sr).items():
        ann.bars[name] = _r(stem_activity(x, sr, ann.grid, n))
    ann.breaks = [list(b) for b in s["breaks"]]
    ann.suggestions = list(s["boundaries"])


def regrid(ann: Annotation, audio_path: Path, bpm: float, anchor_sec: float, stems=None) -> None:
    """Move the annotation to a new grid: phrases keep their times (snapped
    to the new bars), and the per-bar data is measured again."""
    old = ann.grid
    timed = [(old.bar_to_seconds(p.bar), p) for p in ann.phrases]
    ann.bpm, ann.anchor_sec = bpm, anchor_sec
    measure(ann, audio_path, stems=stems)
    bar_sec = 4 * 60.0 / bpm
    moved: dict[int, Phrase] = {}
    for t, p in timed:  # two phrases landing on one bar: the first keeps it
        b = max(0, round((t - anchor_sec) / bar_sec))
        if b < ann.n_bars and b not in moved:
            moved[b] = Phrase(b, 0, p.label, p.note)
    ann.phrases = [moved[b] for b in sorted(moved)] or [Phrase(0, 0)]
    if ann.phrases[0].bar != 0:
        ann.phrases.insert(0, Phrase(0, 0))
    _fix_lengths(ann.phrases, ann.n_bars)


def phrases_from(starts: list[int], n_bars: int) -> list[Phrase]:
    bars = sorted({0, *[b for b in starts if 0 < b < n_bars]})
    out = [Phrase(b, 0) for b in bars]
    _fix_lengths(out, n_bars)
    return out


def _fix_lengths(phrases: list[Phrase], n_bars: int) -> None:
    for p, q in zip(phrases, phrases[1:] + [None]):
        p.bars = (q.bar if q else n_bars) - p.bar


def _read(path: Path):
    import soundfile as sf

    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception:  # e.g. mp3 on an old libsndfile
        import librosa

        y, sr = librosa.load(str(path), sr=None, mono=False)
        data = np.atleast_2d(y).T.astype(np.float32)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    return data[:, :2], int(sr)


def _r(xs) -> list[float]:
    return [round(float(x), 3) for x in xs]


def stem_activity(x: np.ndarray, sr: int, grid: TrackGrid, n_bars: int) -> np.ndarray:
    """Per-bar level of one stem, 0–1: RMS in dB, from ACTIVITY_RANGE_DB
    below the stem's loud bars (its 95th percentile) up to them. Silence
    and bleed land at 0; quiet ad-libs low; the loudest parts at 1."""
    mono = x.mean(axis=1) if x.ndim == 2 else x
    out = np.zeros(n_bars)
    for b in range(n_bars):
        a, e = (max(0, int(round(grid.bar_to_seconds(k) * sr))) for k in (b, b + 1))
        seg = mono[a:e]
        out[b] = np.sqrt(np.mean(seg ** 2)) if len(seg) else 0.0
    db = 20 * np.log10(np.maximum(out, 1e-9))
    top = np.percentile(db, 95) if n_bars else 0.0
    return np.clip((db - (top - ACTIVITY_RANGE_DB)) / ACTIVITY_RANGE_DB, 0.0, 1.0)


def detect_key(audio: np.ndarray, sr: int) -> tuple[str, float]:
    """(key name, confidence 0–1) from the track's average chroma against
    the Krumhansl–Kessler profiles. Confidence: how far the best key's
    correlation is ahead of the runner-up (relative major/minor excluded,
    since they share notes), scaled so ~0.5+ is usually right."""
    import librosa

    y = librosa.resample(audio.mean(axis=1), orig_sr=sr, target_sr=22050)
    harmonic = librosa.effects.harmonic(y)
    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=22050).mean(axis=1)
    scores = []
    for tonic in range(12):
        for mode, prof in (("major", PROFILE_MAJOR), ("minor", PROFILE_MINOR)):
            r = np.corrcoef(chroma, np.roll(prof, tonic))[0, 1]
            scores.append((r, tonic, mode))
    scores.sort(reverse=True)
    best_r, tonic, mode = scores[0]
    rel = (tonic + 9) % 12 if mode == "major" else (tonic + 3) % 12
    other = "minor" if mode == "major" else "major"
    rival = next(r for r, t, m in scores[1:] if not (t == rel and m == other))
    return f"{KEY_NAMES[tonic]} {mode}", float(np.clip((best_r - rival) * 5, 0.0, 1.0))


def camelot(key: str | None) -> str | None:
    """"A minor" → "8A"; None for an unknown key."""
    if not key:
        return None
    name, mode = key.split()
    tonic = KEY_NAMES.index(name)
    if mode == "major":
        return f"{CAMELOT_MAJOR[tonic]}B"
    return f"{CAMELOT_MINOR[tonic]}A"


# ---------------------------------------------------------------------------
# what a planner reads
# ---------------------------------------------------------------------------


def summary(ann: Annotation) -> dict:
    """A compact description: per phrase its bars (and beats), label,
    note, mean energy and how much each stem plays; runs of bars with no
    vocals; breaks; key (with Camelot) and tempo."""

    def mean(name: str, a: int, b: int) -> float:
        xs = ann.bars.get(name, [])[a:b]
        return round(float(np.mean(xs)), 2) if xs else 0.0

    phrases = []
    for p in ann.phrases:
        a, b = p.bar, p.bar + p.bars
        phrases.append({
            "bars": f"{a}–{b - 1}",
            "beats": f"{4 * a}–{4 * b - 1}",
            "length_bars": p.bars,
            "label": p.label,
            **({"note": p.note} if p.note else {}),
            "energy": mean("energy", a, b),
            **{s: mean(s, a, b) for s in STEMS if s in ann.bars},
        })
    def beat_of(sec: float) -> float:
        return (sec - ann.anchor_sec) * ann.bpm / 60.0

    vocals = ann.bars.get("vocals", [])
    runs, start = [], None
    for i, v in enumerate(vocals + [1.0]):
        if v < ACTIVE and start is None:
            start = i
        elif v >= ACTIVE and start is not None:
            if i - start >= MIN_RUN_BARS:
                runs.append(f"bars {start}–{i - 1} (beats {4 * start}–{4 * i - 1})")
            start = None
    return {
        "track": Path(ann.audio).name,
        "bpm": ann.bpm,
        "grid_checked": ann.grid_checked,
        "key": ann.key,
        "camelot": camelot(ann.key),
        "key_checked": ann.key_checked,
        "key_confidence": ann.key_confidence,
        "duration_sec": round(ann.duration_sec, 1),
        "bars": ann.n_bars,
        "tags": ann.tags,
        **({"notes": ann.notes} if ann.notes else {}),
        "phrases": phrases,
        "no_vocals": runs,
        "breaks": [f"bars {a}–{b - 1}" for a, b in ann.breaks],
        "cues": [
            {"name": c.name, "beat": round(beat_of(c.second), 2), "bar": int(beat_of(c.second) // 4),
             "second": round(c.second, 3), **({"note": c.note} if c.note else {})}
            for c in ann.cues
        ],
    }


def clean_cues(raw: list[dict], duration_sec: float) -> list[Cue]:
    """Cues from the page: names trimmed, non-empty and unique; times
    inside the track; in time order."""
    cues, seen = [], set()
    for c in raw:
        name = str(c.get("name", "")).strip()
        if not name:
            raise ValueError("a cue needs a name")
        if name in seen:
            raise ValueError(f"two cues are called {name!r}")
        seen.add(name)
        sec = float(c["second"])
        if not 0 <= sec <= duration_sec:
            raise ValueError(f"cue {name!r} at {sec:.2f}s is outside the track")
        cues.append(Cue(name, round(sec, 4), str(c.get("note", ""))))
    return sorted(cues, key=lambda c: c.second)


def annotation_cues(audio_path: Path, folder: Path = Path("annotations")) -> dict[str, float] | None:
    """name → second from the track's annotation, or None when there's no
    annotation for this exact file (one for a different version doesn't count)."""
    path = annotation_path(audio_path, folder)
    if not path.exists():
        return None
    ann = load(path)
    if ann.audio_sha1 != file_sha1(audio_path):
        return None
    return {c.name: c.second for c in ann.cues}
