import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import pytest

import metadata_repair as mr

_REAL_ASK_LOCAL_AI = mr.ask_local_ai


@pytest.fixture(autouse=True)
def _no_local_ai(monkeypatch):
    """Unit tests stay offline; AI behaviour is tested explicitly below."""
    monkeypatch.setattr(mr, 'ask_local_ai', lambda system, prompt: None)


def track(key='1', title='Song', album='Album', album_artist='Artist', track_artist=''):
    return SimpleNamespace(
        ratingKey=key, title=title, parentTitle=album,
        grandparentTitle=album_artist, originalTitle=track_artist,
        parentThumb='/library/metadata/1/thumb',
    )


class TestNeedsRepair:
    def test_complete_track_is_fine(self):
        assert mr.needs_repair(track()) is None

    def test_unknown_album(self):
        assert mr.needs_repair(track(album='[Unknown Album]')) == 'unknown album'
        assert mr.needs_repair(track(album='')) == 'unknown album'

    def test_unknown_artist(self):
        assert mr.needs_repair(track(album_artist='[Unknown Artist]')) == 'unknown artist'

    def test_various_artists_without_track_artist(self):
        assert mr.needs_repair(track(album_artist='Various Artists')) == 'Various Artists album with no track artist'

    def test_unknown_title(self):
        assert mr.needs_repair(track(title='Unknown Title')) == 'unknown title'
        assert mr.track_meta(track(title='Unknown Title'))['title'] == ''

    def test_real_compilation_is_left_alone(self):
        # A genuine compilation: album artist is Various Artists but every
        # track carries its own artist, so nothing is missing.
        assert mr.needs_repair(track(album_artist='Various Artists', track_artist='Daft Punk')) is None

    def test_track_artist_covers_missing_album_artist(self):
        assert mr.needs_repair(track(album_artist='', track_artist='Daft Punk')) is None


class TestLidarrResolution:
    def test_recovers_artist_from_known_album(self, monkeypatch):
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [
            {'artist': 'The Glitch Mob', 'album': 'Love Death Immortality'},
        ])
        meta = {'artist': '', 'album': 'Love Death Immortality', 'title': 'Skullclub'}
        assert mr.resolve_with_lidarr(meta) == {
            'artist': 'The Glitch Mob', 'album': 'Love Death Immortality', 'title': 'Skullclub',
        }

    def test_rejects_loose_catalogue_matches(self, monkeypatch):
        # Lidarr's search really does return "Love & Death" by Sentenced for
        # "Love Death Immortality"; that must not be accepted.
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [
            {'artist': 'Sentenced', 'album': 'Love & Death'},
        ])
        meta = {'artist': '', 'album': 'Love Death Immortality', 'title': 'Skullclub'}
        assert mr.resolve_with_lidarr(meta) is None

    def test_ambiguous_album_title_is_left_to_fingerprinting(self, monkeypatch):
        # Lidarr's search: "The Heist" is an album by nine artists and "CTRL
        # ESCAPE" by two. Taking the first hit would tag Macklemore's "Can't
        # Hold Us" as Alan Fitzpatrick.
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [
            {'artist': 'Alan Fitzpatrick', 'album': 'The Heist'},
            {'artist': 'Macklemore & Ryan Lewis', 'album': 'The Heist'},
        ])
        meta = {'artist': '', 'album': 'The Heist', 'title': "Can't Hold Us"}
        assert mr.resolve_with_lidarr(meta) is None

    def test_same_artist_listed_twice_is_not_ambiguous(self, monkeypatch):
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [
            {'artist': 'The Glitch Mob', 'album': 'Love Death Immortality'},
            {'artist': 'The Glitch Mob', 'album': 'Love Death Immortality (Deluxe)'},
        ])
        meta = {'artist': '', 'album': 'Love Death Immortality', 'title': 'Skullclub'}
        assert mr.resolve_with_lidarr(meta)['artist'] == 'The Glitch Mob'

    def test_known_artist_must_match_too(self, monkeypatch):
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [
            {'artist': 'Someone Else', 'album': 'Love Death Immortality'},
        ])
        meta = {'artist': 'The Glitch Mob', 'album': 'Love Death Immortality', 'title': 'Skullclub'}
        assert mr.resolve_with_lidarr(meta) is None

    def test_no_album_guess_from_title_alone(self, monkeypatch):
        # A release named after the song is usually the single; that guess is
        # left to pick_album() as a last resort, after fingerprinting.
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [{'artist': 'Coldplay', 'album': 'Yellow'}])
        assert mr.resolve_with_lidarr({'artist': 'Coldplay', 'album': '', 'title': 'Yellow'}) is None

    def test_nothing_to_search_with(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: calls.append(term) or [])
        assert mr.resolve_with_lidarr({'artist': '', 'album': '', 'title': 'Track 01'}) is None


