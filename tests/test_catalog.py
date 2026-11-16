import gzip
import hashlib
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from brick_games import catalog, sources, web


def png(w: int = 600, h: int = 800) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (0, 200, 255)).save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize("a,b", [
    ("Adventures of Rocky and Bullwinkle, The (U).gb", "The Adventures of Rocky & Bullwinkle (USA)"),
    ("A Nightmare on Elm Street.nes", "Nightmare on Elm Street, A (USA)"),
    ("Disney's Aladdin.smc", "Aladdin (USA)"),
    ("barts_nightmare.bin", "Bart's Nightmare (USA, Europe)"),
    ("Final Fight II (U) [!].smc", "Final Fight 2 (USA)"),
])
def test_norm_matches_across_naming_schemes(a, b):
    assert sources.norm(a) == sources.norm(b)


def test_loose_finds_a_subtitled_name_but_not_short_ones():
    index = {sources.norm("Adventure Island II - Aliens in Paradise (USA)"): ["x.png"]}
    assert sources.lookup(index, "Adventure Island II (UE) [!].gb") == ["x.png"]
    assert sources.loose({"tetris2": ["t2"]}, "tet") is None


def test_best_prefers_usa_then_world_then_europe():
    assert sources.best(["G (Japan).png", "G (Europe).png", "G (USA).png"]) == "G (USA).png"
    assert sources.best(["G (Japan).png", "G (World).png"]) == "G (World).png"


def test_clean_title():
    assert catalog.clean_title("Legend of Zelda, The - Majora's Mask (USA).n64") == "The Legend of Zelda - Majora's Mask"
    assert catalog.clean_title("Super Mario Land (W) (V1.1) [!].gb") == "Super Mario Land"


def test_parse_metadat_comment_or_rom_name_quoted_or_bare():
    text = '''clrmamepro ( name "x" )

game (
\tcomment "2020 Super Baseball (Japan)"
\tgenre "Sports"
\tusers 2
\trom ( crc 05FBB855 )
)

game (
   developer "Toys for Bob"
\trom ( name "102 Dalmatians (UK) (Track 1).bin" size 1 )
)
'''
    assert sources.parse_metadat(text, "genre") == {"2020 Super Baseball (Japan)": "Sports"}
    assert sources.parse_metadat(text, "users") == {"2020 Super Baseball (Japan)": "2"}
    assert sources.parse_metadat(text, "developer") == {"102 Dalmatians (UK).bin": "Toys for Bob"}


def test_fbneo_titles_and_parents():
    xml = ('<game name="sf2" sourcefile="x"><description>Street Fighter II (World)</description></game>'
           '<game name="sf2ua" cloneof="sf2" romof="sf2"><description>Street Fighter II (US rev A)</description></game>')
    assert sources.fbneo_titles(xml) == {"sf2": "Street Fighter II (World)", "sf2ua": "Street Fighter II (US rev A)"}


def make_tree(root: Path):
    for f in ["snes/Final Fight (U) [!].smc", "snes/Final Fight (Japan).smc", "snes/readme.txt",
              "Dreamcast/Shenmue (USA) (Disc 1).chd", "Dreamcast/Shenmue (USA) (Disc 2).chd",
              "fba/sf2.zip", "fba-retroid/sf2ua.zip", "fba-retroid/neogeo.zip"]:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_bytes(b"")


@pytest.fixture
def offline(monkeypatch):
    """The sources, without the network."""
    thumbs = {"Named_Boxarts": ["Final Fight (USA).png", "Street Fighter II (World).png"], "Named_Titles": [],
              "Named_Snaps": []}

    def thumb_index(system, kind):
        index = {}
        for name in thumbs[kind]:
            index.setdefault(sources.norm(name), []).append(name)
        return index

    monkeypatch.setattr(sources, "thumb_index", thumb_index)
    monkeypatch.setattr(sources, "fetch_thumb", lambda system, kind, name: png())
    monkeypatch.setattr(sources, "metadata", lambda system: {sources.norm("Final Fight (USA)"): {"year": "1991", "genre": "Beat'em up"}})
    monkeypatch.setattr(catalog, "load_fbneo", lambda: (
        {"sf2": "Street Fighter II (World)", "sf2ua": "Street Fighter II (US rev A)"}, {"sf2ua": "sf2"}))


