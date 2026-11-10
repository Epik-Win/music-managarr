"""Albums Plex files wrongly, and albums with nothing real to show:

* an album under an artist it doesn't belong to although its files are
  tagged correctly (Plex keeps a local album under the artist it was first
  filed under): the daily scan compares Plex's artist with the files';
* "Miscellaneous" albums (a placeholder written when no album is known) are
  looked up like any other unknown album;
* an album with no art of its own showing its artist's picture does not count
  as having artwork.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import artwork
import fix_metadata
import metadata_repair as mr
import plex_refile

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 4096


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(mr, 'ask_local_ai', lambda system, prompt: None)
    monkeypatch.delenv('PLEX_REFILE_ENABLED', raising=False)


@pytest.fixture
def mp3(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = tmp_path / "in" / "song.mp3"
    path.parent.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "1", "-q:a", "9", str(path)], check=True)
    return path


# ── Miscellaneous and borrowed artwork ──────────────────────────────────────

def _track(key='1', title='Song', album='Album', artist='Artist', thumb=None, album_key='50'):
    return SimpleNamespace(ratingKey=key, title=title, parentTitle=album, grandparentTitle=artist,
                           originalTitle='', parentRatingKey=album_key,
                           parentThumb=thumb if thumb is not None else f'/library/metadata/{album_key}/thumb/1')


class TestPlaceholdersAndArt:
    def test_miscellaneous_album_needs_repair(self):
        assert mr.needs_repair(_track(album='Miscellaneous')) == 'unknown album'
        assert mr.needs_repair(_track(album='Misc.')) == 'unknown album'

    def test_miscellaneous_album_tag_is_not_known(self, monkeypatch):
        class Tags(dict):
            pass
        monkeypatch.setattr('mutagen.File', lambda p, easy=True: Tags(
            artist=['George Benson'], album=['Miscellaneous'], title=['Mona Lisa']))
        assert mr.file_tag_meta('/music/x.flac')['album'] == ''

    def test_artist_picture_on_album_counts_as_missing_art(self):
        nina = _track(album='A Single Woman', artist='Nina Simone', album_key='69133',
                      thumb='/library/metadata/31523/thumb/1791361981')
        assert mr.needs_repair(nina) == mr.NO_ARTWORK
        assert mr.needs_repair(_track()) is None

    def test_one_track_per_album_for_artwork_with_its_own_budget(self):
        def album(key, n, thumb):
            tracks = [_track(f'{key}{i}', album=f'Album {key}', album_key=key,
                             thumb=thumb or f'/library/metadata/{key}/thumb/1') for i in range(n)]
            return SimpleNamespace(title=f'Album {key}', parentTitle='Artist', ratingKey=key,
                                   thumb=thumb, tracks=lambda: tracks)
        borrowed = [album(str(k), 3, '/library/metadata/9/thumb/1') for k in range(10, 14)]
        unknown = SimpleNamespace(title='[Unknown Album]', parentTitle='X', ratingKey='20',
                                  thumb='/library/metadata/20/thumb/1',
                                  tracks=lambda: [_track(f'u{i}', album='[Unknown Album]', album_key='20') for i in range(3)])
        found = mr.find_candidates(SimpleNamespace(albums=lambda: borrowed + [unknown]), set(), limit=3)
        reasons = [(t.ratingKey, r) for t, r in found]
        # metadata repairs keep their full budget; one artwork track per album
        assert reasons[:3] == [('u0', 'unknown album'), ('u1', 'unknown album'), ('u2', 'unknown album')]
        assert reasons[3:] == [('100', mr.NO_ARTWORK), ('110', mr.NO_ARTWORK), ('120', mr.NO_ARTWORK)]

    def test_embedded_art_is_written_out_as_folder_cover(self, mp3):
        artwork.embed_art(str(mp3), JPEG)
        assert artwork.ensure_artwork(str(mp3), {}) == "folder cover from embedded art"
        assert (mp3.parent / "cover.jpg").read_bytes() == JPEG
        assert artwork.ensure_artwork(str(mp3), {}) == "present"


# ── detection ───────────────────────────────────────────────────────────────

def _plex_track(key, album_key, path, rating=None, plays=0, artist='?'):
    part = SimpleNamespace(file=path)
    return SimpleNamespace(ratingKey=key, parentRatingKey=album_key, title=os.path.basename(path),
                           userRating=rating, viewCount=plays, grandparentTitle=artist,
                           media=[SimpleNamespace(parts=[part])])


def _album(key, title, artist, artist_key, guid=None):
    return SimpleNamespace(ratingKey=key, title=title, parentTitle=artist, parentRatingKey=artist_key,
                           guid=guid or f'local://{key}')


class FakeSection:
    def __init__(self, albums, tracks):
        self._albums, self._tracks = albums, tracks
        self.refreshing = False
        self.calls = []

    def albums(self):
        return self._albums

    def searchTracks(self):
        return self._tracks

    def reload(self):
        pass

    def update(self, path=None):
        self.calls.append('update')

    def emptyTrash(self):
        self.calls.append('emptyTrash')


@pytest.fixture
def library(tmp_path):
    """Files on disk + the tags each one carries."""
    tags = {}

    def add(rel, albumartist, artist=None, other=''):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'audio')
        tags[str(p)] = {'albumartist': albumartist, 'artist': artist or albumartist, 'other': other}
        return str(p)
    return SimpleNamespace(add=add, tags=tags, root=tmp_path)


def _find(section, library):
    return plex_refile.find_misfiled(section, read_tags=lambda p: library.tags.get(p, {}))


class TestFindMisfiled:
    def test_sink_artist_albums_are_plex_errors(self, library):
        predator = library.add('albums/Crack Amico/Crack Amico/Predator.mp3', 'Crack Amico')
        bear = library.add('albums/Thao Sam/Remember Me/Remember Me.mp3', 'Thao Sam')
        own = library.add('albums/Hans Zimmer/Inception/Time.mp3', 'Hans Zimmer')
        section = FakeSection(
            [_album('1', 'Crack Amico', 'Hans Zimmer', '130'), _album('2', 'Remember Me', 'Hans Zimmer', '130'),
             _album('3', 'Inception', 'Hans Zimmer', '130')],
            [_plex_track('11', '1', predator), _plex_track('21', '2', bear), _plex_track('31', '3', own)])
        found = _find(section, library)
        assert sorted((m.tagged_artist, m.verdict) for m in found) == [
            ('Crack Amico', plex_refile.SINK), ('Thao Sam', plex_refile.SINK)]

    def test_credited_or_respelled_artist_is_not_misfiled(self, library):
        donny = library.add('albums/Donny Benét/Mr Experience/x.mp3', 'Donny Benét')
        kit = library.add('albums/Kit Armstrong/Sonatas/x.flac', 'Kit Armstrong', 'Kit Armstrong, Renaud Capuçon')
        jazz = library.add('albums/The Smooth Jazz Alley/EP/x.mp3', 'The Smooth Jazz Alley')
        rufus = library.add('albums/RÜFÜS DU SOL/Inhale/x.mp3', 'RÜFÜS DU SOL')
        section = FakeSection(
            [_album('2', 'Mr Experience', 'Donny Benet', '6'), _album('3', 'Sonatas', 'Renaud Capuçon', '7'),
             _album('4', 'EP', 'Smooth Jazz Alley', '8'), _album('5', 'Inhale / Exhale', 'RÜFÜS', '9')],
            [_plex_track('21', '2', donny), _plex_track('31', '3', kit), _plex_track('41', '4', jazz),
             _plex_track('51', '5', rufus)])
        assert _find(section, library) == []

    def test_placeholder_artist_files_are_their_own_case(self, library):
        rain = library.add('albums/Unknown/Rain/x.mp3', 'Unknown')
        elgar = library.add('albums/Lost/The Dream of Gerontius/x.mp3', 'Lost')
        section = FakeSection(
            [_album('1', 'Rain', 'Hans Zimmer', '130'), _album('2', 'The Dream of Gerontius', 'Hans Zimmer', '130')],
            [_plex_track('11', '1', rain), _plex_track('21', '2', elgar)])
        found = _find(section, library)
        # never re-filed as-is (Plex would sink them again), and they don't
        # make Hans Zimmer a sink on their own
        assert [m.verdict for m in found] == [plex_refile.PLACEHOLDER, plex_refile.PLACEHOLDER]

    def test_combined_plex_artist_goes_under_the_files_artist(self, library):
        becky = library.add('albums/Becky G/MALA SANTA/x.mp3', 'Becky G')
        section = FakeSection([_album('1', 'MALA SANTA', 'Becky G/Natti Natasha', '5')], [_plex_track('11', '1', becky)])
        [m] = _find(section, library)
        assert m.verdict == plex_refile.COMBINED

    def test_one_acts_name_with_and_is_not_a_combined_credit(self, library):
        dana = library.add('albums/Dana/Another Friend/x.mp3', 'Dana')
        grover = library.add('albums/Grover Washington/Grover Live/x.mp3', 'Grover Washington')
        section = FakeSection([_album('1', 'Another Friend', 'Dana and the Wolf', '5'),
                               _album('2', 'Grover Live', 'Grover Washington, Jr.', '6')],
                              [_plex_track('11', '1', dana), _plex_track('21', '2', grover)])
        assert _find(section, library) == []

    def test_matched_albums_and_missing_files_are_skipped(self, library):
        f = library.add('albums/Earth, Wind & Fire/I Am/x.flac', 'Earth, Wind & Fire')
        section = FakeSection(
            [_album('1', 'I Am', 'Somebody Else', '5', guid='plex://album/abc'),
             _album('2', 'Gone', 'Somebody Else', '5')],
            [_plex_track('11', '1', f), _plex_track('21', '2', str(library.root / 'missing.mp3'))])
        assert _find(section, library) == []

    def test_untouched_other_artist_tags_count_as_credit(self, library):
        beatles = library.add('albums/George Benson/Miscellaneous/Baby\'s In Black.flac', 'George Benson',
                              other='The Beatles')
        section = FakeSection([_album('1', 'Miscellaneous', 'The Beatles', '5')], [_plex_track('11', '1', beatles)])
        assert _find(section, library) == []

    def test_single_stray_album_is_unclear(self, library):
        jay = library.add('albums/JAY-Z/The Blueprint/x.mp3', 'JAY-Z')
        section = FakeSection([_album('1', 'The Blueprint', 'Eminem', '5')], [_plex_track('11', '1', jay)])
        [m] = _find(section, library)
        assert m.verdict == plex_refile.UNCLEAR and m.plex_artist == 'Eminem'


# ── the daily pass ──────────────────────────────────────────────────────────

class TestRepairMisfiled:
    def _misfiled(self, verdict, plex='Eminem', tagged='JAY-Z'):
        t = _plex_track('11', '1', '/music/albums/x/y/z.mp3')
        return plex_refile.Misfiled(_album('1', 'The Blueprint', plex, '5'), [t], plex, tagged,
                                    verdict=verdict, paths=['/music/albums/x/y/z.mp3'])

    def _run(self, monkeypatch, found, side=None, enabled=False, dry_run=False):
        monkeypatch.setattr(plex_refile, 'find_misfiled', lambda section: found)
        monkeypatch.setattr(plex_refile, 'restore_staged', lambda root=None: 0)
        monkeypatch.setattr(fix_metadata, '_file_path', lambda t: '/music/albums/x/y/z.mp3')
        monkeypatch.setattr(mr, 'fingerprint_side', lambda *a: side)
        applied, refiled = [], []
        monkeypatch.setattr(mr, 'apply_fix', lambda t, fp, meta: applied.append(meta) or True)
        monkeypatch.setattr(plex_refile, 'refile', lambda section, albums: refiled.extend(albums) or
                            {'refiled': [(m.label, m.tagged_artist) for m in albums], 'failed': [], 'ratings': 0})
        if enabled:
            monkeypatch.setenv('PLEX_REFILE_ENABLED', 'true')
        stats, unresolved = mr.repair_misfiled(object(), set(), budget=5, dry_run=dry_run)
        return stats, unresolved, applied, refiled

    def test_refile_waits_for_the_switch(self, monkeypatch):
        stats, _, _, refiled = self._run(monkeypatch, [self._misfiled(plex_refile.SINK)])
        assert refiled == [] and stats['refile_pending'] == 1

    def test_sink_albums_are_refiled_when_enabled(self, monkeypatch):
        stats, _, _, refiled = self._run(monkeypatch, [self._misfiled(plex_refile.SINK)], enabled=True)
        assert len(refiled) == 1 and stats['refiled'] == 1

    def test_dry_run_never_refiles(self, monkeypatch):
        stats, _, _, refiled = self._run(monkeypatch, [self._misfiled(plex_refile.SINK)], enabled=True, dry_run=True)
        assert refiled == [] and stats['refile_pending'] == 1

    def test_fingerprint_for_the_tags_refiles(self, monkeypatch):
        _, _, applied, refiled = self._run(monkeypatch, [self._misfiled(plex_refile.UNCLEAR)], side='tags', enabled=True)
        assert len(refiled) == 1 and applied == []

    def test_fingerprint_for_plex_retags_to_plex_artist(self, monkeypatch):
        stats, _, applied, refiled = self._run(monkeypatch, [self._misfiled(plex_refile.UNCLEAR)], side='plex', enabled=True)
        assert refiled == [] and stats['retagged'] == 1
        assert applied == [{'artist': 'Eminem', 'album': 'The Blueprint', 'title': 'z.mp3'}]

    def test_retag_uses_the_primary_artist_of_a_combined_credit(self, monkeypatch):
        m = self._misfiled(plex_refile.UNCLEAR, plex='Edward Gardner/London Philharmonic Orchestra/Hallé Choir', tagged='Lost')
        _, _, applied, _ = self._run(monkeypatch, [m], side='plex')
        assert applied[0]['artist'] == 'Edward Gardner'

    def test_combined_albums_are_refiled(self, monkeypatch):
        _, _, _, refiled = self._run(monkeypatch, [self._misfiled(plex_refile.COMBINED)], enabled=True)
        assert len(refiled) == 1

    def test_undecided_goes_to_manual_fix(self, monkeypatch):
        stats, unresolved, applied, refiled = self._run(monkeypatch, [self._misfiled(plex_refile.UNCLEAR)], side=None)
        assert refiled == applied == [] and stats['unresolved'] == 1
        assert mr.MISFILED in unresolved[0]['reason']


# ── re-filing ───────────────────────────────────────────────────────────────

class RatedTrack(SimpleNamespace):
    def rate(self, value):
        self.userRating = value

    def markPlayed(self):
        self.viewCount = (self.viewCount or 0) + 1

    def editAddedAt(self, value):
        self.addedAt = value


class FakePlaylist(SimpleNamespace):
    def items(self):
        return self.entries

    def addItems(self, items):
        self.entries.extend(items)


class FakeServer:
    """One account's view of the library: its own ratings and playlists."""
    def __init__(self, state, old, new, album, ratings, playlists=()):
        self.state, self.old, self.new, self.album = state, old, new, album
        self.ratings, self._playlists = ratings, list(playlists)
        self.views = {}

    def _view(self, item):
        if item.ratingKey not in self.views:
            rating, plays = self.ratings.get(item.ratingKey, (None, 0))
            self.views[item.ratingKey] = RatedTrack(**{**vars(item), 'userRating': rating, 'viewCount': plays})
        return self.views[item.ratingKey]

    def fetchItems(self, ekey):
        keys = ekey.rsplit('/', 1)[1].split(',')
        pool = [self.new] if self.state['deleted'] else [self.old]
        return [self._view(t) for t in pool if t.ratingKey in keys]

    def fetchItem(self, key):
        if str(key) == self.album.ratingKey:
            if self.state['deleted']:
                raise LookupError(key)
            return self.album
        if str(key) == self.new.parentRatingKey:
            return self.new_album
        raise LookupError(key)

    def playlists(self, playlistType=None):
        return self._playlists


