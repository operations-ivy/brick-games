"""The page's script, checked without a browser. A top-level name declared
twice (`let shown` and later `const shown`) is a SyntaxError that stops the
whole page at "loading…" (2026-11-18)."""

import re
from collections import Counter
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "brick_games" / "static" / "index.html"
# re.match anchors at column 0: an unindented declaration is a top-level one.
FUNCTION = re.compile(r"(?:async\s+)?function\s+(\w+)")
VARIABLES = re.compile(r"(?:const|let|var)\s+(.*)")


def declared_names(declaration: str) -> list[str]:
    """The names of one `const`/`let`/`var` line's declarators: each
    `name =` at bracket depth 0 (`let a = 1, b = {x, y}` is a and b)."""
    names, depth, start = [], 0, 0
    for i, ch in enumerate(declaration + ","):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            if m := re.match(r"\s*(\w+)\s*=", declaration[start:i]):
                names.append(m.group(1))
            start = i + 1
    return names


def top_level_names(script: str) -> list[str]:
    names = []
    for line in script.splitlines():
        if m := FUNCTION.match(line):
            names.append(m.group(1))
        elif m := VARIABLES.match(line):
            names += declared_names(m.group(1))
    return names


def page_script() -> str:
    match = re.search(r"<script>(.*?)</script>", PAGE.read_text(), re.S)
    assert match, "no <script> block in index.html"
    return match.group(1)


def test_no_top_level_name_is_declared_twice():
    assert [n for n, c in Counter(top_level_names(page_script())).items() if c > 1] == []


def test_the_parse_sees_the_pages_names():
    assert {"load", "counted", "shown", "MIN_REVIEWS", "PRIOR_REVIEWS"} <= set(top_level_names(page_script()))


@pytest.mark.parametrize("script,names", [
    ("let cat = null, shown = 0;\nconst shown = (g) => g;\nfunction f() {}\n", ["cat", "shown", "shown", "f"]),
    ("const a = {x: 1, y: 2}, b = [1, 2];\n", ["a", "b"]),
    ("async function load() {\n  const inner = 1;\n}\nvar v = f(1, 2);\n", ["load", "v"]),
])
def test_top_level_names(script, names):
    assert top_level_names(script) == names
