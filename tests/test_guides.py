import email.message
import json
import time
import urllib.error

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


def test_a_failure_keeps_the_last_list_and_gives_up_after_systems_in_a_row(tmp_path, monkeypatch):
    path = tmp_path / "guides" / "psx.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"fetched": 0, "platforms": ["Q10677"],
                                "records": [{"names": ["Final Fantasy VII"], "gamefaqs": "197341"}]}))
    calls = []

    def down(query):
        calls.append(query)
        raise OSError("connection refused")

    monkeypatch.setattr(guides, "sparql", down)
    monkeypatch.setattr(guides, "PAUSE", 0)
    g = guides.Guides(tmp_path)
    assert ratings.match(g.for_system(PSX), "Final Fantasy VII") == {"gamefaqs": "197341"}  # the last list
    for n in range(guides.GIVE_UP - 1):  # one heavy query isn't Wikidata down: the next system is asked
        assert g.for_system({"id": f"s{n}", "wikidata": ["Q1"]}) == {}
    assert len(calls) == guides.GIVE_UP
    assert g.for_system({"id": "ps2", "wikidata": ["Q10680"]}) == {}  # given up: not asked
    assert len(calls) == guides.GIVE_UP and g.results == {"fetched": 0, "cached": 0, "error": guides.GIVE_UP + 1}


def test_a_success_resets_the_count_toward_giving_up(tmp_path, monkeypatch):
    answers = iter([OSError("slow")] * (guides.GIVE_UP - 1) + [[]] * 2 + [OSError("slow")] * (guides.GIVE_UP - 1))

    def flaky(query):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(guides, "sparql", flaky)
    monkeypatch.setattr(guides, "PAUSE", 0)
    g = guides.Guides(tmp_path)
    for n in range(2 * guides.GIVE_UP - 1):
        g.for_system({"id": f"s{n}", "wikidata": ["Q1"]})
    assert g.results == {"fetched": 1, "cached": 0, "error": 2 * guides.GIVE_UP - 2}  # every one asked


def http_error(code, retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://query.wikidata.org/sparql", code, "x", headers, None)


@pytest.mark.parametrize("failure,wait", [
    pytest.param(http_error(429, "7"), 7, id="429-retry-after"),
    pytest.param(http_error(429, "99999"), guides.MAX_WAIT, id="429-capped"),
    pytest.param(http_error(429), "pause", id="429-no-header"),
    pytest.param(TimeoutError("The read operation timed out"), "pause", id="read-timeout"),
    pytest.param(urllib.error.URLError(TimeoutError("timed out")), "pause", id="connect-timeout"),
])
def test_sparql_tries_again_after_a_timeout_or_429(monkeypatch, failure, wait):
    tries, slept = [], []

    def ask(query):
        tries.append(query)
        if len(tries) == 1:
            raise failure
        return [{"ok": True}]

    monkeypatch.setattr(guides, "ask", ask)
    monkeypatch.setattr(guides.time, "sleep", slept.append)
    want = guides.PAUSE if wait == "pause" else wait  # PAUSE as patched for the test (conftest)
    assert guides.sparql("q") == [{"ok": True}] and len(tries) == 2 and slept == [want]


@pytest.mark.parametrize("failure", [
    pytest.param(http_error(500), id="server-error"),
    pytest.param(urllib.error.URLError("connection refused"), id="refused"),
])
def test_sparql_raises_other_failures_at_once(monkeypatch, failure):
    tries = []

    def ask(query):
        tries.append(query)
        raise failure

    monkeypatch.setattr(guides, "ask", ask)
    monkeypatch.setattr(guides.time, "sleep", lambda s: None)
    with pytest.raises(type(failure)):
        guides.sparql("q")
    assert len(tries) == 1


def test_sparql_gives_up_after_its_retries(monkeypatch):
    tries = []

    def ask(query):
        tries.append(query)
        raise TimeoutError("timed out")

    monkeypatch.setattr(guides, "ask", ask)
    monkeypatch.setattr(guides.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError):
        guides.sparql("q")
    assert len(tries) == guides.RETRIES + 1


def test_an_unexpected_answer_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(guides, "sparql", lambda query: [{"item": {}}])  # rows without the fields
    g = guides.Guides(tmp_path)
    assert g.for_system(PSX) == {} and g.results["error"] == 1


def test_a_cache_that_cannot_be_written_still_gives_the_list(tmp_path, wikidata):
    (tmp_path / "guides").write_text("a file where the directory goes")
    index = guides.Guides(tmp_path).for_system(PSX)
    assert ratings.match(index, "Final Fantasy VII") == {"gamefaqs": "197341", "strategywiki": "Final_Fantasy_VII"}


def test_a_system_without_platforms_asks_nothing(tmp_path, wikidata):
    g = guides.Guides(tmp_path)
    assert g.for_system({"id": "arcade"}) == {} and wikidata == []
    assert g.results == {"fetched": 0, "cached": 0, "error": 0}
