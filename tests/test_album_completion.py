import sqlite3
from types import SimpleNamespace

import pytest

import app


class _Deezer:
    """api.deezer.com: an album search, an artist's albums and track lists."""

    def __init__(self, search, discography, tracks):
        self.search, self.discography, self.tracks, self.calls = search, discography, tracks, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url.endswith('/search/album'):
            data = self.search
        elif '/artist/' in url:
            data = self.discography
        else:
            data = [{'title': t} for t in self.tracks[int(url.split('/album/')[1].split('/')[0])]]
        return SimpleNamespace(json=lambda: {'data': data})


LUPE = {'id': 7, 'name': 'Lupe Fiasco'}
STANDARD = ['Intro', 'Real (feat. Sarah Green)', 'Just Might Be OK', 'Kick, Push', 'I Gotcha', 'The Cool']
DELUXE = STANDARD + ['Bonus 1', 'Bonus 2']
SEQUEL = ['Ital (Roses)', 'Around My Way (Freedom Ain\'t Free)', 'The Cool']


@pytest.fixture
def deezer(monkeypatch):
    fake = _Deezer(
        search=[{'id': 1, 'title': "Lupe Fiasco's Food & Liquor (Deluxe Edition)", 'artist': LUPE, 'record_type': 'album'},
                {'id': 2, 'title': 'Food & Liquor II: The Great American Rap Album, Pt. 1', 'artist': LUPE,
                 'record_type': 'album'},
                {'id': 9, 'title': "Lupe Fiasco's Food & Liquor", 'artist': {'id': 99, 'name': 'A Cover Band'},
                 'record_type': 'album'}],
        discography=[{'id': 3, 'title': "Lupe Fiasco's Food & Liquor", 'record_type': 'album'}],
        tracks={1: DELUXE, 2: SEQUEL, 3: STANDARD, 9: STANDARD})
    monkeypatch.setattr(app.requests, 'get', fake.get)
    return fake


class TestTrackList:
    def test_the_standard_edition_of_the_same_album_is_taken(self, deezer):
        here = ['Just Might Be OK', 'I Gotcha', 'The Cool']
        assert app._deezer_album_tracklist('Lupe Fiasco', "Lupe Fiasco’s Food & Liquor", here) == STANDARD
        assert all('/album/9/' not in c for c in deezer.calls)                  # another artist's release

    def test_a_release_without_the_songs_here_is_not_the_album(self, deezer):
        assert app._deezer_album_tracklist('Lupe Fiasco', "Lupe Fiasco’s Food & Liquor",
                                           ['Something Else', 'Another One', 'The Cool']) == []

    def test_edition_words_after_the_title_are_the_same_album(self):
        assert app._same_album_title('DAMN. COLLECTORS EDITION.', 'DAMN.')
        assert app._same_album_title('American Idiot (20th Anniversary Deluxe Edition)', 'American Idiot')
        assert not app._same_album_title('Food & Liquor II: The Great American Rap Album, Pt. 1', 'Food & Liquor')
        assert not app._same_album_title('American Idiot - The Original Broadway Cast Recording', 'American Idiot')


def _scores(tmp_path, rows):
    db = tmp_path / 'music_ai.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE track_scores (track_id TEXT, title TEXT, artist TEXT, album TEXT, album_key TEXT, '
                 'score REAL, rating INTEGER)')
    conn.executemany('INSERT INTO track_scores VALUES (?, ?, ?, ?, ?, ?, ?)', rows)
    conn.commit()
    conn.close()
    return str(db)


def _analyzer(albums):
    analyzer = app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)
    analyzer.plex = SimpleNamespace(fetchItem=lambda key: albums[key])
    return analyzer


def _album(artist, title, tracks):
    return SimpleNamespace(parentTitle=artist, title=title,
                           tracks=lambda: [SimpleNamespace(title=t) for t in tracks])