class TestRefile:
    def _setup(self, tmp_path, monkeypatch, fail_on=None):
        music = tmp_path / 'music'
        f = music / 'albums' / 'Crack Amico' / 'Crack Amico' / 'Predator.mp3'
        f.parent.mkdir(parents=True)
        f.write_bytes(b'audio')
        monkeypatch.setenv('MUSIC_BASE_DIR', str(music))
        monkeypatch.setenv('HOST_MUSIC_BASE_PATH', str(tmp_path / 'host'))
        host_path = str(tmp_path / 'host' / 'music' / 'albums' / 'Crack Amico' / 'Crack Amico' / 'Predator.mp3')
        old = RatedTrack(**vars(_plex_track('44621', '44608', host_path, artist='Hans Zimmer')), addedAt='2026-04-16')
        album = SimpleNamespace(**vars(_album('44608', 'Crack Amico', 'Hans Zimmer', '130')), addedAt='2026-04-16')
        m = plex_refile.Misfiled(album, [old], 'Hans Zimmer', 'Crack Amico', plex_refile.SINK, paths=[str(f)])
        state = {'deleted': False}
        new = RatedTrack(ratingKey='70001', parentRatingKey='70000', title='Predator', userRating=None,
                         viewCount=0, grandparentTitle='Crack Amico', addedAt='2026-10-08',
                         media=[SimpleNamespace(parts=[SimpleNamespace(file=host_path)])])
        admin = FakeServer(state, old, new, album, {'44621': (6.0, 5)})
        mix = FakePlaylist(title='Gym', smart=False, entries=[old])
        user = FakeServer(state, old, new, album, {'44621': (10.0, 0)}, [mix])
        admin.new_album = user.new_album = RatedTrack(ratingKey='70000', addedAt='2026-10-08')
        section = FakeSection([album], [old])
        section._server = admin

        def update(path=None):
            section.calls.append('update')
            if fail_on == 'update' and len(section.calls) == 1:
                raise RuntimeError('Plex unreachable')
            # the first scan sees the file gone; the last sees it back
            assert f.exists() == (section.calls.count('update') > 1)

        def empty_trash():
            section.calls.append('emptyTrash')
            state['deleted'] = True
        section.update, section.emptyTrash = update, empty_trash
        section.searchTracks = lambda: [new] if state['deleted'] else [old]
        monkeypatch.setattr(plex_refile.time, 'sleep', lambda s: None)
        accounts = [('admin', admin), ('bob', user)]
        return SimpleNamespace(section=section, m=m, f=f, admin=admin, user=user, mix=mix, music=music,
                               accounts=accounts)

    def test_file_goes_out_and_back_and_history_is_restored(self, tmp_path, monkeypatch):
        x = self._setup(tmp_path, monkeypatch)
        report = plex_refile.refile(x.section, [x.m], accounts=x.accounts)
        assert x.section.calls == ['update', 'emptyTrash', 'update']
        assert x.f.exists()
        assert report['refiled'] == [(x.m.label, 'Crack Amico')]
        # every account keeps its own rating, plays and playlist entry
        admin_new, user_new = x.admin.views['70001'], x.user.views['70001']
        assert (admin_new.userRating, admin_new.viewCount) == (6.0, 5)
        assert user_new.userRating == 10.0
        assert [e.ratingKey for e in x.mix.entries] == ['44621', '70001']
        # and the album doesn't show up as newly added
        assert admin_new.addedAt == '2026-04-16' and x.admin.new_album.addedAt == '2026-04-16'
        stage = x.music / plex_refile.STAGE_DIRNAME
        assert (stage / '.plexignore').read_text() == '*\n'
        assert not (stage / plex_refile.MANIFEST).exists()

    def test_files_come_back_even_when_plex_fails(self, tmp_path, monkeypatch):
        x = self._setup(tmp_path, monkeypatch, fail_on='update')
        with pytest.raises(RuntimeError):
            plex_refile.refile(x.section, [x.m], accounts=x.accounts)
        assert x.f.exists()

    def test_interrupted_run_is_put_back_next_time(self, tmp_path):
        stage = tmp_path / 'stage'
        staged = stage / 'x' / '0000-000-Predator.mp3'
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b'audio')
        original = tmp_path / 'music' / 'albums' / 'A' / 'B' / 'Predator.mp3'
        (stage / plex_refile.MANIFEST).write_text(json.dumps({str(staged): str(original)}))
        assert plex_refile.restore_staged(str(stage)) == 1
        assert original.exists() and not staged.exists()
        assert not (stage / plex_refile.MANIFEST).exists()
        assert not (stage / 'x').exists()          # emptied batch folder removed

    def test_names_fold_accents_and_split_credits(self):
        assert plex_refile.names('Donny Benét') & plex_refile.names('Donny Benet')
        assert 'nattinatasha' in plex_refile.names('Becky G/Natti Natasha')
        assert not plex_refile.names('Hans Zimmer') & plex_refile.names('Crack Amico')


