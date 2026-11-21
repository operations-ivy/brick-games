"""Game save sync (brick-games#19): brick9000, the Retroid Pocket 2+ and the
RG476H share their saves, and the user decides whenever they disagree.

- What syncs: battery saves (RetroArch `.srm`, kind "sram") and PlayStation
  memory cards (kind "ps1": DuckStation's `<game>_1.mcd`, PCSX-ReARMed's
  `.srm`, both a raw 128 KiB card), never save states (tied to a core's
  version). A save's kind is its game's: "ps1" for PlayStation games.
- Each device runs the agent (saves_agent.py: brick9000 on a timer, a
  handheld over adb when it's plugged in to the laptop). It uploads a save
  that changed since its last sync, naming the version it started from (its
  parent), and writes the current version back only over a save the device
  hasn't changed since.
- Every upload is an event on Kafka (topic saves.events, keyed by game id),
  so one game's events are in one partition, in order, and one consumer
  (this service's referee) decides for that game at a time; the topic keeps
  every event for good, a second record beside the library.
- The referee (apply): a game's first save becomes current; a save whose
  parent is the current one moves it forward; the same bytes change nothing.
  Anything else is a conflict: two devices have different saves, and the
  user picks which to keep, on the page (the user, 2026-11-21). Until then
  the current version stays as it was and no device is overwritten. A pick
  is an event too ("kept"). No version is ever deleted.
- The library keeps it all: LIBRARY/<system>/saves/<Title>/state.json (the
  referee's state: current version, each device's, the versions, the log)
  and history/<sha256>.<srm|mcd>, every version by its content. Outside
  /extra/games like media/: not in the Samba share or brick9000's ROM sync.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import brick_observe

from . import library

log = brick_observe.get_logger(__name__)
TOPIC = "saves.events"
GROUP = "saves-referee"
PS1_CARD = 128 * 1024
EXTENSIONS = {"sram": "srm", "ps1": "mcd"}
LOG_KEEP = 200  # events kept in a state's log (all of them stay in Kafka)


def kind_of(system: str) -> str:
    return "ps1" if system == "psx" else "sram"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_state(game: dict) -> dict[str, Any]:
    return {"game": game["id"], "system": game["system"], "title": game["title"], "kind": kind_of(game["system"]),
            "head": None, "conflict": [], "versions": {}, "devices": {}, "log": []}


def apply(state: dict[str, Any], event: dict[str, Any]) -> str:
    """Apply one event to a game's state; what it did (the referee's decision)."""
    at = event.get("at", time.time())
    sha = event["sha"]
    if event["type"] == "uploaded":
        device = event["device"]
        version = state["versions"].setdefault(sha, {"size": event["size"], "first": at, "from": []})
        if device not in version["from"]:
            version["from"].append(device)
        version.setdefault("names", {})[device] = event["name"]
        version.setdefault("mtime", {})[device] = event.get("mtime")
        state["devices"][device] = {"sha": sha, "name": event["name"], "at": at}
        head, conflict = state["head"], state["conflict"]
        if head is None:
            state["head"], outcome = sha, "first"
        elif sha == head and not conflict:
            outcome = "same"
        elif sha in conflict:
            outcome = "conflict"
        elif event.get("parent") == head and not conflict:
            state["head"], outcome = sha, "forward"
        else:
            state["conflict"], outcome = sorted(set(conflict or [head]) | {sha}), "conflict"
    elif event["type"] == "kept":
        if sha not in state["versions"]:
            outcome = "unknown"
        else:
            state["head"], state["conflict"], outcome = sha, [], "kept"
    else:
        outcome = "ignored"
    state["log"] = (state["log"] + [{"type": event["type"], "sha": sha, "device": event.get("device"),
                                     "outcome": outcome, "at": at}])[-LOG_KEEP:]
    return outcome


class Store:
    """The saves in the library: each game's state.json and history/."""

    def __init__(self, lib: Path) -> None:
        self.lib = lib

    def folder(self, game: dict) -> Path:
        name = library.read_index(self.lib, game["system"]).get(game["id"])
        if name is None:  # the scan hasn't assigned it a folder yet
            name = library.safe_component(game["title"])
        return self.lib / game["system"] / "saves" / name

    def load(self, game: dict) -> dict[str, Any]:
        try:
            return json.loads((self.folder(game) / "state.json").read_text())
        except FileNotFoundError:
            return new_state(game)

    def save(self, game: dict, state: dict[str, Any]) -> None:
        write(self.folder(game) / "state.json", json.dumps(state, indent=1).encode())

    def blob_path(self, game: dict, sha: str) -> Path:
        return self.folder(game) / "history" / f"{sha}.{EXTENSIONS[kind_of(game['system'])]}"

    def put_blob(self, game: dict, data: bytes) -> str:
        sha = sha256(data)
        path = self.blob_path(game, sha)
        if not path.exists():
            write(path, data)
        return sha

    def states(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text()) for p in sorted(self.lib.glob("*/saves/*/state.json"))]


