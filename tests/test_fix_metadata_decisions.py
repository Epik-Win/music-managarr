"""How Fix Metadata decides what a flagged song is: each test is one kind of
song (a re-upload, a sampled beat, a music-video rip, a renamed act …) and
the decision it must get right."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import fix_metadata
import metadata_repair


def run(monkeypatch, current, best, next_best=None, lidarr_album=None, art='not found', path='/music/x.mp3'):
    """Run Fix Metadata on one flagged track; return (written tags, stats)."""
    import audio_id
    artist, album, title = current
    track = SimpleNamespace(ratingKey='5', title=title, parentTitle=album, grandparentTitle=artist,
                            originalTitle='', refresh=lambda: None)

    class Playlist:
        title = 'Fix Metadata'

        def __init__(self):
            self._items = [track]

        def items(self):
            return list(self._items)

        def removeItems(self, items):
            self._items = []

    playlist = Playlist()
    user_plex = SimpleNamespace(playlists=lambda: [playlist], library=SimpleNamespace(section=lambda name: object()))
    admin = SimpleNamespace(switchUser=lambda name: user_plex)
    written = []
    monkeypatch.setattr(fix_metadata, '_file_path', lambda t: path)
    monkeypatch.setattr(fix_metadata, '_write_tags', lambda fp, meta: written.append(meta) or True)
    monkeypatch.setattr(fix_metadata, '_reorganize_file', lambda fp, meta, **kw: fp)
    monkeypatch.setattr(fix_metadata, '_refresh_in_plex', lambda t: None)
    monkeypatch.setattr(audio_id, 'identify_track_by_audio_with_alternatives', lambda fp: (best, next_best))
    monkeypatch.setattr(metadata_repair, 'lidarr_album_for_track', lambda a, t: lidarr_album)
    monkeypatch.setattr(metadata_repair, 'lidarr_lookup', lambda term: [])
    monkeypatch.setattr(metadata_repair, 'ask_local_ai', lambda system, prompt: None)
    import artwork
    monkeypatch.setattr(artwork, 'ensure_artwork', lambda fp, meta, hints=(), dry_run=False, replace=False:
                        art(replace) if callable(art) else art)
    stats = fix_metadata.run_fix_metadata(admin, 'bob', 'Music')
    return written, stats


def test_cover_uploads_name_is_not_an_artist(monkeypatch):
    junk = {'artist': "Thrift Shop & I'm Gonna Pop Some Tags", 'album': '', 'title': 'Thrift Shop'}
    written, stats = run(monkeypatch, ('Thri', '', 'Thri (feat. Shop)'), junk)
    assert written == [] and len(stats['unresolved']) == 1


def test_second_service_naming_another_song_does_not_replace_a_confirmed_one(monkeypatch):
    confirmed = {'artist': 'Kings of Tomorrow', 'album': '', 'title': 'Finally (Inspired Edit) [Extended Mix]'}
    other = {'artist': 'Kiasmos', 'album': 'Swept', 'title': 'Swept'}
    written, stats = run(monkeypatch, ('Kings of Tomorrow', '', 'Finally (Inspired Edit) [Extended Mix]'),
                         confirmed, other, lidarr_album='Finally')
    assert written == [{'artist': 'Kings of Tomorrow', 'album': 'Finally',
                        'title': 'Finally (Inspired Edit) [Extended Mix]'}]


def test_nothing_written_without_an_album(monkeypatch):
    answer = {'artist': 'Nobuya Kobori', 'album': '', 'title': 'Moonlight Sonata'}
    written, stats = run(monkeypatch, ('Charlène Juarez', '', 'Moonlight Sonata'), answer)
    # writing album '' would file the song in a folder called "Unknown"
    assert written == []
    assert 'identified as Nobuya Kobori' in stats['unresolved'][0]['reason']


def test_placeholder_album_is_not_kept(monkeypatch):
    answer = {'artist': 'Bon Jovi', 'album': '', 'title': 'Bon Jovi-Greatest Hits Medley (Japan)'}
    written, stats = run(monkeypatch, ('George Benson', 'Miscellaneous', 'Bon Jovi-Greatest Hits Medley (Japan)'), answer)
    assert written == [] and 'no album was found' in stats['unresolved'][0]['reason']


def test_credited_artist_is_kept_over_a_collaboration(monkeypatch):
    answer = {'artist': 'Kid Cudi & Eminem', 'album': 'The Adventures of Moon Man & Slim Shady - Single',
              'title': 'The Adventures of Moon Man & Slim Shady'}
    written, _ = run(monkeypatch, ('Kid Cudi', 'Unknown', 'The Adventures of Moon Man & Slim Shady (with Eminem)'), answer)
    assert written[0]['artist'] == 'Kid Cudi'
    # No service returned the tagged "(with Eminem)" form: the official title wins.
    assert written[0]['title'] == 'The Adventures of Moon Man & Slim Shady'


def test_full_title_kept_when_a_service_confirms_it(monkeypatch):
    short = {'artist': 'Charley Crockett', 'album': 'Clovis', 'title': "Don't Take Your Guns"}
    full = {'artist': 'Charley Crockett', 'album': '', 'title': 'Don’t Take Your Guns to Town'}
    written, _ = run(monkeypatch, ('George Benson', 'Miscellaneous', 'Don’t Take Your Guns to Town'), short, full)
    assert written == [{'artist': 'Charley Crockett', 'album': 'Clovis', 'title': 'Don’t Take Your Guns to Town'}]


def test_art_alone_does_not_fix_a_song_without_a_title(monkeypatch):
    written, stats = run(monkeypatch, ('Three Days Grace', '', ''), None, art='embedded folder cover')
    assert written == [] and stats['fixed'] == 0 and len(stats['unresolved']) == 1


def test_art_alone_fixes_a_song_whose_names_are_right(monkeypatch):
    same = {'artist': 'League of Legends', 'album': 'RISE', 'title': 'RISE'}
    written, stats = run(monkeypatch, ('League of Legends', 'RISE', 'RISE'), same, art='fetched from Deezer')
    assert written == [] and stats['fixed'] == 1


def test_one_service_naming_an_unrelated_song_goes_to_a_person(monkeypatch):
    life = {'artist': 'Brides of Destruction', 'album': 'Here Come the Brides', 'title': 'Life'}
    written, stats = run(monkeypatch, ('ＤＪ　Ｋａｏｒｉ', 'Lupe Fiasco -LifeK fox', 'ライフ(エル・エイ・トゥ・シカゴ)'), None, life)
    assert written == []
    assert 'suggests Brides of Destruction – Life' in stats['unresolved'][0]['reason']


def test_swapped_artist_and_title_is_the_same_song(monkeypatch):
    answer = {'artist': 'Chiddy Bang', 'album': 'The Swelly Express', 'title': 'Decline'}
    written, _ = run(monkeypatch, ('Decline', 'Decline', 'Chiddy Bang'), answer)
    assert written == [{'artist': 'Chiddy Bang', 'album': 'The Swelly Express', 'title': 'Decline'}]


def test_band_name_with_a_comma_is_kept_whole(monkeypatch):
    answer = {'artist': 'Earth, Wind & Fire', 'album': 'The Best of Earth, Wind & Fire, Vol. 1', 'title': 'September'}
    written, _ = run(monkeypatch, ('Earth', 'Rio After Dark (Live In Rio de Janeiro 1980)', 'September (feat. Fire)'), answer)
    assert written[0]['artist'] == 'Earth, Wind & Fire'


def test_unknown_collaboration_files_under_the_first_artist(monkeypatch):
    answer = {'artist': 'Skrillex & Kendrick Lamar', 'album': 'HUMBLE. (SKRILLEX REMIX) - Single',
              'title': 'HUMBLE. (SKRILLEX REMIX)'}
    written, _ = run(monkeypatch, ('Rando', 'HUMBLE. (SKRILLEX REMIX) - Single', 'HUMBLE (feat. Kendrick Lamar)'), answer)
    assert written[0]['artist'] == 'Skrillex'


def test_missing_title_comes_from_the_file_name():
    assert metadata_repair.title_from_filename('/music/albums/Anis Bahmed/Ikigai/Anis Bahmed - Ikigai (Official Video).mp3',
                                               'Anis Bahmed') == 'Ikigai'
    assert metadata_repair.title_from_filename('/music/x/03 - Forever.mp3') == 'Forever'
    assert metadata_repair.title_from_filename('/music/x/Unknown Title.mp3') == ''


def test_deezers_album_beats_a_bootleg(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'deezer_albums_for_track', lambda a, t, d=None: ['Let It Be'])
    monkeypatch.setattr(metadata_repair, 'lidarr_confirms_album', lambda a, al: al == 'Let It Be')
    answer = {'artist': 'The Beatles', 'album': 'Hours of Darkness', 'title': 'Maggie Mae'}
    written, _ = run(monkeypatch, ('The Beatles', 'Miscellaneous', 'Maggie Mae'), answer)
    assert written[0]['album'] == 'Let It Be'


def test_a_remix_is_not_filed_under_the_original_album(monkeypatch):
    remix = {'artist': 'Gabriella Montez & Troy', 'album': 'Breaking Free (Remix) - Single', 'title': 'Breaking Free (Remix)'}
    original = {'artist': 'Vanessa Anne Hudgens', 'album': 'High School Musical', 'title': 'Breaking Free'}
    written, _ = run(monkeypatch, ('Gabriella Montez', '', 'Breaking Free (feat. Troy)'), remix, original)
    assert written[0]['album'] == 'Breaking Free (Remix) - Single'


def test_tagged_title_kept_only_when_a_service_returned_it(monkeypatch):
    answer = {'artist': 'Earth, Wind & Fire', 'album': 'Rio After Dark', 'title': 'September'}
    written, _ = run(monkeypatch, ('Earth', 'Rio After Dark', 'September (feat. Fire)'), answer)
    assert written[0]['title'] == 'September'


def test_title_found_in_the_file_name_is_written(monkeypatch):
    # a readable file tagged with everything but the title
    monkeypatch.setattr(metadata_repair, 'file_artist_tag', lambda p: 'Thao Sam')
    monkeypatch.setattr(metadata_repair, 'file_tag_meta', lambda p: {'artist': 'Thao Sam', 'album': 'Remember Me', 'title': ''})
    written, stats = run(monkeypatch, ('Thao Sam', 'Remember Me', ''), None,
                         path='/music/albums/Thao Sam/Remember Me/Thao Sam - Who I Am.mp3')
    assert written == [{'artist': 'Thao Sam', 'album': 'Remember Me', 'title': 'Who I Am'}]


def test_no_online_art_for_an_unidentified_artist(tmp_path, monkeypatch):
    import artwork
    called = []
    monkeypatch.setattr(artwork, 'find_artwork', lambda *a, **k: called.append(a) or (b'x' * 4096, 'Deezer'))
    f = tmp_path / 'Tenderly (2011).mp3'
    f.write_bytes(b'')
    assert artwork.ensure_artwork(str(f), {'artist': 'Unidentified', 'title': 'Tenderly (2011)'}) == 'not found'
    assert called == []


def test_a_bootleg_or_compilation_is_not_confirmed_by_lidarr(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'lidarr_lookup', lambda term: [
        {'artist': 'The Beatles', 'album': 'Hours of Darkness', 'statuses': ['Bootleg'], 'secondary': []},
        {'artist': 'The Beatles', 'album': 'Let It Be', 'statuses': ['Official'], 'secondary': []},
        {'artist': 'The Beatles', 'album': '1', 'statuses': ['Official'], 'secondary': ['Compilation']},
    ])
    assert not metadata_repair.lidarr_confirms_album('The Beatles', 'Hours of Darkness')
    assert metadata_repair.lidarr_confirms_album('The Beatles', 'Let It Be')
    assert not metadata_repair.lidarr_confirms_album('The Beatles', '1')


def test_numbers_that_are_part_of_the_title_stay():
    assert metadata_repair.title_from_filename('/m/528 Hz Música de Sanación.mp3') == '528 Hz Música de Sanación'
    assert metadata_repair.title_from_filename('/m/99 Problems.mp3') == '99 Problems'
    assert metadata_repair.title_from_filename('/m/03 - Forever.mp3') == 'Forever'


def test_the_files_artist_wins_over_plexs(monkeypatch, tmp_path):
    """Plex lists Itzhak Perlman's concerto under Carlo Maria Giulini; the
    file says Perlman. The file's artist must not be replaced by Plex's."""
    import shutil
    import subprocess
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg not available')
    f = tmp_path / 'Itzhak Perlman – Beethoven Violin Concerto.mp3'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
                    '-t', '1', '-q:a', '9', str(f)], check=True)
    from mutagen.easyid3 import EasyID3
    tags = EasyID3()
    tags.update({'artist': 'Itzhak Perlman', 'albumartist': 'Itzhak Perlman',
                 'album': 'Beethoven: Violin Concerto'})
    tags.save(str(f))
    written, _ = run(monkeypatch, ('Carlo Maria Giulini', 'Beethoven: Violin Concerto', ''), None, path=str(f))
    assert written and written[0]['artist'] == 'Itzhak Perlman'


def test_same_album_keeps_its_existing_spelling(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'deezer_albums_for_track', lambda a, t, d=None: ['With The Beatles (Remastered 2009)'])
    answer = {'artist': 'The Beatles', 'album': 'Anthropology (Special Collection 1963)', 'title': 'Devil in Her Heart'}
    written, _ = run(monkeypatch, ('The Beatles', 'With The Beatles', 'Devil in Her Heart'), answer)
    assert written == [] or written[0]['album'] == 'With The Beatles'


def test_remaster_suffix_is_dropped_from_deezer_album_names():
    assert metadata_repair._REMASTER_SUFFIX.sub('', 'Let It Be (Remastered 2009)') == 'Let It Be'
    assert metadata_repair._REMASTER_SUFFIX.sub('', 'Rumours (Deluxe Edition)') == 'Rumours (Deluxe Edition)'


def test_deezers_canonical_album_beats_the_ais_tie_break(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'deezer_albums_for_track', lambda a, t, d=None: ['Let It Be', 'Skiffle: Beatles Volume 1'])
    monkeypatch.setattr(metadata_repair, 'ai_choose_album', lambda a, t, options: 'Skiffle: Beatles Volume 1')
    answer = {'artist': 'The Beatles', 'album': 'Hours of Darkness', 'title': 'Maggie Mae'}
    written, _ = run(monkeypatch, ('The Beatles', '', 'Maggie Mae'), answer)
    assert written[0]['album'] == 'Let It Be'


def test_tagged_album_kept_without_its_remaster_suffix(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'deezer_albums_for_track', lambda a, t, d=None: ['Let It Be', 'Skiffle: Beatles Volume 1'])
    answer = {'artist': 'The Beatles', 'album': '', 'title': 'Maggie Mae'}
    written, _ = run(monkeypatch, ('The Beatles', 'Let It Be (Remastered 2009)', 'Maggie Mae'), answer)
    assert written[0]['album'] == 'Let It Be'


def test_name_lookup_fixes_a_song_fingerprinting_missed(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'deezer_lookup_by_name',
                        lambda meta, path, extra=(): {'artist': 'HanMari', 'album': 'Migration', 'title': 'Migration'})
    written, stats = run(monkeypatch, ('HanMari', 'HanMari — Migration (Official video)',
                                       'Excerpt from "The Art Of Flying" by Jan van IJken'), None)
    assert written == [{'artist': 'HanMari', 'album': 'Migration', 'title': 'Migration'}]


def test_name_lookup_confirming_the_tags_reports_plexs_wrong_artist(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'deezer_lookup_by_name',
                        lambda meta, path, extra=(): {'artist': 'Wrong Artist', 'album': 'Wrong Album', 'title': 'Wrong Title'})
    dre = {'artist': 'Dre Ice', 'album': 'FORGIVE - Single', 'title': 'FORGIVE'}
    written, stats = run(monkeypatch, ('Wrong Artist', 'Wrong Album', 'Wrong Title'), dre)
    assert written == [] and 'tags are right' in stats['unresolved'][0]['reason']


def test_unidentifiable_single_is_filed_with_album_named_after_it(monkeypatch):
    monkeypatch.setattr(fix_metadata, '_on_probation', lambda fp: True)
    written, _ = run(monkeypatch, ('Crack Amico', 'Crack Amico', 'Predator (Official Video)'), None)
    assert written == [{'artist': 'Crack Amico', 'album': 'Predator', 'title': 'Predator'}]


def test_the_single_filing_convention_ignores_one_stray_vote_but_not_two(monkeypatch):
    monkeypatch.setattr(fix_metadata, '_on_probation', lambda fp: True)
    stray = {'artist': 'Bynoe', 'album': 'Have The Day You Deserve', 'title': 'Intro'}
    written, _ = run(monkeypatch, ('Crack Amico', 'Crack Amico', 'Arena Hack (AUSTIN DISS)'), stray)
    assert written == [{'artist': 'Crack Amico', 'album': 'Arena Hack (AUSTIN DISS)', 'title': 'Arena Hack (AUSTIN DISS)'}]
    import audio_id
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [dict(stray, votes=2)])
    written, stats = run(monkeypatch, ('Crack Amico', 'Crack Amico', 'Arena Hack (AUSTIN DISS)'), None)
    assert written == [{'artist': 'Bynoe', 'album': 'Have The Day You Deserve', 'title': 'Intro'}] or written == []

def test_accents_dont_stop_titles_matching():
    assert metadata_repair.titles_agree('Ahi', 'Ahí')


def test_artist_dash_title_text_gives_title_candidates():
    meta = {'artist': 'HanMari', 'album': 'HanMari — Migration (Official video)', 'title': 'Excerpt from a film'}
    assert 'Migration' in metadata_repair.name_candidates(meta, None)
    meta = {'artist': 'Karol G & Drake - Ahi', 'album': 'Karol', 'title': 'Karol G & Drake (feat. Drake - Ahi)'}
    assert 'Ahi' in metadata_repair.name_candidates(meta, None)


def test_deezer_name_lookup_needs_matching_length_and_artist(monkeypatch):
    import requests

    class Reply:
        def __init__(self, data):
            self._data = data

        def json(self):
            return {'data': self._data}

    rows = [{'artist': {'name': 'Broken Brass Ensemble'}, 'title': 'Thrift Shop', 'duration': 236, 'album': {'title': 'X'}},
            {'artist': {'name': 'Macklemore & Ryan Lewis'}, 'title': 'Thrift Shop (feat. Wanz)', 'duration': 235,
             'album': {'title': 'The Heist'}}]
    monkeypatch.setattr(metadata_repair, 'audio_duration', lambda p: 235.6)
    monkeypatch.setattr(metadata_repair, 'is_known_artist', lambda n: False)
    monkeypatch.setattr(requests, 'get', lambda *a, **k: Reply(rows))
    real = metadata_repair._real_deezer_lookup_by_name
    # junk artist: only the top result for the exact title counts (Broken Brass here)
    found = real({'artist': 'Thri', 'album': '', 'title': 'Thri (feat. Shop)'}, '/m/x.mp3', ['Thrift Shop'])
    assert found is None or found['artist'] != 'Macklemore'
    # known artist: its own recording of the right length
    found = real({'artist': 'Macklemore & Ryan Lewis', 'album': '', 'title': 'Thrift Shop'}, '/m/x.mp3')
    assert found == {'artist': 'Macklemore', 'album': 'The Heist', 'title': 'Thrift Shop (feat. Wanz)'}


def test_the_answer_that_fits_the_tags_is_preferred(monkeypatch):
    unrelated = {'artist': 'YoungSolidTae', 'album': 'Next Up - Single', 'title': 'Next Up'}
    fitting = {'artist': 'Tiny Meat Gang', 'album': 'Walk Man - Single', 'title': 'Walk Man'}
    written, _ = run(monkeypatch, ('TMG', 'Flex TMG - Whisper Swish', 'Walk Man'), unrelated, fitting)
    assert written and written[0]['artist'] == 'Tiny Meat Gang' and written[0]['title'] == 'Walk Man'


def test_confirmed_single_without_an_album_is_filed_as_a_single(monkeypatch):
    monkeypatch.setattr(fix_metadata, '_on_probation', lambda fp: True)
    monkeypatch.setattr(metadata_repair, 'file_artist_tag', lambda p: 'epicsoju')
    monkeypatch.setattr(metadata_repair, 'file_tag_meta',
                        lambda p: {'artist': 'epicsoju', 'album': '', 'title': 'i will never be a star'})
    same = {'artist': 'epicsoju', 'album': '', 'title': 'i will never be a star'}
    written, _ = run(monkeypatch, ('epicsoju', '', 'i will never be a star'), same)
    assert written == [{'artist': 'epicsoju', 'album': 'i will never be a star', 'title': 'i will never be a star'}]


def test_thrift_shop_is_not_split_at_its_ft():
    from naming import parse_artist_field
    assert parse_artist_field('Thrift Shop') == ('Thrift Shop', '')
    assert parse_artist_field('Artist ft. Someone') == ('Artist', 'Someone')
    import cli
    assert cli.format_song_title_with_features('Thrift Shop') == 'Thrift Shop'


def test_album_named_after_the_song_is_no_cover_evidence():
    import artwork
    assert not artwork.result_matches('Crack Amico', 'Predator', 'Predator', ['Some Band'], 'Predator', 'Predator')
    assert artwork.result_matches('Crack Amico', 'Predator', 'Predator', ['Crack Amico'], 'Predator', 'Predator')


def test_artist_tag_holding_a_title_is_split(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'is_known_artist', lambda n: n in ('Drake', 'Taylor Swift'))
    assert metadata_repair.split_artist_title('Drake - Make Them Pay') == ('Drake', 'Make Them Pay')
    assert metadata_repair.split_artist_title('Taylor Swift - Babylon') == ('Taylor Swift', 'Babylon')
    assert metadata_repair.split_artist_title('Some Band - Some Band') is None
    track = SimpleNamespace(title='Make Them Pay', parentTitle='ICEMAN', grandparentTitle='Drake - Make Them Pay',
                            originalTitle='', parentRatingKey='1', parentThumb='/library/metadata/1/thumb')
    assert metadata_repair.needs_repair(track) == metadata_repair.ARTIST_HOLDS_TITLE
    meta = metadata_repair.known_meta(track, None)
    assert meta['artist'] == 'Drake' and meta['title'] == 'Make Them Pay'


def test_dash_split_leaves_real_act_names_and_junk_titles_alone(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'is_known_artist', lambda n: n in ('Karol G', 'PKF', 'Prague Philharmonia'))
    assert metadata_repair.split_artist_title('PKF - Prague Philharmonia') is None
    track = SimpleNamespace(title='Karol G & Drake (feat. Drake - Ahi)', parentTitle='Karol',
                            grandparentTitle='Karol G & Drake - Ahi', originalTitle='', parentRatingKey='1')
    meta = metadata_repair.known_meta(track, None)
    # The split's title, not the junk title tag; "Karol" is a piece of the artist.
    assert (meta['artist'], meta['album'], meta['title']) == ('Karol G', '', 'Ahi')


def test_isrc_names_the_recording_even_against_the_tags(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'file_isrc', lambda p: 'CBCDA2600002')
    monkeypatch.setattr(metadata_repair, 'deezer_track_by_isrc', lambda isrc, duration=None: {
        'artist': 'Enima', 'title': 'ANNÉES', 'album': 'EVIL WORLD', 'service': 'ISRC', 'votes': 99})
    written, _ = run(monkeypatch, ('P T K', 'KARAKORAM DLC', 'LONG RUN'), None)
    assert written == [{'artist': 'Enima', 'album': 'EVIL WORLD', 'title': 'ANNÉES'}]


def test_majority_of_slices_beats_one_that_matched_the_wrong_tags(monkeypatch):
    import audio_id
    voted = [{'artist': 'Kiasmos', 'title': 'Swept', 'album': 'Swept - EP', 'votes': 6},
             {'artist': 'Kings of Tomorrow', 'title': 'Finally (Inspired Edit) [Extended Mix]', 'album': '', 'votes': 1}]
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: voted)
    written, _ = run(monkeypatch, ('Kings of Tomorrow', '', 'Finally (Inspired Edit) [Extended Mix]'), None)
    assert written == [{'artist': 'Kiasmos', 'album': 'Swept - EP', 'title': 'Swept'}]


def test_youtube_thumbnail_needs_the_artist_title_and_rough_length(monkeypatch):
    import artwork
    videos = [{'videoId': 'abc', 'title': 'Ylvis - I Will Never Be A Star', 'artists': [{'name': 'Ylvis'}],
               'duration_seconds': 213},
              {'videoId': 'xyz', 'title': 'Crack Amico - Predator (Official Video)', 'artists': [{'name': 'Crack Amico'}],
               'duration_seconds': 205}]
    monkeypatch.setattr(artwork, '_ytmusic', lambda kind, a, t, d: videos if kind == 'videos' else [])
    monkeypatch.setattr('subprocess.run', lambda *a, **k: SimpleNamespace(stdout=''))   # no real YouTube search
    real = artwork._real_youtube_thumbnail
    assert real('Crack Amico', 'Predator', 'Predator', 199).startswith('https://i.ytimg.com/vi/xyz/')
    monkeypatch.setattr(artwork, '_matching_video', artwork._matching_video)
    assert real('epicsoju', 'i will never be a star', '', 87) is None or 'abc' not in real('epicsoju', 'i will never be a star', '', 87)



def test_numbered_titles_must_match_for_a_cover():
    import artwork
    assert not artwork.result_matches('Real Kozby', 'Big Talk II', '', ['Real Kozby'], 'Big Talk', 'Big Talk')
    assert artwork.result_matches('Real Kozby', 'Big Talk II', '', ['Real Kozby'], 'Big Talk II', 'X')


def test_shared_folder_keeps_its_cover(tmp_path):
    import artwork
    old, new = tmp_path / 'Crack Amico', tmp_path / 'Kill Phony'
    old.mkdir(); new.mkdir()
    (old / 'cover.jpg').write_bytes(b'x' * 3000)
    (old / 'Arena Hack.mp3').write_bytes(b'a')
    assert artwork.carry_cover(str(old), str(new)) is None and (old / 'cover.jpg').exists()



def test_a_reupload_alias_does_not_replace_the_real_artist(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'is_known_artist', lambda n: n == 'YBN Cordae')
    import audio_id
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': '$v3x3r', 'title': 'S**t Talk', 'album': '', 'votes': 3}])
    written, stats = run(monkeypatch, ('YBN Cordae', 'Sh*t Talk', 'Sh*t Talk'), None)
    assert all(w['artist'] == 'YBN Cordae' for w in written)


def _true_audio_setup(monkeypatch, file_len, ref, downloaded_len=None):
    swapped = {}
    # The release of these names only, as the real lookup gives.
    monkeypatch.setattr(metadata_repair, 'reference_version',
                        lambda a, t: ref if (a, t) == (ref['artist'], ref['title']) else None)
    lengths = {'/music/x.mp3': file_len, '/staging/new.mp3': downloaded_len}
    monkeypatch.setattr(metadata_repair, 'audio_duration', lambda p: lengths.get(p, file_len))
    monkeypatch.setattr(fix_metadata, '_download_reference', lambda r, staging: '/staging/new.mp3')
    monkeypatch.setattr(fix_metadata.os.path, 'exists', lambda p: True)
    monkeypatch.setattr(fix_metadata, '_swap_in', lambda old, new: swapped.setdefault('to', '/music/x.mp3'))
    return swapped


def test_music_video_cut_is_replaced_with_the_release(monkeypatch):
    import audio_id
    swapped = _true_audio_setup(monkeypatch, 184, {'source': 'deezer', 'id': 1, 'duration': 166.0,
                                                   'artist': 'Tiny Meat Gang', 'title': 'Walk Man', 'album': 'Walk Man'},
                                downloaded_len=166)
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': 'Tiny Meat Gang', 'title': 'Walk Man', 'album': 'Walk Man - Single', 'votes': 3}])
    written, stats = run(monkeypatch, ('Tiny Meat Gang', 'Walk Man - Single', 'Walk Man'), None)
    assert swapped == {'to': '/music/x.mp3'} and stats['fixed'] == 1
    assert written[0] == {'artist': 'Tiny Meat Gang', 'album': 'Walk Man', 'title': 'Walk Man'}


def test_audio_of_a_different_known_song_is_a_names_problem(monkeypatch):
    import audio_id
    swapped = _true_audio_setup(monkeypatch, 333, {'source': 'deezer', 'id': 1, 'duration': 300.0,
                                                   'artist': 'Kings of Tomorrow', 'title': 'Finally', 'album': 'Finally'},
                                downloaded_len=300)
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': 'Kiasmos', 'title': 'Swept', 'album': 'Swept', 'votes': 6}])
    written, _ = run(monkeypatch, ('Kings of Tomorrow', '', 'Finally'), None)
    assert swapped == {} and written[0]['artist'] == 'Kiasmos'


def test_unproven_recording_is_reported_with_the_length_mismatch(monkeypatch):
    # "Taylor Swift" is a real self-titled album, not a placeholder
    monkeypatch.setattr(metadata_repair, 'lidarr_confirms_album', lambda a, al: (a, al) == ('Taylor Swift', 'Taylor Swift'))
    monkeypatch.setattr(metadata_repair, 'audio_duration', lambda p: 1213)
    monkeypatch.setattr(metadata_repair, 'listed_length', lambda a, t: 245)
    written, stats = run(monkeypatch, ('Taylor Swift', 'Taylor Swift', 'Everything Has Changed'), None)
    assert written == [] and "4:05 while this file is 20:13" in stats['unresolved'][0]['reason']


def test_reference_skips_radio_specials_and_prefers_the_most_played(monkeypatch):
    import requests

    class Reply:
        def json(self):
            return {'data': [
                {'id': 1, 'title': 'Everything Has Changed', 'duration': 58, 'rank': 300000,
                 'artist': {'name': 'Taylor Swift'}, 'album': {'title': 'Red (Big Machine Radio Release Special)'}},
                {'id': 2, 'title': 'Everything Has Changed (Taylor\'s Version)', 'duration': 245, 'rank': 700000,
                 'artist': {'name': 'Taylor Swift'}, 'album': {'title': 'Red (Taylor\'s Version)'}},
                {'id': 3, 'title': 'Everything Has Changed', 'duration': 245, 'rank': 650000,
                 'artist': {'name': 'Taylor Swift'}, 'album': {'title': 'Red'}}]}
    monkeypatch.setattr(requests, 'get', lambda *a, **k: Reply())
    import importlib
    real = importlib.import_module('metadata_repair').__dict__.get('_real_reference_version')
    ref = (real or metadata_repair.reference_version)('Taylor Swift', 'Everything Has Changed')
    assert ref['id'] == 3


def test_a_lone_unrelated_vote_loses_to_a_same_length_listing(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'release_listing', lambda a, t, d, tolerance=3: None)
    import artwork
    monkeypatch.setattr(artwork, 'own_upload_matches', lambda a, t, d, tolerance=3: True)
    monkeypatch.setattr(artwork, 'verify_cover', lambda path, meta, dry_run=False: 'fetched from YouTube thumbnail')
    nav = {'artist': 'NAV', 'title': 'Champion (feat. Travis Scott)', 'album': 'Bad Habits'}
    written, stats = run(monkeypatch, ('Crack Amico', 'Predator', 'Predator'), nav)
    assert written == [] and stats['fixed'] == 1


def test_release_title_and_album_replace_the_tagged_ones(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'release_listing', lambda a, t, d, tolerance=3: {
        'album': '2Bears1Grave', 'title': '2Bears1Grave', 'source': 'Spotify'})
    written, _ = run(monkeypatch, ('Crack Amico', '2Bears1Grave (BERT & TOM DISS)', '2Bears1Grave (BERT & TOM DISS)'), None)
    assert written == [{'artist': 'Crack Amico', 'album': '2Bears1Grave', 'title': '2Bears1Grave'}]


def test_reference_finds_the_original_album_the_search_ranks_low(monkeypatch):
    # Deezer's search shows only "(Taylor's Version)" and a radio-special
    # commentary; "Red" itself has the 245 s original.
    import requests

    def get(url, params=None, timeout=None):
        if url.endswith('/search'):
            data = [
                {'id': 1, 'title': "Everything Has Changed (Taylor's Version)", 'duration': 245, 'rank': 506068,
                 'artist': {'id': 12246, 'name': 'Taylor Swift'}, 'album': {'id': 272247412, 'title': "Red (Taylor's Version)"}},
                {'id': 2, 'title': 'Everything Has Changed (Commentary)', 'duration': 58, 'rank': 90000,
                 'artist': {'id': 12246, 'name': 'Taylor Swift'},
                 'album': {'id': 81389442, 'title': 'Red (Big Machine Radio Release Special)'}},
                {'id': 3, 'title': 'Everything Has Changed (Karaoke Version)', 'duration': 244, 'rank': 100000,
                 'artist': {'id': 9, 'name': "Singer's Best"}, 'album': {'id': 5, 'title': 'Karaoke Hits'}}]
        elif url.endswith('/artist/12246/albums'):
            data = [{'id': 272247412, 'title': "Red (Taylor's Version)", 'release_date': '2021-11-12'},
                    {'id': 130716962, 'title': 'Red (Deluxe Edition)', 'release_date': '2012-10-22'},
                    {'id': 130721292, 'title': 'Red', 'release_date': '2012-10-22', 'cover_xl': 'red.jpg'},
                    {'id': 288395152, 'title': "Red (Taylor's Version): The Slow Motion Chapter", 'release_date': '2022-01-25'}]
        elif url.endswith('/album/130721292/tracks'):
            data = [{'id': 871688641, 'title': 'State Of Grace', 'duration': 295, 'rank': 400000,
                     'artist': {'name': 'Taylor Swift'}},
                    {'id': 871688642, 'title': 'Everything Has Changed', 'duration': 245, 'rank': 500000,
                     'artist': {'name': 'Taylor Swift'}}]
        else:
            data = []
        return SimpleNamespace(json=lambda: {'data': data})
    monkeypatch.setattr(requests, 'get', get)
    ref = metadata_repair._real_reference_version('Taylor Swift', 'Everything Has Changed')
    assert (ref['id'], ref['album'], ref['duration'], ref['cover']) == (871688642, 'Red', 245.0, 'red.jpg')


def _sample_setup(monkeypatch, voted_len):
    import audio_id
    monkeypatch.setattr(metadata_repair, 'audio_duration', lambda p: 164)
    monkeypatch.setattr(metadata_repair, 'isrc_length', lambda isrc: voted_len if isrc == 'RUAGT2418421' else None)
    monkeypatch.setattr(metadata_repair, 'release_listing', lambda a, t, d, tolerance=3: {
        'album': 'Cracker Barrel Diss', 'title': 'Cracker Barrel Diss', 'source': 'Spotify'} if abs(d - 165) <= tolerance else None)
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': 'ния & bir kyz', 'title': 'хороша', 'album': 'ОК', 'isrc': 'RUAGT2418421',
         'service': 'Shazam', 'votes': 4}])
    return run(monkeypatch, ('Crack Amico', 'Cracker Barrel (Triple Gang Diss)', 'Cracker Barrel (Triple Gang Diss)'), None)


def test_votes_for_a_sampled_beat_keep_the_tagged_song(monkeypatch):
    written, _ = _sample_setup(monkeypatch, voted_len=149)
    assert written == [{'artist': 'Crack Amico', 'album': 'Cracker Barrel Diss', 'title': 'Cracker Barrel Diss'}]


def test_votes_win_when_their_recording_has_the_files_length(monkeypatch):
    written, _ = _sample_setup(monkeypatch, voted_len=164)
    assert written and written[0]['title'] == 'хороша'


def test_failed_true_audio_download_goes_to_a_person_untouched(monkeypatch):
    import audio_id
    _true_audio_setup(monkeypatch, 58, {'source': 'youtube', 'id': 'w1oM3kQpXRo', 'duration': 252.0,
                                        'artist': 'Taylor Swift', 'title': 'Everything Has Changed', 'album': ''},
                      downloaded_len=None)
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': 'Taylor Swift', 'title': 'Everything Has Changed (Commentary)',
         'album': 'Red (Big Machine Radio Release Special)', 'votes': 3}])
    written, stats = run(monkeypatch, ('Taylor Swift', 'Red', 'Everything Has Changed'), None)
    assert written == [] and 'could not be downloaded' in stats['unresolved'][0]['reason']


def test_a_radio_special_is_not_the_songs_cover():
    from artwork import result_matches
    special = (['Taylor Swift'], 'Everything Has Changed (Commentary)', 'Red (Big Machine Radio Release Special)')
    assert not result_matches('Taylor Swift', 'Everything Has Changed', 'Red', *special)
    assert result_matches('Taylor Swift', 'Everything Has Changed (Commentary)', 'Red (Big Machine Radio Release Special)',
                          *special)
    assert result_matches('Taylor Swift', 'Everything Has Changed', 'Red', ['Taylor Swift'], 'Everything Has Changed', 'Red')


def _artist_search(monkeypatch, results):
    import requests
    monkeypatch.setattr(requests, 'get', lambda url, params=None, timeout=None: SimpleNamespace(
        json=lambda: {'data': results.get(params['q'], [])}))


def test_an_old_artist_name_gives_the_current_one(monkeypatch):
    _artist_search(monkeypatch, {
        'YBN Cordae': [{'name': 'Cordae', 'nb_fan': 72375}, {'name': 'YBN Cordae', 'nb_fan': 0}],
        "Soulja Boy Tell 'Em": [{'name': 'Soulja Boy', 'nb_fan': 591748}],
        'Patti Smith Group': [{'name': 'Patti Smith', 'nb_fan': 245768}],
        'Kanye West': [{'name': 'Ye', 'nb_fan': 900000}]})
    monkeypatch.setattr(metadata_repair, 'is_known_artist', lambda name: name == 'Patti Smith Group')
    current = metadata_repair._real_current_artist_name
    assert current('YBN Cordae') == 'Cordae'
    assert current("Soulja Boy Tell 'Em") == 'Soulja Boy'
    assert current('Patti Smith Group') is None          # a known act of its own
    assert current('Kanye West') is None                 # "Ye" is not within it
    assert current('Kid Cudi & Eminem') is None          # a collaboration


def test_renamed_artist_is_written_and_keeps_its_cover(monkeypatch):
    import artwork
    import audio_id
    monkeypatch.setattr(metadata_repair, 'current_artist_name', lambda name: 'Cordae' if name == 'YBN Cordae' else None)
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': 'YBN Cordae', 'title': 'Sh*t Talk', 'album': 'Sh*t Talk', 'votes': 2}])
    replaced = []
    monkeypatch.setattr(artwork, 'verify_cover', lambda path, meta, dry_run=False: 'matches the release')
    written, stats = run(monkeypatch, ('YBN Cordae', 'Sh*t Talk', 'Sh*t Talk'), None,
                         art=lambda replace: replaced.append(replace) or 'present')
    assert written == [{'artist': 'Cordae', 'album': 'Sh*t Talk', 'title': 'Sh*t Talk'}] and stats['fixed'] == 1
    assert replaced == [False]


def test_a_clean_edit_shares_its_albums_cover_but_not_its_listing():
    from artwork import result_matches
    deezer = (['Petey Pablo'], 'Show Me The Money', 'Step Up Soundtrack')
    assert result_matches('Petey Pablo', 'Show Me the Money (Clean Version)', 'Step Up (Original Soundtrack)',
                          *deezer, for_cover=True)
    assert not result_matches('Petey Pablo', 'Show Me the Money (Clean Version)', 'Step Up (Original Soundtrack)',
                              *deezer)


def test_an_upload_name_repeating_the_artist_is_no_answer(monkeypatch):
    # Shazam echoes the file's junk upload name four times; ACRCloud's two
    # votes name the real song (Deezer: 202 s, as the file).
    import audio_id
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': 'Marmar - Arabic (6)', 'title': 'Law Fy 7aiaty - Marmar - Arabic (6)', 'album': '', 'votes': 4},
        {'artist': 'Amr Mostafa', 'title': 'Law Fi Hayati', 'album': 'Law Fi Hayati (Thousands of Miles)', 'votes': 2}])
    written, _ = run(monkeypatch, ('Marmar - Arabic (6)', 'Hayati', 'Law Fy 7aiaty - Marmar - Arabic (6)'), None)
    assert written and (written[0]['artist'], written[0]['title']) == ('Amr Mostafa', 'Law Fi Hayati')
    assert fix_metadata._usable_answer({'artist': 'Drake', 'title': "Drake's Freestyle"})   # not an upload name


def test_an_answers_deezer_track_gives_the_real_release(monkeypatch):
    # Deezer's search hides the single from the US exit; ACRCloud names its
    # Deezer track, which a direct lookup still returns.
    import audio_id
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': 'Amr Mostafa', 'title': 'Law Fi Hayati', 'album': 'Law Fi Hayati (Thousands of Miles)',
         'deezer_id': '3376823', 'votes': 2}])
    monkeypatch.setattr(metadata_repair, 'deezer_release', lambda a, d=None, tolerance=None: {
        'artist': 'Amr Mostafa', 'title': 'Law Fi Hayati', 'album': 'Law Fi Hayati', 'cover_art_url': 'dz.jpg'}
        if a.get('deezer_id') == '3376823' else None)
    written, _ = run(monkeypatch, ('Marmar - Arabic (6)', 'Hayati', 'Law Fy 7aiaty - Marmar - Arabic (6)'), None)
    assert written[0] == {'artist': 'Amr Mostafa', 'album': 'Law Fi Hayati', 'title': 'Law Fi Hayati'}


def test_a_compilation_from_the_isrc_does_not_replace_the_album():
    answers = [{'artist': 'Bon Jovi', 'title': 'When We Were Beautiful', 'album': 'The Circle', 'isrc': 'X',
                'cover_art_url': 'circle.jpg'}]
    metadata_repair_release = {'artist': 'Bon Jovi', 'title': 'When We Were Beautiful',
                               'album': 'Greatest Hits - The Ultimate Collection', 'cover_art_url': 'gh.jpg'}
    import metadata_repair as mr
    real = mr.deezer_release
    mr.deezer_release = lambda a, d=None, tolerance=None: metadata_repair_release
    try:
        fix_metadata._enrich_from_deezer(answers, 318)
    finally:
        mr.deezer_release = real
    assert (answers[0]['album'], answers[0]['cover_art_url']) == ('The Circle', 'circle.jpg')


def test_a_spotify_compilation_is_not_the_release(monkeypatch):
    import requests
    import spotify_web
    monkeypatch.setattr(requests, 'get', lambda *a, **k: SimpleNamespace(json=lambda: {'data': []}))
    track = SimpleNamespace(name='Law Fi Hayaty (Thousands Of Miles)', duration_ms=202000,
                            artists=[SimpleNamespace(name='Amr Mostafa'), SimpleNamespace(name='Orsi')],
                            album=SimpleNamespace(id='7MaG', name='Sunshine Arabia 2008', images=[]), url='', id='x')
    monkeypatch.setattr(spotify_web, 'get_client', lambda: SimpleNamespace(search=lambda q: SimpleNamespace(tracks=[track])))
    monkeypatch.setattr(spotify_web, 'get_album', lambda album_id: ('Sunshine Arabia 2008', 'Various Artists', []))
    assert metadata_repair._real_release_listing('Amr Mostafa', 'Law Fi Hayati', 202) is None
    monkeypatch.setattr(spotify_web, 'get_album', lambda album_id: ('Law Fi Hayaty', 'Amr Mostafa', []))
    assert metadata_repair._real_release_listing('Amr Mostafa', 'Law Fi Hayati', 202)['album'] == 'Sunshine Arabia 2008'


def test_a_library_edit_under_a_writer_credit_becomes_the_release(monkeypatch):
    # "Dana Hobson (BMI)" is a songwriter credit; ACRCloud splits the duo's
    # name; the 228 s library edit is replaced by the 216 s single.
    import audio_id
    swapped = _true_audio_setup(monkeypatch, 228, {'source': 'deezer', 'id': 135136818, 'duration': 216.0,
                                                   'artist': 'Dana and the Wolf', 'title': 'Close Enough',
                                                   'album': 'Close Enough'}, downloaded_len=216)
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': 'Dana, the Wolf', 'title': 'Close Enough', 'album': 'Close Enough', 'deezer_id': '135136818',
         'votes': 1},
        {'artist': 'Dana Hobson (BMI), Daniel Wolf (BMI)', 'title': 'Close Enough',
         'album': 'LAX 037 Dana & The Wolf - Attitude - EP', 'votes': 1}])
    monkeypatch.setattr(metadata_repair, 'deezer_release', lambda a, d=None, tolerance=None: {
        'artist': 'Dana and the Wolf', 'title': 'Close Enough', 'album': 'Close Enough', 'cover_art_url': 'dz.jpg'}
        if a.get('deezer_id') else None)
    monkeypatch.setattr(metadata_repair, 'is_known_artist', lambda name: name == 'Dana and the Wolf')
    written, stats = run(monkeypatch, ('Dana Hobson (BMI)', 'LAX 037 Dana & The Wolf - Attitude - EP', 'Close Enough'),
                         None)
    assert swapped == {'to': '/music/x.mp3'} and stats['fixed'] == 1
    assert written[-1] == {'artist': 'Dana and the Wolf', 'album': 'Close Enough', 'title': 'Close Enough'}


def test_an_emptied_folder_does_not_keep_its_cover(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'data'))
    old = tmp_path / 'albums' / 'KAROL G, Judeline, rusowsky - BbY WOW'
    new = tmp_path / 'albums' / 'KAROL G' / 'NO ME ARREPIENTO DE SENTIR TANTO'
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / 'song.mp3').write_bytes(b'x')
    (old / 'cover.jpg').write_bytes(b'old')
    (new / 'cover.jpg').write_bytes(b'album')
    moved = fix_metadata._reorganize_file(str(old / 'song.mp3'), {'artist': 'KAROL G', 'album': 'NO ME ARREPIENTO DE SENTIR TANTO',
                                                                  'title': 'BbY WOW'}, base_dir=str(tmp_path / 'albums'))
    assert moved == str(new / 'BbY WOW.mp3') and not old.exists() and (new / 'cover.jpg').read_bytes() == b'album'
    assert list((tmp_path / 'data').rglob('cover.jpg'))           # set aside, not deleted


def test_a_title_naming_another_act_gives_the_artist(monkeypatch):
    # The video's own name "Lil Baby - Freestyle"; the artist tag came from
    # what the download looked for.
    monkeypatch.setattr(metadata_repair, 'is_known_artist',
                        lambda n: n in ('Lil Baby', 'Lupe Fiasco', 'Daft Punk', 'Drake', 'Freestyle'))
    monkeypatch.setattr(metadata_repair, 'listed_length', lambda a, t: 162 if (a, t) == ('Lil Baby', 'Freestyle') else None)
    track = SimpleNamespace(title='Lil Baby - Freestyle', parentTitle='Freestyle', grandparentTitle='Lupe Fiasco',
                            originalTitle='Lupe Fiasco', parentRatingKey='1')
    meta = metadata_repair.known_meta(track, None)
    assert (meta['artist'], meta['title']) == ('Lil Baby', 'Freestyle')
    assert metadata_repair.split_title_artist('Daft Punk - Get Lucky (Cover)', 'Pentatonix') is None
    assert metadata_repair.split_title_artist('Drake - Make Them Pay', 'Drake') is None
    assert metadata_repair.split_title_artist('Lupe Fiasco - Drake', 'Someone') is None    # right side is an act


def test_a_withdrawn_singles_cover_comes_from_its_musicbrainz_release(monkeypatch):
    import artwork
    import mutagen
    import requests
    mbid = 'f5977fa6-1b08-4034-aabe-ebf5454e7b02'
    monkeypatch.setattr(mutagen, 'File', lambda path, easy=False: {'musicbrainz_albumid': [mbid]})
    monkeypatch.setattr(requests, 'get', lambda *a, **k: SimpleNamespace(json=lambda: {
        'title': 'Superbloodmoon', 'cover-art-archive': {'front': True},
        'artist-credit': [{'name': 'Holly Humberstone'}, ' & ', {'name': 'd4vd'}]}))
    meta = {'artist': 'Holly Humberstone', 'album': 'Superbloodmoon', 'title': 'Superbloodmoon (feat. d4vd)'}
    assert artwork._real_release_cover_url('/x.flac', meta) == f'https://coverartarchive.org/release/{mbid}/front'
    # A stale ID from before a rename is no evidence for the new names.
    assert artwork._real_release_cover_url('/x.flac', dict(meta, artist='Someone Else', album='Other')) is None


def test_a_fixed_song_joins_the_album_already_in_the_library(tmp_path, monkeypatch):
    import mutagen
    library = tmp_path / 'albums'
    album = library / 'Daddy Yankee' / 'Barrio Fino'
    lidarr = library / 'KAROL G' / 'NO ME ARREPIENTO DE SENTIR TANTO (2026)'
    for folder, song in ((album, 'Corazones.flac'), (lidarr, 'KAROL G - NO ME - 11 - BbY WOW.flac')):
        folder.mkdir(parents=True)
        (folder / song).write_bytes(b'x')
    tags = {str(album / 'Corazones.flac'): {'album': ['Barrio Fino'], 'albumartist': ['Daddy Yankee']},
            str(lidarr / 'KAROL G - NO ME - 11 - BbY WOW.flac'): {'album': ['NO ME ARREPIENTO DE SENTIR TANTO'],
                                                                  'albumartist': ['KAROL G']}}
    monkeypatch.setattr(mutagen, 'File', lambda path, easy=False: tags.get(str(path)))
    monkeypatch.setattr(fix_metadata, '_library_root_of', lambda fp: str(library))
    found = fix_metadata._real_existing_album(str(library / 'Reggaetones' / 'Top 100' / 'Gasolina.flac'),
                                              {'artist': 'Daddy Yankee', 'title': 'King Daddy',
                                               'album': 'Barrio Fino (Bonus Track Version)'})
    assert found == ('Barrio Fino', str(album))
    # An album that already holds the song is left alone (song_dedup's job).
    assert fix_metadata._real_existing_album(str(library / 'x' / 'y' / 'song.mp3'),
                                             {'artist': 'KAROL G', 'title': 'BbY WOW',
                                              'album': 'NO ME ARREPIENTO DE SENTIR TANTO'}) is None


def test_a_version_must_be_the_same_remix_not_just_the_same_kind():
    from artwork import result_matches
    mine = ('Hatiras', 'Spaced Invader (King Unique Radio Edit) (feat. John Slarta)', '')
    assert not result_matches(*mine, ['Hatiras'], 'Spaced Invader (Olav Basoski Radio Edit)', '')
    assert result_matches(*mine, ['Hatiras'], 'Spaced Invader (King Unique Radio Edit)', '')
    assert result_matches('Artist', 'Song (Radio Edit)', '', ['Artist'], 'Song [Radio Edit]', '')


def test_a_quoted_video_title_names_the_artist_and_song(monkeypatch):
    monkeypatch.setattr(metadata_repair, 'is_known_artist', lambda n: n == 'dead prez')
    title = 'Divine of the Dey ＂BabyFace＂ Directed by Jayonez'
    assert metadata_repair.split_title_artist(title, 'dead prez') == ('Divine of the Dey', 'BabyFace')
    track = SimpleNamespace(title=title, parentTitle='Divine of the Dey ＂BabyFace＂ [OFFICIAL VIDEO] Directed by Jayonez',
                            grandparentTitle='dead prez', originalTitle='dead prez', parentRatingKey='1')
    meta = metadata_repair.known_meta(track, None)
    assert meta == {'artist': 'Divine of the Dey', 'album': '', 'title': 'BabyFace'}
    # Without video wording or a listing, a quote in a title is just a quote.
    assert metadata_repair.split_title_artist('Someone "Nickname" Live', 'Band') is None


def test_a_title_prefixed_with_the_artist_loses_the_prefix():
    raw = 'THAO： Remember Me (From Amongst the Disquiet Film)'
    track = SimpleNamespace(title=raw, parentTitle=raw, grandparentTitle='Thao Sam', originalTitle='Thao Sam',
                            parentRatingKey='1')
    meta = metadata_repair.known_meta(track, None)
    assert meta == {'artist': 'Thao Sam', 'album': 'Remember Me (From Amongst the Disquiet Film)',
                    'title': 'Remember Me (From Amongst the Disquiet Film)'}
    assert metadata_repair._without_artist_prefix('Interlude: Coming Home', 'Thao Sam') == 'Interlude: Coming Home'


def test_a_proven_recordings_cleaned_names_are_written(monkeypatch):
    import artwork
    monkeypatch.setattr(artwork, 'own_upload_matches', lambda a, t, d, tolerance=3: True)
    raw = 'THAO： Remember Me (From Amongst the Disquiet Film)'
    written, stats = run(monkeypatch, ('Thao Sam', raw, raw), None, art='present')
    assert written == [{'artist': 'Thao Sam', 'album': 'Remember Me (From Amongst the Disquiet Film)',
                        'title': 'Remember Me (From Amongst the Disquiet Film)'}] and stats['fixed'] == 1


def test_a_services_no_cover_placeholder_is_no_cover(monkeypatch):
    import artwork
    monkeypatch.setattr(artwork, 'image_hash', lambda image: 0xffffc3c3c3c3ffff)
    assert artwork._is_placeholder(b'\xff\xd8\xff' + b'x' * 18000)
    assert not artwork._is_placeholder(b'\xff\xd8\xff' + b'x' * 90000)      # a real cover's size
    monkeypatch.setattr(artwork, 'image_hash', lambda image: 0x0123456789abcdef)
    assert not artwork._is_placeholder(b'\xff\xd8\xff' + b'x' * 18000)


def test_an_unidentified_rip_takes_the_artist_of_the_video_it_came_from(monkeypatch):
    import artwork
    import audio_id
    title = '528 Hz Música de Sanación Profunda para Dormir ｜ Repara y Cura a Nivel de ADN ｜ Meditación del Sueño'
    monkeypatch.setattr(metadata_repair, 'audio_duration', lambda p: 43189)
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': 'Medicamento para eliminar el estres', 'title': 'Escuchando el Silencio', 'album': 'Viaje', 'votes': 1}])
    monkeypatch.setattr(artwork, 'youtube_source', lambda t, d, tolerance=2: ('Soul Healing Música', 'iaKO3uyn8iQ')
                        if d == 43189 else None)
    written, _ = run(monkeypatch, ('Sonoterapia', title, title), None, art='fetched from YouTube thumbnail')
    assert written and written[0]['artist'] == 'Soul Healing Música' and written[0]['title'] == title


def test_a_performance_title_names_the_band(monkeypatch):
    import artwork
    raw = 'NO play ＂Dark Side of the Moon＂ by Pink Floyd'
    track = SimpleNamespace(title=raw, parentTitle=raw, grandparentTitle='Unidentified', originalTitle='',
                            parentRatingKey='1')
    assert metadata_repair.known_meta(track, None) == {'artist': 'NO', 'album': 'Dark Side of the Moon',
                                                       'title': 'Dark Side of the Moon'}
    # The source video proves the recording; its uploader is not the band.
    monkeypatch.setattr(artwork, 'youtube_source', lambda t, d, tolerance=2: ('truthseekers666', 'YvCzJ3vtfwg'))
    written, _ = run(monkeypatch, ('Unidentified', raw, raw), None, art='fetched from YouTube thumbnail')
    assert written == [{'artist': 'NO', 'album': 'Dark Side of the Moon', 'title': 'Dark Side of the Moon'}]


def test_a_title_naming_the_tagged_artist_keeps_the_artist(monkeypatch):
    import artwork
    raw = 'Itzhak Perlman – Beethoven： Violin Concerto (with Daniel Barenboim, Berliner Philharmoniker)'
    monkeypatch.setattr(artwork, 'youtube_source', lambda t, d, tolerance=2: ('Warner Classics', 'cokCgWPRZPg'))
    written, _ = run(monkeypatch, ('Itzhak Perlman', raw, raw), None, art='fetched from YouTube thumbnail')
    clean = 'Beethoven： Violin Concerto (with Daniel Barenboim, Berliner Philharmoniker)'
    assert written == [{'artist': 'Itzhak Perlman', 'album': clean, 'title': clean}]


def test_a_long_file_is_not_replaced_by_a_short_upload_nothing_vouches_for(monkeypatch):
    swapped = _true_audio_setup(monkeypatch, 2828, {'source': 'youtube', 'id': 'eepHWiEu3rY', 'duration': 109.0,
                                                    'artist': 'Itzhak Perlman', 'title': 'Beethoven: Violin Concerto',
                                                    'album': ''}, downloaded_len=109)
    run(monkeypatch, ('Itzhak Perlman', 'Beethoven: Violin Concerto', 'Beethoven: Violin Concerto'), None)
    assert swapped == {}


def test_a_recording_named_by_its_performers_takes_their_albums_cover(monkeypatch):
    import artwork
    import requests
    albums = {'search': [{'id': 1, 'title': 'Beethoven: Violin Concerto', 'artist': {'name': 'Itzhak Perlman'},
                          'cover_xl': 'giulini.jpg'},
                         {'id': 2, 'title': 'Beethoven: Violin Concerto & Romance for Violin',
                          'artist': {'name': 'Itzhak Perlman'}, 'cover_xl': 'barenboim.jpg'}],
              1: ['Itzhak Perlman', 'Philharmonia Orchestra', 'Carlo Maria Giulini'],
              2: ['Itzhak Perlman', 'Berliner Philharmoniker', 'Daniel Barenboim']}

    def get(url, params=None, timeout=None):
        if url.endswith('/search/album'):
            return SimpleNamespace(json=lambda: {'data': albums['search']})
        key = int(url.rsplit('/', 1)[1])
        return SimpleNamespace(json=lambda: {'contributors': [{'name': n} for n in albums[key]]})
    monkeypatch.setattr(requests, 'get', get)
    title = 'Beethoven: Violin Concerto (with Daniel Barenboim, Berliner Philharmoniker)'
    assert artwork._real_deezer_album('Itzhak Perlman', title, title) == 'barenboim.jpg'
    assert not artwork.result_matches('Itzhak Perlman', title, title,
                                      ['Itzhak Perlman, Philharmonia Orchestra & Carlo Maria Giulini'],
                                      'Beethoven: Violin Concerto', 'Beethoven: Violin Concerto', for_cover=True)


def test_a_featured_credit_alone_does_not_change_the_title(monkeypatch):
    import audio_id
    monkeypatch.setattr(audio_id, 'identify_by_votes', lambda fp: [
        {'artist': 'Holly Humberstone', 'title': 'Superbloodmoon (feat. d4vd)', 'album': 'Superbloodmoon', 'votes': 7}])
    written, _ = run(monkeypatch, ('Holly Humberstone', '', 'Superbloodmoon'), None, art='present')
    assert written and written[0]['title'] == 'Superbloodmoon'


def _fake_lidarr(tmp_path, artist_path):
    from lidarr_catalog import LidarrAlbum, LidarrCatalog
    cat = LidarrCatalog('http://lidarr', 'key')
    cat.root_folders = [str(tmp_path / 'albums')]
    album = LidarrAlbum(id=6898, artist_id=1, artist_name='KAROL G', artist_path=str(artist_path),
                        title='NO ME ARREPIENTO DE SENTIR TANTO', year=2026, release_group_id='rg')
    album.track_file_dir = ''
    cat.albums = [album]
    cat._by_artist = {'karolg': [album]}
    cat._tracks = {6898: [{'title': 'Maybe', 'absoluteTrackNumber': 2, 'mediumNumber': 1},
                          {'title': 'BbY WOW', 'absoluteTrackNumber': 11, 'mediumNumber': 1}]}
    cat.loaded = True
    return cat


def test_a_fixed_song_is_filed_the_way_lidarr_files_it(tmp_path, monkeypatch):
    monkeypatch.setattr(fix_metadata, '_library_root_of', lambda fp: str(tmp_path / 'albums'))
    cat = _fake_lidarr(tmp_path, tmp_path / 'albums' / 'KAROL G')
    monkeypatch.setattr(fix_metadata, '_lidarr_catalog', lambda: cat)
    song = tmp_path / 'albums' / 'x' / 'y' / 'song.mp3'
    names, folder, name = fix_metadata._lidarr_place(str(song), {'artist': 'KAROL G', 'title': 'BbY WOW',
                                                                  'album': 'NO ME ARREPIENTO DE SENTIR TANTO'})
    assert folder == str(tmp_path / 'albums' / 'KAROL G' / 'NO ME ARREPIENTO DE SENTIR TANTO (2026)')
    assert name == 'KAROL G - NO ME ARREPIENTO DE SENTIR TANTO - 11 - BbY WOW.mp3'
    assert names['track'] == 11 and names['album'] == 'NO ME ARREPIENTO DE SENTIR TANTO'
    # Lidarr's folder already holding the track: the copy is song_dedup's.
    os.makedirs(folder)
    open(os.path.join(folder, 'KAROL G - NO ME ARREPIENTO DE SENTIR TANTO - 11 - BbY WOW.flac'), 'w').close()
    assert fix_metadata._lidarr_place(str(song), {'artist': 'KAROL G', 'title': 'BbY WOW',
                                                  'album': 'NO ME ARREPIENTO DE SENTIR TANTO'}) is None
    # A song Lidarr doesn't list on the album gets no place from Lidarr.
    assert fix_metadata._lidarr_place(str(song), {'artist': 'KAROL G', 'title': 'Provenza',
                                                  'album': 'NO ME ARREPIENTO DE SENTIR TANTO'}) is None


def test_lidarr_filing_leaves_its_own_files_and_without_lidarr_albums_stay_together(tmp_path, monkeypatch):
    import mutagen
    cat = _fake_lidarr(tmp_path, tmp_path / 'albums' / 'KAROL G')
    owned = tmp_path / 'albums' / 'KAROL G' / 'old name.flac'
    cat._track_file_paths = {os.path.normpath(str(owned))}
    monkeypatch.setattr(fix_metadata, '_lidarr_catalog', lambda: cat)
    monkeypatch.setattr(fix_metadata, '_library_root_of', lambda fp: str(tmp_path / 'albums'))
    monkeypatch.setattr(fix_metadata, '_file_tags', lambda p: {'artist': 'KAROL G', 'title': 'Maybe', 'album': 'x'})
    meta = {'artist': 'KAROL G', 'title': 'Maybe', 'album': 'NO ME ARREPIENTO DE SENTIR TANTO'}
    assert fix_metadata._file_away(str(owned), meta) == (meta, os.path.dirname(owned), 'old name.flac')
    # Without Lidarr, the album's existing folder wins over a new one.
    monkeypatch.setattr(fix_metadata, '_lidarr_catalog', lambda: None)
    monkeypatch.setattr(fix_metadata, '_existing_album',
                        lambda fp, m: ('NO ME ARREPIENTO DE SENTIR TANTO', str(tmp_path / 'albums' / 'KAROL G' / 'NMA')))
    names, folder, name = fix_metadata._file_away(str(tmp_path / 'albums' / 'a' / 'b' / 'Maybe.mp3'), meta)
    assert folder.endswith('NMA') and name is None


def test_lidarr_prefers_the_album_named_exactly():
    from lidarr_catalog import LidarrAlbum, LidarrCatalog
    cat = LidarrCatalog('http://lidarr', 'key')
    red = LidarrAlbum(id=1, artist_id=1, artist_name='Taylor Swift', artist_path='', title='Red', year=2012,
                      release_group_id='a')
    tv = LidarrAlbum(id=2, artist_id=1, artist_name='Taylor Swift', artist_path='', title='Red (Taylor’s Version)',
                     year=2021, release_group_id='b')
    cat._by_artist = {'taylorswift': [tv, red]}
    assert cat.match_album('Taylor Swift', ['Red']).id == 1
    assert cat.match_album('Taylor Swift', ["Red (Taylor's Version)"]).id == 2


def test_lidarrs_track_is_the_song_not_its_commentary(tmp_path):
    cat = _fake_lidarr(tmp_path, tmp_path / 'albums' / 'Taylor Swift')
    album = cat.albums[0]
    cat._tracks = {album.id: [{'title': 'Everything Has Changed (commentary)', 'absoluteTrackNumber': 27},
                              {'title': 'Everything Has Changed', 'absoluteTrackNumber': 14}]}
    assert cat.find_track(album, 'Everything Has Changed')['track'] == 14


def test_a_genius_answer_fixes_a_song_with_no_artist_whose_name_agrees(monkeypatch):
    # "Unidentified – Unknown ♫Rain♫" in Fix Metadata; only Genius knows
    # Maddhouse's "Rain" (no lyrics anywhere to compare).
    monkeypatch.setattr(metadata_repair, 'deezer_albums_for_track', lambda *a, **k: [])
    monkeypatch.setattr(metadata_repair, 'lidarr_confirms_album', lambda *a, **k: False)
    rain = {'artist': 'Maddhouse', 'title': 'Rain', 'album': 'Rain', 'votes': 1, 'unconfirmed': True,
            'service': 'Lyrics (Genius)'}
    written, stats = run(monkeypatch, ('Unidentified', 'Unknown ♫Rain♫', 'Unknown ♫Rain♫'), rain)
    assert written and (written[0]['artist'], written[0]['title'], written[0]['album']) == ('Maddhouse', 'Rain', 'Rain')
    # A name that doesn't agree with the answer, or a real artist: a person decides.
    written, stats = run(monkeypatch, ('Unidentified', 'Unknown', 'Sunshine'), rain)
    assert written == []
    written, stats = run(monkeypatch, ('Some Band', 'Album', 'Rain'), rain)
    assert written == []