# ── where a fixed file is filed ─────────────────────────────────────────────

class TestLibraryRoot:
    def test_song_in_artist_folder_stays_in_its_root(self, tmp_path, monkeypatch):
        music = tmp_path / 'music'
        f = music / 'albums' / 'Death Cab for Cutie' / 'Riptides.mp3'
        f.parent.mkdir(parents=True)
        f.write_bytes(b'audio')
        monkeypatch.setenv('MUSIC_BASE_DIR', str(music))
        new = fix_metadata._reorganize_file(str(f), {'artist': 'Death Cab for Cutie', 'album': 'I Built You A Tower',
                                                     'title': 'Riptides'})
        assert new == str(music / 'albums' / 'Death Cab for Cutie' / 'I Built You A Tower' / 'Riptides.mp3')

    def test_roots(self, tmp_path, monkeypatch):
        music = tmp_path / 'music'
        monkeypatch.setenv('MUSIC_BASE_DIR', str(music))
        assert fix_metadata._library_root_of(str(music / 'albums' / 'A' / 'B' / 'c.mp3')) == str(music / 'albums')
        # One library: a stray file outside every root belongs in albums/
        assert fix_metadata._library_root_of(str(music / 'MORGAN WALLEN' / 'Been By Now' / 'x.mp3')) == str(music / 'albums')
        assert fix_metadata._library_root_of('/elsewhere/a/b/c.mp3') is None


