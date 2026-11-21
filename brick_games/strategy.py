"""Local strategy guides (brick-games#21): the StrategyWiki walkthrough of
every game ranked 80 and up, kept as text the page and an AI session can read.

- Which games: the page's "Ranked" score (ranks(): a score from 10 reviews
  up, weighted toward the catalog's average as if it had 10 more average
  reviews), 80 and up. A guide once kept stays linked if a game's rank drops.
- Which page: the game's StrategyWiki link from Wikidata (guides.py), else
  a page with the game's title that StrategyWiki files under Games and one
  of the system's platforms (`strategywiki` in systems.yaml), so SNES
  "Final Fantasy II" (Final Fantasy IV) doesn't get the NES game's guide.
- What's kept: the main page and every subpage its Table of Contents links
  (or, without one, every subpage), as plain text through MediaWiki's
  TextExtracts, one Markdown file with a heading per subpage and section:
  guide.md, and guide.json with the source, license and authors' link, the
  grade and an index of every heading (title, level, character offsets).
  StrategyWiki is CC BY-SA 4.0, so the source and license head the file.
- The grade is StrategyWiki's own completion stage: 3 to 5 "complete" (the
  game can be finished with it), 2 "partial", 0 and 1 "stub". A session
  reading a partial guide or a stub says so.
- A GameFAQs guide is never downloaded (its authors' copyright; GameFAQs
  forbids scripted copying), but one saved by hand into the game's library
  folder as guide-gamefaqs.txt is added to the guide, its headings found by
  the usual underlines (a line of === or --- below a title).
- Kept in the game's library folder (library.py) and copied to DATA/strategy
  on NVMe, which the web app serves (web.py: /api/guides). Fetched once,
  again after REFRESH days; a failed fetch keeps the old guide. A scan
  fetches for at most BUDGET seconds, a request every PAUSE seconds.
- Never fails the scan: games_strategy_guides{result}, games_guide_grades{grade}.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from operator import itemgetter
from pathlib import Path
from typing import Any, TypedDict

import brick_observe

from . import library

log = brick_observe.get_logger(__name__)
API = "https://strategywiki.org/w/api.php"
WIKI = "https://strategywiki.org/wiki/"
LICENSE = "CC BY-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
USER_AGENT = "brick-games (https://github.com/operations-ivy/brick-games)"
RANK_MIN = 80
MIN_REVIEWS = PRIOR_REVIEWS = 10  # as the page's ranked() (static/index.html)
KINDS = (("critics", "critic_reviews"), ("users", "user_reviews"))
GRADES = {0: "stub", 1: "stub", 2: "partial", 3: "complete", 4: "complete", 5: "complete"}
STAGE = re.compile(r"Category:Guides at completion stage (\d)")
GUIDE_MD, GUIDE_JSON, HAND_SAVED = "guide.md", "guide.json", "guide-gamefaqs.txt"
REFRESH = 30 * 86400
BUDGET = 1800
PAUSE = 0.5
MAX_PAGES = 150
TIMEOUT = 60
VERSION_LEN = 8
# What happened to each game ranked 80+ in one scan:
#   fetched   downloaded (new, or refreshed)
#   cached    kept, not due for a refresh
#   none      no StrategyWiki guide found
#   error     the fetch failed (the old guide, if any, is kept)
#   deferred  left for a later scan (this one's time is up)
RESULTS = ("fetched", "cached", "none", "error", "deferred")
HEADING = re.compile(r"^(={2,5})\s*(.*?)\s*\1\s*$", re.M)
UNDERLINE = re.compile(r"^(\S[^\n]{1,80})\n[=\-*]{5,}\s*$", re.M)


def ranks(games: list[dict]) -> dict[str, float]:
    """game id -> the page's Ranked score, for the games that have one."""
    mean = {}
    for kind, _ in KINDS:
        xs = [g[kind] for g in games if g.get(kind) is not None]
        mean[kind] = sum(xs) / len(xs) if xs else 0
    out = {}
    for g in games:
        if g.get("metascore") is not None:
            out[g["id"]] = float(g["metascore"])
            continue
        for kind, n in KINDS:
            if g.get(kind) is not None and (g.get(n) or 0) >= MIN_REVIEWS:
                out[g["id"]] = (g[n] * g[kind] + PRIOR_REVIEWS * mean[kind]) / (g[n] + PRIOR_REVIEWS)
                break
    return out


