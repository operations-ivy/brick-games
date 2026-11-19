"""Game manuals: PDFs from the Internet Archive's per-system manual sets,
kept in DATA/media and served from the page (brick-games#1).

- Each system's `manuals` in systems.yaml names archive.org items that hold
  one PDF per game, named like the ROMs (No-Intro style; Neo Geo MVS by its
  short name, like the sets). An item's file list is
  archive.org/metadata/<item>; the Archive's own OCR copies (`_text.pdf`)
  are left out.
- Matching is by the exact key (ratings.key), an arcade set also by its
  short name; of several regions, the USA one (sources.best). Not by a longer
  name, as ratings do: names lose their punctuation, so "Phantasy Star II"
  would get "Phantasy Star II - Anne's Adventure", a different game, and a
  wrong manual is worse than the search link a game without one gets.
- A manual is fetched once, to media/<system>/manuals/<game id>.pdf. Later
  scans link the file on disk; the Archive is only asked when a game still
  has no manual.
- The first scan has a few GB to fetch, so a scan downloads for at most
  BUDGET seconds and the rest come on the nights after. A manual over
  MAX_BYTES, or a file that isn't a PDF, is skipped.
- Like ratings, manuals never fail the scan: anything that goes wrong is a
  warning in the log and a count in `results`, pushed as
  games_manual_fetches{result}.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

import brick_observe

from . import ratings, sources

log = brick_observe.get_logger(__name__)
METADATA = "https://archive.org/metadata/"
DOWNLOAD = "https://archive.org/download/"
BUDGET = 3600  # seconds of downloading per scan
MAX_BYTES = 100 * 1024 * 1024
CHUNK = 1024 * 1024
PDF = b"%PDF"
TIMEOUT = 120
VERSION_LEN = 8
# What happened to each game's manual in one scan:
#   ok        downloaded
#   cached    already on disk
#   deferred  matched, left for a later scan (this one's download time is up)
#   skipped   matched, but too big or not a PDF
#   error     the download failed (asked again next scan)
#   none      no manual in the system's sets
RESULTS = ("ok", "cached", "deferred", "skipped", "error", "none")


class Entry(NamedTuple):
    """One manual in a set: the archive.org item and the PDF's name in it."""

    item: str
    name: str


Index = dict[str, list[Entry]]  # key(name) -> the manuals by that name


def get(url: str) -> urllib.request.addinfourl:
    request = urllib.request.Request(url, headers={"User-Agent": "brick-games"})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def item_index(item: str) -> Index:
    """An item's manuals, by key(name)."""
    with get(f"{METADATA}{urllib.parse.quote(item)}") as response:
        files = json.loads(response.read()).get("files", [])
    index: Index = {}
    for f in files:
        name = f.get("name", "")
        if name.lower().endswith(".pdf") and not name.lower().endswith("_text.pdf"):
            index.setdefault(ratings.key(name.rsplit("/", 1)[-1]), []).append(Entry(item, name))
    return index


def best(found: list[Entry]) -> Entry:
    return min(found, key=lambda e: sources.region_rank(e.name))


def find(index: Index, name: str, short: str | None = None) -> Entry | None:
    """A game's manual by its exact name, or an arcade set's by its short name."""
    found = index.get(ratings.key(name))
    if not found and short:
        found = index.get(ratings.key(short))
    return best(found) if found else None


def rel(system: dict, game_id: str) -> str:
    """A kept manual's path under media/."""
    return f"{system['id']}/manuals/{game_id}.pdf"


def version(path: Path) -> str:
    """For the URL's ?v=: from the file's size and mtime, so a big PDF isn't
    read every night just to hash it. A new download is a new version."""
    st = path.stat()
    return hashlib.sha256(f"{st.st_size}-{st.st_mtime_ns}".encode()).hexdigest()[:VERSION_LEN]


class Manuals:
    """One scan's manuals: what's on disk, the sets' indexes, the downloads."""

    def __init__(self, media: Path, budget: float = BUDGET) -> None:
        self.media = media
        self.deadline = time.monotonic() + budget
        self.results = dict.fromkeys(RESULTS, 0)
        self._lock = threading.Lock()

    def count(self, result: str) -> None:
        with self._lock:
            self.results[result] += 1

    def on_disk(self, system: dict, game_id: str) -> str | None:
        """The manual's URL path (with ?v=) when it's already kept, else None."""
        path = self.media / rel(system, game_id)
        return f"{rel(system, game_id)}?v={version(path)}" if path.is_file() else None

    def index(self, system: dict) -> Index:
        """Every manual in the system's sets; {} when there are none or the
        Archive can't be asked (logged)."""
        index: Index = {}
        for item in system.get("manuals", []):
            try:
                for k, found in item_index(item).items():
                    index.setdefault(k, []).extend(found)
            except Exception as e:  # never fails the scan (module docstring)
                log.warning("manual set unreadable", system=system["id"], item=item,
                            error=f"{type(e).__name__}: {e}")
        return index

    def fetch(self, system: dict, game_id: str, entry: Entry) -> str | None:
        """Download one manual (once); its URL path, or None. Never raises."""
        if time.monotonic() > self.deadline:
            self.count("deferred")
            return None
        dest = self.media / rel(system, game_id)
        item, name = entry
        tmp = dest.with_suffix(".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            url = f"{DOWNLOAD}{urllib.parse.quote(item)}/{urllib.parse.quote(name)}"
            with get(url) as response, tmp.open("wb") as out:
                first = response.read(len(PDF))
                if first != PDF:
                    raise ValueError("not a PDF")
                out.write(first)
                size = len(first)
                while chunk := response.read(CHUNK):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError(f"over {MAX_BYTES // 2**20} MB")
                    out.write(chunk)
            tmp.replace(dest)
        except ValueError as e:  # the Archive's file isn't one we keep
            tmp.unlink(missing_ok=True)
            self.count("skipped")
            log.info("manual skipped", system=system["id"], file=name, reason=str(e))
            return None
        except Exception as e:  # never fails the scan; asked again next time
            tmp.unlink(missing_ok=True)
            self.count("error")
            log.warning("manual download failed", system=system["id"], file=name,
                        error=f"{type(e).__name__}: {e}")
            return None
        self.count("ok")
        return f"{rel(system, game_id)}?v={version(dest)}"
