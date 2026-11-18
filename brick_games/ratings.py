"""Ratings: critic and user scores from IGDB, and the Metascore through RAWG.

Metacritic has no API, so its score comes from RAWG, which carries it.

- IGDB (api.igdb.com, with Twitch app credentials IGDB_CLIENT_ID and
  IGDB_CLIENT_SECRET): `aggregated_rating` is the average of critic reviews,
  `rating` IGDB's own users'. Each system's platforms (systems.yaml `igdb`) are
  read 500 games at a time, only games with a rating. All eras.
- RAWG (api.rawg.io, RAWG_KEY): `metacritic`, the Metascore, for games that
  have one (systems.yaml `rawg`). Metacritic starts in about 1995, so the
  8- and 16-bit systems have none.

Both are optional: without credentials the catalog lists every game as
before, without the missing scores. Each system's list is kept in
DATA/ratings/<source>-<system>.json and fetched again after a week.

A source never fails the scan (the user, 2026-11-18): whatever goes wrong
(no connection, a bad key, an answer that isn't what's expected), the scan
logs a warning, keeps the last list, stops asking that source for the rest of
the run, and counts it in `results`. The scan pushes those counts as
games_ratings_fetches{source,result}; brick9000's board shows a failure in
amber and pages nobody.

Matching is by `key`: sources.norm without accents ("Pokémon" is "pokemon").
A name with no exact match takes a longer one that starts with it, if just one
does ("Adventure Island II" finds "Adventure Island II: Aliens in Paradise").
Unlike art, a shorter one never matches: "Mega Man 7" isn't "Mega Man". Nor
does a longer one that goes on with a number: "Earthworm Jim 2" isn't
"Earthworm Jim" (a sequel, not a subtitle).
"""

from __future__ import annotations

import json
import os
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from collections.abc import Callable
from typing import Any, TypeVar

import brick_observe

from . import sources

log = brick_observe.get_logger(__name__)
T = TypeVar("T")  # what an index holds: scores (ratings), file lists (manuals)
TWITCH_TOKEN = "https://id.twitch.tv/oauth2/token"  # secret-scan: allow (public endpoint URL)
IGDB_GAMES = "https://api.igdb.com/v4/games"
RAWG_GAMES = "https://api.rawg.io/api/games"
IGDB_PAGE = 500  # IGDB's most per query
RAWG_PAGE = 40  # RAWG's most per page
IGDB_PAUSE = 0.3  # IGDB allows 4 requests a second
MAX_AGE = 7 * 24 * 3600
MIN_PREFIX = 8  # a prefix match needs at least this many characters of key
TIMEOUT = 60
SOURCES = ("igdb", "rawg")
# What happened to each system's list, per source, in one scan:
#   fetched  asked the source and got it
#   cached   the kept list was fresh enough
#   error    the source failed (the kept list, if any, was used)
#   no_key   no credentials for the source
RESULTS = ("fetched", "cached", "error", "no_key")


