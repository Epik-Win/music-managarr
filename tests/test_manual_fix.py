import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import admin_notify
import manual_fix


def track(key, album='[Unknown Album]', artist='[Unknown Artist]', title='Song'):
    return SimpleNamespace(ratingKey=key, title=title, parentTitle=album, grandparentTitle=artist, originalTitle='',
                           parentThumb='/library/metadata/1/thumb')


class FakePlaylist:
    def __init__(self, items):
        self._items = list(items)
        self.title = manual_fix.PLAYLIST_NAME
        self.deleted = False

    def items(self):
        return list(self._items)

    def addItems(self, items):
        self._items.extend(items)

    def removeItems(self, items):
        keys = {i.ratingKey for i in items}
        self._items = [i for i in self._items if i.ratingKey not in keys]

    def delete(self):
        self.deleted = True


class FakeAdminPlex:
    def __init__(self, library, playlist=None):
        self.library = {t.ratingKey: t for t in library}
        self.playlist = playlist
        self.created = None

    def playlists(self):
        return [self.playlist] if self.playlist and not self.playlist.deleted else []

    def fetchItem(self, key):
        return self.library[str(key)]

    def createPlaylist(self, title, items):
        self.created = (title, list(items))
        self.playlist = FakePlaylist(items)
        return self.playlist


def entry(key, label='X – Y [Z]', reason='unknown album', source='library scan'):
    return {'rating_key': key, 'label': label, 'reason': reason, 'source': source}


def empty_state():
    return {'open': {}, 'resolved_unsent': []}


class TestManualFixPlaylist:
    def test_creates_playlist_with_first_tracks_since_plex_rejects_empty(self):
        plex = FakeAdminPlex([track('1'), track('2')])
        state = empty_state()
        added = manual_fix.add_tracks(plex, [entry('1'), entry('2')], state)
        assert added == 2
        assert plex.created[0] == 'Manual Fix'
        assert [t.ratingKey for t in plex.created[1]] == ['1', '2']
        assert state['open']['1']['notified'] is False

    def test_only_new_tracks_are_added(self):
        playlist = FakePlaylist([track('1')])
        plex = FakeAdminPlex([track('1'), track('2')], playlist)
        state = empty_state()
        added = manual_fix.add_tracks(plex, [entry('1'), entry('2'), entry('2')], state)
        assert added == 1
        assert [i.ratingKey for i in playlist.items()] == ['1', '2']
        assert list(state['open']) == ['2']

    def test_prune_removes_fixed_and_reports_only_notified(self):
        fixed_notified = track('1', album='Discovery', artist='Daft Punk', title='One More Time')
        fixed_silent = track('2', album='Homework', artist='Daft Punk', title='Da Funk')
        still_broken = track('3')
        playlist = FakePlaylist([fixed_notified, fixed_silent, still_broken])
        plex = FakeAdminPlex([], playlist)
        state = {'open': {
            '1': {'label': 'a', 'reason': 'r', 'source': 's', 'notified': True},
            '2': {'label': 'b', 'reason': 'r', 'source': 's', 'notified': False},
            '3': {'label': 'c', 'reason': 'r', 'source': 's', 'notified': True},
            '9': {'label': 'gone', 'reason': 'r', 'source': 's', 'notified': True},
        }, 'resolved_unsent': []}

        assert manual_fix.prune_fixed(plex, state) == 2
        assert [i.ratingKey for i in playlist.items()] == ['3']
        assert state['resolved_unsent'] == ['Daft Punk – One More Time [Discovery]']
        # '9' left the playlist some other way (deleted file / admin removed it)
        assert list(state['open']) == ['3']
        assert playlist.deleted is False

    def test_a_song_flagged_with_complete_names_stays_until_they_change(self):
        # "Carlo Maria Giulini – Beethoven: Violin Concerto" is handed over for
        # its artist; its names are complete from the start, so complete names
        # alone must not take it off the list as "fixed".
        concerto = track('1', album='Violin Concerto', artist='Carlo Maria Giulini', title='Violin Concerto')
        art_only = track('2', album='Homework', artist='Daft Punk', title='Da Funk')
        playlist = FakePlaylist([concerto, art_only])
        plex = FakeAdminPlex([], playlist)
        state = {'open': {
            '1': {'label': 'Carlo Maria Giulini – Violin Concerto [Violin Concerto]',
                  'reason': 'one fingerprint service suggests Itzhak Perlman – Violin Concerto', 'notified': True},
            '2': {'label': 'Daft Punk – Da Funk [Homework]', 'reason': 'missing artwork', 'notified': True},
        }, 'resolved_unsent': []}
        assert manual_fix.prune_fixed(plex, state) == 1
        assert [i.ratingKey for i in playlist.items()] == ['1']
        concerto.grandparentTitle = concerto.originalTitle = 'Itzhak Perlman'       # the admin fixed it
        assert manual_fix.prune_fixed(plex, state) == 1 and playlist.items() == []

    def test_empty_playlist_is_deleted(self):
        playlist = FakePlaylist([track('1', album='Discovery', artist='Daft Punk')])
        plex = FakeAdminPlex([], playlist)
        manual_fix.prune_fixed(plex, empty_state())
        assert playlist.deleted is True


