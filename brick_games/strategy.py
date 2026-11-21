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
from pathlib import Path
from typing import Any

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
        if not page.get("missing") and "Category:Games" in cats and len(cats) > 1:
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
    try:
        text = api(action="parse", page=f"{page}/Table of Contents", prop="wikitext")["parse"]["wikitext"]
    except KeyError:  # no Table of Contents: {"error": {"code": "missingtitle"}}
        text = None
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


def index(md: str) -> list[dict]:
    """Every heading below the title: its text, level and the text it covers."""
    heads = [(m.start(), len(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(#{2,6}) (.+)$", md, re.M)]
    return [{"title": title, "level": level, "start": start,
             "end": heads[i + 1][0] if i + 1 < len(heads) else len(md)}
            for i, (start, level, title) in enumerate(heads)]


def hand_saved(text: str) -> str:
    """A GameFAQs guide saved by hand, as Markdown: its underlined titles as headings."""
    return UNDERLINE.sub(lambda m: f"### {m.group(1).strip()}", text.replace("\r\n", "\n")).strip()


class Strategy:
    """One scan's guides."""

    def __init__(self, data: Path, lib: Path, budget: float = BUDGET) -> None:
        self.store = data / "strategy"
        self.lib = lib
        self.deadline = time.monotonic() + budget
        self.results = dict.fromkeys(RESULTS, 0)
        self.grades: dict[str, int] = {}
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
            if meta := self.kept(game):
                game["strategy"] = {"grade": meta["grade"], "sections": len(meta["sections"]), "v": meta["v"]}
                self.grades[meta["grade"]] = self.grades.get(meta["grade"], 0) + 1

    def _one(self, game: dict, system: dict, rank: float | None) -> None:
        folder = self.folder(game)
        meta = self.kept(game)
        saved = folder / HAND_SAVED if folder else None
        hand = saved.read_text(errors="replace") if saved and saved.is_file() else None
        if rank is None or rank < RANK_MIN:
            if hand and (not meta or meta.get("hand_saved") != digest(hand)):
                self._write(game, folder, None, None, hand)  # a hand-saved guide counts whatever the rank
            return
        due = not meta or time.time() - meta.get("fetched", 0) > REFRESH or \
            (hand is not None and meta.get("hand_saved") != digest(hand))
        if not due:
            self.results["cached"] += 1
            return
        if time.monotonic() > self.deadline:
            self.results["deferred"] += 1
            return
        page = find_page(game, system)
        if page is None and hand is None:
            self.results["none"] += 1
            return
        self._write(game, folder, page, stage(page) if page else None, hand)
        self.results["fetched"] += 1

    def _write(self, game: dict, folder: Path | None, page: str | None, level: int | None, hand: str | None) -> None:
        parts = [f"# {game['title']}"]
        source = None
        if page:
            source = f"{WIKI}{urllib.parse.quote(page.replace(' ', '_'))}"
            parts.append(f"From StrategyWiki, {source}, by its contributors "
                         f"({source}?action=history), under {LICENSE} ({LICENSE_URL}).")
            for title in [page, *subpages(page)[:MAX_PAGES]]:
                text = extract(title)
                time.sleep(PAUSE)
                name = title.split("/", 1)[1] if "/" in title else "Overview"
                if text.strip():
                    parts.append(f"## {name}\n\n{to_markdown(text)}")
        if hand:
            parts.append(f"## GameFAQs guide (saved by hand)\n\n{hand_saved(hand)}")
        md = "\n\n".join(parts) + "\n"
        grade = GRADES.get(level, "stub") if page else "hand-saved"
        meta = {"game": game["title"], "system": game["system"], "source": source, "license": LICENSE if page else None,
                "stage": level, "grade": grade, "fetched": int(time.time()), "v": digest(md)[:VERSION_LEN],
                "hand_saved": digest(hand) if hand else None, "sections": index(md)}
        body = json.dumps(meta, ensure_ascii=False)
        for base, stem in ((self.store, game["id"]), (folder, None)):
            if base is None:
                continue
            write(base / (f"{stem}.md" if stem else GUIDE_MD), md)
            write(base / (f"{stem}.json" if stem else GUIDE_JSON), body)
        log.info("guide kept", game=game["title"], system=game["system"], grade=grade,
                 sections=len(meta["sections"]), chars=len(md))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write(dest: Path, text: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.part")
    tmp.write_text(text)
    tmp.replace(dest)


def search(md: str, sections: list[dict], query: str, limit: int = 5) -> list[dict]:
    """The sections that best match the query's words (how often they appear,
    a heading match counting more), with their text."""
    words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 1]
    if not words:
        return []
    scored = []
    for s in sections:
        text = md[s["start"]:s["end"]].lower()
        title = s["title"].lower()
        score = sum(text.count(w) + 5 * title.count(w) for w in words)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda t: -t[0])
    return [{"title": s["title"], "level": s["level"], "score": score, "text": md[s["start"]:s["end"]]}
            for score, s in scored[:limit]]
