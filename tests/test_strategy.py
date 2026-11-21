import json
import urllib.error
import urllib.request

import pytest

from brick_games import strategy, web

SNES = {"id": "snes", "strategywiki": ["SNES"]}

# A small StrategyWiki: Chrono Trigger (stage 4, SNES) with a Table of Contents,
# Final Fantasy II (stage 3, NES only), and a stub with no Table of Contents.
PAGES = {
    "Chrono Trigger": {"cats": ["Games", "SNES", "Guides at completion stage 4"],
                       "text": "Chrono Trigger is an RPG.\n\n== Gameplay ==\nTime travel."},
    "Chrono Trigger/Table of Contents": {"wikitext": "* [[Chrono Trigger/Walkthrough|Walkthrough]]\n"
                                                     "* [[Chrono Trigger/Bosses]]\n* [[Chrono Trigger/Walkthrough]]"},
    "Chrono Trigger/Walkthrough": {"text": "== Guardia Castle ==\nTalk to the king.\n\t\t\n\t\n"
                                           "=== Secret ===\nA chest behind the throne."},
    "Chrono Trigger/Bosses": {"text": "== Magus ==\nUse Lucca's fire. Magus is weak to it."},
    "Final Fantasy II": {"cats": ["Games", "NES", "Guides at completion stage 3"], "text": "The NES game."},
    "Stub Game": {"cats": ["Games", "SNES", "Guides at completion stage 1"], "text": "Barely begun."},
    "Stub Game/Controls": {"text": "Press A."},
}


def fake_api(**p):
    if p["action"] == "parse":
        page = PAGES.get(p["page"])
        if page is None or "wikitext" not in page:
            return {"error": {"code": "missingtitle"}}
        return {"parse": {"wikitext": page["wikitext"]}}
    if p.get("list") == "allpages":
        return {"query": {"allpages": [{"title": t} for t in PAGES if t.startswith(p["apprefix"])]}}
    out = []
    for title in p["titles"].split("|"):
        page = PAGES.get(title)
        if page is None:
            out.append({"title": title, "missing": True})
        elif p.get("prop") == "extracts":
            out.append({"title": title, "extract": page.get("text", "")})
        else:
            cats = [f"Category:{c}" for c in page.get("cats", [])]
            if "clcategories" in p:
                cats = [c for c in cats if c in p["clcategories"].split("|")]
            out.append({"title": title, "categories": [{"title": c} for c in cats]})
    return {"query": {"pages": out}}


@pytest.fixture
def wiki(monkeypatch):
    monkeypatch.setattr(strategy, "api", fake_api)


def game(gid, title, **scores):
    return {"id": gid, "system": "snes", "title": title, **scores}


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "library"
    (root / "snes").mkdir(parents=True)
    (root / "snes" / "media.json").write_text(json.dumps(
        {"a" * 16: "Chrono Trigger", "b" * 16: "Final Fantasy II", "c" * 16: "Stub Game", "d" * 16: "Obscure"}))
    return root


def test_ranks_match_the_page():
    games = [game("1", "A", metascore=91), game("2", "B", critics=95, critic_reviews=10),
             game("3", "C", critics=99, critic_reviews=2), game("4", "D", critics=55, critic_reviews=10)]
    r = strategy.ranks(games)
    assert r["1"] == 91 and "3" not in r  # a metascore counts as is; under 10 reviews, no rank
    # weighted toward the catalog's critic mean ((95 + 99 + 55) / 3 = 83) as if 10 more reviews
    assert r["2"] == pytest.approx((10 * 95 + 10 * 83) / 20)


def test_a_title_needs_the_systems_platform(wiki):
    assert strategy.find_page(game("1", "Chrono Trigger"), SNES) == "Chrono Trigger"
    # SNES "Final Fantasy II" is Final Fantasy IV: StrategyWiki's page is the NES game
    assert strategy.find_page(game("2", "Final Fantasy II"), SNES) is None
    # Wikidata's link wins
    linked = game("2", "Final Fantasy II", guides={"strategywiki": "Final_Fantasy_IV"})
    assert strategy.find_page(linked, SNES) == "Final Fantasy IV"


