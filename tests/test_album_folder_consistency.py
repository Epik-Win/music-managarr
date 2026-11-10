"""One requested album must land in exactly one folder."""
import os

from downloader import Downloader


def test_embedded_album_survives_an_artist_or_title_override():
    # deemix embedded "Drink the Sea"; the lookup that produced the request
    # context spelled it "Drink The Sea". Overriding the title must not drag
    # the album along.
    d = Downloader.__new__(Downloader)
    merged = d._merge_request_context_metadata(
        external_metadata={'artist': 'The Glitch Mob', 'title': 'Between Two Points (feat. Swan)',
                           'album': 'Drink the Sea', 'source': 'deemix'},
        request_context_metadata={'artist': 'The Glitch Mob', 'title': 'Between Two Points',
                                  'album': 'The Glitch Mob - Drink The Sea'},
        artist='The Glitch Mob', title='Between Two Points',
        album='The Glitch Mob - Drink The Sea',
    )
    assert merged['title'] == 'Between Two Points'
    assert merged['album'] == 'Drink the Sea'


def test_missing_album_is_still_filled_from_the_request():
    d = Downloader.__new__(Downloader)
    merged = d._merge_request_context_metadata(
        external_metadata={'artist': 'A', 'title': 'B', 'album': ''},
        request_context_metadata={'artist': 'A', 'title': 'B', 'album': 'Some Album'},
        artist='A', title='B', album='Some Album',
    )
    assert merged['album'] == 'Some Album'


def test_existing_folder_is_reused_across_spellings(tmp_path):
    d = Downloader.__new__(Downloader)
    (tmp_path / 'Drink the Sea').mkdir()
    assert d._existing_dir_match(str(tmp_path), 'Drink The Sea') == 'Drink the Sea'
    assert d._existing_dir_match(str(tmp_path), 'Drink  the   Sea!') == 'Drink the Sea'
    assert d._existing_dir_match(str(tmp_path), 'Love Death Immortality') == 'Love Death Immortality'
    assert d._existing_dir_match(str(tmp_path / 'nope'), 'Anything') == 'Anything'


def test_second_track_joins_the_first_tracks_album_folder(tmp_path):
    d = Downloader.__new__(Downloader)
    library = tmp_path / 'albums'

    first = tmp_path / 'one.mp3'
    first.write_bytes(b'x')
    d._organize_downloaded_file(
        str(first), str(library),
        {'artist': 'The Glitch Mob', 'title': 'We Swarm', 'album': 'Drink the Sea'},
        'The Glitch Mob', 'We Swarm', 'Drink the Sea', None, {})

    second = tmp_path / 'two.mp3'
    second.write_bytes(b'x')
    out = d._organize_downloaded_file(
        str(second), str(library),
        {'artist': 'The Glitch Mob', 'title': 'Between Two Points', 'album': 'Drink The Sea'},
        'The Glitch Mob', 'Between Two Points', 'Drink The Sea', None, {})

    albums = sorted(p.name for p in (library / 'The Glitch Mob').iterdir())
    assert albums == ['Drink the Sea']
    assert out.endswith('Drink the Sea/Between Two Points.mp3')


def test_artist_prefix_is_stripped_from_album_names():
    d = Downloader.__new__(Downloader)
    assert d._clean_album_name('Quadeca - SCRAPYARD', 'Quadeca') == 'SCRAPYARD'
    assert d._clean_album_name('The Glitch Mob - Drink The Sea', 'The Glitch Mob') == 'Drink The Sea'
    # Case differences between sources must not defeat it.
    assert d._clean_album_name('quadeca - SCRAPYARD', 'Quadeca') == 'SCRAPYARD'
    # An album genuinely containing " - " is left alone.
    assert d._clean_album_name('Songs - Volume 2', 'Quadeca') == 'Songs - Volume 2'
    assert d._clean_album_name('SCRAPYARD', 'Quadeca') == 'SCRAPYARD'
    assert d._clean_album_name('', 'Quadeca') == ''


def test_enriched_metadata_album_is_clean():
    d = Downloader.__new__(Downloader)
    d._read_embedded_tags = lambda *a, **k: None
    d.get_metadata_from_sources = lambda **k: None
    meta = d._enrich_downloaded_metadata(
        '/tmp/x.mp3', 'deemix',
        {'artist': 'Quadeca', 'title': 'TEXAS BLUE', 'album': 'Quadeca - SCRAPYARD'},
        {}, 'Quadeca', 'TEXAS BLUE', 'Quadeca - SCRAPYARD', 0)
    assert meta['album'] == 'SCRAPYARD'


