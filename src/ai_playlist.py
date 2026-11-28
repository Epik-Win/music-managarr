"""AI playlists: a listener describes a playlist in their own words and the
local AI fills it from the library (`/ai_playlist` in Discord).

The AI never names a song of its own. Asked for songs outright, a model
answers with songs the library doesn't have, or with real songs given to
the wrong artist. So it works from the library's own lists instead:

1. It reads the request into filters: the years it asks for, and the
   countries it is about, chosen from the countries the library's artists
   come from. The code keeps only the songs of albums from those years and
   the artists of those countries (or, for a language with its own script,
   the artists whose names or song titles are written in it). A model asked
   for "Arabic" or "the 2000s" from a list of a thousand artists fills the
   playlist with whatever is popular; filtered lists leave it nothing else.
   For a theme, holiday or occasion ("Christmas", "Halloween", "a wedding")
   it also gives the words such songs and albums are titled with; the songs
   whose title or album has one of them, across the whole library, are the
   ones offered, and step 2 is skipped (a Christmas album hides among any
   artist's records, and no artist list says "Christmas").
2. It chooses the artists that fit, by their numbers in the list of those
   artists (with their genres, countries and how many songs they have here).
3. It chooses the songs, by their numbers in those artists' own song lists,
   in the order they should play.

Every number is checked against the lists, so every song in the playlist is
one the library has. The playlist is made on the asker's own Plex account
and leaves out the songs they rated 1★. Asking again with the same words
replaces that playlist.

AI_PLAYLIST_API_KEY and AI_PLAYLIST_MODEL choose the AI connection (by
default the LOCAL_AI_* settings). The lists are long, so the model needs a
large context: with an OpenAI-compatible proxy such as LiteLLM, a key of
its own lets the proxy send these requests to a large model.
"""
import json
import logging
import os
import re
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

import requests

logger = logging.getLogger('musicai.ai_playlist')


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


DEFAULT_SONGS = max(5, _env_int('AI_PLAYLIST_SONGS', 25))
MAX_SONGS = max(DEFAULT_SONGS, _env_int('AI_PLAYLIST_MAX_SONGS', 50))
MAX_ARTISTS = max(3, _env_int('AI_PLAYLIST_ARTISTS', 12))
SONGS_PER_ARTIST_LISTED = max(10, _env_int('AI_PLAYLIST_ARTIST_SONGS', 60))
PREFIX = os.environ.get('AI_PLAYLIST_PREFIX', 'AI: ')
TITLE_LENGTH = 60
DISLIKED_RATING = 2                 # Plex ratings are 0–10: 2 is 1★
MIN_SONGS = 3
TIMEOUT = max(30, _env_int('AI_PLAYLIST_TIMEOUT', 300))
THEME_SONGS_LISTED = max(50, _env_int('AI_PLAYLIST_THEME_SONGS', 400))   # songs a theme offers at most
# Longest answer asked for: a list of numbers or words is short, and a model
# that starts repeating itself would otherwise write until it runs out of
# room, holding the AI server for everyone meanwhile.
MAX_ANSWER_TOKENS = max(200, _env_int('AI_PLAYLIST_MAX_TOKENS', 1000))
MAX_WORDS = 30


class Artist:
    """One artist of the library, with their songs the listener hasn't rated 1★."""

    def __init__(self, name: str, genres: Sequence[str] = (), countries: Sequence[str] = ()):
        self.name = name
        self.genres = list(genres)
        self.countries = list(countries)
        self.tracks: List[object] = []
        self.years: Dict[object, int] = {}        # a song's ratingKey → its album's year


# -- the library ----------------------------------------------------------------

def _tags(item, attr: str) -> List[str]:
    return [t.tag for t in (getattr(item, attr, None) or []) if getattr(t, 'tag', None)]


def _artist_of(track) -> str:
    return (getattr(track, 'grandparentTitle', None) or getattr(track, 'originalTitle', None) or '').strip()


def _liking(track) -> Tuple[float, float]:
    return (getattr(track, 'userRating', None) or 0, getattr(track, 'viewCount', None) or 0)


