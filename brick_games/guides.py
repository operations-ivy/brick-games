"""Strategy guides: each game's GameFAQs page and StrategyWiki walkthrough,
found through Wikidata (the user, 2026-11-19).

GameFAQs answers scripted requests with a Cloudflare 403, so its guides are
never read. Wikidata, which is open to queries, holds each game's GameFAQs
game id (P4769) and StrategyWiki page (P9075). The page links GameFAQs'
page for the game (its Guides tab lists them all) and the StrategyWiki
walkthrough when there is one. A game Wikidata doesn't match gets a
GameFAQs search link from the page instead.

- Each system's `wikidata` in systems.yaml names its Wikidata platform items.
  A game's GameFAQs id can be tagged with a platform (one id per release:
  Symphony of the Night has PlayStation's and Saturn's); an untagged id
  counts when the game itself is on the platform. Two queries per system, one
  for each, because together they time out on the big platforms.
- Names are the item's English label, its aliases and its English Wikipedia
  title, matched like ratings (ratings.match): exact, or one longer name that
  starts with it and doesn't go on with a number.
- Each system's list is kept in DATA/guides/<system>.json and asked for
  again after a week. Like ratings, guides never fail the scan: anything that
  goes wrong is a warning, the last list is kept, and it's counted in
  `results`, pushed as games_guide_fetches.
- The query service is shared, rate-limited and uneven: the same query took
  1 s, 39 s and over 120 s (2026-11-19), and quick queries in a row got 429.
  So queries are PAUSE apart, one that times out or gets 429 is tried again
  (after Retry-After), and Wikidata counts as down for the run only after
  GIVE_UP systems in a row fail, not after one heavy query.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NotRequired, TypedDict

import brick_observe

from . import ratings

log = brick_observe.get_logger(__name__)
SPARQL = "https://query.wikidata.org/sparql"
# Wikimedia asks automated clients to say what they are and where they live.
USER_AGENT = "brick-games (https://github.com/operations-ivy/brick-games)"
MAX_AGE = 7 * 24 * 3600
TIMEOUT = 90  # the service stops a query at 60 s itself
PAUSE = 5.0  # between queries: the query service is shared
RETRIES = 2  # more tries for a query that timed out or got 429
MAX_WAIT = 120  # the longest Retry-After waited for
GIVE_UP = 3  # systems failing in a row: Wikidata is down, not asked again this run
# What happened to each system's list in one scan (as in ratings.RESULTS):
#   fetched  asked Wikidata and got it
#   cached   the kept list was fresh enough
#   error    Wikidata failed (the kept list, if any, was used)
RESULTS = ("fetched", "cached", "error")

# Names: label, aliases, and the English Wikipedia title ("Shenmue (video
# game)": the parenthesis goes in sources.norm). StrategyWiki is optional.
_NAMES = """
  OPTIONAL { ?item wdt:P9075 ?sw }
  { ?item rdfs:label ?name } UNION { ?item skos:altLabel ?name }
  UNION { ?article schema:about ?item; schema:isPartOf <https://en.wikipedia.org/>; schema:name ?name }
  FILTER(LANG(?name) = "en")"""
TAGGED = """SELECT ?item ?gf ?sw ?name WHERE {
  VALUES ?plat { %s }
  ?item p:P4769 ?st. ?st ps:P4769 ?gf; pq:P400 ?plat.%s
}"""


class Record(TypedDict):
    """One game's links, as kept in DATA/guides."""

    names: list[str]
    gamefaqs: str
    strategywiki: NotRequired[str]


UNTAGGED = """SELECT ?item ?gf ?sw ?name WHERE {
  VALUES ?plat { %s }
  ?item wdt:P400 ?plat; p:P4769 ?st. ?st ps:P4769 ?gf.
  FILTER NOT EXISTS { ?st pq:P400 ?any }%s
}"""


def queries(platforms: list[str]) -> tuple[str, str]:
    """The tagged and untagged queries for a system's platform items."""
    plats = " ".join(f"wd:{p}" for p in platforms)
    return TAGGED % (plats, _NAMES), UNTAGGED % (plats, _NAMES)


def records(tagged: list[dict], untagged: list[dict]) -> list[Record]:
    """SPARQL rows -> one record per game: {names, gamefaqs, strategywiki?}.
    A game with several ids takes a platform-tagged one, the lowest first."""
    games: dict[str, dict[str, Any]] = {}
    for rank, rows in enumerate((tagged, untagged)):
        for row in rows:
            gf = row["gf"]["value"]
            if not gf.isdigit():  # not an id GameFAQs' links take
                continue
            item = row["item"]["value"].rsplit("/", 1)[-1]
            g = games.setdefault(item, {"names": set(), "ids": set()})
            g["names"].add(row["name"]["value"])
            g["ids"].add((rank, int(gf)))
            if "sw" in row:
                g["strategywiki"] = row["sw"]["value"]
    out = []
    for item in sorted(games):
        g = games[item]
        record: Record = {"names": sorted(g["names"]), "gamefaqs": str(min(g["ids"])[1])}
        if "strategywiki" in g:
            record["strategywiki"] = g["strategywiki"]
        out.append(record)
    return out


def build_index(recs: list[Record]) -> dict[str, dict]:
    """key(name) -> {gamefaqs, strategywiki?}. Two games with one name on a
    platform: the first (lowest item id) keeps it."""
    index: dict[str, dict] = {}
    for record in recs:
        links = {k: v for k, v in record.items() if k != "names"}
        for name in record["names"]:
            if k := ratings.key(name):
                index.setdefault(k, links)
    return index


def ask(query: str) -> list[dict]:
    """One query's result rows (SPARQL JSON bindings)."""
    url = f"{SPARQL}?{urllib.parse.urlencode({'query': query})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return json.loads(response.read())["results"]["bindings"]


def retry_after(e: urllib.error.HTTPError) -> float:
    """How long a 429 asks us to wait (seconds; a date or nothing: PAUSE)."""
    value = e.headers.get("Retry-After", "") if e.headers else ""
    return min(float(value), MAX_WAIT) if value.isdigit() else PAUSE


def timed_out(e: Exception) -> bool:
    """A read timeout (TimeoutError) or a connect one (URLError wrapping it)."""
    return isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)


