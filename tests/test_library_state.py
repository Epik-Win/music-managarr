"""Every song lives in albums/; which are new or soon to go is kept as
relative symlinks outside the library, which follow a song that moves and
are dropped (never anything deleted) when a song is gone."""
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import library_state


@pytest.fixture
def library(tmp_path, monkeypatch):
    """The container's layout: /app/data/library beside /music/albums."""
    albums = tmp_path / 'music' / 'albums'
    state = tmp_path / 'app' / 'data' / 'library'
    albums.mkdir(parents=True)
    monkeypatch.setattr(library_state, 'LIBRARY_ROOT', str(albums))
    monkeypatch.setattr(library_state, 'STATE_DIR', str(state))
    return albums, state


def _song(albums, rel, data=b'audio'):
    path = albums / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def test_a_probationary_song_is_a_relative_link_dated_when_it_arrived(library):
    albums, state = library
    song = _song(albums, 'Artist/Album/Song.mp3')
    assert library_state.mark(song, library_state.PROBATION, since=1_700_000_000)
    link = state / 'probation' / 'Artist' / 'Album' / 'Song.mp3'
    assert link.is_symlink() and not os.readlink(link).startswith('/')
    assert os.path.samefile(link, song)
    assert library_state.state_of(song) == library_state.PROBATION
    assert library_state.since(song) == 1_700_000_000
    assert library_state.songs(library_state.PROBATION) == [(song, 1_700_000_000)]


def test_links_resolve_from_another_mount_root(library, tmp_path):
    """The host sees /srv/media/app/data and /srv/media/music where the
    container sees /app/data and /music: relative links work in both."""
    albums, state = library
    song = _song(albums, 'A/B/c.flac')
    library_state.mark(song, library_state.SOON_TO_GO)
    moved_root = tmp_path.parent / (tmp_path.name + '-host')
    os.rename(tmp_path, moved_root)
    try:
        link = moved_root / 'app' / 'data' / 'library' / 'soon_to_go' / 'A' / 'B' / 'c.flac'
        assert link.read_bytes() == b'audio'
    finally:
        os.rename(moved_root, tmp_path)


def test_a_song_is_in_one_state_at_most(library):
    albums, state = library
    song = _song(albums, 'A/B/c.mp3')
    library_state.mark(song, library_state.PROBATION)
    library_state.mark(song, library_state.SOON_TO_GO)
    assert library_state.state_of(song) == library_state.SOON_TO_GO
    assert not (state / 'probation' / 'A').exists()            # emptied folders go too
    assert library_state.counts() == {'probation': 0, 'soon_to_go': 1}


def test_files_outside_albums_are_never_marked(library, tmp_path):
    outside = tmp_path / 'music' / 'archive' / 'x.mp3'
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b'a')
    assert not library_state.mark(str(outside), library_state.PROBATION)
    assert library_state.state_of(str(outside)) is None


def test_a_link_follows_a_song_moved_behind_its_back(library):
    """Lidarr or a CLI cleanup moved the file without telling: same inode."""
    albums, state = library
    song = _song(albums, 'Artist/Title/Song.mp3')
    library_state.mark(song, library_state.PROBATION, since=1_600_000_000)
    new_place = albums / 'Artist' / 'Album (2024)' / 'Artist - Album - 01 - Song.mp3'
    new_place.parent.mkdir(parents=True)
    os.rename(song, new_place)
    assert library_state.repair() == {'repointed': 1, 'dropped': 0}
    assert library_state.state_of(str(new_place)) == library_state.PROBATION
    assert library_state.since(str(new_place)) == 1_600_000_000
    assert not (state / 'probation' / 'Artist' / 'Title').exists()


def test_a_link_to_a_song_that_is_gone_is_dropped_and_nothing_deleted(library):
    albums, state = library
    song = _song(albums, 'A/B/c.mp3')
    keep = _song(albums, 'A/B/d.mp3')
    library_state.mark(song, library_state.SOON_TO_GO)
    os.remove(song)
    assert library_state.repair() == {'repointed': 0, 'dropped': 1}
    assert library_state.songs(library_state.SOON_TO_GO) == []
    assert os.path.exists(keep)


