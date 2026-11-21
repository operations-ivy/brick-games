#!/usr/bin/env python3
"""File GameFAQs guides you save by hand into the games' library folders
(brick-games#21). Standard library only; runs on the laptop.

GameFAQs guides are their authors' work and GameFAQs forbids scripted
copying, so nothing here downloads them: you open a guide in your browser
and save it as text; this files it.

    guide_filer.py --wanted          # the games ranked 80+ without a complete guide, with links
    guide_filer.py                   # watch ~/Downloads and file each guide saved there
    guide_filer.py --once            # file what's in ~/Downloads now, then stop
    guide_filer.py FILE --game ID    # file one file for one game

A saved .txt is matched to a wanted game by its name (GameFAQs' file names
start with the game's title; the system's name, when it's in the file name,
settles a title two systems share; otherwise you're asked). It's copied
over ssh to brick2000's library as
/extra/brick-games/<system>/media/<Title>/guide-gamefaqs.txt (an older one
is kept beside it, dated), and your copy moves to ~/Downloads/filed/. The
next scan indexes it (strategy.py): searchable on the page and by the
game-guide skill, whatever the game's rank.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path, PurePosixPath

SERVER = "https://games.brick.nozdormu.cloud"
HOST = "brick2000"
LIBRARY = "/extra/brick-games"
HAND_SAVED = "guide-gamefaqs.txt"
POLL = 2.0
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8"}
# Words GameFAQs puts in a guide's page title for each system (file names are page titles).
SYSTEM_WORDS = {
    "nes": ["nes"], "snes": ["super nintendo", "snes"], "gb": ["game boy"], "gba": ["game boy advance", "gba"],
    "n64": ["nintendo 64", "n64"], "nds": ["ds"], "gc": ["gamecube"], "wii": ["wii"], "megadrive": ["genesis"],
    "gamegear": ["game gear"], "sega32x": ["32x"], "segacd": ["sega cd"], "saturn": ["saturn"],
    "dreamcast": ["dreamcast"], "pce": ["turbografx-16", "turbografx 16"], "pcecd": ["turbografx-cd"],
    "psx": ["playstation"], "ps2": ["playstation 2"], "psp": ["psp"], "neogeo": ["neo-geo", "neo geo"],
    "arcade": ["arcade"], "dos": ["pc", "dos"],
}


def norm(name: str) -> str:
    """As brick_games.sources.norm: the name, letters and digits only."""
    n = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", name)
    n = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", n)
    n = n.replace("_", " ").replace("&", " ")
    n = re.sub(r",\s*(the|a|an)\b", "", n, flags=re.I)
    n = re.sub(r"^(the|a|an)\s+", "", n, flags=re.I)
    n = re.sub(r"\b(disney's|disneys|and|32x)\b", " ", n, flags=re.I)
    return "".join(ROMAN.get(w, w) for w in re.findall(r"[a-z0-9]+", n.lower()))


def wanted(server: str) -> list[dict]:
    with urllib.request.urlopen(f"{server}/api/guides/wanted", timeout=60) as r:
        return json.loads(r.read())


def candidates(filename: str, games: list[dict]) -> list[dict]:
    """The wanted games a file name could be: its name starts with the game's
    title (or contains a long one), the longest titles first; narrowed to one
    system when the file name says which."""
    key = norm(filename)
    titled = [(t, g) for g in games if (t := norm(g["title"]))]
    found = [(t, g) for t, g in titled if key.startswith(t) or (len(t) >= 8 and t in key)]
    if not found:
        return []
    best = max(len(t) for t, _ in found)
    hits = [g for t, g in found if len(t) == best]
    if len(hits) > 1:
        lowered = filename.lower()
        by_system = [g for g in hits
                     if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in SYSTEM_WORDS.get(g["system"], []))]
        hits = by_system or hits
    return hits


def looks_like_text(path: Path) -> bool:
    with path.open("rb") as f:
        head = f.read(512).lstrip().lower()
    return not head.startswith((b"<!doctype", b"<html")) and b"\x00" not in head


def upload(path: Path, game: dict, host: str = HOST) -> str:
    """Copy the file to the game's library folder on brick2000; its path there."""
    folder = PurePosixPath(LIBRARY, game["folder"])  # a path on brick2000, not here
    dest = str(folder / HAND_SAVED)
    q = shlex.quote(dest)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    old = shlex.quote(str(folder / f"guide-gamefaqs.{stamp}.txt"))
    script = (f"mkdir -p {shlex.quote(str(folder))} && "
              f"{{ [ ! -e {q} ] || mv {q} {old}; }} && cat > {q}.part && mv {q}.part {q}")
    subprocess.run(["ssh", host, script], input=path.read_bytes(), check=True, timeout=120)
    return dest


