"""How the cycle and the daily pass use the user's taste (taste.py)."""
import sqlite3
from collections import Counter
from types import SimpleNamespace

import pytest

import taste


@pytest.fixture
def analyzer(tmp_path, monkeypatch):
    import app
    import downloader_manager
    import library_state
    db = str(tmp_path / 'music_ai.db')
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE temp_downloads (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT, track_title TEXT, '
                 'file_path TEXT, reason TEXT, confidence REAL, status TEXT, is_trending BOOLEAN, '
                 'created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.commit()
    conn.close()
    monkeypatch.setattr(app, 'DB_PATH', db)
    monkeypatch.setattr(app, 'TEMP_DOWNLOAD_DIR', str(tmp_path / 'albums'))
    monkeypatch.setattr(app, 'check_if_track_exists', lambda artist, title: (False, None, 0))
    monkeypatch.setattr(library_state, 'mark', lambda path, state, since=None: True)
    tried = []

    class Downloader:
        def download_track(self, artist, title, **kw):
            tried.append(title)
            if title.startswith('fails'):
                return {'success': False, 'error': 'not found'}
            return {'success': True, 'file_path': f"/music/albums/{artist}/{title}.mp3"}
    monkeypatch.setattr(downloader_manager, 'get_downloader', lambda owner: Downloader())
    a = app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)
    a.cycle_downloads, a.plex = [], None
    a.is_blacklisted = lambda artist, title: False
    a.tried, a.db = tried, db
    return a


def _picks(db):
    return sqlite3.connect(db).execute('SELECT artist, title, source FROM picks ORDER BY id').fetchall()


def test_downloads_stop_once_enough_came_in_and_failures_leave_no_gap(analyzer):
    recs = [{'artist': 'A', 'title': t, 'type': 'song', 'source': taste.SIMILAR_ARTIST, 'reason': 'r',
             'confidence': 0.9} for t in ('fails one', 'two', 'three', 'four')]
    done = analyzer.download_recommended_music(recs, preference_confidence=0.0, playlist='similar',
                                              limit=len(recs), max_downloads=2)
    assert [d['file_path'] for d in done] == ['/music/albums/A/two.mp3', '/music/albums/A/three.mp3']
    assert analyzer.tried == ['fails one', 'two', 'three']


def test_every_pick_with_a_source_is_written_down_and_counted_for_its_artist(analyzer):
    recs = [{'artist': 'ScHoolboy Q', 'title': 'Collard Greens', 'type': 'song', 'source': taste.SIMILAR_SONG,
             'reason': 'Like a loved song', 'confidence': 0.8},
            {'artist': 'Asked For', 'title': 'Request', 'type': 'song', 'reason': 'Discord song request from u'}]
    analyzer.download_recommended_music(recs, preference_confidence=0.0, limit=2)
    assert _picks(analyzer.db) == [('ScHoolboy Q', 'Collard Greens', taste.SIMILAR_SONG)]
    assert analyzer.picked_artists == Counter({'schoolboyq': 1})


def test_charts_pass_unchanged_while_the_taste_is_unknown(analyzer):
    tracks = [{'artist': 'Some Star', 'title': 'hit'}]
    assert analyzer.rank_for_taste(tracks, taste.TRENDING, 10) == tracks
    analyzer.taste = taste.build('u', [])
    assert analyzer.rank_for_taste(tracks, taste.TRENDING, 10) == tracks       # too few loved songs


def test_charts_keep_only_songs_that_fit_a_known_taste(analyzer, monkeypatch):
    monkeypatch.setattr(taste, 'MIN_LOVED', 1)
    analyzer.taste = taste.build('u', [taste.Song(key=str(i), artist='Kendrick Lamar', title=f"x{i}", rating=10)
                                       for i in range(5)])
    ranked = analyzer.rank_for_taste([{'artist': 'Some Star', 'title': 'hit'},
                                      {'artist': 'Kendrick Lamar', 'title': 'new single'}], taste.TRENDING, 10)
    assert [t['title'] for t in ranked] == ['new single'] and ranked[0]['source'] == taste.TRENDING


def test_the_taste_stage_picks_from_the_profile_once_it_is_known(analyzer, monkeypatch):
    import app
    monkeypatch.setattr(taste, 'MIN_LOVED', 1)
    analyzer.taste = taste.build('u', [taste.Song(key='1', artist='Kendrick Lamar', title='HUMBLE.', rating=10)])
    analyzer.get_similar_tracks_from_lastfm = lambda artist, title, limit=10: [
        {'artist': 'ScHoolboy Q', 'title': 'Collard Greens', 'confidence': 1.0, 'source': 'lastfm_similar'}]
    monkeypatch.setattr(taste, 'deezer_top_tracks', lambda name, n: [])
    monkeypatch.setattr(app, 'LAST_FM_API_KEY', '')
    old = []
    analyzer.download_from_similar_tracks = lambda **kw: old.append(kw) or 0
    stats = {'downloads': {}, 'errors': []}
    app._stage_similar_tracks(analyzer, stats)
    assert stats['downloads']['similar_tracks'] == 1 and not old
    assert analyzer.cycle_downloads[0]['playlist'] == 'similar'
    assert _picks(analyzer.db) == [('ScHoolboy Q', 'Collard Greens', taste.SIMILAR_SONG)]


def test_the_daily_pass_makes_each_users_playlist_on_their_own_account(monkeypatch):
    import fix_metadata_daemon as daemon
    import users
    monkeypatch.setattr(daemon, 'is_due', lambda key, now, interval: True)
    monkeypatch.setattr(daemon, 'write_last_run', lambda key, when: None)
    monkeypatch.setattr(users, 'plex_usernames', lambda mapping: ['admin', 'kid', 'away'])
    made = []
    monkeypatch.setattr(taste, 'refresh_for_you',
                        lambda admin, server, section, user, is_admin: made.append((server, user, is_admin)))

    class Admin:
        def switchUser(self, name):
            if name == 'away':
                raise RuntimeError('not a Home user')
            return f"{name}'s connection"
    admin = Admin()
    daemon.picked_for_you_if_due(admin, {}, 'Music', 86400)
    assert made == [(admin, 'admin', True), ("kid's connection", 'kid', False)]


def test_lastfm_similar_songs_are_used_rather_than_always_falling_back(analyzer, monkeypatch):
    import sys
    import app
    found = {'Lupe Fiasco': [SimpleNamespace(item=SimpleNamespace(artist=SimpleNamespace(name='Common'), title='Be'),
                                             match=0.9)]}

    class Network:
        def __init__(self, api_key):
            pass

        def get_track(self, artist, title):
            return SimpleNamespace(info=None, get_similar=lambda limit: found.get(artist, []))
    monkeypatch.setitem(sys.modules, 'pylast', SimpleNamespace(LastFMNetwork=Network))
    monkeypatch.setattr(app, 'LAST_FM_API_KEY', 'key')
    fallback = []
    analyzer._get_similar_tracks_from_deezer = lambda artist, title, limit: fallback.append(title) or []
    got = analyzer.get_similar_tracks_from_lastfm('Lupe Fiasco', 'Hurt Me Soul')
    assert [(r['artist'], r['title'], r['confidence'], r['source']) for r in got] == [
        ('Common', 'Be', 0.9, 'lastfm_similar')] and not fallback
    analyzer.get_similar_tracks_from_lastfm('Unknown Act', 'Nothing')
    assert fallback == ['Nothing']
