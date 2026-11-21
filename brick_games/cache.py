"""The manual cache: the manuals people open, on the NVMe data volume.

Three tiers between the drive and a browser (README "Caching"):

- the library on the drive (library.py): every original, read only on a miss;
- this cache, DATA/manual-cache/<system>/<game id>.pdf, at most `limit`
  bytes, the least recently opened dropped first (art needs no such cache:
  the scan keeps every picture's AVIF on NVMe, ~150 MB in all);
- the browser: versioned URLs, cached for a year as immutable (web.py).

A miss copies the manual whole into the cache, then serves the copy, so a
second reader never waits on the drive. The copy keeps the original's mtime,
so its ETag and the catalog's ?v= stay the same. With the drive offline, what
is cached still opens; the rest is Offline (web.py answers 503). A manual
bigger than the whole cache is streamed from the drive and not kept.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import BinaryIO

import brick_observe
from prometheus_client import Gauge, Histogram

from . import library

log = brick_observe.get_logger(__name__)
CACHE_BYTES = Gauge("games_manual_cache_bytes", "Bytes in the manual cache on NVMe")
CACHE_LIMIT = Gauge("games_manual_cache_limit_bytes", "The manual cache's size limit")
ORIGIN_SECONDS = Histogram("games_manual_origin_copy_seconds",
                           "Copying one manual from the drive into the cache",
                           buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))


class Offline(OSError):
    """The drive isn't there, so a manual that isn't cached can't be read."""


class ManualCache:
    def __init__(self, root: Path, lib: Path, limit: int) -> None:
        self.root, self.lib, self.limit = root, lib, limit
        self._lock = threading.Lock()  # one copy or eviction at a time
        CACHE_LIMIT.set(limit)
        CACHE_BYTES.set(self._size())

    def _size(self) -> int:
        return sum(f.stat().st_size for f in self._files())

    def _files(self) -> list[Path]:
        try:
            return [f for f in self.root.glob("*/*.pdf") if f.is_file()]
        except OSError:
            return []

    def open(self, system: str, game_id: str) -> tuple[BinaryIO, str]:
        """The manual, opened, and the tier it came from ("hot" or "origin").
        FileNotFoundError when the game has none; Offline without the drive."""
        hot = self.root / system / f"{game_id}.pdf"
        try:
            f = hot.open("rb")
        except FileNotFoundError:
            pass
        else:
            self._touch(hot)
            return f, "hot"
        origin = self._origin(system, game_id)
        if origin.stat().st_size > self.limit:
            return origin.open("rb"), "origin"
        with self._lock:
            if not hot.exists():  # another request may have copied it meanwhile
                self._copy(origin, hot)
                self._evict(keep=hot)
        return hot.open("rb"), "origin"

    def _origin(self, system: str, game_id: str) -> Path:
        if not self.lib.is_dir():
            raise Offline("the drive is offline")
        try:
            folder = library.read_index(self.lib, system).get(game_id)
        except (OSError, ValueError) as e:
            raise Offline(f"media.json unreadable: {e}") from e
        if folder is None:
            raise FileNotFoundError(game_id)
        origin = self.lib / system / "media" / folder / library.MANUAL_FILE
        if not origin.is_file():
            raise FileNotFoundError(str(origin))
        return origin

    def _copy(self, origin: Path, hot: Path) -> None:
        tmp = hot.with_name(f".{hot.name}.part")
        start = time.monotonic()
        try:
            hot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, tmp)
            tmp.replace(hot)
        except OSError as e:  # the drive went away mid-copy, most likely
            tmp.unlink(missing_ok=True)
            raise Offline(f"copy from the drive failed: {e}") from e
        ORIGIN_SECONDS.observe(time.monotonic() - start)

    def _touch(self, path: Path) -> None:
        """Opened now: the access time is the LRU's clock (set by hand, since the
        volume may be mounted noatime); the mtime, the ETag's, stays."""
        try:
            st = path.stat()
            os.utime(path, ns=(time.time_ns(), st.st_mtime_ns))
        except OSError:
            pass

    def _evict(self, keep: Path) -> None:
        files = []
        for f in self._files():
            try:
                st = f.stat()
            except FileNotFoundError:
                continue
            files.append((st.st_atime_ns, st.st_size, f))
        total = sum(size for _, size, _ in files)
        for _, size, f in sorted(files, key=lambda t: t[0]):
            if total <= self.limit:
                break
            if f == keep:
                continue
            f.unlink(missing_ok=True)
            total -= size
            log.info("manual dropped from the cache", file=f"{f.parent.name}/{f.name}", bytes=size)
        CACHE_BYTES.set(total)