class TestWhichAlbums:
    def test_only_albums_with_enough_loved_songs(self, tmp_path, monkeypatch, deezer):
        here = ['Just Might Be OK', 'I Gotcha', 'The Cool']
        rows = [(f'/t/{i}', t, 'Lupe Fiasco', 'F&L', '/a/1', 0.6 if i else 0.2, 8 if i == 0 else 0)
                for i, t in enumerate(here)]                                     # 2 by score + 1 by 4★ rating
        rows += [('/t/9', 'Song', 'Someone', 'Liked Once', '/a/2', 0.9, 0),       # one loved song is not enough
                 ('/t/10', 'Other', 'Someone', 'Liked Once', '/a/2', 0.3, 6)]
        monkeypatch.setattr(app, 'DB_PATH', _scores(tmp_path, rows))
        analyzer = _analyzer({'/a/1': _album('Lupe Fiasco', "Lupe Fiasco’s Food & Liquor", here),
                              '/a/2': _album('Someone', 'Liked Once', ['Song', 'Other'])})
        [album] = analyzer.find_albums_to_complete()
        assert album == {'artist': 'Lupe Fiasco', 'album': "Lupe Fiasco’s Food & Liquor", 'loved': 3,
                         'missing': ['Intro', 'Real (feat. Sarah Green)', 'Kick, Push']}

    def test_compilations_and_huge_gaps_are_left_alone(self, tmp_path, monkeypatch, deezer):
        rows = [(f'/t/{i}', t, 'Various Artists', 'Hits', '/a/1', 0.9, 0) for i, t in enumerate(['A', 'B', 'C'])]
        monkeypatch.setattr(app, 'DB_PATH', _scores(tmp_path, rows))
        assert _analyzer({'/a/1': _album('Various Artists', 'Hits', ['A', 'B', 'C'])}).find_albums_to_complete() == []
        here = ['Just Might Be OK', 'I Gotcha', 'The Cool']
        rows = [(f'/t/{i}', t, 'Lupe Fiasco', 'F&L', '/a/1', 0.9, 0) for i, t in enumerate(here)]
        monkeypatch.setattr(app, 'DB_PATH', _scores_fresh(tmp_path, rows))
        monkeypatch.setattr(app, 'ALBUM_COMPLETION_MAX_TRACKS', 2)
        assert _analyzer({'/a/1': _album('Lupe Fiasco', "Lupe Fiasco’s Food & Liquor", here)}
                         ).find_albums_to_complete() == []


def _scores_fresh(tmp_path, rows):
    sub = tmp_path / 'second'
    sub.mkdir()
    return _scores(sub, rows)


def test_the_stage_downloads_each_missing_song_into_its_album(monkeypatch):
    calls = []
    analyzer = SimpleNamespace(
        find_albums_to_complete=lambda: [{'artist': 'Kendrick Lamar', 'album': 'DAMN.', 'missing': ['FEAR.'],
                                          'loved': 5},
                                         {'artist': 'Eminem', 'album': 'Recovery', 'missing': ['Untitled', 'X'],
                                          'loved': 3}],
        download_recommended_music=lambda recs, **kw: calls.append((recs, kw)) or recs)
    monkeypatch.setattr(app, '_push_cycle_status', lambda *a, **k: None)
    stats = {'downloads': {'album_completion': 0}}
    app._stage_album_completion(analyzer, stats)
    assert stats['downloads']['album_completion'] == 3
    recs, kw = calls[1]
    assert [(r['artist'], r['title'], r['album'], r['type'], r['confidence']) for r in recs] == [
        ('Eminem', 'Untitled', 'Recovery', 'song', 1.0), ('Eminem', 'X', 'Recovery', 'song', 1.0)]
    assert kw == {'preference_confidence': 1.0, 'playlist': 'similar', 'limit': 2}  # not cut at 10, not filtered out
    assert 'target_dir' not in kw                                              # MusicAI's own picks: on probation


def test_trending_skips_artists_the_user_clearly_dislikes(tmp_path, monkeypatch):
    # A chart song by an artist this user rated half a star, with 25 of the
    # artist's songs in the library never played.
    import time
    db = tmp_path / 'scores.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE track_scores (artist TEXT, rating INTEGER, view_count INTEGER, date_added INTEGER)')
    month_ago = int(time.time()) - 40 * 86400
    rows = [('Tate McRae', 1, 0, month_ago)] + [('Tate McRae', 0, 0, month_ago)] * 3          # rated ½★
    rows += [('Jadakiss', 0, 0, month_ago)] * 5                                                   # 5 songs, never played
    rows += [('New Artist', 0, 0, int(time.time()))] * 5                                          # just arrived
    rows += [('Liked Once', 2, 0, month_ago), ('Liked Once', 6, 0, month_ago)]                    # a 3★ rescues
    rows += [('Played', 0, 3, month_ago)] + [('Played', 0, 0, month_ago)] * 5
    conn.executemany('INSERT INTO track_scores VALUES (?, ?, ?, ?)', rows)
    conn.commit()
    conn.close()
    monkeypatch.setattr(app, 'DB_PATH', str(db))
    analyzer = app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)
    assert analyzer.disliked_artists() == {'tatemcrae', 'jadakiss'}
    picks = [{'artist': 'Tate McRae', 'title': 'Housewife'}, {'artist': 'Tate McRae, Khalid', 'title': 'x'},
             {'artist': 'KAROL G', 'title': 'BbY WOW'}, {'artist': 'New Artist', 'title': 'y'}]
    kept = analyzer.without_disliked_artists(picks, 'Trending')
    assert [p['artist'] for p in kept] == ['KAROL G', 'New Artist']
