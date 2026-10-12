"""Lyrics from LRCLIB (lrclib.net): free, no key, often line-timed (LRC).

`lookup(artist, title, album, duration)` tries LRCLIB's exact match first
(artist + title + album + duration within a couple of seconds), then its
search, keeping the closest duration. Returns the record (a dict with
`syncedLyrics` and/or `plainLyrics`) or None.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://lrclib.net/api"
USER_AGENT = "dj-segue/0.1 (personal DJ project)"
# A search hit whose duration is further off than this is another edit.
MAX_DURATION_DIFF_SEC = 4.0

_LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")
# Tags that aren't part of a song's title: "[Clean]", "(feat. X)", "(Radio Version)".
_TITLE_NOISE = re.compile(
    r"\s*[\[(](?:clean|explicit|radio (?:version|edit)|feat\.?[^)\]]*|ft\.?[^)\]]*)[\])]",
    re.IGNORECASE,
)


def clean_title(title: str) -> str:
    return _TITLE_NOISE.sub("", title).strip()


def lookup(artist: str, title: str, album: str | None, duration: float) -> dict | None:
    title = clean_title(title)
    params = {"artist_name": artist, "track_name": title, "duration": round(duration)}
    if album:
        params["album_name"] = clean_title(album)
    try:
        return _get("/get?" + urllib.parse.urlencode(params))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    hits = _get("/search?" + urllib.parse.urlencode({"artist_name": artist, "track_name": title}))
    hits = [
        h for h in hits
        if not h.get("instrumental") and (h.get("syncedLyrics") or h.get("plainLyrics"))
        and abs((h.get("duration") or 0) - duration) <= MAX_DURATION_DIFF_SEC
    ]
    # Prefer line-timed lyrics, then the closest duration.
    hits.sort(key=lambda h: (not h.get("syncedLyrics"), abs(h["duration"] - duration)))
    return hits[0] if hits else None


def _get(path: str):
    req = urllib.request.Request(API + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def synced_lines(record: dict) -> list[tuple[float, str]]:
    """(start second, text) per LRC line, in order; empty lines kept (they
    end the previous line)."""
    out = []
    for line in (record.get("syncedLyrics") or "").splitlines():
        m = _LRC_LINE.match(line.strip())
        if m:
            out.append((int(m[1]) * 60 + float(m[2]), m[3].strip()))
    return out


def plain_lines(record: dict) -> list[str]:
    """Lyric lines without blank lines or section headers like "[Chorus]"."""
    return [
        s for s in (line.strip() for line in (record.get("plainLyrics") or "").splitlines())
        if s and not re.fullmatch(r"\[.*\]", s)
    ]