def write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.part")
    tmp.write_bytes(data)
    tmp.replace(dest)


class EventLog(Protocol):
    def produce(self, key: str, event: dict[str, Any]) -> None: ...
    def run(self, handle: Callable[[dict[str, Any]], None], stop: threading.Event) -> None: ...


class MemoryLog:
    """The event log without Kafka (tests, a laptop): events are handled as they're produced."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._handle: Callable[[dict[str, Any]], None] | None = None

    def produce(self, key: str, event: dict[str, Any]) -> None:
        self.events.append((key, event))
        if self._handle:
            self._handle(event)

    def run(self, handle: Callable[[dict[str, Any]], None], stop: threading.Event) -> None:
        self._handle = handle
        for _, event in self.events:
            handle(event)


class KafkaLog:
    """saves.events on the cluster's Kafka: kept for good, one partition per
    game's key. The referee commits an event only once its state is written,
    so a restart picks up where it stopped."""

    def __init__(self, servers: str) -> None:
        from confluent_kafka import Producer
        from confluent_kafka.admin import AdminClient, NewTopic

        self.servers = servers
        admin = AdminClient({"bootstrap.servers": servers})
        topics = self._wait_for_kafka(admin)
        if TOPIC not in topics:
            futures = admin.create_topics([NewTopic(TOPIC, num_partitions=3, replication_factor=1,
                                                    config={"retention.ms": "-1", "retention.bytes": "-1"})])
            for future in futures.values():
                future.result(30)
            log.info("topic created", topic=TOPIC)
        self.producer = Producer({"bootstrap.servers": servers, "acks": "all", "enable.idempotence": True})

    @staticmethod
    def _wait_for_kafka(admin, attempts: int = 60, pause: float = 5) -> dict:
        """The broker's topics, once it answers: on a cold start the saves pod
        can be up before the broker's name resolves."""
        from confluent_kafka import KafkaException

        for attempt in range(1, attempts + 1):
            try:
                return admin.list_topics(timeout=10).topics
            except KafkaException as e:
                if attempt == attempts:
                    raise
                log.info("waiting for kafka", attempt=attempt, error=str(e))
                time.sleep(pause)
        raise AssertionError("unreachable")

    def produce(self, key: str, event: dict[str, Any]) -> None:
        self.producer.produce(TOPIC, key=key.encode(), value=json.dumps(event).encode())
        self.producer.flush(30)

    def run(self, handle: Callable[[dict[str, Any]], None], stop: threading.Event) -> None:
        from confluent_kafka import Consumer

        consumer = Consumer({"bootstrap.servers": self.servers, "group.id": GROUP, "enable.auto.commit": False,
                             "auto.offset.reset": "earliest"})
        consumer.subscribe([TOPIC])
        try:
            while not stop.is_set():
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    log.warning("kafka", error=str(msg.error()))
                    continue
                handle(json.loads(msg.value()))
                consumer.commit(msg, asynchronous=False)
        finally:
            consumer.close()


class Referee:
    """Applies the events, one at a time, and writes each game's state."""

    def __init__(self, store: Store, games: Callable[[], dict[str, dict]]) -> None:
        self.store = store
        self.games = games
        self.applied = threading.Condition()
        self.seen: set[str] = set()  # event ids applied
        self.outcomes: dict[str, int] = {}

    def handle(self, event: dict[str, Any]) -> str:
        game = self.games().get(event["game"])
        if game is None:
            log.warning("save event for a game not in the catalog", game=event["game"])
            outcome = "unknown-game"
        else:
            state = self.store.load(game)
            outcome = apply(state, event)
            self.store.save(game, state)
            log.info("save event", game=game["title"], system=game["system"], type=event["type"],
                     device=event.get("device"), outcome=outcome)
        with self.applied:
            self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
            self.seen.add(event["id"])
            self.applied.notify_all()
        return outcome

    def wait(self, event_id: str, timeout: float = 15) -> bool:
        with self.applied:
            return self.applied.wait_for(lambda: event_id in self.seen, timeout)
