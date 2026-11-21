"""Where the art and the facts come from, and how a file name finds them.

- Art: libretro's thumbnails, from their GitHub repos
  (github.com/libretro-thumbnails/<System_Name>: Named_Boxarts, Named_Titles,
  Named_Snaps). thumbnails.libretro.com served an empty index from 2026-11-20,
  and the repos are what it is built from. A repo's file list is one git-tree
  call (per folder when the whole tree is too big, as FBNeo's is), revalidated
  with its ETag: GitHub doesn't count a 304 against the 60 calls an hour it
  allows without a token. Shared art is a git symlink, which the raw URL
  serves as a line naming the real picture; `fetch_thumb` follows it.
- Facts: libretro-database's metadat (clrmamepro dats per field: genre,
  releaseyear, developer, publisher, maxusers), keyed by game name.
- Arcade zips are named by short name (sf2.zip): FinalBurn Neo's own dat
  turns that into the title the thumbnails and metadata use.

Matching is by `norm`: the name without extension, (tags) and [flags], leading
or trailing articles, "Disney's", "&"/"and", punctuation, with roman numerals
as digits. `loose` then tries a prefix match for long enough names, so
"Adventure Island II" finds "Adventure Island II - Aliens in Paradise".
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

THUMBS_API = "https://api.github.com/repos/libretro-thumbnails/"
THUMBS_RAW = "https://raw.githubusercontent.com/libretro-thumbnails/"
BRANCH = "master"
METADAT = "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/"
FBNEO_DAT = ("https://raw.githubusercontent.com/libretro/FBNeo/master/dats/"
             "FinalBurn%20Neo%20(ClrMame%20Pro%20XML%2C%20Arcade%20only).dat")
KIND_KEYS = {"Named_Boxarts": "box", "Named_Titles": "title", "Named_Snaps": "snap"}
KINDS = tuple(KIND_KEYS)
# metadat folder -> (the field inside its dats, the catalog key)
FIELDS = {"genre": ("genre", "genre"), "releaseyear": ("releaseyear", "year"),
          "developer": ("developer", "developer"), "publisher": ("publisher", "publisher"),
          "maxusers": ("users", "players")}
MAX_POINTER_HOPS = 3
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8"}
REGION_PREF = (r"\bUSA\b|\(U\)|\[U\]|\(UE\)|\(JU\)|\(JUE\)", r"\bWorld\b|\(W\)", r"\bEurope\b|\(E\)")
PNG = b"\x89PNG\r\n\x1a\n"
TIMEOUT = 60


def norm(name: str) -> str:
    n = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", name)
    n = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", n)
    n = n.replace("_", " ").replace("&", " ")
    n = re.sub(r",\s*(the|a|an)\b", "", n, flags=re.I)
    n = re.sub(r"^(the|a|an)\s+", "", n, flags=re.I)
    n = re.sub(r"\b(disney's|disneys|and|32x)\b", " ", n, flags=re.I)
    return "".join(ROMAN.get(w, w) for w in re.findall(r"[a-z0-9]+", n.lower()))


def region_rank(name: str) -> int:
    for i, pattern in enumerate(REGION_PREF):
        if re.search(pattern, name):
            return i
    return len(REGION_PREF)


def best(candidates: list[str]) -> str:
    return min(candidates, key=region_rank)


def loose(index: dict[str, list[str]], key: str) -> list[str] | None:
    """A prefix match for names long enough to be unambiguous: the closest in length."""
    if len(key) < 6:
        return None
    hits = [k for k in index if (k.startswith(key) or key.startswith(k)) and len(k) >= 6
            and min(len(k), len(key)) / max(len(k), len(key)) >= 0.45]
    return index[min(hits, key=lambda k: abs(len(k) - len(key)))] if hits else None


def lookup(index: dict[str, list[str]], name: str) -> list[str] | None:
    key = norm(name)
    return index.get(key) or loose(index, key)


class RateLimited(OSError):
    """GitHub said 429 (or 403 for its rate limit): stop asking until the next scan."""


def headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """GITHUB_TOKEN, when set, lifts GitHub's limits; nothing needs it."""
    h = {"User-Agent": "brick-games"} | (extra or {})
    if token := os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {token}"
    return h


