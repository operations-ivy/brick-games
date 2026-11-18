"""brick-games: `python -m brick_games web|scan`.

    ROMS_ROOT   the ROM tree, read-only (default /roms)
    DATA        the catalog and the art cache (default /data)
    PORT        web: the page and /metrics (default 8080)
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m brick_games")
    parser.add_argument("mode", choices=("web", "scan"))
    mode = parser.parse_args(argv).mode
    brick_observe.init(f"brick-games-{mode}")
    data = Path(os.environ.get("DATA", "/data"))
    if mode == "web":
        from . import web

        web.serve(data, int(os.environ.get("PORT", "8080")))
        return
    from . import catalog

    stats = catalog.Stats()
    with brick_observe.job("brick-games-scan") as run:  # pushed to the Pushgateway on exit
        catalog.scan(Path(os.environ.get("ROMS_ROOT", "/roms")), data, stats)
        publish(run, stats)


if __name__ == "__main__":
    main()