def sparql(query: str) -> list[dict]:
    """ask(), tried again after a timeout or a 429 (module docstring)."""
    for _ in range(RETRIES):
        try:
            return ask(query)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(retry_after(e))
        except (TimeoutError, urllib.error.URLError) as e:
            if not timed_out(e):
                raise
            time.sleep(PAUSE)
    return ask(query)  # the last try: its failure is the caller's


class Guides:
    """Each system's guide links, from the cache in DATA/guides or Wikidata."""

    def __init__(self, data: Path) -> None:
        self.cache_dir = data / "guides"
        self.failed_in_a_row = 0  # GIVE_UP of these: Wikidata isn't asked again this run
        self.results = dict.fromkeys(RESULTS, 0)

    def fetch(self, platforms: list[str]) -> list[Record]:
        tagged, untagged = queries(platforms)
        time.sleep(PAUSE)
        rows = sparql(tagged)
        time.sleep(PAUSE)
        return records(rows, sparql(untagged))

    def cached(self, system: dict) -> list[Record]:
        """The system's records: the cache while it's fresh and for the same
        platforms, else fetched (and kept); the old cache if that fails.
        Never raises."""
        platforms = system.get("wikidata")
        if not platforms:
            return []
        path = self.cache_dir / f"{system['id']}.json"
        try:
            old = json.loads(path.read_text())
        except (OSError, ValueError):
            old = None
        if not isinstance(old, dict) or not isinstance(old.get("records"), list):
            old = None  # missing, or not a list this code wrote
        last = old["records"] if old else []
        if old and old.get("platforms") == platforms and time.time() - old.get("fetched", 0) < MAX_AGE:
            self.results["cached"] += 1
            return last
        if self.failed_in_a_row >= GIVE_UP:
            self.results["error"] += 1
            return last
        try:
            recs = self.fetch(platforms)
        except Exception as e:  # anything at all: the scan goes on (module docstring)
            self.failed_in_a_row += 1
            self.results["error"] += 1
            log.warning("guides: Wikidata failed; keeping the last list", system=system["id"],
                        error=f"{type(e).__name__}: {e}", given_up=self.failed_in_a_row >= GIVE_UP)
            return last
        self.failed_in_a_row = 0
        self.results["fetched"] += 1
        log.info("guides fetched", system=system["id"], games=len(recs))
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_text(json.dumps({"fetched": int(time.time()), "platforms": platforms, "records": recs}))
            tmp.replace(path)
        except OSError as e:  # this run still has the list; the next one asks again
            log.warning("guides not cached", system=system["id"], error=str(e))
        return recs

    def for_system(self, system: dict) -> dict[str, dict]:
        """key(name) -> links for one system ({} without `wikidata`). Never raises."""
        try:
            return build_index(self.cached(system))
        except Exception as e:  # a list that can't be read: no links, the scan goes on
            self.results["error"] += 1
            log.warning("guides list unusable", system=system["id"], error=f"{type(e).__name__}: {e}")
            return {}