class TestNotifications:
    def _notices(self, monkeypatch, notify_ok=True, resolve_ok=True):
        calls = []
        monkeypatch.setattr(admin_notify, 'resolve_notification',
                            lambda nid: calls.append(('resolve', nid)) or resolve_ok)
        monkeypatch.setattr(admin_notify, 'notify_admin',
                            lambda msg, nid=None: calls.append(('notify', msg, nid)) or notify_ok)
        return calls

    def test_new_tracks_replace_the_notice_with_a_short_note(self, monkeypatch):
        calls = self._notices(monkeypatch)
        state = {'open': {'1': {'label': 'X – Y [Z]', 'reason': 'unknown album', 'source': 'library scan', 'notified': False},
                          '2': {'label': 'Old – One [?]', 'reason': 'r', 'source': 's', 'notified': True}},
                 'resolved_unsent': ['A – B [C]']}
        manual_fix.send_pending_notifications(state)
        assert calls[0] == ('resolve', 'manual-fix')
        _, msg, nid = calls[1]
        assert nid == 'manual-fix'
        assert '1 new track(s)' in msg and '2 waiting' in msg and 'Manual Fix' in msg
        assert 'X – Y' not in msg and 'Old – One' not in msg and 'A – B' not in msg
        assert state['open']['1']['notified'] is True
        assert state['resolved_unsent'] == []

    def test_fixes_alone_send_nothing(self, monkeypatch):
        calls = self._notices(monkeypatch)
        state = {'open': {'2': {'label': 'O', 'notified': True}}, 'resolved_unsent': ['A']}
        manual_fix.send_pending_notifications(state)
        assert calls == []
        assert state['resolved_unsent'] == []

    def test_empty_list_only_removes_the_old_notice(self, monkeypatch):
        calls = self._notices(monkeypatch)
        state = {'open': {}, 'resolved_unsent': ['A']}
        manual_fix.send_pending_notifications(state)
        assert calls == [('resolve', 'manual-fix')]
        assert state['resolved_unsent'] == []

    def test_failed_delivery_is_retried_later(self, monkeypatch):
        self._notices(monkeypatch, notify_ok=False)
        state = {'open': {'1': {'label': 'X', 'reason': 'r', 'source': 's', 'notified': False}},
                 'resolved_unsent': ['A']}
        manual_fix.send_pending_notifications(state)
        assert state['open']['1']['notified'] is False
        assert state['resolved_unsent'] == ['A']

    def test_old_notice_not_removed_means_nothing_sent(self, monkeypatch):
        calls = self._notices(monkeypatch, resolve_ok=False)
        state = {'open': {'1': {'label': 'X', 'reason': 'r', 'source': 's', 'notified': False}}, 'resolved_unsent': []}
        manual_fix.send_pending_notifications(state)
        assert calls == [('resolve', 'manual-fix')]
        assert state['open']['1']['notified'] is False

    def test_nothing_pending_sends_nothing(self, monkeypatch):
        calls = self._notices(monkeypatch)
        manual_fix.send_pending_notifications({'open': {'1': {'notified': True}}, 'resolved_unsent': []})
        assert calls == []

    def test_state_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        assert manual_fix.load_state() == empty_state()
        manual_fix.save_state({'open': {'1': {'label': 'детство', 'notified': True}}, 'resolved_unsent': []})
        assert manual_fix.load_state()['open']['1']['label'] == 'детство'


class TestAdminNotices:
    def test_a_notice_is_kept_until_the_bot_posts_it_and_replaced_when_changed(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        admin_notify.notify_admin('3 tracks to fix', 'manual-fix')
        assert admin_notify.pending()['manual-fix']['message'] == '3 tracks to fix'
        admin_notify.delivered('manual-fix', 111, '3 tracks to fix')
        assert admin_notify.pending() == {}
        admin_notify.notify_admin('5 tracks to fix', 'manual-fix')
        assert admin_notify.pending()['manual-fix']['message_id'] == 111     # the bot replaces message 111

    def test_a_resolved_notice_is_removed_once_the_bot_deletes_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        admin_notify.notify_admin('x', 'manual-fix')
        admin_notify.delivered('manual-fix', 7, 'x')
        admin_notify.resolve_notification('manual-fix')
        assert admin_notify.pending()['manual-fix']['resolved'] is True
        admin_notify.removed('manual-fix')
        assert admin_notify.pending() == {}