def catalogue(section) -> List[Artist]:
    """The library's artists with their songs, as the listener's own
    connection sees them (their ratings and plays), leaving out songs rated
    1★. The artists they play most come first, and each artist's songs
    they rate and play most: a model leans on what a list shows first."""
    tags: Dict[str, Tuple[List[str], List[str]]] = {}
    try:
        for artist in section.searchArtists():
            tags[artist.title.casefold()] = ((_tags(artist, 'genres') + _tags(artist, 'styles'))[:4],
                                             _tags(artist, 'countries')[:2])
    except Exception as exc:
        logger.debug(f"Could not read the artists' genres: {exc}")
    years: Dict[str, int] = {}
    try:
        years = {str(album.ratingKey): album.year for album in section.searchAlbums() if album.year}
    except Exception as exc:
        logger.debug(f"Could not read the albums' years: {exc}")
    artists: Dict[str, Artist] = {}
    for track in section.searchTracks():
        name = _artist_of(track)
        if not name or name.startswith('<') or not (track.title or '').strip():
            continue
        rating = getattr(track, 'userRating', None) or 0
        if 0 < rating <= DISLIKED_RATING:
            continue
        artist = artists.get(name.casefold())
        if artist is None:
            genres, countries = tags.get(name.casefold(), ((), ()))
            artist = artists[name.casefold()] = Artist(name, genres, countries)
        artist.tracks.append(track)
        year = years.get(str(getattr(track, 'parentRatingKey', '')))
        if year:
            artist.years[track.ratingKey] = year
    for artist in artists.values():
        artist.tracks.sort(key=lambda t: tuple(-v for v in _liking(t)))
    plays = lambda a: sum(getattr(t, 'viewCount', None) or 0 for t in a.tracks)
    return sorted(artists.values(), key=lambda a: (-plays(a), a.name.casefold()))


# -- what the request asks for -----------------------------------------------

# Languages written in a script of their own: an artist with no country in
# Plex still counts when their names or titles are written in it.
SCRIPTS = {
    'arabic': r'[\u0600-\u06FF\u0750-\u077F]', 'persian': r'[\u0600-\u06FF]', 'urdu': r'[\u0600-\u06FF]',
    'hebrew': r'[\u0590-\u05FF]', 'russian': r'[\u0400-\u04FF]', 'ukrainian': r'[\u0400-\u04FF]',
    'greek': r'[\u0370-\u03FF]', 'hindi': r'[\u0900-\u097F]', 'korean': r'[\uAC00-\uD7AF]',
    'japanese': r'[\u3040-\u30FF]', 'chinese': r'[\u4E00-\u9FFF]', 'thai': r'[\u0E00-\u0E7F]',
    'armenian': r'[\u0530-\u058F]', 'georgian': r'[\u10A0-\u10FF]',
}


class Wish:
    """The filters a request sets: album years, artists' countries, a language,
    and for a theme the words its songs' or albums' titles hold."""

    def __init__(self, from_year: Optional[int] = None, to_year: Optional[int] = None,
                 countries: Sequence[str] = (), language: Optional[str] = None, words: Sequence[str] = ()):
        self.from_year, self.to_year = from_year, to_year
        self.countries = {c.casefold() for c in countries}
        self.language = (language or '').strip().casefold() or None
        self.words = [w.casefold() for w in words]
        self._theme = (re.compile('|'.join(rf'(?<!\w){re.escape(w)}(?!\w)' for w in self.words), re.IGNORECASE)
                       if self.words else None)

    def on_theme(self, track) -> bool:
        """The song's title or its album's has one of the theme's words."""
        if self._theme is None:
            return False
        return bool(self._theme.search(getattr(track, 'title', '') or '')
                    or self._theme.search(getattr(track, 'parentTitle', '') or ''))

    def year_ok(self, year: Optional[int]) -> bool:
        if self.from_year is None and self.to_year is None:
            return True
        return bool(year) and (self.from_year or 0) <= year <= (self.to_year or 9999)

    def place_ok(self, artist: 'Artist') -> bool:
        if not self.countries and not self.language:
            return True
        if self.countries & {c.casefold() for c in artist.countries}:
            return True
        script = SCRIPTS.get(self.language or '')
        if script:
            texts = [artist.name] + [getattr(t, 'title', '') or '' for t in artist.tracks]
            return any(re.search(script, text) for text in texts)
        return False

    def describe(self) -> str:
        parts = []
        if self.from_year or self.to_year:
            parts.append(f"from {self.from_year or '…'} to {self.to_year or '…'}")
        if self.countries:
            parts.append('from ' + ', '.join(sorted(self.countries)))
        if self.language:
            parts.append(f"in {self.language}")
        if self.words:
            parts.append('titled ' + '/'.join(self.words[:8]) + ('…' if len(self.words) > 8 else ''))
        return ', '.join(parts)


def _year(value) -> Optional[int]:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


