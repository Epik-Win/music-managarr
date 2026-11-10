"""one_star_watcher: admin's exact one-star rating deletes the track right away."""
import sqlite3
from types import SimpleNamespace

import one_star_watcher as w


def _track(key, artist, title, rating, file_path):
    part = SimpleNamespace(file=file_path)
    return SimpleNamespace(ratingKey=key, grandparentTitle=artist, originalTitle='', title=title,
                           userRating=rating, media=[SimpleNamespace(parts=[part])])


class FakeSection:
    def __init__(self, tracks):
        self.tracks = tracks
        self.updated = self.trash_emptied = 0

    def searchTracks(self, filters=None):
        assert filters == {'userRating': 2.0}
        return [t for t in self.tracks if t.userRating == 2.0]

    def update(self):
        self.updated += 1

    def emptyTrash(self):
        self.trash_emptied += 1


def test_only_exact_one_star_tracks_are_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('MUSIC_BASE_DIR', str(tmp_path / 'music'))
    monkeypatch.setenv('HOST_MUSIC_BASE_PATH', str(tmp_path))
    monkeypatch.setattr(w.time, 'sleep', lambda s: None)

    album = tmp_path / 'music' / 'albums' / 'Artist' / 'Album'
    album.mkdir(parents=True)
    one = album / 'One Star.mp3'; one.write_bytes(b'x')
    half = album / 'Half Star.mp3'; half.write_bytes(b'x')
    none = album / 'Unrated.mp3'; none.write_bytes(b'x')
    (album / 'cover.jpg').write_bytes(b'x')

    section = FakeSection([
        _track('1', 'Artist', 'One Star', 2.0, str(one)),
        _track('2', 'Artist', 'Half Star', 1.0, str(half)),
        _track('3', 'Artist', 'Unrated', None, str(none)),
    ])
    db_paths = [str(tmp_path / 'admin' / 'music_ai.db'), str(tmp_path / 'other' / 'music_ai.db')]
    handled = set()

    assert w.process_once(section, 'admin', db_paths, handled) == 1
    assert not one.exists() and half.exists() and none.exists()
    assert handled == {'1'}
    assert section.updated == 1 and section.trash_emptied == 1

    # Logged in every user's history, but nothing is blacklisted: a one-star
    # rating is easy to give by mistake.
    for db in db_paths:
        conn = sqlite3.connect(db)
        assert conn.execute('SELECT title FROM deletions').fetchall() == [('One Star',)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'blacklist'").fetchall() == []

    # Second pass: nothing new, Plex not touched again, no duplicate rows.
    assert w.process_once(section, 'admin', db_paths, handled) == 0
    assert section.updated == 1


def test_album_folder_removed_when_no_audio_left(tmp_path, monkeypatch):
    monkeypatch.setenv('MUSIC_BASE_DIR', str(tmp_path / 'music'))
    monkeypatch.setenv('HOST_MUSIC_BASE_PATH', str(tmp_path))
    album = tmp_path / 'music' / 'albums' / 'Artist' / 'Single'
    album.mkdir(parents=True)
    song = album / 'Song.mp3'; song.write_bytes(b'x')
    (album / 'cover.jpg').write_bytes(b'x')
    assert w.delete_files([str(song)]) == 1
    assert not album.exists()


def test_a_one_star_deletion_does_not_stop_the_song_coming_back(tmp_path, monkeypatch):
    from downloader import Downloader
    db = tmp_path / 'u' / 'music_ai.db'
    w.record_deletion([str(db)], 'Artist', 'One Star', '1')
    monkeypatch.setenv('DB_PATH', str(db))
    d = Downloader.__new__(Downloader)
    assert d._is_track_blacklisted('Artist', 'One Star') is False