def test_a_reported_move_carries_the_state_and_the_lyrics(library):
    albums, state = library
    song = _song(albums, 'A/Title/Song.mp3')
    Path(song).with_suffix('.lrc').write_text('[00:01.00] la')
    library_state.mark(song, library_state.PROBATION, since=1_650_000_000)
    target = albums / 'A' / 'Album' / 'Song.mp3'
    target.parent.mkdir(parents=True)
    os.rename(song, target)
    library_state.moved(song, str(target))
    assert library_state.state_of(str(target)) == library_state.PROBATION
    assert library_state.since(str(target)) == 1_650_000_000
    assert target.with_suffix('.lrc').read_text() == '[00:01.00] la'


# -- the per-pass library review ------------------------------------------------

@pytest.fixture
def review_env(library, tmp_path, monkeypatch):
    """Two users' databases, a Plex section listing the songs, Lidarr reachable."""
    import library_review
    albums, _state = library
    data = tmp_path / 'data'
    for user in ('ann', 'bob'):
        (data / user).mkdir(parents=True)
        conn = sqlite3.connect(data / user / 'music_ai.db')
        conn.execute('CREATE TABLE track_scores (track_id TEXT PRIMARY KEY, title TEXT, artist TEXT, score REAL, rating INTEGER)')
        conn.execute('CREATE TABLE blacklist (id INTEGER PRIMARY KEY, user_id TEXT, title TEXT, artist TEXT, '
                     'reason TEXT, rating REAL, UNIQUE(user_id, title, artist))')
        conn.execute('CREATE TABLE deletions (id INTEGER PRIMARY KEY, track_id TEXT, title TEXT, artist TEXT, '
                     'score REAL, reason TEXT, archived BOOLEAN)')
        conn.commit()
        conn.close()
    monkeypatch.setattr(library_review, 'DATA_DIR', str(data))
    monkeypatch.setattr(library_review, 'lidarr_kept_paths', lambda: set())
    monkeypatch.setattr(library_review, 'pass_started', lambda: time.time() - 3600)
    monkeypatch.setattr(library_review, 'free_gb', lambda path: 100.0)
    tracks = {}

    def add(rel, scores=(), probation_since=None, added=None):
        path = _song(albums, rel)
        key = f'/library/metadata/{len(tracks) + 1}'
        added_at = datetime.fromtimestamp(added if added is not None else time.time() - 90 * 86400)
        tracks[path] = SimpleNamespace(key=key, addedAt=added_at)
        for user, score in zip(('ann', 'bob'), scores):
            conn = sqlite3.connect(data / user / 'music_ai.db')
            conn.execute('INSERT INTO track_scores (track_id, score) VALUES (?, ?)', (key, score))
            conn.commit()
            conn.close()
        if probation_since is not None:
            library_state.mark(path, library_state.PROBATION, since=probation_since)
        return path

    monkeypatch.setattr(library_review, 'plex_tracks_by_path', lambda section: dict(tracks))
    section = SimpleNamespace(update=lambda: None)
    return library_review, section, add, data


def _blacklisted(data, user):
    return [r[0] for r in sqlite3.connect(data / user / 'music_ai.db').execute('SELECT title FROM blacklist')]


def test_a_probationary_song_any_user_loves_graduates(review_env):
    review, section, add, _data = review_env
    loved = add('A/B/loved.mp3', scores=(0.1, 0.6), probation_since=time.time() - 86400)
    review.review(section, 'end')
    assert library_state.state_of(loved) is None and os.path.exists(loved)


def test_probation_ends_after_a_whole_pass(review_env):
    """A song from an earlier pass nobody plays goes; one from the pass that
    just ran waits for the next review."""
    review, section, add, data = review_env
    unplayed = add('A/B/unplayed.mp3', scores=(0.0, 0.05), probation_since=time.time() - 40 * 86400)
    Path(unplayed).with_suffix('.lrc').write_text('x')
    played = add('A/B/played.mp3', scores=(0.3, 0.0), probation_since=time.time() - 40 * 86400)
    fresh = add('A/C/fresh.mp3', scores=(0.0,), probation_since=time.time() - 60, added=time.time() - 60)
    review.review(section, 'end')
    assert not os.path.exists(unplayed) and not os.path.exists(Path(unplayed).with_suffix('.lrc'))
    assert _blacklisted(data, 'ann') == ['unplayed'] and _blacklisted(data, 'bob') == ['unplayed']
    assert library_state.state_of(played) == library_state.PROBATION
    assert library_state.state_of(fresh) == library_state.PROBATION and os.path.exists(fresh)


