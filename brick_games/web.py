"""games.brick.nozdormu.cloud: the catalog page.

LAN only and read-only: it serves the page, the catalog the scan wrote, and
the cached art. Nothing here touches the ROMs.

    GET  /                  the page
    GET  /api/catalog       catalog.json (systems, devices, games)
    GET  /media/<path>.png  art from the cache (paths checked to stay inside it)
    GET  /health /ready /metrics

Caching (README "Caching"): the page and the catalog carry a strong ETag (the
catalog also Last-Modified) and answer a matching If-None-Match or
If-Modified-Since with 304; both are gzipped once, in memory, for clients that
accept it. The catalog is re-read and re-hashed only when the file changes
(inode, mtime or size), not per request. Art has an ETag from its mtime and
size; a request with `?v=<content hash>` (the scan writes those URLs into the
catalog) is cached for a year as immutable, since changed art gets a new `v`.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import os
import re
import threading
from dataclasses import dataclass
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import brick_observe
from brick_observe import metrics as observe_metrics
from prometheus_client import Counter

log = brick_observe.get_logger(__name__)
PAGE_FILE = Path(__file__).parent / "static" / "index.html"
MEDIA_PATH = re.compile(r"^/media/([a-z0-9]+/Named_(?:Boxarts|Titles|Snaps)/[0-9a-f]{16}\.png)$")
REQUESTS = Counter("games_web_requests_total", "Requests, by route and status", ["route", "status"])
SENT_BYTES = Counter("games_web_response_bytes_total", "Body bytes sent, by route and content encoding",
                     ["route", "encoding"])
NO_CATALOG = b'{"error": "no catalog yet: the first scan has not finished"}'
MEDIA_CACHE = "public, max-age=604800"  # art asked for without a version
MEDIA_IMMUTABLE = "public, max-age=31536000, immutable"
GZIP_MIN = 1024  # smaller bodies aren't worth it


@dataclass(frozen=True)
class Entity:
    """One resource, ready to send: its bytes, gzipped once (None when that
    doesn't pay), a strong ETag and, if known, its mtime."""

    body: bytes
    gz: bytes | None
    tag: str
    modified: float | None = None

    @classmethod
    def of(cls, body: bytes, modified: float | None = None) -> Entity:
        gz = gzip.compress(body, 9, mtime=0) if len(body) >= GZIP_MIN else None
        return cls(body, gz if gz and len(gz) < len(body) else None,
                   hashlib.sha256(body).hexdigest()[:32], modified)

    def etag(self, gzipped: bool) -> str:
        # Each encoding is its own representation, so it has its own strong
        # ETag; an If-None-Match with either matches (same_tag).
        return f'"{self.tag}-gz"' if gzipped else f'"{self.tag}"'


class FileCache:
    """One file as an Entity, rebuilt only when the file changes. The scan
    renames a new catalog into place, so the open file's fstat says which
    version was read."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[int, int, int] | None = None
        self._entity: Entity | None = None

    def get(self, path: Path) -> Entity:
        with path.open("rb") as f:  # FileNotFoundError: no file yet
            st = os.fstat(f.fileno())
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            with self._lock:
                if key != self._key or self._entity is None:
                    self._entity = Entity.of(f.read(), st.st_mtime)
                    self._key = key
                    log.info("file cached", file=path.name, bytes=len(self._entity.body),
                             gzip_bytes=len(self._entity.gz or b""), etag=self._entity.tag)
                return self._entity


@functools.cache
def page_entity() -> Entity:
    # No Last-Modified: a reproducible image build may give every release's
    # file the same mtime, so the ETag alone says whether the page changed.
    return Entity.of(PAGE_FILE.read_bytes())


def accepts_gzip(header: str) -> bool:
    """Whether an Accept-Encoding header allows gzip (q=0 refuses it)."""
    weights: dict[str, float] = {}
    for part in header.lower().split(","):
        name, *params = (p.strip() for p in part.split(";"))
        weight = 1.0
        for p in params:
            if p.startswith("q="):
                try:
                    weight = float(p[2:])
                except ValueError:
                    weight = 0.0
        if name:
            weights[name] = weight
    for name in ("gzip", "x-gzip", "*"):
        if name in weights:
            return weights[name] > 0
    return False


def same_tag(header: str, tag: str) -> bool:
    """If-None-Match's weak comparison: any listed ETag (either encoding, W/ or not) is this tag."""
    for item in header.split(","):
        item = item.strip()
        if item == "*" or item.removeprefix("W/").strip('"').removesuffix("-gz") == tag:
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    data: Path
    catalog: FileCache
    # Keep-alive for Traefik's connections (art comes 120 tiles at a time);
    # an idle connection is dropped after a minute.
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, *args: object) -> None:  # quiet: requests are counted in /metrics instead
        pass

    def _respond(self, status: int, body: bytes, kind: str | None, route: str, cache: str = "no-cache",
                 headers: dict[str, str] | None = None, encoding: str = "identity") -> None:
        self.send_response(status)
        if kind:
            self.send_header("Content-Type", kind)
        if status != 304:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and status != 304:
            self.wfile.write(body)
            SENT_BYTES.labels(route, encoding).inc(len(body))
        REQUESTS.labels(route, str(status)).inc()

    def _not_modified(self, tag: str, modified: float | None) -> bool:
        """RFC 9110 13.2.2: If-None-Match wins; If-Modified-Since counts only without it."""
        if (inm := self.headers.get("If-None-Match")) is not None:
            return same_tag(inm, tag)
        if modified is not None and (ims := self.headers.get("If-Modified-Since")):
            try:
                return int(modified) <= parsedate_to_datetime(ims).timestamp()
            except (TypeError, ValueError):  # a date we can't read: send the whole thing
                return False
        return False

    def _send_entity(self, entity: Entity, kind: str, route: str) -> None:
        gzipped = entity.gz is not None and accepts_gzip(self.headers.get("Accept-Encoding", ""))
        headers = {"ETag": entity.etag(gzipped)}
        if entity.modified is not None:
            headers["Last-Modified"] = formatdate(entity.modified, usegmt=True)
        if entity.gz is not None:
            headers["Vary"] = "Accept-Encoding"
        if self._not_modified(entity.tag, entity.modified):
            self._respond(304, b"", None, route, headers=headers)
        elif gzipped and entity.gz is not None:
            self._respond(200, entity.gz, kind, route, headers=headers | {"Content-Encoding": "gzip"},
                          encoding="gzip")
        else:
            self._respond(200, entity.body, kind, route, headers=headers)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if route := ROUTES.get(path):
            route(self, path)
        elif m := MEDIA_PATH.match(path):
            self._media(m.group(1), "v" in parse_qs(query))
        else:
            self._respond(404, b"not found", "text/plain", "other")

    def _page(self, path: str) -> None:
        self._send_entity(page_entity(), "text/html; charset=utf-8", path)

    def _catalog(self, path: str) -> None:
        try:  # open, not exists() then open: the scan may be renaming a new one in
            entity = self.catalog.get(self.data / "catalog.json")
        except FileNotFoundError:
            self._respond(503, NO_CATALOG, "application/json", path)
        else:
            self._send_entity(entity, "application/json", path)

    def _media(self, rel: str, versioned: bool) -> None:
        media = (self.data / "media").resolve()
        file = (media / rel).resolve()
        if not file.is_relative_to(media):
            self._respond(404, b"not found", "text/plain", "/media")
            return
        try:
            f = file.open("rb")
        except (FileNotFoundError, IsADirectoryError):
            self._respond(404, b"not found", "text/plain", "/media")
            return
        with f:
            st = os.fstat(f.fileno())
            tag = f"{st.st_mtime_ns:x}-{st.st_size:x}"
            cache = MEDIA_IMMUTABLE if versioned else MEDIA_CACHE
            headers = {"ETag": f'"{tag}"', "Last-Modified": formatdate(st.st_mtime, usegmt=True)}
            if self._not_modified(tag, st.st_mtime):
                self._respond(304, b"", None, "/media", cache, headers)
            else:
                self._respond(200, f.read(), "image/png", "/media", cache, headers)

    def _health(self, path: str) -> None:
        ok = path == "/health" or (self.data / "catalog.json").exists()
        self._respond(200 if ok else 503, b"ok" if ok else b"no catalog yet", "text/plain", path)

    def _metrics(self, path: str) -> None:
        body, kind = observe_metrics.exposition()
        self._respond(200, body, kind, path)


ROUTES = {"/": Handler._page, "/api/catalog": Handler._catalog, "/health": Handler._health,
          "/ready": Handler._health, "/metrics": Handler._metrics}


def make_handler(data: Path) -> type[Handler]:
    """A Handler class bound to one data directory, with its own catalog cache."""
    return type("BoundHandler", (Handler,), {"data": data, "catalog": FileCache()})


def serve(data: Path, port: int) -> None:
    server = ThreadingHTTPServer(("", port), make_handler(data))
    log.info("serving", port=port, data=str(data))
    server.serve_forever()
