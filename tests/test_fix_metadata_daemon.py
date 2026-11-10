from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import fix_metadata_daemon as fmd


class TestFixMetadataDaemonScheduling:
    """fix_metadata_daemon.py is a companion process started once by
    entrypoint.py, with its own daily schedule per user, independent of
    SCAN_INTERVAL (a per-user cycle may run only monthly)."""

    def test_never_run_before_is_due(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        now = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        assert fmd.is_due('newuser', now, interval=86400) is True

    def test_due_after_interval_elapsed(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        now = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        fmd.write_last_run('someuser', now - timedelta(hours=25))
        assert fmd.is_due('someuser', now, interval=86400) is True

    def test_not_due_before_interval_elapsed(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        now = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        fmd.write_last_run('someuser', now - timedelta(hours=23))
        assert fmd.is_due('someuser', now, interval=86400) is False

    def test_each_user_tracked_independently(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        now = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        fmd.write_last_run('alice', now - timedelta(hours=1))
        # bob has never run — due regardless of alice's state
        assert fmd.is_due('alice', now, interval=86400) is False
        assert fmd.is_due('bob', now, interval=86400) is True

    def test_process_due_users_only_runs_for_due_users_and_updates_timestamp(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        monkeypatch.setenv('METADATA_REPAIR_DRY_RUN', 'false')
        real_now = datetime.now(timezone.utc)
        fmd.write_last_run('alice', real_now - timedelta(hours=1))  # ran an hour ago — not due
        # bob has never run — due

        calls = []
        monkeypatch.setattr(
            fmd, 'run_fix_metadata',
            lambda admin, user, section: calls.append(user) or {'fixed': 1, 'skipped': 0, 'failed': 0},
        )

        mapping = {'alice_discord': 'alice', 'bob_discord': 'bob'}
        fmd.process_due_users(admin_plex=object(), mapping=mapping, plex_section='Music', interval=86400)

        assert calls == ['bob']
        # bob's timestamp should now be recorded so the next pass isn't due immediately
        assert fmd.is_due('bob', datetime.now(timezone.utc), interval=86400) is False

    def test_interval_seconds_has_sane_floor(self, monkeypatch):
        monkeypatch.setenv('FIX_METADATA_INTERVAL_SECONDS', '10')
        assert fmd.interval_seconds() == 3600  # floored, never faster than hourly

    def test_interval_seconds_respects_configured_value(self, monkeypatch):
        monkeypatch.setenv('FIX_METADATA_INTERVAL_SECONDS', '86400')
        assert fmd.interval_seconds() == 86400

    def test_poll_seconds_scales_with_interval(self):
        assert fmd.poll_seconds(86400) == 300  # capped at 5 minutes
        assert fmd.poll_seconds(3600) == 150  # 3600 // 24


class TestRunPass:
    def test_both_sources_feed_manual_fix_and_state_is_saved(self, monkeypatch):
        import manual_fix
        flagged = {'rating_key': '1', 'label': 'a', 'reason': 'r', 'source': 'flagged by bob'}
        scanned = {'rating_key': '2', 'label': 'b', 'reason': 'r', 'source': 'library scan'}
        calls = []
        monkeypatch.setattr(manual_fix, 'load_state', lambda: {'open': {}, 'resolved_unsent': []})
        monkeypatch.setattr(fmd, 'process_due_users', lambda *a: [flagged])
        monkeypatch.setattr(fmd, 'repair_library_if_due', lambda *a: [scanned])
        monkeypatch.setattr(manual_fix, 'add_tracks', lambda plex, entries, state: calls.append(('add', [e['rating_key'] for e in entries])))
        monkeypatch.setattr(manual_fix, 'send_pending_notifications', lambda state: calls.append(('notify',)))
        monkeypatch.setattr(manual_fix, 'save_state', lambda state: calls.append(('save',)))

        fmd.run_pass(admin_plex=object(), mapping={'b': 'bob'}, plex_section='Music', interval=86400)

        assert calls == [('add', ['1', '2']), ('notify',), ('save',)]

    def test_plex_scans_after_a_pass_that_moved_files(self, monkeypatch):
        """Plex may not watch the folders, so a moved song would stay
        unavailable until its next scheduled scan."""
        import manual_fix
        from types import SimpleNamespace
        scans = []
        section = SimpleNamespace(update=lambda: scans.append('scan'))
        plex = SimpleNamespace(library=SimpleNamespace(section=lambda name: section))
        monkeypatch.setenv('METADATA_REPAIR_DRY_RUN', 'false')
        monkeypatch.setattr(manual_fix, 'load_state', lambda: {'open': {}, 'resolved_unsent': []})
        monkeypatch.setattr(manual_fix, 'send_pending_notifications', lambda state: None)
        monkeypatch.setattr(manual_fix, 'save_state', lambda state: None)
        monkeypatch.setattr(fmd, 'process_due_users', lambda *a: [])
        monkeypatch.setattr(fmd, 'merge_albums_if_due', lambda *a: None)

        monkeypatch.setattr(fmd, 'repair_library_if_due', lambda *a: fmd._count_changes(3) or [])
        fmd.run_pass(admin_plex=plex, mapping={}, plex_section='Music', interval=86400)
        assert scans == ['scan']

        monkeypatch.setattr(fmd, 'repair_library_if_due', lambda *a: [])
        fmd.run_pass(admin_plex=plex, mapping={}, plex_section='Music', interval=86400)
        assert scans == ['scan']          # nothing changed: no extra scan

    def test_pending_dms_are_retried_even_if_the_pass_fails(self, monkeypatch):
        import manual_fix
        calls = []
        monkeypatch.setattr(manual_fix, 'load_state', lambda: {'open': {}, 'resolved_unsent': []})

        def plex_down(*a):
            raise ConnectionError('plex down')

        monkeypatch.setattr(fmd, 'process_due_users', plex_down)
        monkeypatch.setattr(manual_fix, 'send_pending_notifications', lambda state: calls.append('notify'))
        monkeypatch.setattr(manual_fix, 'save_state', lambda state: calls.append('save'))
        try:
            fmd.run_pass(admin_plex=object(), mapping={}, plex_section='Music', interval=86400)
        except ConnectionError:
            pass
        assert calls == ['notify', 'save']

    def test_library_scan_runs_once_per_interval(self, tmp_path, monkeypatch):
        import manual_fix
        import metadata_repair
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        monkeypatch.setenv('METADATA_REPAIR_DRY_RUN', 'false')
        scans = []
        monkeypatch.setattr(manual_fix, 'prune_fixed', lambda plex, state: 0)
        monkeypatch.setattr(manual_fix, 'playlist_keys', lambda plex: {'7'})
        monkeypatch.setattr(metadata_repair, 'repair_library', lambda section, skip, limit, dry_run=False: scans.append(skip) or (
            {'found': 0, 'fixed': 0, 'unresolved': 0}, []))
        plex = SimpleNamespace(library=SimpleNamespace(section=lambda name: object()))

        fmd.repair_library_if_due(plex, 'Music', 86400, {'open': {}, 'resolved_unsent': []})
        fmd.repair_library_if_due(plex, 'Music', 86400, {'open': {}, 'resolved_unsent': []})

        # Second call within the interval is a no-op; tracks already waiting
        # in Manual Fix are skipped rather than re-fingerprinted.
        assert scans == [{'7'}]


    def test_album_reconcile_runs_once_per_interval(self, tmp_path, monkeypatch):
        import album_artist_merger
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        runs = []

        class FakeMerger:
            def lidarr_reconcile(self):
                runs.append('reconcile')

        monkeypatch.setattr(album_artist_merger, 'build_merger_from_env', lambda plex=None: FakeMerger())
        fmd.merge_albums_if_due(object(), 86400)
        fmd.merge_albums_if_due(object(), 86400)
        assert runs == ['reconcile']

    def test_a_failing_reconcile_waits_for_the_next_interval(self, tmp_path, monkeypatch):
        import album_artist_merger
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        runs = []

        class Broken:
            def lidarr_reconcile(self):
                runs.append('x')
                raise RuntimeError('lidarr down')

        monkeypatch.setattr(album_artist_merger, 'build_merger_from_env', lambda plex=None: Broken())
        for _ in range(2):
            try:
                fmd.merge_albums_if_due(object(), 86400)
            except RuntimeError:
                pass
        assert runs == ['x']

    def test_reconcile_failure_does_not_stop_the_pass(self, monkeypatch):
        import manual_fix
        calls = []
        monkeypatch.setattr(manual_fix, 'load_state', lambda: {'open': {}, 'resolved_unsent': []})
        monkeypatch.setattr(fmd, 'process_due_users', lambda *a: [])
        monkeypatch.setattr(fmd, 'repair_library_if_due', lambda *a: [])
        monkeypatch.setattr(fmd, 'merge_albums_if_due', lambda *a: (_ for _ in ()).throw(RuntimeError('boom')))
        monkeypatch.setattr(manual_fix, 'send_pending_notifications', lambda state: calls.append('notify'))
        monkeypatch.setattr(manual_fix, 'save_state', lambda state: calls.append('save'))
        fmd.run_pass(admin_plex=object(), mapping={}, plex_section='Music', interval=86400)
        assert calls == ['notify', 'save']


class TestUserFlaggedEscalation:
    def _run(self, monkeypatch, fingerprint):
        import audio_id
        import fix_metadata

        flagged = SimpleNamespace(ratingKey='5', title='Wrong Title', parentTitle='Wrong Album',
                                  grandparentTitle='Wrong Artist', originalTitle='', refresh=lambda: None)

        class Playlist:
            title = 'Fix Metadata'

            def __init__(self):
                self._items = [flagged]

            def items(self):
                return list(self._items)

            def removeItems(self, items):
                self._items = []

        playlist = Playlist()
        user_plex = SimpleNamespace(playlists=lambda: [playlist], library=SimpleNamespace(section=lambda name: object()))
        admin = SimpleNamespace(switchUser=lambda name: user_plex)
        monkeypatch.setattr(fix_metadata, '_file_path', lambda t: '/music/x.mp3')
        identify = fingerprint if callable(fingerprint) else (lambda fp: fingerprint)
        monkeypatch.setattr(audio_id, 'identify_track_by_audio_with_alternatives', identify)
        stats = fix_metadata.run_fix_metadata(admin, 'bob', 'Music')
        return stats, playlist

    def test_no_fingerprint_result_goes_to_admin(self, monkeypatch):
        stats, playlist = self._run(monkeypatch, (None, None))
        assert [u['rating_key'] for u in stats['unresolved']] == ['5']
        assert stats['unresolved'][0]['source'] == 'flagged by bob'
        assert playlist.items() == []  # handed off, not retried forever

    def test_fingerprint_agreeing_with_flagged_tags_goes_to_admin(self, monkeypatch):
        same = {'artist': 'Wrong Artist', 'album': 'Wrong Album', 'title': 'Wrong Title'}
        stats, _ = self._run(monkeypatch, (same, None))
        assert 'confirms the current tags' in stats['unresolved'][0]['reason']

    def test_fingerprint_exception_goes_to_admin(self, monkeypatch):
        def boom(fp):
            raise RuntimeError('ACRCloud down')

        stats, playlist = self._run(monkeypatch, boom)
        assert 'fingerprinting failed' in stats['unresolved'][0]['reason']
        assert playlist.items() == []

    def test_identified_track_is_fixed_not_escalated(self, monkeypatch):
        import fix_metadata
        import metadata_repair
        written = []
        monkeypatch.setattr(fix_metadata, '_write_tags', lambda fp, meta: written.append(meta) or True)
        monkeypatch.setattr(fix_metadata, '_reorganize_file', lambda fp, meta, **kw: fp)
        monkeypatch.setattr(metadata_repair, 'lidarr_album_for_track', lambda a, t: 'Discovery')
        right = {'artist': 'Daft Punk', 'album': '', 'title': 'One More Time'}
        # A different song is applied when a second service agrees on it.
        stats, _ = self._run(monkeypatch, (right, dict(right)))
        # Fixed; with every cover source offline it is also listed for a cover.
        assert stats['fixed'] == 1 and all('no cover' in u['reason'] for u in stats['unresolved'])
        # Fingerprint had no album; Lidarr's album for the corrected
        # artist + title replaces the flagged (wrong) one.
        assert written == [{'artist': 'Daft Punk', 'album': 'Discovery', 'title': 'One More Time'}]

    def test_flagged_album_kept_when_lidarr_has_nothing(self, monkeypatch):
        import fix_metadata
        import metadata_repair
        written = []
        monkeypatch.setattr(fix_metadata, '_write_tags', lambda fp, meta: written.append(meta) or True)
        monkeypatch.setattr(fix_metadata, '_reorganize_file', lambda fp, meta, **kw: fp)
        monkeypatch.setattr(metadata_repair, 'lidarr_album_for_track', lambda a, t: None)
        right = {'artist': 'Daft Punk', 'album': '', 'title': 'One More Time'}
        self._run(monkeypatch, (right, dict(right)))
        assert written[0]['album'] == 'Wrong Album'


class TestDryRun:
    def test_dry_run_is_the_default(self, monkeypatch):
        monkeypatch.delenv('METADATA_REPAIR_DRY_RUN', raising=False)
        assert fmd.dry_run() is True

    def test_dry_run_skips_user_playlists(self, monkeypatch):
        monkeypatch.setenv('METADATA_REPAIR_DRY_RUN', 'true')
        monkeypatch.setattr(fmd, 'run_fix_metadata', lambda *a: (_ for _ in ()).throw(AssertionError('must not run')))
        assert fmd.process_due_users(object(), {'b': 'bob'}, 'Music', 86400) == []

    def test_dry_run_library_scan_writes_nothing(self, tmp_path, monkeypatch):
        import manual_fix
        import metadata_repair
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        monkeypatch.setenv('METADATA_REPAIR_DRY_RUN', 'true')
        monkeypatch.setattr(manual_fix, 'prune_fixed', lambda *a: (_ for _ in ()).throw(AssertionError('no prune')))
        monkeypatch.setattr(manual_fix, 'playlist_keys', lambda plex: set())
        seen = {}
        monkeypatch.setattr(metadata_repair, 'repair_library', lambda section, skip, limit, dry_run=False: seen.update(dry_run=dry_run) or (
            {'found': 1, 'fixed': 0, 'unresolved': 1}, [{'rating_key': '1'}]))
        plex = SimpleNamespace(library=SimpleNamespace(section=lambda name: object()))
        assert fmd.repair_library_if_due(plex, 'Music', 86400, {'open': {}, 'resolved_unsent': []}) == []
        assert seen == {'dry_run': True}