def test_the_pass_start_review_never_ends_probation(review_env):
    review, section, add, _data = review_env
    unplayed = add('A/B/unplayed.mp3', scores=(0.0,), probation_since=time.time() - 40 * 86400)
    review.review(section, 'start')
    assert os.path.exists(unplayed)


def test_low_disk_deletes_soon_to_go_first_then_probation_before_permanent(review_env, monkeypatch):
    review, section, add, _data = review_env
    monkeypatch.setattr(review, 'free_gb', lambda path: 5.0)
    monkeypatch.setattr(review, 'DISK_TARGET_GB', 5.0 + 11 / 1024 ** 3)       # three 5-byte songs' worth
    disliked = add('A/B/disliked.mp3', scores=(0.4,))
    library_state.mark(disliked, library_state.SOON_TO_GO)
    trial = add('A/B/trial.mp3', scores=(0.3,), probation_since=time.time() - 3 * 3600)
    permanent_low = add('A/B/permanent.mp3', scores=(0.1,))
    permanent_ok = add('A/B/kept.mp3', scores=(0.45,))
    loved = add('A/B/loved.mp3', scores=(0.9,))
    done = review.review(section, 'end')
    # trial: 0.3 − 0.3 penalty = 0.0 goes before the permanent song at 0.1
    assert done['space_deleted'] == [disliked, trial, permanent_low]
    assert os.path.exists(permanent_ok) and os.path.exists(loved)


def test_nothing_is_deleted_without_lidarr_or_with_auto_delete_off(review_env, monkeypatch):
    review, section, add, _data = review_env
    old = add('A/B/old.mp3', scores=(0.0,), probation_since=time.time() - 40 * 86400)
    monkeypatch.setattr(review, 'lidarr_kept_paths', lambda: None)
    review.review(section, 'end')
    assert os.path.exists(old)
    monkeypatch.setattr(review, 'lidarr_kept_paths', lambda: set())
    monkeypatch.setattr(review, 'ENABLE_AUTO_DELETE', False)
    review.review(section, 'end')
    assert os.path.exists(old)


def test_a_song_lidarr_keeps_leaves_probation_and_is_never_deleted(review_env, monkeypatch):
    review, section, add, _data = review_env
    song = add('Kept/Album (2020)/Kept - Album - 01 - Song.flac', scores=(0.0,),
               probation_since=time.time() - 40 * 86400)
    monkeypatch.setattr(review, 'lidarr_kept_paths', lambda: {os.path.normpath(song)})
    monkeypatch.setattr(review, 'free_gb', lambda path: 1.0)
    review.review(section, 'end')
    assert os.path.exists(song) and library_state.state_of(song) is None


# -- the per-user "Soon to go" playlist ---------------------------------------

@pytest.fixture
def analyzer(library, monkeypatch):
    import app
    albums, _state = library
    monkeypatch.setattr(app, 'ALBUM_DIR', str(albums))
    a = app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)
    a.plex = None
    a._lidarr_kept = (time.time(), set())
    return a, albums


def _plex_track(key, path, rating=None):
    return SimpleNamespace(key=f'/library/metadata/{key}', ratingKey=key, userRating=rating, skipCount=0,
                           title=str(key), media=[SimpleNamespace(parts=[SimpleNamespace(file=path)])])


def test_a_soon_to_go_song_rated_four_stars_is_rescued(analyzer):
    a, albums = analyzer
    loved, doomed = _song(albums, 'A/B/loved.mp3'), _song(albums, 'A/B/doomed.mp3')
    for p in (loved, doomed):
        library_state.mark(p, library_state.SOON_TO_GO)
    tracks = {loved: _plex_track(1, loved, rating=8.0), doomed: _plex_track(2, doomed, rating=2.0)}
    a._tracks_by_path = (time.time(), tracks)
    created = []
    a.plex = SimpleNamespace(playlists=lambda: [], createPlaylist=lambda title, items: created.append((title, items)))
    a.refresh_soon_to_go_playlist()
    assert library_state.state_of(loved) is None
    assert created == [('Soon to go', [tracks[doomed]])]
