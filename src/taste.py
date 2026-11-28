"""Each user's taste, read the way Plex reads a listener, and the songs it picks.

Plexamp's mixes and radios know a listener from three things, and so does
MusicAI:

- **Listening history.** Every play Plex recorded for the user's accounts in
  the last TASTE_HISTORY_DAYS, each counting less the older it is (half after
  TASTE_HALF_LIFE_DAYS), with the user's own star ratings and skips. A song
  played often lately, or rated 4★ or more, is loved; one skipped more often
  than played, or rated 2★ or less, is not (`affinity`).
- **Plex's music metadata.** For every artist Plex lists similar artists,
  styles and moods. Artists that several favourites point to are the next ones
  to discover; the favourites' styles and moods describe the user (`build`).
- **Sonic analysis.** Plex places every analysed song in a sonic space
  (`/library/metadata/<id>/nearest`). Library songs that sound like the loved
  ones fill each user's "Picked for You" playlist with songs they have not
  heard yet (`picked_for_you`).

Every candidate a cycle finds on its own (similar songs, similar artists,
trending charts, popular playlists) is scored against the profile (`fit`):
songs by artists the user dislikes and songs that do not fit are dropped, and
the best are downloaded, a few per artist and no source taking every place
(`rank`). Each pick is written down with where it came from (`picks` table);
how the user took to earlier picks from each source (loved, played, never
played, skipped, rated low, deleted) weighs that source's next picks
(`source_weights`).
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger('musicai.taste')


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


HALF_LIFE_DAYS = max(1.0, _env_float('TASTE_HALF_LIFE_DAYS', 90))
HISTORY_DAYS = max(30, int(_env_float('TASTE_HISTORY_DAYS', 365)))
LOVED = 0.5                       # affinity from which a song counts as loved
MIN_LOVED = max(1, int(_env_float('TASTE_MIN_LOVED', 10)))
SEEDS = max(1, int(_env_float('TASTE_SEEDS', 25)))
SEEDS_PER_ARTIST = 2
FAVOURITES_READ = 40              # favourite artists whose Plex metadata is read
MIN_FIT = _env_float('TASTE_MIN_FIT', 0.35)
PICKS_PER_ARTIST = max(1, int(_env_float('TASTE_PICKS_PER_ARTIST', 2)))
SONIC_DISTANCE = _env_float('SONIC_MAX_DISTANCE', 0.3)
CHART_EVIDENCE = 0.25             # a chart or playlist hit: popular, but nothing ties it to this user
UNHEARD_ARTIST = 0.6              # an artist whose songs here the user hasn't played: those come first
PICK_GRACE_DAYS = 14              # a pick is judged once the user has had this long to hear it
FOR_YOU_SIZE = max(5, int(_env_float('FOR_YOU_SIZE', 50)))
FOR_YOU_PER_ARTIST = 3
DISLIKED_RATING, LIKED_RATING = 2, 6           # Plex ratings: 1★ and 3★
UNPLAYED_ARTIST_SONGS, UNPLAYED_ARTIST_DAYS = 5, 30

# Where a pick came from; each source's picks are judged on their own.
SIMILAR_SONG = 'similar_song'           # Last.fm / Deezer: songs like a loved song
SIMILAR_ARTIST = 'similar_artist'       # Plex's similar artists of the favourites
TRENDING = 'trending'
POPULAR_PLAYLIST = 'popular_playlist'
ALBUM_COMPLETION = 'album_completion'
SOURCES = (SIMILAR_SONG, SIMILAR_ARTIST, TRENDING, POPULAR_PLAYLIST, ALBUM_COMPLETION)


def for_you_enabled() -> bool:
    return os.environ.get('ENABLE_FOR_YOU_PLAYLIST', 'true').strip().lower() not in ('0', 'false', 'no', 'off')


def for_you_playlist_name() -> str:
    return os.environ.get('FOR_YOU_PLAYLIST_NAME', '').strip() or 'Picked for You'


# -- names ---------------------------------------------------------------------

def artist_key(name: Optional[str]) -> str:
    """An artist as songs are compared: the first credited artist, compacted."""
    from song_dedup import primary_artist
    return primary_artist(name or '')


def song_key(artist: Optional[str], title: Optional[str]) -> Tuple[str, str, Tuple[str, ...]]:
    """A song as songs are compared: its first artist and core title, with
    versions ("Live", "Remix") kept apart."""
    from song_dedup import song_key as _key
    return _key(artist or '', title or '')


# -- songs and how much the user likes each -------------------------------------

@dataclass
class Song:
    key: str                      # Plex ratingKey
    artist: str                   # performing artist
    title: str
    album_artist: str = ''
    artist_ref: str = ''          # Plex ratingKey of the album artist
    plays: int = 0
    skips: int = 0
    rating: float = 0.0           # Plex's 0–10, two per star
    last_played: float = 0.0
    added: float = 0.0
    album_rating: float = 0.0     # the user's rating of the album, 0–10


def _epoch(value) -> float:
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):
            return 0.0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def song_from_plex(track, features: Optional[dict] = None) -> Optional[Song]:
    """A Plex track as a Song; `features` (the cycle's extract_track_features)
    is used for the counts when given, as it already read them."""
    key = getattr(track, 'ratingKey', None)
    title = getattr(track, 'title', None)
    if key is None or not title:
        return None
    f = features or {}
    album_artist = getattr(track, 'grandparentTitle', None) or ''
    artist = getattr(track, 'originalTitle', None) or album_artist
    return Song(
        key=str(key), artist=str(artist), title=str(title), album_artist=str(album_artist),
        artist_ref=str(getattr(track, 'grandparentRatingKey', '') or ''),
        plays=int(f.get('view_count', getattr(track, 'viewCount', 0)) or 0),
        skips=int(f.get('skip_count', getattr(track, 'skipCount', 0)) or 0),
        rating=float(f.get('rating', getattr(track, 'userRating', 0)) or 0),
        last_played=_epoch(f.get('last_played', getattr(track, 'lastViewedAt', None))),
        added=_epoch(f.get('date_added', getattr(track, 'addedAt', None))),
        album_rating=float(f.get('album_rating') or 0),
    )


def decay(age_days: float) -> float:
    """What a play this many days ago still counts for."""
    return 0.5 ** (max(0.0, age_days) / HALF_LIFE_DAYS)


def affinity(recent_plays: float, plays: int = 0, skips: int = 0, rating: float = 0.0) -> float:
    """How much the user likes one song, from -1 to 1.

    recent_plays: their plays, each weighted by its age (decay). A rating
    speaks louder than plays: 1★ is -1, 2★ at most -0.6, 4★ at least 0.55.
    Skips count against an unrated or 3★ song as a share of its plays."""
    stars = (rating or 0) / 2
    if 0 < stars <= 1.5:
        return -1.0
    listened = 1 - math.exp(-max(0.0, recent_plays) / 2.5)
    if 0 < stars <= 2:
        return min(-0.6, listened - 1)
    if stars >= 4:
        return min(1.0, 0.55 + 0.2 * (stars - 4) + 0.25 * listened)
    score = listened + (0.1 if stars >= 3 else -0.2 if stars > 0 else 0.0)
    score -= 0.8 * skips / (skips + plays + 1)
    return max(-1.0, min(1.0, score))


REPEAT_HOURS = 24                 # a song played again within this is "on repeat"
KEPT_LIKING = LOVED               # a song the user keeps in a playlist of their own
REQUESTED_LIKING = 0.3            # a song the user asked for, until their listening says otherwise
RATED_FAIR_LIKING = 0.3           # a song rated 3★


@dataclass
class Signals:
    """Everything known about how one user takes one song, beyond Plex's own
    counts and rating: their tracked listens (listening.py), how often they
    played it again within REPEAT_HOURS, the user's rating of its artist,
    whether they keep it in a playlist of their own, and whether they asked
    for it."""
    heard: object = None          # listening.Heard, or None
    repeats: int = 0
    artist_rating: float = 0.0    # Plex's 0–10
    kept: bool = False
    requested: bool = False


def _rating_prior(rating: float, weight: float) -> float:
    """What an album's or artist's rating says about one of its songs."""
    stars = (rating or 0) / 2
    if stars <= 0:
        return 0.0
    if stars <= 1.5:
        return -0.5 * weight
    if stars <= 2.5:
        return -0.25 * weight
    if stars >= 4.5:
        return 0.25 * weight
    if stars >= 3.5:
        return 0.15 * weight
    return 0.0


def liking(recent_plays: float, song: Song, sig: Optional[Signals] = None) -> float:
    """How much the user likes one song, from -1 to 1, from everything known.

    - The song's own rating speaks loudest (affinity()): 1★ is -1, 4★ is at
      least 0.55, whatever else is known.
    - Plays count, the recent ones most; a play heard only in part counts
      half. Skips count against: Plex's skip count, or the tracked plays
      left in their first seconds when there are more of those.
    - A song the user usually leaves before the end is liked less than one
      they hear through (from the tracked listens' share heard).
    - Played again within REPEAT_HOURS: on repeat, a sign of love.
    - Without a song rating, the album's rating and, half as much, the
      artist's lean it up or down.
    - Kept in a playlist of their own: at least KEPT_LIKING. Asked for: at
      least REQUESTED_LIKING while they haven't skipped it more than played it.
    """
    sig = sig or Signals()
    heard = sig.heard
    skips = max(song.skips, getattr(heard, 'skipped', 0) or 0)
    recent = recent_plays + 0.5 * (getattr(heard, 'partial', 0) or 0)
    value = affinity(recent, song.plays, skips, song.rating)
    if song.rating:
        # 3★ is a song they find good enough: liked a little, whatever the plays.
        return max(value, RATED_FAIR_LIKING) if 3 <= song.rating / 2 < 4 else value
    tracked = getattr(heard, 'plays', 0) or 0
    if value > 0 and tracked:
        value *= 0.6 + 0.4 * (1 - (1 - heard.share) * min(1.0, tracked / 3))
    value += min(0.2, 0.1 * sig.repeats)
    value += _rating_prior(song.album_rating, 1.0) + _rating_prior(sig.artist_rating, 0.5)
    if sig.kept:
        value = max(value, KEPT_LIKING)
    listened = max(song.plays, (getattr(heard, 'finished', 0) or 0) + (getattr(heard, 'partial', 0) or 0))
    if sig.requested and skips <= listened:
        value = max(value, REQUESTED_LIKING)
    return max(-1.0, min(1.0, value))


def repeats(events: Iterable[Tuple[str, float, str, str]]) -> Dict[str, int]:
    """{Plex key: plays that came within REPEAT_HOURS of the one before}."""
    times: Dict[str, List[float]] = defaultdict(list)
    for key, when, _artist, _title in events:
        times[key].append(when)
    out = {}
    for key, ts in times.items():
        ts.sort()
        n = sum(1 for a, b in zip(ts, ts[1:]) if b - a <= REPEAT_HOURS * 3600)
        if n:
            out[key] = n
    return out


def _dislikes(songs: Iterable[Song], now: float, played: Set[str] = frozenset()) -> Tuple[Set[str], Set[str]]:
    """(rated low, passed over): artists (artist_key) with a song rated 1★ and
    none 3★ or more; artists with UNPLAYED_ARTIST_SONGS songs here for
    UNPLAYED_ARTIST_DAYS days, none ever played (nor in `played`, Plex keys
    played in the history) or rated 3★ or more."""
    old = now - UNPLAYED_ARTIST_DAYS * 86400
    stats: Dict[str, Counter] = defaultdict(Counter)
    for s in songs:
        if not s.artist or s.artist.startswith('<'):
            continue                    # a Plex object's text ("<Artist:…>"), not a name
        c = stats[artist_key(s.artist)]
        was_played = (s.plays or 0) > 0 or s.key in played
        c['low'] += 0 < (s.rating or 0) <= DISLIKED_RATING
        c['high'] += (s.rating or 0) >= LIKED_RATING
        c['played'] += was_played
        c['old_unplayed'] += not was_played and bool(s.added) and s.added <= old
    keys = [key for key, c in stats.items() if key and not stats[key]['high']]
    return ({k for k in keys if stats[k]['low']},
            {k for k in keys if not stats[k]['played'] and stats[k]['old_unplayed'] >= UNPLAYED_ARTIST_SONGS})


def disliked_artists(songs: Iterable[Song], now: Optional[float] = None) -> Set[str]:
    """Artists (artist_key) this user clearly doesn't like: a song of theirs
    rated 1★ and none 3★ or more, or UNPLAYED_ARTIST_SONGS songs here for
    UNPLAYED_ARTIST_DAYS days and none ever played. No more of their songs
    are downloaded for the user."""
    rated_low, passed_over = _dislikes(songs, time.time() if now is None else now)
    return rated_low | passed_over


# -- the profile ----------------------------------------------------------------

@dataclass
class Profile:
    user: str
    songs: Dict[str, float] = field(default_factory=dict)        # Plex key → liking now (recent plays count most)
    lasting: Dict[str, float] = field(default_factory=dict)      # Plex key → liking with every play in full
    by_key: Dict[str, Song] = field(default_factory=dict)
    index: Dict[tuple, List[str]] = field(default_factory=dict)  # song_key → Plex keys
    artists: Dict[str, float] = field(default_factory=dict)      # artist_key → -1 … 1
    lately: Dict[str, float] = field(default_factory=dict)       # artist_key → 0.4 … 1, played lately
    names: Dict[str, str] = field(default_factory=dict)          # artist_key → name as written
    heard_artists: Set[str] = field(default_factory=set)         # a song of theirs played, rated or skipped
    library_artists: Set[str] = field(default_factory=set)
    styles: Dict[str, float] = field(default_factory=dict)       # the favourites' styles, 0 … 1
    moods: Dict[str, float] = field(default_factory=dict)
    similar: Dict[str, float] = field(default_factory=dict)      # Plex's similar artists, 0 … 1
    similar_via: Dict[str, List[str]] = field(default_factory=dict)
    seeds: List[Song] = field(default_factory=list)
    heard: Set[str] = field(default_factory=set)                 # Plex keys played, rated or skipped
    disliked: Set[str] = field(default_factory=set)              # no more downloads of theirs
    passed_over: Set[str] = field(default_factory=set)           # disliked only for songs never played
    weights: Dict[str, float] = field(default_factory=dict)      # source → weight of its picks

    @property
    def loved(self) -> List[str]:
        return [k for k, a in self.songs.items() if a >= LOVED]

    def ready(self) -> bool:
        """Enough loved songs to pick from."""
        return len(self.loved) >= MIN_LOVED

    def favourites(self, limit: int) -> List[str]:
        """The artists the user likes most, the ones played lately first."""
        liked = [a for a, v in self.artists.items() if v >= 0.3]
        return sorted(liked, key=lambda a: -self.artists[a] * self.lately.get(a, 0.4))[:limit]

    def owns(self, artist: str, title: str) -> bool:
        return song_key(artist, title) in self.index

    def summary(self) -> dict:
        top = lambda d, n: [k for k, _ in sorted(d.items(), key=lambda x: -x[1])[:n]]
        return {
            'user': self.user,
            'loved_songs': len(self.loved),
            'favourite_artists': [self.names.get(a, a) for a in self.favourites(15)],
            'styles': top(self.styles, 10),
            'moods': top(self.moods, 10),
            'similar_artists': [self.names.get(a, a) for a in top(self.similar, 15)],
            'seeds': [f"{s.artist} - {s.title}" for s in self.seeds],
            'source_weights': {k: round(v, 2) for k, v in sorted(self.weights.items())},
        }


ArtistInfo = Callable[[str], Optional[dict]]          # Plex artist key → similar/styles/moods


def _normalised(weights: Dict[str, float]) -> Dict[str, float]:
    top = max(weights.values(), default=0.0)
    return {k: round(v / top, 4) for k, v in weights.items()} if top > 0 else {}


def build(user: str, songs: Sequence[Song], events: Sequence[Tuple[str, float, str, str]] = (),
          now: Optional[float] = None, artist_info: Optional[ArtistInfo] = None,
          listens: Optional[Dict[str, object]] = None, kept: Iterable[str] = (),
          requested: Iterable[Tuple[str, str]] = (), artist_ratings: Optional[Dict[str, float]] = None) -> Profile:
    """The user's profile from their songs (with their own plays, skips and
    ratings), their play history: (Plex key, when, artist, title) per play,
    their tracked listens ({Plex key: listening.Heard}), the Plex keys of the
    songs in their own playlists, the (artist, title) of the songs they asked
    for, and their artist ratings ({artist Plex key: rating}). A play of a
    song Plex has since re-added under a new key is matched to the song by
    artist and title."""
    now = time.time() if now is None else now
    p = Profile(user=user)
    for s in songs:
        p.by_key[s.key] = s
        for artist in {s.artist, s.album_artist} - {''}:
            p.index.setdefault(song_key(artist, s.title), []).append(s.key)
    match = lambda key, artist, title: [key] if key in p.by_key else p.index.get(song_key(artist, title), [])[:1]

    recent: Dict[str, float] = defaultdict(float)
    play_counts: Counter = Counter()
    matched_events = []
    for key, when, artist, title in events:
        for k in match(key, artist, title):
            recent[k] += decay((now - when) / 86400)
            play_counts[k] += 1
            matched_events.append((k, when, artist, title))
    on_repeat = repeats(matched_events)
    heard_songs: Dict[str, object] = {}
    for key, h in (listens or {}).items():
        for k in match(key, getattr(h, 'artist', ''), getattr(h, 'title', '')):
            heard_songs[k] = h
    kept_keys = {k for k in kept if k in p.by_key}
    requested_keys = {k for artist, title in requested for k in p.index.get(song_key(artist, title), [])}
    artist_ratings = artist_ratings or {}

    names: Dict[str, Counter] = defaultdict(Counter)
    seed_order: Dict[str, float] = {}
    positive: Dict[str, float] = defaultdict(float)
    negative: Dict[str, float] = defaultdict(float)
    lately: Dict[str, float] = defaultdict(float)
    for s in songs:
        if s.key in recent:
            r = recent[s.key]
        else:                          # plays from before the history Plex keeps
            r = s.plays * decay((now - s.last_played) / 86400) if s.plays and s.last_played else 0.0
        sig = Signals(heard=heard_songs.get(s.key), repeats=on_repeat.get(s.key, 0),
                      artist_rating=artist_ratings.get(s.artist_ref, 0.0),
                      kept=s.key in kept_keys, requested=s.key in requested_keys)
        a = liking(r, s, sig)
        p.songs[s.key] = a
        # Whether the song deserves its place: every play counts in full,
        # however long ago (a song finished months ago is no dislike).
        p.lasting[s.key] = liking(max(float(s.plays), float(play_counts.get(s.key, 0))), s, sig)
        seed_order[s.key] = a * (0.5 + 0.5 * min(1.0, r / 2))     # loved lately first
        heard = bool(s.plays or s.rating or s.skips or s.key in recent or s.key in heard_songs
                     or s.key in kept_keys)
        if heard:
            p.heard.add(s.key)
        ak = artist_key(s.artist)
        if not ak:
            continue
        names[ak][s.artist] += 1
        if heard:
            p.heard_artists.add(ak)
        lately[ak] += r
        positive[ak] += max(a, 0.0)
        negative[ak] += max(-a, 0.0)
    p.names = {k: c.most_common(1)[0][0] for k, c in names.items()}
    p.library_artists = set(names)
    rated_low, p.passed_over = _dislikes(songs, now, set(recent))
    p.disliked = rated_low | p.passed_over
    for ak in names:
        # A few songs the user doesn't like take something off an artist they
        # love, not everything: many loved songs and a few rated low is still
        # a favourite.
        net = positive[ak] - 0.5 * negative[ak]
        value = math.copysign(1 - math.exp(-abs(net) / 1.5), net)
        p.artists[ak] = round(value, 4)
        p.lately[ak] = round(0.4 + 0.6 * (1 - math.exp(-lately[ak] / 5)), 4)

    # Seeds: the loved songs, the ones loved lately first, a few per artist.
    per_artist: Counter = Counter()
    for s in sorted((p.by_key[k] for k in p.loved), key=lambda s: -seed_order[s.key]):
        ak = artist_key(s.artist)
        if per_artist[ak] < SEEDS_PER_ARTIST:
            per_artist[ak] += 1
            p.seeds.append(s)
            if len(p.seeds) >= SEEDS:
                break

    refs = _artist_refs(songs)
    favourites = p.favourites(FAVOURITES_READ)
    if artist_info:
        similar: Dict[str, float] = defaultdict(float)
        styles: Dict[str, float] = defaultdict(float)
        moods: Dict[str, float] = defaultdict(float)
        for ak in favourites:
            info = artist_info(refs[ak]) if ak in refs else None
            if not info:
                continue
            weight = p.artists[ak] * p.lately.get(ak, 0.4)
            for tag in info.get('styles') or []:
                styles[tag] += weight / len(info['styles'])
            for tag in info.get('moods') or []:
                moods[tag] += weight / len(info['moods'])
            listed = info.get('similar') or []
            for i, name in enumerate(listed):
                other = artist_key(name)
                if other and other != ak:
                    similar[other] += weight * (1 - 0.5 * i / len(listed))
                    p.names.setdefault(other, name)
                    p.similar_via.setdefault(other, []).append(p.names.get(ak, ak))
        p.styles, p.moods = _normalised(styles), _normalised(moods)
        p.similar = _normalised({k: v for k, v in similar.items() if _new_to(p, k)})
    return p


def _new_to(p: Profile, ak: str) -> bool:
    """An artist worth discovering: not a favourite already, not disliked, and
    not one the user has listened to without coming to like."""
    value = p.artists.get(ak)
    if ak in p.disliked or (value is not None and value < 0):
        return False
    if value is not None and value >= 0.3:
        return False
    return not (ak in p.heard_artists and (value or 0) < 0.15)


def _artist_refs(songs: Iterable[Song]) -> Dict[str, str]:
    """artist_key → the Plex key of that artist (their own albums' album artist)."""
    refs: Dict[str, str] = {}
    for s in songs:
        if s.artist_ref and s.album_artist:
            refs.setdefault(artist_key(s.album_artist), s.artist_ref)
    return refs


# -- what Plex is asked ------------------------------------------------------------

def account_ids(admin_server, username: str, is_admin: bool) -> Set[int]:
    """The Plex account ids whose plays are this user's: the server owner's
    (1) for the admin, and every account of that name."""
    ids = {1} if is_admin else set()
    name = (username or '').strip().casefold()
    if not name:
        return ids
    try:
        ids |= {int(a.id) for a in admin_server.systemAccounts() if (a.name or '').strip().casefold() == name}
    except Exception as exc:
        logger.debug(f"Could not list the server's accounts: {exc}")
    try:
        for u in admin_server.myPlexAccount().users():
            if name in {(u.username or '').strip().casefold(), (u.title or '').strip().casefold()}:
                ids.add(int(u.id))
    except Exception as exc:
        logger.debug(f"Could not list the Plex users: {exc}")
    return ids


def play_events(admin_server, section, ids: Set[int], now: Optional[float] = None,
                days: int = HISTORY_DAYS) -> List[Tuple[str, float, str, str]]:
    """Every play of a song in `section` by these accounts in the last `days`
    days: (Plex key, when, album artist, title). Only the server owner can read
    the history, hence the admin connection."""
    now = time.time() if now is None else now
    if not ids:
        return []
    events = []
    for h in admin_server.history(maxresults=200000, mindate=datetime.fromtimestamp(now - days * 86400),
                                  librarySectionID=section.key):
        if int(getattr(h, 'accountID', 0) or 0) in ids and getattr(h, 'type', 'track') == 'track':
            events.append((str(h.ratingKey), _epoch(h.viewedAt), h.grandparentTitle or '', h.title or ''))
    return events


def plex_artist_info(server) -> ArtistInfo:
    """Plex's similar artists, styles and moods of an artist, by Plex key (cached)."""
    cache: Dict[str, Optional[dict]] = {}

    def info(ref: str) -> Optional[dict]:
        if ref not in cache:
            try:
                item = server.fetchItem(int(ref))
                cache[ref] = {name: [t.tag for t in getattr(item, name, None) or []]
                              for name in ('similar', 'styles', 'moods')}
            except Exception as exc:
                logger.debug(f"Plex artist {ref} not read: {exc}")
                cache[ref] = None
        return cache[ref]
    return info


def nearest(server, ref: str, limit: int, max_distance: float) -> List[Tuple[str, str, str, float]]:
    """Plex's sonic neighbours of a song or artist, closest first:
    (type, title, Plex key, distance)."""
    try:
        data = server.query(f'/library/metadata/{ref}/nearest?limit={int(limit)}&maxDistance={max_distance}')
    except Exception as exc:
        logger.debug(f"No sonic neighbours for {ref}: {exc}")
        return []
    found = []
    for el in data:
        key = el.attrib.get('ratingKey')
        if key:
            try:
                distance = float(el.attrib.get('distance') or max_distance)
            except ValueError:
                distance = max_distance
            found.append((el.attrib.get('type', ''), el.attrib.get('title', ''), key, distance))
    return sorted(found, key=lambda x: x[3])


def automatic_playlists() -> Set[str]:
    """Playlists MusicAI makes and fills itself: their songs say nothing of
    what the user chose."""
    import playlist_keeper
    return playlist_keeper.kept_by_musicai() | {
        os.environ.get('TRENDING_PLAYLIST_NAME', 'Trending Now for You'),
        os.environ.get('SIMILAR_PLAYLIST_NAME', 'You Might Like'), for_you_playlist_name()}


def own_playlist_songs(server) -> Set[str]:
    """Plex keys of the songs in the playlists the user made themselves (not
    MusicAI's own, not AI playlists, not smart playlists)."""
    automatic = automatic_playlists()
    prefix = os.environ.get('AI_PLAYLIST_PREFIX', 'AI: ')
    keys: Set[str] = set()
    try:
        for pl in server.playlists():
            if getattr(pl, 'playlistType', 'audio') != 'audio' or getattr(pl, 'smart', False):
                continue
            if pl.title in automatic or (prefix and pl.title.startswith(prefix)):
                continue
            keys |= {str(item.ratingKey) for item in pl.items()}
    except Exception as exc:
        logger.debug(f"The user's playlists were not read: {exc}")
    return keys


def artist_ratings(section) -> Dict[str, float]:
    """{artist Plex key: the user's rating} for the artists they rated."""
    try:
        return {str(a.ratingKey): float(a.userRating) for a in section.searchArtists()
                if getattr(a, 'userRating', None)}
    except Exception as exc:
        logger.debug(f"Artist ratings were not read: {exc}")
        return {}


def requested_songs(user: str, data_dir: Optional[str] = None) -> List[Tuple[str, str]]:
    """(artist, title) of every song the user asked for over Discord."""
    import request_history
    path = os.path.join(data_dir or os.environ.get('DATA_DIR', '/app/data'), 'discord_requests.json')
    try:
        history = request_history.load(path)
    except Exception as exc:
        logger.debug(f"Request history not read: {exc}")
        return []
    name = (user or '').casefold()
    return [(e.get('artist') or '', e.get('title') or '') for u, entries in history.items() if u.casefold() == name
            for e in entries or [] if isinstance(e, dict) and e.get('title')]


def from_plex(user: str, songs: Sequence[Song], admin_server, section, is_admin: bool,
              now: Optional[float] = None, artists: bool = True, user_server=None,
              user_section=None) -> Profile:
    """build() with everything read: the user's plays from Plex's history
    (the admin connection), their tracked listens (listening.py), and through
    their own connection (`user_server` / `user_section`) the songs of their
    own playlists and their artist ratings; with `artists`, the favourite
    artists' metadata."""
    import listening
    ids = account_ids(admin_server, user, is_admin)
    events = play_events(admin_server, section, ids, now=now)
    listens = listening.heard_by(ids, names=[user])
    kept = own_playlist_songs(user_server) if user_server is not None else set()
    rated_artists = artist_ratings(user_section) if user_section is not None else {}
    asked = requested_songs(user)
    logger.info(f"🎧 {user}: {len(events)} play(s) in Plex's history over {HISTORY_DAYS} days "
                f"(account(s) {sorted(ids)}), {sum(h.plays for h in listens.values())} tracked listen(s) of "
                f"{len(listens)} song(s), {len(kept)} song(s) in their own playlists, {len(rated_artists)} rated "
                f"artist(s), {len(asked)} request(s)")
    return build(user, songs, events, now=now, artist_info=plex_artist_info(admin_server) if artists else None,
                 listens=listens, kept=kept, requested=asked, artist_ratings=rated_artists)


# -- scoring candidates ---------------------------------------------------------------

def fit(p: Profile, cand: dict) -> float:
    """How well a candidate song fits the user, 0 … 1 (before its source's weight).

    The candidate's `evidence` (0 … 1) is how strongly its source ties it to the
    user: a loved song it is similar to, a similar artist's top song. The
    artist part is how the user feels about its artist, or how close Plex puts
    that artist to their favourites. Disliked artists and songs already in the
    library score 0; an artist with songs here the user has never played
    scores less, as those are theirs to hear first ("Picked for You")."""
    ak = artist_key(cand.get('artist'))
    if not ak or not cand.get('title') or ak in p.disliked or p.artists.get(ak, 0.0) < -0.25:
        return 0.0
    if p.owns(cand.get('artist'), cand.get('title')):
        return 0.0
    evidence = max(0.0, min(1.0, float(cand.get('evidence') or 0.0)))
    artist = max(p.artists.get(ak, 0.0), p.similar.get(ak, 0.0))
    score = 0.55 * evidence + 0.45 * artist
    if ak in p.library_artists and ak not in p.heard_artists:
        score *= UNHEARD_ARTIST
    return round(score, 4)


def rank(p: Profile, candidates: Iterable[dict], limit: int, taken: Optional[Counter] = None,
         min_fit: float = MIN_FIT, per_source: Optional[int] = None) -> List[dict]:
    """The candidates worth downloading for this user, best first, at most
    PICKS_PER_ARTIST per artist, counting `taken` (songs per artist_key already
    downloaded this cycle). With `per_source`, a source's picks beyond that many
    come after every other source's, as spares, so each source gets picks to be
    judged by. Each comes back with `confidence` set to its score (fit times its
    source's weight)."""
    best: Dict[tuple, Tuple[float, dict]] = {}
    for cand in candidates:
        score = fit(p, cand) * p.weights.get(cand.get('source'), 1.0)
        if score < min_fit:
            continue
        key = song_key(cand.get('artist'), cand.get('title'))
        if key not in best or score > best[key][0]:
            best[key] = (round(score, 4), cand)
    per_artist, by_source = Counter(taken or {}), Counter()
    picked, spares = [], []
    for score, cand in sorted(best.values(), key=lambda x: -x[0]):
        ak = artist_key(cand.get('artist'))
        if per_artist[ak] >= PICKS_PER_ARTIST:
            continue
        per_artist[ak] += 1
        if per_source is not None and by_source[cand.get('source')] >= per_source:
            spares.append(dict(cand, confidence=score))
            continue
        by_source[cand.get('source')] += 1
        picked.append(dict(cand, confidence=score))
        if len(picked) >= limit:
            break
    return (picked + spares)[:limit]


def similar_song_candidates(p: Profile, lookup: Callable[[str, str], List[dict]]) -> List[dict]:
    """Songs like the seeds: `lookup(artist, title)` returns similar songs best
    first (Last.fm's, with its match as `confidence`, or Deezer's). A song more
    seeds lead to, from more loved seeds, has stronger evidence."""
    votes: Dict[tuple, dict] = {}
    for seed in p.seeds:
        weight = max(0.0, p.songs.get(seed.key, 0.0))
        found = lookup(seed.artist, seed.title) or []
        for i, rec in enumerate(found):
            if not rec.get('artist') or not rec.get('title'):
                continue
            if rec.get('source') == 'lastfm_similar':
                match = max(0.0, min(1.0, float(rec.get('confidence') or 0.0)))
            else:
                match = 1 - 0.5 * i / max(len(found), 1)
            key = song_key(rec['artist'], rec['title'])
            entry = votes.setdefault(key, dict(rec, source=SIMILAR_SONG, strength=0.0, seeds=[]))
            entry['strength'] += weight * match
            entry['seeds'].append(f"{seed.artist} - {seed.title}")
    out = []
    for entry in votes.values():
        entry['evidence'] = round(1 - math.exp(-entry.pop('strength') / 0.5), 4)
        seeds = entry.pop('seeds')
        entry['reason'] = f"Like {seeds[0]}" + (f" and {len(seeds) - 1} more you love" if len(seeds) > 1 else '')
        entry['type'] = 'song'
        out.append(entry)
    return out


def artist_candidates(p: Profile, top_tracks: Callable[[str, int], List[dict]], artists: int = 12,
                      per_artist: int = 4, per_favourite: int = 2) -> List[dict]:
    """The most popular songs of the artists Plex lists as similar to the
    favourites, strongest first. At most `per_favourite` of them come by way of
    one favourite, so a cluster of favourites that all point to the same scene
    doesn't take every place. `top_tracks(name, n)` returns an artist's most
    popular songs ({'artist', 'title', 'album'}), best first."""
    out, by_favourite, chosen = [], Counter(), 0
    for ak, weight in sorted(p.similar.items(), key=lambda x: -x[1]):
        if chosen >= artists:
            break
        friends = list(dict.fromkeys(p.similar_via.get(ak, [])))
        if friends and by_favourite[friends[0]] >= per_favourite:
            continue
        name = p.names.get(ak, ak)
        songs = [t for t in top_tracks(name, per_artist) or [] if artist_key(t.get('artist')) == ak]
        if not songs:
            continue                    # unknown there, or only another act of a similar name
        chosen += 1
        if friends:
            by_favourite[friends[0]] += 1
        for i, track in enumerate(songs):
            out.append({'artist': track['artist'], 'title': track['title'],
                        'album': track.get('album') or 'Unknown', 'type': 'song', 'source': SIMILAR_ARTIST,
                        'evidence': round(weight * (1 - 0.1 * i), 4),
                        'reason': f"Plex lists them with {', '.join(friends[:2]) or 'your favourites'}"})
    return out


def chart_candidates(tracks: Iterable[dict], source: str) -> List[dict]:
    """Chart and playlist songs as candidates: popular, nothing more."""
    return [dict(t, source=source, evidence=CHART_EVIDENCE) for t in tracks]


def deezer_top_tracks(name: str, limit: int = 4) -> List[dict]:
    """An artist's most popular songs on Deezer (the artist whose name matches)."""
    import requests
    try:
        found = requests.get('https://api.deezer.com/search/artist', params={'q': name, 'limit': 5},
                             timeout=15).json().get('data') or []
        artist = next((a for a in found if artist_key(a.get('name')) == artist_key(name)), None)
        if not artist:
            return []
        top = requests.get(f"https://api.deezer.com/artist/{artist['id']}/top", params={'limit': limit},
                           timeout=15).json().get('data') or []
    except Exception as exc:
        logger.debug(f"Deezer top songs of {name} not read: {exc}")
        return []
    return [{'artist': (t.get('artist') or {}).get('name') or artist['name'], 'title': t.get('title'),
             'album': (t.get('album') or {}).get('title')} for t in top if t.get('title')]


# -- picks and how they were taken -----------------------------------------------------

_PICKS_TABLE = ('CREATE TABLE IF NOT EXISTS picks (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT, '
                'title TEXT, source TEXT, fit REAL, reason TEXT, picked_at REAL)')


def record_pick(db_path: str, artist: str, title: str, source: str, fit_score: float, reason: str,
                when: Optional[float] = None) -> None:
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute(_PICKS_TABLE)
            conn.execute('INSERT INTO picks (artist, title, source, fit, reason, picked_at) VALUES (?, ?, ?, ?, ?, ?)',
                         (artist, title, source, fit_score, reason, time.time() if when is None else when))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(f"Could not write down the pick {artist} - {title}: {exc}")


def outcome(p: Profile, artist: str, title: str, deleted: Set[tuple]) -> Optional[float]:
    """How the user took to a pick, 0 … 1: loved 1, played 0.6, never played
    0.15, skipped away or rated low 0; deleted 0. None when the song is gone for
    another reason and can't be judged."""
    key = song_key(artist, title)
    keys = [k for k in p.index.get(key, []) if k in p.by_key]
    if not keys:
        return 0.0 if key in deleted else None
    best = max(p.songs.get(k, 0.0) for k in keys)
    if best >= LOVED:
        return 1.0
    if best < 0:
        return 0.0
    if any(p.by_key[k].plays or k in p.heard for k in keys):
        return 0.6
    return 0.15


def source_weights(db_path: str, p: Profile, now: Optional[float] = None) -> Dict[str, float]:
    """Each source's weight, 0.5 … 1.5, from how the user took to its picks
    older than PICK_GRACE_DAYS; 1.0 for a source with nothing to judge yet."""
    now = time.time() if now is None else now
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute(_PICKS_TABLE)
            conn.commit()
            rows = conn.execute('SELECT artist, title, source FROM picks WHERE picked_at <= ?',
                                (now - PICK_GRACE_DAYS * 86400,)).fetchall()
            try:
                gone = conn.execute('SELECT artist, title FROM deletions').fetchall()
            except sqlite3.Error:
                gone = []
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(f"Could not read the earlier picks: {exc}")
        return {}
    deleted = {song_key(a, t) for a, t in gone}
    judged: Dict[str, List[float]] = defaultdict(list)
    for artist, title, source in rows:
        result = outcome(p, artist, title, deleted)
        if result is not None:
            judged[source].append(result)
    weights = {}
    for source in SOURCES:
        results = judged.get(source, [])
        weights[source] = round(0.5 + (sum(results) + 1) / (len(results) + 2), 4)
    return weights


# -- Picked for You -----------------------------------------------------------------------

def picked_for_you(p: Profile, neighbours: Callable[[str], List[Tuple[str, float]]],
                   size: int = FOR_YOU_SIZE) -> List[str]:
    """Plex keys of library songs this user has never played, rated or skipped
    that sound most like their loved songs: each seed's sonic neighbours
    (`neighbours(key)` → [(Plex key, distance)]) count by how loved the seed is
    and how close they are. Best first, at most FOR_YOU_PER_ARTIST per artist,
    none by an artist they rated low or skip. Artists disliked only for songs
    never played are in: this is where those songs get heard."""
    score: Dict[str, float] = defaultdict(float)
    for seed in p.seeds:
        weight = max(0.0, p.songs.get(seed.key, 0.0))
        for key, distance in neighbours(seed.key):
            song = p.by_key.get(key)
            if song is None or key in p.heard:
                continue
            ak = artist_key(song.artist)
            if (ak in p.disliked and ak not in p.passed_over) or p.artists.get(ak, 0.0) < 0:
                continue
            score[key] += weight * max(0.0, 1 - distance / SONIC_DISTANCE)
    chosen, per_artist, seen = [], Counter(), set()
    for key in sorted(score, key=lambda k: -score[k]):
        song = p.by_key[key]
        ak, identity = artist_key(song.artist), song_key(song.artist, song.title)
        if score[key] <= 0 or per_artist[ak] >= FOR_YOU_PER_ARTIST or identity in seen:
            continue
        per_artist[ak] += 1
        seen.add(identity)
        chosen.append(key)
        if len(chosen) >= size:
            break
    return chosen


def song_neighbours(server, limit: int = 40) -> Callable[[str], List[Tuple[str, float]]]:
    def near(key: str) -> List[Tuple[str, float]]:
        return [(k, d) for kind, _t, k, d in nearest(server, key, limit, SONIC_DISTANCE) if kind == 'track']
    return near


def refresh_for_you(admin_server, server, section_name: str, user: str, is_admin: bool,
                    now: Optional[float] = None) -> int:
    """Rebuild one user's "Picked for You" playlist on their own account
    (`server`, their connection): library songs they have never played that
    sound like their loved ones (picked_for_you). Users whose taste isn't known
    well enough have none. Returns the number of songs in it."""
    name = for_you_playlist_name()
    section = server.library.section(section_name)
    tracks = {str(t.ratingKey): t for t in section.searchTracks()}
    songs = [s for s in (song_from_plex(t) for t in tracks.values()) if s]
    p = from_plex(user, songs, admin_server, admin_server.library.section(section_name), is_admin,
                  now=now, artists=False, user_server=server, user_section=section)
    playlists = [pl for pl in server.playlists() if pl.title.strip() == name
                 and getattr(pl, 'playlistType', 'audio') == 'audio' and not getattr(pl, 'smart', False)]
    items = []
    if p.ready():
        items = [tracks[k] for k in picked_for_you(p, song_neighbours(admin_server)) if k in tracks]
    if not items:
        for playlist in playlists:
            playlist.delete()
        logger.info(f"🎯 {user}: no '{name}' playlist" +
                    ("" if p.ready() else f" (fewer than {MIN_LOVED} loved songs so far)"))
        return 0
    for extra in playlists[1:]:
        extra.delete()
    playlist = playlists[0] if playlists else None
    if playlist is None:
        playlist = server.createPlaylist(name, items=items)
        try:
            playlist.editSummary("Songs here you haven't heard yet that sound like the ones you love. "
                                 "New picks every day.")
        except Exception as exc:
            logger.debug(f"Could not describe '{name}': {exc}")
    else:
        # Emptied and refilled rather than recreated, so a pin keeps working.
        current = playlist.items()
        if [str(i.ratingKey) for i in current] != [str(t.ratingKey) for t in items]:
            if current:
                playlist.removeItems(current)
                playlist.reload()
            playlist.addItems(items)
    logger.info(f"🎯 {user}: '{name}' holds {len(items)} song(s) they haven't heard, picked by sound")
    return len(items)


def save_summary(p: Profile, folder: str) -> None:
    """The profile in short, as taste.json in the user's data folder."""
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'taste.json'), 'w', encoding='utf-8') as handle:
            json.dump(dict(p.summary(), written=datetime.now().isoformat(timespec='seconds')), handle,
                      ensure_ascii=False, indent=1)
    except OSError as exc:
        logger.debug(f"Could not write the taste summary: {exc}")