def api(**params: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"format": "json", "formatversion": "2", **params})
    request = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read())


def title_candidates(title: str) -> list[str]:
    """The game's title as StrategyWiki may have it: subtitles after a colon or a dash."""
    out = [title, title.replace(" - ", ": "), title.replace(": ", " - ")]
    return list(dict.fromkeys(out))


def find_page(game: dict, system: dict) -> str | None:
    """The game's StrategyWiki page title, or None."""
    if link := (game.get("guides") or {}).get("strategywiki"):
        return urllib.parse.unquote(link).replace("_", " ")
    platforms = system.get("strategywiki") or []
    if not platforms:
        return None
    wanted = ["Category:Games"] + [f"Category:{p}" for p in platforms]
    found = api(action="query", titles="|".join(title_candidates(game["title"])), redirects="1",
                prop="categories", cllimit="max", clcategories="|".join(wanted))
    for page in found["query"].get("pages", []):
        cats = {c["title"] for c in page.get("categories", [])}
        # A game (Games) on one of the system's platforms (any other category asked for).
        if not page.get("missing") and cats > {"Category:Games"}:
            return page["title"]
    return None


def stage(page: str) -> int | None:
    found = api(action="query", titles=page, redirects="1", prop="categories", cllimit="max")
    for p in found["query"].get("pages", []):
        for c in p.get("categories", []):
            if m := STAGE.fullmatch(c["title"]):
                return int(m.group(1))
    return None


def subpages(page: str) -> list[str]:
    """The walkthrough's pages in order: as the Table of Contents links them,
    else every subpage by name."""
    prefix = f"{page}/"
    # No Table of Contents: the API answers {"error": {"code": "missingtitle"}}.
    text = api(action="parse", page=f"{page}/Table of Contents", prop="wikitext").get("parse", {}).get("wikitext")
    if text:
        names = [n.strip().replace("_", " ") for n in re.findall(r"\[\[([^\]|#]+)", text)]
        pages = [n for n in dict.fromkeys(names) if n.startswith(prefix) and not n.endswith("/Table of Contents")]
        if pages:
            return pages
    listed = api(action="query", list="allpages", apprefix=prefix, aplimit="500")
    return [p["title"] for p in listed["query"]["allpages"] if not p["title"].endswith("/Table of Contents")]


def extract(title: str) -> str:
    found = api(action="query", prop="extracts", explaintext="1", exsectionformat="wiki", titles=title,
                redirects="1")
    pages = found["query"].get("pages", [])
    return pages[0].get("extract", "") if pages else ""


def to_markdown(text: str, shift: int = 1) -> str:
    """MediaWiki's == Section == headings as Markdown, one level below the page's own."""
    text = HEADING.sub(lambda m: "#" * (len(m.group(1)) + shift) + " " + m.group(2), text)
    text = "\n".join(line.rstrip() for line in text.splitlines())  # tables leave lines of tabs
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class Section(TypedDict):
    """One heading of a guide and the text it covers (md[start:end])."""

    title: str
    level: int
    start: int
    end: int


