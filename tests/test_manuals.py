import io
import json
import urllib.error

import pytest

from brick_games import catalog, manuals, ratings

SNES = {"id": "snes", "manuals": ["SNESManuals"]}
NEOGEO = {"id": "neogeo", "arcade": True, "manuals": ["SNKNeoGeoMVSManuals"]}


def listing(*names):
    return io.BytesIO(json.dumps({"files": [{"name": n} for n in names]}).encode())


def test_item_index_keeps_pdfs_not_the_archives_ocr_copies(monkeypatch):
    monkeypatch.setattr(manuals, "get", lambda url: listing(
        "Super Metroid (Japan, USA) (En,Ja).pdf", "Super Metroid (Japan, USA) (En,Ja)_text.pdf",
        "Super Metroid (Japan, USA) (En,Ja)_jp2.zip", "SNESManuals_meta.xml"))
    assert manuals.item_index("SNESManuals") == {
        ratings.key("Super Metroid"): [manuals.Entry("SNESManuals", "Super Metroid (Japan, USA) (En,Ja).pdf")]}


INDEX = {
    ratings.key("Contra III - The Alien Wars"): [manuals.Entry("A", "Contra III - The Alien Wars (Europe).pdf"),
                                                 manuals.Entry("A", "Contra III - The Alien Wars (USA).pdf")],
    ratings.key("Phantasy Star II - Anne's Adventure"): [manuals.Entry("C", "Phantasy Star II - Anne's Adventure.pdf")],
    "aof": [manuals.Entry("B", "aof.pdf")],
}


@pytest.mark.parametrize("name,short,found", [
    pytest.param("Contra III - The Alien Wars (U) [!].smc", None, ("A", "Contra III - The Alien Wars (USA).pdf"),
                 id="usa-preferred"),
    pytest.param("Art of Fighting / Ryuuko no Ken (NGM-044)(NGH-044)", "aof", ("B", "aof.pdf"), id="arcade-short-name"),
    pytest.param("Unknown Game (USA).smc", None, None, id="no-manual"),
    pytest.param("Phantasy Star II (USA).md", None, None, id="exact-names-only"),
])
def test_find(name, short, found):
    assert manuals.find(INDEX, name, short) == found


@pytest.fixture
def shelf(tmp_path):
    return manuals.Manuals(tmp_path / "media")


@pytest.fixture
def dest(tmp_path):
    """A game's manual in the library."""
    return tmp_path / "library" / "snes" / "media" / "Super Metroid" / "manual.pdf"


def test_fetch_keeps_the_pdf_and_links_it_with_a_version(shelf, dest, monkeypatch):
    monkeypatch.setattr(manuals, "get", lambda url: io.BytesIO(b"%PDF-1.4 manual"))
    rel = shelf.fetch(SNES, "0123456789abcdef", manuals.Entry("SNESManuals", "Super Metroid (USA).pdf"), dest)
    path, _, v = rel.partition("?v=")
    assert path == "snes/manuals/0123456789abcdef.pdf" and len(v) == manuals.VERSION_LEN
    assert dest.read_bytes() == b"%PDF-1.4 manual"
    assert shelf.on_disk(SNES, "0123456789abcdef", dest) == rel  # the same URL while the file is the same
    assert shelf.results["ok"] == 1


@pytest.mark.parametrize("body,max_bytes", [
    pytest.param(b"<html>not found</html>", manuals.MAX_BYTES, id="not-a-pdf"),
    pytest.param(b"%PDF" + b"x" * 100, 50, id="too-big"),
])
def test_fetch_skips_what_it_wont_keep(shelf, dest, monkeypatch, body, max_bytes):
    monkeypatch.setattr(manuals, "get", lambda url: io.BytesIO(body))
    monkeypatch.setattr(manuals, "MAX_BYTES", max_bytes)
    monkeypatch.setattr(manuals, "CHUNK", 16)
    assert shelf.fetch(SNES, "0123456789abcdef", manuals.Entry("SNESManuals", "X.pdf"), dest) is None
    assert shelf.results["skipped"] == 1
    assert not list(dest.parent.iterdir())  # no .part left behind


@pytest.mark.parametrize("failure", [
    pytest.param(urllib.error.URLError("no connection"), id="no-connection"),
    pytest.param(RuntimeError("anything else"), id="anything-else"),
])
def test_a_failed_download_is_counted_not_raised(shelf, dest, monkeypatch, failure):
    def broken(url):
        raise failure

    monkeypatch.setattr(manuals, "get", broken)
    assert shelf.fetch(SNES, "0123456789abcdef", manuals.Entry("SNESManuals", "X.pdf"), dest) is None
    assert shelf.results["error"] == 1


def test_downloads_stop_when_the_budget_is_spent(tmp_path, dest, monkeypatch):
    monkeypatch.setattr(manuals, "get", lambda url: pytest.fail("downloaded after the budget"))
    shelf = manuals.Manuals(tmp_path / "media", budget=-1)
    assert shelf.fetch(SNES, "0123456789abcdef", manuals.Entry("SNESManuals", "X.pdf"), dest) is None
    assert shelf.results["deferred"] == 1


def test_an_unreadable_set_means_no_manuals_not_a_failed_scan(shelf, dest, monkeypatch):
    def broken(url):
        raise urllib.error.URLError("archive.org down")

    monkeypatch.setattr(manuals, "get", broken)
    games = [{"id": "0123456789abcdef", "key": "supermetroid", "source_name": "Super Metroid (USA).sfc"}]
    catalog.add_manuals(games, SNES, shelf, {"0123456789abcdef": dest.parent})
    assert "manual" not in games[0] and shelf.results["none"] == 1


def test_kept_manuals_are_linked_without_asking_the_archive(shelf, tmp_path, monkeypatch):
    folder = tmp_path / "library" / "neogeo" / "media" / "Art of Fighting"
    folder.mkdir(parents=True)
    (folder / "manual.pdf").write_bytes(b"%PDF kept")
    monkeypatch.setattr(manuals, "get", lambda url: pytest.fail("asked the Archive"))
    games = [{"id": "0123456789abcdef", "key": "aof", "source_name": "Art of Fighting"}]
    catalog.add_manuals(games, NEOGEO, shelf, {"0123456789abcdef": folder})
    assert games[0]["manual"].startswith("neogeo/manuals/0123456789abcdef.pdf?v=")
    assert shelf.results["cached"] == 1


def test_a_manual_kept_on_the_data_volume_moves_into_the_library(shelf, dest, monkeypatch):
    old = shelf.legacy / "snes" / "manuals" / "0123456789abcdef.pdf"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"%PDF from 0.6")
    monkeypatch.setattr(manuals, "get", lambda url: pytest.fail("downloaded again"))
    rel = shelf.on_disk(SNES, "0123456789abcdef", dest)
    assert rel.startswith("snes/manuals/0123456789abcdef.pdf?v=")
    assert dest.read_bytes() == b"%PDF from 0.6" and not old.exists()
    assert shelf.results["moved"] == 1
    assert shelf.on_disk(SNES, "0123456789abcdef", dest) == rel and shelf.results["cached"] == 1
