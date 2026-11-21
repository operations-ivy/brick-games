"""The saves service (`python -m brick_games saves`): the referee and the API
the agents and the page use, behind games.brick.nozdormu.cloud/api/saves.

    GET  /api/saves                     every game with saves: its current version,
                                        each device's, and whether there's a conflict
    GET  /api/saves/config?device=      that device's save folders (saves.yaml)
    GET  /api/saves/agent.py            the agent (saves_agent.py), for brick9000's timer
    GET  /api/saves/<game>              a game's state: versions, devices, conflict, log
    GET  /api/saves/<game>/<sha>        one version's bytes
    POST /api/saves/<game>/upload?device=&name=&mtime=&parent=
                                        a device's save (the body): stored by its hash,
                                        then an "uploaded" event; answers the new state
    POST /api/saves/<game>/keep         {"sha": ...}: the user's pick in a conflict
    POST /api/saves/seen?device=        an agent's run finished (its last-sync time)
    GET  /health /metrics

LAN only, like the page. Games come from the catalog the scan writes (DATA,
read-only); saves live in the library (LIBRARY, read-write; saves.py).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import brick_observe
import yaml
from brick_observe import metrics as observe_metrics
from prometheus_client import Counter, Gauge

from . import saves

log = brick_observe.get_logger(__name__)
CONFIG_FILE = Path(__file__).with_name("saves.yaml")
AGENT_FILE = Path(__file__).with_name("saves_agent.py")
GAME = re.compile(r"^/api/saves/(?P<game>[0-9a-f]{16})(?:/(?P<what>upload|keep|[0-9a-f]{64}))?$")
MAX_SAVE = 8 * 2**20  # bytes: the biggest battery saves and cards are well under this
EVENTS = Counter("games_saves_events_total", "Save events, by type and the referee's outcome", ["type", "outcome"])
CONFLICTS = Gauge("games_saves_conflicts", "Games whose saves wait for the user's pick")
GAMES = Gauge("games_saves_games", "Games with at least one synced save")
LAST_SYNC = Gauge("games_saves_last_sync_timestamp_seconds", "When each device's agent last finished", ["device"])


class Catalog:
    """The scan's catalog.json, by game id, re-read when it changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._key: tuple[int, int] | None = None
        self._games: dict[str, dict] = {}
        self._lock = threading.Lock()

    def games(self) -> dict[str, dict]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return self._games
        with self._lock:
            if (st.st_ino, st.st_mtime_ns) != self._key:
                self._games = {g["id"]: g for g in json.loads(self.path.read_text())["games"]}
                self._key = (st.st_ino, st.st_mtime_ns)
            return self._games


class Service:
    def __init__(self, data: Path, lib: Path, events: saves.EventLog) -> None:
        self.catalog = Catalog(data / "catalog.json")
        self.store = saves.Store(lib)
        self.events = events
        self.referee = saves.Referee(self.store, self.catalog.games)
        self.config = yaml.safe_load(CONFIG_FILE.read_text())["devices"]
        self.stop = threading.Event()
        self.last_sync: dict[str, float] = {}

    def start(self) -> None:
        def handle(event: dict) -> None:
            EVENTS.labels(event["type"], self.referee.handle(event)).inc()
            self.gauges()

        threading.Thread(target=self.events.run, args=(handle, self.stop), daemon=True, name="referee").start()
        self.gauges()

    def gauges(self) -> None:
        states = self.store.states()
        GAMES.set(len(states))
        CONFLICTS.set(sum(1 for s in states if s["conflict"]))

    def emit(self, game: dict, event: dict) -> dict:
        event |= {"id": uuid.uuid4().hex, "game": game["id"], "at": time.time()}
        self.events.produce(game["id"], event)
        self.referee.wait(event["id"])
        return self.store.load(game)


