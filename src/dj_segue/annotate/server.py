"""The annotate page's server: one track, its annotation, the audio.

GET  /             the page
GET  /annotation   the annotation (+ "summary", "labels")
GET  /audio        the track
GET  /words        the track's word timing from `dj-segue words` ({"available": false} if not run)
POST /save         {key, key_checked, grid_checked, tags, notes, phrases}
POST /grid         {bpm, anchor_sec}: move to a new grid (re-measures bars)
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from dj_segue.annotate import (
    KEY_NAMES,
    LABELS,
    Annotation,
    Phrase,
    _fix_lengths,
    camelot,
    regrid,
    save,
    summary,
)


class AnnotateSession:
    def __init__(self, ann: Annotation, audio_path: Path, out_path: Path, stems=None) -> None:
        self.ann, self.audio_path, self.out_path = ann, Path(audio_path), Path(out_path)
        self._stems = stems
        self._cache: dict | None = None
        self._words: dict | None = None
        self.lock = threading.Lock()

    def stems(self, audio, sr):
        """The separator's stems, kept in memory across re-grids."""
        if self._cache is None:
            if self._stems is None:
                from dj_segue.stems import stems_for as fn
            else:
                fn = self._stems
            self._cache = fn(audio, sr)
        return self._cache

    def words(self) -> dict:
        """Word timing from the words cache (never computed here: it's slow)."""
        if self._words is None:
            from dj_segue.words import cached, summary

            wt = cached(self.audio_path)
            self._words = {"available": False} if wt is None else {
                "available": True, **wt.to_json(), "summary": summary(wt),
            }
        return self._words

    def payload(self) -> dict:
        keys = [f"{k} {m}" for m in ("major", "minor") for k in KEY_NAMES]
        return {
            **self.ann.to_json(),
            "summary": summary(self.ann),
            "labels": list(LABELS),
            "keys": [{"key": k, "camelot": camelot(k)} for k in keys],
            "saved_to": str(self.out_path),
        }

    def apply(self, body: dict) -> None:
        """Take the page's edits and save."""
        ann = self.ann
        n = ann.n_bars
        phrases = sorted(
            (Phrase(int(p["bar"]), 0, str(p.get("label", "?")), str(p.get("note", "")))
             for p in body.get("phrases", []) if 0 <= int(p["bar"]) < n),
            key=lambda p: p.bar,
        )
        uniq = []
        for p in phrases:
            if p.label not in LABELS:
                raise ValueError(f"unknown label {p.label!r}")
            if not uniq or uniq[-1].bar != p.bar:
                uniq.append(p)
        if not uniq or uniq[0].bar != 0:
            uniq.insert(0, Phrase(0, 0))
        _fix_lengths(uniq, n)
        ann.phrases = uniq
        key = body.get("key")
        if key is not None and camelot(key) is None:
            raise ValueError(f"unknown key {key!r}")
        ann.key = key
        ann.key_checked = bool(body.get("key_checked", ann.key_checked))
        ann.grid_checked = bool(body.get("grid_checked", ann.grid_checked))
        ann.tags = [str(t).strip() for t in body.get("tags", ann.tags) if str(t).strip()]
        ann.notes = str(body.get("notes", ann.notes))
        save(ann, self.out_path)

    def regrid(self, bpm: float, anchor_sec: float) -> None:
        if not (20 <= bpm <= 300):
            raise ValueError(f"bpm {bpm} is out of range")
        regrid(self.ann, self.audio_path, bpm, anchor_sec, stems=self.stems)


def make_server(session: AnnotateSession, port: int = 0) -> ThreadingHTTPServer:
    page = resources.files(__package__).joinpath("page.html").read_bytes()
    audio_type = mimetypes.guess_type(session.audio_path.name)[0] or "application/octet-stream"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                self._send(page, "text/html; charset=utf-8")
            elif self.path == "/annotation":
                with session.lock:
                    self._json(session.payload())
            elif self.path == "/words":
                self._json(session.words())
            elif self.path == "/audio":
                self._send(session.audio_path.read_bytes(), audio_type)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                with session.lock:
                    if self.path == "/save":
                        session.apply(body)
                    elif self.path == "/grid":
                        session.regrid(float(body["bpm"]), float(body["anchor_sec"]))
                    else:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self._json({"ok": True, **session.payload()})
            except (ValueError, KeyError, TypeError) as e:
                self._json({"ok": False, "error": str(e)})

        def _json(self, obj) -> None:
            self._send(json.dumps(obj).encode(), "application/json")

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(session: AnnotateSession, port: int = 0, open_browser: bool = True) -> None:
    import webbrowser

    server = make_server(session, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"annotate: {url}  (Ctrl-C to stop) — saves to {session.out_path}", flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

