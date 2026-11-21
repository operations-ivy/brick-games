"""brick-games: `python -m brick_games web|scan`.

    ROMS_ROOT   scan: the ROM tree, read-only (default /roms)
    DATA        the catalog, the art cache and the manual cache, on NVMe (default /data)
    LIBRARY     the media library on the drive (library.py; default /library):
                the scan writes it, the web app only reads it
    MANUAL_CACHE_MB
                web: the manual cache's size (cache.py; default 2048)
    PORT        web: the page and /metrics (default 8080)
    GITHUB_TOKEN
                scan, optional: lifts GitHub's rate limits for the art
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import brick_observe


def publish(run, stats) -> None:
    """The scan's counts as gauges on a brick_observe job run."""
    for system, counts in stats.per_system.items():
        for what, value in counts.items():
            run.gauge(f"games_catalog_{what}", f"The catalog's {what.replace('_', ' ')}, by system",
                      value, {"system": system})
    for result, value in stats.fetches.items():
        run.gauge("games_art_fetches", "Art downloads in the run, by result", value, {"result": result})
    # The board (brick-status) shows result="error" in amber; nothing alerts on it.
    for source, results in stats.ratings.items():
        for result, value in results.items():
            run.gauge("games_ratings_fetches", "Systems' ratings lists in the run, by source and result",
                      value, {"source": source, "result": result})
    for result, value in stats.manuals.items():
        run.gauge("games_manual_fetches", "Games' manuals in the run, by result", value, {"result": result})
    for result, value in stats.guides.items():
        run.gauge("games_guide_fetches", "Systems' guide lists in the run, by result", value, {"result": result})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m brick_games")
    parser.add_argument("mode", choices=("web", "scan"))
    mode = parser.parse_args(argv).mode
    brick_observe.init(f"brick-games-{mode}")
    data = Path(os.environ.get("DATA", "/data"))
    lib = Path(os.environ.get("LIBRARY", "/library"))
    if mode == "web":
        from . import web

        web.serve(data, int(os.environ.get("PORT", "8080")), lib=lib,
                  manual_cache=int(os.environ.get("MANUAL_CACHE_MB", "2048")) * 2**20)
        return
    from . import catalog

    stats = catalog.Stats()
    with brick_observe.job("brick-games-scan") as run:  # pushed to the Pushgateway on exit
        catalog.scan(Path(os.environ.get("ROMS_ROOT", "/roms")), data, lib, stats)
        publish(run, stats)


if __name__ == "__main__":
    main()
