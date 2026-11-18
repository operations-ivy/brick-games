import json
import re
import time
import urllib.error

import pytest

from brick_games import ratings

SNES = {"id": "snes", "igdb": [19, 58]}
PS2 = {"id": "ps2", "igdb": [8], "rawg": [15]}
KEYS = {"IGDB_CLIENT_ID": "id", "IGDB_CLIENT_SECRET": "secret", "RAWG_KEY": "key"}


def test_key_drops_accents_and_tags():
    assert ratings.key("Pokémon Snap") == ratings.key("Pokemon Snap (USA).z64")
    assert ratings.key("The Legend of Zelda: A Link to the Past") == \
        ratings.key("Legend of Zelda, The - A Link to the Past (USA).sfc")


def test_match_exact_then_one_longer_name_never_a_shorter_one():
    index = ratings.build_index([
        {"names": ["Adventure Island II: Aliens in Paradise"], "critics": 70, "critic_reviews": 2},
        {"names": ["Mega Man"], "critics": 80, "critic_reviews": 5},
        {"names": ["Mega Man X2"], "critics": 81, "critic_reviews": 5},
        {"names": ["Mega Man X3"], "critics": 79, "critic_reviews": 5},
    ])
    assert ratings.match(index, "Adventure Island II (UE) [!].gb")["critics"] == 70
    assert ratings.match(index, "Mega Man (USA).nes")["critics"] == 80
    assert ratings.match(index, "Mega Man 7 (USA).sfc") is None  # not "Mega Man"
    assert ratings.match(index, "Mega Man X (USA).sfc") is None  # X2 or X3: ambiguous


def test_build_index_names_alternatives_and_keeps_the_most_reviewed():
    index = ratings.build_index([
        {"names": ["Contra III: The Alien Wars", "Super Probotector"], "critics": 88, "critic_reviews": 9},
        {"names": ["Contra III: The Alien Wars"], "users": 60, "user_reviews": 1},
    ])
    assert index[ratings.key("Super Probotector (Europe)")]["critics"] == 88
    assert index[ratings.key("Contra III - The Alien Wars (USA)")] == {"critics": 88, "critic_reviews": 9}


def test_igdb_record_rounds_scores_and_keeps_only_what_is_known():
    game = {"id": 1, "name": "Chrono Trigger", "alternative_names": [{"id": 2, "name": "Kurono Torigaa"}],
            "aggregated_rating": 92.4, "aggregated_rating_count": 11}
    assert ratings.igdb_record(game) == {"names": ["Chrono Trigger", "Kurono Torigaa"],
                                         "critics": 92, "critic_reviews": 11}


def test_igdb_query_pages_by_id_over_every_platform():
    q = ratings.igdb_query([19, 58], 1234)
    assert "platforms = (19,58)" in q and "id > 1234" in q and "sort id asc" in q and "limit 500" in q


@pytest.fixture
def api(monkeypatch):
    """IGDB, Twitch and RAWG without the network; `calls` lists the URLs asked for."""
    calls = []
    monkeypatch.setattr(ratings, "IGDB_PAUSE", 0)
    monkeypatch.setattr(ratings, "IGDB_PAGE", 2)
    igdb_games = [{"id": i, "name": f"Game {i}", "aggregated_rating": 70 + i, "aggregated_rating_count": 3}
                  for i in (1, 2, 3)]

    def request(url, body=None, headers=None):
        calls.append(url)
        if url.startswith(ratings.TWITCH_TOKEN):
            return {"access_token": "t"}
        if url == ratings.IGDB_GAMES:
            assert headers["Authorization"] == "Bearer t" and headers["Client-ID"] == "id"
            after = int(re.search(r"id > (\d+)", body.decode()).group(1))
            return [g for g in igdb_games if g["id"] > after][:ratings.IGDB_PAGE]
        if url.startswith(ratings.RAWG_GAMES):
            if "page=2" in url:
                return {"results": [{"name": "Game 3", "metacritic": 91}], "next": None}
            return {"results": [{"name": "Game 1", "metacritic": 85}, {"name": "Unscored", "metacritic": None}],
                    "next": url + "&page=2"}
        raise AssertionError(url)

    monkeypatch.setattr(ratings, "request", request)
    return calls


def test_fetches_pages_caches_and_scores(tmp_path, api):
    rater = ratings.Ratings(tmp_path, KEYS)
    scored = rater.for_system(PS2)
    assert ratings.scores(scored, "Game 1 (USA).iso") == {"critics": 71, "critic_reviews": 3, "metascore": 85}
    assert ratings.scores(scored, "Game 3 (USA).iso") == {"critics": 73, "critic_reviews": 3, "metascore": 91}
    assert ratings.scores(scored, "Game 2 (USA).iso") == {"critics": 72, "critic_reviews": 3}
    assert ratings.scores(scored, "Unknown (USA).iso") == {}
    assert sum(u == ratings.IGDB_GAMES for u in api) == 2  # 3 games, 2 a page
    # a second scan reads the cache
    api.clear()
    assert ratings.Ratings(tmp_path, KEYS).for_system(PS2) == scored
    assert api == []


def test_stale_or_replatformed_cache_is_fetched_again(tmp_path, api):
    rater = ratings.Ratings(tmp_path, KEYS)
    rater.for_system(SNES)
    path = tmp_path / "ratings" / "igdb-snes.json"
    old = json.loads(path.read_text())
    path.write_text(json.dumps(old | {"fetched": time.time() - ratings.MAX_AGE - 1}))
    api.clear()
    ratings.Ratings(tmp_path, KEYS).for_system(SNES)
    assert ratings.IGDB_GAMES in api
    api.clear()
    ratings.Ratings(tmp_path, KEYS).for_system(SNES | {"igdb": [19]})
    assert ratings.IGDB_GAMES in api


def test_without_keys_nothing_is_fetched_but_a_cache_is_used(tmp_path, api):
    assert ratings.Ratings(tmp_path, {}).for_system(PS2) == ({}, {})
    assert api == []
    ratings.Ratings(tmp_path, KEYS).for_system(PS2)
    api.clear()
    stale = tmp_path / "ratings" / "igdb-ps2.json"
    stale.write_text(json.dumps(json.loads(stale.read_text()) | {"fetched": 0}))
    igdb, _ = ratings.Ratings(tmp_path, {}).for_system(PS2)
    assert igdb and api == []


def test_a_failed_refresh_keeps_the_old_list_and_bad_keys_stop_igdb(tmp_path, api, monkeypatch):
    ratings.Ratings(tmp_path, KEYS).for_system(PS2)
    for f in (tmp_path / "ratings").iterdir():
        f.write_text(json.dumps(json.loads(f.read_text()) | {"fetched": 0}))

    def refused(url, body=None, headers=None):
        api.append(url)
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(ratings, "request", refused)
    api.clear()
    rater = ratings.Ratings(tmp_path, KEYS)
    igdb, rawg = rater.for_system(PS2)
    assert igdb and rawg  # the old lists
    assert rater.igdb_down
    api.clear()
    rater.for_system(SNES)  # no cache, and IGDB isn't asked again
    assert not any(u.startswith(ratings.TWITCH_TOKEN) or u == ratings.IGDB_GAMES for u in api)


def test_a_system_without_platforms_has_no_ratings(tmp_path, api):
    assert ratings.Ratings(tmp_path, KEYS).for_system({"id": "x"}) == ({}, {})
    assert api == []
