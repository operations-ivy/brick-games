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


@pytest.mark.parametrize("name", ["Legend of Zelda, The - A Link to the Past (U) [!].smc", "Final Fantasy III (USA)",
                                  "Disney's Aladdin.smc", "Vagrant Story (USA)_1"])
def test_the_agents_names_match_the_scans(name):
    assert saves_agent.norm(name) == sources.norm(name)


class Env(tuple):
    """(base, tmp_path, lib), plus .svc: the Service."""


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
    env = Env((base, tmp_path, lib))
    env.svc = svc  # for tests that change a device's settings
    yield env
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


def state(base, game_id, track=None):
    with urllib.request.urlopen(f"{base}/api/saves/{game_id}" + (f"?track={track}" if track else "")) as r:
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



def test_a_skipped_system_settles_a_shared_name(tmp_path):
    # The RG476H has Chrono Trigger for SNES and DS; DS is DraStic there, so a RetroArch .srm is SNES.
    cat = {"games": [{"id": "s" * 16, "system": "snes", "title": "Chrono Trigger", "files": ["snes/Chrono Trigger (U) [!].smc"]},
                     {"id": "n" * 16, "system": "nds", "title": "Chrono Trigger", "files": ["nds/Chrono Trigger (USA).nds"]}]}
    games = saves_agent.Games(cat)
    assert games.match("Chrono Trigger (U) [!]", on_device={"s" * 16, "n" * 16}) is None
    put(tmp_path / "saves" / "Chrono Trigger (U) [!].srm", b"sram")
    cfg = {"roots": [{"path": str(tmp_path / "saves"), "kind": "sram"}], "skip": ["nds"]}
    found = saves_agent.find_saves(saves_agent.Local(), cfg, games, {"s" * 16: "x", "n" * 16: "y"})
    assert list(found) == ["s" * 16]


def test_a_save_synced_before_keeps_its_game(tmp_path):
    # TMNT is GBA and arcade, both ROMs on the device, one flat folder: the last sync says which.
    cat = {"games": [{"id": "g" * 16, "system": "gba", "title": "TMNT", "files": ["gba/TMNT (USA).gba"]},
                     {"id": "a" * 16, "system": "arcade", "title": "TMNT", "files": ["arcade/TMNT (USA).zip"]}]}
    games = saves_agent.Games(cat)
    save = tmp_path / "saves" / "TMNT (USA).srm"
    put(save, b"sram")
    cfg = {"roots": [{"path": str(tmp_path / "saves"), "kind": "sram"}]}
    roms = {"g" * 16: "TMNT (USA)", "a" * 16: "TMNT (USA)"}
    assert saves_agent.find_saves(saves_agent.Local(), cfg, games, roms) == {}
    found = saves_agent.find_saves(saves_agent.Local(), cfg, games, roms, {str(save): "g" * 16})
    assert list(found) == ["g" * 16]

def test_new_saves_go_in_the_systems_core_folder_and_skipped_systems_stay_out(tmp_path):
    fs = saves_agent.Local()
    cfg = {"roots": [{"path": str(tmp_path / "saves"), "kind": "sram",
                      "write": {"snes": "Snes9x/{stem}.srm", "gba": "mGBA/{stem}.srm"}}], "skip": ["dreamcast"]}
    snes = {"system": "snes"}
    assert saves_agent.new_path(fs, cfg, snes, "Super Metroid (USA)") == str(tmp_path / "saves" / "Snes9x" / "Super Metroid (USA).srm")
    assert saves_agent.new_path(fs, cfg, {"system": "nes"}, "Zelda") is None  # a core it doesn't name: no new save
    cat = {"games": [{"id": "d" * 16, "system": "dreamcast", "title": "Marvel vs. Capcom",
                      "files": ["Dreamcast/Marvel vs. Capcom.chd"]}]}
    put(tmp_path / "saves" / "Marvel vs. Capcom.srm", b"vmu")
    assert saves_agent.find_saves(fs, cfg, saves_agent.Games(cat)) == {}


