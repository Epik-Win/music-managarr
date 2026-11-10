import os

import library_owner


def _library(tmp_path):
    album = tmp_path / "albums" / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "Song.mp3").write_bytes(b"audio")
    return tmp_path / "albums"


def test_files_not_the_owners_are_handed_to_the_owner(tmp_path, monkeypatch):
    root = _library(tmp_path)
    me = os.stat(root)
    calls = []
    monkeypatch.setattr(library_owner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(library_owner.os, "chown", lambda p, u, g, follow_symlinks=True: calls.append((p, u, g)))
    monkeypatch.setenv("PUID", str(me.st_uid + 1))
    monkeypatch.setenv("PGID", str(me.st_gid))
    assert library_owner.adopt(str(root)) == 4                     # albums/, Artist/, Album/, Song.mp3
    assert {u for _, u, _ in calls} == {me.st_uid + 1}


def test_the_library_folders_owner_is_the_default(tmp_path, monkeypatch):
    root = _library(tmp_path)
    monkeypatch.setattr(library_owner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(library_owner.os, "chown", lambda *a, **k: (_ for _ in ()).throw(AssertionError("chown")))
    monkeypatch.delenv("PUID", raising=False)
    monkeypatch.delenv("PGID", raising=False)
    assert library_owner.adopt(str(root)) == 0                     # everything is already its owner's


def test_nothing_happens_without_root(tmp_path, monkeypatch):
    root = _library(tmp_path)
    monkeypatch.setattr(library_owner.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("PUID", "4242")
    assert library_owner.adopt(str(root)) == 0
