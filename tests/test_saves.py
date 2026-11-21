import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from brick_games import saves, saves_agent, saves_web, sources

GAME = {"id": "a" * 16, "system": "snes", "title": "Super Metroid", "files": ["snes/Super Metroid (JU) [!].smc"]}
CARD = {"id": "b" * 16, "system": "psx", "title": "Vagrant Story", "files": ["psx/Vagrant Story (USA).chd"]}


def up(sha, device, parent=None):
    return {"type": "uploaded", "sha": sha, "device": device, "name": "x.srm", "size": 8, "parent": parent, "at": 1}


def test_the_referee_moves_forward_and_flags_conflicts():
    s = saves.new_state(GAME)
    assert saves.apply(s, up("v1", "brick9000")) == "first" and s["head"] == "v1"
    assert saves.apply(s, up("v1", "rp2plus")) == "same"
    assert saves.apply(s, up("v2", "rp2plus", parent="v1")) == "forward" and s["head"] == "v2"
    # brick9000 played on from v1 too: two devices disagree, the user picks
    assert saves.apply(s, up("v3", "brick9000", parent="v1")) == "conflict"
    assert s["head"] == "v2" and s["conflict"] == ["v2", "v3"]
    # nothing moves forward while it waits, not even a "forward" upload
    assert saves.apply(s, up("v4", "rp2plus", parent="v2")) == "conflict" and s["conflict"] == ["v2", "v3", "v4"]
    assert saves.apply(s, {"type": "kept", "sha": "v3", "at": 2}) == "kept"
    assert s["head"] == "v3" and s["conflict"] == [] and set(s["versions"]) == {"v1", "v2", "v3", "v4"}
    assert saves.apply(s, {"type": "kept", "sha": "nope", "at": 3}) == "unknown"


def test_the_agents_names_match_the_scans():
    for name in ["Legend of Zelda, The - A Link to the Past (U) [!].smc", "Final Fantasy III (USA)",
                 "Disney's Aladdin.smc", "Vagrant Story (USA)_1"]:
        assert saves_agent.norm(name) == sources.norm(name)


@pytest.fixture
def service(tmp_path, monkeypatch):
    data, lib = tmp_path / "data", tmp_path / "library"
    data.mkdir()
    (data / "catalog.json").write_text(json.dumps({"games": [GAME, CARD], "systems": [
        {"id": "snes", "exts": ["smc", "sfc"]}, {"id": "psx", "exts": ["chd", "cue"]}]}))
    (lib / "snes").mkdir(parents=True)
    (lib / "snes" / "media.json").write_text(json.dumps({GAME["id"]: "Super Metroid"}))
    svc = saves_web.Service(data, lib, saves.MemoryLog())
    svc.config = {
        "brick9000": {"name": "brick9000", "roots": [{"path": str(tmp_path / "b9k"), "match": ["*.srm"],
                                                      "write": "{stem}.srm"}],
                      "roms": [str(tmp_path / "b9k-roms")]},
        "rp2plus": {"name": "Retroid", "roots": [
            {"path": str(tmp_path / "rp"), "match": ["*.srm"], "recursive": True, "kind": "sram", "write": "{stem}.srm"},
            {"path": str(tmp_path / "rp-cards"), "match": ["*_1.mcd"], "system": "psx", "strip": "_1", "kind": "ps1",
             "write": "{stem}_1.mcd"}],
                    "roms": [str(tmp_path / "rp-roms")]},
    }
    svc.start()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (saves_web.Handler,), {"service": svc}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    # the agent reads the catalog from the page's server: here, the saves service's catalog
    real = saves_agent.Server.json

    def json_(self, method, path, body=None, query=None):
        if path == "/api/catalog":
            return json.loads((data / "catalog.json").read_text())
        return real(self, method, path, body, query)

    monkeypatch.setattr(saves_agent.Server, "json", json_)
    yield base, tmp_path, lib
    srv.shutdown()
    srv.server_close()
    svc.stop.set()


