"""Identifying a song by its sung lyrics when fingerprinting finds nothing."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import lyrics_id

FREESTYLE = ("Hold it down for the four end of the night with the wolves all of my dollars for shows "
             "we won't fall out over nothing I got my wrist on froze I got my people on go")
HEARD = ("Hold it down for the foe end of the night with the wolves all of my dollars for shows "
         "we won't fall out by nothing I got my wrists on froze I got my people on go")
OTHER = ("You're holding on can't pretend that nothing's changed living in the shadows of the love "
         "we made back when we were beautiful before the world got small")


def test_an_instrumental_has_no_vocals():
    assert not lyrics_id.has_vocals("Thank you for watching. ♪ Music ♪")
    assert lyrics_id.has_vocals(HEARD)


def test_the_right_lyrics_score_far_above_the_wrong_ones():
    right, wrong = lyrics_id.lyrics_score(HEARD, FREESTYLE), lyrics_id.lyrics_score(HEARD, OTHER)
    assert right >= lyrics_id.MATCH_SCORE > wrong + lyrics_id.MATCH_LEAD


def test_whispers_unpunctuated_lines_are_split_at_capitals():
    lines = lyrics_id.search_lines("You're holding on Can't pretend that nothing's changed Living in the "
                                   "shadows of the love we made. WHERE WE WANNATED ALONE BY If I", 3)
    assert "Living in the shadows of the love" in lines and not any(l.isupper() for l in lines)


def test_result_titles_are_read_both_ways_round():
    assert ('Bon Jovi', 'When We Were Beautiful') in lyrics_id._pairs_from_title(
        'When We Were Beautiful Lyrics - Bon Jovi')
    assert lyrics_id._pairs_from_title('Wolves lyrics by Down Like Silver, 8 meanings') == [('Down Like Silver', 'Wolves')]


def _identify(monkeypatch, records, length):
    monkeypatch.setattr(lyrics_id, 'ENABLED', True)
    monkeypatch.setattr(lyrics_id, 'genius_search', lambda t: [])
    monkeypatch.setattr(lyrics_id, 'candidates', lambda t, genius=None: [('Lil Baby', 'Freestyle'), ('Bon Jovi', 'Beautiful')])
    monkeypatch.setattr(lyrics_id, '_lrclib', lambda a, t: records.get((a, t), []))
    monkeypatch.setattr(lyrics_id, '_duration', lambda p: length)
    return lyrics_id._real_identify('/music/x.mp3', transcript=HEARD)


def test_a_lyrics_match_at_the_files_length_identifies_it(monkeypatch):
    records = {('Lil Baby', 'Freestyle'): [{'artistName': 'Lil Baby', 'trackName': 'Freestyle', 'albumName': 'Too Hard',
                                            'duration': 162.0, 'plainLyrics': FREESTYLE}],
               ('Bon Jovi', 'Beautiful'): [{'artistName': 'Bon Jovi', 'trackName': 'Beautiful', 'duration': 318.0,
                                            'plainLyrics': OTHER}]}
    found = _identify(monkeypatch, records, 163)
    assert (found['artist'], found['title'], found['votes'], found['service']) == ('Lil Baby', 'Freestyle', 2, 'Lyrics')
    # Another length: the song, maybe not this recording — only a suggestion.
    assert _identify(monkeypatch, records, 240)['votes'] == 1


def test_no_clear_lyrics_match_identifies_nothing(monkeypatch):
    records = {('Bon Jovi', 'Beautiful'): [{'artistName': 'Bon Jovi', 'trackName': 'Beautiful', 'duration': 163.0,
                                            'plainLyrics': OTHER}]}
    assert _identify(monkeypatch, records, 163) is None


def test_web_searches_keep_within_their_allowance(tmp_path, monkeypatch):
    monkeypatch.setattr(lyrics_id, '_BUDGET_FILE', str(tmp_path / 'budget.json'))
    monkeypatch.setattr(lyrics_id, 'SEARX_PER_DAY', 2)
    monkeypatch.setattr(lyrics_id, 'SEARX_INTERVAL', 0)
    assert lyrics_id._spend_search() and lyrics_id._spend_search() and not lyrics_id._spend_search()
    monkeypatch.setattr(lyrics_id, 'SEARX_PER_DAY', 99)
    lyrics_id._pause_searches()
    assert not lyrics_id._spend_search()                               # paused after a suspension


def test_lyrics_are_tried_only_when_fingerprinting_finds_nothing(monkeypatch):
    import audio_id
    import mutagen
    monkeypatch.setattr(mutagen, 'File', lambda path: SimpleNamespace(info=SimpleNamespace(length=200)))
    monkeypatch.setattr(audio_id.subprocess, 'run', lambda *a, **k: None)          # no slices made
    monkeypatch.setattr(audio_id, '_identify_with_acoustid', lambda path: None)
    monkeypatch.setattr(lyrics_id, 'identify', lambda path, transcript=None: {
        'artist': 'Lil Baby', 'title': 'Freestyle', 'album': 'Too Hard', 'service': 'Lyrics', 'votes': 2})
    assert audio_id.identify_by_votes('/music/x.mp3')[0]['service'] == 'Lyrics'
    monkeypatch.setattr(audio_id, '_identify_with_acoustid', lambda path: {'artist': 'A', 'title': 'B'})
    assert audio_id.identify_by_votes('/music/x.mp3')[0]['service'] == 'AcoustID'


RAIN = ("And I know we're both confused, but I need to be with you Now I, I, I, never meant to make that rain "
        "fall from your eyes Got me drowning in the rain Rain, rain, go away, never till we turn again "
        "Rain, rain, go away, go away, go away Now I, I, I Never meant to make that raindrop from you")


def test_without_lyrics_to_compare_genius_top_answer_is_only_a_suggestion(monkeypatch):
    # Maddhouse's "Rain" is on Genius only; LRCLIB has no lyrics for it.
    genius = [("If there's no you and I, I", [('Hayd', 'All of the Stars'), ('INNA', 'More Than Friends')]),
              ("And I know we're both confused, but", [('Maddhouse', 'Rain'), ('Michl', 'Broken Roots')]),
              ("Now I, I, I Never meant to", [('Demi Lovato', 'It’s Not Too Late')])]
    assert lyrics_id.unconfirmed_suggestion(RAIN, genius) == ('Maddhouse', 'Rain')
    monkeypatch.setattr(lyrics_id, '_duration', lambda p: 213.0)
    monkeypatch.setattr(lyrics_id, 'genius_search', lambda t: genius)
    monkeypatch.setattr(lyrics_id, '_lrclib', lambda a, t: [])
    monkeypatch.setattr(lyrics_id, 'ENABLED', True)
    found = lyrics_id._real_identify('/m/rain.mp3', transcript=RAIN)
    assert (found['artist'], found['title'], found['votes'], found['unconfirmed']) == ('Maddhouse', 'Rain', 1, True)


def test_a_title_not_sung_or_two_candidates_suggest_nothing():
    assert lyrics_id.unconfirmed_suggestion(RAIN, [("x", [('Hayd', 'All of the Stars')])]) is None
    two = [("a", [('Maddhouse', 'Rain')]), ("b", [('Lil Tracy', 'rain rain go away')])]
    assert lyrics_id.unconfirmed_suggestion(RAIN, two) is None


def test_repeated_lines_are_searched_once():
    lines = lyrics_id.search_lines("Now I, I, I, never meant to make that rain fall. "
                                   "Now I, I, I Never meant to make that raindrop fall.", 8)
    assert len(lines) == 1


def test_an_unconfirmed_suggestion_is_never_applied_on_its_own():
    import fix_metadata
    known = {'artist': 'Unidentified', 'title': 'Rain', 'album': 'Rain'}
    answer = {'artist': 'Maddhouse', 'title': 'Rain', 'votes': 1, 'unconfirmed': True}
    assert not fix_metadata._supported(known, answer, [answer], names=['Rain'])
