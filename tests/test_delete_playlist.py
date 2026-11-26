"""The admin's Delete playlist: every song in it is deleted on the daily pass."""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import delete_playlist
import fix_metadata_daemon
import library_review
import library_state
import paths


class FakePlaylist:
    """Like Plex's client: items() keeps what it last loaded until reload()."""

    def __init__(self, title, items):
        self.title = title
        self._on_server = list(items)
        self._items = list(items)

    def items(self):
        return list(self._items)

    def removeItems(self, items):
        for item in items:
            self._on_server.remove(item)

    def reload(self):
        self._items = list(self._on_server)


class FakeSection:
    def __init__(self, tracks=()):
        self.tracks = list(tracks)
        self.calls = []

    def search(self, libtype=None, maxresults=None):
        return self.tracks[:maxresults]

    def update(self):
        self.calls.append("update")

    def emptyTrash(self):
        self.calls.append("emptyTrash")


class FakePlex:
    def __init__(self, playlists=(), section=None):
        self._playlists = list(playlists)
        self.section_obj = section or FakeSection()
        self.library = SimpleNamespace(section=lambda name: self.section_obj)

    def playlists(self):
        return list(self._playlists)

    def createPlaylist(self, title, items):
        made = FakePlaylist(title, items)
        self._playlists.append(made)
        return made


def _track(path, title="Song", artist="Artist"):
    part = SimpleNamespace(file=path)
    return SimpleNamespace(title=title, grandparentTitle=artist, originalTitle=None,
                           media=[SimpleNamespace(parts=[part])])


