#!/usr/bin/env python3
"""The save agent: syncs one device's saves with brick-games' saves service
(saves.py, brick-games#19). Standard library only, so brick9000 runs it as
is; the saves service serves this file (/api/saves/agent.py).

    saves_agent.py --device brick9000                  # brick9000's own saves
    saves_agent.py --device rp2plus --adb <serial>     # a handheld, over adb
    saves_agent.py --device rg476h --adb <serial> --dry-run

Each run:
1. Asks the service where this device keeps its saves (saves.yaml) and reads
   the catalog, to match each save to a game by its name.
2. Uploads every save that changed since this device's last sync, naming
   the version it started from (its parent): the referee moves the game
   forward, or flags a conflict for the user to settle on the page.
3. Writes the game's current version over a save the device hasn't changed
   since its last sync, and adds the current save for a game whose ROM is on
   the device but has no save there yet. Never during a conflict.
4. Tells the service it ran (the page's "last synced").

What it last synced per game is kept in --state (default
~/.local/state/brick-saves/<device>.json): that's how it tells "changed here"
from "behind".
"""

from __future__ import annotations

import argparse
import fnmatch
from collections import defaultdict
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = "https://games.brick.nozdormu.cloud"
PS1_CARD = 128 * 1024
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8"}


def norm(name: str) -> str:
    """As brick_games.sources.norm (tests check they agree): the name a save,
    a ROM and a catalog entry share."""
    n = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", name)
    n = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", n)
    n = n.replace("_", " ").replace("&", " ")
    n = re.sub(r",\s*(the|a|an)\b", "", n, flags=re.I)
    n = re.sub(r"^(the|a|an)\s+", "", n, flags=re.I)
    n = re.sub(r"\b(disney's|disneys|and|32x)\b", " ", n, flags=re.I)
    return "".join(ROMAN.get(w, w) for w in re.findall(r"[a-z0-9]+", n.lower()))


def kind_of(system: str) -> str:
    return "ps1" if system == "psx" else "sram"


def stem_of(name: str) -> str:
    return name.rsplit(".", 1)[0]