class TestResolveOrder:
    def test_lidarr_first_skips_fingerprinting(self, monkeypatch):
        monkeypatch.setattr(mr, 'resolve_with_lidarr', lambda meta: {'artist': 'A', 'album': 'B', 'title': 'C'})
        monkeypatch.setattr(mr, 'resolve_with_audio_id', lambda fp, meta: (_ for _ in ()).throw(AssertionError('not reached')))
        assert mr.resolve('/x.mp3', {}) == ({'artist': 'A', 'album': 'B', 'title': 'C'}, 'lidarr')

    def test_falls_back_to_fingerprinting(self, monkeypatch):
        monkeypatch.setattr(mr, 'resolve_with_lidarr', lambda meta: None)
        monkeypatch.setattr(mr, 'resolve_with_audio_id', lambda fp, meta: {'artist': 'A', 'album': 'B', 'title': 'C'})
        assert mr.resolve('/x.mp3', {})[1] == 'audio fingerprint'

    def test_unresolved_when_both_fail(self, monkeypatch):
        monkeypatch.setattr(mr, 'resolve_with_lidarr', lambda meta: None)
        monkeypatch.setattr(mr, 'resolve_with_audio_id', lambda fp, meta: None)
        assert mr.resolve('/x.mp3', {}) == (None, '')

    def test_fingerprint_without_album_asks_lidarr_for_it(self, monkeypatch):
        import audio_id
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [])
        monkeypatch.setattr(
            audio_id, 'identify_track_by_audio_with_alternatives',
            lambda fp: ({'artist': 'Daft Punk', 'title': 'One More Time', 'album': ''}, None),
        )
        monkeypatch.setattr(mr, 'lidarr_album_for_track', lambda artist, title: 'Discovery')
        resolved = mr.resolve_with_audio_id('/x.mp3', {'artist': '', 'album': '', 'title': ''})
        assert resolved == {'artist': 'Daft Punk', 'title': 'One More Time', 'album': 'Discovery'}

    def test_partial_fingerprint_is_not_applied(self, monkeypatch):
        import audio_id
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [])
        monkeypatch.setattr(
            audio_id, 'identify_track_by_audio_with_alternatives',
            lambda fp: ({'artist': 'Daft Punk', 'title': 'One More Time', 'album': ''}, None),
        )
        monkeypatch.setattr(mr, 'lidarr_album_for_track', lambda artist, title: None)
        assert mr.resolve_with_audio_id('/x.mp3', {'artist': '', 'album': '', 'title': ''}) is None


