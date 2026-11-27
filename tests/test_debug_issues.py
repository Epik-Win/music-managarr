"""Manual Fix songs reported to Debugarr, and the fixes it sends back."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import admin_notify
import debug_issues
import fix_metadata
import fix_metadata_daemon as fmd
import manual_fix
import metadata_repair


def track(key, artist='', album='', title='Song'):
    return SimpleNamespace(ratingKey=key, title=title, parentTitle=album, grandparentTitle=artist,
                           originalTitle='', parentThumb=None, parentRatingKey=None, duration=185000, year=None)


class Playlist:
    def __init__(self, items):
        self._items = list(items)
        self.title = manual_fix.PLAYLIST_NAME
        self.deleted = False

    def items(self):
        return list(self._items)

    def removeItems(self, items):
        keys = {i.ratingKey for i in items}
        self._items = [i for i in self._items if i.ratingKey not in keys]

    def delete(self):
        self.deleted = True


class Plex:
    machineIdentifier = 'abc123'

    def __init__(self, tracks, playlist=None):
        self.tracks = {str(t.ratingKey): t for t in tracks}
        self.playlist = playlist

    def fetchItem(self, key):
        return self.tracks[str(key)]

    def playlists(self):
        return [self.playlist] if self.playlist and not self.playlist.deleted else []


class Debugarr:
    """Records the calls MusicAI makes; answers from `replies` by path."""

    def __init__(self, replies=None, status=200):
        self.calls, self.replies, self.status = [], replies or {}, status

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.status, self.replies.get(path.split('?')[0], {'status': 'waiting_ai', 'code': 'IABCDEF'})


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('DEBUGGARR_URL', 'http://debuggarr:8095/api/notify')
    monkeypatch.setenv('DEBUGGARR_API_SECRET', 'test-secret')


@pytest.fixture
def offline_lookups(monkeypatch):
    monkeypatch.setattr(debug_issues, '_deezer', lambda meta, fp: [
        {'artist': 'Maddhouse', 'title': 'Rain', 'album': 'Rain', 'length_seconds': 184}])
    monkeypatch.setattr(debug_issues, '_lidarr', lambda meta: [])
    monkeypatch.setattr(debug_issues, '_file_tags', lambda fp: {'title': 'Unknown ♫Rain♫'})
    monkeypatch.setattr(fix_metadata, '_file_path', lambda t: '/music/Unidentified/Rain/01 Rain.mp3')
    monkeypatch.setattr(metadata_repair, 'audio_duration', lambda fp: 185.0)
    monkeypatch.setattr(metadata_repair, 'known_meta', lambda t, fp: {'artist': '', 'album': '', 'title': 'Rain'})


def record(reason='unknown artist; not found by Lidarr or audio fingerprinting', source='library scan', **extra):
    return dict({'label': 'Unknown Artist – Rain [Unknown Album]', 'reason': reason, 'source': source,
                 'heard': [{'artist': 'Maddhouse', 'title': 'Rain', 'service': 'Shazam', 'votes': 1}],
                 'notified': False}, **extra)


class TestSettings:
    def test_the_api_address_is_taken_from_any_of_its_urls(self, configured):
        assert debug_issues.base_url() == 'http://debuggarr:8095'
        assert debug_issues.enabled()

    def test_the_secret_can_come_from_debugarrs_own_env_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv('DEBUGGARR_URL', 'http://debuggarr:8095')
        monkeypatch.delenv('DEBUGGARR_API_SECRET', raising=False)
        env = tmp_path / 'debuggarr.env'
        env.write_text('DISCORD_BOT_TOKEN=x\nAPI_SECRET="from-file"\n')
        monkeypatch.setenv('DEBUGGARR_API_SECRET_FILE', str(env))
        assert debug_issues._secret() == 'from-file'
        env.write_text('raw-secret\n')
        assert debug_issues._secret() == 'raw-secret'

    def test_off_without_an_address_or_secret(self, monkeypatch):
        monkeypatch.delenv('DEBUGGARR_URL', raising=False)
        assert not debug_issues.enabled()
        assert debug_issues.waiting({'open': {}}) == []


class TestReport:
    def test_a_song_with_unknown_names_carries_its_evidence_and_fix(self, offline_lookups):
        report = debug_issues.build_report(Plex([track('42', title='Unknown ♫Rain♫')]), '42', record())
        assert report['issue_id'] == 'manual-fix-42'
        assert report['kind'] == 'metadata'
        assert [f['name'] for f in report['fix']['fields']] == ['artist', 'album', 'title']
        # The current values are what Plex shows; the file's reading is a fact.
        assert [f['current'] for f in report['fix']['fields']] == ['', '', 'Unknown ♫Rain♫']
        assert 'MusicAI reads the file as: Unknown Artist – Rain [Unknown Album]' in report['facts']
        assert report['evidence']['fingerprints'][0]['artist'] == 'Maddhouse'
        assert report['evidence']['file']['artist_folder'] == 'Unidentified'
        assert report['evidence']['deezer_by_name'][0]['title'] == 'Rain'
        assert 'Found by the daily library scan' in report['facts']
        assert 'File: Unidentified/Rain/01 Rain.mp3 (3:05)' in report['facts']
        assert any(f.startswith('Fingerprinting heard: Maddhouse – Rain (Shazam)') for f in report['facts'])
        assert report['link'].startswith('https://app.plex.tv/desktop/#!/server/abc123/details?key=')
        assert 'daily library repair' in report['attempts'][0]['method']

    def test_a_missing_cover_goes_to_the_admin_without_a_fix(self, offline_lookups):
        report = debug_issues.build_report(Plex([track('43')]), '43', record(reason='missing artwork; none found online'))
        assert report['kind'] == 'artwork'
        assert 'fix' not in report
        assert 'Edit → Poster' in report['needs_person']
        assert 'deezer_by_name' not in report['evidence']

    def test_a_wrong_recording_goes_to_the_admin_and_a_flag_names_who(self, offline_lookups):
        reason = 'the names hold but the audio is a truncated copy (75s); the true recording could not be downloaded'
        report = debug_issues.build_report(Plex([track('44')]), '44', record(reason=reason, source='flagged by sam'))
        assert report['kind'] == 'audio'
        assert 'Flagged by sam in their Fix Metadata playlist' in report['facts']

    def test_a_song_plex_no_longer_has_is_not_reported(self, offline_lookups):
        assert debug_issues.build_report(Plex([]), '45', record()) is None


class TestDeezerEvidence:
    def test_only_songs_with_a_matching_title_are_kept(self, monkeypatch):
        rows = [{'id': 1, 'title': 'Rain', 'artist': {'name': 'Maddhouse'}, 'album': {'title': 'Rain'}, 'duration': 184},
                {'id': 2, 'title': 'Purple Rain', 'artist': {'name': 'Other Band'}, 'album': {'title': 'Purple'},
                 'duration': 520},
                {'id': 3, 'title': 'Rain (Acoustic)', 'artist': {'name': 'Maddhouse'}, 'album': {'title': 'Rain'},
                 'duration': 200}]
        response = SimpleNamespace(json=lambda: {'data': rows})
        monkeypatch.setattr(debug_issues.requests, 'get', lambda *a, **k: response)
        monkeypatch.setattr(metadata_repair, 'name_candidates', lambda meta, fp: ['Rain'])
        found = debug_issues._deezer({'artist': 'Maddhouse', 'title': 'Rain'}, '/music/a/b/Rain.mp3')
        assert [f['title'] for f in found] == ['Rain', 'Rain (Acoustic)']


class TestReportOpen:
    def test_each_unreported_song_is_reported_once(self, configured, offline_lookups, monkeypatch):
        fake = Debugarr({'/api/issues/batch': {'issues': [{'issue_id': 'manual-fix-42', 'code': 'IABCDEF'}]}})
        monkeypatch.setattr(debug_issues, '_call', fake)
        state = {'open': {'42': record(), '43': record(issue='IOLD123')}}
        assert debug_issues.report_open(Plex([track('42'), track('43')]), state) == 1
        assert [i['issue_id'] for i in fake.calls[0][2]['issues']] == ['manual-fix-42']
        assert state['open']['42']['issue'] == 'IABCDEF'
        assert debug_issues.report_open(Plex([track('42')]), state) == 0
        assert len(fake.calls) == 1

    def test_the_songs_of_a_pass_go_in_one_batch(self, configured, offline_lookups, monkeypatch):
        fake = Debugarr({'/api/issues/batch': {'issues': [
            {'issue_id': 'manual-fix-42', 'code': 'IAAAAAA'}, {'issue_id': 'manual-fix-44', 'code': 'IBBBBBB'}]}})
        monkeypatch.setattr(debug_issues, '_call', fake)
        state = {'open': {'42': record(), '44': record()}}
        assert debug_issues.report_open(Plex([track('42'), track('44')]), state) == 2
        assert [(c[0], c[1]) for c in fake.calls] == [('POST', '/api/issues/batch')]
        assert fake.calls[0][2]['source'] == 'music-managarr'
        assert [state['open'][k]['issue'] for k in ('42', '44')] == ['IAAAAAA', 'IBBBBBB']

    def test_reporting_stops_while_debugarr_is_unreachable(self, configured, offline_lookups, monkeypatch):
        fake = Debugarr(status=0)
        monkeypatch.setattr(debug_issues, '_call', fake)
        state = {'open': {'42': record(), '43': record()}}
        assert debug_issues.report_open(Plex([track('42'), track('43')]), state) == 0
        assert len(fake.calls) == 1
        assert not any(r.get('issue') for r in state['open'].values())

    def test_the_thread_notice_goes_once_debugarr_has_every_song(self, configured, offline_lookups, monkeypatch):
        monkeypatch.setattr(debug_issues, '_call', Debugarr())
        resolved, notified = [], []
        monkeypatch.setattr(admin_notify, 'resolve_notification', lambda nid: resolved.append(nid) or True)
        monkeypatch.setattr(admin_notify, 'notify_admin', lambda *a: notified.append(a) or True)
        state = {'open': {'42': record()}, 'resolved_unsent': [], 'issues_to_close': []}
        manual_fix.send_pending_notifications(state, Plex([track('42')]))
        assert resolved == [manual_fix.NOTIFICATION_ID]
        assert notified == []

    def test_without_a_plex_connection_the_thread_notice_is_kept(self, configured, monkeypatch):
        notified = []
        monkeypatch.setattr(admin_notify, 'resolve_notification', lambda nid: True)
        monkeypatch.setattr(admin_notify, 'notify_admin', lambda *a: notified.append(a) or True)
        state = {'open': {'42': record()}, 'resolved_unsent': [], 'issues_to_close': []}
        manual_fix.send_pending_notifications(state)
        assert len(notified) == 1


class TestActing:
    @pytest.fixture
    def song(self, configured, monkeypatch):
        monkeypatch.setattr(fix_metadata, '_file_path', lambda t: '/music/Unidentified/Rain/01 Rain.mp3')
        monkeypatch.setattr(metadata_repair, 'audio_duration', lambda fp: 185.0)
        written = []
        monkeypatch.setattr(debug_issues, 'write', lambda t, fp, meta: (written.append(meta) or True, 'fetched', meta))
        song = track('42')
        plex = Plex([song], Playlist([song]))
        state = {'open': {'42': record(issue='IABCDEF')}, 'issues_to_close': []}
        return SimpleNamespace(plex=plex, state=state, written=written)

    @staticmethod
    def fix(by='local-ai', status='fix_ready', key='42'):
        return {'issue_id': f'manual-fix-{key}', 'status': status, 'proposed_by': by,
                'solution': {'artist': 'Maddhouse', 'album': 'Rain', 'title': 'Rain'}}

    def test_a_confirmed_ai_fix_is_written_then_waits_for_plex(self, song, monkeypatch):
        fake = Debugarr()
        monkeypatch.setattr(debug_issues, '_call', fake)
        monkeypatch.setattr(debug_issues, 'confirm', lambda meta, length: (True, 'Deezer lists it on that album'))
        assert debug_issues.act(song.plex, song.state, [self.fix()]) == 1
        assert song.written == [{'artist': 'Maddhouse', 'album': 'Rain', 'title': 'Rain'}]
        assert fake.calls == []
        applied = song.state['open']['42']['applied']
        assert applied['names'] == song.written[0] and 'Deezer lists it on that album' in applied['detail']
        assert debug_issues.act(song.plex, song.state, [self.fix()]) == 0      # not written twice
        assert len(song.written) == 1

    def test_the_fix_is_reported_once_plex_shows_it(self, song, monkeypatch):
        fake = Debugarr()
        monkeypatch.setattr(debug_issues, '_call', fake)
        names = {'artist': 'Maddhouse', 'album': 'Rain', 'title': 'Rain'}
        song.state['open']['42']['applied'] = {'names': names, 'detail': 'wrote it', 'at': debug_issues.time.time()}
        debug_issues.verify_applied(song.plex, song.state, 'Music')
        assert fake.calls == []                                   # Plex still shows the old names
        song.plex.tracks['42'].grandparentTitle, song.plex.tracks['42'].parentTitle = 'Maddhouse', 'Rain'
        song.plex.tracks['42'].title = 'Rain'
        debug_issues.verify_applied(song.plex, song.state, 'Music')
        assert fake.calls == [('POST', '/api/issues/music-managarr/manual-fix-42/result',
                               {'outcome': 'fixed', 'detail': 'wrote it'})]
        assert '42' not in song.state['open']
        assert song.plex.playlist.deleted

    def test_a_fix_plex_never_shows_is_reported_failed(self, song, monkeypatch):
        fake = Debugarr()
        monkeypatch.setattr(debug_issues, '_call', fake)
        names = {'artist': 'Example Band', 'album': 'Rain', 'title': 'Song'}
        song.state['open']['42']['applied'] = {'names': names, 'detail': 'wrote it',
                                               'at': debug_issues.time.time() - debug_issues.VERIFY_SECONDS - 1}
        debug_issues.verify_applied(song.plex, song.state, 'Music')
        body = fake.calls[-1][2]
        assert body['outcome'] == 'failed'
        assert body['detail'] == ('MusicAI wrote Example Band – Song [Rain] to the file, but 60 minutes later '
                                  'Plex still shows Unknown Artist – Song [Unknown Album]')
        assert 'applied' not in song.state['open']['42']

    def test_plexs_own_spelling_of_the_artist_counts_as_shown(self, song, monkeypatch):
        fake = Debugarr()
        monkeypatch.setattr(debug_issues, '_call', fake)
        names = {'artist': 'Northern Light Ensemble', 'album': 'Rain', 'title': 'Rain'}
        song.state['open']['42']['applied'] = {'names': names, 'detail': 'wrote it', 'at': debug_issues.time.time()}
        shown = song.plex.tracks['42']
        shown.grandparentTitle, shown.parentTitle, shown.title = 'Northern Lights Ensemble', 'Rain', 'Rain'
        debug_issues.verify_applied(song.plex, song.state, 'Music')
        assert fake.calls[-1][2] == {'outcome': 'fixed', 'detail': 'wrote it'}

    def test_a_moved_files_new_item_is_what_plex_shows(self, song, monkeypatch):
        fake = Debugarr()
        monkeypatch.setattr(debug_issues, '_call', fake)
        moved = track('99', artist='Other Band', album='Rain', title='Rain')
        song.plex.tracks.pop('42')
        song.plex.library = SimpleNamespace(section=lambda name: SimpleNamespace(searchTracks=lambda title: [moved]))
        names = {'artist': 'Example Band', 'album': 'Rain', 'title': 'Rain'}
        song.state['open']['42']['applied'] = {'names': names, 'detail': 'wrote it',
                                               'at': debug_issues.time.time() - debug_issues.VERIFY_SECONDS - 1}
        debug_issues.verify_applied(song.plex, song.state, 'Music')
        assert fake.calls[-1][2]['detail'].endswith('Plex still shows Other Band – Rain [Rain]')
        moved.grandparentTitle = 'Example Band'
        song.state['open']['42']['applied'] = {'names': names, 'detail': 'wrote it', 'at': debug_issues.time.time()}
        debug_issues.verify_applied(song.plex, song.state, 'Music')
        assert fake.calls[-1][2] == {'outcome': 'fixed', 'detail': 'wrote it'}

    def test_an_ai_fix_no_catalogue_backs_is_not_written(self, song, monkeypatch):
        fake = Debugarr()
        monkeypatch.setattr(debug_issues, '_call', fake)
        monkeypatch.setattr(debug_issues, 'confirm', lambda meta, length: (False, 'neither Deezer nor Spotify lists it'))
        assert debug_issues.act(song.plex, song.state, [self.fix()]) == 0
        assert song.written == []
        assert fake.calls[-1][2] == {'outcome': 'failed', 'detail': 'neither Deezer nor Spotify lists it'}
        assert '42' in song.state['open']

    def test_the_admins_values_are_written_as_given(self, song, monkeypatch):
        monkeypatch.setattr(debug_issues, '_call', Debugarr())
        monkeypatch.setattr(debug_issues, 'confirm', lambda *a: pytest.fail('the admin is not second-guessed'))
        assert debug_issues.act(song.plex, song.state, [self.fix(by='person')]) == 1
        assert song.written

    def test_placeholder_names_are_never_written(self, song, monkeypatch):
        fake = Debugarr()
        monkeypatch.setattr(debug_issues, '_call', fake)
        issue = dict(self.fix(by='person'), solution={'artist': 'Unknown Artist', 'album': 'Rain', 'title': 'Rain'})
        debug_issues.act(song.plex, song.state, [issue])
        assert song.written == []
        assert fake.calls[-1][2]['outcome'] == 'failed'

    def test_a_song_the_admin_closed_leaves_manual_fix(self, song, monkeypatch):
        monkeypatch.setattr(debug_issues, '_call', Debugarr())
        debug_issues.act(song.plex, song.state, [self.fix(status='closed')])
        assert song.written == []
        assert '42' not in song.state['open']

    def test_a_fix_for_a_song_no_longer_in_manual_fix_closes_its_issue(self, song, monkeypatch):
        fake = Debugarr()
        monkeypatch.setattr(debug_issues, '_call', fake)
        debug_issues.act(song.plex, song.state, [self.fix(key='99')])
        assert fake.calls == [('POST', '/api/issues/music-managarr/manual-fix-99/resolve',
                               {'detail': 'the song is no longer in Manual Fix'})]

    def test_waiting_keeps_fixes_and_closed_issues_of_tracked_songs(self, song, monkeypatch):
        listed = {'issues': [self.fix(), self.fix(status='closed'), self.fix(status='closed', key='7')]}
        monkeypatch.setattr(debug_issues, '_call', Debugarr({'/api/issues': listed}))
        assert [i['status'] for i in debug_issues.waiting(song.state)] == ['fix_ready', 'closed']


class TestWrite:
    def test_a_file_lidarr_owns_stays_where_lidarr_keeps_it(self, monkeypatch, tmp_path):
        folder = tmp_path / 'Example Band' / 'First Light (2024)'
        folder.mkdir(parents=True)
        song = folder / 'Example Band - First Light - 04 - Bright Things.mp3'
        song.write_bytes(b'')
        names = {'artist': 'Example Band', 'album': 'First Light', 'title': 'Bright Things'}
        import artwork
        writes, refreshed = [], []
        monkeypatch.setattr(fix_metadata, '_file_tags', lambda fp: dict(names))
        monkeypatch.setattr(fix_metadata, '_file_away', lambda fp, meta: (meta, str(folder), song.name))
        monkeypatch.setattr(fix_metadata, '_write_tags', lambda fp, meta: writes.append((fp, meta)) or True)
        monkeypatch.setattr(fix_metadata, '_tell_lidarr', lambda folder: None)
        monkeypatch.setattr(fix_metadata, '_refresh_in_plex', lambda t: refreshed.append(t))
        monkeypatch.setattr(artwork, 'ensure_artwork', lambda path, meta, hints=(), replace=False: 'present')
        written, art, out = debug_issues.write('track', str(song), names)
        assert (written, art, out) == (True, 'present', names)
        assert song.exists() and writes == [(str(song), names)] and refreshed == ['track']


class TestConfirm:
    META = {'artist': 'Maddhouse', 'album': 'Rain', 'title': 'Rain'}

    @pytest.fixture(autouse=True)
    def catalogues(self, monkeypatch):
        import artwork
        self.listing, self.lidarr, self.upload = None, False, False
        monkeypatch.setattr(metadata_repair, 'release_listing', lambda a, t, d: self.listing)
        monkeypatch.setattr(metadata_repair, 'lidarr_confirms_album', lambda a, al: self.lidarr)
        monkeypatch.setattr(artwork, 'own_upload_matches', lambda a, t, d: self.upload)

    def test_a_listing_on_the_same_album_confirms(self):
        self.listing = {'album': 'Rain', 'title': 'Rain', 'source': 'Deezer'}
        assert debug_issues.confirm(self.META, 185.0) == (True, 'Deezer lists it on that album at this length')

    def test_a_listing_on_another_album_needs_lidarr(self):
        self.listing = {'album': 'Greatest Hits', 'title': 'Rain', 'source': 'Spotify'}
        confirmed, why = debug_issues.confirm(self.META, 185.0)
        assert not confirmed and 'on "Greatest Hits"' in why
        self.lidarr = True
        assert debug_issues.confirm(self.META, 185.0)[0]

    def test_the_artists_own_upload_confirms_a_single(self):
        self.upload = True
        assert debug_issues.confirm(self.META, 185.0)[0]
        assert not debug_issues.confirm(dict(self.META, album='Other'), 185.0)[0]

    def test_nothing_listed_is_not_confirmed(self):
        confirmed, why = debug_issues.confirm(self.META, 185.0)
        assert not confirmed and "at the file's length (3:05)" in why
        assert not debug_issues.confirm(self.META, None)[0]


class TestManualFixState:
    def test_a_reported_song_that_leaves_the_playlist_closes_its_issue(self, monkeypatch):
        monkeypatch.setattr(manual_fix, 'needs_repair', lambda item: None)
        gone, fixed = track('1'), track('2', artist='Maddhouse', album='Rain', title='Rain')
        plex = Plex([fixed], Playlist([fixed]))
        state = {'open': {'1': record(issue='I1'), '2': record(issue='I2'), '3': record()},
                 'resolved_unsent': [], 'issues_to_close': []}
        manual_fix.prune_fixed(plex, state)
        assert state['open'] == {}
        assert sorted(k for k, _ in state['issues_to_close']) == ['1', '2']

    def test_issues_to_close_are_kept_until_debugarr_answers(self, configured, monkeypatch):
        monkeypatch.setattr(debug_issues, '_call', Debugarr(status=0))
        state = {'issues_to_close': [['1', 'fixed']]}
        debug_issues.close_finished(state)
        assert state['issues_to_close'] == [['1', 'fixed']]
        monkeypatch.setattr(debug_issues, '_call', Debugarr())
        debug_issues.close_finished(state)
        assert state['issues_to_close'] == []

    def test_fingerprint_answers_are_kept_in_short(self):
        heard = metadata_repair.heard_summary([
            {'artist': 'Maddhouse', 'title': 'Rain', 'album': 'Rain', 'service': 'Shazam', 'votes': 2,
             'cover_art_url': 'https://example.org/x.jpg'}, {}, None])
        assert heard == [{'artist': 'Maddhouse', 'title': 'Rain', 'album': 'Rain', 'service': 'Shazam', 'votes': 2}]


class TestDaemonPass:
    def test_fixes_are_applied_after_the_playlists_are_noted(self, monkeypatch):
        calls = []
        monkeypatch.setattr(fmd, 'dry_run', lambda: False)
        monkeypatch.setattr(manual_fix, 'load_state', lambda: {'open': {}, 'resolved_unsent': [], 'issues_to_close': []})
        monkeypatch.setattr(manual_fix, 'save_state', lambda state: None)
        monkeypatch.setattr(manual_fix, 'send_pending_notifications', lambda state, plex=None: calls.append(('notify', plex)))
        monkeypatch.setattr(debug_issues, 'waiting', lambda state: [{'status': 'fix_ready'}])
        monkeypatch.setattr(debug_issues, 'act', lambda plex, state, issues: calls.append('act') or 1)
        monkeypatch.setattr(fmd, 'keep_playlists', lambda *a, moving=False: calls.append(('keep', moving)))
        for step in ('delete_playlist_if_due', 'merge_albums_if_due', 'lyrics_if_due', 'picked_for_you_if_due'):
            monkeypatch.setattr(fmd, step, lambda *a, **k: None)
        monkeypatch.setattr(fmd, 'process_due_users', lambda *a: [])
        monkeypatch.setattr(fmd, 'repair_library_if_due', lambda *a: [])
        rescans = []
        monkeypatch.setattr(fmd, 'rescan_if_changed', lambda plex, section: rescans.append(fmd._files_changed))
        plex = object()
        fmd.run_pass(plex, {}, 'Music', 86400)
        assert calls == [('keep', True), 'act', ('notify', plex)]
        assert rescans == [1]

    def test_a_dry_run_leaves_debugarr_alone(self, monkeypatch):
        calls = []
        monkeypatch.setattr(fmd, 'dry_run', lambda: True)
        monkeypatch.setattr(manual_fix, 'load_state', lambda: {'open': {}, 'resolved_unsent': [], 'issues_to_close': []})
        monkeypatch.setattr(manual_fix, 'save_state', lambda state: None)
        monkeypatch.setattr(manual_fix, 'send_pending_notifications', lambda state, plex=None: calls.append(plex))
        monkeypatch.setattr(debug_issues, 'waiting', lambda state: pytest.fail('no fixes in a dry run'))
        for step in ('keep_playlists', 'delete_playlist_if_due', 'merge_albums_if_due', 'lyrics_if_due',
                     'picked_for_you_if_due', 'rescan_if_changed'):
            monkeypatch.setattr(fmd, step, lambda *a, **k: None)
        monkeypatch.setattr(fmd, 'repair_library_if_due', lambda *a: [])
        fmd.run_pass(object(), {}, 'Music', 86400)
        assert calls == [None]
