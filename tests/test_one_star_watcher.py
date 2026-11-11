"""one_star_watcher: admin's exact one-star rating deletes the track right away."""
import asyncio
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import runtime_settings

import one_star_watcher as w


SWITCHED_ON = datetime(2026, 1, 10, 12, 0, 0).timestamp()


def _track(key, artist, title, rating, file_path, rated_at=SWITCHED_ON + 30):
    part = SimpleNamespace(file=file_path)
    return SimpleNamespace(ratingKey=key, grandparentTitle=artist, originalTitle='', title=title,
                           userRating=rating, media=[SimpleNamespace(parts=[part])],
                           lastRatedAt=datetime.fromtimestamp(rated_at) if rated_at else None,
                           addedAt=datetime.fromtimestamp(SWITCHED_ON - 86400))


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

    assert w.process_once(section, 'admin', db_paths, handled, SWITCHED_ON) == 1
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
    assert w.process_once(section, 'admin', db_paths, handled, SWITCHED_ON) == 0
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


def _song(tmp_path, name):
    album = tmp_path / 'music' / 'albums' / 'Artist' / name
    album.mkdir(parents=True)
    song = album / f'{name}.mp3'
    song.write_bytes(b'x')
    return song


def test_only_ratings_given_after_deletion_was_switched_on_delete(tmp_path, monkeypatch):
    monkeypatch.setenv('MUSIC_BASE_DIR', str(tmp_path / 'music'))
    monkeypatch.setenv('HOST_MUSIC_BASE_PATH', str(tmp_path))
    monkeypatch.setattr(w.time, 'sleep', lambda s: None)
    old = _song(tmp_path, 'Rated While Off')
    same_second = _song(tmp_path, 'Rated As It Was Switched On')
    new = _song(tmp_path, 'Rated While On')
    unknown = _song(tmp_path, 'No Rating Time')
    rated_while_off = _track('1', 'Artist', 'Rated While Off', 2.0, str(old), rated_at=SWITCHED_ON - 3600)
    section = FakeSection([
        rated_while_off,
        _track('2', 'Artist', 'Rated As It Was Switched On', 2.0, str(same_second), rated_at=SWITCHED_ON + 0.4),
        _track('3', 'Artist', 'Rated While On', 2.0, str(new), rated_at=SWITCHED_ON + 120),
        _track('4', 'Artist', 'No Rating Time', 2.0, str(unknown), rated_at=None),
    ])
    handled, kept = set(), {}
    db = [str(tmp_path / 'admin' / 'music_ai.db')]

    assert w.process_once(section, 'admin', db, handled, SWITCHED_ON + 0.7, kept) == 2
    assert old.exists() and unknown.exists()
    assert not same_second.exists() and not new.exists()
    assert set(kept) == {'1', '4'} and handled == {'2', '3'}
    assert rated_while_off.userRating == 2.0          # the older rating itself is left as it is

    # Later passes keep it too; once the admin rates it one star again it goes.
    assert w.process_once(section, 'admin', db, handled, SWITCHED_ON, kept) == 0
    assert old.exists()
    rated_while_off.lastRatedAt = datetime.fromtimestamp(SWITCHED_ON + 600)
    assert w.process_once(section, 'admin', db, handled, SWITCHED_ON, kept) == 1
    assert not old.exists()


def test_switching_deletion_back_on_does_not_delete_songs_rated_while_it_was_off(tmp_path, monkeypatch):
    """Discord: on, off, a song is rated one star, on again: the song stays."""
    import discord_bot
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('MUSIC_BASE_DIR', str(tmp_path / 'music'))
    monkeypatch.setenv('HOST_MUSIC_BASE_PATH', str(tmp_path))
    monkeypatch.delenv('ONE_STAR_DELETE', raising=False)
    monkeypatch.setattr(w.time, 'sleep', lambda s: None)
    monkeypatch.setattr(discord_bot, 'DISCORD_MAIN_CHANNEL', 42)
    monkeypatch.setattr(discord_bot, '_admin_member', lambda guild: SimpleNamespace(id=1))
    clock = [SWITCHED_ON]
    monkeypatch.setattr(discord_bot.time, 'time', lambda: clock[0])
    sent = []

    class Response:
        async def send_message(self, text, ephemeral=False):
            sent.append(text)

    def switch(value):
        admin = SimpleNamespace(user=SimpleNamespace(id=1, name='admin'), guild=SimpleNamespace(),
                                response=Response(), channel=SimpleNamespace(id=42, parent_id=None))
        asyncio.run(discord_bot.one_star_delete.callback(admin, SimpleNamespace(value=value)))

    switch('on')
    assert w.enabled() and w.deleting_since() == SWITCHED_ON
    assert f'since <t:{int(SWITCHED_ON)}:f>' in sent[-1]
    clock[0] += 60
    switch('on')                                        # already on: the time it was switched on stays
    assert w.deleting_since() == SWITCHED_ON

    clock[0] += 60
    switch('off')
    assert not w.enabled() and runtime_settings.get_value(w.SINCE_SETTING) is None
    assert '**off**' in sent[-1]

    song = _song(tmp_path, 'Rated While Off')
    section = FakeSection([_track('1', 'Artist', 'Rated While Off', 2.0, str(song), rated_at=clock[0] + 60)])

    clock[0] += 3600
    switch('on')
    assert w.process_once(section, 'admin', [], set(), w.deleting_since(), {}) == 0
    assert song.exists()


def test_deletion_switched_on_outside_discord_counts_from_the_first_pass(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.delenv('ONE_STAR_DELETE', raising=False)
    monkeypatch.setattr(w.time, 'time', lambda: SWITCHED_ON)
    assert w.enabled() and runtime_settings.get_value(w.SINCE_SETTING) is None
    assert w.deleting_since() == SWITCHED_ON
    assert runtime_settings.get_value(w.SINCE_SETTING) == SWITCHED_ON

    # Switched off by hand: the watcher drops the time, so switching it back
    # on later starts a new one.
    runtime_settings.set_value('ONE_STAR_DELETE', False)
    w.forget_since()
    assert runtime_settings.get_value(w.SINCE_SETTING) is None
    runtime_settings.set_value('ONE_STAR_DELETE', True)
    w.forget_since()                                    # back on: nothing is dropped
    monkeypatch.setattr(w.time, 'time', lambda: SWITCHED_ON + 7200)
    assert w.deleting_since() == SWITCHED_ON + 7200
