"""Where the art and the facts come from, and how a file name finds them.

- Art: libretro's thumbnails (thumbnails.libretro.com/<system>/Named_Boxarts,
  Named_Titles, Named_Snaps). Shared art is stored as a small text file naming
  the real image; `fetch` follows it.
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

import html
import re
import urllib.parse
import urllib.request
from collections import defaultdict

THUMBS = "https://thumbnails.libretro.com/"
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


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "brick-games"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def thumb_url(system: str, kind: str, name: str = "") -> str:
    return f"{THUMBS}{urllib.parse.quote(system)}/{kind}/{urllib.parse.quote(name)}"


def thumb_index(system: str, kind: str) -> dict[str, list[str]]:
    """norm(name) -> thumbnail file names, from the server's directory listing."""
    page = get(thumb_url(system, kind)).decode("utf-8", "replace")
    index: dict[str, list[str]] = defaultdict(list)
    for href in re.findall(r'href="([^"/]+\.png)"', page):
        name = html.unescape(urllib.parse.unquote(href))
        index[norm(name)].append(name)
    return index


def fetch_thumb(system: str, kind: str, name: str) -> bytes | None:
    """The PNG, following libretro's "pointer" files to the shared image: a
    pointer is a small text file holding one line, the real picture's name."""
    for _ in range(MAX_POINTER_HOPS):
        data = get(thumb_url(system, kind, name))
        if data.startswith(PNG):
            return data
        target = data.decode("utf-8", "replace").strip()
        if not target.endswith(".png") or "\n" in target:
            return None
        name = target
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