class TestLibraryScan:
    def _section(self, albums):
        return SimpleNamespace(albums=lambda: albums)

    def _album(self, title, artist, tracks):
        return SimpleNamespace(title=title, parentTitle=artist, thumb='/library/metadata/1/thumb', tracks=lambda: tracks)

    def test_only_suspect_albums_and_missing_tracks_are_candidates(self):
        good = self._album('Discovery', 'Daft Punk', [track('1', album='Discovery', album_artist='Daft Punk')])
        comp = self._album('Hits', 'Various Artists', [
            track('2', album='Hits', album_artist='Various Artists', track_artist='Real Artist'),
            track('3', album='Hits', album_artist='Various Artists'),
        ])
        unknown = self._album('[Unknown Album]', 'Someone', [track('4', album='[Unknown Album]', album_artist='Someone')])
        found = mr.find_candidates(self._section([good, comp, unknown]), skip_keys=set(), limit=10)
        assert [(t.ratingKey, reason) for t, reason in found] == [
            ('3', 'Various Artists album with no track artist'),
            ('4', 'unknown album'),
        ]

    def test_skip_keys_and_limit(self):
        tracks = [track(str(i), album='[Unknown Album]') for i in range(5)]
        album = self._album('[Unknown Album]', 'X', tracks)
        found = mr.find_candidates(self._section([album]), skip_keys={'0', '1'}, limit=2)
        assert [t.ratingKey for t, _ in found] == ['2', '3']

    def test_repair_library_fixes_or_escalates(self, monkeypatch):
        import fix_metadata
        fixable = track('10', title='Skullclub', album='Love Death Immortality', album_artist='[Unknown Artist]')
        hopeless = track('11', title='Track 01', album='[Unknown Album]', album_artist='[Unknown Artist]')
        missing = track('12', title='Gone', album='[Unknown Album]', album_artist='X')
        monkeypatch.setattr(mr, 'find_candidates', lambda s, k, l: [
            (fixable, 'unknown artist'), (hopeless, 'unknown album'), (missing, 'unknown album'),
        ])
        monkeypatch.setattr(fix_metadata, '_file_path', lambda t: None if t is missing else f'/music/{t.ratingKey}.mp3')
        monkeypatch.setattr(mr, 'resolve', lambda fp, meta: (
            ({'artist': 'The Glitch Mob', 'album': meta['album'], 'title': meta['title']}, 'lidarr')
            if meta['album'] else (None, '')
        ))
        applied = []
        monkeypatch.setattr(mr, 'apply_fix', lambda t, fp, meta: applied.append((t.ratingKey, meta['artist'])) or True)

        stats, unresolved = mr.repair_library(section=None, skip_keys=set(), limit=10)

        # A Plex entry whose file is gone is stale (the file was just moved),
        # so it is skipped rather than handed to the admin.
        assert stats == {'found': 3, 'fixed': 1, 'unresolved': 1}
        # The hopeless track is escalated, and parked under "Unidentified" so
        # Plex stops filing it under some unrelated artist.
        assert applied == [('10', 'The Glitch Mob'), ('11', 'Unidentified')]
        assert [u['rating_key'] for u in unresolved] == ['11']
        assert 'not found by Lidarr or audio fingerprinting' in unresolved[0]['reason']
        assert all(u['source'] == 'library scan' for u in unresolved)

    def test_lidarr_outage_is_retried_not_escalated(self, monkeypatch):
        import fix_metadata
        t = track('20', title='Your Attention Please', album='[Unknown Album]', album_artist='Lupe Fiasco')
        monkeypatch.setattr(mr, 'find_candidates', lambda s, k, l: [(t, 'unknown album')])
        monkeypatch.setattr(fix_metadata, '_file_path', lambda t: '/music/x.mp3')
        monkeypatch.setattr(mr, 'file_tag_meta', lambda p: {})

        def resolve_during_outage(fp, meta):
            mr._service_errors += 1
            return None, ''

        monkeypatch.setattr(mr, 'resolve', resolve_during_outage)
        stats, unresolved = mr.repair_library(section=None, skip_keys=set(), limit=10)
        assert unresolved == [] and stats['unresolved'] == 0


class TestFolderHints:
    def test_folders_fill_unknown_fields(self):
        meta = {'artist': '', 'album': '', 'title': 'SHADOWS'}
        hinted = mr.folder_hints('/music/albums/John Summit/CTRL ESCAPE/SHADOWS.mp3', meta)
        assert hinted == {'artist': 'John Summit', 'album': 'CTRL ESCAPE', 'title': 'SHADOWS'}

    def test_noise_folders_are_ignored(self):
        meta = {'artist': '', 'album': '', 'title': 'Love And The Hunter'}
        assert mr.folder_hints('/music/albums/music/Unknown/Love And The Hunter.mp3', meta) is None

    def test_known_fields_are_not_overridden(self):
        meta = {'artist': 'Lupe Fiasco', 'album': 'Food & Liquor', 'title': 'Kick, Push'}
        assert mr.folder_hints('/music/albums/Other/Folder/x.mp3', meta) is None

    def test_hint_must_be_confirmed_by_lidarr(self, monkeypatch):
        seen = []
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: seen.append(term) or [])
        monkeypatch.setattr(mr, 'resolve_with_audio_id', lambda fp, meta: None)
        resolved, source = mr.resolve('/music/albums/Some Artist/Some Album/x.mp3',
                                      {'artist': '', 'album': '', 'title': 'x'})
        assert (resolved, source) == (None, '')
        assert seen == ['Some Artist Some Album']

    def test_confirmed_hint_resolves(self, monkeypatch):
        # Lidarr confirms the folder album; the known artist spelling is kept.
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [{'artist': 'N*E*R*D', 'album': 'Seeing Sounds'}]
                            if term == 'N.E.R.D Seeing Sounds' else [])
        resolved, source = mr.resolve('/music/albums/N.E.R.D/Seeing Sounds/Everyone Nose.mp3',
                                      {'artist': 'N.E.R.D', 'album': '', 'title': 'Everyone Nose'})
        assert source == 'lidarr (folder names)'
        assert resolved == {'artist': 'N.E.R.D', 'album': 'Seeing Sounds', 'title': 'Everyone Nose'}


