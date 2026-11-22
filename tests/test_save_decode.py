"""save_decode (#29): what's in a save, from bytes built the way each format is."""

from __future__ import annotations

import pytest

from brick_games import save_decode as sd

PSX = {"system": "psx", "title": "Breath of Fire III"}
SM = {"system": "snes", "title": "Super Metroid"}


def card(*saves: tuple[str, str], deleted: int = 0) -> bytes:
    """A 128 KiB PS1 card with these (product code, title) saves, one block each."""
    data = bytearray(sd.PS1_BLOCK * (sd.PS1_BLOCKS + 1))
    data[:2] = b"MC"
    for i in range(1, sd.PS1_BLOCKS + 1):
        data[i * sd.PS1_FRAME : i * sd.PS1_FRAME + 4] = (0xA0).to_bytes(4, "little")
    for n, (code, title) in enumerate(saves, start=1):
        entry = n * sd.PS1_FRAME
        data[entry : entry + 4] = sd.PS1_FIRST.to_bytes(4, "little")
        data[entry + 10 : entry + 10 + len(code)] = code.encode()
        block = n * sd.PS1_BLOCK
        data[block : block + 2] = b"SC"
        raw = title.encode("shift_jis")
        data[block + 4 : block + 4 + len(raw)] = raw
    for n in range(len(saves) + 1, len(saves) + 1 + deleted):
        data[n * sd.PS1_FRAME : n * sd.PS1_FRAME + 4] = sd.PS1_DELETED.to_bytes(4, "little")
    return bytes(data)


def test_a_card_lists_its_saves_with_their_titles_in_plain_text():
    # Breath of Fire III writes its title in full-width characters
    got = sd.describe(PSX, card(("BASLUS-00422BOF300", "Ｂｒｅａｔｈ ｏｆ Ｆｉｒｅ ３（１） ００ｈ４５ｍ Ｌｖ．０３")))
    assert got["blank"] is False
    assert got["summary"] == "Breath of Fire 3(1) 00h45m Lv.03"
    assert got["saves"] == [{"block": 1, "code": "BASLUS-00422BOF300", "title": "Breath of Fire 3(1) 00h45m Lv.03"}]


def test_an_empty_card_is_blank_even_with_leftover_bytes():
    data = bytearray(card())
    data[sd.PS1_BLOCK * 3 : sd.PS1_BLOCK * 3 + 100] = b"\x5a" * 100  # data in a block the directory calls free
    assert sd.describe(PSX, bytes(data)) == {"blank": True, "summary": "empty card", "saves": []}


def test_a_card_with_only_deleted_saves_says_so():
    assert sd.describe(PSX, card(deleted=1))["summary"] == "empty card (1 deleted)"


@pytest.mark.parametrize("data", [bytes(8192), b"\xff" * 8192, b"\x60" * 8190 + bytes(2)])
def test_unwritten_sram_is_blank(data):
    assert sd.describe({"system": "snes", "title": "Chrono Trigger"}, data)["blank"] is True


def test_a_save_with_no_decoder_says_nothing_it_cant_know():
    assert sd.describe({"system": "gba", "title": "Sonic Advance"}, bytes(range(256)) * 4) == {
        "blank": False, "summary": ""}


def sm_sram(*slots: dict) -> bytes:
    """Super Metroid SRAM with these slots (slot number -> fields), checksummed."""
    data = bytearray(8192)
    for fields in slots:
        n = fields["slot"] - 1
        slot = bytearray(sd.SM_SLOT)
        for offset, value in [(0x20, 99), (0x22, 99), (0x24, 10), (0x26, 10), (0x3E, fields["h"]),
                              (0x3C, fields["m"]), (0x3A, fields["s"]), (0x156, 1), (0x158, fields["area"])]:
            slot[offset : offset + 2] = value.to_bytes(2, "little")
        start = sd.SM_BASE + n * sd.SM_SLOT
        data[start : start + sd.SM_SLOT] = slot
        check = sd.sm_checksum(bytes(slot))
        data[2 * n : 2 * n + 2] = check.to_bytes(2, "little")
        data[8 + 2 * n : 10 + 2 * n] = (check ^ 0xFFFF).to_bytes(2, "little")
    return bytes(data)


def test_super_metroid_slots_give_time_and_place():
    got = sd.describe(SM, sm_sram({"slot": 1, "h": 0, "m": 11, "s": 39, "area": 0},
                                  {"slot": 3, "h": 2, "m": 5, "s": 0, "area": 4}))
    assert got["summary"] == "slot 1: 0:11:39, Crateria, energy 99/99; slot 3: 2:05:00, Maridia, energy 99/99"
    assert [s["slot"] for s in got["slots"]] == [1, 3]


def test_a_super_metroid_slot_with_a_bad_checksum_is_skipped():
    data = bytearray(sm_sram({"slot": 1, "h": 0, "m": 11, "s": 39, "area": 0}))
    data[sd.SM_BASE + 0x20] ^= 1  # energy changed, checksum not
    assert sd.describe(SM, bytes(data)) == {"blank": True, "summary": "no saved slots"}


def test_the_checksum_carries_the_low_byte_into_the_high():
    # The rule was checked against a real save (its stored 0x98f7), which stays out of the repo.
    slot = bytearray(sd.SM_SLOT)
    assert sd.sm_checksum(bytes(slot)) == 0  # sanity: nothing in, nothing out
    slot[0] = 0xFF
    slot[1] = 0x01
    slot[2] = 0x02  # carry from the low byte into the high
    assert sd.sm_checksum(bytes(slot)) == 0x0201
