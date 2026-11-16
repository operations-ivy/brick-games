"""Ready-to-play DOS zips for devices without a keyboard (brick-games#5).

DOSBox Pure starts the program named on the first line of AUTOBOOT.DBP in the
root of drive C (the game's zip) instead of showing its start menu; it's the
file the menu's own "auto start" option writes. A game that also needs keys
pressed, a CPU speed or sound settings gets a DOS.YML beside it (DOSBox
Pure's per-game settings: run_input, cpu_cycles, sound_irq, ...). `build`
copies every zip named in dos_programs.yaml with those files added; the
source zips are not changed.

    python3 -m brick_games.dos SRC DST
"""

from __future__ import annotations

import re
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

import yaml

PROGRAMS = Path(__file__).with_name("dos_programs.yaml")
AUTOBOOT = "AUTOBOOT.DBP"
DOS_YML = "DOS.YML"
DOS_NAME = re.compile(r"[^.\s]{1,8}(\.[^.\s]{1,3})?")


Entry = str | dict  # a program, or {"program": ..., "dos_yml": {key: value}}


def load_programs(path: Path = PROGRAMS) -> dict[str, Entry]:
    """Zip name (no .zip) -> the program to start (a path inside the zip),
    or a mapping with that `program` and extra `dos_yml` settings."""
    return yaml.safe_load(path.read_text())


def dos_yml_text(program: str, settings: dict) -> str:
    lines = [f"run_path: {autoboot_line(program)}"]
    lines += [f"{key}: {value}" for key, value in settings.items()]
    return "\r\n".join(lines) + "\r\n"


def strip_prefix(names: list[str]) -> str:
    """The top folder to drop: one that holds everything and isn't an 8.3 name
    (DOS would see it under a mangled name, so a path to it can't be written)."""
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) == 1 and all("/" in n for n in names):
        top = tops.pop()
        if not DOS_NAME.fullmatch(top):
            return top + "/"
    return ""


def autoboot_line(program: str) -> str:
    return "C:\\" + str(PurePosixPath(program)).replace("/", "\\").upper()


def build(src: Path, dst: Path, entry: Entry) -> None:
    """Copy src to dst with AUTOBOOT.DBP starting the entry's program (and its DOS.YML)."""
    program = entry if isinstance(entry, str) else entry["program"]
    settings = {} if isinstance(entry, str) else entry.get("dos_yml", {})
    replaced = {AUTOBOOT} | ({DOS_YML} if settings else set())
    with zipfile.ZipFile(src) as zin:
        infos = [i for i in zin.infolist() if i.filename.upper() not in replaced]
        prefix = strip_prefix([i.filename for i in infos])
        files = {i.filename[len(prefix):].upper() for i in infos if not i.is_dir()}
        if program.upper() not in files:
            raise ValueError(f"{src.name}: no {program} in the zip")
        tmp = dst.with_name(dst.name + ".part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in infos:
                name = info.filename[len(prefix):]
                if not name:
                    continue
                out = zipfile.ZipInfo(name, info.date_time)
                out.compress_type = zipfile.ZIP_DEFLATED
                out.external_attr = info.external_attr
                if info.is_dir():
                    zout.writestr(out, b"")
                    continue
                with zin.open(info) as fin, zout.open(out, "w") as fout:
                    shutil.copyfileobj(fin, fout, 1 << 20)
            zout.writestr(AUTOBOOT, autoboot_line(program))
            if settings:
                zout.writestr(DOS_YML, dos_yml_text(program, settings))
        tmp.replace(dst)


def build_all(src: Path, dst: Path, programs: dict[str, Entry]) -> list[str]:
    """Build every listed zip into dst and copy the others unchanged (they keep
    DOSBox Pure's start menu); returns the names of those copied unchanged."""
    zips = sorted(src.glob("*.zip"))
    if missing := sorted(set(programs) - {p.stem for p in zips}):
        raise ValueError(f"listed but not in {src}: {', '.join(missing)}")
    dst.mkdir(parents=True, exist_ok=True)
    unlisted = []
    for zip_path in zips:
        if (entry := programs.get(zip_path.stem)) is None:
            shutil.copy2(zip_path, dst / zip_path.name)
            unlisted.append(zip_path.stem)
        else:
            build(zip_path, dst / zip_path.name, entry)
    return unlisted


def main(argv: list[str]) -> None:
    src, dst = map(Path, argv)
    for name in build_all(src, dst, load_programs()):
        print(f"not in dos_programs.yaml, copied unchanged: {name}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