class TestFilePathFallback:
    def _track(self, path, seconds=206):
        part = SimpleNamespace(file=path)
        return SimpleNamespace(media=[SimpleNamespace(parts=[part])], duration=seconds * 1000)

    def test_renamed_file_found_when_alone_in_folder(self, tmp_path, monkeypatch):
        import fix_metadata
        monkeypatch.setattr(fix_metadata, 'convert_host_path_to_container_path', lambda p: p)
        monkeypatch.setattr(mr, 'audio_duration', lambda p: 206.4)
        (tmp_path / 'renamed.mp3').write_bytes(b'')
        assert fix_metadata._file_path(self._track(str(tmp_path / 'old.mp3'))) == str(tmp_path / 'renamed.mp3')

    def test_a_different_song_left_alone_in_the_folder_is_not_it(self, tmp_path, monkeypatch):
        # "Step Up (Main Version)" (206 s) moved out; "Show Me the Money"
        # (227 s) is all that's left in the soundtrack folder.
        import fix_metadata
        monkeypatch.setattr(fix_metadata, 'convert_host_path_to_container_path', lambda p: p)
        monkeypatch.setattr(mr, 'audio_duration', lambda p: 227.0)
        (tmp_path / 'Show Me the Money.mp3').write_bytes(b'')
        assert fix_metadata._file_path(self._track(str(tmp_path / 'Step Up (Main Version).mp3'))) is None

    def test_no_guessing_between_several_songs(self, tmp_path, monkeypatch):
        # Picking one of several songs could write this track's tags onto a
        # different song.
        import fix_metadata
        monkeypatch.setattr(fix_metadata, 'convert_host_path_to_container_path', lambda p: p)
        (tmp_path / 'a.mp3').write_bytes(b'')
        (tmp_path / 'b.mp3').write_bytes(b'')
        assert fix_metadata._file_path(self._track(str(tmp_path / 'old.mp3'))) is None