def test_deemix_prefers_the_release_the_request_belongs_to():
    # DUSTCUTTER is on both the 15-track album and the 3-track EP; with the
    # album known, the album release must win.
    d = Downloader.__new__(Downloader)
    d._has_variant_request_hint = lambda ctx: False
    d._is_official_youtube_request = lambda *a, **k: False
    d._select_best_candidate_with_ai = lambda **k: None

    class FakeDZ:
        class gw:
            @staticmethod
            def search_music(q, type='TRACK', limit=5):
                return {'data': [
                    {'SNG_ID': '1', 'SNG_TITLE': 'DUSTCUTTER', 'ART_NAME': 'Quadeca', 'ALB_TITLE': 'SCRAPYARD II'},
                    {'SNG_ID': '2', 'SNG_TITLE': 'DUSTCUTTER', 'ART_NAME': 'Quadeca', 'ALB_TITLE': 'SCRAPYARD'},
                ]}

    picked = d._deemix_pick_track(FakeDZ(), 'Quadeca - DUSTCUTTER', None, 'Quadeca', 'DUSTCUTTER',
                                  expected_album='SCRAPYARD')
    assert picked['ALB_TITLE'] == 'SCRAPYARD'

    # Without the hint the ranking is unchanged (first best-scoring result).
    picked = d._deemix_pick_track(FakeDZ(), 'Quadeca - DUSTCUTTER', None, 'Quadeca', 'DUSTCUTTER')
    assert picked['ALB_TITLE'] == 'SCRAPYARD II'


def _write_mp3(path):
    """A minimal real MP3 so mutagen can attach tags."""
    import base64
    path.write_bytes(base64.b64decode(
        'SUQzAwAAAAAAAP/7kMQAAAAAAAAAAAAAAAAAAAAAAEluZm8AAAAPAAAAAgAAAbAAgICAgICAgICA'
        'gICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgP////////////8='
    ) + b'\x00' * 2048)


def test_album_and_album_artist_follow_the_folder(tmp_path):
    from mutagen.easyid3 import EasyID3
    d = Downloader.__new__(Downloader)
    library = tmp_path / 'albums'

    src = tmp_path / 'in.mp3'
    _write_mp3(src)
    tags = EasyID3()
    # What deemix embedded: every contributor credited, album spelled its way.
    tags['album'] = 'Drink the Sea'
    tags['albumartist'] = 'The Glitch Mob/Superposition'
    tags.save(str(src))

    out = d._organize_downloaded_file(
        str(src), str(library),
        {'artist': 'The Glitch Mob', 'title': 'Starve the Ego', 'album': 'Drink the Sea'},
        'The Glitch Mob', 'Starve the Ego', 'Drink the Sea', None, {})

    written = EasyID3(out)
    assert written['album'] == ['Drink the Sea']
    assert written['albumartist'] == ['The Glitch Mob']


def test_a_slash_in_the_album_survives_in_the_tag(tmp_path):
    from mutagen.easyid3 import EasyID3
    d = Downloader.__new__(Downloader)
    library = tmp_path / 'albums'
    src = tmp_path / 'in.mp3'
    _write_mp3(src)

    meta = {'artist': 'Bizarrap', 'title': 'Session', 'album': 'Bzrp Music Sessions, Vol. 52/66'}
    out = d._organize_downloaded_file(str(src), str(library), meta,
                                      'Bizarrap', 'Session', meta['album'], None, {})

    # The directory cannot hold a slash; the tag can and should.
    assert 'Bzrp Music Sessions, Vol. 52_66' in out
    assert EasyID3(out)['album'] == ['Bzrp Music Sessions, Vol. 52/66']
    assert meta['album'] == 'Bzrp Music Sessions, Vol. 52/66'


class _FoxDZ:
    """Deezer's search for "Ylvis - The Fox": every version has the same
    SNG_TITLE; only VERSION tells the acapella and instrumental apart."""
    class gw:
        @staticmethod
        def search_music(q, type='TRACK', limit=5):
            base = {'ART_NAME': 'Ylvis', 'ALB_TITLE': 'The Fox (What Does the Fox Say?)',
                    'SNG_TITLE': 'The Fox (What Does the Fox Say?)'}
            return {'data': [dict(base, SNG_ID='71348770', VERSION='(Acapella)'),
                             dict(base, SNG_ID='71348769', VERSION='(Instrumental)'),
                             dict(base, SNG_ID='71348768', VERSION='(Extended Mix)'),
                             dict(base, SNG_ID='70054724', VERSION='')]}