def _database(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE blacklist (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, title TEXT NOT NULL, "
                 "artist TEXT NOT NULL, reason TEXT, rating REAL, UNIQUE(user_id, title, artist))")
    conn.execute("CREATE TABLE deletions (id INTEGER PRIMARY KEY AUTOINCREMENT, track_id TEXT, title TEXT, "
                 "artist TEXT, score REAL, reason TEXT, archived BOOLEAN)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def library(tmp_path, monkeypatch):
    music = tmp_path / "music"
    root = music / "albums"
    album = root / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    song = album / "01 - Song.flac"
    song.write_bytes(b"audio")
    (album / "01 - Song.lrc").write_text("[00:01.00] words")
    (album / "cover.jpg").write_bytes(b"art")
    monkeypatch.setenv("MUSIC_BASE_DIR", str(music))
    monkeypatch.setattr(library_state, "LIBRARY_ROOT", str(root))
    monkeypatch.setattr(library_state, "unmark", lambda path: None)
    db = _database(str(tmp_path / "data" / "user1" / "music_ai.db"))
    monkeypatch.setattr(library_review, "user_databases", lambda: {"user1": db})
    monkeypatch.setattr(library_review, "_tags", lambda path: ("Song", "Artist"))
    monkeypatch.setattr(delete_playlist, "_lidarr", lambda: None)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return SimpleNamespace(root=str(root), album=album, song=str(song), db=db)


def test_every_song_in_the_playlist_is_deleted_and_blacklisted(library):
    track = _track(library.song)
    playlist = FakePlaylist("Delete", [track])
    plex = FakePlex([playlist])

    assert delete_playlist.run(plex, "Music") == ["Artist - Song"]

    assert not os.path.exists(library.song)
    assert not library.album.exists()                      # only artwork was left
    playlist.reload()
    assert playlist.items() == []
    assert plex.section_obj.calls == ["update", "emptyTrash"]
    conn = sqlite3.connect(library.db)
    assert conn.execute("SELECT user_id, title, artist, reason FROM blacklist").fetchall() == [
        ("user1", "Song", "Artist", delete_playlist.REASON)]
    assert conn.execute("SELECT COUNT(*) FROM deletions").fetchone() == (1,)
    with open(os.path.join(os.environ["DATA_DIR"], "admin_notices.json")) as fh:
        notice = json.load(fh)[delete_playlist.NOTIFICATION_ID]
    assert "Artist - Song" in notice["message"] and not notice["resolved"]


def test_lidarr_deletes_its_own_file_and_the_rest_is_cleaned_up(library, monkeypatch):
    asked = []

    class Lidarr:
        def forget_track_file(self, path):
            asked.append(path)
            os.remove(path)                                # Lidarr deletes its file itself
            return "Album"

    monkeypatch.setattr(delete_playlist, "_lidarr", lambda: Lidarr())
    playlist = FakePlaylist("Delete", [_track(library.song)])

    assert delete_playlist.run(FakePlex([playlist]), "Music") == ["Artist - Song"]
    assert asked == [library.song]
    assert not os.path.exists(library.song[:-5] + ".lrc")
    assert not library.album.exists()


def test_a_file_the_container_cannot_reach_stays_in_the_playlist(library, tmp_path):
    outside = tmp_path / "elsewhere" / "song.flac"
    outside.parent.mkdir()
    outside.write_bytes(b"audio")
    track = _track(str(outside))
    playlist = FakePlaylist("Delete", [track])

    assert delete_playlist.run(FakePlex([playlist]), "Music") == []
    assert outside.exists()
    assert playlist.items() == [track]
    assert sqlite3.connect(library.db).execute("SELECT COUNT(*) FROM blacklist").fetchone() == (0,)


def test_a_song_already_gone_leaves_the_playlist(library):
    os.remove(library.song)
    playlist = FakePlaylist("Delete", [_track(library.song)])
    assert delete_playlist.run(FakePlex([playlist]), "Music") == ["Artist - Song"]
    playlist.reload()
    assert playlist.items() == []


def test_a_new_fix_metadata_playlist_holds_nothing_to_fix():
    import fix_metadata
    song = object()
    plex = FakePlex([], FakeSection([song]))
    made = fix_metadata._get_or_create_fix_playlist(plex, "Music")
    assert made.items() == []


def test_dry_run_deletes_nothing(library):
    playlist = FakePlaylist("Delete", [_track(library.song)])
    assert delete_playlist.run(FakePlex([playlist]), "Music", dry_run=True) == ["Artist - Song"]
    assert os.path.exists(library.song) and len(playlist.items()) == 1


def test_a_missing_playlist_is_created_empty(library, monkeypatch):
    monkeypatch.setenv("DELETE_PLAYLIST_NAME", "Remove these")
    section = FakeSection([_track(library.song)])
    plex = FakePlex([], section)

    assert delete_playlist.run(plex, "Music") == []
    assert [(p.title, p.items()) for p in plex.playlists()] == [("Remove these", [])]
    assert os.path.exists(library.song)                    # the song it was made with stays
    assert delete_playlist.run(plex, "Music") == []        # and the next pass finds it empty
    assert os.path.exists(library.song)


def test_the_daily_pass_runs_it_once_per_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    runs = []
    monkeypatch.setattr(delete_playlist, "run", lambda plex, section: runs.append(section) or [])

    fix_metadata_daemon.delete_playlist_if_due(object(), "Music", 86400)
    fix_metadata_daemon.delete_playlist_if_due(object(), "Music", 86400)
    assert runs == ["Music"]

    yesterday = datetime.now(timezone.utc) - timedelta(days=1, minutes=1)
    fix_metadata_daemon.write_last_run(fix_metadata_daemon.DELETE_KEY, yesterday)
    fix_metadata_daemon.delete_playlist_if_due(object(), "Music", 86400)
    assert runs == ["Music", "Music"]


def test_lidarr_forgets_the_file_and_stops_wanting_its_album(monkeypatch):
    import lidarr_catalog

    catalog = lidarr_catalog.LidarrCatalog("http://lidarr:8686", "key", path_map=("/data/music", "/music"))
    catalog.albums = [lidarr_catalog.LidarrAlbum(id=7, artist_id=3, artist_name="Artist", artist_path="",
                                                 title="Album", year=2020, release_group_id="",
                                                 monitored=True)]
    catalog._artists_with_files = [3]
    monkeypatch.setattr(catalog, "_get", lambda path, **params: [
        {"id": 42, "albumId": 7, "path": "/data/music/albums/Artist/Album (2020)/01 - Song.flac"}])
    sent = []

    class Reply:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(lidarr_catalog.requests, "delete", lambda url, **kw: sent.append(("DELETE", url)) or Reply())
    monkeypatch.setattr(lidarr_catalog.requests, "put",
                        lambda url, **kw: sent.append(("PUT", url, kw["json"])) or Reply())

    assert catalog.forget_track_file("/music/albums/Artist/Album (2020)/02 - Other.flac") is None
    assert catalog.forget_track_file("/music/albums/Artist/Album (2020)/01 - Song.flac") == "Album"
    assert sent == [("DELETE", "http://lidarr:8686/api/v1/trackfile/42"),
                    ("PUT", "http://lidarr:8686/api/v1/album/monitor", {"albumIds": [7], "monitored": False})]
    assert catalog.albums[0].monitored is False