class TestFillOnlyAndAgreement:
    """Fingerprint answers only fill what is unknown: they never replace
    known-correct artists or titles, nor fragment artist folders."""

    def _fingerprint(self, monkeypatch, *answers, lidarr_album=None):
        import audio_id
        monkeypatch.setattr(audio_id, 'identify_track_by_audio_with_alternatives', lambda fp: answers)
        monkeypatch.setattr(mr, 'lidarr_album_for_track', lambda artist, title: lidarr_album)
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [])

    def test_disagreeing_service_is_never_used(self, monkeypatch):
        # ACRCloud matched a different song; Shazam agreed with the tags but
        # had no album, and Lidarr has none either -> Manual Fix, not a guess.
        self._fingerprint(
            monkeypatch,
            {'artist': 'Charlène Juarez', 'title': 'First Love(R)', 'album': 'First Love(R)'},
            {'artist': 'Nobuya Kobori', 'title': 'Moonlight Sonata', 'album': ''},
        )
        meta = {'artist': 'Nobuya Kobori', 'album': '', 'title': 'Moonlight Sonata'}
        assert mr.resolve_with_audio_id('/x.mp3', meta) is None

    def test_second_service_used_when_it_agrees(self, monkeypatch):
        self._fingerprint(
            monkeypatch,
            {'artist': 'Kiasmos', 'title': 'Swept', 'album': 'Swept EP'},
            {'artist': 'Kings of Tomorrow', 'title': 'Finally (Inspired Edit) [Extended Mix]', 'album': 'Finally'},
        )
        meta = {'artist': 'Kings of Tomorrow', 'album': '', 'title': 'Finally (Inspired Edit) [Extended Mix]'}
        assert mr.resolve_with_audio_id('/x.mp3', meta) == {
            'artist': 'Kings of Tomorrow', 'album': 'Finally', 'title': 'Finally (Inspired Edit) [Extended Mix]',
        }

    def test_different_title_is_rejected(self, monkeypatch):
        self._fingerprint(monkeypatch, {'artist': 'Lupe Fiasco', 'title': 'Trials and Tribulations', 'album': 'Food & Liquor'})
        meta = {'artist': 'Lupe Fiasco', 'album': '', 'title': 'Failure'}
        assert mr.resolve_with_audio_id('/x.mp3', meta) is None

    def test_known_album_and_title_are_kept(self, monkeypatch):
        # Only the missing artist is filled; the single's album name from the
        # fingerprint must not replace the known album.
        self._fingerprint(monkeypatch, {'artist': 'John Summit/LAVINIA', 'title': 'SHADOWS', 'album': 'SHADOWS'})
        meta = {'artist': '', 'album': 'CTRL ESCAPE', 'title': 'SHADOWS'}
        assert mr.resolve_with_audio_id('/x.mp3', meta) == {'artist': 'John Summit', 'album': 'CTRL ESCAPE', 'title': 'SHADOWS'}

    def test_feat_credit_in_answer_title_still_agrees(self, monkeypatch):
        self._fingerprint(monkeypatch, {'artist': 'T.I., Young Dro, Young Dro', 'title': "SEE WH'AM SAYIN (feat. Young Dro)", 'album': 'KILL THE KING'})
        meta = {'artist': 'T.I.', 'album': '', 'title': "SEE WH'AM SAYIN"}
        assert mr.resolve_with_audio_id('/x.mp3', meta) == {'artist': 'T.I.', 'album': 'KILL THE KING', 'title': "SEE WH'AM SAYIN"}

    def test_lidarr_cannot_rename_a_known_artist(self, monkeypatch):
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [{'artist': 'N*E*R*D', 'album': 'Seeing Sounds'}]
                            if term == 'N.E.R.D Seeing Sounds' else [])
        resolved, _ = mr.resolve('/music/albums/N.E.R.D/Seeing Sounds/x.mp3',
                                 {'artist': 'N.E.R.D', 'album': '', 'title': 'Everyone Nose'})
        assert resolved['artist'] == 'N.E.R.D'

    def test_primary_artist(self):
        assert mr.primary_artist('T.I., Young Dro, Young Dro') == 'T.I.'
        assert mr.primary_artist('Macklemore & Ryan Lewis, Ray Dalton') == 'Macklemore & Ryan Lewis'
        assert mr.primary_artist('John Summit/LAVINIA') == 'John Summit'
        assert mr.primary_artist('N.E.R.D feat. Pusha T') == 'N.E.R.D'
        assert mr.primary_artist('Rauf & Faik') == 'Rauf & Faik'


def test_repair_library_dry_run_never_applies(monkeypatch):
    import fix_metadata
    t = track('1', album='[Unknown Album]')
    monkeypatch.setattr(mr, 'find_candidates', lambda s, k, l: [(t, 'unknown album')])
    monkeypatch.setattr(fix_metadata, '_file_path', lambda tr: '/x.mp3')
    monkeypatch.setattr(mr, 'resolve', lambda fp, meta: ({'artist': 'Artist', 'album': 'A', 'title': 'Song'}, 'lidarr'))
    monkeypatch.setattr(mr, 'apply_fix', lambda *a: (_ for _ in ()).throw(AssertionError('must not write')))
    stats, unresolved = mr.repair_library(None, set(), 10, dry_run=True)
    assert stats['fixed'] == 1 and unresolved == []



