"""Lyrics files from LRCLIB, written next to the songs."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

import fix_metadata_daemon
import library_state
import lyrics_files

SYNCED = "[00:01.00] First line\n[00:04.00] Second line"
PLAIN = "First line\nSecond line"


class Reply:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeLrclib:
    """Answers /get from `exact` and /search from `found`, recording the calls."""

    def __init__(self, exact=None, found=(), down=False):
        self.exact, self.found, self.down, self.calls = exact, list(found), down, []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[1], dict(params or {})))
        if self.down:
            raise requests.ConnectionError("unreachable")
        if url.endswith("/get"):
            return Reply(200, self.exact) if self.exact else Reply(404, {"message": "not found"})
        return Reply(200, self.found)


SONGS = {}


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "albums"
    root.mkdir()
    monkeypatch.setattr(library_state, "LIBRARY_ROOT", str(root))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LYRICS_ENABLED", "true")
    SONGS.clear()
    monkeypatch.setattr(lyrics_files, "_song", lambda path: SONGS.get(os.path.basename(path)))

    def song(name, artist="Artist", title="Song", album="Album", length=200.0):
        folder = root / artist / f"{album} (2020)"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(b"audio")
        SONGS[name] = {"artist": artist, "title": title, "album": album, "length": length}
        return str(path)
    return song


def _record(**kw):
    record = {"trackName": "Song", "artistName": "Artist", "albumName": "Album", "duration": 200,
              "instrumental": False, "syncedLyrics": SYNCED, "plainLyrics": PLAIN}
    record.update(kw)
    return record


def _state():
    with open(os.path.join(os.environ["DATA_DIR"], "lyrics_lookups.json")) as fh:
        return json.load(fh)


def test_lrclib_s_exact_match_is_written_as_a_synced_lrc(library, monkeypatch):
    path = library("01 - Song.flac")
    lrclib = FakeLrclib(exact=_record())
    monkeypatch.setattr(lyrics_files.requests, "get", lrclib)

    assert lyrics_files.fetch_for(path) == "written"
    with open(path[:-5] + ".lrc") as fh:
        assert fh.read() == SYNCED + "\n"
    assert lrclib.calls == [("get", {"artist_name": "Artist", "track_name": "Song", "duration": 200,
                                     "album_name": "Album"})]
    assert _state() == {}


def test_a_search_result_must_be_the_same_song_and_length(library, monkeypatch):
    path = library("01 - Song (Remastered).mp3", title="Song (Remastered)", artist="Artist feat. Guest")
    lrclib = FakeLrclib(found=[
        _record(duration=230),                                     # another length
        _record(artistName="Somebody Else"),                       # another artist
        _record(trackName="Another Song"),                         # another song
        _record(duration=201, syncedLyrics=None),                  # the song, plain lyrics only
    ])
    monkeypatch.setattr(lyrics_files.requests, "get", lrclib)

    assert lyrics_files.fetch_for(path) == "written"
    assert not os.path.exists(path[:-4] + ".lrc")
    with open(path[:-4] + ".txt") as fh:
        assert fh.read() == PLAIN + "\n"
    assert lrclib.calls[1] == ("search", {"artist_name": "Artist", "track_name": "Song (Remastered)"})


def test_synced_lyrics_are_preferred_among_matches(library, monkeypatch):
    path = library("01 - Song.flac")
    monkeypatch.setattr(lyrics_files.requests, "get", FakeLrclib(found=[
        _record(duration=200, syncedLyrics=None), _record(duration=202)]))
    assert lyrics_files.fetch_for(path) == "written"
    assert os.path.exists(path[:-5] + ".lrc")


def test_an_instrumental_gets_no_file_and_is_remembered(library, monkeypatch):
    path = library("01 - Song.flac")
    monkeypatch.setattr(lyrics_files.requests, "get",
                        FakeLrclib(exact=_record(instrumental=True, syncedLyrics=None, plainLyrics=None)))
    assert lyrics_files.fetch_for(path) == "instrumental"
    assert not lyrics_files.has_lyrics(path)
    assert _state()[os.path.relpath(path, library_state._root())]["result"] == "instrumental"


def test_a_song_with_lyrics_is_not_asked_about(library, monkeypatch):
    path = library("01 - Song.flac")
    open(path[:-5] + ".txt", "w").close()
    lrclib = FakeLrclib(exact=_record())
    monkeypatch.setattr(lyrics_files.requests, "get", lrclib)
    assert lyrics_files.fetch_for(path) == "had"
    assert lrclib.calls == []


def test_lrclib_unreachable_is_not_remembered(library, monkeypatch):
    path = library("01 - Song.flac")
    monkeypatch.setattr(lyrics_files.requests, "get", FakeLrclib(down=True))
    assert lyrics_files.fetch_for(path) == "error"
    assert not os.path.exists(os.path.join(os.environ["DATA_DIR"], "lyrics_lookups.json"))


def test_the_daily_pass_asks_new_songs_first_and_retries_after_a_while(library, monkeypatch):
    old = library("01 - Old.flac", title="Old")
    new = library("02 - New.flac", title="New")
    library("03 - Has.flac", title="Has")
    open(os.path.join(os.path.dirname(old), "03 - Has.lrc"), "w").close()
    os.makedirs(os.environ["DATA_DIR"])
    with open(os.path.join(os.environ["DATA_DIR"], "lyrics_lookups.json"), "w") as fh:
        json.dump({os.path.relpath(old, library_state._root()): {"checked": time.time() - 40 * 86400,
                                                                  "result": "none"},
                   "Gone/Album (2020)/01 - Gone.flac": {"checked": time.time(), "result": "none"}}, fh)
    lrclib = FakeLrclib()
    monkeypatch.setattr(lyrics_files.requests, "get", lrclib)

    assert lyrics_files.backfill(limit=1, pause=0) == {"none": 1}
    assert lrclib.calls[0][1]["track_name"] == "New"              # never asked comes first
    assert set(_state()) == {os.path.relpath(p, library_state._root()) for p in (old, new)}

    lrclib.calls.clear()
    assert lyrics_files.backfill(pause=0) == {"none": 1}          # the old one, 40 days on
    assert lrclib.calls[0][1]["track_name"] == "Old"
    lrclib.calls.clear()
    assert lyrics_files.backfill(pause=0) == {}                   # both asked recently
    assert lrclib.calls == []


def test_the_daily_pass_stops_when_lrclib_is_down(library, monkeypatch):
    for n in range(8):
        library(f"0{n} - Song {n}.flac", title=f"Song {n}")
    monkeypatch.setattr(lyrics_files.requests, "get", FakeLrclib(down=True))
    assert lyrics_files.backfill(pause=0) == {"error": lyrics_files.ERRORS_TO_STOP}


def test_songs_given_lyrics_have_their_albums_refreshed_in_plex(library, monkeypatch):
    first = library("01 - Song.flac")
    second = library("02 - Other.flac", title="Other")
    monkeypatch.setattr(lyrics_files.requests, "get", FakeLrclib(exact=_record()))
    assert lyrics_files.backfill(pause=0) == {"written": 2}

    refreshed = []
    album = type("Album", (), {"refresh": lambda self: refreshed.append(7)})()
    plex = type("Plex", (), {"fetchItem": lambda self, key: album})()
    track = type("Track", (), {"parentRatingKey": 7})()
    monkeypatch.setattr("library_review.plex_tracks_by_path", lambda section: {first: track, second: track})

    assert lyrics_files.show_in_plex(plex, object(), pause=0) == 1        # one album, refreshed once
    assert refreshed == [7]
    assert lyrics_files.show_in_plex(plex, object(), pause=0) == 0        # nothing left to show


def test_the_metadata_daemon_runs_it_once_per_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LYRICS_ENABLED", "true")
    runs, shown = [], []
    monkeypatch.setattr(lyrics_files, "backfill", lambda: runs.append(1) or {"written": 2})
    monkeypatch.setattr(lyrics_files, "show_in_plex", lambda plex, section: shown.append(1))

    class Plex:
        library = type("L", (), {"section": staticmethod(lambda name: object())})()

    fix_metadata_daemon.lyrics_if_due(Plex(), "Music", 86400)
    fix_metadata_daemon.lyrics_if_due(Plex(), "Music", 86400)
    assert runs == [1] and shown == [1]
    fix_metadata_daemon.write_last_run(fix_metadata_daemon.LYRICS_KEY,
                                       datetime.now(timezone.utc) - timedelta(days=2))
    monkeypatch.setenv("LYRICS_ENABLED", "false")
    fix_metadata_daemon.lyrics_if_due(Plex(), "Music", 86400)
    assert runs == [1]
