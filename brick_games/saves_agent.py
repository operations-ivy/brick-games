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

State slot 10 (`<game>.state10`, a root with kind "state") goes the same way
as its own track, for the systems in the device's `cores`: each upload names
the core, and a state is written only onto a device running the same core
for that system (a state loads in no other).

What it last synced per game is kept in --state (default
~/.local/state/brick-saves/<device>.json): that's how it tells "changed here"
from "behind".
"""

from __future__ import annotations

import argparse
import fnmatch
from collections import defaultdict
from collections.abc import Collection
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = "https://games.brick.nozdormu.cloud"
SAVE, SLOT10 = "save", "slot10"  # the tracks: in-game saves, and RetroArch's state slot 10
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


def core_id(name: str | None) -> str:
    """A core's name, comparable across devices: "Genesis Plus GX" and
    genesis_plus_gx_libretro.so are both "genesisplusgx"."""
    n = re.sub(r"_libretro(_android)?\.so$", "", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", n)


def track_kind(system: str, track: str) -> str:
    return "state" if track == SLOT10 else kind_of(system)


def file_key(game_id: str, track: str) -> str:
    """The agent's own record of what it last synced, per game and track."""
    return game_id if track == SAVE else f"{game_id}#{track}"


def split_key(key: str) -> tuple[str, str]:
    """file_key's inverse: (game id, track)."""
    game_id, _, track = key.partition("#")
    return game_id, track or SAVE


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
        # Through the shell, not `adb push`: Android 13 refuses a push into another app's
        # Android/data (DuckStation's cards, "remote fchown failed"). The shell's file is
        # its own, so it's opened up for the emulator (chmod is a no-op on /sdcard's FUSE).
        folder = shlex.quote(posixpath.dirname(path))
        part = shlex.quote(f"{posixpath.dirname(path)}/.{posixpath.basename(path)}.part")
        dest = shlex.quote(path)
        self._adb("shell", f"mkdir -p {folder} && cat > {part} && {{ chmod 666 {part} 2>/dev/null; mv {part} {dest}; }}",
                  data=data)
        # Read back: a shell pipe has no checksum of its own, and a bad save is worse than none.
        if self.read(path) != data:
            raise OSError(f"{path}: what's on the device isn't what was sent")

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

    def match(self, name: str, system: str | None = None, on_device: set[str] | None = None,
              skip: Collection[str] = ()) -> str | None:
        """The game id a name is, or None: no match, or games in two systems
        that the device's ROMs (on_device: game ids) don't tell apart.
        Systems in skip aren't candidates."""
        ids = {i for s, i in self.keys.get(norm(name), set())
               if (system is None or s == system) and s not in skip}
        if len(ids) > 1 and on_device is not None:
            ids &= on_device  # Breath of Fire II is SNES and GBA: the one whose ROM is here
        return next(iter(ids)) if len(ids) == 1 else None


def find_saves(fs, cfg: dict, games: Games, roms: dict[str, str] | None = None,
               known: dict[str, str] | None = None, track: str = SAVE) -> dict[str, tuple[str, int, float]]:
    """game id -> this device's save for it (the newest, if it has several).
    roms (game id -> ROM stem on the device) settles a name two systems share;
    known (path -> game id, from the last sync) settles one the ROMs don't.
    track SLOT10: its state slot 10, for the systems it names a core for."""
    found: dict[str, tuple[str, int, float]] = {}
    on_device = set(roms) if roms is not None else None
    skip, known = cfg.get("skip", []), known or {}
    cores = cfg.get("cores", {})
    for root in cfg["roots"]:
        is_state_root = root.get("kind") == "state"
        if is_state_root != (track == SLOT10):
            continue  # state roots are slot 10's, the others the in-game saves'
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
            # A skipped system is no candidate: the RG476H's Chrono Trigger .srm is SNES, not DS (DraStic).
            game_id = games.match(stem, root.get("system") or folders.get(folder), on_device, skip)
            if game_id is None and known.get(path) in games.by_id:
                # TMNT is GBA and arcade on the RG476H, in one flat folder: the save the agent synced is the GBA one.
                game_id = known[path]
            if game_id is None or games.by_id[game_id]["system"] in skip:
                continue
            system = games.by_id[game_id]["system"]
            kind = track_kind(system, track)
            if root.get("kind", kind) != kind or (kind == "ps1" and size != PS1_CARD):
                continue
            if track == SLOT10 and system not in cores:
                continue  # no core named for it here: its states stay put
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


def new_path(fs, cfg: dict, game: dict, stem: str, track: str = SAVE) -> str | None:
    """Where a new save for the game goes on this device: the first root that
    writes its kind. `write` is one name for every system, or one per system
    (RetroArch sorting saves into a folder per core): a system it doesn't
    name gets no new saves there."""
    kind = track_kind(game["system"], track)
    for root in cfg["roots"]:
        write = root.get("write")
        if isinstance(write, dict):
            write = write.get(game["system"])
        # a root without a kind holds saves of every kind but states (state roots always say so)
        root_kind = root.get("kind") or (kind if kind != "state" else None)
        if write and root_kind == kind and root.get("system", game["system"]) == game["system"]:
            return fs.join(root["path"], write.format(stem=stem))
    return None


