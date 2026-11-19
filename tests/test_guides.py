import json
import time

import pytest

from brick_games import guides, ratings

PSX = {"id": "psx", "wikidata": ["Q10677"]}


def row(item, gf, name, sw=None):
    r = {"item": {"value": f"http://www.wikidata.org/entity/{item}"}, "gf": {"value": gf}, "name": {"value": name}}
    if sw:
        r["sw"] = {"value": sw}
    return r


def test_queries_name_every_platform_and_split_tagged_from_untagged():
    tagged, untagged = guides.queries(["Q186437", "Q203992"])
    for q in (tagged, untagged):
        assert "VALUES ?plat { wd:Q186437 wd:Q203992 }" in q and "wdt:P9075" in q and 'LANG(?name) = "en"' in q
    assert "pq:P400 ?plat" in tagged and "FILTER NOT EXISTS" not in tagged
    assert "wdt:P400 ?plat" in untagged and "FILTER NOT EXISTS { ?st pq:P400 ?any }" in untagged


def test_records_merge_names_and_prefer_a_platform_tagged_id():
    tagged = [row("Q2", "196885", "Castlevania: Symphony of the Night"),
              row("Q2", "196885", "Symphony of the Night"),
              row("Q1", "197341", "Final Fantasy VII", sw="Final_Fantasy_VII")]
    untagged = [row("Q2", "100", "Castlevania: Symphony of the Night"),  # lower, but untagged
                row("Q3", "abc", "Not An Id"),
                row("Q4", "300", "Only Untagged"), row("Q4", "200", "Only Untagged")]
    assert guides.records(tagged, untagged) == [
        {"names": ["Final Fantasy VII"], "gamefaqs": "197341", "strategywiki": "Final_Fantasy_VII"},
        {"names": ["Castlevania: Symphony of the Night", "Symphony of the Night"], "gamefaqs": "196885"},
        {"names": ["Only Untagged"], "gamefaqs": "200"},  # of two untagged ids, the lowest
    ]


def test_build_index_keys_every_name_and_the_first_game_keeps_a_shared_one():
    index = guides.build_index([{"names": ["Tetris", "Shenmue (video game)"], "gamefaqs": "1"},
                                {"names": ["Tetris"], "gamefaqs": "2", "strategywiki": "Tetris"}])
    assert index[ratings.key("Tetris")] == {"gamefaqs": "1"}
    assert index[ratings.key("Shenmue")] == {"gamefaqs": "1"}  # the Wikipedia title's parenthesis goes


@pytest.fixture
def wikidata(monkeypatch):
    """Wikidata's answers, by query kind; the queries asked, in order."""
    asked = []
    answers = {"tagged": [row("Q1", "197341", "Final Fantasy VII", sw="Final_Fantasy_VII")], "untagged": []}

    def sparql(query):
        kind = "untagged" if "FILTER NOT EXISTS" in query else "tagged"
        asked.append(kind)
        return answers[kind]

    monkeypatch.setattr(guides, "sparql", sparql)
    monkeypatch.setattr(guides, "PAUSE", 0)
    return asked


def test_fetches_both_queries_caches_and_matches(tmp_path, wikidata):
    index = guides.Guides(tmp_path).for_system(PSX)
    assert wikidata == ["tagged", "untagged"]
    assert ratings.match(index, "Final Fantasy VII (USA) (Disc 1)") == {"gamefaqs": "197341",
                                                                         "strategywiki": "Final_Fantasy_VII"}
    kept = json.loads((tmp_path / "guides" / "psx.json").read_text())
    assert kept["platforms"] == ["Q10677"] and len(kept["records"]) == 1
    again = guides.Guides(tmp_path)
    assert again.for_system(PSX) == index and wikidata == ["tagged", "untagged"]  # from the cache
    assert again.results == {"fetched": 0, "cached": 1, "error": 0}


@pytest.mark.parametrize("change", [
    pytest.param({"fetched": 0}, id="stale"),
    pytest.param({"platforms": ["Q1"]}, id="other-platforms"),
])
def test_a_stale_or_replatformed_cache_is_asked_again(tmp_path, wikidata, change):
    path = tmp_path / "guides" / "psx.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"fetched": time.time(), "platforms": ["Q10677"], "records": []} | change))
    guides.Guides(tmp_path).for_system(PSX)
    assert wikidata == ["tagged", "untagged"]


@pytest.mark.parametrize("content", ["not json", "[]", '{"records": "no"}'])
def test_a_corrupt_cache_is_asked_again(tmp_path, wikidata, content):
    path = tmp_path / "guides" / "psx.json"
    path.parent.mkdir()
    path.write_text(content)
    assert guides.Guides(tmp_path).for_system(PSX)
    assert wikidata == ["tagged", "untagged"]


def test_a_failure_keeps_the_last_list_is_counted_and_asked_once(tmp_path, monkeypatch):
    path = tmp_path / "guides" / "psx.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"fetched": 0, "platforms": ["Q10677"],
                                "records": [{"names": ["Final Fantasy VII"], "gamefaqs": "197341"}]}))
    calls = []

    def down(query):
        calls.append(query)
        raise OSError("timed out")

    monkeypatch.setattr(guides, "sparql", down)
    g = guides.Guides(tmp_path)
    assert ratings.match(g.for_system(PSX), "Final Fantasy VII") == {"gamefaqs": "197341"}  # the last list
    assert g.for_system({"id": "ps2", "wikidata": ["Q10680"]}) == {}  # not asked again this run
    assert len(calls) == 1 and g.results == {"fetched": 0, "cached": 0, "error": 2}


def test_an_unexpected_answer_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(guides, "sparql", lambda query: [{"item": {}}])  # rows without the fields
    g = guides.Guides(tmp_path)
    assert g.for_system(PSX) == {} and g.results["error"] == 1


def test_a_cache_that_cannot_be_written_still_gives_the_list(tmp_path, wikidata):
    (tmp_path / "guides").write_text("a file where the directory goes")
    assert guides.Guides(tmp_path).for_system(PSX)


def test_a_system_without_platforms_asks_nothing(tmp_path, wikidata):
    g = guides.Guides(tmp_path)
    assert g.for_system({"id": "arcade"}) == {} and wikidata == []
    assert g.results == {"fetched": 0, "cached": 0, "error": 0}
