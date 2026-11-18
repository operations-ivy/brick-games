"""The image copies an explicit file list (Dockerfile): every module the app
imports, and the files it reads, must be on it. 0.4.0 shipped without
ratings.py and the scan failed on import (2026-11-18)."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "brick_games"
# Read at run time, not imported.
DATA_FILES = ("brick_games/systems.yaml", "brick_games/static/index.html")


def copied() -> set[str]:
    """The paths the Dockerfile's COPY lines name (continuation lines joined)."""
    text = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    return {word for line in text.splitlines() if line.startswith("COPY ")
            for word in line.split()[1:-1] if not word.startswith("--")}


def package_imports(module: str) -> set[str]:
    """The brick_games modules a module imports (relative imports, as the
    package writes them), wherever they are in its code."""
    found = set()
    for node in ast.walk(ast.parse((PACKAGE / f"{module}.py").read_text())):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:  # from .catalog import x
                found.add(node.module.split(".")[0])
            else:  # from . import catalog, web
                found.update(a.name for a in node.names)
    return found


def reachable(start: str = "__main__") -> set[str]:
    seen, todo = set(), [start]
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        todo += package_imports(module)
    return seen


def test_every_module_the_app_imports_is_in_the_image():
    needed = {f"brick_games/{m}.py" for m in reachable() | {"__init__"}}
    missing = sorted(needed - copied())
    assert not missing, f"add to the Dockerfile's COPY list: {missing}"


def test_the_files_the_app_reads_are_in_the_image():
    missing = sorted(set(DATA_FILES) - copied())
    assert not missing, f"add to the Dockerfile's COPY list: {missing}"


def test_the_check_sees_the_modules():
    # catalog -> ratings -> sources, and web, from __main__'s function-level imports
    assert {"catalog", "ratings", "sources", "web"} <= reachable()