def test_scan_groups_games_fetches_art_and_writes_the_catalog(tmp_path, offline):
    roms, data = tmp_path / "roms", tmp_path / "data"
    make_tree(roms)
    stats = catalog.Stats()
    cat = catalog.scan(roms, data, stats)
    by_title = {g["title"]: g for g in cat["games"]}
    # two regions of one game, one entry; the .txt ignored
    ff = by_title["Final Fight"]
    assert len(ff["files"]) == 2 and ff["year"] == "1991" and ff["genre"] == "Beat'em up"
    box, _, version = ff["art"]["box"].partition("?v=")
    assert box.endswith(".png") and (data / "media" / box).exists()
    with Image.open(data / "media" / box) as im:
        assert max(im.size) <= catalog.ART_SIZE
    # the art's URL carries its content hash, and a cached picture keeps it
    assert version == hashlib.sha256((data / "media" / box).read_bytes()).hexdigest()[:catalog.ART_VERSION_LEN]
    # two discs, one game
    assert by_title["Shenmue"]["discs"] == 2
    # a clone grouped under its parent, and the BIOS left out
    sf2 = by_title["Street Fighter II"]
    assert sorted(Path(f).name for f in sf2["files"]) == ["sf2.zip", "sf2ua.zip"]
    assert "neogeo" not in json.dumps(cat["games"])
    assert stats.per_system["snes"] == {"files": 2, "games": 1, "with_box_art": 1}
    assert json.loads((data / "catalog.json").read_text())["games"] == cat["games"]
    # a second scan downloads nothing again
    before = dict(stats.fetches)
    written = (data / "catalog.json").stat().st_mtime_ns
    again = catalog.scan(roms, data, stats)
    # nothing changed, so the file (and the page's ETag) is left as it was
    assert (data / "catalog.json").stat().st_mtime_ns == written
    assert stats.fetches == before
    assert {g["title"]: g["art"] for g in again["games"]}["Final Fight"] == ff["art"]


