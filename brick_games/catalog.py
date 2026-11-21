"""The scan: every game in ROMS_ROOT, its art and facts, as catalog.json.

    python -m brick_games scan

For each system in systems.yaml it walks the system's folders (read-only) and
keeps the files with the system's extensions. Files that are the same game
become one entry: the same title once tags are stripped (regions, revisions,
formats), with the discs kept apart in `discs`. Arcade zips are grouped under
their parent set (clones are variants of the same game).

Art (box art, title screen, screenshot) comes from libretro's thumbnail repos
(sources.py), fetched once and kept as the original PNG in the game's library
folder on the drive (LIBRARY, library.py); manuals come from the Internet
Archive's sets into the same folder (manuals.py). Facts come from
libretro-database, ratings from IGDB and RAWG when their keys are set
(ratings.py), strategy guide links from Wikidata (guides.py).

The page never reads the drive for art: each original is shrunk to a small
AVIF in DATA/media (the NVMe data volume), rebuilt when the original is newer,
and the catalog names it with `?v=<its content hash>`, so browsers cache it
for a year and still see a changed picture as a new URL. A source that can't
be reached only stops new downloads: the art already kept is still listed
(on 2026-11-20 thumbnails.libretro.com went empty and the catalog lost every
picture). The scan won't run without the drive (library.on_drive).
The catalog is written whole, then renamed into place: the web app never reads
half a file.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import io
import json
import os
import re
import threading
import time
from pathlib import Path, PurePath
from typing import NamedTuple

import brick_observe
import yaml
from PIL import Image

from . import guides, library, manuals, ratings, sources

log = brick_observe.get_logger(__name__)
SYSTEMS_FILE = Path(__file__).with_name("systems.yaml")
# AVIF: a box art scan is ~8 KB instead of ~180 KB as PNG (2026-11-16, 60
# samples), and screens ~9 KB instead of ~40 KB. Box art is drawn at most
# ~300 px wide; screens at their own size (320x224 and the like).
ART_FORMAT = "avif"


class ArtSpec(NamedTuple):
    size: int  # the longest side, px
    quality: int  # AVIF quality, 0-100


ART_SIZES = {
    "Named_Boxarts": ArtSpec(300, 50),
    "Named_Titles": ArtSpec(400, 60),
    "Named_Snaps": ArtSpec(400, 60),
}
ART_VERSION_LEN = 8
WORKERS = 8
MANUAL_WORKERS = 4  # the Internet Archive is shared; don't hammer it
# Seconds of art downloading per scan: the first fill is ~15,000 pictures, so
# what's left comes on the nights after (as manuals.BUDGET).
ART_BUDGET = 7200
# A failed download or a bad picture: OSError covers URLError, timeouts and PIL's
# UnidentifiedImageError; ValueError a picture PIL can't decode.
FETCH_ERRORS = (OSError, ValueError)


class Stats:
    """What a scan found, for the job's pushed metrics (brick_observe.job)."""

    def __init__(self) -> None:
        self.per_system: dict[str, dict[str, int]] = {}
        # Art per run: ok downloaded, missing no such picture, error a failed
        # download or a bad picture, deferred left for a later run (time is up
        # or GitHub rate limited us), built an AVIF made from a kept original.
        self.fetches = {"ok": 0, "missing": 0, "error": 0, "deferred": 0, "built": 0}
        self.ratings: dict[str, dict[str, int]] = {}  # source -> result -> systems (ratings.RESULTS)
        self.manuals: dict[str, int] = {}  # result -> games (manuals.RESULTS)
        self.guides: dict[str, int] = {}  # result -> systems (guides.RESULTS)

    def fetched(self, result: str) -> None:
        self.fetches[result] += 1


def load_systems(path: Path = SYSTEMS_FILE) -> dict:
    return yaml.safe_load(path.read_text())


def clean_title(name: str) -> str:
    """The display title: no extension, no (tags) or [flags], "Title, The" turned round."""
    n = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", name)
    n = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", n).replace("_", " ").strip()
    m = re.match(r"^(.*), (The|A|An)( - .*)?$", n)
    if m:
        n = f"{m.group(2)} {m.group(1)}{m.group(3) or ''}"
    return n or name


def disc_of(name: str) -> int | None:
    m = re.search(r"\b(?:disc|disk|cd)\s*([0-9]+)", name, re.I)
    return int(m.group(1)) if m else None


# A dump named by its disc's ID (SX4E01.wbfs, as USB loaders keep Wii games)
# or Nintendont's game.iso / disc2.iso: the game's name is its folder's.
DISC_FILE = re.compile(r"[A-Z0-9]{6}|game|disc2")


