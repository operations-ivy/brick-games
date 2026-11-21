"""games.brick.nozdormu.cloud: the catalog page.

LAN only: it serves the page, the catalog the scan wrote, the art and the
manuals. Nothing here touches the ROMs; the library on the drive is only read.

    GET  /                  the page
    GET  /api/catalog       catalog.json (systems, devices, games)
    GET  /media/<path>.avif art from the cache on NVMe (paths checked to stay
                            inside it); never the drive
    GET  /media/<system>/manuals/<id>.pdf
                            a game's manual, streamed: from the manual cache on
                            NVMe, or copied in from the library first (cache.py);
                            503 when it isn't cached and the drive is offline
    GET  /api/guides?q=<title words>
                            games with a local strategy guide (strategy.py), by title
    GET  /api/guides/wanted the games ranked 80+ without a complete guide (guide_filer.py)
    GET  /api/guides/<id>   a game's guide: source, license, grade, its sections
    GET  /api/guides/<id>/text
                            the guide, as Markdown
    GET  /api/guides/<id>/search?q=<words>
                            the sections that best match, with their text
                            (for an AI session: homelab-context's game-guide skill)
    GET  /health /ready /metrics

Caching (README "Caching"): the page and the catalog carry a strong ETag (the
catalog also Last-Modified) and answer a matching If-None-Match or
If-Modified-Since with 304; both are gzipped once, in memory, for clients that
accept it. The catalog is re-read and re-hashed only when the file changes
(inode, mtime or size), not per request. Art has an ETag from its mtime and
size; a request with `?v=<content hash>` (the scan writes those URLs into the
catalog) is cached for a year as immutable, since changed art gets a new `v`.
Manuals the same way, their ETag from the original's mtime and size.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs

import brick_observe
from brick_observe import metrics as observe_metrics
from prometheus_client import Counter

from . import strategy
from .cache import ManualCache, Offline

log = brick_observe.get_logger(__name__)
PAGE_FILE = Path(__file__).parent / "static" / "index.html"
MEDIA_PATH = re.compile(r"^/media/(?P<rel>[a-z0-9]+/(?:Named_(?:Boxarts|Titles|Snaps)/[0-9a-f]{16}\.(?P<art>avif|png)"
                        r"|manuals/[0-9a-f]{16}\.(?P<manual>pdf)))$")
MAX_GUIDE_HITS = 50  # games /api/guides?q= lists at most
GUIDE_PATH = re.compile(r"^/api/guides/(?P<id>[0-9a-f]{16})(?P<what>/text|/search)?$")
MEDIA_TYPES = {"avif": "image/avif", "png": "image/png", "pdf": "application/pdf"}
STREAM_CHUNK = 256 * 1024  # media is sent in pieces: a manual can be tens of MB
REQUESTS = Counter("games_web_requests_total", "Requests, by route and status", ["route", "status"])
SENT_BYTES = Counter("games_web_response_bytes_total", "Body bytes sent, by route and content encoding",
                     ["route", "encoding"])
MEDIA = Counter("games_media_requests_total", "Media requests, by kind and the tier that answered "
                "(hot: NVMe, origin: the drive, offline: the drive wasn't there, missing: no such file)",
                ["kind", "tier"])
NO_CATALOG = b'{"error": "no catalog yet: the first scan has not finished"}'
MEDIA_CACHE = "public, max-age=604800"  # art asked for without a version
MEDIA_IMMUTABLE = "public, max-age=31536000, immutable"
OFFLINE = b"the drive with the manuals is offline; try again later"
GZIP_MIN = 1024  # smaller bodies aren't worth it
MANUAL_CACHE_BYTES = 2 * 2**30  # the manual cache's default size (MANUAL_CACHE_MB)


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
    manuals: ManualCache
    # Keep-alive for Traefik's connections (art comes 120 tiles at a time);
    # an idle connection is dropped after a minute.
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, *args: object) -> None:  # quiet: requests are counted in /metrics instead
        pass

    def _start(self, status: int, kind: str | None, length: int | None, cache: str,
               headers: dict[str, str] | None) -> None:
        self.send_response(status)
        if kind:
            self.send_header("Content-Type", kind)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _respond(self, status: int, body: bytes, kind: str | None, route: str, cache: str = "no-cache",
                 headers: dict[str, str] | None = None, encoding: str = "identity") -> None:
        self._start(status, kind, None if status == 304 else len(body), cache, headers)
        if self.command != "HEAD" and status != 304:
            self.wfile.write(body)
            SENT_BYTES.labels(route, encoding).inc(len(body))
        REQUESTS.labels(route, str(status)).inc()

    def _stream(self, f: BinaryIO, size: int, kind: str, route: str, cache: str, headers: dict[str, str]) -> None:
        """A 200 with a file's bytes sent in pieces, not read whole (a manual
        can be tens of MB; the pod has 256 MiB)."""
        self._start(200, kind, size, cache, headers)
        if self.command != "HEAD":
            shutil.copyfileobj(f, self.wfile, STREAM_CHUNK)
            SENT_BYTES.labels(route, "identity").inc(size)
        REQUESTS.labels(route, "200").inc()

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
        elif m := GUIDE_PATH.match(path):
            self._guide(m["id"], m["what"] or "", query_q(query))
        elif m := MEDIA_PATH.match(path):
            # parse_qs drops blank values: a bare ?v= is not a versioned URL.
            versioned = "v" in parse_qs(query)
            if m["manual"]:
                self._manual(m["rel"], versioned)
            else:
                self._media(m["rel"], MEDIA_TYPES[m["art"]], versioned)
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

    def _media(self, rel: str, content_type: str, versioned: bool) -> None:
        media = (self.data / "media").resolve()
        file = (media / rel).resolve()
        if not file.is_relative_to(media):
            self._respond(404, b"not found", "text/plain", "/media")
            return
        try:
            f = file.open("rb")
        except (FileNotFoundError, IsADirectoryError):
            MEDIA.labels("art", "missing").inc()
            self._respond(404, b"not found", "text/plain", "/media")
            return
        MEDIA.labels("art", "hot").inc()
        self._send_file(f, content_type, versioned)

    def _manual(self, rel: str, versioned: bool) -> None:
        system, _, name = rel.partition("/manuals/")
        try:
            f, tier = self.manuals.open(system, name.removesuffix(".pdf"))
        except Offline as e:
            MEDIA.labels("manual", "offline").inc()
            log.warning("manual not served: the drive is offline", manual=rel, error=str(e))
            self._respond(503, OFFLINE, "text/plain", "/media", headers={"Retry-After": "300"})
            return
        except FileNotFoundError:
            MEDIA.labels("manual", "missing").inc()
            self._respond(404, b"not found", "text/plain", "/media")
            return
        MEDIA.labels("manual", tier).inc()
        self._send_file(f, MEDIA_TYPES["pdf"], versioned)

    def _send_file(self, f: BinaryIO, content_type: str, versioned: bool) -> None:
        with f:
            st = os.fstat(f.fileno())
            tag = f"{st.st_mtime_ns:x}-{st.st_size:x}"
            cache = MEDIA_IMMUTABLE if versioned else MEDIA_CACHE
            headers = {"ETag": f'"{tag}"', "Last-Modified": formatdate(st.st_mtime, usegmt=True)}
            if self._not_modified(tag, st.st_mtime):
                self._respond(304, b"", None, "/media", cache, headers)
                return
            self._stream(f, st.st_size, content_type, "/media", cache, headers)

    def _guide_files(self, game_id: str) -> tuple[Path, Path]:
        store = self.data / "strategy"
        return store / f"{game_id}.json", store / f"{game_id}.md"

    def _guide(self, game_id: str, what: str, q: str) -> None:
        meta_file, md_file = self._guide_files(game_id)
        try:
            if what == "/text":
                st = md_file.stat()
                self._send_entity(Entity.of(md_file.read_bytes(), st.st_mtime), "text/markdown; charset=utf-8",
                                  "/api/guides")
            elif what == "/search":
                meta = json.loads(meta_file.read_text())
                found = strategy.search(md_file.read_text(), meta["sections"], q)
                body = json.dumps({"game": meta["game"], "grade": meta["grade"], "source": meta["source"],
                                   "license": meta["license"], "q": q, "sections": found}, ensure_ascii=False)
                self._send_entity(Entity.of(body.encode()), "application/json", "/api/guides")
            else:
                st = meta_file.stat()
                self._send_entity(Entity.of(meta_file.read_bytes(), st.st_mtime), "application/json", "/api/guides")
        except FileNotFoundError:
            self._respond(404, b'{"error": "no local guide for this game"}', "application/json", "/api/guides")
        except (ValueError, KeyError) as e:  # a guide file the scan didn't finish, or an old format
            log.warning("guide unreadable", game=game_id, error=f"{type(e).__name__}: {e}")
            self._respond(500, b'{"error": "this guide is unreadable"}', "application/json", "/api/guides")

    def _wanted(self, path: str) -> None:
        try:
            f = self.data / "strategy" / "wanted.json"
            self._send_entity(Entity.of(f.read_bytes(), f.stat().st_mtime), "application/json", path)
        except FileNotFoundError:
            self._respond(503, b'{"error": "no list yet: the next scan writes it"}', "application/json", path)

    def _find_guides(self, path: str) -> None:
        """Games with a local guide whose title has every word of ?q=."""
        _, _, query = self.path.partition("?")
        words = query_q(query).lower().split()
        try:
            games = json.loads(self.catalog.get(self.data / "catalog.json").body)["games"]
        except FileNotFoundError:
            self._respond(503, NO_CATALOG, "application/json", path)
            return
        found = []
        for g in games:
            title = g["title"].lower()
            if g.get("strategy") and all(w in title for w in words):
                found.append({"id": g["id"], "title": g["title"], "system": g["system"], **g["strategy"]})
                if len(found) == MAX_GUIDE_HITS:
                    break
        self._send_entity(Entity.of(json.dumps(found, ensure_ascii=False).encode()), "application/json", path)

    def _health(self, path: str) -> None:
        ok = path == "/health" or (self.data / "catalog.json").exists()
        self._respond(200 if ok else 503, b"ok" if ok else b"no catalog yet", "text/plain", path)

    def _metrics(self, path: str) -> None:
        body, kind = observe_metrics.exposition()
        self._respond(200, body, kind, path)


ROUTES = {"/": Handler._page, "/api/catalog": Handler._catalog, "/api/guides": Handler._find_guides,
          "/api/guides/wanted": Handler._wanted,
          "/health": Handler._health, "/ready": Handler._health, "/metrics": Handler._metrics}


def query_q(query: str) -> str:
    """A query string's ?q= (blank when it has none)."""
    return parse_qs(query).get("q", [""])[0]


def make_handler(data: Path, lib: Path | None = None, manual_cache: int = MANUAL_CACHE_BYTES) -> type[Handler]:
    """A Handler class bound to one data directory and library, with its own
    catalog and manual caches."""
    manuals = ManualCache(data / "manual-cache", lib or data / "library", manual_cache)
    return type("BoundHandler", (Handler,), {"data": data, "catalog": FileCache(), "manuals": manuals})


def serve(data: Path, port: int, lib: Path, manual_cache: int = MANUAL_CACHE_BYTES) -> None:
    server = ThreadingHTTPServer(("", port), make_handler(data, lib, manual_cache))
    log.info("serving", port=port, data=str(data), library=str(lib), manual_cache_bytes=manual_cache)
    server.serve_forever()