class TestPickAlbum:
    """Album choice, from the read-only evaluation against known songs."""

    def _catalogue(self, monkeypatch, owned, single=None):
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: owned)
        monkeypatch.setattr(mr, 'lidarr_album_for_track', lambda artist, title: single)

    def test_album_over_same_named_single(self, monkeypatch):
        # Eminem – Without Me belongs on The Eminem Show, as the fingerprint
        # says, not on the single.
        self._catalogue(monkeypatch, [{'artist': 'Eminem', 'album': 'The Eminem Show'}], single='Without Me')
        assert mr.pick_album('Eminem', 'Without Me',
                             ['The Eminem Show', 'Promo Only: Mainstream Radio, June 2002']) == 'The Eminem Show'

    def test_compilation_not_owned_by_artist_is_dropped(self, monkeypatch):
        # A compilation answer ("2012 Grammy Nominees" for Adele – Rolling in the Deep).
        self._catalogue(monkeypatch, [], single='Rolling in the Deep')
        assert mr.pick_album('Adele', 'Rolling in the Deep', ['2012 Grammy Nominees']) == 'Rolling in the Deep'

    def test_lidarr_confirmed_album_wins(self, monkeypatch):
        self._catalogue(monkeypatch, [{'artist': 'Adele', 'album': '21'}])
        assert mr.pick_album('Adele', 'Rolling in the Deep', ['Rolling in the Deep', '21']) == '21'

    def test_unconfirmed_real_album_beats_single(self, monkeypatch):
        self._catalogue(monkeypatch, [], single='Dreams')
        assert mr.pick_album('Fleetwood Mac', 'Dreams', ['Rumours (Super Deluxe)']) == 'Rumours (Super Deluxe)'

    def test_nothing_usable_falls_back_to_single_then_none(self, monkeypatch):
        self._catalogue(monkeypatch, [], single='Yellow')
        assert mr.pick_album('Coldplay', 'Yellow', []) == 'Yellow'
        self._catalogue(monkeypatch, [], single=None)
        assert mr.pick_album('Coldplay', 'Yellow', ['']) is None


def test_first_services_compilation_album_is_not_taken_directly(monkeypatch):
    # Daft Punk – One More Time must not get "[Hits of the '00s]": the first
    # answer's album goes through pick_album too.
    import audio_id
    monkeypatch.setattr(audio_id, 'identify_track_by_audio_with_alternatives', lambda fp: (
        {'artist': 'Daft Punk', 'title': 'One More Time', 'album': "Hits of the '00s"},
        {'artist': 'Daft Punk', 'title': 'One More Time', 'album': 'Discovery'},
    ))
    monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [])
    monkeypatch.setattr(mr, 'lidarr_album_for_track', lambda a, t: None)
    got = mr.resolve_with_audio_id('/x.mp3', {'artist': 'Daft Punk', 'album': '', 'title': 'One More Time'})
    assert got['album'] == 'Discovery'


def test_common_artist_is_order_independent():
    # SHADOWS is "John Summit" whichever fingerprint service answers first
    # ("John Summit" or "John Summit & LAVINIA").
    assert mr.common_artist(['John Summit', 'John Summit & LAVINIA']) == 'John Summit'
    assert mr.common_artist(['John Summit & LAVINIA', 'John Summit']) == 'John Summit'
    assert mr.common_artist(['Macklemore & Ryan Lewis', 'Macklemore & Ryan Lewis']) == 'Macklemore & Ryan Lewis'
    assert mr.common_artist(['Adele', 'Someone Else']) == 'Adele'


def test_album_owner_keeps_album_under_its_artist(monkeypatch):
    monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [
        {'artist': 'BFlecha', 'album': 'Ctrl.Escape'},
        {'artist': 'John Summit', 'album': 'CTRL ESCAPE'},
    ])
    assert mr.album_owner('CTRL ESCAPE', 'John Summit & LAVINIA') == 'John Summit'
    # An owner who isn't part of the credit is never used.
    assert mr.album_owner('CTRL ESCAPE', 'Someone Else') is None



def test_whole_word_artist_matching():
    # Lidarr lists The College Dropout under "Ye", which must not match
    # inside "Kanye West" as a substring.
    assert mr.name_within('John Summit', 'John Summit & LAVINIA')
    assert not mr.name_within('Ye', 'Kanye West')
    assert mr.album_owner.__doc__


def test_album_owner_ignores_substring_names(monkeypatch):
    monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [{'artist': 'Ye', 'album': 'The College Dropout'}])
    assert mr.album_owner('The College Dropout', 'Kanye West') is None