def source_name(path: Path, system: dict) -> str:
    """The name a file's game goes by: the file's, or for a dump named only
    by its disc (DISC_FILE) inside a folder of its own, the folder's, with
    the file's extension so it reads like a file name (brick-games#16)."""
    folders = {PurePath(f).name.lower() for f in system["folders"]}
    parent = path.parent.name
    if DISC_FILE.fullmatch(path.stem) and parent.lower() not in folders:
        return parent + path.suffix
    return path.name


def find_files(root: Path, system: dict) -> list[Path]:
    exts = {e.lower() for e in system["exts"]}
    out = []
    for folder in system["folders"]:
        base = root / folder
        if base.is_dir():
            out.extend(p for p in base.rglob("*") if p.is_file() and not p.name.startswith(".")
                    and p.suffix[1:].lower() in exts)
    return sorted(out)


def group(files: list[Path], system: dict, titles: dict[str, str], parents: dict[str, str]) -> list[dict]:
    """Files -> games: one entry per title (per parent set for arcade)."""
    games: dict[str, dict] = {}
    for path in files:
        if system.get("arcade"):
            short = path.stem
            if short not in titles:
                continue  # BIOS files, sets FinalBurn Neo doesn't know
            key = parents.get(short, short)
            name = titles.get(key, titles[short])
        else:
            name = source_name(path, system)
            key = sources.norm(name)
        if not key:
            continue
        game = games.setdefault(key, {"key": key, "names": [], "files": []})
        game["names"].append(name)
        game["files"].append(path)
    out = []
    for game in games.values():
        name = sources.best(game["names"])
        discs = sorted({d for d in (disc_of(f.name) for f in game["files"]) if d})
        out.append({"key": game["key"], "title": clean_title(name), "source_name": name,
                    "files": sorted(game["files"]), "discs": len(discs) or 1})
    return sorted(out, key=lambda g: g["title"].lower())


def slug(system_id: str, key: str) -> str:
    return hashlib.sha1(f"{system_id}/{key}".encode()).hexdigest()[:16]


def shrink(picture: bytes, kind: str) -> bytes:
    """The picture as a small AVIF for the page."""
    spec = ART_SIZES[kind]
    with Image.open(io.BytesIO(picture)) as im:
        im.load()
        im.thumbnail((spec.size, spec.size))
        out = io.BytesIO()
        im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "AVIF", quality=spec.quality)
        return out.getvalue()


def versioned(rel: str, picture: bytes) -> str:
    """The art's URL path with a content hash, so the page can cache it for
    good (web.py's MEDIA_IMMUTABLE): changed art gets a new URL."""
    return f"{rel}?v={hashlib.sha256(picture).hexdigest()[:ART_VERSION_LEN]}"


