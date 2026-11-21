import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from brick_games import library, sources


@pytest.mark.parametrize("title,name", [
    ("Ys: The Oath in Felghana", "Ys - The Oath in Felghana"),
    ('Who? "Me"/You*', "Who Me You"),
    ("...Iridion 3D.", "Iridion 3D"),
    ("", "untitled"),
])
def test_safe_component(title, name):
    assert library.safe_component(title) == name


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


class Response(io.BytesIO):
    def __init__(self, body: bytes, etag: str | None = None):
        super().__init__(body)
        self.headers = Message()
        if etag:
            self.headers["ETag"] = etag

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(url: str, code: int, headers: dict | None = None):
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(url, code, "error", msg, None)


def test_tree_listing_falls_back_to_folders_and_revalidates(tmp_path, monkeypatch):
    calls = []
    top = {"tree": [{"path": "Named_Boxarts", "type": "tree", "sha": "b0x"},
                    {"path": "Named_Logos", "type": "tree", "sha": "l0g"},
                    {"path": "README.md", "type": "blob", "sha": "r"}], "truncated": False}
    box = {"tree": [{"path": "Street Fighter II (World).png", "type": "blob"}], "truncated": False}

    def urlopen(request, timeout=None):
        url = request.full_url
        calls.append((url, request.get_header("If-none-match")))
        if url.endswith("?recursive=1"):
            raise http_error(url, 500)  # too big for GitHub, as FBNeo's is
        if request.get_header("If-none-match"):
            raise http_error(url, 304)
        body = top if url.endswith("/trees/master") else box
        return Response(json.dumps(body).encode(), etag=f'"{len(calls)}"')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    indexes = sources.thumb_indexes("FBNeo - Arcade Games", tmp_path)
    assert indexes["Named_Boxarts"][sources.norm("Street Fighter II (World).png")] == ["Street Fighter II (World).png"]
    assert not indexes["Named_Titles"]
    assert not any("l0g" in url for url, _ in calls)  # logos aren't shown: never listed
    calls.clear()
    assert sources.thumb_indexes("FBNeo - Arcade Games", tmp_path) == indexes  # from 304s
    assert all(etag for url, etag in calls if not url.endswith("?recursive=1"))


def test_a_rate_limit_is_its_own_error(monkeypatch):
    def urlopen(request, timeout=None):
        raise http_error(request.full_url, 403, {"X-RateLimit-Remaining": "0"})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(sources.RateLimited):
        sources.thumb_indexes("Nintendo - Nintendo Entertainment System")


def test_shared_art_follows_the_symlink(monkeypatch):
    png = sources.PNG + b"picture"
    files = {"Named_Boxarts/10-Yard Fight (Japan) (En) (Rev 1).png": b"10-Yard Fight (Japan) (Rev 1).png",
             "Named_Boxarts/10-Yard Fight (Japan) (Rev 1).png": png}
    prefix = sources.thumb_url("Nintendo - Nintendo Entertainment System", "")
    monkeypatch.setattr(sources, "get", lambda url: files[urllib.request.url2pathname(url.removeprefix(prefix))])
    assert sources.fetch_thumb("Nintendo - Nintendo Entertainment System", "Named_Boxarts",
                               "10-Yard Fight (Japan) (En) (Rev 1).png") == png
    assert prefix.startswith("https://raw.githubusercontent.com/libretro-thumbnails/"
                             "Nintendo_-_Nintendo_Entertainment_System/master/")
