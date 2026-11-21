import gzip
import hashlib
import io
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from brick_games import cache, catalog, guides, library, manuals, ratings, sources, web


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


WII = {"id": "wii", "folders": ["Wii"], "exts": ["wbfs", "iso"]}


@pytest.mark.parametrize("rel,name", [
    pytest.param("Wii/Xenoblade Chronicles/SX4E01.wbfs", "Xenoblade Chronicles.wbfs", id="disc-id-in-a-folder"),
    pytest.param("Wii/Xenoblade Chronicles [SX4E01]/SX4E01.wbfs", "Xenoblade Chronicles [SX4E01].wbfs",
                 id="folder-with-the-id"),
    pytest.param("Wii/Super Mario Galaxy 2 v1.01/game.iso", "Super Mario Galaxy 2 v1.01.iso", id="nintendont"),
    pytest.param("Wii/SX4E01.wbfs", "SX4E01.wbfs", id="no-folder-of-its-own"),
    pytest.param("Wii/Xenoblade Chronicles (USA)/Xenoblade Chronicles (USA).wbfs",
                 "Xenoblade Chronicles (USA).wbfs", id="named-file"),
])
def test_source_name(rel, name):
    assert catalog.source_name(Path("/roms") / rel, WII) == name


def test_disc_id_dumps_are_named_by_their_folder():
    files = [Path("/roms/Wii/Xenoblade Chronicles [SX4E01]/SX4E01.wbfs"),
             Path("/roms/Wii/Super Mario Galaxy 2 v1.01/game.iso")]
    assert [g["title"] for g in catalog.group(files, WII, {}, {})] == ["Super Mario Galaxy 2 v1.01",
                                                                     "Xenoblade Chronicles"]


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

    def thumb_indexes(system, cache=None):
        indexes = {}
        for kind, names in thumbs.items():
            for name in names:
                indexes.setdefault(kind, {}).setdefault(sources.norm(name), []).append(name)
            indexes.setdefault(kind, {})
        return indexes

    monkeypatch.setattr(sources, "thumb_indexes", thumb_indexes)
    monkeypatch.setattr(sources, "fetch_thumb", lambda system, kind, name: png())
    monkeypatch.setattr(sources, "metadata", lambda system: {sources.norm("Final Fight (USA)"): {"year": "1991", "genre": "Beat'em up"}})
    monkeypatch.setattr(catalog, "load_fbneo", lambda: (
        {"sf2": "Street Fighter II (World)", "sf2ua": "Street Fighter II (US rev A)"}, {"sf2ua": "sf2"}))
    scores = {"snes": ratings.build_index([{"names": ["Final Fight"], "critics": 77, "critic_reviews": 6}]),
              "dreamcast": ratings.build_index([{"names": ["Shenmue"], "metascore": 88}])}
    monkeypatch.setattr(ratings.Ratings, "for_system", lambda self, system: (scores.get(system["id"], {}),))
    monkeypatch.setattr(manuals, "item_index", lambda item: (
        {ratings.key("Final Fight (USA).pdf"): [manuals.Entry(item, "Final Fight (USA).pdf")]} if item == "SNESManuals" else {}))
    monkeypatch.setattr(manuals, "get", lambda url: io.BytesIO(b"%PDF-1.4 a manual"))
    final_fight = {"item": {"value": "http://www.wikidata.org/entity/Q1"}, "gf": {"value": "588397"},
                   "sw": {"value": "Final_Fight"}, "name": {"value": "Final Fight"}}
    monkeypatch.setattr(guides, "PAUSE", 0)
    # SNES's tagged query finds Final Fight; every other query, nothing
    monkeypatch.setattr(guides, "sparql", lambda q: [final_fight] if "wd:Q183259" in q and "pq:P400 ?plat" in q
                        and "FILTER NOT EXISTS" not in q else [])


def dirs(tmp_path):
    """roms, data and the library (on one filesystem, as on the drive)."""
    roms, data, lib = tmp_path / "roms", tmp_path / "data", tmp_path / "library"
    make_tree(roms)
    lib.mkdir()
    return roms, data, lib