def index(md: str) -> list[Section]:
    """Every heading below the title: its text, level and the text it covers."""
    heads = [(m.start(), len(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(#{2,6}) (.+)$", md, re.M)]
    return [Section(title=title, level=level, start=start, end=heads[i + 1][0] if i + 1 < len(heads) else len(md))
            for i, (start, level, title) in enumerate(heads)]


def hand_saved(text: str) -> str:
    """A GameFAQs guide saved by hand, as Markdown: its underlined titles as headings."""
    return UNDERLINE.sub(lambda m: f"### {m.group(1).strip()}", text.replace("\r\n", "\n")).strip()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write(dest: Path, text: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.part")
    tmp.write_text(text)
    tmp.replace(dest)


def wiki_parts(page: str) -> list[str]:
    """A StrategyWiki guide's text, page by page, with its source and license first."""
    source = source_url(page)
    parts = [f"From StrategyWiki, {source}, by its contributors ({source}?action=history), "
             f"under {LICENSE} ({LICENSE_URL})."]
    for title in [page, *subpages(page)[:MAX_PAGES]]:
        text = extract(title)
        time.sleep(PAUSE)
        name = title.split("/", 1)[1] if "/" in title else "Overview"
        if text.strip():
            parts.append(f"## {name}\n\n{to_markdown(text)}")
    return parts


def source_url(page: str) -> str:
    return f"{WIKI}{urllib.parse.quote(page.replace(' ', '_'))}"


def build_guide(title: str, wiki: list[str], hand: str | None) -> str:
    """The guide's Markdown: the title, the wiki's parts, then a hand-saved guide."""
    parts = [f"# {title}", *wiki]
    if hand:
        parts.append(f"## GameFAQs guide (saved by hand)\n\n{hand_saved(hand)}")
    return "\n\n".join(parts) + "\n"


class Strategy:
    """One scan's guides."""

    def __init__(self, data: Path, lib: Path, budget: float = BUDGET) -> None:
        self.store = data / "strategy"
        self.lib = lib
        self.deadline = time.monotonic() + budget
        self.results = dict.fromkeys(RESULTS, 0)
        self.grades: Counter[str] = Counter()
        self._folders: dict[str, dict[str, str]] = {}

    def folder(self, game: dict) -> Path | None:
        if game["system"] not in self._folders:
            self._folders[game["system"]] = library.read_index(self.lib, game["system"])
        name = self._folders[game["system"]].get(game["id"])
        return self.lib / game["system"] / "media" / name if name else None

    def kept(self, game: dict) -> dict | None:
        try:
            return json.loads((self.store / f"{game['id']}.json").read_text())
        except (OSError, ValueError):
            return None

    def add(self, games: list[dict], systems: dict[str, dict]) -> None:
        """Each game's guide (game["strategy"]): kept ones linked, the 80+
        ones fetched or refreshed. Never raises."""
        rank = ranks(games)
        for game in games:
            try:
                self._one(game, systems[game["system"]], rank.get(game["id"]))
            except Exception as e:  # never fails the scan (module docstring)
                self.results["error"] += 1
                log.warning("guide failed", game=game["title"], system=game["system"],
                            error=f"{type(e).__name__}: {e}")
            if (meta := self.kept(game)) and meta["sections"]:  # one kept empty by 0.8.0 isn't shown
                game["strategy"] = {"grade": meta["grade"], "sections": len(meta["sections"]), "v": meta["v"]}
                self.grades[meta["grade"]] += 1
        self._wanted(games, rank)

    def _wanted(self, games: list[dict], rank: dict[str, float]) -> None:
        """DATA/strategy/wanted.json: the games ranked 80+ without a complete
        guide, best first, with where to find one on GameFAQs and the library
        folder a hand-saved one goes in (guide_filer.py, /api/guides/wanted)."""
        wanted = []
        for game in games:
            score = rank.get(game["id"])
            grade = (game.get("strategy") or {}).get("grade")
            if score is None or score < RANK_MIN or grade in ("complete", "hand-saved"):
                continue
            gamefaqs = (game.get("guides") or {}).get("gamefaqs")
            folder = self.folder(game)
            wanted.append({
                "id": game["id"], "title": game["title"], "system": game["system"], "rank": round(score, 1),
                "grade": grade,
                "gamefaqs": f"https://gamefaqs.gamespot.com/-/{gamefaqs}-/faqs" if gamefaqs
                else f"https://gamefaqs.gamespot.com/search?game={urllib.parse.quote(game['title'])}",
                "folder": str(folder.relative_to(self.lib)) if folder else None,
            })
        wanted.sort(key=lambda w: -w["rank"])
        write(self.store / "wanted.json", json.dumps(wanted, ensure_ascii=False, indent=1))

    def _one(self, game: dict, system: dict, rank: float | None) -> None:
        folder = self.folder(game)
        meta = self.kept(game)
        saved = folder / HAND_SAVED if folder else None
        hand = saved.read_text(errors="replace") if saved and saved.is_file() else None
        hand_changed = hand is not None and (not meta or meta.get("hand_saved") != digest(hand))
        if rank is None or rank < RANK_MIN:
            if hand_changed:  # a hand-saved guide counts whatever the game's rank
                self._keep(game, folder, None, None, hand)
            return
        stale = not meta or time.time() - meta.get("fetched", 0) > REFRESH
        if not meta and not hand_changed and time.time() - self.checked(game) <= REFRESH:
            self.results["none"] += 1  # looked for last time; asked again after REFRESH
            return
        if not (stale or hand_changed):
            self.results["cached"] += 1
            return
        if time.monotonic() > self.deadline:
            self.results["deferred"] += 1
            return
        page = find_page(game, system)
        kept = (page is not None or hand is not None) and self._keep(game, folder, page,
                                                                     stage(page) if page else None, hand)
        if not kept:
            write(self.store / f"{game['id']}.none", json.dumps({"checked": int(time.time()), "page": page}))
        self.results["fetched" if kept else "none"] += 1

    def checked(self, game: dict) -> float:
        """When a game was last found to have no guide (0: never)."""
        try:
            return json.loads((self.store / f"{game['id']}.none").read_text())["checked"]
        except (OSError, ValueError, KeyError):
            return 0

    def _keep(self, game: dict, folder: Path | None, page: str | None, level: int | None, hand: str | None) -> bool:
        """Build the guide and write it to DATA/strategy and the game's folder;
        False when there's nothing in it (a wiki page with no text)."""
        md = build_guide(game["title"], wiki_parts(page) if page else [], hand)
        sections = index(md)
        if not sections:  # Bahamut Lagoon, 2026-11-21: a page with nothing on it
            log.info("guide empty; not kept", game=game["title"], system=game["system"], page=page)
            return False
        grade = GRADES.get(level, "stub") if page else "hand-saved"
        meta = {"game": game["title"], "system": game["system"], "source": source_url(page) if page else None,
                "license": LICENSE if page else None, "stage": level, "grade": grade, "fetched": int(time.time()),
                "v": digest(md)[:VERSION_LEN], "hand_saved": digest(hand) if hand else None, "sections": sections}
        body = json.dumps(meta, ensure_ascii=False)
        targets = [(self.store, f"{game['id']}.md", f"{game['id']}.json")]
        if folder:
            targets.append((folder, GUIDE_MD, GUIDE_JSON))
        for base, md_name, json_name in targets:
            write(base / md_name, md)
            write(base / json_name, body)
        log.info("guide kept", game=game["title"], system=game["system"], grade=grade, sections=len(sections),
                 chars=len(md))
        return True


def search(md: str, sections: list[Section], query: str, limit: int = 5) -> list[dict]:
    """The sections that best match the query's words (how often they appear,
    a heading match counting more), with their text."""
    words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 1]
    if not words:
        return []
    scored = []
    for s in sections:
        text = md[s["start"]:s["end"]].lower()
        title = s["title"].lower()
        if score := sum(text.count(w) + 5 * title.count(w) for w in words):
            scored.append((score, s))
    scored.sort(key=itemgetter(0), reverse=True)
    return [{"title": s["title"], "level": s["level"], "score": score, "text": md[s["start"]:s["end"]]}
            for score, s in scored[:limit]]
