"""The saves service (`python -m brick_games saves`): the referee and the API
the agents and the page use, behind games.brick.nozdormu.cloud/api/saves.

    GET  /api/saves                     every game with saves: its current version,
                                        each device's, and whether there's a conflict;
                                        `states`: the same for state slot 10 (with its core)
    GET  /api/saves/config?device=      that device's save folders (saves.yaml)
    GET  /api/saves/agent.py            the agent (saves_agent.py), for brick9000's timer
    GET  /api/saves/<game>              a game's state: versions, devices, conflict, log,
                                        and `about`: what's in each version (save_decode)
    GET  /api/saves/<game>/<sha>        one version's bytes
    POST /api/saves/<game>/upload?device=&name=&mtime=&parent=
                                        a device's save (the body): stored by its hash,
                                        then an "uploaded" event; answers the new state
    POST /api/saves/<game>/keep         {"sha": ...}: the user's pick in a conflict
    POST /api/saves/seen?device=        an agent's run finished (its last-sync time)
    GET  /health /metrics

Each of a game's calls takes `track=slot10` for its state slot 10 (an upload
also `core=`, the core that made it); without it, the in-game save.

LAN only, like the page. Games come from the catalog the scan writes (DATA,
read-only); saves live in the library (LIBRARY, read-write; saves.py).
"""

from __future__ import annotations

import functools
import json
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

from . import save_decode, saves

log = brick_observe.get_logger(__name__)
CONFIG_FILE = Path(__file__).with_name("saves.yaml")
AGENT_FILE = Path(__file__).with_name("saves_agent.py")
GAME = re.compile(r"^/api/saves/(?P<game>[0-9a-f]{16})(?:/(?P<what>upload|keep|[0-9a-f]{64}))?$")
MAX_SAVE = 8 * 2**20  # bytes: the biggest battery saves and cards are well under this
MAX_STATE = 32 * 2**20  # a state is the core's whole machine: PlayStation and N64 ones run to megabytes
EVENTS = Counter("games_saves_events_total", "Save events, by type and the referee's outcome", ["type", "outcome"])
CONFLICTS = Gauge("games_saves_conflicts", "Games whose saves wait for the user's pick")
GAMES = Gauge("games_saves_games", "Games with at least one synced save")
LAST_SYNC = Gauge("games_saves_last_sync_timestamp_seconds", "When each device's agent last finished", ["device"])


UNKNOWN = {"blank": False, "summary": ""}


@functools.lru_cache(maxsize=1024)  # a version never changes: its file is named by its hash
def _describe(system: str, title: str, path: Path) -> dict:
    return save_decode.describe({"system": system, "title": title}, path.read_bytes())


def about(system: str, title: str, path: Path) -> dict:
    """What's in one version (save_decode). Shared by every caller: read only.
    A file that isn't there (yet) is never cached."""
    try:
        return _describe(system, title, path)
    except OSError:
        return UNKNOWN


class Catalog:
    """The scan's catalog.json, by game id, re-read when it changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._key: tuple[int, int] | None = None
        self._games: dict[str, dict] = {}
        self._lock = threading.Lock()

    def games(self) -> dict[str, dict]:
        with self._lock:
            try:
                st = self.path.stat()
            except FileNotFoundError:  # no scan yet: no games
                return self._games
            if (st.st_ino, st.st_mtime_ns) != self._key:
                self._games = {g["id"]: g for g in json.loads(self.path.read_text())["games"]}
                self._key = (st.st_ino, st.st_mtime_ns)
            return self._games


class Service:
    """The referee, the store and the devices' settings, shared by the
    request threads. last_sync is written one key at a time (atomic in
    CPython), so it needs no lock."""

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
        CONFLICTS.set(sum(1 for s in states + self.store.states(saves.SLOT10) if s["conflict"]))

    def state(self, game: dict, track: str = saves.SAVE) -> dict:
        """A game's state, with what's in each version."""
        state = self.store.load(game, track)
        if track == saves.SLOT10:  # a core's snapshot: nothing to read in it but which core
            state["about"] = {sha: {"blank": False, "summary": f"state, {v.get('core') or 'core unknown'}"}
                              for sha, v in state["versions"].items()}
        else:
            state["about"] = {sha: about(game["system"], game["title"], self.store.blob_path(game, sha))
                              for sha in state["versions"]}
        return state

    def emit(self, game: dict, event: dict, track: str = saves.SAVE) -> dict:
        event |= {"id": uuid.uuid4().hex, "game": game["id"], "at": time.time()}
        if track != saves.SAVE:
            event["track"] = track
        self.events.produce(game["id"], event)
        self.referee.wait(event["id"])
        return self.state(game, track)


class BadRequest(ValueError):
    """A request the service can't take: answered 400 with its message."""


