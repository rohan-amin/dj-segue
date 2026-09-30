"""`dj-segue scrub`: a local web page for finding plan positions by ear.

Serves one page (`page.html`), the audio file, and its analysis (beat grid +
bar-level structure) from a small stdlib HTTP server on 127.0.0.1. The page
decodes and plays the audio with Web Audio, so all transport logic (seek in
beats, loops, click, marks) lives in the page; Python only analyzes.

Beat numbers use the plan grid (beat 0 = the track's first downbeat, bar N =
beat 4N), so a number on the page is the number to write in a plan.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from dj_segue.analyzer.structure import TrackStructure, analyze_structure
from dj_segue.time_math import TrackGrid


@dataclass(frozen=True)
class ScrubTrack:
    audio_path: Path
    grid: TrackGrid
    duration_sec: float
    structure: TrackStructure
    start_beat: int = 0

    def analysis_json(self) -> dict:
        return {
            "name": self.audio_path.name,
            "bpm": self.grid.bpm,
            "anchor_sec": self.grid.anchor_sec,
            "duration_sec": self.duration_sec,
            "start_beat": self.start_beat,
            "bars": self.structure.to_json(),
        }


def load_track(audio_path: Path, bpm: float | None = None, start_beat: int = 0) -> ScrubTrack:
    """Beat grid (cached next to the file, like preprocess) + structure."""
    from dj_segue.analyzer import analyze_audio, is_fresh, load_cache, write_cache

    audio_path = Path(audio_path).resolve()
    if is_fresh(audio_path):
        analysis = load_cache(audio_path).to_analysis()
    else:
        analysis = analyze_audio(audio_path)
        write_cache(audio_path, analysis)
    tempo = bpm if bpm is not None else analysis.detected_bpm
    if tempo is None:
        raise ValueError(f"{audio_path.name}: no beat detected; pass --bpm")
    grid = TrackGrid(anchor_sec=analysis.grid_anchor(tempo), bpm=tempo)
    return ScrubTrack(
        audio_path=audio_path,
        grid=grid,
        duration_sec=analysis.duration_sec,
        structure=analyze_structure(audio_path, grid),
        start_beat=start_beat,
    )


def make_server(track: ScrubTrack, port: int = 0, on_mark=None) -> ThreadingHTTPServer:
    """An HTTP server for `track` on 127.0.0.1 (port 0 = any free port).
    `on_mark(beat)` is called when the page marks a beat."""
    page = resources.files(__package__).joinpath("page.html").read_bytes()
    analysis = json.dumps(track.analysis_json()).encode()
    audio_type = mimetypes.guess_type(track.audio_path.name)[0] or "application/octet-stream"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                self._send(page, "text/html; charset=utf-8")
            elif self.path == "/analysis":
                self._send(analysis, "application/json")
            elif self.path == "/audio":
                self._send(track.audio_path.read_bytes(), audio_type)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/mark":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                beat = int(json.loads(self.rfile.read(length))["beat"])
            except (ValueError, KeyError, TypeError):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if on_mark is not None:
                on_mark(beat)
            self._send(b"{}", "application/json")

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(track: ScrubTrack, port: int = 0, open_browser: bool = True) -> list[int]:
    """Serve until Ctrl-C. Prints each marked beat; returns them sorted."""
    import webbrowser

    marks: set[int] = set()

    def on_mark(beat: int) -> None:
        if beat not in marks:
            marks.add(beat)
            print(f"  marked beat {beat}", flush=True)

    server = make_server(track, port, on_mark)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"scrub: {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return sorted(marks)
