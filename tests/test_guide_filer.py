import json

import pytest

from brick_games import guide_filer, sources, strategy

WANTED = [
    {"id": "1" * 16, "title": "Chrono Trigger", "system": "snes", "rank": 92, "grade": None,
     "gamefaqs": "x", "folder": "snes/media/Chrono Trigger"},
    {"id": "2" * 16, "title": "Final Fantasy III", "system": "snes", "rank": 91, "grade": "partial",
     "gamefaqs": "x", "folder": "snes/media/Final Fantasy III"},
    {"id": "3" * 16, "title": "Final Fantasy III", "system": "nes", "rank": 85, "grade": None,
     "gamefaqs": "x", "folder": "nes/media/Final Fantasy III"},
    {"id": "4" * 16, "title": "Ys", "system": "pce", "rank": 84, "grade": None, "gamefaqs": "x",
     "folder": "pce/media/Ys"},
]


@pytest.mark.parametrize("filename,ids", [
    ("Chrono Trigger - Guide and Walkthrough - Super Nintendo - By someone - GameFAQs.txt", ["1" * 16]),
    ("Final Fantasy III - FAQ - Super Nintendo - GameFAQs.txt", ["2" * 16]),  # the system settles it
    ("Final Fantasy III - FAQ - NES - GameFAQs.txt", ["3" * 16]),
    ("Final Fantasy III walkthrough.txt", ["2" * 16, "3" * 16]),  # no system: the user picks
    ("Ys - Guides and Walkthroughs.txt", ["4" * 16]),  # "ds" in "Guides" isn't the DS
    ("Keys to the castle.txt", []),  # a short title only counts at the start
    ("shopping list.txt", []),
])
def test_a_file_name_finds_its_game(filename, ids):
    assert [g["id"] for g in guide_filer.candidates(filename, WANTED)] == ids


@pytest.mark.parametrize("name", ["Chrono Trigger - Guide - GameFAQs.txt", "The Legend of Zelda, A Link to the Past"])
def test_the_names_match_the_scans(name):
    assert guide_filer.norm(name) == sources.norm(name)


def test_watching_files_a_guide_and_leaves_the_rest(tmp_path, monkeypatch):
    sent = []

    def fake_upload(path, game, host=None):
        sent.append((path.read_text(), game["id"]))
        return f"/extra/brick-games/{game['folder']}/guide-gamefaqs.txt"

    monkeypatch.setattr(guide_filer, "upload", fake_upload)
    (tmp_path / "Chrono Trigger - Guide and Walkthrough - GameFAQs.txt").write_text("Chrono Trigger FAQ\n=====\n")
    (tmp_path / "notes.txt").write_text("not a guide")
    (tmp_path / "Chrono Trigger page.txt").write_text("<!DOCTYPE html><html>")  # a saved page, not text
    guide_filer.watch(tmp_path, WANTED, once=True)
    assert sent == [("Chrono Trigger FAQ\n=====\n", "1" * 16)]
    assert (tmp_path / "filed" / "Chrono Trigger - Guide and Walkthrough - GameFAQs.txt").exists()
    assert (tmp_path / "notes.txt").exists() and (tmp_path / "Chrono Trigger page.txt").exists()


def test_the_scan_lists_the_games_wanting_a_guide(tmp_path, monkeypatch):
    lib = tmp_path / "library"
    (lib / "snes").mkdir(parents=True)
    (lib / "snes" / "media.json").write_text(json.dumps({"a" * 16: "Good", "b" * 16: "Partial", "c" * 16: "None"}))
    pages = {"Good": ("Guides at completion stage 4", "Text."), "Partial": ("Guides at completion stage 2", "Text.")}

    def api(**p):
        if p["action"] == "parse":
            return {"error": {"code": "missingtitle"}}
        if p.get("list") == "allpages":
            return {"query": {"allpages": []}}
        out = []
        for t in p["titles"].split("|"):
            if t not in pages:
                out.append({"title": t, "missing": True})
            elif p.get("prop") == "extracts":
                out.append({"title": t, "extract": pages[t][1]})
            else:
                cats = ["Category:Games", "Category:SNES", f"Category:{pages[t][0]}"]
                if "clcategories" in p:
                    cats = [c for c in cats if c in p["clcategories"].split("|")]
                out.append({"title": t, "categories": [{"title": c} for c in cats]})
        return {"query": {"pages": out}}

    monkeypatch.setattr(strategy, "api", api)
    games = [{"id": i * 16, "system": "snes", "title": t, "metascore": m}
             for i, t, m in (("a", "Good", 95), ("b", "Partial", 90), ("c", "None", 85), ("d", "Low", 60))]
    strategy.Strategy(tmp_path / "data", lib).add(games, {"snes": {"id": "snes", "strategywiki": ["SNES"]}})
    wanted = json.loads((tmp_path / "data" / "strategy" / "wanted.json").read_text())
    assert [(w["title"], w["grade"]) for w in wanted] == [("Partial", "partial"), ("None", None)]
    assert wanted[1]["folder"] == "snes/media/None"
    assert wanted[1]["gamefaqs"].startswith("https://gamefaqs.gamespot.com/search?game=")