def write_file(dest: Path, body: bytes) -> None:
    """Whole, then renamed in (a dotted .part beside it): nothing reads half a file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.part")
    tmp.write_bytes(body)
    tmp.replace(dest)


def adopt(original: Path, dest: Path) -> None:
    """A new original whose AVIF is already cached: that AVIF was made from the
    same libretro picture, so the original takes its time and nothing is built
    again (no work, and the URL's ?v= stays, so browsers keep their copy).
    Only an original changed later is newer than its AVIF."""
    try:
        st = dest.stat()
    except FileNotFoundError:
        return
    os.utime(original, ns=(st.st_atime_ns, st.st_mtime_ns))


class Downloads:
    """The art downloads' allowance for one scan: ART_BUDGET seconds, and none
    after GitHub rate limits us. Shared by the worker threads."""

    def __init__(self, budget: float = ART_BUDGET) -> None:
        self.deadline = time.monotonic() + budget
        self._limited = threading.Event()
        self._lock = threading.Lock()

    @property
    def limited(self) -> bool:
        return self._limited.is_set()

    def limit(self) -> bool:
        """Rate limited: no more downloads this scan. True for the first caller only (it logs)."""
        with self._lock:
            first = not self._limited.is_set()
            self._limited.set()
        return first

    def open(self) -> bool:
        return not self.limited and time.monotonic() <= self.deadline


def download_original(system: dict, kind: str, thumb: str, original: Path, dest: Path,
                      downloads: Downloads) -> str:
    """Fetch one original into the library; what happened, for Stats."""
    if not downloads.open():
        return "deferred"
    try:
        data = sources.fetch_thumb(system["libretro"], kind, thumb)
    except sources.RateLimited as e:
        if downloads.limit():
            log.warning("GitHub rate limited the art downloads; the rest wait for the next scan", error=str(e))
        return "deferred"
    except FETCH_ERRORS as e:  # one picture; the rest go on
        log.warning("art fetch failed", system=system["id"], kind=kind, error=str(e))
        return "error"
    if not data:
        return "missing"
    write_file(original, data)
    adopt(original, dest)
    return "ok"


def stale(original: Path, dest: Path) -> bool:
    """There's an original, and no AVIF from it yet or one older than it."""
    try:
        made = original.stat().st_mtime
    except FileNotFoundError:  # no original
        return False
    try:
        return dest.stat().st_mtime < made
    except FileNotFoundError:  # no AVIF yet
        return True


def build_avif(original: Path, dest: Path, kind: str) -> str | None:
    """The AVIF made from the original when it's stale; "built", "error" (a bad
    original, dropped so it's fetched again) or None when there was nothing to do."""
    if not stale(original, dest):
        return None
    try:
        small = shrink(original.read_bytes(), kind)
    except FETCH_ERRORS as e:
        log.warning("kept art unreadable; fetching it again", file=str(original), error=str(e))
        original.unlink(missing_ok=True)
        return "error"
    write_file(dest, small)
    return "built"


def get_art(cache: Path, system: dict, kind: str, thumb: str | None, game_id: str, original: Path,
            downloads: Downloads) -> tuple[str | None, str | None]:
    """One picture: the original fetched once into the library (when there's a
    match and downloads are open), its AVIF built into the cache when missing
    or older than the original. (Its URL under /media/, versioned by its
    content, or None; what happened for Stats, or None when nothing did.) Runs
    in worker threads, so the caller does the counting."""
    rel = f"{system['id']}/{kind}/{game_id}.{ART_FORMAT}"
    dest = cache / rel
    fetched = download_original(system, kind, thumb, original, dest, downloads) \
        if thumb and not original.exists() else None
    built = build_avif(original, dest, kind)
    result = "error" if built == "error" else fetched or built
    try:
        return versioned(rel, dest.read_bytes()), result
    except FileNotFoundError:
        return None, result


def add_manuals(games: list[dict], system: dict, shelf: manuals.Manuals, folders: dict[str, Path]) -> None:
    """Each game's manual (game["manual"]): kept ones from disk, new ones
    matched in the system's sets and downloaded. Never raises: matching and
    downloading fail apart, so a failed match keeps what's on disk and a
    failed download keeps the others. folders: game id -> its library folder."""
    jobs: list[tuple[dict, manuals.Entry]] = []
    try:
        index = None
        for game in games:
            if rel := shelf.on_disk(system, game["id"], folders[game["id"]] / library.MANUAL_FILE):
                game["manual"] = rel
                continue
            if not system.get("manuals"):
                continue
            if index is None:  # only asked when a game still has no manual
                index = shelf.index(system)
            short = game["key"] if system.get("arcade") else None
            if entry := manuals.find(index, game["source_name"], short):
                jobs.append((game, entry))
            else:
                shelf.count("none")
    except Exception as e:  # manuals never fail the scan (manuals.py)
        log.warning("manuals not matched", system=system["id"], error=f"{type(e).__name__}: {e}")
    with cf.ThreadPoolExecutor(MANUAL_WORKERS) as pool:
        futures = {pool.submit(shelf.fetch, system, game["id"], entry, folders[game["id"]] / library.MANUAL_FILE): game
                   for game, entry in jobs}
        for future in cf.as_completed(futures):
            try:
                if rel := future.result():  # fetch doesn't raise; this is the last net
                    futures[future]["manual"] = rel
            except Exception as e:
                log.warning("manual not kept", system=system["id"], error=f"{type(e).__name__}: {e}")


def scan_system(root: Path, media: Path, lib: Path, system: dict, fbneo: tuple[dict, dict], stats: Stats,
                rater: ratings.Ratings, shelf: manuals.Manuals, guide: guides.Guides, downloads: Downloads,
                listings: Path | None = None) -> list[dict]:
    titles, parents = fbneo if system.get("arcade") else ({}, {})
    files = find_files(root, system)
    games = group(files, system, titles, parents)
    indexes: dict[str, dict[str, list[str]]] = {}
    if games and system.get("fetch", True):
        try:
            indexes = sources.thumb_indexes(system["libretro"], listings)
        except OSError as e:  # no new art; the kept art is still listed
            log.warning("thumbnails unreachable; listing the art already kept", system=system["id"], error=str(e))
    try:
        facts = sources.metadata(system["libretro"]) if games else {}
    except OSError as e:
        log.warning("facts unreachable; listing without them", system=system["id"], error=str(e))
        facts = {}
    folders = library.Folders(lib, system["id"])
    scored = rater.for_system(system) if games else ({}, {})  # no fetch for an empty system
    linked = guide.for_system(system) if games else {}
    jobs = []
    folder_of: dict[str, Path] = {}
    for game in games:
        game["id"] = slug(system["id"], game["key"])
        game["system"] = system["id"]
        game["art"] = {}
        folder_of[game["id"]] = folders.folder(game["id"], game["title"])
        name = game["source_name"]
        game.update(facts.get(sources.norm(name), {}))
        game.update(ratings.scores(scored, name))
        if links := ratings.match(linked, name):
            game["guides"] = links
        for kind in sources.KINDS:
            cands = sources.lookup(index, name) if (index := indexes.get(kind)) else None
            jobs.append((game, kind, sources.best(cands) if cands else None))
    folders.save()  # before any download: a folder is recorded before it holds anything
    with cf.ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(get_art, media, system, kind, thumb, game["id"],
                               folder_of[game["id"]] / library.ART_FILES[kind], downloads): (game, kind)
                   for game, kind, thumb in jobs}
        for future in cf.as_completed(futures):
            game, kind = futures[future]
            rel, result = future.result()
            if result:
                stats.fetched(result)
            if rel:
                game["art"][sources.KIND_KEYS[kind]] = rel
    add_manuals(games, system, shelf, folder_of)
    stats.per_system[system["id"]] = {"files": len(files), "games": len(games),
                                      "with_box_art": sum(1 for g in games if "box" in g["art"]),
                                      "with_rating": sum(1 for g in games if ratings.has_score(g)),
                                      "with_manual": sum(1 for g in games if "manual" in g),
                                      "with_guide": sum(1 for g in games if "guides" in g)}
    for game in games:
        del game["key"], game["source_name"]
        game["files"] = [str(f.relative_to(root)) for f in game["files"]]
    log.info("system scanned", system=system["id"], files=len(files), games=len(games))
    return games