def test_scan_groups_games_fetches_art_and_writes_the_catalog(tmp_path, offline):
    roms, data, lib = dirs(tmp_path)
    stats = catalog.Stats()
    cat = catalog.scan(roms, data, lib, stats)
    by_title = {g["title"]: g for g in cat["games"]}
    # two regions of one game, one entry; the .txt ignored
    ff = by_title["Final Fight"]
    assert len(ff["files"]) == 2 and ff["year"] == "1991" and ff["genre"] == "Beat'em up"
    assert ff["critics"] == 77 and ff["critic_reviews"] == 6 and "metascore" not in ff
    assert ff["manual"].startswith(f"snes/manuals/{ff['id']}.pdf?v=")
    folder = lib / "snes" / "media" / "Final Fight"  # the library: one folder per game, by title
    assert (folder / "manual.pdf").read_bytes().startswith(b"%PDF")
    assert library.read_index(lib, "snes") == {ff["id"]: "Final Fight"}
    with Image.open(folder / "box.png") as im:
        assert im.format == "PNG" and im.size == (600, 800)  # the original, as downloaded
    assert by_title["Shenmue"]["metascore"] == 88
    assert ff["guides"] == {"gamefaqs": "588397", "strategywiki": "Final_Fight"}
    assert "guides" not in by_title["Shenmue"]  # no match: the page links a GameFAQs search
    box, _, version = ff["art"]["box"].partition("?v=")
    assert box.endswith(".avif") and (data / "media" / box).exists()
    with Image.open(data / "media" / box) as im:
        assert im.format == "AVIF" and max(im.size) <= catalog.ART_SIZES["Named_Boxarts"].size
    # the art's URL carries its content hash, and a cached picture keeps it
    assert version == hashlib.sha256((data / "media" / box).read_bytes()).hexdigest()[:catalog.ART_VERSION_LEN]
    # two discs, one game
    assert by_title["Shenmue"]["discs"] == 2
    # a clone grouped under its parent, and the BIOS left out
    sf2 = by_title["Street Fighter II"]
    assert sorted(Path(f).name for f in sf2["files"]) == ["sf2.zip", "sf2ua.zip"]
    assert "neogeo" not in json.dumps(cat["games"])
    assert stats.per_system["snes"] == {"files": 2, "games": 1, "with_box_art": 1, "with_rating": 1,
                                        "with_manual": 1, "with_guide": 1}
    assert json.loads((data / "catalog.json").read_text())["games"] == cat["games"]
    # a second scan downloads nothing again
    before = dict(stats.fetches)
    written = (data / "catalog.json").stat().st_mtime_ns
    again = catalog.scan(roms, data, lib, stats)
    # nothing changed, so the file (and the page's ETag) is left as it was
    assert (data / "catalog.json").stat().st_mtime_ns == written
    assert stats.fetches == before
    assert stats.manuals["ok"] == 0 and stats.manuals["cached"] == 1  # kept, not downloaded again
    assert stats.guides == {"fetched": 0, "cached": 2, "error": 0}  # snes and dreamcast, from DATA/guides
    assert {g["title"]: g["art"] for g in again["games"]}["Final Fight"] == ff["art"]