class Handler(BaseHTTPRequestHandler):
    service: Service
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, *args: object) -> None:
        pass  # requests are counted in /metrics and logged by what they do, not one line each

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

    def _request(self) -> tuple[str, dict[str, str]]:
        url = urlsplit(self.path)
        return url.path, {k: v[0] for k, v in parse_qs(url.query).items()}

    def _game(self, game_id: str) -> dict:
        game = self.service.catalog.games().get(game_id)
        if game is None:
            raise LookupError("no such game in the catalog")
        return game

    def _device(self, q: dict[str, str]) -> str:
        device = q.get("device", "")
        if device not in self.service.config:
            raise LookupError("unknown device")
        return device

    def _body(self, limit: int = MAX_SAVE) -> bytes:
        """The request's body, refused before it's read when it's over limit."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("a bad Content-Length") from None
        if not 0 <= length <= limit:
            raise BadRequest(f"a body of 0 to {limit} bytes")
        return self.rfile.read(length)

    @staticmethod
    def _track(q: dict[str, str]) -> str:
        track = q.get("track") or saves.SAVE
        if track not in saves.TRACK_FILES:
            raise BadRequest(f"track is {' or '.join(saves.TRACK_FILES)}")
        return track

    def do_GET(self) -> None:
        path, q = self._request()
        self._dispatch(lambda: self._get(path, q))

    def do_POST(self) -> None:
        path, q = self._request()
        self._dispatch(lambda: self._post(path, q))

    def _dispatch(self, route) -> None:
        try:
            route()
        except BadRequest as e:
            self._json(400, {"error": str(e)})
        except LookupError as e:
            self._json(404, {"error": str(e).strip("'")})

    def _get(self, path: str, q: dict[str, str]) -> None:
        if path in ("/health", "/ready"):
            self._send(200, b"ok", "text/plain")
        elif path == "/metrics":
            body, kind = observe_metrics.exposition()
            self._send(200, body, kind)
        elif path == "/api/saves":
            self._json(200, self._summary())
        elif path == "/api/saves/config":
            self._json(200, self.service.config[self._device(q)])
        elif path == "/api/saves/agent.py":
            self._send(200, AGENT_FILE.read_bytes(), "text/x-python; charset=utf-8")
        elif (m := GAME.match(path)) and m["what"] is None:
            self._json(200, self.service.state(self._game(m["game"]), self._track(q)))
        elif m and len(m["what"]) == 64:
            self._blob(self._game(m["game"]), m["what"], self._track(q))
        else:
            raise LookupError("not found")

    def _summary(self) -> dict:
        def entry(s: dict) -> dict:
            head = s["versions"].get(s["head"] or "", {})
            return {"game": s["game"], "system": s["system"], "title": s["title"], "head": s["head"],
                    "conflict": bool(s["conflict"]), "core": head.get("core"),
                    "devices": {d: v["sha"] == s["head"] for d, v in s["devices"].items()}}

        # states apart from games: an agent from before slot 10 must never take a state for a save
        return {"games": [entry(s) for s in self.service.store.states()],
                "states": [entry(s) for s in self.service.store.states(saves.SLOT10)],
                "last_sync": self.service.last_sync,
                "devices": {k: v["name"] for k, v in self.service.config.items()}}

    def _blob(self, game: dict, sha: str, track: str) -> None:
        path = self.service.store.blob_path(game, sha, track)
        if not path.is_file():
            raise LookupError("no such version")
        names = self.service.store.load(game, track)["versions"].get(sha, {}).get("names", {})
        name = next(iter(names.values()), path.name)
        self._send(200, path.read_bytes(), "application/octet-stream",
                   {"Content-Disposition": f'attachment; filename="{name}"'})

    def _post(self, path: str, q: dict[str, str]) -> None:
        body = self._body(MAX_STATE if self._track(q) == saves.SLOT10 else MAX_SAVE)
        if path == "/api/saves/seen":
            device = self._device(q)
            self.service.last_sync[device] = time.time()
            LAST_SYNC.labels(device).set(self.service.last_sync[device])
            self._json(200, {"ok": True})
            return
        m = GAME.match(path)
        if not m or m["what"] not in ("upload", "keep"):
            raise LookupError("not found")
        game, track = self._game(m["game"]), self._track(q)
        if m["what"] == "keep":
            self._json(200, self._keep(game, track, body))
        else:
            self._json(200, self._upload(game, track, self._device(q), body, q))

    def _keep(self, game: dict, track: str, body: bytes) -> dict:
        try:
            sha = json.loads(body)["sha"]
        except (ValueError, KeyError, TypeError):
            raise BadRequest('send {"sha": "<the version to keep>"}') from None
        if sha not in self.service.store.load(game, track)["versions"]:
            raise LookupError("no such version")
        return self.service.emit(game, {"type": "kept", "sha": sha}, track)

    def _upload(self, game: dict, track: str, device: str, body: bytes, q: dict[str, str]) -> dict:
        if not body:
            raise BadRequest("an empty save")
        event = {"type": "uploaded", "device": device, "name": q.get("name", ""), "size": len(body)}
        if track == saves.SLOT10:
            if not q.get("core"):
                raise BadRequest("a state needs core=: the core that made it")
            event["core"] = q["core"]
        else:
            if saves.kind_of(game["system"]) == "ps1" and len(body) != saves.PS1_CARD:
                raise BadRequest(f"a PlayStation card is {saves.PS1_CARD} bytes, this is {len(body)}")
            # nothing saved in it (an emulator's fresh file): never over a real save (#32)
            event["blank"] = bool(save_decode.describe(game, body)["blank"])
        try:
            event["mtime"] = float(q["mtime"]) if q.get("mtime") else None
        except ValueError:
            raise BadRequest("mtime is seconds since 1970") from None
        event |= {"sha": self.service.store.put_blob(game, body, track), "parent": q.get("parent") or None}
        return self.service.emit(game, event, track)


def serve(data: Path, lib: Path, port: int, kafka: str | None) -> None:
    events: saves.EventLog = saves.KafkaLog(kafka) if kafka else saves.MemoryLog()
    service = Service(data, lib, events)
    service.start()
    class SavesHandler(Handler):
        pass

    SavesHandler.service = service
    log.info("serving saves", port=port, library=str(lib), kafka=kafka or "in memory")
    ThreadingHTTPServer(("", port), SavesHandler).serve_forever()

