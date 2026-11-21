"""The media library on brick2000's drive: /extra/brick-games, LIBRARY in the pods.

    <system>/media/<Title>/box.png      the originals, as libretro-thumbnails has them
    <system>/media/<Title>/title.png
    <system>/media/<Title>/snap.png
    <system>/media/<Title>/manual.pdf   the game's manual (manuals.py)
    <system>/media.json                 game id -> its folder under media/

It sits outside /extra/games on purpose: the Samba `games` share and
brick9000's ROM sync only see /extra/games, so nothing here reaches the share
or the handhelds. The library is the archive of record; the page is served
from the cache on the NVMe data volume (catalog.py builds the art there,
cache.py keeps the manuals people open).

A game keeps its folder for good: media.json records it the first time, and a
later game with the same title (exFAT compares names without case) gets
"Title [<id>]" instead of taking it over, so a folder never changes hands.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import brick_observe

log = brick_observe.get_logger(__name__)
ART_FILES = {"Named_Boxarts": "box.png", "Named_Titles": "title.png", "Named_Snaps": "snap.png"}
MANUAL_FILE = "manual.pdf"
INDEX_FILE = "media.json"
UNSAFE = re.compile(r'[\x00-\x1f\x7f"*/<>?\\|]')  # exFAT and Samba refuse these (":" is handled first)
MAX_NAME = 150  # characters: exFAT allows 255 UTF-16 units, and the [id] suffix needs room
ID_LEN = 8  # of the game id, in a clashing title's folder name


def safe_component(s: str, fallback: str = "untitled") -> str:
    """One path component exFAT takes: no separators or reserved characters, no
    leading dot (hidden), no trailing dot or space, at most MAX_NAME characters.
    The same rules as presto's media importer."""
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"\s*:\s*", " - ", s)
    s = UNSAFE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lstrip(".").strip().rstrip(". ")
    if len(s) > MAX_NAME:
        s = s[:MAX_NAME].rstrip(". ")
    return s or fallback


def on_drive(library: Path, roms: Path) -> bool:
    """The library is there and on the same filesystem as the ROMs: with the
    drive unplugged, /extra is an empty folder on brick2000's own disk, and
    nothing should be written there."""
    try:
        return library.is_dir() and library.stat().st_dev == roms.stat().st_dev
    except OSError:
        return False


def read_index(library: Path, system_id: str) -> dict[str, str]:
    """game id -> folder name, as the last scan left it; {} when there's none."""
    try:
        index = json.loads((library / system_id / INDEX_FILE).read_text())
    except FileNotFoundError:
        return {}
    return {str(k): str(v) for k, v in index.items()}


class Folders:
    """One system's game folders: kept ones from media.json, new ones assigned.
    Not thread-safe: the scan assigns every folder before its workers start."""

    def __init__(self, library: Path, system_id: str) -> None:
        self.base = library / system_id
        self.index = read_index(library, system_id)
        self._saved = dict(self.index)
        self._taken = {name.casefold() for name in self.index.values()}

    def folder(self, game_id: str, title: str) -> Path:
        if (name := self.index.get(game_id)) is None:
            name = safe_component(title)
            if name.casefold() in self._taken:
                name = f"{name[:MAX_NAME - ID_LEN - 3]} [{game_id[:ID_LEN]}]"
            self.index[game_id] = name
            self._taken.add(name.casefold())
        return self.base / "media" / name

    def save(self) -> None:
        """Write media.json if a folder was assigned (whole, then renamed in)."""
        if self.index == self._saved:
            return
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.base / f".{INDEX_FILE}.part"
        tmp.write_text(json.dumps(self.index, indent=1, sort_keys=True, ensure_ascii=False))
        tmp.replace(self.base / INDEX_FILE)
        self._saved = dict(self.index)
        log.info("media folders recorded", system=self.base.name, games=len(self.index))
