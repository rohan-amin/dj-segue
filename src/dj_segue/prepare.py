"""`dj-segue prepare`: get a new track ready in one step.

For each audio file:

  1. Beat analysis (the `.beats` sidecar the preprocessor uses), so plans
     with it resolve at once.
  2. Word timing (`dj-segue words`: lyrics from LRCLIB aligned to the vocals
     stem, checked against Whisper) — which separates the stems first.
  3. An annotation draft in `annotations/` (grid, sections, key, stem
     activity), unless the track has one already; then `dj-segue annotate`
     opens straight onto it.

Every step is cached, so running it again only does what's missing. It
reports counts only: lyrics stay in the cache.
"""

from __future__ import annotations

from pathlib import Path

from dj_segue.analyzer import analyze_audio, is_fresh, load_cache, write_cache


def prepare(
    audio_path: Path,
    *,
    folder: Path = Path("annotations"),
    words: bool = True,
    whisper: bool = True,
    stems=None,
    log=print,
) -> dict:
    """Run the steps on one file; returns what was found (no lyrics)."""
    from dj_segue.annotate import annotation_path, draft, file_sha1, load, save
    from dj_segue.annotate import summary as ann_summary

    audio_path = Path(audio_path)
    out: dict = {"audio": audio_path.name}

    if is_fresh(audio_path):
        beats = load_cache(audio_path).to_analysis()
    else:
        log("  analyzing the beat…")
        beats = analyze_audio(audio_path)
        write_cache(audio_path, beats)
    out["bpm"] = beats.detected_bpm

    if words:
        try:
            from dj_segue.words import summary, words_for

            out["words"] = summary(words_for(audio_path, whisper=whisper, log=lambda m: log("  " + m)))
        except ImportError as e:
            out["words_error"] = f"word timing needs: pip install -e '.[stems,words]' ({e})"
        except OSError as e:  # e.g. LRCLIB unreachable
            out["words_error"] = str(e)

    path = annotation_path(audio_path, folder)
    if path.exists():
        ann = load(path)
        out["annotation"] = "kept" if ann.audio_sha1 == file_sha1(audio_path) else "for another version of the file"
    else:
        log("  drafting the annotation…")
        ann = draft(audio_path, str(audio_path), stems=stems)
        save(ann, path)
        out["annotation"] = "drafted"
    s = ann_summary(ann)
    out.update(annotation_path=str(path), annotated_bpm=s["bpm"], bars=s["bars"], key=s["key"],
               camelot=s["camelot"], key_confidence=s["key_confidence"],
               sections=len(s["phrases"]), labelled=sum(p["label"] != "?" for p in s["phrases"]),
               cues=len(s["cues"]))
    return out
