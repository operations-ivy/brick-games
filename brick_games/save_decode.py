"""What's inside a save (#29): enough to pick a version in a conflict, and to
tell the game-guide skill where the player is.

`describe(game, data)` returns a dict with:
    blank    True when there's no game saved in it (an empty or freshly
             formatted card, SRAM the game never wrote)
    summary  one line for the page, e.g. "Breath of Fire 3 (1) 00h45m Lv.03"
    slots    per-slot details, when the format has slots (Super Metroid)

Only formats checked against real saves are decoded: PlayStation memory cards
(their directory and each save's own title) and Super Metroid (its slots, with
their checksums). Anything else is described only as blank or not.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from typing import Any

from .sources import norm

PS1_FRAME = 128
PS1_BLOCK = 8192
PS1_BLOCKS = 15
PS1_FIRST = 0x51  # a save's first block
PS1_DELETED = 0xA1  # a deleted save's first block
BLANK_SHARE = 0.99  # one byte value filling this much of a save: never written


def describe(game: dict[str, Any], data: bytes) -> dict[str, Any]:
    if game["system"] == "psx" and len(data) == PS1_BLOCK * (PS1_BLOCKS + 1) and data[:2] == b"MC":
        return ps1_card(data)
    decoder = GAMES.get((game["system"], norm(game["title"])))
    if decoder:
        found = decoder(data)
        if found:
            return found
    if is_blank(data):
        return {"blank": True, "summary": "blank: nothing saved"}
    return {"blank": False, "summary": ""}


def is_blank(data: bytes) -> bool:
    """All one byte value, or nearly (the RG476H's never-saved Chrono Trigger
    is 8190 bytes of 0x60 and 2 zeros)."""
    if not data:
        return True
    _, count = Counter(data).most_common(1)[0]
    return count / len(data) >= BLANK_SHARE


def _word(buf: bytes, offset: int) -> int:
    return int.from_bytes(buf[offset : offset + 2], "little")


def _ps1_title(block: bytes) -> str:
    """The title in a save block's header: Shift-JIS, often full-width, as plain text."""
    title = block[4:68].decode("shift_jis", errors="replace").split("\x00")[0]
    return " ".join(unicodedata.normalize("NFKC", title).split())


def ps1_card(data: bytes) -> dict[str, Any]:
    """A raw 128 KiB card: frame 0 is the header, frames 1-15 the directory,
    block n holds save n, whose header carries the title the console shows
    (Shift-JIS, often full-width: normalised to plain text)."""
    saves, deleted = [], 0
    for i in range(1, PS1_BLOCKS + 1):
        entry = data[i * PS1_FRAME : (i + 1) * PS1_FRAME]
        status = int.from_bytes(entry[:4], "little")
        if status == PS1_DELETED:
            deleted += 1
        if status != PS1_FIRST:
            continue
        title = _ps1_title(data[i * PS1_BLOCK : (i + 1) * PS1_BLOCK])
        code = entry[10:30].split(b"\x00")[0].decode("ascii", errors="replace")
        saves.append({"block": i, "code": code, "title": title or code})
    if not saves:
        note = f" ({deleted} deleted)" if deleted else ""
        return {"blank": True, "summary": f"empty card{note}", "saves": []}
    summary = "; ".join(s["title"] for s in saves)
    return {"blank": False, "summary": summary, "saves": saves}


# Super Metroid: SRAM holds three 0x65C-byte slots from 0x10, each a copy of
# RAM from $7E:09A2, with a 16-bit checksum at 2*slot and its complement at
# 8 + 2*slot. Checked against a real save: the checksum matches.
SM_BASE, SM_SLOT, SM_SLOTS = 0x10, 0x65C, 3
SM_AREAS = ["Crateria", "Brinstar", "Norfair", "Wrecked Ship", "Maridia", "Tourian", "Ceres", "Debug"]


def sm_checksum(slot: bytes) -> int:
    lo = hi = 0
    for i in range(0, len(slot), 2):
        lo += slot[i]
        carry, lo = lo >> 8, lo & 0xFF
        hi = (hi + slot[i + 1] + carry) & 0xFF
    return hi << 8 | lo


def super_metroid(data: bytes) -> dict[str, Any] | None:
    if len(data) < SM_BASE + SM_SLOTS * SM_SLOT:
        return None
    slots = []
    for n in range(SM_SLOTS):
        slot = data[SM_BASE + n * SM_SLOT : SM_BASE + (n + 1) * SM_SLOT]
        stored, complement = _word(data, 2 * n), _word(data, 8 + 2 * n)
        if not any(slot) or stored ^ 0xFFFF != complement or sm_checksum(slot) != stored:
            continue
        area = _word(slot, 0x158)
        slots.append({
            "slot": n + 1,
            "time": f"{_word(slot, 0x3E)}:{_word(slot, 0x3C):02d}:{_word(slot, 0x3A):02d}",
            "area": SM_AREAS[area] if area < len(SM_AREAS) else f"area {area}",
            "save_station": _word(slot, 0x156),
            "energy": f"{_word(slot, 0x20)}/{_word(slot, 0x22)}",
            "missiles": f"{_word(slot, 0x24)}/{_word(slot, 0x26)}",
        })
    if not slots:
        return {"blank": True, "summary": "no saved slots"}
    summary = "; ".join(f"slot {s['slot']}: {s['time']}, {s['area']}, energy {s['energy']}" for s in slots)
    return {"blank": False, "summary": summary, "slots": slots}


GAMES = {
    ("snes", norm("Super Metroid")): super_metroid,
}