class Handler(BaseHTTPRequestHandler):
    service: Service
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, kind: str = "application/json",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: object) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode())

    def _game(self, game_id: str) -> dict | None:
        game = self.service.catalog.games().get(game_id)
        if game is None:
            self._json(404, {"error": "no such game in the catalog"})
        return game

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/health", "/ready"):
            self._send(200, b"ok", "text/plain")
        elif url.path == "/metrics":
            body, kind = observe_metrics.exposition()
            self._send(200, body, kind)
        elif url.path == "/api/saves":
            summary = [{"game": s["game"], "system": s["system"], "title": s["title"], "head": s["head"],
                        "conflict": bool(s["conflict"]),
                        "devices": {d: v["sha"] == s["head"] for d, v in s["devices"].items()}}
                       for s in self.service.store.states()]
            self._json(200, {"games": summary, "last_sync": self.service.last_sync,
                             "devices": {k: v["name"] for k, v in self.service.config.items()}})
        elif url.path == "/api/saves/config":
            if (cfg := self.service.config.get(q.get("device", ""))) is None:
                self._json(404, {"error": "unknown device", "devices": sorted(self.service.config)})
            else:
                self._json(200, cfg)
        elif url.path == "/api/saves/agent.py":
            self._send(200, AGENT_FILE.read_bytes(), "text/x-python; charset=utf-8")
        elif m := GAME.match(url.path):
            if (game := self._game(m["game"])) is None:
                return
            what = m["what"]
            if what is None:
                self._json(200, self.service.store.load(game))
            elif len(what) == 64:
                path = self.service.store.blob_path(game, what)
                if not path.is_file():
                    self._json(404, {"error": "no such version"})
                    return
                state = self.service.store.load(game)
                names = state["versions"].get(what, {}).get("names", {})
                name = next(iter(names.values()), path.name)
                self._send(200, path.read_bytes(), "application/octet-stream",
                           {"Content-Disposition": f'attachment; filename="{name}"'})
            else:
                self._json(405, {"error": "POST"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_SAVE:
            self._json(413, {"error": f"over {MAX_SAVE} bytes"})
            return
        body = self.rfile.read(length)
        if url.path == "/api/saves/seen":
            device = q.get("device", "")
            if device not in self.service.config:
                self._json(404, {"error": "unknown device"})
                return
            self.service.last_sync[device] = time.time()
            LAST_SYNC.labels(device).set(self.service.last_sync[device])
            self._json(200, {"ok": True})
            return
        m = GAME.match(url.path)
        if not m or m["what"] not in ("upload", "keep"):
            self._json(404, {"error": "not found"})
            return
        if (game := self._game(m["game"])) is None:
            return
        if m["what"] == "keep":
            try:
                sha = json.loads(body)["sha"]
            except (ValueError, KeyError, TypeError):
                self._json(400, {"error": 'send {"sha": "<the version to keep>"}'})
                return
            if sha not in self.service.store.load(game)["versions"]:
                self._json(404, {"error": "no such version"})
                return
            self._json(200, self.service.emit(game, {"type": "kept", "sha": sha}))
            return
        device = q.get("device", "")
        if device not in self.service.config:
            self._json(404, {"error": "unknown device"})
            return
        if saves.kind_of(game["system"]) == "ps1" and len(body) != saves.PS1_CARD:
            self._json(400, {"error": f"a PlayStation card is {saves.PS1_CARD} bytes, this is {len(body)}"})
            return
        if not body:
            self._json(400, {"error": "an empty save"})
            return
        sha = self.service.store.put_blob(game, body)
        event = {"type": "uploaded", "sha": sha, "device": device, "name": q.get("name", ""),
                 "size": len(body), "mtime": float(q["mtime"]) if q.get("mtime") else None,
                 "parent": q.get("parent") or None}
        self._json(200, self.service.emit(game, event))


def serve(data: Path, lib: Path, port: int, kafka: str | None) -> None:
    events: saves.EventLog = saves.KafkaLog(kafka) if kafka else saves.MemoryLog()
    service = Service(data, lib, events)
    service.start()
    handler = type("SavesHandler", (Handler,), {"service": service})
    log.info("serving saves", port=port, library=str(lib), kafka=kafka or "in memory")
    ThreadingHTTPServer(("", port), handler).serve_forever()


def main() -> None:
    serve(Path(os.environ.get("DATA", "/data")), Path(os.environ.get("LIBRARY", "/library")),
          int(os.environ.get("PORT", "8081")), os.environ.get("KAFKA"))
