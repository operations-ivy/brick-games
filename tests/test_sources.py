import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from brick_games import sources


class Response(io.BytesIO):
    def __init__(self, body: bytes, etag: str | None = None):
        super().__init__(body)
        self.headers = Message()
        if etag:
            self.headers["ETag"] = etag


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