def choose(path: Path, hits: list[dict]) -> dict | None:
    print(f"\n{path.name} could be:")
    for i, g in enumerate(hits, 1):
        print(f"  {i}. {g['title']} ({g['system']})")
    answer = input("Which one? (number, or Enter to skip) ").strip()
    return hits[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(hits) else None


def file_one(path: Path, games: list[dict], game: dict | None = None, done: Path | None = None) -> bool:
    if not looks_like_text(path):
        print(f"skip    {path.name}: that's a web page, not text (save the guide's text instead)")
        return False
    if game is None:
        hits = candidates(path.name, games)
        if not hits:
            return False  # not a guide for a wanted game: left alone
        game = hits[0] if len(hits) == 1 else choose(path, hits)
        if game is None:
            return False
    if not game.get("folder"):
        print(f"skip    {path.name}: {game['title']} has no library folder yet (the next scan makes it)")
        return False
    dest = upload(path, game)
    print(f"filed   {path.name} -> {game['title']} ({game['system']}): {HOST}:{dest}")
    if done:
        shutil.move(path, done / path.name)
    return True


def watch(downloads: Path, games: list[dict], once: bool) -> None:
    done = downloads / "filed"
    done.mkdir(exist_ok=True)
    sizes: dict[Path, int] = {}
    seen: set[Path] = set()
    if not once:
        print(f"watching {downloads} for saved guides (Ctrl-C to stop)")
    while True:
        for path in sorted(downloads.glob("*.txt")):
            if path in seen:
                continue
            size = path.stat().st_size
            if not once and sizes.get(path) != size:  # still being written: look again next poll
                sizes[path] = size
                continue
            seen.add(path)
            try:
                file_one(path, games, done=done)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                print(f"failed  {path.name}: {e}", file=sys.stderr)
                seen.discard(path)  # tried again next poll: brick2000 may just be away
        if once:
            return
        time.sleep(POLL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", nargs="?", type=Path, help="one saved guide to file (with --game)")
    parser.add_argument("--game", help="the game's id or exact title, for FILE")
    parser.add_argument("--wanted", action="store_true", help="list the games still wanting a guide")
    parser.add_argument("--once", action="store_true", help="file what's in the folder now, then stop")
    parser.add_argument("--downloads", type=Path, default=Path.home() / "Downloads")
    parser.add_argument("--server", default=SERVER)
    args = parser.parse_args(argv)
    games = wanted(args.server)
    if args.wanted:
        for g in games:
            have = f"{g['grade']} on StrategyWiki" if g["grade"] else "no guide"
            print(f"{g['rank']:5.1f}  {g['title']} ({g['system']}), {have}\n       {g['gamefaqs']}")
        print(f"\n{len(games)} games ranked 80+ without a complete guide.")
        return 0
    if args.file:
        game = None
        if args.game:
            game = next((g for g in games if args.game in (g["id"], g["title"])), None)
            if game is None:
                print(f"no wanted game {args.game!r} (see --wanted)", file=sys.stderr)
                return 1
        return 0 if file_one(args.file, games, game) else 1
    try:
        watch(args.downloads, games, args.once)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