def key(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return sources.norm(plain)


def match(index: dict[str, T], name: str) -> T | None:
    k = key(name)
    if k in index:
        return index[k]
    if len(k) < MIN_PREFIX:
        return None
    # a subtitle, not a sequel: what follows the name mustn't start with a number
    longer = [v for i, v in index.items() if i.startswith(k) and not i.removeprefix(k)[:1].isdigit()]
    return longer[0] if len(longer) == 1 else None


def request(url: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "brick-games", **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return json.loads(response.read())


def igdb_query(platforms: list[int], after: int) -> str:
    """One page of a platform's rated games, by id (IGDB pages by `id >` best)."""
    ids = ",".join(str(p) for p in platforms)
    return (f"fields name,alternative_names.name,aggregated_rating,aggregated_rating_count,rating,rating_count;"
            f" where platforms = ({ids}) & id > {after} & (aggregated_rating != null | rating != null);"
            f" sort id asc; limit {IGDB_PAGE};")


def igdb_record(game: dict) -> dict:
    """An IGDB game as {names, critics, critic_reviews, users, user_reviews}, scores 0-100."""
    names = [game["name"], *(a["name"] for a in game.get("alternative_names", []) if a.get("name"))]
    out: dict[str, Any] = {"names": names}
    if game.get("aggregated_rating") is not None:
        out |= {"critics": round(game["aggregated_rating"]), "critic_reviews": game.get("aggregated_rating_count", 0)}
    if game.get("rating") is not None:
        out |= {"users": round(game["rating"]), "user_reviews": game.get("rating_count", 0)}
    return out


def rawg_record(game: dict) -> dict:
    return {"names": [game["name"]], "metascore": game["metacritic"]}


def reviews(record: dict) -> int:
    return record.get("critic_reviews", 0) + record.get("user_reviews", 0)


def build_index(records: list[dict]) -> dict[str, dict]:
    """key(name) -> the record, for every name of every game. Two games with
    one name (a remake on the same platform): the one with more reviews."""
    index: dict[str, dict] = {}
    for record in records:
        rated = {k: v for k, v in record.items() if k != "names"}
        for name in record["names"]:
            k = key(name)
            if k and (k not in index or reviews(rated) > reviews(index[k])):
                index[k] = rated
    return index


def scores(indexes: tuple[dict[str, dict], ...], name: str) -> dict:
    """A game's scores by its name: those known of critics, critic_reviews,
    users, user_reviews and metascore."""
    out: dict = {}
    for index in indexes:
        out |= match(index, name) or {}
    return out


def has_score(game: dict) -> bool:
    """Whether a catalog game has a critics' score (the scan's with_rating)."""
    return "critics" in game or "metascore" in game


class Ratings:
    """Each system's scores, from the cache in DATA/ratings or the sources."""

    def __init__(self, data: Path, env: dict[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.cache_dir = data / "ratings"
        self.igdb_id = env.get("IGDB_CLIENT_ID", "")
        self.igdb_secret = env.get("IGDB_CLIENT_SECRET", "")
        self.rawg_key = env.get("RAWG_KEY", "")
        self._token: str | None = None
        self.down: set[str] = set()  # sources that failed this run: not asked again
        self.results = {source: dict.fromkeys(RESULTS, 0) for source in SOURCES}

    def token(self) -> str:
        if self._token is None:
            query = urllib.parse.urlencode({"client_id": self.igdb_id, "client_secret": self.igdb_secret,
                                            "grant_type": "client_credentials"})
            self._token = request(f"{TWITCH_TOKEN}?{query}", body=b"")["access_token"]
        return self._token

    def igdb(self, platforms: list[int]) -> list[dict]:
        headers = {"Client-ID": self.igdb_id, "Authorization": f"Bearer {self.token()}"}
        out, after = [], 0
        while True:
            time.sleep(IGDB_PAUSE)
            page = request(IGDB_GAMES, igdb_query(platforms, after).encode(), headers)
            out += [igdb_record(g) for g in page]
            if len(page) < IGDB_PAGE:
                return out
            after = page[-1]["id"]

    def rawg(self, platforms: list[int]) -> list[dict]:
        query = urllib.parse.urlencode({"key": self.rawg_key, "platforms": ",".join(map(str, platforms)),
                                        "metacritic": "1,100", "page_size": RAWG_PAGE})
        out, url = [], f"{RAWG_GAMES}?{query}"
        while url:
            page = request(url)
            out += [rawg_record(g) for g in page["results"] if g.get("metacritic")]
            url = page.get("next")
        return out

    def cached(self, source: str, system: dict, fetch: Callable[[list[int]], list[dict]],
               has_key: bool) -> list[dict]:
        """The system's records: the cache while it's fresh and for the same
        platforms, else fetched (and kept); the old cache if that fails.
        Never raises."""
        platforms = system.get(source)
        if not platforms:
            return []
        path = self.cache_dir / f"{source}-{system['id']}.json"
        try:
            old = json.loads(path.read_text())
        except (OSError, ValueError):
            old = None
        if not isinstance(old, dict) or not isinstance(old.get("records"), list):
            old = None  # missing, or not a list this code wrote
        last = old["records"] if old else []
        fresh = bool(old) and old.get("platforms") == platforms and time.time() - old.get("fetched", 0) < MAX_AGE
        if fresh:
            self.results[source]["cached"] += 1
            return last
        if not has_key:
            self.results[source]["no_key"] += 1
            return last
        if source in self.down:
            self.results[source]["error"] += 1
            return last
        try:
            records = fetch(platforms)
        except Exception as e:  # anything at all: the scan goes on (module docstring)
            self.down.add(source)
            self.results[source]["error"] += 1
            log.warning("ratings source failed; keeping the last list and not asking it again this run",
                        source=source, system=system["id"], error=f"{type(e).__name__}: {e}")
            return last
        self.results[source]["fetched"] += 1
        log.info("ratings fetched", source=source, system=system["id"], games=len(records))
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_text(json.dumps({"fetched": int(time.time()), "platforms": platforms, "records": records}))
            tmp.replace(path)
        except OSError as e:  # this run still has the list; the next one asks again
            log.warning("ratings not cached", source=source, system=system["id"], error=str(e))
        return records

    def for_system(self, system: dict) -> tuple[dict[str, dict], dict[str, dict]]:
        """(IGDB index, RAWG index) for one system, each key(name) -> scores."""
        plan = (("igdb", self.igdb, bool(self.igdb_id and self.igdb_secret)),
                ("rawg", self.rawg, bool(self.rawg_key)))
        indexes = []
        for source, fetch, has_key in plan:
            try:
                indexes.append(build_index(self.cached(source, system, fetch, has_key)))
            except Exception as e:  # a list that can't be read: no scores, the scan goes on
                self.results[source]["error"] += 1
                log.warning("ratings list unusable", source=source, system=system["id"],
                            error=f"{type(e).__name__}: {e}")
                indexes.append({})
        return indexes[0], indexes[1]