class Local:
    """This machine's files."""

    def list(self, root: str, recursive: bool) -> list[tuple[str, int, float]]:
        base = Path(os.path.expanduser(root))
        if not base.is_dir():
            return []
        files = base.rglob("*") if recursive else base.iterdir()
        out = []
        for p in files:
            if p.is_file():
                st = p.stat()
                out.append((str(p), st.st_size, st.st_mtime))
        return out

    def read(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def write(self, path: str, data: bytes) -> None:
        dest = Path(os.path.expanduser(path))
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part")
        tmp.write_bytes(data)
        tmp.replace(dest)

    def join(self, root: str, name: str) -> str:
        return str(Path(os.path.expanduser(root)) / name)

    def exists(self, path: str) -> bool:
        return Path(os.path.expanduser(path)).exists()


class Adb:
    """A handheld's files, over adb (USB or wireless debugging)."""

    def __init__(self, serial: str) -> None:
        self.serial = serial

    def _adb(self, *args: str, data: bytes | None = None) -> bytes:
        return subprocess.run(["adb", "-s", self.serial, *args], input=data, capture_output=True,
                              check=True, timeout=300).stdout

    def list(self, root: str, recursive: bool) -> list[tuple[str, int, float]]:
        depth = "" if recursive else "-maxdepth 1 "
        # A missing folder is no saves (exit 0); a dead connection or an
        # unauthorised device is an error, never "no saves".
        cmd = (f"[ -d {shlex.quote(root)} ] || exit 0; "
               f"find {shlex.quote(root)} {depth}-type f -exec stat -c '%s %Y %n' {{}} +")
        out = self._adb("shell", cmd)
        files = []
        for line in out.decode(errors="replace").splitlines():
            size, _, rest = line.partition(" ")
            mtime, _, path = rest.partition(" ")
            if size.isdigit() and path:
                files.append((path, int(size), float(mtime)))
        return files

    def read(self, path: str) -> bytes:
        return self._adb("exec-out", f"cat {shlex.quote(path)}")

    def write(self, path: str, data: bytes) -> None:
        with tempfile.NamedTemporaryFile() as f:
            f.write(data)
            f.flush()
            part = f"{posixpath.dirname(path)}/.{posixpath.basename(path)}.part"
            self._adb("shell", f"mkdir -p {shlex.quote(posixpath.dirname(path))}")
            self._adb("push", f.name, part)
            self._adb("shell", f"mv {shlex.quote(part)} {shlex.quote(path)}")

    def join(self, root: str, name: str) -> str:
        return posixpath.join(root, name)

    def exists(self, path: str) -> bool:
        return self._adb("shell", f"[ -e {shlex.quote(path)} ] && echo yes || true").strip() == b"yes"


class Server:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def request(self, method: str, path: str, body: bytes | None = None, query: dict | None = None) -> bytes:
        url = f"{self.base}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "brick-saves-agent"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def json(self, method: str, path: str, body: bytes | None = None, query: dict | None = None) -> dict:
        return json.loads(self.request(method, path, body, query))


class Games:
    """The catalog's games, findable by a save's or a ROM's name."""

    def __init__(self, catalog: dict) -> None:
        self.by_id = {g["id"]: g for g in catalog["games"]}
        self.keys: dict[str, set[tuple[str, str]]] = {}
        self.exts: dict[str, set[str]] = {}
        for g in catalog["games"]:
            names = [Path(f).name for f in g["files"]] + [g["title"]]
            for name in names:
                self.keys.setdefault(norm(name), set()).add((g["system"], g["id"]))
            self.exts.setdefault(g["system"], set()).update(Path(f).suffix.lower() for f in g["files"])
        for system in catalog.get("systems", []):  # each system's game files (systems.yaml's exts)
            self.exts.setdefault(system["id"], set()).update(f".{e.lower()}" for e in system.get("exts", []))

    def match(self, name: str, system: str | None = None, on_device: set[str] | None = None) -> str | None:
        """The game id a name is, or None: no match, or games in two systems
        that the device's ROMs (on_device: game ids) don't tell apart."""
        ids = {i for s, i in self.keys.get(norm(name), set()) if system is None or s == system}
        if len(ids) > 1 and on_device is not None:
            ids &= on_device  # Breath of Fire II is SNES and GBA: the one whose ROM is here
        return next(iter(ids)) if len(ids) == 1 else None


def find_saves(fs, cfg: dict, games: Games, roms: dict[str, str] | None = None) -> dict[str, tuple[str, int, float]]:
    """game id -> this device's save for it (the newest, if it has several).
    roms (game id -> ROM stem on the device) settles a name two systems share."""
    found: dict[str, tuple[str, int, float]] = {}
    on_device = set(roms) if roms is not None else None
    for root in cfg["roots"]:
        folders = core_folders(root)
        for path, size, mtime in fs.list(root["path"], root.get("recursive", False)):
            name = posixpath.basename(path)
            if name.startswith(".") or not any(fnmatch.fnmatch(name, p) for p in root.get("match", ["*"])):
                continue
            stem = stem_of(name)
            if root.get("strip") and stem.endswith(root["strip"]):
                stem = stem[: -len(root["strip"])]
            # A save in a core's folder (mGBA/) is that core's system's: TMNT is GBA, GameCube and arcade.
            folder = posixpath.basename(posixpath.dirname(path))
            game_id = games.match(stem, root.get("system") or folders.get(folder), on_device)
            if game_id is None or games.by_id[game_id]["system"] in cfg.get("skip", []):
                continue
            kind = kind_of(games.by_id[game_id]["system"])
            if root.get("kind", kind) != kind or (kind == "ps1" and size != PS1_CARD):
                continue
            if game_id not in found or mtime > found[game_id][2]:
                found[game_id] = (path, size, mtime)
    return found


def core_folders(root: dict) -> dict[str, str]:
    """folder -> system, from a root's per-system `write` names (Snes9x/ -> snes),
    for folders only one system writes to."""
    write = root.get("write")
    if not isinstance(write, dict):
        return {}
    seen: defaultdict[str, set[str]] = defaultdict(set)
    for system, name in write.items():
        folder, sep, _ = name.partition("/")
        if sep:
            seen[folder].add(system)
    return {folder: next(iter(systems)) for folder, systems in seen.items() if len(systems) == 1}


def rom_stems(fs, cfg: dict, games: Games) -> dict[str, str]:
    """game id -> the stem of its ROM on this device (how a new save is named)."""
    stems: dict[str, str] = {}
    for root in cfg.get("roms", []):
        for path, _, _ in fs.list(root, True):
            name = posixpath.basename(path)
            for system, game_id in games.keys.get(norm(name), set()):
                if Path(name).suffix.lower() in games.exts.get(system, set()):
                    stems.setdefault(game_id, stem_of(name))
    return stems


def new_path(fs, cfg: dict, game: dict, stem: str) -> str | None:
    """Where a new save for the game goes on this device: the first root that
    writes its kind. `write` is one name for every system, or one per system
    (RetroArch sorting saves into a folder per core): a system it doesn't
    name gets no new saves there."""
    kind = kind_of(game["system"])
    for root in cfg["roots"]:
        write = root.get("write")
        if isinstance(write, dict):
            write = write.get(game["system"])
        if write and root.get("kind", kind) == kind and root.get("system", game["system"]) == game["system"]:
            return fs.join(root["path"], write.format(stem=stem))
    return None


def sync(server: Server, fs, device: str, state_file: Path, dry_run: bool = False, say=print) -> dict:
    cfg = server.json("GET", "/api/saves/config", query={"device": device})
    games = Games(server.json("GET", "/api/catalog"))
    try:
        mine = json.loads(state_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):  # none yet, or cut short: start afresh (uploads only)
        mine = {"files": {}}
    files = mine["files"]
    done = {"uploaded": 0, "written": 0, "conflicts": 0, "same": 0}

    def upload(game_id: str, data: bytes, path: str, mtime: float, parent: str | None) -> dict:
        query = {"device": device, "name": posixpath.basename(path), "mtime": str(mtime), "parent": parent or ""}
        return server.json("POST", f"/api/saves/{game_id}/upload", data, query)

    stems = rom_stems(fs, cfg, games) if cfg.get("roms") else None
    found = find_saves(fs, cfg, games, stems)
    states: dict[str, dict] = {}
    for game_id, (path, _, mtime) in sorted(found.items()):
        data = fs.read(path)
        sha = hashlib.sha256(data).hexdigest()
        last = files.get(game_id, {}).get("sha")
        title = games.by_id[game_id]["title"]
        if sha == last:
            done["same"] += 1
            continue
        say(f"upload  {title}: {posixpath.basename(path)} ({'changed' if last else 'new'})")
        if dry_run:
            continue
        states[game_id] = upload(game_id, data, path, mtime, last)
        files[game_id] = {"path": path, "sha": sha}
        done["uploaded"] += 1

    summary = server.json("GET", "/api/saves")["games"]
    for entry in summary:
        game_id, head = entry["game"], entry["head"]
        game = games.by_id.get(game_id)
        if game is None or head is None or game["system"] in cfg.get("skip", []):
            continue
        if entry["conflict"]:
            done["conflicts"] += 1
            say(f"conflict {game['title']}: pick the version to keep on the page")
            continue
        here = files.get(game_id, {})
        if game_id in found:
            path = found[game_id][0]
            if here.get("sha") == head:
                continue
            current = hashlib.sha256(fs.read(path)).hexdigest()
            if current != here.get("sha"):  # changed here since: it was uploaded above
                continue
        else:
            if not stems or game_id not in stems or (path := new_path(fs, cfg, game, stems[game_id])) is None:
                continue  # the game isn't on this device
            if fs.exists(path):  # a save there that wasn't matched to this game: never written over
                say(f"skip    {game['title']}: {posixpath.basename(path)} is already there (not matched; left alone)")
                continue
        say(f"write   {game['title']}: {posixpath.basename(path)} (the current version)")
        if dry_run:
            continue
        data = server.request("GET", f"/api/saves/{game_id}/{head}")
        fs.write(path, data)
        upload(game_id, data, path, time.time(), head)  # now in sync: the page shows it
        files[game_id] = {"path": path, "sha": head}
        done["written"] += 1

    if not dry_run:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_name(f".{state_file.name}.part")
        tmp.write_text(json.dumps(mine, indent=1))
        tmp.replace(state_file)
        server.json("POST", "/api/saves/seen", b"", {"device": device})
    say(f"{device}: {done['uploaded']} uploaded, {done['written']} written, {done['same']} unchanged, "
        f"{done['conflicts']} conflicts waiting")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", required=True, help="brick9000, rp2plus or rg476h (saves.yaml)")
    parser.add_argument("--adb", metavar="SERIAL", help="reach the device over adb (`adb devices`)")
    parser.add_argument("--server", default=SERVER)
    parser.add_argument("--state", type=Path, help="what was last synced (default ~/.local/state/brick-saves/)")
    parser.add_argument("--dry-run", action="store_true", help="say what would happen, change nothing")
    args = parser.parse_args(argv)
    state = args.state or Path.home() / ".local" / "state" / "brick-saves" / f"{args.device}.json"
    fs = Adb(args.adb) if args.adb else Local()
    try:
        sync(Server(args.server), fs, args.device, state, args.dry_run)
    except (urllib.error.URLError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError,
            json.JSONDecodeError) as e:
        print(f"save sync failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
