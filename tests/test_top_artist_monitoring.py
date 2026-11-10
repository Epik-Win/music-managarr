"""Only the users' top-N artists may be monitored in Lidarr."""
import json
import sqlite3
from types import SimpleNamespace

import app


def _rotation_db(path, ids):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE lidarr_followed_artists '
                 '(artist_name TEXT, foreign_artist_id TEXT, lidarr_artist_id INTEGER)')
    conn.executemany('INSERT INTO lidarr_followed_artists VALUES (?, ?, ?)',
                     [(f'a{i}', f'f{i}', i) for i in ids])
    conn.commit()
    conn.close()


class FakeLidarr:
    def __init__(self, artists, folders):
        self.artists = artists
        self.folders = folders
        self.edits = []
        self.folder_puts = []

    def get(self, url, headers=None, timeout=None):
        body = self.folders if url.endswith('/rootfolder') else self.artists
        return SimpleNamespace(json=lambda: json.loads(json.dumps(body)), status_code=200)

    def put(self, url, headers=None, json=None, timeout=None):
        if url.endswith('/artist/editor'):
            self.edits.append(json)
        else:
            self.folder_puts.append(json)
        return SimpleNamespace(status_code=202)


def _setup(tmp_path, monkeypatch, lidarr):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    mapping = tmp_path / 'map.json'
    mapping.write_text(json.dumps({'alice_d': 'alice', 'bob_d': 'bob'}))
    monkeypatch.setenv('WIZARR_MAPPING_FILE', str(mapping))
    monkeypatch.setattr(app, 'LIDARR_URLS', ['http://lidarr'])
    monkeypatch.setattr(app, 'LIDARR_API_KEYS', ['key'])
    monkeypatch.setattr(app.requests, 'get', lidarr.get)
    monkeypatch.setattr(app.requests, 'put', lidarr.put)
    return app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)


def test_everything_outside_the_top_lists_is_unmonitored(tmp_path, monkeypatch):
    # alice follows 1 and 2, bob follows 3. Lidarr auto-added 4 and 5 monitored,
    # and 3 had been unmonitored by hand.
    _rotation_db(tmp_path / 'alice' / 'music_ai.db', [1, 2])
    _rotation_db(tmp_path / 'bob' / 'music_ai.db', [3])
    lidarr = FakeLidarr(
        artists=[{'id': i, 'monitored': i != 3} for i in (1, 2, 3, 4, 5)],
        folders=[{'id': 1, 'path': '/music/albums', 'defaultMonitorOption': 'all',
                  'defaultNewItemMonitorOption': 'all'}],
    )
    analyzer = _setup(tmp_path, monkeypatch, lidarr)

    assert analyzer.enforce_top_artist_monitoring() == 2
    off = next(e for e in lidarr.edits if e['monitored'] is False)
    on = next(e for e in lidarr.edits if e['monitored'] is True)
    assert sorted(off['artistIds']) == [4, 5] and off['monitorNewItems'] == 'none'
    assert on['artistIds'] == [3]
    # New artists found in the root folder are then added unmonitored.
    assert lidarr.folder_puts[0]['defaultMonitorOption'] == 'none'
    assert lidarr.folder_puts[0]['defaultNewItemMonitorOption'] == 'none'


def test_nothing_changes_when_already_compliant(tmp_path, monkeypatch):
    _rotation_db(tmp_path / 'alice' / 'music_ai.db', [1])
    lidarr = FakeLidarr(
        artists=[{'id': 1, 'monitored': True}, {'id': 2, 'monitored': False}],
        folders=[{'id': 1, 'defaultMonitorOption': 'none', 'defaultNewItemMonitorOption': 'none'}],
    )
    analyzer = _setup(tmp_path, monkeypatch, lidarr)
    assert analyzer.enforce_top_artist_monitoring() == 0
    assert lidarr.edits == [] and lidarr.folder_puts == []