def load_fbneo() -> tuple[dict, dict]:
    """(short name -> title, clone -> parent) from FinalBurn Neo's dat."""
    xml_text = sources.get(sources.FBNEO_DAT).decode("utf-8", "replace")
    titles = sources.fbneo_titles(xml_text)
    parents = dict(re.findall(r'<game name="([^"]+)"[^>]*cloneof="([^"]+)"', xml_text))
    return titles, parents


def unchanged(path: Path, catalog: dict) -> bool:
    """Whether the catalog on disk says the same as this one, apart from when it was made."""
    try:
        old = json.loads(path.read_text())
        return {**old, "generated": None} == {**catalog, "generated": None}
    except (OSError, ValueError, TypeError):  # missing, unreadable, or not a catalog
        return False


class NoDrive(RuntimeError):
    """The library isn't on the drive the ROMs are on: the scan doesn't run."""


def scan(root: Path, data: Path, lib: Path, stats: Stats | None = None, rater: ratings.Ratings | None = None,
         shelf: manuals.Manuals | None = None, guide: guides.Guides | None = None,
         downloads: Downloads | None = None) -> dict:
    if not library.on_drive(lib, root):
        raise NoDrive(f"{lib} is missing or not on the drive {root} is on; the catalog is left as it was")
    stats = stats or Stats()
    rater = rater or ratings.Ratings(data)
    shelf = shelf or manuals.Manuals(data / "media")
    guide = guide or guides.Guides(data)
    downloads = downloads or Downloads()
    config = load_systems()
    media = data / "media"
    media.mkdir(parents=True, exist_ok=True)
    try:
        fbneo = load_fbneo()
    except OSError as e:
        log.warning("FinalBurn Neo dat unreachable; arcade sets listed by short name", error=str(e))
        fbneo = ({}, {})
    games, systems = [], []
    for system in config["systems"]:
        found = scan_system(root, media, lib, system, fbneo, stats, rater, shelf, guide, downloads,
                            data / "sources")
        games += found
        if found:
            systems.append({k: system[k] for k in ("id", "name", "devices")}
                           | {"count": len(found), "guides": bool(system.get("wikidata"))})
    stats.ratings = rater.results
    stats.manuals = shelf.results
    stats.guides = guide.results
    catalog = {"generated": int(time.time()), "devices": config["devices"], "systems": systems, "games": games}
    if unchanged(data / "catalog.json", catalog):
        # Left alone, so its ETag stays and browsers keep their copy.
        log.info("catalog unchanged", games=len(games), systems=len(systems))
        return catalog
    tmp = data / ".catalog.json.part"
    tmp.write_text(json.dumps(catalog, separators=(",", ":")))
    tmp.replace(data / "catalog.json")
    log.info("catalog written", games=len(games), systems=len(systems))
    return catalog