@pytest.mark.parametrize("query,body,status", [
    ("device=rp2plus&name=x&mtime=yesterday", b"save", 400),  # mtime isn't a number
    ("device=rp2plus&name=x", b"", 400),  # empty
    ("device=nobody&name=x", b"save", 404),  # unknown device
])
def test_bad_uploads_are_refused(service, query, body, status):
    base, _, _ = service
    req = urllib.request.Request(f"{base}/api/saves/{GAME['id']}/upload?{query}", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == status


@pytest.mark.parametrize("write,folders", [
    ({"gba": "mGBA/{stem}.srm", "snes": "Snes9x/{stem}.srm"}, {"mGBA": "gba", "Snes9x": "snes"}),
    ({"megadrive": "Genesis Plus GX/{stem}.srm", "segacd": "Genesis Plus GX/{stem}.srm"}, {}),  # two systems
    ("{stem}.srm", {}),  # one name for every system: no folders
])
def test_core_folders(write, folders):
    assert saves_agent.core_folders({"write": write}) == folders


def test_a_core_folder_settles_a_name_three_games_share(tmp_path):
    fs = saves_agent.Local()
    root = {"path": str(tmp_path / "saves"), "recursive": True, "kind": "sram",
            "write": {"gba": "mGBA/{stem}.srm", "arcade": "FinalBurn Neo/{stem}.srm"}}
    cat = {"games": [{"id": "g" * 16, "system": "gba", "title": "TMNT", "files": ["gba/TMNT (USA).gba"]},
                     {"id": "a" * 16, "system": "arcade", "title": "Teenage Mutant Ninja Turtles", "files": ["fba/tmnt.zip"]}],
           "systems": [{"id": "gba", "exts": ["gba"]}, {"id": "arcade", "exts": ["zip"]}]}
    put(tmp_path / "saves" / "mGBA" / "TMNT (USA).srm", b"gba save")
    both = {"g" * 16: "TMNT (USA)", "a" * 16: "tmnt"}  # both ROMs on the device: the names alone can't tell
    assert list(saves_agent.find_saves(fs, {"roots": [root]}, saves_agent.Games(cat), both)) == ["g" * 16]


def test_a_new_save_is_never_written_over_a_file(service, monkeypatch):
    base, tmp, _ = service
    put(tmp / "b9k-roms" / "snes" / "Super Metroid (JU) [!].smc", b"rom")
    put(tmp / "rp" / "Super Metroid (USA).srm", b"retroid save")
    run(base, "rp2plus", tmp)  # current: the Retroid's
    mine = put(tmp / "b9k" / "Super Metroid (JU) [!].srm", b"brick9000 save, not matched")
    monkeypatch.setattr(saves_agent, "find_saves", lambda *a, **k: {})  # as if the agent couldn't tell whose it is
    said = []
    saves_agent.sync(saves_agent.Server(base), saves_agent.Local(), "brick9000", tmp / "state" / "brick9000.json",
                     say=said.append)
    assert mine.read_bytes() == b"brick9000 save, not matched"
    assert any(line.startswith("skip    Super Metroid") for line in said)


def test_the_service_says_whats_in_each_version(service):
    base, tmp, _ = service
    put(tmp / "b9k" / "Super Metroid (JU) [!].srm", bytes(8192))  # never saved
    run(base, "brick9000", tmp)
    about = state(base, GAME["id"])["about"]
    assert list(about.values()) == [{"blank": True, "summary": "no saved slots"}]


# brick-games#32: a blank save never replaces a real one.
def blank_up(sha, device, parent=None):
    return up(sha, device, parent) | {"blank": True}


def test_a_blank_save_never_replaces_a_real_one():
    s = saves.new_state(GAME)
    saves.apply(s, up("real", "rg476h"))
    # brick9000's emulator wrote a fresh .srm, and it was uploaded as if it were progress
    assert saves.apply(s, blank_up("zeros", "brick9000", parent="real")) == "blank"
    assert s["head"] == "real" and s["conflict"] == [] and s["versions"]["zeros"]["blank"]
    # nor does it join a conflict
    saves.apply(s, up("other", "rp2plus", parent="old"))
    assert saves.apply(s, blank_up("zeros", "brick9000")) == "blank" and "zeros" not in s["conflict"]


def test_a_real_save_moves_on_from_a_blank_one():
    s = saves.new_state(GAME)
    assert saves.apply(s, blank_up("zeros", "brick9000")) == "first"
    # the RG476H's real save never had the blank as its parent: forward all the same, no conflict
    assert saves.apply(s, up("real", "rg476h")) == "forward" and s["head"] == "real"


def test_a_device_that_blanked_its_save_gets_the_real_one_back(service):
    base, tmp, _ = service
    b9k = put(tmp / "b9k" / "Super Metroid (JU) [!].srm", b"real progress")
    run(base, "brick9000", tmp)
    b9k.write_bytes(bytes(8192))  # the emulator started the game afresh
    done = run(base, "brick9000", tmp)
    assert (done["uploaded"], done["written"]) == (1, 1)
    assert b9k.read_bytes() == b"real progress"
    s = state(base, GAME["id"])
    assert s["log"][-2]["outcome"] == "blank" and len(s["versions"]) == 2


# State slot 10: shared between devices that run the same core for the system.
def states_config(svc, tmp, rp_core="Snes9x"):
    for device, folder, core in (("brick9000", "b9k-states", "snes9x_libretro.so"), ("rp2plus", "rp-states", rp_core)):
        svc.config[device]["roots"].append(
            {"path": str(tmp / folder), "match": ["*.state10"], "kind": "state", "write": "{stem}.state10"})
        svc.config[device]["cores"] = {"snes": core}


def test_slot_10_goes_to_a_device_with_the_same_core_and_slots_0_to_9_stay(service):
    base, tmp, lib = service
    states_config(service.svc, tmp)
    put(tmp / "rp-roms" / "SNES" / "Super Metroid (USA).sfc", b"rom")
    put(tmp / "b9k-states" / "Super Metroid (JU) [!].state10", b"shared state")
    put(tmp / "b9k-states" / "Super Metroid (JU) [!].state1", b"brick9000's own")
    assert run(base, "brick9000", tmp)["uploaded"] == 1
    assert run(base, "rp2plus", tmp)["written"] == 1
    assert (tmp / "rp-states" / "Super Metroid (USA).state10").read_bytes() == b"shared state"
    assert not (tmp / "rp-states" / "Super Metroid (USA).state1").exists()
    s = state(base, GAME["id"], "slot10")
    assert s["versions"][s["head"]]["core"] == "snes9x_libretro.so"
    assert (lib / "snes" / "saves" / "Super Metroid" / "history" / f"{s['head']}.state").read_bytes() == b"shared state"
    # the in-game save track knows nothing of it, so an agent from before slot 10 can't mix them up
    with urllib.request.urlopen(f"{base}/api/saves") as r:
        summary = json.loads(r.read())
    assert summary["games"] == [] and summary["states"][0]["core"] == "snes9x_libretro.so"


def test_slot_10_never_goes_to_a_device_with_another_core(service):
    base, tmp, _ = service
    states_config(service.svc, tmp, rp_core="bsnes")
    put(tmp / "rp-roms" / "SNES" / "Super Metroid (USA).sfc", b"rom")
    put(tmp / "b9k-states" / "Super Metroid (JU) [!].state10", b"snes9x state")
    run(base, "brick9000", tmp)
    said = []
    saves_agent.sync(saves_agent.Server(base), saves_agent.Local(), "rp2plus", tmp / "state" / "rp2plus.json",
                     say=said.append)
    assert not (tmp / "rp-states").exists()
    assert any("made with snes9x_libretro.so, this device runs bsnes" in line for line in said)


def test_a_state_needs_its_core(service):
    base, _, _ = service
    req = urllib.request.Request(f"{base}/api/saves/{GAME['id']}/upload?device=rp2plus&name=x&track=slot10",
                                 data=b"state", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400


@pytest.mark.parametrize(("name", "core"), [("Genesis Plus GX", "genesisplusgx"),
                                            ("genesis_plus_gx_libretro.so", "genesisplusgx"),
                                            ("snes9x_libretro_android.so", "snes9x"), (None, "")])
def test_core_ids(name, core):
    assert saves_agent.core_id(name) == core


def test_an_event_for_an_unknown_track_never_stops_the_referee(tmp_path):
    ref = saves.Referee(saves.Store(tmp_path), lambda: {GAME["id"]: GAME})
    event = up("v1", "brick9000") | {"id": "e1", "game": GAME["id"], "track": "slot99"}
    assert ref.handle(event) == "unknown-track"


def test_an_in_game_save_over_8_mib_is_refused_before_it_is_read(service):
    import http.client

    base, _, _ = service
    conn = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
    conn.putrequest("POST", f"/api/saves/{GAME['id']}/upload?device=rp2plus&name=x")
    conn.putheader("Content-Length", str(saves_web.MAX_SAVE + 1))
    conn.endheaders()  # the headers alone: the answer comes before any body is sent
    assert conn.getresponse().status == 400
    conn.close()
