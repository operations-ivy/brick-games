"""games.brick.nozdormu.cloud: the catalog page.

LAN only and read-only: it serves the page, the catalog the scan wrote, and
the cached art. Nothing here touches the ROMs.

    GET  /                  the page
    GET  /api/catalog       catalog.json (systems, devices, games)
    GET  /media/<path>.png  art from the cache (paths checked to stay inside it)
    GET  /health /ready /metrics
"""

from __future__ import annotations

import functools
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import brick_observe
from brick_observe import metrics as observe_metrics
from prometheus_client import Counter

log = brick_observe.get_logger(__name__)
PAGE_FILE = Path(__file__).parent / "static" / "index.html"
MEDIA_PATH = re.compile(r"^/media/([a-z0-9]+/Named_(?:Boxarts|Titles|Snaps)/[0-9a-f]{16}\.png)$")
REQUESTS = Counter("games_web_requests_total", "Requests, by route and status", ["route", "status"])
NO_CATALOG = b'{"error": "no catalog yet: the first scan has not finished"}'


@functools.cache
def load_page() -> bytes:
    return PAGE_FILE.read_bytes()


class Handler(BaseHTTPRequestHandler):
    data: Path

    def log_message(self, *args: object) -> None:  # quiet: requests are counted in /metrics instead
        pass

    def _respond(self, status: int, body: bytes, kind: str, route: str, cache: str = "no-cache") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        REQUESTS.labels(route, str(status)).inc()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if route := ROUTES.get(path):
            route(self, path)
        elif m := MEDIA_PATH.match(path):
            self._media(m.group(1))
        else:
            self._respond(404, b"not found", "text/plain", "other")

    def _page(self, path: str) -> None:
        self._respond(200, load_page(), "text/html; charset=utf-8", path)

    def _catalog(self, path: str) -> None:
        try:  # read, not exists() then read: the scan may be renaming a new one in
            body = (self.data / "catalog.json").read_bytes()
        except FileNotFoundError:
            self._respond(503, NO_CATALOG, "application/json", path)
        else:
            self._respond(200, body, "application/json", path)

    def _media(self, rel: str) -> None:
        media = (self.data / "media").resolve()
        file = (media / rel).resolve()
        if file.is_relative_to(media) and file.is_file():
            self._respond(200, file.read_bytes(), "image/png", "/media", "public, max-age=604800")
        else:
            self._respond(404, b"not found", "text/plain", "/media")

    def _health(self, path: str) -> None:
        ok = path == "/health" or (self.data / "catalog.json").exists()
        self._respond(200 if ok else 503, b"ok" if ok else b"no catalog yet", "text/plain", path)

    def _metrics(self, path: str) -> None:
        body, kind = observe_metrics.exposition()
        self._respond(200, body, kind, path)


ROUTES = {"/": Handler._page, "/api/catalog": Handler._catalog, "/health": Handler._health,
          "/ready": Handler._health, "/metrics": Handler._metrics}


def make_handler(data: Path) -> type[Handler]:
    """A Handler class bound to one data directory."""
    return type("BoundHandler", (Handler,), {"data": data})


def serve(data: Path, port: int) -> None:
    server = ThreadingHTTPServer(("", port), make_handler(data))
    log.info("serving", port=port, data=str(data))
    server.serve_forever()