class TestLocalAIAlbum:
    """The AI only chooses among albums fingerprinting returned for the audio."""

    def _no_lidarr(self, monkeypatch):
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [])
        monkeypatch.setattr(mr, 'lidarr_album_for_track', lambda a, t: None)

    def test_ai_picks_among_the_options(self, monkeypatch):
        self._no_lidarr(monkeypatch)
        monkeypatch.setattr(mr, 'ask_local_ai', lambda s, p: {'album': 'Rumours', 'confident': True})
        assert mr.pick_album('Fleetwood Mac', 'Dreams', ['Dreams', 'Rumours']) == 'Rumours'

    def test_ai_cannot_introduce_its_own_album(self, monkeypatch):
        # A model can answer that "S&M" is on "Anti" (it's on "Loud").
        self._no_lidarr(monkeypatch)
        monkeypatch.setattr(mr, 'ask_local_ai', lambda s, p: {'album': 'Anti', 'confident': True})
        assert mr.pick_album('Rihanna', 'S&M', ['S&M - EP', 'S&M']) == 'S&M - EP'

    def test_unsure_ai_is_ignored(self, monkeypatch):
        self._no_lidarr(monkeypatch)
        monkeypatch.setattr(mr, 'ask_local_ai', lambda s, p: {'album': 'Rumours', 'confident': False})
        assert mr.pick_album('Fleetwood Mac', 'Dreams', ['Dreams', 'Rumours']) == 'Rumours'  # rules: album over single

    def test_ai_not_asked_when_rules_are_sure_or_no_choice(self, monkeypatch):
        monkeypatch.setattr(mr, 'ask_local_ai', lambda s, p: (_ for _ in ()).throw(AssertionError('not asked')))
        monkeypatch.setattr(mr, 'lidarr_lookup', lambda term: [{'artist': 'Adele', 'album': '21'}])
        assert mr.pick_album('Adele', 'Rolling in the Deep', ['21', 'Rolling in the Deep']) == '21'
        self._no_lidarr(monkeypatch)
        assert mr.pick_album('Coldplay', 'Yellow', ['Parachutes']) == 'Parachutes'

    def test_ai_down_changes_nothing(self, monkeypatch):
        monkeypatch.setenv('LOCAL_AI_API_BASE', 'http://127.0.0.1:9/v1')
        monkeypatch.setenv('LOCAL_AI_MODEL', 'x')

        def down(*a, **k):
            raise mr.requests.ConnectionError('down')

        monkeypatch.setattr(mr.requests, 'post', down)
        assert _REAL_ASK_LOCAL_AI('system', 'prompt') is None



class TestKnownMetadataComesFromTheFile:
    """Plex files an untagged-album track into a nameless album owned by some
    unrelated artist; that artist must not be treated as known."""

    def test_file_tags_beat_plexs_album_artist(self, monkeypatch):
        t = track('64831', title='Unknown Title', album='[Unknown Album]', album_artist='Hans Zimmer')
        monkeypatch.setattr(mr, 'file_tag_meta', lambda p: {'artist': 'Grateful Dead', 'album': '', 'title': ''})
        monkeypatch.setattr(mr, 'file_artist_tag', lambda p: 'Grateful Dead')   # a readable file
        meta = mr.known_meta(t, '/music/albums/Grateful Dead/Built to Last (1989)/Foolish Heart.mp3')
        # No title tag: the file name supplies it.
        assert meta == {'artist': 'Grateful Dead', 'album': '', 'title': 'Foolish Heart'}

    def test_plex_album_artist_ignored_when_album_unknown_and_file_untagged(self, monkeypatch):
        t = track('41949', title='', album='[Unknown Album]', album_artist='Three Days Grace')
        monkeypatch.setattr(mr, 'file_tag_meta', lambda p: {})
        assert mr.known_meta(t, '/music/x.mp3')['artist'] == ''

    def test_plex_album_artist_used_when_album_is_known(self, monkeypatch):
        t = track('5', title='Skullclub', album='Love Death Immortality', album_artist='The Glitch Mob')
        monkeypatch.setattr(mr, 'file_tag_meta', lambda p: {})
        assert mr.known_meta(t, '/music/x.mp3')['artist'] == 'The Glitch Mob'


class TestCollaborationCredits:
    def test_one_credited_artist_agrees(self):
        assert mr.artists_agree('Kid Cudi', 'Kid Cudi & Eminem')
        assert mr.artists_agree('Macklemore', 'Macklemore & Ryan Lewis')
        assert mr.artists_agree('KAROL G', 'KAROL G, Drake')
        assert mr.artists_agree('Yeat', 'Yeat feat. Drake')

    def test_different_artists_still_disagree(self):
        assert not mr.artists_agree('Hans Zimmer', 'Grateful Dead')
        assert not mr.artists_agree('Kings of Tomorrow', 'Kiasmos')

    def test_credited_artists_split(self):
        assert mr.credited_artists('Kid Cudi & Eminem') == ['Kid Cudi', 'Eminem']
        assert mr.credited_artists('Tyler x Kali Uchis') == ['Tyler', 'Kali Uchis']


