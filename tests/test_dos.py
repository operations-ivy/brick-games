import zipfile

import pytest

from brick_games import dos


def make_zip(path, names):
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.writestr(name, b"" if name.endswith("/") else name.encode())


def test_build_adds_autoboot_and_keeps_files(tmp_path):
    src, dst = tmp_path / "Game (1993).zip", tmp_path / "out.zip"
    make_zip(src, ["GAME.EXE", "INSTALL.EXE", "DATA/LEVEL1.DAT"])
    dos.build(src, dst, "game.exe")
    with zipfile.ZipFile(dst) as z:
        assert z.read("AUTOBOOT.DBP") == b"C:\\GAME.EXE"
        assert z.read("DATA/LEVEL1.DAT") == b"DATA/LEVEL1.DAT"
        assert sorted(z.namelist()) == ["AUTOBOOT.DBP", "DATA/LEVEL1.DAT", "GAME.EXE", "INSTALL.EXE"]


def test_build_uses_dos_path_for_subfolder(tmp_path):
    src, dst = tmp_path / "a.zip", tmp_path / "b.zip"
    make_zip(src, ["REDALERT/RA.EXE", "ALLIES.BAT"])
    dos.build(src, dst, "RedAlert/ra.exe")
    with zipfile.ZipFile(dst) as z:
        assert z.read("AUTOBOOT.DBP") == b"C:\\REDALERT\\RA.EXE"


def test_build_strips_a_long_named_top_folder(tmp_path):
    src, dst = tmp_path / "a.zip", tmp_path / "b.zip"
    top = "Theme Hospital (1997)(Bullfrog Productions)/"
    make_zip(src, [top, top + "HOSPITAL.EXE", top + "DATA/X.DAT"])
    dos.build(src, dst, "HOSPITAL.EXE")
    with zipfile.ZipFile(dst) as z:
        assert sorted(z.namelist()) == ["AUTOBOOT.DBP", "DATA/X.DAT", "HOSPITAL.EXE"]


def test_build_keeps_a_dos_named_top_folder(tmp_path):
    src, dst = tmp_path / "a.zip", tmp_path / "b.zip"
    make_zip(src, ["DUNECD/DUNE.EXE", "DUNECD/DUNE.DAT"])
    dos.build(src, dst, "DUNECD/DUNE.EXE")
    with zipfile.ZipFile(dst) as z:
        assert z.read("AUTOBOOT.DBP") == b"C:\\DUNECD\\DUNE.EXE"


def test_build_replaces_an_existing_autoboot(tmp_path):
    src, dst = tmp_path / "a.zip", tmp_path / "b.zip"
    make_zip(src, ["A.EXE", "B.EXE", "AUTOBOOT.DBP"])
    dos.build(src, dst, "B.EXE")
    with zipfile.ZipFile(dst) as z:
        assert z.namelist().count("AUTOBOOT.DBP") == 1
        assert z.read("AUTOBOOT.DBP") == b"C:\\B.EXE"


def test_build_writes_dos_yml_settings(tmp_path):
    src, dst = tmp_path / "a.zip", tmp_path / "b.zip"
    make_zip(src, ["ALONECRK.COM", "TATOU.COM", "DOS.YML"])
    dos.build(src, dst, {"program": "alonecrk.com", "dos_yml": {"run_input": "(WAIT:3000)(F1)", "cpu_cycles": 1320}})
    with zipfile.ZipFile(dst) as z:
        assert z.namelist().count("DOS.YML") == 1
        assert z.read("DOS.YML") == (b"run_path: C:\\ALONECRK.COM\r\nrun_input: (WAIT:3000)(F1)\r\n"
                                     b"cpu_cycles: 1320\r\n")
        assert z.read("AUTOBOOT.DBP") == b"C:\\ALONECRK.COM"


def test_build_keeps_the_zips_own_dos_yml_without_settings(tmp_path):
    src, dst = tmp_path / "a.zip", tmp_path / "b.zip"
    make_zip(src, ["GAME.EXE", "DOS.YML"])
    dos.build(src, dst, "GAME.EXE")
    with zipfile.ZipFile(dst) as z:
        assert z.read("DOS.YML") == b"DOS.YML"


def test_build_refuses_a_missing_program(tmp_path):
    src = tmp_path / "a.zip"
    make_zip(src, ["INSTALL.EXE"])
    with pytest.raises(ValueError, match="no GAME.EXE"):
        dos.build(src, tmp_path / "b.zip", "GAME.EXE")
    assert not (tmp_path / "b.zip").exists()


def test_build_all_copies_unlisted_zips_unchanged(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    make_zip(src / "One (1990).zip", ["ONE.EXE"])
    make_zip(src / "Two (1991).zip", ["TWO.EXE"])
    assert dos.build_all(src, dst, {"One (1990)": "ONE.EXE"}) == ["Two (1991)"]
    assert sorted(p.name for p in dst.iterdir()) == ["One (1990).zip", "Two (1991).zip"]
    assert (dst / "Two (1991).zip").read_bytes() == (src / "Two (1991).zip").read_bytes()
    with zipfile.ZipFile(dst / "One (1990).zip") as z:
        assert "AUTOBOOT.DBP" in z.namelist()


def test_build_all_refuses_a_listed_game_without_a_zip(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(ValueError, match="Gone"):
        dos.build_all(src, tmp_path / "dst", {"Gone (1990)": "GONE.EXE"})


def test_programs_file_loads():
    programs = dos.load_programs()
    assert programs
    for entry in programs.values():
        program, settings = dos.program_of(entry)
        assert isinstance(program, str) and program
        assert isinstance(settings, dict)


def test_failed_build_leaves_no_partial_file(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.zip", tmp_path / "b.zip"
    make_zip(src, ["GAME.EXE"])
    monkeypatch.setattr(dos, "autoboot_line", lambda program: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        dos.build(src, dst, "GAME.EXE")
    assert list(tmp_path.iterdir()) == [src]