def test_guides_for_games_ranked_80_and_up(wiki, tmp_path, lib):
    games = [game("a" * 16, "Chrono Trigger", metascore=92), game("b" * 16, "Final Fantasy II", metascore=90),
             game("c" * 16, "Stub Game", metascore=85), game("d" * 16, "Obscure", metascore=70)]
    st = strategy.Strategy(tmp_path / "data", lib)
    st.add(games, {"snes": SNES})
    ct, ff, stub, obscure = games
    assert ct["strategy"]["grade"] == "complete" and stub["strategy"]["grade"] == "stub"
    assert "strategy" not in ff and "strategy" not in obscure  # no SNES page; under 80
    assert st.results == {"fetched": 2, "cached": 0, "none": 1, "error": 0, "deferred": 0}
    assert st.grades == {"complete": 1, "stub": 1}
    folder = lib / "snes" / "media" / "Chrono Trigger"
    md = (folder / "guide.md").read_text()
    assert md.startswith("# Chrono Trigger\n\nFrom StrategyWiki, https://strategywiki.org/wiki/Chrono_Trigger")
    assert "CC BY-SA 4.0" in md and "\t" not in md
    # the Table of Contents' order, each subpage once
    assert md.index("## Walkthrough") < md.index("## Bosses") and md.count("## Walkthrough") == 1
    assert "### Guardia Castle" in md and "#### Secret" in md
    meta = json.loads((folder / "guide.json").read_text())
    assert [s["title"] for s in meta["sections"]][:3] == ["Overview", "Gameplay", "Walkthrough"]
    assert (tmp_path / "data" / "strategy" / f"{'a' * 16}.md").read_text() == md  # the NVMe copy
    # next scan: kept, not fetched again
    again = strategy.Strategy(tmp_path / "data", lib)
    again.add(games, {"snes": SNES})
    assert again.results["cached"] == 2 and again.results["fetched"] == 0


def test_a_hand_saved_gamefaqs_guide_is_indexed(wiki, tmp_path, lib):
    folder = lib / "snes" / "media" / "Obscure"
    folder.mkdir(parents=True)
    (folder / "guide-gamefaqs.txt").write_text("Intro\n=====\nHello.\n\nLevel 1\n-------\nGo right.\n")
    games = [game("d" * 16, "Obscure", metascore=70)]  # under 80: a hand-saved guide still counts
    strategy.Strategy(tmp_path / "data", lib).add(games, {"snes": SNES})
    assert games[0]["strategy"]["grade"] == "hand-saved"
    meta = json.loads((folder / "guide.json").read_text())
    assert [s["title"] for s in meta["sections"]] == ["GameFAQs guide (saved by hand)", "Intro", "Level 1"]


def test_a_failing_wiki_never_fails_the_scan(tmp_path, lib, monkeypatch):
    def down(**p):
        raise urllib.error.URLError("strategywiki down")

    monkeypatch.setattr(strategy, "api", down)
    games = [game("a" * 16, "Chrono Trigger", metascore=92)]
    st = strategy.Strategy(tmp_path / "data", lib)
    st.add(games, {"snes": SNES})
    assert st.results["error"] == 1 and "strategy" not in games[0]


def test_search_ranks_headings_and_words():
    md = "# G\n\n## Bosses\n\n### Magus\nUse fire on Magus.\n\n### Lavos\nThe last boss.\n"
    sections = strategy.index(md)
    found = strategy.search(md, sections, "magus fire")
    assert found[0]["title"] == "Magus" and "Use fire" in found[0]["text"]
    assert strategy.search(md, sections, "") == []


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_the_web_serves_guides(tmp_path, wiki, lib):
    from http.server import ThreadingHTTPServer
    import threading

    data = tmp_path / "data"
    games = [game("a" * 16, "Chrono Trigger", metascore=92)]
    strategy.Strategy(data, lib).add(games, {"snes": SNES})
    (data / "catalog.json").write_text(json.dumps({"games": games}))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(data))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, h, body = get(f"{base}/api/guides/{'a' * 16}")
        assert status == 200 and json.loads(body)["grade"] == "complete" and h["ETag"]
        status, h, body = get(f"{base}/api/guides/{'a' * 16}/text")
        assert status == 200 and h["Content-Type"].startswith("text/markdown") and body.startswith(b"# Chrono Trigger")
        status, _, body = get(f"{base}/api/guides/{'a' * 16}/search?q=magus+fire")
        assert json.loads(body)["sections"][0]["title"] == "Magus"
        status, _, body = get(f"{base}/api/guides?q=chrono")
        assert [g["title"] for g in json.loads(body)] == ["Chrono Trigger"]
        assert get(f"{base}/api/guides/{'f' * 16}")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()