def save_state(state_file: Path, mine: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(f".{state_file.name}.part")
    tmp.write_text(json.dumps(mine, indent=1))
    tmp.replace(state_file)


class Sync:
    """One run of the agent on one device: upload what changed here, then
    write the current versions over saves that haven't, for each track."""

    def __init__(self, server: Server, fs, device: str, files: dict, dry_run: bool, say) -> None:
        self.server, self.fs, self.device, self.files, self.dry_run, self.say = server, fs, device, files, dry_run, say
        self.cfg = server.json("GET", "/api/saves/config", query={"device": device})
        self.skip = self.cfg.get("skip", [])
        self.cores = self.cfg.get("cores", {})
        self.games = Games(server.json("GET", "/api/catalog"))
        self.stems = rom_stems(fs, self.cfg, self.games) if self.cfg.get("roms") else None
        self.found: dict[str, dict[str, tuple[str, int, float]]] = {}
        for track in (SAVE, SLOT10):
            known = {}  # path -> game id, from this track's last sync
            for key, f in files.items():
                game_id, key_track = split_key(key)
                if key_track == track:
                    known[f["path"]] = game_id
            self.found[track] = find_saves(fs, self.cfg, self.games, self.stems, known, track)
        self.done = {"uploaded": 0, "written": 0, "conflicts": 0, "same": 0}

    def _label(self, game_id: str, track: str) -> str:
        return self.games.by_id[game_id]["title"] + (" (slot 10)" if track == SLOT10 else "")

    def _upload(self, game_id: str, track: str, data: bytes, path: str, mtime: float, parent: str | None) -> None:
        query = {"device": self.device, "name": posixpath.basename(path), "mtime": str(mtime), "parent": parent or ""}
        if track == SLOT10:
            query |= {"track": track, "core": self.cores[self.games.by_id[game_id]["system"]]}
        self.server.json("POST", f"/api/saves/{game_id}/upload", data, query)

    def upload_changed(self) -> None:
        for track, found in self.found.items():
            for game_id, (path, _, mtime) in sorted(found.items()):
                data = self.fs.read(path)
                sha = hashlib.sha256(data).hexdigest()
                key = file_key(game_id, track)
                last = self.files.get(key, {}).get("sha")
                if sha == last:
                    self.done["same"] += 1
                    continue
                self.say(f"upload  {self._label(game_id, track)}: {posixpath.basename(path)} "
                         f"({'changed' if last else 'new'})")
                if self.dry_run:
                    continue
                self._upload(game_id, track, data, path, mtime, last)
                self.files[key] = {"path": path, "sha": sha}
                self.done["uploaded"] += 1

    def _target(self, game: dict, head: str, track: str) -> str | None:
        """Where the current version goes on this device, or None to leave it be."""
        here = self.files.get(file_key(game["id"], track), {})
        if game["id"] in self.found[track]:
            path = self.found[track][game["id"]][0]
            if here.get("sha") == head:
                return None
            if hashlib.sha256(self.fs.read(path)).hexdigest() != here.get("sha"):
                return None  # changed here since: it was uploaded
            return path
        stem = (self.stems or {}).get(game["id"])
        if stem is None or (path := new_path(self.fs, self.cfg, game, stem, track)) is None:
            return None  # the game isn't on this device
        if self.fs.exists(path):  # a save there that wasn't matched to this game: never written over
            self.say(f"skip    {self._label(game['id'], track)}: {posixpath.basename(path)} is already there "
                     "(not matched; left alone)")
            return None
        return path

    def write_heads(self) -> None:
        summary = self.server.json("GET", "/api/saves")
        for track, entries in ((SAVE, summary["games"]), (SLOT10, summary.get("states", []))):
            for entry in entries:
                self._write_head(entry, track)

    def _write_head(self, entry: dict, track: str) -> None:
        game, head = self.games.by_id.get(entry["game"]), entry["head"]
        if game is None or head is None or game["system"] in self.skip:
            return
        if track == SLOT10:
            mine = self.cores.get(game["system"])
            if mine is None:
                return  # this device doesn't share states for the system
            if core_id(entry.get("core")) != core_id(mine):
                self.say(f"skip    {self._label(game['id'], track)}: made with {entry.get('core')}, "
                         f"this device runs {mine} for it")
                return
        if entry["conflict"]:
            self.done["conflicts"] += 1
            self.say(f"conflict {self._label(game['id'], track)}: pick the version to keep on the page")
            return
        if (path := self._target(game, head, track)) is None:
            return
        self.say(f"write   {self._label(game['id'], track)}: {posixpath.basename(path)} (the current version)")
        if self.dry_run:
            return
        query = {"track": track} if track != SAVE else None
        data = self.server.request("GET", f"/api/saves/{game['id']}/{head}", query=query)
        self.fs.write(path, data)
        self._upload(game["id"], track, data, path, time.time(), head)  # now in sync: the page shows it
        self.files[file_key(game["id"], track)] = {"path": path, "sha": head}
        self.done["written"] += 1


def sync(server: Server, fs, device: str, state_file: Path, dry_run: bool = False, say=print) -> dict:
    try:
        mine = json.loads(state_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):  # none yet, or cut short: start afresh (uploads only)
        mine = {"files": {}}
    run = Sync(server, fs, device, mine["files"], dry_run, say)
    try:
        run.upload_changed()
        run.write_heads()
    finally:  # a run cut short still remembers what it uploaded and wrote
        if not dry_run:
            save_state(state_file, mine)
    if not dry_run:
        server.json("POST", "/api/saves/seen", b"", {"device": device})
    done = run.done
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