def test_a_plain_request_never_gets_another_version_of_the_song():
    d = Downloader.__new__(Downloader)
    d._select_best_candidate_with_ai = lambda **kw: (_ for _ in ()).throw(AssertionError('no choice to make'))
    picked = d._deemix_pick_track(_FoxDZ(), 'Ylvis - The Fox', 'video_title=Ylvis - The Fox [Official music video HD]',
                                  'Ylvis', 'The Fox')
    assert picked['SNG_ID'] == '70054724'


def test_a_requested_version_is_still_found():
    d = Downloader.__new__(Downloader)
    picked = d._deemix_pick_track(_FoxDZ(), 'Ylvis - The Fox (Instrumental)', None, 'Ylvis', 'The Fox (Instrumental)')
    assert picked['SNG_ID'] == '71348769'


def test_a_vocals_only_file_does_not_pass_as_the_song(tmp_path):
    d = Downloader.__new__(Downloader)
    f = tmp_path / 'Ylvis - The Fox (Acapella).mp3'
    f.write_bytes(b'')
    d._extract_artist_title_from_file = lambda path: ('Ylvis', 'The Fox (What Does the Fox Say?) (Acapella)')
    assert d._is_download_match(str(f), 'Ylvis', 'The Fox') is False
    assert d._is_download_match(str(f), 'Ylvis', 'The Fox (Acapella)') is True
    assert Downloader._variants_of('Strobe (Original Mix)') == set()


class _LiveDZ:
    class gw:
        @staticmethod
        def search_music(q, type='TRACK', limit=5):
            base = {'ART_NAME': 'Adele', 'ALB_TITLE': 'x', 'SNG_TITLE': 'Hello'}
            return {'data': [dict(base, SNG_ID='1', VERSION='', DURATION='295'),
                             dict(base, SNG_ID='2', VERSION='(Live at the BBC)', DURATION='301'),
                             dict(base, SNG_ID='3', VERSION='(Live from NYC)', DURATION='318')]}


def test_a_live_video_gets_a_live_version_and_the_ai_sees_the_lengths():
    d = Downloader.__new__(Downloader)
    asked = []

    def choose(**kw):
        asked.append(kw['candidates'])
        return kw['candidates'][1]
    d._select_best_candidate_with_ai = choose
    ctx = 'video_title=Adele - Hello | Live from NYC; video_duration=318s'
    picked = d._deemix_pick_track(_LiveDZ(), 'Adele - Hello', ctx, 'Adele', 'Hello')
    assert [c['raw']['SNG_ID'] for c in asked[0]] == ['2', '3']          # the studio version is out
    assert 'length=5:18' in Downloader._describe_candidates(asked[0])
    assert picked['SNG_ID'] == '3'


def test_a_version_deezer_lacks_leaves_it_to_the_next_tool():
    d = Downloader.__new__(Downloader)
    ctx = 'video_title=Ylvis - The Fox (Live at Wembley)'
    assert d._deemix_pick_track(_FoxDZ(), 'Ylvis - The Fox', ctx, 'Ylvis', 'The Fox') is None


def test_the_final_check_wants_the_version_the_request_names(tmp_path):
    d = Downloader.__new__(Downloader)
    f = tmp_path / 'x.mp3'
    f.write_bytes(b'')
    d._extract_artist_title_from_file = lambda path: ('Adele', 'Hello (Live from NYC)')
    assert d._is_download_match(str(f), 'Adele', 'Hello', request_context='video_title=Adele - Hello (Live from NYC)')
    assert not d._is_download_match(str(f), 'Adele', 'Hello', request_context='video_title=Adele - Hello')
    d._extract_artist_title_from_file = lambda path: ('Adele', 'Hello')
    assert not d._is_download_match(str(f), 'Adele', 'Hello', request_context='video_title=Adele - Hello (Live)')
    assert not d._is_download_match(str(f), 'Adele', 'Hello', request_context='message=the live version please https://x')


def test_a_word_in_a_songs_name_is_not_a_version():
    d = Downloader.__new__(Downloader)
    assert d._requested_variants('Live Your Life', 'video_title=T.I. - Live Your Life (Official Video)') == set()
    assert d._requested_variants('Song', 'video_title=Artist - Song (Official Cover Art)') == set()