def test_doubled_feat_credit_from_acrcloud_is_dropped():
    from text_repair import dedupe_feat
    assert dedupe_feat('Fall (feat. Christina Sapalo) feat. Christina Sapalo') == 'Fall (feat. Christina Sapalo)'
    assert dedupe_feat('Song (feat. A) feat. B') == 'Song (feat. A) feat. B'
    assert dedupe_feat('Song feat. X') == 'Song feat. X'


def test_collaboration_credit_does_not_become_the_album_artist(monkeypatch):
    tags = {'artist': ['George Benson & Idina Menzel'], 'albumartist': ['George Benson'],
            'album': ['Miscellaneous'], 'title': ['When I Fall In Love (feat. Idina Menzel)']}
    monkeypatch.setattr('mutagen.File', lambda p, easy=True: tags)
    assert mr.file_tag_meta('/music/x.flac')['artist'] == 'George Benson'
    tags.update(artist=['George Benson Feat. Judith Hill'], albumartist=['Someone Else'])
    assert mr.file_tag_meta('/music/x.flac')['artist'] == 'George Benson'
    tags.update(artist=['Crack Amico'], albumartist=['Crack Amico'])
    assert mr.file_tag_meta('/music/x.flac')['artist'] == 'Crack Amico'


# ── songs Plex filed under an invented artist ───────────────────────────────

