"""The scan: every game in ROMS_ROOT, its art and facts, as catalog.json.

    python -m brick_games scan

For each system in systems.yaml it walks the system's folders (read-only) and
keeps the files with the system's extensions. Files that are the same game
become one entry: the same title once tags are stripped (regions, revisions,
formats), with the discs kept apart in `discs`. Arcade zips are grouped under
their parent set (clones are variants of the same game).

Art comes from libretro's thumbnails into MEDIA (box art, title screen,
screenshot; downscaled, fetched once and kept), facts from libretro-database.
A system whose sources can't be reached still lists its games, without art.
The catalog is written whole, then renamed into place: the web app never reads
half a file.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import io
import json
import re
import time
from pathlib import Path

import brick_observe
import yaml
from PIL import Image

from . import sources

log = brick_observe.get_logger(__name__)
SYSTEMS_FILE = Path(__file__).with_name("systems.yaml")
ART_SIZE = 400
WORKERS = 8
# A failed download or a bad picture: OSError covers URLError, timeouts and PIL's
# UnidentifiedImageError; ValueError a picture PIL can't decode.
FETCH_ERRORS = (OSError, ValueError)


class Stats:
    """What a scan found, for the job's pushed metrics (brick_observe.job)."""

    def __init__(self) -> None:
        self.per_system: dict[str, dict[str, int]] = {}
        self.fetches = {"ok": 0, "missing": 0, "error": 0}

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
            key, name = sources.norm(path.name), path.name
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


def shrink(png: bytes) -> bytes:
    with Image.open(io.BytesIO(png)) as im:
        im.load()
        im.thumbnail((ART_SIZE, ART_SIZE))
        out = io.BytesIO()
        im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "PNG", optimize=True)
        return out.getvalue()


def get_art(media: Path, system: dict, kind: str, thumb: str, dest_name: str) -> tuple[str | None, str | None]:
    """Fetch (once) and keep one picture: (its path under MEDIA or None, the
    fetch result for Stats or None when it was already cached). Runs in worker
    threads, so the caller does the counting."""
    rel = f"{system['id']}/{kind}/{dest_name}.png"
    dest = media / rel
    if dest.exists():
        return rel, None
    try:
        data = sources.fetch_thumb(system["libretro"], kind, thumb)
        if not data:
            return None, "missing"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(shrink(data))
        tmp.replace(dest)
        return rel, "ok"
    except FETCH_ERRORS as e:  # one picture; the rest go on
        log.warning("art fetch failed", system=system["id"], kind=kind, error=str(e))
        return None, "error"


def scan_system(root: Path, media: Path, system: dict, fbneo: tuple[dict, dict], stats: Stats) -> list[dict]:
    titles, parents = fbneo if system.get("arcade") else ({}, {})
    files = find_files(root, system)
    games = group(files, system, titles, parents)
    try:
        indexes = {kind: sources.thumb_index(system["libretro"], kind) for kind in sources.KINDS}
        facts = sources.metadata(system["libretro"])
    except OSError as e:
        log.warning("sources unreachable; listing without art", system=system["id"], error=str(e))
        indexes, facts = {}, {}
    jobs = []
    for game in games:
        game["id"] = slug(system["id"], game["key"])
        game["system"] = system["id"]
        game["art"] = {}
        name = game["source_name"]
        game.update(facts.get(sources.norm(name), {}))
        for kind, index in indexes.items():
            cands = sources.lookup(index, name)
            if cands:
                jobs.append((game, kind, sources.best(cands)))
    with cf.ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(get_art, media, system, kind, thumb, game["id"]): (game, kind)
                   for game, kind, thumb in jobs}
        for future in cf.as_completed(futures):
            game, kind = futures[future]
            rel, result = future.result()
            if result:
                stats.fetched(result)
            if rel:
                game["art"][sources.KIND_KEYS[kind]] = rel
    stats.per_system[system["id"]] = {"files": len(files), "games": len(games),
                                      "with_box_art": sum(1 for g in games if "box" in g["art"])}
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


def scan(root: Path, data: Path, stats: Stats | None = None) -> dict:
    stats = stats or Stats()
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
        found = scan_system(root, media, system, fbneo, stats)
        games += found
        if found:
            systems.append({k: system[k] for k in ("id", "name", "devices")} | {"count": len(found)})
    catalog = {"generated": int(time.time()), "devices": config["devices"], "systems": systems, "games": games}
    tmp = data / ".catalog.json.part"
    tmp.write_text(json.dumps(catalog, separators=(",", ":")))
    tmp.replace(data / "catalog.json")
    log.info("catalog written", games=len(games), systems=len(systems))
    return catalog
