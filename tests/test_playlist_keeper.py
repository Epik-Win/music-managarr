"""Songs keep their places in Plex playlists when their files move, and
playlist songs Plex hadn't scanned yet are added once it has."""
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import fix_metadata_daemon
import library_state
import playlist_keeper


class Track:
    def __init__(self, key, path, title=None):
        self.ratingKey, self.title = key, title or os.path.basename(path)
        self.media = [SimpleNamespace(parts=[SimpleNamespace(file=path)])]


class Playlist:
    playlistType, smart = "audio", False

    def __init__(self, title, items):
        self.title, self._items = title, list(items)

    def items(self):
        return list(self._items)

    def addItems(self, items):
        self._items += [i for i in items if i not in self._items]

    def removeItems(self, items):
        self._items = [i for i in self._items if i not in items]

    def reload(self):
        pass

    def moveItem(self, item, after=None):
        self._items.remove(item)
        self._items.insert(self._items.index(after) + 1 if after is not None else 0, item)


class Server:
    def __init__(self, playlists=()):
        self._playlists = list(playlists)

    def playlists(self):
        return list(self._playlists)

    def createPlaylist(self, title, items):
        made = Playlist(title, items)
        self._playlists.append(made)
        return made


class Section:
    def __init__(self, tracks=()):
        self.tracks, self.scans = list(tracks), 0

    def searchTracks(self):
        return list(self.tracks)

    def update(self):
        self.scans += 1


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MUSIC_BASE_DIR", "/music")
    monkeypatch.setattr(playlist_keeper, "LIST_EVERY", 0)
    monkeypatch.setattr(playlist_keeper, "_last_listed", 0.0)
    return tmp_path


def _paths(playlist):
    return [t.media[0].parts[0].file for t in playlist.items()]


def test_a_moved_song_goes_back_to_its_place():
    a, b, c = (Track(i, f"/music/albums/X/A/{n}.mp3") for i, n in enumerate("abc"))
    mix = Playlist("Mix", [a, b, c])
    server = Server([mix, Playlist("Fix Metadata", [b])])
    assert playlist_keeper.snapshot({"admin": server}) == 1        # MusicAI's own playlists are not noted

    library_state.moved(b.media[0].parts[0].file, "/music/albums/Y/B/b.mp3")
    mix.removeItems([b])                                           # Plex empties its trash
    new_b = Track(9, "/music/albums/Y/B/b.mp3")

    section = Section([a, c])
    assert playlist_keeper.settle(lambda user: server, section) == 0    # not scanned yet: it waits
    section.tracks.append(new_b)
    assert playlist_keeper.settle(lambda user: server, section) == 1
    assert _paths(mix) == ["/music/albums/X/A/a.mp3", "/music/albums/Y/B/b.mp3", "/music/albums/X/A/c.mp3"]
    assert playlist_keeper.settle(lambda user: server, section) == 0    # done once


def test_songs_that_moved_one_after_the_other_keep_their_order():
    a, b, c = (Track(i, f"/music/albums/X/A/{n}.mp3") for i, n in enumerate("abc"))
    mix = Playlist("Mix", [a, b, c])
    server = Server([mix])
    playlist_keeper.snapshot({"admin": server})

    library_state.moved("/music/albums/X/A/c.mp3", "/music/albums/Z/c.mp3")   # c first, then b
    library_state.moved("/music/albums/X/A/b.mp3", "/music/albums/Z/b.mp3")
    library_state.moved("/music/albums/Z/b.mp3", "/music/albums/W/b.mp3")      # b again: one move
    mix.removeItems([b, c])
    section = Section([a, Track(7, "/music/albums/Z/c.mp3"), Track(8, "/music/albums/W/b.mp3")])

    assert playlist_keeper.settle(lambda user: server, section) == 2
    assert _paths(mix) == ["/music/albums/X/A/a.mp3", "/music/albums/W/b.mp3", "/music/albums/Z/c.mp3"]


def test_a_song_plex_never_shows_is_given_up_after_a_few_days():
    a = Track(1, "/music/albums/X/A/a.mp3")
    server = Server([Playlist("Mix", [a])])
    playlist_keeper.snapshot({"admin": server})
    library_state.moved("/music/albums/X/A/a.mp3", "/music/albums/Y/a.mp3")
    section = Section([])
    playlist_keeper.settle(lambda user: server, section)
    later = time.time() + playlist_keeper.KEEP_DAYS * 86400 + 60
    assert playlist_keeper.settle(lambda user: server, section, now=later) == 0
    section.tracks.append(Track(2, "/music/albums/Y/a.mp3"))
    assert playlist_keeper.settle(lambda user: server, section) == 0