def test_scan_without_network_still_lists_games(tmp_path, monkeypatch):
    roms, data, lib = dirs(tmp_path)

    def down(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(sources, "get", down)
    monkeypatch.setattr(sources, "github", down)
    monkeypatch.setattr(ratings, "request", down)
    monkeypatch.setattr(manuals, "get", down)
    monkeypatch.setattr(guides, "sparql", down)
    keys = {"IGDB_CLIENT_ID": "i", "IGDB_CLIENT_SECRET": "s", "RAWG_KEY": "k"}  # keys set, network down
    stats = catalog.Stats()
    cat = catalog.scan(roms, data, lib, stats, rater=ratings.Ratings(data, keys))
    assert {g["title"] for g in cat["games"]} >= {"Final Fight", "Shenmue"}
    assert all(not g["art"] for g in cat["games"])
    assert (data / "catalog.json").exists()  # ratings down doesn't fail the scan
    # one per system with games (RAWG has only the Dreamcast of the two)
    assert (stats.ratings["igdb"]["error"], stats.ratings["rawg"]["error"]) == (2, 1)
    assert stats.guides["error"] == 2 and not any("guides" in g for g in cat["games"])  # both systems, no links


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


def test_kept_art_is_listed_when_the_thumbnails_are_unreachable(tmp_path, offline, monkeypatch):
    """2026-11-20: thumbnails.libretro.com went empty and every picture left the catalog."""
    roms, data, lib = dirs(tmp_path)
    first = {g["title"]: g["art"] for g in catalog.scan(roms, data, lib)["games"]}

    def down(system, cache=None):
        raise OSError("HTTP Error 404: Not Found")

    monkeypatch.setattr(sources, "thumb_indexes", down)
    again = {g["title"]: g["art"] for g in catalog.scan(roms, data, lib)["games"]}
    assert again["Final Fight"] == first["Final Fight"] and again["Final Fight"]["box"]


def test_art_is_built_from_the_kept_original_and_rebuilt_when_it_changes(tmp_path, monkeypatch):
    cache_dir, original = tmp_path / "media", tmp_path / "library" / "snes" / "media" / "T" / "title.png"
    original.parent.mkdir(parents=True)
    original.write_bytes(png(320, 224))
    monkeypatch.setattr(sources, "fetch_thumb", lambda *a: pytest.fail("downloaded again"))
    system = {"id": "snes", "libretro": "x"}
    rel, result = catalog.get_art(cache_dir, system, "Named_Titles", "T.png", "0123456789abcdef", original,
                                  catalog.Downloads())
    assert result == "built" and rel.startswith("snes/Named_Titles/0123456789abcdef.avif?v=")
    with Image.open(cache_dir / rel.partition("?")[0]) as im:
        assert im.format == "AVIF" and im.size == (320, 224)  # screens keep their size
    # unchanged: nothing to do; a newer original: built again, with a new version
    assert catalog.get_art(cache_dir, system, "Named_Titles", None, "0123456789abcdef", original,
                           catalog.Downloads()) == (rel, None)
    original.write_bytes(png(256, 224))
    later = (cache_dir / rel.partition("?")[0]).stat().st_mtime + 10
    os.utime(original, (later, later))
    rel2, result = catalog.get_art(cache_dir, system, "Named_Titles", None, "0123456789abcdef", original,
                                   catalog.Downloads())
    assert result == "built" and rel2 != rel


def test_a_rate_limit_defers_the_rest_of_the_downloads(tmp_path, monkeypatch):
    calls = []

    def limited(*a):
        calls.append(a)
        raise sources.RateLimited("429")

    monkeypatch.setattr(sources, "fetch_thumb", limited)
    downloads, system = catalog.Downloads(), {"id": "snes", "libretro": "x"}
    for i in range(3):
        original = tmp_path / "library" / f"g{i}" / "box.png"
        assert catalog.get_art(tmp_path / "media", system, "Named_Boxarts", "X.png", f"{i:016x}", original,
                               downloads) == (None, "deferred")
    assert len(calls) == 1 and downloads.limited


def test_the_scan_needs_the_drive(tmp_path, offline):
    roms, data, lib = dirs(tmp_path)
    lib.rmdir()
    with pytest.raises(catalog.NoDrive):
        catalog.scan(roms, data, lib)
    assert not (data / "catalog.json").exists()


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
    avif = pic.with_suffix(".avif")
    avif.write_bytes(catalog.shrink(png(10, 10), "Named_Boxarts"))
    with urllib.request.urlopen(base + "/media/snes/Named_Boxarts/0123456789abcdef.avif") as r:
        assert r.headers["Content-Type"] == "image/avif" and r.read() == avif.read_bytes()
    for bad in ("/media/../catalog.json", "/media/snes/Named_Boxarts/../../catalog.json", "/media/x.png", "/etc/passwd"):
        assert fetch(base + bad)[0] == 404
    assert fetch(base + "/metrics")[0] == 200


def put_manual(data: Path, body: bytes, game_id: str = "0123456789abcdef") -> Path:
    """A manual in the library (the web app's default, DATA/library), filed by media.json."""
    lib = data / "library"
    pdf = lib / "snes" / "media" / "Super Metroid" / "manual.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(body)
    (lib / "snes" / "media.json").write_text(json.dumps({game_id: "Super Metroid"}))
    return pdf


def test_web_streams_manuals_as_pdf(server):
    base, data = server
    body = b"%PDF-1.4 " + bytes(range(256)) * 4000  # ~1 MB: several stream chunks
    put_manual(data, body)
    with urllib.request.urlopen(base + "/media/snes/manuals/0123456789abcdef.pdf?v=1") as r:
        assert r.headers["Content-Type"] == "application/pdf"
        assert int(r.headers["Content-Length"]) == len(body) and r.read() == body
        assert "immutable" in r.headers["Cache-Control"]
        tag = r.headers["ETag"]
    assert get(base + "/media/snes/manuals/0123456789abcdef.pdf", {"If-None-Match": tag})[0] == 304
    head = get(base + "/media/snes/manuals/0123456789abcdef.pdf", method="HEAD")
    assert head[0] == 200 and head[2] == b""
    for bad in ("/media/snes/manuals/0123456789abcdef.avif", "/media/snes/Named_Boxarts/0123456789abcdef.pdf",
                "/media/snes/manuals/../manuals/0123456789abcdef.pdf"):
        assert fetch(base + bad)[0] == 404


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
    stats.ratings = {"igdb": {"fetched": 0, "cached": 3, "error": 2, "no_key": 0}}
    stats.manuals = {"ok": 4, "deferred": 9}
    run = Run()
    publish(run, stats)
    assert ("games_catalog_games", 1, {"system": "snes"}) in run.gauges
    assert ("games_art_fetches", 1, {"result": "ok"}) in run.gauges
    assert ("games_ratings_fetches", 2, {"source": "igdb", "result": "error"}) in run.gauges
    assert ("games_ratings_fetches", 0, {"source": "igdb", "result": "fetched"}) in run.gauges
    assert ("games_manual_fetches", 9, {"result": "deferred"}) in run.gauges


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


def test_manuals_come_from_nvme_after_the_first_read(server):
    base, data = server
    origin = put_manual(data, b"%PDF-1.4 the manual")
    url = base + "/media/snes/manuals/0123456789abcdef.pdf?v=1"
    status, h, got = get(url)
    hot = data / "manual-cache" / "snes" / "0123456789abcdef.pdf"
    assert status == 200 and got == b"%PDF-1.4 the manual" and hot.read_bytes() == got
    origin.unlink()  # from now on only the cache has it
    status, h2, got = get(url)
    assert status == 200 and got == b"%PDF-1.4 the manual" and h2["ETag"] == h["ETag"]


def test_a_manual_not_cached_is_503_with_the_drive_offline(server):
    base, data = server
    put_manual(data, b"%PDF-1.4 cached", "00000000000000aa")
    assert get(base + "/media/snes/manuals/00000000000000aa.pdf")[0] == 200
    shutil.rmtree(data / "library")  # the drive unplugged
    assert get(base + "/media/snes/manuals/00000000000000aa.pdf")[0] == 200  # still on NVMe
    status, h, _ = get(base + "/media/snes/manuals/00000000000000bb.pdf")
    assert status == 503 and h["Retry-After"]


def test_the_manual_cache_drops_the_least_recently_opened(tmp_path):
    lib = tmp_path / "library"
    index = {}
    for i in range(3):
        game_id = f"{i:016x}"
        folder = lib / "snes" / "media" / f"Game {i}"
        folder.mkdir(parents=True)
        (folder / "manual.pdf").write_bytes(b"%PDF" + b"x" * 96)  # 100 bytes each
        index[game_id] = f"Game {i}"
    (lib / "snes" / "media.json").write_text(json.dumps(index))
    shelf = cache.ManualCache(tmp_path / "manual-cache", lib, limit=250)
    for game_id in (f"{0:016x}", f"{1:016x}"):
        f, tier = shelf.open("snes", game_id)
        f.close()
        assert tier == "origin"
    time.sleep(0.01)
    shelf.open("snes", f"{0:016x}")[0].close()  # 0 opened again: 1 is now the oldest
    time.sleep(0.01)
    shelf.open("snes", f"{2:016x}")[0].close()  # over 250 bytes: 1 goes
    kept = sorted(f.stem for f in (tmp_path / "manual-cache" / "snes").iterdir())
    assert kept == [f"{0:016x}", f"{2:016x}"]


def test_a_new_original_keeps_the_cached_avif(tmp_path, monkeypatch):
    """0.7.0's first fill: every download re-encoded art already cached (and changed its URL)."""
    cache_dir = tmp_path / "media"
    dest = cache_dir / "snes" / "Named_Boxarts" / "0123456789abcdef.avif"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(catalog.shrink(png(), "Named_Boxarts"))
    old = 1_700_000_000
    os.utime(dest, (old, old))
    monkeypatch.setattr(sources, "fetch_thumb", lambda *a: png())
    monkeypatch.setattr(catalog, "shrink", lambda *a: pytest.fail("built again"))
    original = tmp_path / "library" / "snes" / "media" / "G" / "box.png"
    rel, result = catalog.get_art(cache_dir, {"id": "snes", "libretro": "x"}, "Named_Boxarts", "G.png",
                                  "0123456789abcdef", original, catalog.Downloads())
    assert result == "ok" and original.exists() and original.stat().st_mtime == dest.stat().st_mtime
    assert rel == catalog.versioned("snes/Named_Boxarts/0123456789abcdef.avif", dest.read_bytes())