class TestPlaceholderArtists:
    def _run(self, monkeypatch, resolved=None):
        t = _plex_track('11', '1', '/music/albums/Unknown/Rain/Unknown Rain.mp3')
        m = plex_refile.Misfiled(_album('1', 'Unknown ♫Rain♫', 'Gracie Abrams', '5'), [t], 'Gracie Abrams', 'Unknown',
                                 verdict=plex_refile.PLACEHOLDER, paths=['/music/albums/Unknown/Rain/Unknown Rain.mp3'])
        monkeypatch.setattr(plex_refile, 'find_misfiled', lambda section: [m])
        monkeypatch.setattr(plex_refile, 'restore_staged', lambda root=None: 0)
        monkeypatch.setattr(fix_metadata, '_file_path', lambda t: '/music/albums/Unknown/Rain/Unknown Rain.mp3')
        monkeypatch.setattr(mr, 'known_meta', lambda t, p: {'artist': '', 'album': 'Unknown ♫Rain♫', 'title': ''})
        monkeypatch.setattr(mr, 'resolve', lambda p, meta: (resolved, 'audio fingerprint') if resolved else (None, ''))
        applied = []
        monkeypatch.setattr(mr, 'apply_fix', lambda t, fp, meta: applied.append(meta) or True)
        stats, unresolved = mr.repair_misfiled(object(), set(), budget=5)
        return stats, unresolved, applied

    def test_identified_song_gets_its_real_artist(self, monkeypatch):
        answer = {'artist': 'Rain Sounds', 'album': 'Sleep', 'title': 'Rain'}
        stats, unresolved, applied = self._run(monkeypatch, resolved=answer)
        assert applied == [answer] and unresolved == []

    def test_unidentified_song_is_parked_not_left_under_plexs_artist(self, monkeypatch):
        stats, unresolved, applied = self._run(monkeypatch)
        assert applied == [{'artist': 'Unidentified', 'album': 'Unknown ♫Rain♫', 'title': 'Unknown Rain'}]
        assert stats['parked'] == 1 and 'Gracie Abrams' in unresolved[0]['reason']

    def test_parked_songs_are_still_unknown(self):
        assert not mr.artist_known('Unidentified')

    def test_plex_artist_is_no_evidence_when_the_file_names_nobody(self, mp3):
        from mutagen.easyid3 import EasyID3
        tags = EasyID3()
        tags.update({'artist': 'Unknown', 'album': 'FULL BATTLE BROCK VS MOLLY'})
        tags.save(str(mp3))
        plex = SimpleNamespace(title='', parentTitle='FULL BATTLE BROCK VS MOLLY', grandparentTitle='Gracie Abrams',
                               originalTitle='', parentGuid='local://69511')
        assert mr.known_meta(plex, str(mp3))['artist'] == ''
        plex.parentGuid = 'plex://album/abc'
        assert mr.known_meta(plex, str(mp3))['artist'] == 'Gracie Abrams'


def test_admin_is_recognised_by_username_not_only_email(monkeypatch):
    """The server reports the admin by email; the mapping says "alice".
    The admin must not be treated as a Home user (Plex: "not found")."""
    import plex_auth
    import users
    minted = []
    server = SimpleNamespace(myPlexUsername='admin@example.com', _baseurl='http://plex')
    monkeypatch.setattr(plex_auth, 'admin_account', lambda: SimpleNamespace(
        username='alice', title='alice', email='admin@example.com'))
    monkeypatch.setattr(users, 'load_wizarr_mapping', lambda: {'alice_d': 'alice', 'bob_d': 'bob'})
    monkeypatch.setattr(plex_auth, 'home_user_token', lambda url, name: minted.append(name) or None)
    accounts = plex_refile.accounts_for(server)
    assert minted == ['bob']
    assert [name for name, _ in accounts] == ['admin']