def test_a_playlist_from_downloads_gets_its_late_songs_in_order():
    a, c = Track(1, "/music/albums/X/a.mp3"), Track(3, "/music/albums/X/c.mp3")
    section, server = Section([a, c]), Server()
    files = ["/music/albums/X/a.mp3", "/music/albums/X/b.mp3", "/music/albums/X/c.mp3"]

    made = playlist_keeper.make_playlist(server, section, "Soundtrack", files, "admin", wait=0)
    assert section.scans == 1
    assert _paths(made) == ["/music/albums/X/a.mp3", "/music/albums/X/c.mp3"]

    section.tracks.append(Track(2, "/music/albums/X/b.mp3"))
    assert playlist_keeper.settle(lambda user: server, section) == 1
    assert _paths(made) == files


def test_a_playlist_plex_showed_none_of_is_made_later():
    section, server = Section([]), Server()
    assert playlist_keeper.make_playlist(server, section, "Soundtrack", ["/music/albums/X/a.mp3",
                                                                       "/music/albums/X/b.mp3"], "admin", wait=0) is None
    section.tracks += [Track(1, "/music/albums/X/a.mp3"), Track(2, "/music/albums/X/b.mp3")]
    assert playlist_keeper.settle(lambda user: server, section) == 2
    assert [p.title for p in server.playlists()] == ["Soundtrack"]
    assert _paths(server.playlists()[0]) == ["/music/albums/X/a.mp3", "/music/albums/X/b.mp3"]


def test_a_playlist_deleted_since_is_not_made_again():
    a = Track(1, "/music/albums/X/a.mp3")
    server = Server([Playlist("Mix", [a])])
    playlist_keeper.snapshot({"admin": server})
    server._playlists = []
    library_state.moved("/music/albums/X/a.mp3", "/music/albums/Y/a.mp3")
    assert playlist_keeper.settle(lambda user: server, Section([Track(2, "/music/albums/Y/a.mp3")])) == 0
    assert server.playlists() == []


def test_each_user_s_playlists_are_kept_with_their_own_connection(monkeypatch):
    a = Track(1, "/music/albums/X/a.mp3")
    other = Server([Playlist("Theirs", [a])])
    section = Section([a])
    switched = []

    class Plex(Server):
        library = SimpleNamespace(section=lambda n: section)

        def switchUser(self, name):
            switched.append(name)
            return other

    plex = Plex([Playlist("Mine", [a])])
    mapping = {"admin_discord": "admin", "friend_discord": "friend"}
    monkeypatch.setattr(fix_metadata_daemon, "_work_due", lambda mapping, interval: True)
    fix_metadata_daemon.keep_playlists(plex, mapping, "Music", 86400)
    library_state.moved("/music/albums/X/a.mp3", "/music/albums/Y/a.mp3")
    for server in (plex, other):
        server.playlists()[0].removeItems([a])
    section.tracks = [Track(2, "/music/albums/Y/a.mp3")]
    monkeypatch.setattr(fix_metadata_daemon, "_work_due", lambda mapping, interval: False)
    fix_metadata_daemon.keep_playlists(plex, mapping, "Music", 86400)

    assert set(switched) == {"friend"}
    assert _paths(other.playlists()[0]) == ["/music/albums/Y/a.mp3"]
    assert _paths(plex.playlists()[0]) == ["/music/albums/Y/a.mp3"]


def test_the_admin_s_playlists_use_the_admin_connection(monkeypatch):
    a = Track(1, "/music/albums/X/a.mp3")
    admin = Server([Playlist("Mine", [a])])
    section = Section([a])

    class Plex(Server):
        library = SimpleNamespace(section=lambda n: section)

        def switchUser(self, name):
            raise AssertionError("the admin is never switched to")

    plex = Plex(admin.playlists())
    monkeypatch.setattr(fix_metadata_daemon, "_work_due", lambda mapping, interval: True)
    fix_metadata_daemon.keep_playlists(plex, {"admin_discord": "admin"}, "Music", 86400)
    library_state.moved("/music/albums/X/a.mp3", "/music/albums/Y/a.mp3")
    plex.playlists()[0].removeItems([a])
    section.tracks = [Track(2, "/music/albums/Y/a.mp3")]
    monkeypatch.setattr(fix_metadata_daemon, "_work_due", lambda mapping, interval: False)
    fix_metadata_daemon.keep_playlists(plex, {"admin_discord": "admin"}, "Music", 86400)
    assert _paths(plex.playlists()[0]) == ["/music/albums/Y/a.mp3"]