def read_wish(description: str, countries: Sequence[str]) -> Wish:
    """The request's filters, read by the AI; countries only from `countries`
    (those of the library's artists), years only as plain years."""
    reply = ask(SYSTEM, (
        f'A listener asked for this playlist: "{description}"\n\n'
        'Countries the artists of their library come from: ' + '; '.join(countries) + '\n\n'
        'Read the request into filters. from_year and to_year: the years it asks for, only if it names '
        'a decade, year or era ("2000s" is 2000 to 2009), else null. countries: only if it asks for music '
        'of a region, people or language, every country from the list above that is in that region or '
        'where that language is mainly spoken, else []. '
        'language: only if it asks for songs in a language, or of a people or culture named after its '
        'language ("Arabic", "French", "Korean"), that language in English, else null. '
        'words: only if it asks for a theme, holiday, season, occasion or topic (Christmas, Halloween, a '
        'wedding, summer, rain), the words or short phrases songs and albums about it are titled with, in '
        'English and in the other languages of the countries above, at most 30 (Christmas: christmas, '
        'xmas, santa, noel, navidad, jingle, sleigh, mistletoe, snow, reindeer …), else []. A genre, mood, '
        'decade or artist is not a theme. '
        'Reply {"from_year": null, "to_year": null, "countries": [], "language": null, "words": []}.'),
        temperature=0.0)
    reply = reply or {}
    known = {c.casefold(): c for c in countries}
    chosen = [known[str(c).casefold()] for c in reply.get('countries') or [] if str(c).casefold() in known]
    language = reply.get('language') if isinstance(reply.get('language'), str) else None
    words = []
    for word in reply.get('words') if isinstance(reply.get('words'), list) else []:
        word = ' '.join(str(word).split()) if isinstance(word, str) else ''
        if 3 <= len(word) <= 40 and word.casefold() not in words:
            words.append(word.casefold())
    return Wish(_year(reply.get('from_year')), _year(reply.get('to_year')), chosen, language, words[:MAX_WORDS])


def narrowed(artists: Sequence[Artist], wish: Wish) -> List[Artist]:
    """The artists and songs the request's filters leave."""
    out = []
    for artist in artists:
        if not wish.place_ok(artist):
            continue
        kept = Artist(artist.name, artist.genres, artist.countries)
        kept.years = artist.years
        kept.tracks = [t for t in artist.tracks if wish.year_ok(artist.years.get(t.ratingKey))]
        if kept.tracks:
            out.append(kept)
    return out


def themed(artists: Sequence[Artist], wish: Wish) -> List[Artist]:
    """The artists with only their songs on the request's theme, the most such
    songs first, at most THEME_SONGS_LISTED songs in all."""
    out, total = [], 0
    for artist in artists:
        on = [t for t in artist.tracks if wish.on_theme(t)]
        if on:
            kept = Artist(artist.name, artist.genres, artist.countries)
            kept.years, kept.tracks = artist.years, on
            out.append(kept)
    out.sort(key=lambda a: -len(a.tracks))
    for artist in out:
        artist.tracks = artist.tracks[:max(0, THEME_SONGS_LISTED - total)]
        total += len(artist.tracks)
    return [a for a in out if a.tracks]


# -- the AI ---------------------------------------------------------------------

def _connection() -> Tuple[str, str, str]:
    base = (os.environ.get('LOCAL_AI_API_BASE') or '').strip().rstrip('/')
    key = (os.environ.get('AI_PLAYLIST_API_KEY') or os.environ.get('LOCAL_AI_API_KEY') or '').strip()
    model = (os.environ.get('AI_PLAYLIST_MODEL') or os.environ.get('LOCAL_AI_MODEL') or '').strip()
    return base, key, model