def test_scan_without_network_still_lists_games(tmp_path, monkeypatch):
    roms, data = tmp_path / "roms", tmp_path / "data"
    make_tree(roms)

    def down(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(sources, "get", down)
    cat = catalog.scan(roms, data)
    assert {g["title"] for g in cat["games"]} >= {"Final Fight", "Shenmue"}
    assert all(not g["art"] for g in cat["games"])


@pytest.fixture
def server(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(tmp_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    srv.shutdown()
    srv.server_close()


def fetch(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_web_serves_page_catalog_and_media_only(server):
    base, data = server
    assert fetch(base + "/")[0] == 200
    assert fetch(base + "/api/catalog")[0] == 503
    assert fetch(base + "/ready")[0] == 503
    (data / "catalog.json").write_text('{"games": []}')
    assert fetch(base + "/api/catalog") == (200, b'{"games": []}')
    assert fetch(base + "/ready")[0] == 200
    pic = data / "media" / "snes" / "Named_Boxarts" / "0123456789abcdef.png"
    pic.parent.mkdir(parents=True)
    pic.write_bytes(png(10, 10))
    assert fetch(base + "/media/snes/Named_Boxarts/0123456789abcdef.png")[0] == 200
    for bad in ("/media/../catalog.json", "/media/snes/Named_Boxarts/../../catalog.json", "/media/x.png", "/etc/passwd"):
        assert fetch(base + bad)[0] == 404
    assert fetch(base + "/metrics")[0] == 200


def test_publish_sends_each_count_as_a_gauge():
    from brick_games.__main__ import publish

    class Run:
        def __init__(self):
            self.gauges = []

        def gauge(self, name, help_text, value, labels):
            self.gauges.append((name, value, labels))

    stats = catalog.Stats()
    stats.per_system["snes"] = {"files": 2, "games": 1, "with_box_art": 1}
    stats.fetched("ok")
    run = Run()
    publish(run, stats)
    assert ("games_catalog_games", 1, {"system": "snes"}) in run.gauges
    assert ("games_art_fetches", 1, {"result": "ok"}) in run.gauges


def get(url, headers=None, method="GET"):
    """(status, headers, body) without urllib's own error handling."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}, method=method)) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_catalog_has_validators_gzip_and_304s(server):
    base, data = server
    body = json.dumps({"games": [{"title": f"Game {i}"} for i in range(500)]}).encode()
    (data / "catalog.json").write_bytes(body)
    status, h, got = get(base + "/api/catalog")
    assert (status, got) == (200, body)
    etag, modified = h["ETag"], h["Last-Modified"]
    assert etag.startswith('"') and h["Cache-Control"] == "no-cache" and h["Vary"] == "Accept-Encoding"
    assert "Content-Encoding" not in h
    # gzip for clients that take it, a different ETag for that representation
    status, hz, got = get(base + "/api/catalog", {"Accept-Encoding": "br, gzip"})
    assert status == 200 and hz["Content-Encoding"] == "gzip" and gzip.decompress(got) == body
    assert hz["ETag"] != etag and int(hz["Content-Length"]) == len(got) < len(body)
    assert get(base + "/api/catalog", {"Accept-Encoding": "gzip;q=0, *"})[1]["Content-Encoding"] is None
    # either ETag, or the date, revalidates
    for cond in ({"If-None-Match": etag}, {"If-None-Match": hz["ETag"]}, {"If-None-Match": f'"x", W/{etag}'},
                 {"If-Modified-Since": modified}):
        status, h304, got = get(base + "/api/catalog", cond)
        assert (status, got) == (304, b"") and h304["ETag"] == etag
    # If-None-Match wins over a date that alone would be a 304
    assert get(base + "/api/catalog", {"If-None-Match": '"other"', "If-Modified-Since": modified})[0] == 200
    # a new catalog (renamed into place, as the scan does) is a new ETag and a 200
    new = data / ".catalog.json.part"
    new.write_bytes(body.replace(b"Game 1", b"Game X"))
    new.replace(data / "catalog.json")
    status, h2, got = get(base + "/api/catalog", {"If-None-Match": etag})
    assert status == 200 and h2["ETag"] != etag and b"Game X" in got
    # HEAD: the same headers, no body
    status, hh, got = get(base + "/api/catalog", method="HEAD")
    assert status == 200 and got == b"" and hh["ETag"] == h2["ETag"]


def test_catalog_is_hashed_once_per_version(tmp_path, monkeypatch):
    calls = []
    real = web.Entity.of
    monkeypatch.setattr(web.Entity, "of", classmethod(lambda cls, *a: calls.append(1) or real(*a)))
    (tmp_path / "catalog.json").write_text('{"games": []}')
    cache = web.FileCache()
    assert cache.get(tmp_path / "catalog.json") is cache.get(tmp_path / "catalog.json")
    assert len(calls) == 1


def test_page_has_an_etag_and_revalidates(server):
    base, _ = server
    status, h, _ = get(base + "/", {"Accept-Encoding": "gzip"})
    assert status == 200 and h["Content-Encoding"] == "gzip" and h["ETag"]
    assert get(base + "/", {"If-None-Match": h["ETag"]})[0] == 304


def test_versioned_art_is_immutable_and_revalidates(server):
    base, data = server
    pic = data / "media" / "snes" / "Named_Boxarts" / "0123456789abcdef.png"
    pic.parent.mkdir(parents=True)
    pic.write_bytes(png(10, 10))
    url = base + "/media/snes/Named_Boxarts/0123456789abcdef.png"
    status, h, got = get(url + "?v=abc123")
    assert status == 200 and got == pic.read_bytes()
    assert h["Cache-Control"] == "public, max-age=31536000, immutable"
    assert get(url)[1]["Cache-Control"] == "public, max-age=604800"  # an old catalog's URL
    assert get(url, {"If-None-Match": h["ETag"]})[0] == 304
    assert get(url, {"If-Modified-Since": h["Last-Modified"]})[0] == 304


@pytest.mark.parametrize("header,ok", [
    ("gzip, deflate, br, zstd", True), ("GZIP", True), ("*", True), ("br", False), ("", False),
    ("gzip;q=0", False), ("gzip;q=0.5", True), ("*;q=0", False), ("gzip;q=0, *", False),
])
def test_accepts_gzip(header, ok):
    assert web.accepts_gzip(header) is ok