def put(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def run(base, device, tmp_path, **kw):
    return saves_agent.sync(saves_agent.Server(base), saves_agent.Local(), device,
                            tmp_path / "state" / f"{device}.json", say=lambda *a: None, **kw)


def state(base, game_id):
    with urllib.request.urlopen(f"{base}/api/saves/{game_id}") as r:
        return json.loads(r.read())


def test_two_devices_sync_and_a_conflict_waits_for_the_user(service):
    base, tmp, lib = service
    b9k = put(tmp / "b9k" / "Super Metroid (JU) [!].srm", b"save one")
    put(tmp / "rp-roms" / "SNES" / "Super Metroid (USA).sfc", b"rom")
    put(tmp / "b9k-roms" / "snes" / "Super Metroid (JU) [!].smc", b"rom")
    # brick9000's save is the first: it becomes current, and goes to the Retroid by its own ROM's name
    assert run(base, "brick9000", tmp)["uploaded"] == 1
    assert run(base, "rp2plus", tmp)["written"] == 1
    rp = tmp / "rp" / "Super Metroid (USA).srm"
    assert rp.read_bytes() == b"save one"
    s = state(base, GAME["id"])
    assert s["devices"]["rp2plus"]["sha"] == s["head"] == s["devices"]["brick9000"]["sha"]
    assert (lib / "snes" / "saves" / "Super Metroid" / "history" / f"{s['head']}.srm").read_bytes() == b"save one"
    # the Retroid plays on: brick9000 gets it
    rp.write_bytes(b"save two")
    assert run(base, "rp2plus", tmp)["uploaded"] == 1
    assert run(base, "brick9000", tmp)["written"] == 1 and b9k.read_bytes() == b"save two"
    # both play on from "save two": a conflict, and neither is overwritten
    rp.write_bytes(b"retroid three")
    b9k.write_bytes(b"brick9000 three")
    run(base, "rp2plus", tmp)
    assert run(base, "brick9000", tmp)["conflicts"] == 1
    assert run(base, "rp2plus", tmp)["written"] == 0
    assert rp.read_bytes() == b"retroid three" and b9k.read_bytes() == b"brick9000 three"
    s = state(base, GAME["id"])
    assert len(s["conflict"]) == 2
    # the user keeps brick9000's: it goes to the Retroid on its next sync
    keep = next(sha for sha in s["conflict"] if "brick9000" in s["versions"][sha]["from"])
    req = urllib.request.Request(f"{base}/api/saves/{GAME['id']}/keep", data=json.dumps({"sha": keep}).encode(),
                                 method="POST")
    with urllib.request.urlopen(req) as r:
        assert json.loads(r.read())["head"] == keep
    assert run(base, "rp2plus", tmp)["written"] == 1 and rp.read_bytes() == b"brick9000 three"
    # every version is still there
    assert len(state(base, GAME["id"])["versions"]) == 4


def test_ps1_cards_must_be_a_card(service):
    base, tmp, _ = service
    put(tmp / "rp-cards" / "Vagrant Story (USA)_1.mcd", b"x" * 100)  # not a card: not synced
    put(tmp / "rp-cards" / "Unknown Game_1.mcd", b"x" * saves.PS1_CARD)  # not in the catalog
    assert run(base, "rp2plus", tmp)["uploaded"] == 0
    put(tmp / "rp-cards" / "Vagrant Story (USA)_1.mcd", b"c" * saves.PS1_CARD)
    assert run(base, "rp2plus", tmp)["uploaded"] == 1
    req = urllib.request.Request(f"{base}/api/saves/{CARD['id']}/upload?device=rp2plus&name=x", data=b"short",
                                 method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400


def test_a_dry_run_changes_nothing(service):
    base, tmp, _ = service
    put(tmp / "b9k" / "Super Metroid (JU) [!].srm", b"save")
    assert run(base, "brick9000", tmp, dry_run=True)["uploaded"] == 0
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{base}/api/saves/{'f' * 16}")
    assert state(base, GAME["id"])["head"] is None
    assert not (tmp / "state").exists()


def test_the_service_lists_games_and_serves_the_agent(service):
    base, tmp, _ = service
    put(tmp / "b9k" / "Super Metroid (JU) [!].srm", b"save")
    run(base, "brick9000", tmp)
    with urllib.request.urlopen(f"{base}/api/saves") as r:
        summary = json.loads(r.read())
    assert summary["games"][0]["title"] == "Super Metroid" and summary["games"][0]["devices"] == {"brick9000": True}
    assert "brick9000" in summary["last_sync"]
    with urllib.request.urlopen(f"{base}/api/saves/agent.py") as r:
        assert b"def sync(" in r.read()
    with urllib.request.urlopen(f"{base}/api/saves/config?device=rp2plus") as r:
        assert json.loads(r.read())["name"] == "Retroid"


def test_a_name_two_systems_share_goes_by_the_rom_on_the_device():
    cat = {"games": [{"id": "s" * 16, "system": "snes", "title": "Breath of Fire II", "files": ["snes/Breath of Fire II.sfc"]},
                     {"id": "g" * 16, "system": "gba", "title": "Breath of Fire II", "files": ["gba/Breath of Fire II.gba"]}],
           "systems": [{"id": "snes", "exts": ["sfc"]}, {"id": "gba", "exts": ["gba"]}]}
    games = saves_agent.Games(cat)
    assert games.match("Breath of Fire II") is None  # two systems: no guess
    assert games.match("Breath of Fire II", on_device={"s" * 16}) == "s" * 16
