"""AI playlists: the AI chooses only from the library's own lists."""
import re
from types import SimpleNamespace

import pytest

import ai_playlist


def _tag(name):
    return SimpleNamespace(tag=name)


def _track(artist, title, album='Album', rating=None, key=None, album_key='al1', plays=0):
    return SimpleNamespace(grandparentTitle=artist, originalTitle=None, title=title, parentTitle=album,
                           parentRatingKey=album_key, userRating=rating, viewCount=plays,
                           ratingKey=key or f'{artist}/{title}')


class FakeSection:
    def __init__(self, tracks, genres, countries=None, years=None):
        self.tracks = tracks
        self.genres = genres
        self.countries = countries or {}
        self.years = years or {'al1': 2001}

    def searchArtists(self):
        return [SimpleNamespace(title=a, genres=[_tag(g) for g in gs], styles=[],
                                countries=[_tag(c) for c in self.countries.get(a, [])])
                for a, gs in self.genres.items()]

    def searchAlbums(self):
        return [SimpleNamespace(ratingKey=k, year=y) for k, y in self.years.items()]

    def searchTracks(self):
        return list(self.tracks)


class FakeServer:
    def __init__(self, existing=()):
        self.saved = []
        self.deleted = []
        self._playlists = [SimpleNamespace(title=t, delete=lambda t=t: self.deleted.append(t)) for t in existing]

    def playlists(self):
        return self._playlists

    def createPlaylist(self, title, items):
        self.saved.append((title, items))
        return SimpleNamespace(title=title)


@pytest.fixture
def library():
    tracks = [_track('Calm Pianist', f'Nocturne {i}') for i in range(1, 6)]
    tracks += [_track('Loud Band', 'Scream'), _track('Calm Pianist', 'Hated Etude', rating=2)]
    genres = {'Calm Pianist': ['Classical', 'Piano'], 'Loud Band': ['Metal']}
    return FakeSection(tracks, genres)


def _listed(prompt, wanted):
    """The numbers the prompt gives the lines containing these words."""
    lines = [m for line in prompt.splitlines() if (m := re.match(r'(\d+)\. (.*)', line))]
    return [int(m.group(1)) for w in wanted for m in lines if w in m.group(2)]


def test_the_ai_chooses_artists_then_songs_by_their_numbers(library, monkeypatch):
    asked = []

    def fake_ask(system, prompt, **kw):
        asked.append(prompt)
        if 'Read the request' in prompt:
            return {'from_year': None, 'to_year': None, 'countries': [], 'language': None}
        if 'Reply {"artists"' in prompt:
            return {'artists': _listed(prompt, ['Calm Pianist']) + [99, 'x']}   # unknown numbers are ignored
        return {'songs': _listed(prompt, ['Nocturne 3', 'Nocturne 1', 'Nocturne 2']) + [1]}

    monkeypatch.setattr(ai_playlist, 'ask', fake_ask)
    server = FakeServer(existing=['AI: calm piano for studying', 'Someone else'])
    result = ai_playlist.make(server, library, '  calm piano   for studying ', 10)
    assert result['ok'] and result['title'] == 'AI: calm piano for studying'
    assert result['songs'] == [('Calm Pianist', 'Nocturne 3'), ('Calm Pianist', 'Nocturne 1'),
                               ('Calm Pianist', 'Nocturne 2')]
    assert server.deleted == ['AI: calm piano for studying']             # the earlier one is replaced
    assert [t.title for t in server.saved[0][1]] == ['Nocturne 3', 'Nocturne 1', 'Nocturne 2']
    assert 'Classical, Piano' in asked[1] and 'Loud Band' in asked[1]
    assert 'Hated Etude' not in asked[2] and 'Scream' not in asked[2]     # 1★ songs and other artists: not offered
    assert '(2001 · Album)' in asked[2]                                   # the album's year


def test_nothing_is_saved_when_the_ai_finds_too_little(library, monkeypatch):
    monkeypatch.setattr(ai_playlist, 'ask', lambda system, prompt, **kw: None)
    server = FakeServer()
    result = ai_playlist.make(server, library, 'sea shanties', 10)
    assert not result['ok'] and server.saved == []


