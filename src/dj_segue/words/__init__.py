"""Word timing (M6 W1): when each word is sung, for planning on words.

`words_for(audio_path)`:

  1. Lyrics from LRCLIB (artist / title / album from the file's tags, or
     given). Line-timed lyrics align line by line; plain lyrics as one span.
  2. The vocals stem (M3's separation, cached) is aligned to the lyrics with
     wav2vec2 (`align`): every word gets a start, end and score.
  3. Whisper transcribes the same stem (`transcribe`), and its text is aligned
     the same way: what's actually sung, ad-libs included.
  4. `merge`: lyric words, each `confirmed` if Whisper heard it there too,
     plus Whisper's words where the lyrics have none. No lyrics → Whisper's.

Each step is cached in `.cache/words/<sha1 of the audio file>/` (git-ignored;
DJ_SEGUE_WORDS_CACHE overrides the folder): `lrclib.json` (the LRCLIB record,
or a miss), `lyrics.json` and `whisper.json` (aligned words), `words.json`
(merged). Lyrics are copyrighted: they stay in the cache, never in a repo.

Needs `pip install -e '.[stems,words]'`.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dj_segue.words.align import Aligned
from dj_segue.words.merge import Word, merge

CACHE_ENV = "DJ_SEGUE_WORDS_CACHE"
# Bump when aligned or merged results would change.
VERSION = 2
# An alignment score below this is weak: on real tracks a third of such words
# were confirmed by Whisper, against four in five above 0.4 (2026-10-11).
WEAK_SCORE = 0.1


@dataclass
class WordTiming:
    audio: str
    audio_sha1: str
    lyrics: dict | None  # {"id", "artist", "title", "synced"} of the LRCLIB record used
    whisper: bool  # whether Whisper's pass is in
    words: list[Word] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "version": VERSION, "audio": self.audio, "audio_sha1": self.audio_sha1,
            "lyrics": self.lyrics, "whisper": self.whisper,
            "words": [w.to_json() for w in self.words],
        }

    @classmethod
    def from_json(cls, d: dict) -> WordTiming:
        return cls(d["audio"], d["audio_sha1"], d["lyrics"], d["whisper"],
                   [Word(**w) for w in d["words"]])


def words_for(
    audio_path: Path,
    *,
    artist: str | None = None,
    title: str | None = None,
    whisper: bool = True,
    refresh: bool = False,
    log=print,
) -> WordTiming:
    from dj_segue.annotate import file_sha1

    audio_path = Path(audio_path)
    sha = file_sha1(audio_path)
    folder = cache_dir() / sha
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "words.json"
    if out.exists() and not refresh:
        d = json.loads(out.read_text())
        if d.get("version") == VERSION and (d["whisper"] or not whisper):
            return WordTiming.from_json(d)

    record = _lyrics_record(folder, audio_path, artist, title, refresh, log)
    vocals = None

    def stem():
        nonlocal vocals
        if vocals is None:
            vocals = _vocals16k(audio_path, log)
        return vocals

    lyrics = _cached_aligned(folder / "lyrics.json", refresh, lambda: _align_lyrics(record, stem(), log)) \
        if record else None
    heard = _cached_aligned(folder / "whisper.json", refresh, lambda: _whisper(stem(), log)) \
        if whisper else None
    meta = None
    if record:
        meta = {"id": record.get("id"), "artist": record.get("artistName"),
                "title": record.get("trackName"), "synced": bool(record.get("syncedLyrics"))}
    wt = WordTiming(audio_path.name, sha, meta, whisper, merge(lyrics, heard))
    _write(out, wt.to_json())
    return wt


def summary(wt: WordTiming) -> dict:
    """Counts only — no lyrics (safe to print anywhere)."""
    lyr = [w for w in wt.words if w.source == "lyrics"]
    return {
        "lyrics": wt.lyrics and {k: wt.lyrics[k] for k in ("id", "synced")},
        "words": len(wt.words),
        "from_lyrics": len(lyr),
        "confirmed": sum(bool(w.confirmed) for w in lyr) if wt.whisper else None,
        "whisper_only": sum(w.source == "whisper" for w in wt.words),
        "low_score": sum(w.score < WEAK_SCORE for w in wt.words),
    }


def cache_dir() -> Path:
    root = os.environ.get(CACHE_ENV)
    if root:
        return Path(root).expanduser()
    project = Path(__file__).resolve().parents[3]  # src/dj_segue/words/
    if (project / "pyproject.toml").exists() and (project / "src" / "dj_segue").is_dir():
        return project / ".cache" / "words"
    return Path.home() / ".cache" / "dj-segue" / "words"


# -- steps ---------------------------------------------------------------


def _lyrics_record(folder: Path, audio_path: Path, artist, title, refresh, log) -> dict | None:
    from dj_segue.words import lrclib

    path = folder / "lrclib.json"
    if path.exists() and not refresh:
        d = json.loads(path.read_text())
        return d.get("record")
    tags = read_tags(audio_path)
    artist, title = artist or tags.get("artist"), title or tags.get("title")
    if not (artist and title):
        log("words: no artist/title (tags or --artist/--title); skipping lyrics")
        return None
    record = lrclib.lookup(artist, title, tags.get("album"), tags["duration"])
    log(f"words: LRCLIB {'found ' + str(record['id']) if record else 'has nothing'} "
        f"for {artist} / {lrclib.clean_title(title)}")
    _write(path, {"query": {"artist": artist, "title": title}, "record": record})
    return record


def _align_lyrics(record: dict, audio16k, log) -> list[Aligned]:
    from dj_segue.words import lrclib
    from dj_segue.words.align import SAMPLE_RATE, align

    dur = len(audio16k) / SAMPLE_RATE
    rows = lrclib.synced_lines(record)
    if rows:
        spans = []
        for i, (t, text) in enumerate(rows):
            end = rows[i + 1][0] if i + 1 < len(rows) else dur
            if text:  # a little slack: LRC times are by ear
                spans.append((max(0.0, t - 0.3), min(dur, end + 0.3), text))
    else:
        spans = [(0.0, dur, " ".join(lrclib.plain_lines(record)))]
    log(f"words: aligning lyrics ({len(spans)} span{'s' * (len(spans) != 1)})…")
    return align(audio16k, spans)


def _whisper(audio16k, log) -> list[Aligned]:
    from dj_segue.words.align import align
    from dj_segue.words.transcribe import transcribe

    log("words: transcribing the vocals with Whisper (~30 s; cached after)…")
    return align(audio16k, transcribe(audio16k))


def _vocals16k(audio_path: Path, log):
    import numpy as np
    import soundfile as sf

    from dj_segue.stems import is_cached, stems_for
    from dj_segue.words.align import to_mono_16k

    # Read exactly as the preprocessor does, so the stem cache is shared.
    audio, sr = sf.read(str(audio_path.resolve()), dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = audio.repeat(2, axis=1)
    audio = np.ascontiguousarray(audio[:, :2])
    if not is_cached(audio, sr):
        log("words: separating stems (~40 s; cached after)…")
    return to_mono_16k(stems_for(audio, sr)["vocals"], sr)


def _cached_aligned(path: Path, refresh: bool, compute) -> list[Aligned]:
    if path.exists() and not refresh:
        d = json.loads(path.read_text())
        if d.get("version") == VERSION:
            return [Aligned(**w) for w in d["words"]]
    words = compute()
    _write(path, {"version": VERSION, "words": [asdict(w) for w in words]})
    return words


def read_tags(audio_path: Path) -> dict:
    """artist / title / album (when tagged) and duration in seconds."""
    import mutagen

    f = mutagen.File(str(audio_path), easy=True)
    tags = {"duration": float(f.info.length) if f is not None else 0.0}
    if f is not None and f.tags:
        for k in ("artist", "title", "album"):
            if f.tags.get(k):
                tags[k] = f.tags[k][0]
    return tags


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, path)
