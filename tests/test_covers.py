"""Covers: a requested cover offers the official song first, covers below it."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import covers
import request_choice as rc
from downloader import Downloader


def _hit(track_id, artist, title, album='', duration=200, rank=1000):
    return {'id': track_id, 'artist': {'name': artist}, 'title': title, 'album': {'title': album},
            'duration': duration, 'rank': rank}


OFFICIAL = _hit(1, 'Example Composer', 'Nocturne', 'Example Show Original Soundtrack 3', 223, 380000)
MINE = _hit(2, 'Studio Band', 'Nocturne', 'Example Show, Vol. 2', 224, 160000)
GUITAR = _hit(3, 'Guitar Person', 'Nocturne (From Example Show)', 'Guitar Covers #2', 228, 74000)
REMIX = _hit(4, 'Example Composer', 'Nocturne (Remix)', 'Example Show Original Soundtrack 3', 223, 500000)
KARAOKE = _hit(5, 'Sing Along', 'Nocturne', 'Karaoke Hits', 223, 600000)
UNRELATED = _hit(6, 'Somebody', 'Nocturne (From "Other Game")', 'Other Game Songs', 169, 146000)
ALL = [REMIX, KARAOKE, MINE, UNRELATED, GUITAR, OFFICIAL]


def _deezer(*hits):
    return lambda path, **params: {'data': list(hits)}


def _find(title, performer, video_title, uploader='', description='', duration=0, hits=ALL):
    return covers.find(title, performer, video_title, uploader, description, duration, get=_deezer(*hits))


def _ids(tracks):
    return [t['id'] for t in tracks]


class TestDetecting:
    @pytest.mark.parametrize('title, uploader', [
        ('Nocturne「Example Show」Epic Orchestral Cover', 'Someone'),
        ('Nocturne (From "Example Show")', "Someone's Covers"),
        ('Creepy Song (Famous Band cover)', 'Someone'),
        ('Famous Band - Creepy Song (Cover by Someone)', 'Someone'),
    ])
    def test_a_video_that_says_so(self, title, uploader):
        assert covers.clues(title, uploader)['says']

    @pytest.mark.parametrize('title, uploader', [
        ('Artist - Song (Official Audio)', 'Artist'), ('Artist - Song (Album Cover)', 'Artist'),
        ('Artist - Song (Cover Art)', 'Artist'), ('Discover', 'Recovery Records'), ('', '')])
    def test_nothing_says_so(self, title, uploader):
        assert covers.clues(title, uploader) == {}
        assert covers.context_fields(title, uploader) == []
        assert _find('Song', 'Artist', title, uploader) is None

    def test_the_work_a_title_names(self):
        assert covers._work('Nocturne (From "Example Show: The War")') == 'Example Show: The War'
        assert covers._work('Band - Nocturne (Example Show OST)') == 'Example Show'
        assert covers._work('Theme「Example Show Episode 4 OST」Epic Cover') == 'Example Show'
        assert covers._work('Nocturne (Live)') == ''

    def test_a_soundtrack_named_between_dashes(self):
        assert covers._work('Example Show OST 3 - Track 18 - Nocturne') == 'Example Show'
        assert covers._work('Example Show OST- Night Theme') == 'Example Show'
        assert covers._work('Artist - Song - Live') == ''
        assert covers.song_names('Track 18 - Nocturne', 'Example Show OST 3 - Track 18 - Nocturne') == ['Nocturne']
        assert covers.song_names('Soundtrack 2 My Life', 'Artist - Soundtrack 2 My Life (Cover)')[0] == 'Soundtrack 2 My Life'
        assert covers.song_names('The Score')[0] == 'The Score'

    def test_the_songs_own_name(self):
        assert covers.song_names('Heat「Show Episode 7 OST」Epic Orchestral Cover')[0] == 'Heat'
        assert covers.song_names('Them vs Us Theme「Show OST」＜Hollow Remaster＞ Epic Rock Cover') == ['Them vs Us Theme', 'Hollow']

    def test_who_the_video_credits(self):
        assert covers.credited('Creepy Song (Famous Band acoustic cover)')[0] == 'Famous Band'
        assert 'Famous Band' in covers.credited('Famous Band - Creepy Song (Cover by Someone)')
        assert covers.credited('Theme (From "Show")', 'Released on: 1\nComposer: Example Composer') == ['Example Composer']


class TestOfficialSong:
    def test_the_soundtrack_recording_comes_first(self):
        found = _find('Nocturne', 'Studio Band', 'Studio Band - Nocturne (Example Show OST)', 'Studio Band', duration=224)
        assert _ids(found['official']) == ['1'] and found['sure']
        assert _ids(found['covers']) == ['2', '5', '3']                    # the linked one, then by popularity
        assert [t['linked'] for t in found['covers']] == [True, False, False]

    def test_another_works_song_is_not_a_cover_of_it(self):
        found = _find('Nocturne', 'Studio Band', 'Studio Band - Nocturne (Example Show OST)', 'Studio Band')
        assert '6' not in _ids(found['official'] + found['covers'])

    def test_an_artist_on_the_soundtrack_is_no_cover(self):
        theirs = _hit(7, 'Studio Band', 'Nocturne', 'Example Show Original Soundtrack 3', 224, 90)
        assert _find('Nocturne', 'Studio Band', 'Nocturne (From "Example Show")', 'Studio Band', hits=[OFFICIAL, theirs]) is None
        assert _find('Nocturne', 'Example Composer', 'Nocturne (Example Show OST)', 'Example Composer - Topic') is None

    def test_a_fans_upload_of_the_official_recording_is_no_cover(self):
        assert _find('Nocturne', 'Some Fan', 'Nocturne (Example Show OST Extended)', 'Some Fan', hits=[OFFICIAL, GUITAR]) is None

    def test_a_fans_upload_at_the_soundtracks_length_is_that_recording(self):
        found = _find('Track 18 - Nocturne', 'Example Show OST 3', 'Example Show OST 3 - Track 18 - Nocturne',
                      'Some Fan', duration=225, hits=[OFFICIAL, GUITAR])
        assert _ids(found['official']) == ['1'] and found['reupload'] and found['covers'] == []
        # Another length: an edit or a medley, not that recording.
        assert _find('Track 18 - Nocturne', 'Example Show OST 3', 'Example Show OST 3 - Track 18 - Nocturne',
                     'Some Fan', duration=400, hits=[OFFICIAL, GUITAR]) is None
        # The performer's own release of it: a cover after all.
        assert 'reupload' not in _find('Nocturne', 'Studio Band', 'Studio Band - Nocturne (Example Show OST)',
                                       'Studio Band', duration=224)

    def test_no_official_song_is_nothing_to_ask(self):
        assert _find('Nocturne', 'Studio Band', 'Nocturne Epic Cover', 'Studio Band', hits=[MINE, KARAOKE, REMIX]) is None

    def test_the_credited_artist_is_the_official_one(self):
        band = _hit(8, 'Famous Band', 'Creepy Song', 'Debut', 238, 900)
        other = _hit(9, 'Bigger Band', 'Creepy Song', 'Other', 100, 99999)
        found = _find('Creepy Song', 'Someone', 'Someone - Creepy Song (Famous Band cover)', 'Someone', hits=[other, band])
        assert _ids(found['official']) == ['8'] and found['sure']

    def test_popularity_alone_is_only_likely(self):
        popular, small = _hit(8, 'Famous Band', 'Creepy Song', 'Debut', 238, 900000), _hit(9, 'Tiny', 'Creepy Song', 'X', 100, 500)
        found = _find('Creepy Song', 'Someone', 'Creepy Song piano cover', 'Someone', hits=[small, popular])
        assert _ids(found['official']) == ['8'] and not found['sure']
        close = _hit(9, 'Rival', 'Creepy Song', 'X', 100, 500000)
        assert _find('Creepy Song', 'Someone', 'Creepy Song piano cover', 'Someone', hits=[close, popular]) is None


LINK = 'https://www.youtube.com/watch?v=vid00000001'
CONTEXT = f'source=youtube_playlist; url={LINK}; video_duration=224s'
VIDEO = {covers.TITLE_FIELD: 'Studio Band - Nocturne (Example Show OST)', covers.UPLOADER_FIELD: 'Studio Band'}


class TestSettling:
    @pytest.fixture(autouse=True)
    def deezer(self, monkeypatch):
        monkeypatch.setattr(covers.find, '__defaults__', ('', '', 0, _deezer(*ALL)))
        monkeypatch.setattr(rc, 'can_ask', lambda: True)

    def _settle(self, monkeypatch, answer, context=CONTEXT, video=VIDEO, url=LINK):
        self.asked = []
        monkeypatch.setattr(rc, 'ask', lambda request, source, options, next_step, **more:
                            (self.asked.append({'options': options, **more}), answer)[1])
        return Downloader.__new__(Downloader)._settle_cover('Studio Band', 'Nocturne', url, context, dict(video))

    def test_the_official_song_heads_the_list(self, monkeypatch):
        settled = self._settle(monkeypatch, 0)
        [question] = self.asked
        assert [o['label'] for o in question['options']][:2] == ['Example Composer - Nocturne', 'Studio Band - Nocturne']
        assert question['options'][0]['detail'].startswith('Official · Example Show Original Soundtrack 3')
        assert question['options'][1]['detail'].startswith('Cover, the one you linked')
        assert question['cover'] and question['fallback'] == 'the official song'
        assert (settled['artist'], settled['title']) == ('Example Composer', 'Nocturne')
        assert settled['context'] == 'source=youtube_playlist; album=Example Show Original Soundtrack 3; deezer_track=1'

    def test_a_cover_can_be_chosen(self, monkeypatch):
        assert self._settle(monkeypatch, 1)['context'].endswith('deezer_track=2')

    def test_the_linked_video_itself_can_be_chosen(self, monkeypatch):
        monkeypatch.setattr(covers.find, '__defaults__', ('', '', 0, _deezer(OFFICIAL, GUITAR)))
        video = {covers.TITLE_FIELD: 'Nocturne (Example Show OST) Epic Cover', covers.UPLOADER_FIELD: 'Studio Band'}
        settled = self._settle(monkeypatch, 1, video=video)
        assert self.asked[0]['options'][1]['detail'].startswith('Cover, the video you linked')
        assert settled == {'context': CONTEXT + '; linked_video=first'}
        d = Downloader.__new__(Downloader)
        assert d._link_first(['deemix', 'soundcloud', 'yt-dlp'], LINK, settled['context']) == ['yt-dlp', 'deemix', 'soundcloud']

    def test_no_answer_takes_the_official_song(self, monkeypatch):
        assert self._settle(monkeypatch, None)['context'].endswith('deezer_track=1')

    def test_none_of_them_goes_on_as_requested(self, monkeypatch):
        assert self._settle(monkeypatch, rc.NEXT) is None

    def test_nobody_to_ask_takes_the_official_song(self, monkeypatch):
        monkeypatch.setattr(rc, 'can_ask', lambda: False)
        assert self._settle(monkeypatch, 1)['context'].endswith('deezer_track=1') and not self.asked

    def test_a_likely_original_is_only_taken_when_chosen(self, monkeypatch):
        popular = _hit(8, 'Famous Band', 'Nocturne', 'Debut', 238, 900000)
        monkeypatch.setattr(covers.find, '__defaults__', ('', '', 0, _deezer(popular)))
        video = {covers.TITLE_FIELD: 'Nocturne piano cover', covers.UPLOADER_FIELD: 'Studio Band'}
        assert self._settle(monkeypatch, None, video=video) is None
        assert self.asked[0]['options'][0]['detail'].startswith('Likely the original')
        assert self._settle(monkeypatch, 0, video=video)['context'].endswith('deezer_track=8')

    def test_a_message_asking_for_the_cover_is_honoured(self, monkeypatch):
        assert self._settle(monkeypatch, 0, CONTEXT + f'; message={LINK} the cover please') is None and not self.asked
        assert self._settle(monkeypatch, 0, CONTEXT + '; message=https://example.com/cover')['artist'] == 'Example Composer'

    def test_a_fans_upload_is_filed_as_the_recording_without_a_question(self, monkeypatch):
        monkeypatch.setattr(covers.find, '__defaults__', ('', '', 0, _deezer(OFFICIAL, GUITAR)))
        video = {covers.TITLE_FIELD: 'Example Show OST 3 - Track 18 - Nocturne', covers.UPLOADER_FIELD: 'Some Fan'}
        self.asked = []
        monkeypatch.setattr(rc, 'ask', lambda *a, **k: self.asked.append(1))
        settled = Downloader.__new__(Downloader)._settle_cover('Example Show OST 3', 'Track 18 - Nocturne', LINK,
                                                               CONTEXT, video)
        assert (settled['artist'], settled['title']) == ('Example Composer', 'Nocturne') and not self.asked
        assert settled['context'].endswith('deezer_track=1')

    def test_an_exact_deezer_track_is_left_alone(self, monkeypatch):
        assert self._settle(monkeypatch, 0, CONTEXT + '; deezer_track=99') is None and not self.asked


def test_the_context_fields_come_off_again():
    fields = covers.context_fields('Nocturne (From "Example Show")', 'Band; Two', 'Composer: Example Composer')
    context, taken = covers.take_fields('; '.join(['source=youtube_playlist', f'url={LINK}'] + fields))
    assert context == f'source=youtube_playlist; url={LINK}'
    assert taken == {covers.TITLE_FIELD: 'Nocturne (From "Example Show")', covers.UPLOADER_FIELD: 'Band, Two',
                     covers.NOTES_FIELD: 'Composer: Example Composer'}
    assert covers.take_fields(None) == (None, {})


def test_covers_come_last_in_any_question(monkeypatch):
    shown = {}
    monkeypatch.setattr(rc, 'ask', lambda request, source, options, next_step, suggestion=None: shown.update(options=options))
    candidates = [{'artist': 'Sing Along', 'title': 'Song One (Karaoke)', 'album': ''},
                  {'artist': 'Artist', 'title': 'Song One More', 'album': 'Covers'}, {'artist': 'Artist', 'title': 'Song One Two', 'album': 'Debut'}]
    d = Downloader.__new__(Downloader)
    d._ask_local_ai = lambda *a: {}
    d._choose_or_ask('Artist', 'Song One', candidates, '', 'Deezer')
    assert [o['label'] for o in shown['options']][0] == 'Artist - Song One Two'


def test_the_question_says_it_is_a_cover():
    import discord_bot
    question = {'request': 'Studio Band - Nocturne', 'source': 'Cover', 'options': [{'label': 'A', 'detail': 'Official'}],
                'cover': True, 'fallback': 'the official song', 'timeout': 900}
    text = discord_bot._choice_text(question)
    assert 'is a cover' in text and "I'll take the official song" in text
    assert "couldn't tell" in discord_bot._choice_text({**question, 'cover': False, 'fallback': ''})


def test_a_playlist_song_carries_its_videos_words():
    import discord_bot
    track = {'url': LINK, 'cover': covers.context_fields('Nocturne (Example Show OST)', 'Band')}
    assert 'cover_title=Nocturne (Example Show OST)' in discord_bot.playlist_track_context('youtube', track)
    assert 'cover_' not in discord_bot.playlist_track_context('youtube', {**track, 'deezer_track': '5'})