def get(url: str) -> bytes:
    hdrs = headers() if url.startswith((THUMBS_API, THUMBS_RAW)) else {"User-Agent": "brick-games"}
    request = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 429 or (e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0"):
            raise RateLimited(f"rate limited: {url}") from e
        raise


def repo(system: str) -> str:
    """libretro's system name as its thumbnails repo is named."""
    return system.replace(" ", "_")


def thumb_url(system: str, path: str) -> str:
    """The raw URL of one file in a system's repo (path: Named_Boxarts/<name>.png)."""
    return f"{THUMBS_RAW}{repo(system)}/{BRANCH}/{urllib.parse.quote(path)}"


def github(url: str, cache: Path | None) -> dict:
    """A GitHub API answer, revalidated against the copy kept in cache (its
    ETag): an unchanged one costs no rate limit and no download."""
    kept = cache / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}.json" if cache else None
    old = None
    if kept and kept.is_file():
        try:
            old = json.loads(kept.read_text())
        except ValueError:
            old = None
    extra = {"Accept": "application/vnd.github+json"}
    if old and old.get("etag"):
        extra["If-None-Match"] = old["etag"]
    request = urllib.request.Request(url, headers=headers(extra))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body, etag = json.loads(response.read()), response.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304 and old:
            return old["body"]
        if e.code == 429 or (e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0"):
            raise RateLimited(f"rate limited: {url}") from e
        raise
    if kept:
        kept.parent.mkdir(parents=True, exist_ok=True)
        tmp = kept.with_suffix(".part")
        tmp.write_text(json.dumps({"url": url, "etag": etag, "body": body}))
        tmp.replace(kept)
    return body


def tree_files(system: str, cache: Path | None) -> list[str]:
    """Every file's path in a system's repo (Named_Boxarts/<name>.png ...): the
    whole tree in one call, or folder by folder when that's too big for GitHub
    (it answers 500 or a truncated tree)."""
    base = f"{THUMBS_API}{repo(system)}/git/trees/"
    try:
        tree = github(f"{base}{BRANCH}?recursive=1", cache)
        if not tree.get("truncated"):
            return [e["path"] for e in tree["tree"] if e["type"] == "blob"]
    except urllib.error.HTTPError as e:  # RateLimited isn't one: it goes up
        if e.code < 500:
            raise
    top = github(f"{base}{BRANCH}", cache)
    paths = []
    for entry in top["tree"]:
        if entry["type"] == "tree" and entry["path"] in KIND_KEYS:
            sub = github(f"{base}{entry['sha']}", cache)
            paths += [f"{entry['path']}/{e['path']}" for e in sub["tree"] if e["type"] == "blob"]
    return paths


def thumb_indexes(system: str, cache: Path | None = None) -> dict[str, dict[str, list[str]]]:
    """kind -> norm(name) -> thumbnail file names, for the three kinds the page shows."""
    indexes: dict[str, dict[str, list[str]]] = {kind: defaultdict(list) for kind in KINDS}
    for path in tree_files(system, cache):
        kind, _, name = path.partition("/")
        if kind in indexes and name.endswith(".png") and "/" not in name:
            indexes[kind][norm(name)].append(name)
    return indexes


def fetch_thumb(system: str, kind: str, name: str) -> bytes | None:
    """The PNG, following libretro's shared-art links: a git symlink, which the
    raw URL serves as one line, the real picture's name (relative to its folder)."""
    path = f"{kind}/{name}"
    for _ in range(MAX_POINTER_HOPS):
        data = get(thumb_url(system, path))
        if data.startswith(PNG):
            return data
        target = data.decode("utf-8", "replace").strip()
        if not target.endswith(".png") or "\n" in target:
            return None
        path = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        if path.startswith("../"):
            return None
    return None


def parse_metadat(text: str, field: str) -> dict[str, str]:
    """name -> value from a libretro metadat dat. A game is named by its
    `comment`, or else by its first rom's file name; values may be quoted
    (`genre "Sports"`) or bare (`users 2`)."""
    out = {}
    for block in re.findall(r"\ngame \((.*?)\n\)", text, re.S):
        name = re.search(r'^\s*comment "([^"]*)"', block, re.M) or re.search(r'rom \( name "([^"]*)"', block)
        value = re.search(rf'^\s*{field} (?:"([^"]*)"|(\S+))', block, re.M)
        if name and value:
            out[re.sub(r"\s*\(Track \d+\)", "", name.group(1))] = value.group(1) or value.group(2)
    return out


def metadata(system: str) -> dict[str, dict[str, str]]:
    """norm(name) -> {genre, year, developer, publisher, players} for a system."""
    facts: dict[str, dict[str, str]] = defaultdict(dict)
    for folder, (field, key) in FIELDS.items():
        try:
            text = get(f"{METADAT}{folder}/{urllib.parse.quote(system)}.dat").decode("utf-8", "replace")
        except OSError:  # not every system has every field (a 404)
            continue
        for name, value in parse_metadat(text, field).items():
            facts[norm(name)].setdefault(key, value)
    return facts


def fbneo_titles(xml_text: str) -> dict[str, str]:
    """short name -> title, from FinalBurn Neo's ClrMame Pro XML dat."""
    return {m.group(1): html.unescape(m.group(2)) for m in
            re.finditer(r'<game name="([^"]+)"[^>]*>\s*<description>([^<]*)</description>', xml_text)}
