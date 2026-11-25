"""A YouTube playlist is read whole: videos that can't be watched any more are
left out, the others keep their order, and a long playlist gets the time it needs."""
import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import link_extractor as le

PLAYLIST = 'https://www.youtube.com/playlist?list=PLexample'


def _video(video_id, title, uploader='Some Channel', **more):
    return {'id': video_id, 'title': title, 'uploader': uploader, 'channel': uploader, 'duration': 200, **more}


class FakeYouTube:
    """Answers run_ytdlp for a playlist. `failing` maps a video id to the error
    its lookup ends with; `elsewhere` holds the ones another country has."""

    def __init__(self, entries, failing=None, elsewhere=()):
        self.entries, self.failing, self.elsewhere = entries, failing or {}, elsewhere
        self.lookups, self.lock = [], threading.Lock()

    def __call__(self, cmd, timeout=30, other_countries=True):
        if '--dump-single-json' in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'title': 'Best of'}), stderr='')
        if '--flat-playlist' in cmd:
            return SimpleNamespace(returncode=0, stdout='\n'.join(map(json.dumps, self.entries)), stderr='')
        video_id = cmd[-1].split('v=')[1]
        with self.lock:
            self.lookups.append((video_id, other_countries))
        if video_id in self.failing and not (other_countries and video_id in self.elsewhere):
            return SimpleNamespace(returncode=1, stdout='', stderr=self.failing[video_id])
        entry = next(e for e in self.entries if e['id'] == video_id)
        return SimpleNamespace(returncode=0, stdout=json.dumps({**entry, 'categories': ['Music']}), stderr='')


def _read(monkeypatch, youtube):
    monkeypatch.setattr(le, 'run_ytdlp', youtube)
    monkeypatch.setattr(le, '_try_songnamesplit', lambda *a, **k: None)
    return le.extract_youtube_metadata(PLAYLIST)


def test_private_and_deleted_videos_do_not_sink_the_playlist(monkeypatch):
    gone = "ERROR: [youtube] vid00000002: Private video"
    youtube = FakeYouTube([
        _video('vid00000001', 'Number One', 'Example Composer - Topic'),
        {'id': 'vid00000002', 'title': None, 'uploader': None, 'channel': None, 'duration': None},
        {'id': 'vid00000003', 'title': '[Deleted video]', 'uploader': None, 'channel': None, 'duration': None},
        _video('vid00000004', 'Invasion', 'Example Composer - Topic'),
    ], failing={'vid00000002': gone, 'vid00000003': gone})
    result = _read(monkeypatch, youtube)
    assert result['name'] == 'Best of' and result['unavailable'] == 2
    assert [(t['artist'], t['title']) for t in result['tracks']] == [
        ('Example Composer', 'Number One'), ('Example Composer', 'Invasion')]
    assert [t['url'][-11:] for t in result['tracks']] == ['vid00000001', 'vid00000004']


def test_a_long_playlist_keeps_its_order(monkeypatch):
    youtube = FakeYouTube([_video(f'vid{i:08d}', f'Song {i}') for i in range(40)])
    result = _read(monkeypatch, youtube)
    assert [t['title'] for t in result['tracks']] == [f'Song {i}' for i in range(40)]
    assert result['unavailable'] == 0
    assert all(other_countries is False for _, other_countries in youtube.lookups)


def test_a_region_locked_video_is_looked_up_elsewhere_after_the_others(monkeypatch):
    locked = "ERROR: [youtube] vid00000001: Video unavailable in your country"
    youtube = FakeYouTube([{'id': 'vid00000001', 'title': None, 'uploader': None}] +
                          [_video(f'vid0000001{i}', f'Song {i}') for i in range(6)],
                          failing={'vid00000001': locked}, elsewhere=('vid00000001',))
    youtube.entries[0] = {'id': 'vid00000001', 'title': 'Found abroad', 'uploader': 'Some Channel'}
    result = _read(monkeypatch, youtube)
    assert youtube.lookups[-1] == ('vid00000001', True)
    assert sum(1 for _, other_countries in youtube.lookups if other_countries) == 1
    assert result['tracks'][0]['title'] == 'Found abroad' and len(result['tracks']) == 7


def test_a_failed_lookup_keeps_what_the_playlist_says(monkeypatch):
    youtube = FakeYouTube([_video('vid00000001', 'Still listed')], failing={'vid00000001': 'ERROR: timed out'})
    result = _read(monkeypatch, youtube)
    assert [t['title'] for t in result['tracks']] == ['Still listed']


def test_a_playlist_with_nothing_left_is_no_result(monkeypatch):
    youtube = FakeYouTube([{'id': 'vid00000001', 'title': None}], failing={'vid00000001': 'ERROR: Private video'})
    assert _read(monkeypatch, youtube) is None


def test_a_playlist_gets_longer_than_a_single_link(monkeypatch):
    waits = {}

    async def wait_for(future, timeout):
        waits[len(waits)] = timeout
        return await future

    monkeypatch.setattr(le.asyncio, 'wait_for', wait_for)
    monkeypatch.setattr(le, 'extract_youtube_metadata', lambda url: None)
    monkeypatch.setattr(le, '_build_variant_context', lambda result, url, source: result)
    for url in (PLAYLIST, 'https://www.youtube.com/watch?v=vid00000001'):
        asyncio.run(le.extract_link_metadata(url))
    assert waits == {0: le.PLAYLIST_TIMEOUT, 1: le.LINK_TIMEOUT}
    assert le.PLAYLIST_TIMEOUT > le.LINK_TIMEOUT


def test_a_video_with_empty_fields_is_read(monkeypatch):
    monkeypatch.setattr(le, 'run_ytdlp', lambda cmd, timeout=30: SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(
        {'title': 'Invasion', 'artist': None, 'album': None, 'uploader': None, 'channel': 'Example Composer - Topic',
         'duration': 171, 'categories': ['Music']})))
    monkeypatch.setattr(le, '_try_songnamesplit', lambda *a, **k: None)
    result = le.extract_youtube_metadata('https://www.youtube.com/watch?v=vid00000001')
    assert (result['artist'], result['title'], result['album']) == ('Example Composer', 'Invasion', '')


def test_an_official_upload_goes_by_the_artist_and_song_it_states(monkeypatch):
    # "Number One - Reprise" is the song's whole name, not "artist - song".
    youtube = FakeYouTube([_video('vid00000001', 'Number One - Reprise', 'Example Composer - Topic',
                                  artist='Example COMPOSER', track='Number One - Reprise'),
                           _video('vid00000002', 'Some Uploader - Nocturne', 'Some Uploader')])
    result = _read(monkeypatch, youtube)
    assert [(t['artist'], t['title']) for t in result['tracks']] == [
        ('Example COMPOSER', 'Number One - Reprise'), ('Some Uploader', 'Nocturne')]


def test_the_music_category_outweighs_a_word_in_the_title():
    title = 'Main Theme (Some Show Episode 6 OST) Epic Orchestral Cover'
    assert le.is_music_video({'title': title, 'categories': ['Music'], 'duration': 88})[0] is True
    assert le.is_music_video({'title': 'My show episode 6', 'categories': ['Entertainment'], 'duration': 88})[0] is False
    assert le.is_music_video({'title': 'My show episode 6', 'categories': None, 'duration': 88})[0] is False