class TestMusicBrainzLengthMatch:
    def _search(self, recordings):
        import types
        fake = types.SimpleNamespace(set_useragent=lambda *a: None,
                                     search_recordings=lambda **k: {'recording-list': recordings})
        return fake

    def test_album_from_recording_of_the_same_length(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, 'musicbrainzngs', self._search([
            {'title': 'Failure', 'artist-credit-phrase': 'Lupe Fiasco', 'length': '139000',
             'release-list': [{'title': 'Chi-Town Guevara'}]},
            {'title': 'Failure', 'artist-credit-phrase': 'Lupe Fiasco', 'length': '297000',
             'release-list': [{'title': 'Fahrenheit 1/15, Part 1: The Truth Is Among Us'}]},
        ]))
        monkeypatch.setattr(mr, 'audio_duration', lambda p: 297.4)
        monkeypatch.setattr(mr, 'lidarr_confirms_album', lambda a, b: False)
        monkeypatch.setattr(mr, 'ai_choose_album', lambda *a: None)
        got = mr.resolve_with_musicbrainz('/x.mp3', {'artist': 'Lupe Fiasco', 'album': '', 'title': 'Failure'})
        assert got == {'artist': 'Lupe Fiasco', 'album': 'Fahrenheit 1/15, Part 1: The Truth Is Among Us', 'title': 'Failure'}

    def test_closest_length_wins(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, 'musicbrainzngs', self._search([
            {'title': 'Failure', 'artist-credit-phrase': 'Lupe Fiasco', 'length': '296000',
             'release-list': [{'title': 'Lupe Fiasco’s Food & Liquor'}]},
            {'title': 'Failure', 'artist-credit-phrase': 'Lupe Fiasco', 'length': '297000',
             'release-list': [{'title': 'Fahrenheit 1/15, Part 1: The Truth Is Among Us'}]},
        ]))
        monkeypatch.setattr(mr, 'audio_duration', lambda p: 297.4)
        monkeypatch.setattr(mr, 'lidarr_confirms_album', lambda a, b: False)
        monkeypatch.setattr(mr, 'ai_choose_album', lambda *a: None)
        got = mr.resolve_with_musicbrainz('/x.mp3', {'artist': 'Lupe Fiasco', 'album': '', 'title': 'Failure'})
        assert got['album'] == 'Fahrenheit 1/15, Part 1: The Truth Is Among Us'

    def test_no_recording_of_that_length_means_no_answer(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, 'musicbrainzngs', self._search([
            {'title': 'Freestyle', 'artist-credit-phrase': 'Lil Baby', 'length': '100000',
             'release-list': [{'title': 'Some Other Freestyle'}]},
        ]))
        monkeypatch.setattr(mr, 'audio_duration', lambda p: 180.0)
        assert mr.resolve_with_musicbrainz('/x.mp3', {'artist': 'Lil Baby', 'album': '', 'title': 'Freestyle'}) is None

    def test_known_album_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(mr, 'audio_duration', lambda p: pytest.fail('should not look up'))
        assert mr.resolve_with_musicbrainz('/x.mp3', {'artist': 'A', 'album': 'Real Album', 'title': 'T'}) is None


class TestAlbumTagThatRepeatsTheArtist:
    def _tags(self, monkeypatch, **tags):
        import types
        fake = {k: [v] for k, v in tags.items()}
        monkeypatch.setitem(__import__('sys').modules, 'mutagen',
                            types.SimpleNamespace(File=lambda p, easy=False: fake))

    def test_album_equal_to_artist_is_treated_as_empty(self, monkeypatch):
        self._tags(monkeypatch, artist='Lupe Fiasco', album='Lupe Fiasco', title='Failure')
        assert mr.file_tag_meta('/x.mp3') == {'artist': 'Lupe Fiasco', 'album': '', 'title': 'Failure'}

    def test_real_album_is_kept(self, monkeypatch):
        self._tags(monkeypatch, artist='Lupe Fiasco', album='Tetsuo & Youth', title='Mural')
        assert mr.file_tag_meta('/x.mp3')['album'] == 'Tetsuo & Youth'