def ask(system: str, prompt: str, temperature: float = 0.4) -> Optional[dict]:
    """One chat completion whose reply is a JSON object, or None."""
    base, key, model = _connection()
    if not base or not model:
        logger.warning("AI playlists need LOCAL_AI_API_BASE and a model")
        return None
    try:
        resp = requests.post(
            f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {key}'} if key else {},
            json={'model': model, 'temperature': temperature, 'max_tokens': MAX_ANSWER_TOKENS,
                  'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.json()['choices'][0]['message']['content'] or ''
    except Exception as exc:
        logger.warning(f"AI playlist request failed: {exc}")
        return None
    match = re.search(r'\{.*\}', text, re.DOTALL)
    try:
        reply = json.loads(match.group(0)) if match else None
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        logger.info(f"AI playlist reply was not a JSON object: {text[:200]!r}")
        return None
    return reply


def _numbers(values, limit: int) -> List[int]:
    """The distinct whole numbers in a reply's list, in order."""
    out: List[int] = []
    for value in values if isinstance(values, list) else []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < number <= limit and number not in out:
            out.append(number)
    return out


SYSTEM = ("You are a DJ who builds playlists from one listener's own music library. You may only "
          "choose from the numbered lists you are given, by their numbers. Reply with one JSON object only.")


def choose_artists(description: str, artists: Sequence[Artist], size: int) -> List[Artist]:
    lines = []
    for i, a in enumerate(artists, 1):
        genres = f" — {', '.join(a.genres)}" if a.genres else ''
        countries = f" [{', '.join(a.countries)}]" if a.countries else ''
        lines.append(f"{i}. {a.name}{genres}{countries} ({len(a.tracks)} songs)")
    reply = ask(SYSTEM, (
        f'The listener asked for a playlist of about {size} songs: "{description}"\n\n'
        f'The artists in the library:\n' + '\n'.join(lines) + '\n\n'
        f'Choose the artists (at most {MAX_ARTISTS}) whose songs can make this playlist, best first, '
        'from anywhere in the list (it starts with the ones this listener plays most). '
        + RULES + 'If the listener named artists, choose those that are listed. '
        'Reply {"artists": [numbers from the list]}.'))
    return [artists[n - 1] for n in _numbers((reply or {}).get('artists'), len(artists))][:MAX_ARTISTS]


def _song_line(number: int, track, year: Optional[int] = None, genres: Sequence[str] = ()) -> str:
    album = getattr(track, 'parentTitle', None) or ''
    year = year or getattr(track, 'parentYear', None) or getattr(track, 'year', None)
    extra = ' · '.join(str(v) for v in (year, album) if v)
    return (f"{number}. {_artist_of(track)} — {track.title}" + (f" ({extra})" if extra else '')
            + (f" [{', '.join(genres[:2])}]" if genres else ''))


RULES = ("Respect everything the listener asked: a decade or year (use the years shown, which are the "
         "album's; a compilation's year can be later than the song's), a mood, a tempo, a language or "
         "region (the artists' countries are shown in brackets), an occasion. ")


def choose_songs(description: str, artists: Sequence[Artist], size: int) -> List[object]:
    tracks: List[object] = []
    years: Dict[object, int] = {}
    genres: Dict[object, List[str]] = {}
    for artist in artists:
        listed = artist.tracks[:SONGS_PER_ARTIST_LISTED]
        tracks.extend(listed)
        years.update(artist.years)
        genres.update({t.ratingKey: artist.genres for t in listed})
    if not tracks:
        return []
    listing = '\n'.join(_song_line(i, t, years.get(t.ratingKey), genres.get(t.ratingKey, ()))
                         for i, t in enumerate(tracks, 1))

    chosen: List[object] = []
    for _round in range(2):          # a second round fills a playlist the first left short
        wanted = size - len(chosen)
        taken = ''
        if chosen:
            taken = ('Already in the playlist, do not choose again: '
                     + ', '.join(str(tracks.index(t) + 1) for t in chosen) + '. ')
        reply = ask(SYSTEM, (
            f'The listener asked for a playlist of {size} songs: "{description}"\n\n'
            f'The songs of the artists that fit:\n{listing}\n\n'
            f'Choose {wanted} songs that fit what the listener asked, in the order they should play, '
            'mixing the artists. ' + RULES + taken +
            'Leave out songs that do not fit, even if that makes the playlist shorter. '
            'Reply {"songs": [numbers from the list]}.'))
        picked = [tracks[n - 1] for n in _numbers((reply or {}).get('songs'), len(tracks))]
        chosen = _capped(chosen + [t for t in picked if t not in chosen], size)
        if len(chosen) >= size or not picked:
            break
    return chosen[:size]


def confirm_theme(description: str, artists: Sequence[Artist]) -> List[object]:
    """The theme's songs the AI confirms are really about it (a yes or no for
    each, which a model answers far more steadily than it picks and orders):
    a title can share a word with the theme and be about something else."""
    tracks: List[object] = []
    years: Dict[object, int] = {}
    genres: Dict[object, List[str]] = {}
    for artist in artists:
        tracks.extend(artist.tracks)
        years.update(artist.years)
        genres.update({t.ratingKey: artist.genres for t in artist.tracks})
    if not tracks:
        return []
    listing = '\n'.join(_song_line(i, t, years.get(t.ratingKey), genres.get(t.ratingKey, ()))
                         for i, t in enumerate(tracks, 1))
    reply = ask(SYSTEM, (
        f'The listener asked for a playlist: "{description}"\n\n'
        f'These songs have a word of it in their title or album:\n{listing}\n\n'
        'Which of them really are songs for this playlist? A song that only shares a word with it is not '
        '(a rap song that mentions Santa is not a Christmas song, nor is a song about a town called Santa Cruz). '
        + RULES + 'Reply {"songs": [numbers of the songs that are]}.'), temperature=0.0)
    return [tracks[n - 1] for n in _numbers((reply or {}).get('songs'), len(tracks))]


def arranged(chosen: Sequence[object], size: int) -> List[object]:
    """The playlist in playing order: at most a third of a mixed playlist by
    one artist, and the artists taking turns, every one of them once a round
    (in the order the AI gave them), so a few big albums don't crowd out the
    artists with one fitting song; each artist's songs keep their order."""
    queues: Dict[str, List[object]] = {}
    for t in _capped(list(chosen), size):
        queues.setdefault(_artist_of(t).casefold(), []).append(t)
    order: List[object] = []
    while queues and len(order) < size:
        for name in list(queues):
            order.append(queues[name].pop(0))
            if not queues[name]:
                del queues[name]
            if len(order) >= size:
                break
    return order


def _capped(chosen: List[object], size: int) -> List[object]:
    """One artist may not take a mixed playlist: at most a third of it each,
    unless only one artist fits."""
    if len({_artist_of(t).casefold() for t in chosen}) <= 1:
        return chosen
    cap, per, kept = max(3, size // 3), Counter(), []
    for t in chosen:
        per[_artist_of(t).casefold()] += 1
        if per[_artist_of(t).casefold()] <= cap:
            kept.append(t)
    return kept


# -- the playlist ---------------------------------------------------------------

def playlist_title(description: str) -> str:
    words = ' '.join(description.split())
    if len(words) > TITLE_LENGTH:
        words = words[:TITLE_LENGTH].rsplit(' ', 1)[0] + '…'
    return f"{PREFIX}{words}"


def save(server, title: str, tracks: Sequence[object]):
    """The listener's playlist with these songs in order; an AI playlist of
    the same title is replaced."""
    for existing in server.playlists():
        if existing.title == title:
            existing.delete()
    return server.createPlaylist(title, items=list(tracks))


def make(server, section, description: str, size: Optional[int] = None) -> Dict[str, object]:
    """Build and save the playlist. Returns {'ok', 'title', 'songs': [(artist,
    title)], 'message'}; nothing is saved when the AI found too little."""
    size = max(MIN_SONGS, min(size or DEFAULT_SONGS, MAX_SONGS))
    description = ' '.join((description or '').split())
    title = playlist_title(description)
    if not description:
        return {'ok': False, 'title': title, 'songs': [], 'message': 'Describe the playlist you want.'}
    artists = catalogue(section)
    if not artists:
        return {'ok': False, 'title': title, 'songs': [], 'message': 'The library has no songs to choose from.'}
    countries = sorted({c for a in artists for c in a.countries})
    wish = read_wish(description, countries)
    pool = narrowed(artists, wish)
    logger.info(f"AI playlist '{title}': filters [{wish.describe() or 'none'}] leave "
                f"{len(pool)} of {len(artists)} artists")
    if not pool:
        return {'ok': False, 'title': title, 'songs': [], 'message': (
            f"The library has no songs that fit that ({wish.describe()}).")}
    theme = themed(pool, wish) if wish.words else []
    songs: List[object] = []
    if sum(len(a.tracks) for a in theme) >= MIN_SONGS:
        confirmed = confirm_theme(description, theme)
        logger.info(f"AI playlist '{title}': {sum(len(a.tracks) for a in theme)} song(s) titled on the theme, "
                    f"{len(confirmed)} confirmed")
        songs = arranged(confirmed, size)
    if len(songs) < MIN_SONGS:
        chosen_artists = choose_artists(description, pool, size)
        logger.info(f"AI playlist '{title}': artists {[a.name for a in chosen_artists][:15]}")
        songs = arranged(choose_songs(description, chosen_artists, size), size) if chosen_artists else []
    if len(songs) < MIN_SONGS:
        return {'ok': False, 'title': title, 'songs': [], 'message': (
            "The local AI couldn't find enough songs in the library for that. "
            "Try describing it another way, or name a genre, mood or artist.")}
    save(server, title, songs)
    logger.info(f"AI playlist '{title}': {len(songs)} song(s) saved")
    short = (f" The local AI found {len(songs)} that fit, not {size}." if len(songs) < size else '')
    return {'ok': True, 'title': title, 'songs': [(_artist_of(t), t.title) for t in songs],
            'message': f"Made **{title}** with {len(songs)} songs from the library.{short} It's in your "
                       "Plex and Plexamp playlists."}