def test_an_unreadable_top_list_blocks_enforcement(tmp_path, monkeypatch):
    # A broken database must not be read as "no top artists" and unmonitor all.
    bad = tmp_path / 'alice' / 'music_ai.db'
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b'not a sqlite database' * 50)
    lidarr = FakeLidarr(artists=[{'id': 1, 'monitored': True}], folders=[])
    analyzer = _setup(tmp_path, monkeypatch, lidarr)
    assert analyzer.enforce_top_artist_monitoring() is None
    assert lidarr.edits == []


def test_user_without_a_rotation_table_is_simply_empty(tmp_path, monkeypatch):
    (tmp_path / 'bob').mkdir()
    sqlite3.connect(tmp_path / 'bob' / 'music_ai.db').close()
    _rotation_db(tmp_path / 'alice' / 'music_ai.db', [1])
    lidarr = FakeLidarr(artists=[{'id': 1, 'monitored': True}, {'id': 9, 'monitored': True}],
                        folders=[])
    analyzer = _setup(tmp_path, monkeypatch, lidarr)
    assert analyzer.enforce_top_artist_monitoring() == 1


def test_lidarr_album_path_never_follows_or_searches_a_whole_artist():
    from downloader import LidarrClient
    client = LidarrClient.__new__(LidarrClient)
    sent = []
    client._request = lambda path, method='GET', params=None, json_payload=None: (
        sent.append((path, json_payload)) or ({'id': 42} if path == '/api/v1/artist' and method == 'POST' else [])
    )
    client._add_artist_to_library({'artistName': 'X', 'foreignArtistId': 'abc'})
    payload = sent[-1][1]
    assert payload['monitored'] is False
    assert payload['addOptions'] == {'monitor': 'none', 'searchForMissingAlbums': False}

    # No matching album: no artist-wide search is issued.
    sent.clear()
    client._ensure_artist_in_library = lambda artist: 7
    assert client.download_album('X', 'Missing Album') is False
    assert not any(path == '/api/v1/command' for path, _ in sent)


def test_one_lidarr_follows_every_users_top_artists():
    from lidarr_catalog import lidarr_for_user
    one = [('http://lidarr', 'key')]
    assert lidarr_for_user(0, one) == lidarr_for_user(3, one) == one[0]
    two = [('http://a', 'ka'), ('http://b', 'kb')]
    assert lidarr_for_user(1, two) == two[1] and lidarr_for_user(3, two) == two[0]
    assert lidarr_for_user(0, []) is None


def test_an_artist_another_user_follows_stays_monitored_when_this_user_drops_it(tmp_path, monkeypatch):
    lidarr = FakeLidarr(artists=[], folders=[])
    analyzer = _setup(tmp_path, monkeypatch, lidarr)
    _rotation_db(tmp_path / 'alice' / 'music_ai.db', [1, 2])        # alice follows a1, a2
    _rotation_db(tmp_path / 'bob' / 'music_ai.db', [1])             # bob follows a1 too
    monkeypatch.setattr(app, 'DB_PATH', str(tmp_path / 'alice' / 'music_ai.db'))
    monkeypatch.setattr(app, 'APP_USER_NAME', 'alice')
    analyzer.get_recency_weighted_top_artists = lambda **kw: [('a3', 9, 0, 0.9, 0)]
    analyzer._lidarr_find_artist = lambda name, url, headers: None
    unmonitored = []
    analyzer._lidarr_unmonitor_artist = lambda name, lid, fid, url, headers: unmonitored.append(name) or True
    stats = analyzer.sync_top_artists_to_lidarr(top_n=1, user_index=0)
    assert unmonitored == ['a2']                                     # a1 stays monitored for bob
    assert stats['evicted'] == 2                                     # both leave alice's rotation
    left = [r[0] for r in sqlite3.connect(tmp_path / 'alice' / 'music_ai.db').execute(
        'SELECT artist_name FROM lidarr_followed_artists')]
    assert left == []
