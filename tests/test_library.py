import json

import pytest

from brick_games import library


@pytest.mark.parametrize("title,name", [
    ("Ys: The Oath in Felghana", "Ys - The Oath in Felghana"),
    ('Who? "Me"/You*', "Who Me You"),
    ("...Iridion 3D.", "Iridion 3D"),
    ("", "untitled"),
])
def test_safe_component(title, name):
    assert library.safe_component(title) == name


def test_long_titles_fit_even_with_the_id(tmp_path):
    folders = library.Folders(tmp_path, "nes")
    first = folders.folder("aaaaaaaaaaaaaaaa", "x" * 300)
    second = folders.folder("bbbbbbbbbbbbbbbb", "x" * 300)
    assert len(first.name) == library.MAX_NAME
    assert len(second.name) <= library.MAX_NAME and second.name.endswith(" [bbbbbbbb]")


def test_a_game_keeps_its_folder_and_a_clash_gets_its_id(tmp_path):
    folders = library.Folders(tmp_path, "nes")
    first = folders.folder("aaaaaaaaaaaaaaaa", "Tetris")
    assert first == tmp_path / "nes" / "media" / "Tetris"
    # exFAT ignores case, so "TETRIS" is the same folder: the second game gets its id
    assert folders.folder("bbbbbbbbbbbbbbbb", "TETRIS").name == "TETRIS [bbbbbbbb]"
    folders.save()
    assert json.loads((tmp_path / "nes" / "media.json").read_text())["aaaaaaaaaaaaaaaa"] == "Tetris"
    # next scan: recorded folders stay, even when a title would now read differently
    again = library.Folders(tmp_path, "nes")
    assert again.folder("aaaaaaaaaaaaaaaa", "Tetris (Renamed)") == first
    assert again.folder("cccccccccccccccc", "Tetris").name == "Tetris [cccccccc]"


def test_the_library_must_be_on_the_drive(tmp_path):
    roms, lib = tmp_path / "roms", tmp_path / "library"
    roms.mkdir()
    assert not library.on_drive(lib, roms)  # not there: the drive isn't mounted
    lib.mkdir()
    assert library.on_drive(lib, roms)