def test_one_artist_may_not_fill_a_mixed_playlist(monkeypatch):
    tracks = [_track('A', f'a{i}') for i in range(10)] + [_track('B', f'b{i}') for i in range(3)]
    artists = [ai_playlist.Artist('A'), ai_playlist.Artist('B')]
    artists[0].tracks, artists[1].tracks = tracks[:10], tracks[10:]
    monkeypatch.setattr(ai_playlist, 'ask', lambda system, prompt, **kw: {'songs': list(range(1, 14))})
    chosen = ai_playlist.choose_songs('anything', artists, 9)
    assert [t.title for t in chosen] == ['a0', 'a1', 'a2', 'b0', 'b1', 'b2']


def test_a_long_description_makes_a_short_title():
    title = ai_playlist.playlist_title('songs for a very long drive across the whole country at night with friends')
    assert title.startswith('AI: songs for a very long drive') and title.endswith('…')
    assert len(title) <= len(ai_playlist.PREFIX) + ai_playlist.TITLE_LENGTH + 1


def test_the_playlist_key_is_used_when_set(monkeypatch):
    monkeypatch.setenv('LOCAL_AI_API_BASE', 'http://ai/v1/')
    monkeypatch.setenv('LOCAL_AI_API_KEY', 'shared')
    monkeypatch.setenv('LOCAL_AI_MODEL', 'm')
    monkeypatch.setenv('AI_PLAYLIST_API_KEY', 'own')
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, model=json['model'])
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {'choices': [{'message': {'content': 'Sure: {"artists": [1]}'}}]})

    monkeypatch.setattr(ai_playlist.requests, 'post', fake_post)
    assert ai_playlist.ask('s', 'p') == {'artists': [1]}
    assert sent == {'url': 'http://ai/v1/chat/completions', 'headers': {'Authorization': 'Bearer own'}, 'model': 'm'}


def test_the_filters_leave_only_the_years_and_places_asked_for():
    section = FakeSection(
        [_track('Old Rapper', 'Nineties', album_key='a90'), _track('Old Rapper', 'Noughties', album_key='a00'),
         _track('Desert Singer', 'Sunset', album_key='a00'), _track('Untagged Singer', 'أغنية الصباح', album_key='a00'),
         _track('Untagged Singer', 'Morning Light', album_key='a00'), _track('No Year', 'Song', album_key='x')],
        {'Old Rapper': ['Hip Hop'], 'Desert Singer': ['Rai'], 'Untagged Singer': [], 'No Year': []},
        countries={'Old Rapper': ['United States of America'], 'Desert Singer': ['Algeria']},
        years={'a90': 1995, 'a00': 2004})
    artists = ai_playlist.catalogue(section)
    noughties = ai_playlist.narrowed(artists, ai_playlist.Wish(2000, 2009))
    assert {(a.name, t.title) for a in noughties for t in a.tracks} == {
        ('Old Rapper', 'Noughties'), ('Desert Singer', 'Sunset'),
        ('Untagged Singer', 'أغنية الصباح'), ('Untagged Singer', 'Morning Light')}
    arabic = ai_playlist.narrowed(artists, ai_playlist.Wish(countries=['Algeria'], language='Arabic'))
    assert [a.name for a in arabic] == ['Desert Singer', 'Untagged Singer']   # by country, or by script
    assert ai_playlist.narrowed(artists, ai_playlist.Wish(countries=['Japan'])) == []


def test_the_ai_reads_the_filters_but_only_known_countries_and_plain_years_count(monkeypatch):
    monkeypatch.setattr(ai_playlist, 'ask', lambda system, prompt, **kw: {
        'from_year': '2000', 'to_year': 3000, 'countries': ['algeria', 'Atlantis'], 'language': 'Arabic'})
    wish = ai_playlist.read_wish('arabic party', ['Algeria', 'France'])
    assert (wish.from_year, wish.to_year, wish.countries, wish.language) == (2000, None, {'algeria'}, 'arabic')


def test_a_request_nothing_in_the_library_fits_says_so(library, monkeypatch):
    monkeypatch.setattr(ai_playlist, 'ask', lambda system, prompt, **kw: {
        'from_year': 1960, 'to_year': 1969, 'countries': [], 'language': None})
    server = FakeServer()
    result = ai_playlist.make(server, library, '60s soul', 10)
    assert not result['ok'] and 'no songs that fit' in result['message'] and server.saved == []
