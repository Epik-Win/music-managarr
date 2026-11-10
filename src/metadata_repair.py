"""Library-wide metadata repair, run once a day by fix_metadata_daemon.py.

Finds tracks Plex is missing metadata for and fixes what it can:

1. Detection (`needs_repair`): a track whose album or title is unknown, whose
   artist is unknown, or that sits on a "Various Artists" album without a
   track artist of its own. A real compilation (Various Artists album whose
   tracks each carry their own artist) is complete, so it is left alone.
   Also: names that are mojibake (UTF-8 read as Latin-1, "MontrÃ©al") are
   repaired directly (text_repair), and a track Plex shows no artwork for gets
   it from the file's folder or online (artwork.ensure_artwork); art that
   can't be found goes to the Manual Fix playlist.
2. Correction, in order:
   a. Lidarr catalogue lookup (`resolve_with_lidarr`) — a cheap text search
      that recovers the artist from a known album title (only when exactly one
      artist has an album by that name), also trying the file's Artist/Album
      folder names as long as Lidarr confirms the pair.
   b. Audio fingerprinting through audio_id.py (`resolve_with_audio_id`) —
      identifies the song from the audio itself. A missing album is chosen
      from the services' suggestions by `pick_album`, preferring one Lidarr
      confirms is in the artist's catalogue and skipping compilations. When
      the rules can't settle it and there are several options, the local AI
      chooses among the albums fingerprinting returned (it may not name one of
      its own). The release Lidarr has under the song's own name (usually the
      single) is the last resort.
3. Anything still incomplete goes to the admin's "Manual Fix" playlist (see
   manual_fix.py) instead of being guessed at.

Answers only fill the fields that are missing — a known artist, album or
title is never changed — and are used only if they agree with the known title
and artist (merge_answer). Fingerprint services regularly match a cover, a
compilation or an unrelated song, so disagreement means "not this one", not
"the tags are wrong". Filled-in artists are the primary artist only, so a
collaboration doesn't become a new artist folder. A fix is only written when
artist, album and title are all known afterwards; partial answers are
escalated rather than half-applied.
"""
from naming import balanced, has_unbalanced_brackets, split_outside_brackets
import json
import logging
import os
import re
import time
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Set, Tuple

import requests

from text_repair import looks_garbled, repair_fields

logger = logging.getLogger('musicai.metadata_repair')

# Placeholder artists: "Lost" / "Lost & Found" are what library cleaners and
# rippers write for songs they could not identify; "Unidentified" is where
# this scan parks songs nothing could identify.
UNKNOWN_ARTISTS = {'', 'unknown', 'unknown artist', 'none', 'na', 'n/a', 'miscellaneous',
                   'lost', 'lost & found', 'lost and found', 'unidentified'}
UNIDENTIFIED = 'Unidentified'
VARIOUS_ARTISTS = {'various artists', 'various', 'va'}
# "Miscellaneous" is a placeholder, not an album: the library cleanup
# (cli.py) and some taggers write it when they have no album, and Plex then
# groups every such song of an artist into one fake album.
UNKNOWN_ALBUMS = {'', 'unknown', 'unknown album', 'none', 'na', 'n/a', 'miscellaneous', 'misc', 'misc.'}
UNKNOWN_TITLES = {'', 'unknown', 'unknown title', 'untitled', 'none', 'na', 'n/a'}
MATCH_THRESHOLD = 0.85
GARBLED = 'garbled text (mojibake)'
ARTIST_HOLDS_TITLE = 'artist name holds a song title'
MALFORMED_NAME = 'malformed name (an unclosed bracket, or a video title in the artist)'
NO_ARTWORK = 'missing artwork'


def _clean(value: Optional[str]) -> str:
    return (value or '').strip()


def _norm_label(value: Optional[str]) -> str:
    """Lower-case and drop surrounding brackets, so Plex's "[Unknown Album]"
    compares equal to "unknown album"."""
    return _clean(value).strip('[]() ').casefold()


def _norm_match(value: Optional[str]) -> str:
    # \w keeps letters of every script, so Cyrillic/Japanese names still
    # compare; accents are dropped so "Ahi" matches "Ahí".
    import unicodedata
    text = unicodedata.normalize('NFKD', _clean(value).casefold())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[\W_]+', '', text)


def similar(a: Optional[str], b: Optional[str]) -> float:
    left, right = _norm_match(a), _norm_match(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


# A performing-rights society after a name ("Dana Hobson (BMI)", "Daniel
# Wolf (ASCAP)") marks a production-music library's songwriter credit, not
# the act that performs the song (Dana and the Wolf).
_WRITER_CREDIT = re.compile(r"\((?:BMI|ASCAP|SESAC|GMR|PRS|SOCAN|GEMA|SACEM|APRA|JASRAC|STIM|IMRO|SIAE|SGAE|KODA|"
                            r"TONO|TEOSTO|BUMA|SABAM|AKM|ZAIKS)\)", re.I)


def artist_known(value: Optional[str]) -> bool:
    label = _norm_label(value)
    return label not in UNKNOWN_ARTISTS and label not in VARIOUS_ARTISTS and not _WRITER_CREDIT.search(value or '')


def album_known(value: Optional[str]) -> bool:
    return _norm_label(value) not in UNKNOWN_ALBUMS


def title_known(value: Optional[str]) -> bool:
    return _norm_label(value) not in UNKNOWN_TITLES


def track_meta(track) -> Dict[str, str]:
    """Current artist/album/title of a Plex track. The track's own artist
    (originalTitle) wins over the album artist, which may be Various Artists."""
    track_artist = _clean(getattr(track, 'originalTitle', ''))
    album_artist = _clean(getattr(track, 'grandparentTitle', ''))
    artist = track_artist if artist_known(track_artist) else (album_artist if artist_known(album_artist) else '')
    title = _clean(getattr(track, 'title', ''))
    return {
        'artist': artist,
        'album': _clean(getattr(track, 'parentTitle', '')) if album_known(getattr(track, 'parentTitle', '')) else '',
        'title': title if title_known(title) else '',
    }


def file_tag_meta(file_path: Optional[str]) -> Dict[str, str]:
    """Artist/album/title as tagged in the file itself."""
    try:
        from mutagen import File as MutagenFile
        tags = MutagenFile(file_path, easy=True) if file_path else None
    except Exception:
        tags = None
    if not tags:
        return {}

    def tag(key):
        return _clean((tags.get(key) or [''])[0])

    artist = tag('artist') if artist_known(tag('artist')) else tag('albumartist')
    # The artist written back becomes the album artist and the folder, so a
    # collaboration credit ("George Benson & Idina Menzel") would split the
    # album into an artist of its own. The album artist is used whenever it
    # is one of the credited artists; otherwise only the primary artist.
    if artist_known(tag('albumartist')) and artist != tag('albumartist'):
        if name_within(tag('albumartist'), artist):
            artist = tag('albumartist')
        else:
            artist = main_artist(artist)
    album = tag('album') if album_known(tag('album')) else ''
    # Downloaders write the artist's name as the album when they have none
    # ("Lupe Fiasco/Lupe Fiasco/Your Attention Please.mp3").
    if album and any(similar(album, a) >= MATCH_THRESHOLD for a in (artist, tag('albumartist')) if a) \
            and not lidarr_confirms_album(artist or tag('albumartist'), album):
        album = ''      # but a real self-titled album ("Taylor Swift", "Weezer") stays
    return {
        'artist': artist if artist_known(artist) else '',
        'album': album,
        'title': tag('title') if title_known(tag('title')) else '',
    }


def file_artist_tag(file_path: Optional[str]) -> Optional[str]:
    """The artist the file itself names: '' if it names none (or a
    placeholder is all there is), None if the file can't be read."""
    try:
        from mutagen import File as MutagenFile
        tags = MutagenFile(file_path, easy=True) if file_path else None
    except Exception:
        return None
    if tags is None:
        return None
    for key in ('albumartist', 'artist'):
        value = tags.get(key)
        if value:
            return '/'.join(value)
    return ''


def known_meta(track, file_path: Optional[str]) -> Dict[str, str]:
    """What is genuinely known about a track that needs repair.

    Plex files a song whose artist tag is missing or a placeholder
    ("Unknown") under some unrelated real artist, which can collect dozens
    of other artists' songs that way. So Plex's album artist is not evidence
    when the file can be read:
    the file's own tags are, then Plex's per-track artist. Only an album Plex
    matched online, or a file that can't be read, falls back to it.
    """
    plex = track_meta(track)
    tags = file_tag_meta(file_path)
    track_artist = _clean(getattr(track, 'originalTitle', ''))
    readable = file_artist_tag(file_path) is not None
    album_trusted = album_known(getattr(track, 'parentTitle', '')) and (
        not readable or str(getattr(track, 'parentGuid', '') or '').startswith('plex://'))
    artist = (tags.get('artist')
              or (track_artist if artist_known(track_artist) else '')
              or (plex['artist'] if album_trusted else ''))
    # An album that only repeats the artist's name ("Crack Amico / Crack
    # Amico") is a downloader's placeholder, from the file or from Plex.
    split = split_artist_title(artist) if ' - ' in (artist or '') else None
    split_title, junk_title = '', False
    if split:
        artist, split_title = split        # "Taylor Swift - Babylon" → Taylor Swift / Babylon
        # A title built from the same YouTube string ("Karol G & Drake (feat.
        # Drake - Ahi)") is junk too: the split's title stands instead (a
        # lookup by length can't match a music video's longer audio).
        junk_title = _norm_match(artist) in _norm_match(tags.get('title') or plex['title'])
    raw_title = tags.get('title') or plex['title']
    title_split = split_title_artist(raw_title, artist) if not split else None
    if title_split:
        artist, split_title = title_split
        junk_title = True
    album = tags.get('album') or plex['album']
    if title_split and album and _norm_match(album) == _norm_match(raw_title):
        album = split_title                # a single's album, named after the video
    if album and _VIDEO_WORDS.search(album):
        album = ''                         # a video's title, not an album
    # An album that only repeats the artist's name, or part of it ("Karol"
    # for KAROL G), is a downloader's placeholder; a self-titled album Lidarr
    # knows stays.
    if artist and album and (similar(album, artist) >= MATCH_THRESHOLD or name_within(album, artist)) \
            and not lidarr_confirms_album(artist, album):
        album = ''
    # Without a title tag Plex shows the file name anyway, and its copy can
    # be stale after a fix; the file name itself is the better source.
    title = split_title if junk_title else (tags.get('title') or (title_from_filename(file_path, artist) if readable else '')
                                            or plex['title'] or split_title)
    # "THAO: Remember Me (…)" by Thao Sam: a video's "Artist: Song" name
    # repeating the artist. A single's album, named after it, loses it too.
    unprefixed = _without_artist_prefix(title, artist)
    if unprefixed != title:
        album = unprefixed if album and _norm_match(album) == _norm_match(title) else album
        title = unprefixed                 # the artist stays as the library files the act
    return {'artist': artist, 'album': album, 'title': title}


_ARTIST_PREFIXES = (re.compile(r'^(?P<who>.{2,40}?)\s+[-–—]\s+(?P<rest>.+)$'),      # "Itzhak Perlman – Beethoven: …"
                    re.compile(r'^(?P<who>[^:：]{2,40}?)\s*[:：]\s*(?P<rest>.+)$'))    # "THAO: Remember Me"


def _without_artist_prefix(title: str, artist: str) -> str:
    for pattern in _ARTIST_PREFIXES:
        m = pattern.match(title or '')
        if m and artist and (name_within(m.group('who'), artist) or artists_agree(m.group('who'), artist)):
            return m.group('rest').strip()
    return title or ''


_ARTIST_AND_TITLE = re.compile(r'^(?P<artist>.+?)\s+-\s+(?P<title>.+)$')


def split_artist_title(value: Optional[str]) -> Optional[Tuple[str, str]]:
    """("Drake", "Make Them Pay") for an artist tag "Drake - Make Them Pay":
    YouTube-style "Artist - Title" text that ended up as the artist. Only
    when the left part is a real act and the whole is not (a band called
    "Something - Something" stays)."""
    m = _ARTIST_AND_TITLE.match(_clean(value))
    if not m or is_known_artist(value):
        return None
    left = main_artist(primary_artist(m.group('artist')))
    right = m.group('title').strip()
    # Two act names joined by a dash are one act ("PKF - Prague Philharmonia").
    if not is_known_artist(left) or is_known_artist(right):
        return None
    return left, right


def split_title_artist(title: str, artist: str) -> Optional[Tuple[str, str]]:
    """("Lil Baby", "Freestyle") for a title tag "Lil Baby - Freestyle" on a
    file tagged with another act ("Lupe Fiasco"): the video's own
    "Artist - Title" name, while the artist tag came from what the download
    was looking for. Only when the left part is a known act the tagged artist
    isn't part of, and Deezer lists the song by that act ("Freestyle" is
    also some act's name, so being one proves nothing)."""
    if artist and name_within(artist, title):
        return None
    # 'NO play "Dark Side of the Moon" by Pink Floyd': a performance named
    # by who plays what (the band "No", live in 2011).
    p = _PLAYS_TITLE.match(_clean(title))
    if p and not artists_agree(artist, p.group('artist')):
        return p.group('artist').strip(), p.group('title').strip()
    q = _QUOTED_TITLE.match(_clean(title))
    if q and not artists_agree(artist, q.group('artist')):
        # 'Divine of the Dey "BabyFace" [OFFICIAL VIDEO] Directed by Jayonez':
        # a video's own name, believed from its video wording or a listing.
        left, right = q.group('artist').strip(), q.group('title').strip()
        if _VIDEO_WORDS.search(q.group('rest')) or (is_known_artist(left) and listed_length(left, right)):
            return left, right
    m = _ARTIST_AND_TITLE.match(_clean(title))
    if not m:
        return None
    left, right = m.group('artist').strip(), m.group('title').strip()
    if not is_known_artist(left) or variant_labels_any(right):
        return None
    if artist and artists_agree(artist, left):
        return None
    left = main_artist(left)
    return (left, right) if listed_length(left, right) else None


_PLAYS_TITLE = re.compile(r'^(?P<artist>[^"“”＂]{1,40}?)\s+(?:plays?|performs?|covers?)\s+["“＂](?P<title>[^"“”＂]{1,80})["”＂]'
                          r'(?:\s+by\s+.+)?$', re.I)
_QUOTED_TITLE = re.compile(r'^(?P<artist>[^"“”＂]{2,60}?)\s+["“＂](?P<title>[^"“”＂]{1,80})["”＂]\s*(?P<rest>.*)$')
_VIDEO_WORDS = re.compile(r'official\s+(?:music\s+)?video|music\s+video|directed\s+by|dir\.\s|visuali[sz]er|lyric\s+video',
                          re.I)


def variant_labels_any(text: str) -> bool:
    """A cover, remix, live or other version label: such a title names the
    original act while another one performs ("Song - Artist (Cover)")."""
    from song_dedup import variant_labels
    return bool(variant_labels(text)) or bool(re.search(r'\b(?:cover|tribute|karaoke|reaction)\b', text, re.I))


def has_own_art(thumb: Optional[str], key) -> bool:
    """Whether a Plex thumb is the item's own picture. Plex shows an album
    with no art of its own using its artist's picture (thumb
    /library/metadata/<artist key>/thumb/...), which looks like art but is the
    same image on every such album."""
    if not thumb:
        return False
    if key in (None, ''):
        return True
    return str(thumb).startswith(f'/library/metadata/{key}/')


def needs_repair(track) -> Optional[str]:
    """Why this track needs repair, or None if it is fine."""
    if not album_known(getattr(track, 'parentTitle', '')):
        return 'unknown album'
    if not title_known(getattr(track, 'title', '')):
        return 'unknown title'
    track_artist = getattr(track, 'originalTitle', '')
    album_artist = getattr(track, 'grandparentTitle', '')
    if not (artist_known(track_artist) or artist_known(album_artist)):
        if _norm_label(album_artist) in VARIOUS_ARTISTS:
            return 'Various Artists album with no track artist'
        return 'unknown artist'
    if any(looks_garbled(getattr(track, attr, '')) for attr in ('title', 'parentTitle', 'grandparentTitle', 'originalTitle')):
        return GARBLED
    if any(' - ' in (value or '') and split_artist_title(value) for value in (album_artist, track_artist)):
        return ARTIST_HOLDS_TITLE
    names = [getattr(track, attr, '') for attr in ('title', 'parentTitle', 'grandparentTitle', 'originalTitle')]
    if any(has_unbalanced_brackets(n) for n in names) or any(' | ' in (a or '') for a in (album_artist, track_artist)):
        # "RISE (ft. The Glitch Mob" (a cut-off credit), "RISE (…) | Worlds 2018" (a video title).
        return MALFORMED_NAME
    if not has_own_art(getattr(track, 'parentThumb', None), getattr(track, 'parentRatingKey', None)):
        return NO_ARTWORK
    return None


def track_label(meta: Dict[str, str]) -> str:
    return f"{meta.get('artist') or 'Unknown Artist'} – {meta.get('title') or 'Unknown Title'} [{meta.get('album') or 'Unknown Album'}]"


def is_complete(meta: Optional[Dict[str, str]]) -> bool:
    return bool(meta and artist_known(meta.get('artist')) and album_known(meta.get('album')) and title_known(meta.get('title')))


# ── Lidarr ───────────────────────────────────────────────────────────────────

def _lidarr_config() -> Tuple[str, str]:
    from lidarr_catalog import lidarr_instances
    instances = lidarr_instances()
    return instances[0] if instances else ('', '')


# Lookups that failed for reasons other than "no match" (Lidarr's metadata
# server returns 503 at times). A track that hit one is retried on the next
# pass rather than handed to the admin as unidentifiable.
_service_errors = 0


def lidarr_lookup(term: str) -> List[Dict[str, str]]:
    """Albums matching a search term, as [{'artist', 'album'}]."""
    base_url, api_key = _lidarr_config()
    if not base_url or not term.strip():
        return []
    try:
        resp = requests.get(
            f'{base_url}/api/v1/album/lookup',
            params={'term': term},
            headers={'X-Api-Key': api_key},
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json() or []
    except Exception as exc:
        # Lidarr's upstream metadata server intermittently fails on some
        # searches (HTTP 503). Treated as "no match", which only ever falls
        # through to fingerprinting or Manual Fix, never to a guess.
        logger.warning(f"Lidarr lookup failed for {term!r}: {exc}")
        global _service_errors
        _service_errors += 1
        return []
    results = []
    for item in items if isinstance(items, list) else []:
        artist = _clean((item.get('artist') or {}).get('artistName'))
        album = _clean(item.get('title'))
        if artist and album:
            results.append({
                'artist': artist, 'album': album,
                'statuses': sorted({r.get('status') for r in item.get('releases') or [] if r.get('status')}),
                'secondary': list(item.get('secondaryTypes') or []),
            })
    return results


# A release that only exists as a bootleg, or a compilation / DJ mix, isn't
# where a song belongs, even though MusicBrainz (and so Lidarr) lists it under
# the artist: Lidarr "confirming" a Beatles bootleg must not file a song there.
_NOT_A_HOME = {'Compilation', 'DJ-mix', 'Mixtape/Street', 'Demo'}


def _real_release(candidate: Dict) -> bool:
    statuses = candidate.get('statuses')
    if statuses and 'Official' not in statuses:
        return False
    return not (_NOT_A_HOME & set(candidate.get('secondary') or []))


def lidarr_album_for_track(artist: str, title: str) -> Optional[str]:
    """The album a known artist + title belongs to, when Lidarr has it as a
    release of that name (typically a single)."""
    if not artist_known(artist) or not title_known(title):
        return None
    for candidate in lidarr_lookup(f'{artist} {title}'):
        if similar(candidate['artist'], artist) >= MATCH_THRESHOLD and similar(candidate['album'], title) >= MATCH_THRESHOLD:
            return candidate['album']
    return None


def resolve_with_lidarr(meta: Dict[str, str]) -> Optional[Dict[str, str]]:
    """Fill in the missing artist or album from Lidarr's catalogue. Returns a
    complete artist/album/title dict or None."""
    artist, album, title = meta.get('artist', ''), meta.get('album', ''), meta.get('title', '')
    if album_known(album):
        term = f'{artist} {album}' if artist_known(artist) else album
        matches = [
            c for c in lidarr_lookup(term)
            if similar(c['album'], album) >= MATCH_THRESHOLD
            and (not artist_known(artist) or similar(c['artist'], artist) >= MATCH_THRESHOLD)
        ]
        if not matches:
            return None
        # Album titles are rarely unique ("The Heist" is an album by nine
        # different artists in Lidarr), so an unknown artist is only taken
        # from Lidarr when exactly one artist has an album by that name;
        # otherwise fingerprinting decides from the audio.
        if not artist_known(artist) and len({_norm_match(c['artist']) for c in matches}) > 1:
            logger.info(
                f"Lidarr: album {album!r} belongs to {len(matches)} different artists — "
                f"too ambiguous, leaving it to audio fingerprinting"
            )
            return None
        resolved = {'artist': matches[0]['artist'], 'album': matches[0]['album'], 'title': title}
        return resolved if is_complete(resolved) else None
    # With no album to search on, Lidarr's only guess is a release named after
    # the song — for most hits that's the single, not the album the song is
    # on, so it is left to pick_album() as a last resort after fingerprinting.
    return None


# Album names fingerprint services return for compilations and radio promos.
_COMPILATION_MARKERS = (
    'hits', 'grammy', 'nominee', 'promoonly', 'nowthat', 'various', 'compilation',
    'karaoke', 'workout', 'party', 'hiphopmix', 'besthiphop', 'djmix', 'radiomix',
)


def looks_like_compilation(album: Optional[str]) -> bool:
    text = _norm_match(album)
    return any(marker in text for marker in _COMPILATION_MARKERS)


def album_owner(album: str, credited: str) -> Optional[str]:
    """The artist Lidarr lists the album under, if that artist is part of the
    song's credit — so "John Summit & LAVINIA – SHADOWS" on John Summit's
    CTRL ESCAPE is filed under John Summit with the rest of the album."""
    for candidate in lidarr_lookup(album):
        if name_within(candidate['artist'], credited) and similar(candidate['album'], album) >= MATCH_THRESHOLD:
            return candidate['artist']
    return None


def lidarr_confirms_album(artist: str, album: str) -> bool:
    """True if Lidarr lists an album of (about) that name by this artist."""
    for candidate in lidarr_lookup(f'{artist} {album}'):
        if not artists_agree(artist, candidate['artist']) or not _real_release(candidate):
            continue
        if similar(candidate['album'], album) >= MATCH_THRESHOLD or \
                similar(_core_title(candidate['album']), _core_title(album)) >= MATCH_THRESHOLD:
            return True
    return False


def pick_album(artist: str, title: str, candidates: List[Optional[str]]) -> Optional[str]:
    """Best album for a track from the fingerprint services' suggestions.

    Preference: an album Lidarr confirms is in the artist's catalogue, and an
    album over a single named after the song. Compilations Lidarr can't tie to
    the artist are dropped ("2012 Grammy Nominees" is not where "Rolling in
    the Deep" belongs). If nothing qualifies, Lidarr's release named after the
    song (usually the single) is the last resort.
    """
    best, best_score, seen, options = None, -1, set(), []
    for album in candidates:
        if not album_known(album) or _norm_match(album) in seen:
            continue
        seen.add(_norm_match(album))
        confirmed = lidarr_confirms_album(artist, album)
        if not confirmed and looks_like_compilation(album):
            continue
        options.append(_clean(album))
        score = (2 if confirmed else 0) + (0 if titles_agree(album, title) else 1)
        if score > best_score:
            best, best_score = _clean(album), score
    # When the rules can't settle it (no Lidarr-confirmed album that isn't just
    # the single) and there is a real choice, the local AI picks among the
    # albums fingerprinting returned for this audio.
    if best_score < 3 and len(options) >= 2:
        ai_album = ai_choose_album(artist, title, options)
        if ai_album:
            return ai_album
    return best or lidarr_album_for_track(artist, title)


_REMASTER_SUFFIX = re.compile(r'\s*[\(\[](?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?[\)\]]\s*$', re.I)


def file_isrc(file_path: Optional[str]) -> str:
    """The ISRC tagged in the file (deemix writes it), or ''."""
    try:
        from mutagen import File as MutagenFile
        tags = MutagenFile(file_path, easy=True) if file_path else None
    except Exception:
        tags = None
    value = ((tags.get('isrc') or [''])[0] if tags else '') or ''
    value = re.sub(r'[^A-Z0-9]', '', value.upper())
    return value if len(value) == 12 else ''


def deezer_track_by_isrc(isrc: str, duration: Optional[float] = None) -> Optional[Dict[str, str]]:
    """The exact recording an ISRC names, from Deezer — the strongest evidence
    there is: a wrong fix rewrites the names but leaves the ISRC deemix wrote,
    so the ISRC still names the recording in the file. Only used if the
    length matches the file."""
    if not isrc:
        return None
    try:
        r = requests.get(f'https://api.deezer.com/track/isrc:{isrc}', timeout=10).json()
    except Exception as exc:
        logger.debug(f"Deezer ISRC lookup failed for {isrc}: {exc}")
        return None
    if not isinstance(r, dict) or r.get('error') or not r.get('title'):
        return None
    if duration and r.get('duration') and abs(r['duration'] - duration) > DURATION_TOLERANCE_SECONDS:
        logger.info(f"ISRC {isrc} is {r['duration']}s on Deezer, the file {duration:.0f}s: not used")
        return None
    album = r.get('album') or {}
    return {'artist': _clean((r.get('artist') or {}).get('name')), 'title': _clean(r.get('title')),
            'album': _REMASTER_SUFFIX.sub('', _clean(album.get('title'))),
            'cover_art_url': album.get('cover_xl') or album.get('cover_big'), 'service': 'ISRC', 'votes': 99}


def deezer_release(answer: Dict[str, str], duration: Optional[float] = None,
                   tolerance: Optional[float] = None) -> Optional[Dict[str, str]]:
    """The Deezer release a fingerprint answer points to by Deezer track ID
    (ACRCloud) or ISRC (Shazam, ACRCloud): a direct lookup, which works where
    Deezer's search hides songs not licensed in the VPN's exit country (Amr
    Mostafa's "Law Fi Hayati" single from a US exit). Only a recording of the
    file's length. {'artist', 'title', 'album', 'cover_art_url'} or None."""
    urls = ([f"https://api.deezer.com/track/{answer['deezer_id']}"] if answer.get('deezer_id') else []) \
        + ([f"https://api.deezer.com/track/isrc:{answer['isrc']}"] if answer.get('isrc') else [])
    for url in urls:
        try:
            r = requests.get(url, timeout=10).json()
        except Exception as exc:
            logger.debug(f"Deezer track lookup failed ({url}): {exc}")
            continue
        if not isinstance(r, dict) or r.get('error') or not r.get('title'):
            continue
        if duration and r.get('duration') and abs(r['duration'] - duration) > (tolerance or DURATION_TOLERANCE_SECONDS):
            continue
        album = r.get('album') or {}
        return {'artist': _clean((r.get('artist') or {}).get('name')), 'title': _clean(r.get('title')),
                'album': _REMASTER_SUFFIX.sub('', _clean(album.get('title'))),
                'cover_art_url': album.get('cover_xl') or album.get('cover_big') or ''}
    return None


def deezer_albums_for_track(artist: str, title: str, duration: Optional[float] = None) -> List[str]:
    """Albums Deezer lists this song on (its ranking puts the canonical
    release first), keeping only results whose artist, title and version
    labels match. Fingerprint services often name a bootleg or a
    compilation ("Hours of Darkness" for the Beatles' "Maggie Mae")."""
    if not artist_known(artist) or not title_known(title):
        return []
    try:
        from artwork import _artists, _plain, _search_artist, result_matches
        first = (_artists(artist) or [artist])[0].strip()
        data = []
        # Deezer's artist: filter needs the exact name ("The Beatles", not "Beatles").
        for query in (f'artist:"{first}" track:"{_plain(title)}"', f'{_search_artist(artist)} {_plain(title)}'):
            data = requests.get('https://api.deezer.com/search', params={'q': query, 'limit': 10},
                                timeout=10).json().get('data') or []
            if data:
                break
    except Exception as exc:
        logger.debug(f"Deezer album lookup failed for {artist} – {title}: {exc}")
        return []
    albums = []
    for r in data:
        names = [(r.get('artist') or {}).get('name', '')] + [c.get('name', '') for c in r.get('contributors') or []]
        album = _REMASTER_SUFFIX.sub('', _clean((r.get('album') or {}).get('title')))
        # Same title, different recording: Deezer's 396 s "Moonlight Sonata"
        # is not the 162 s file, so its album isn't this file's album.
        if duration and r.get('duration') and abs(r['duration'] - duration) > DURATION_TOLERANCE_SECONDS + 2:
            continue
        if album and album not in albums and result_matches(artist, title, '', names, r.get('title', ''), album):
            albums.append(album)
    return albums


_ARTIST_DASH_TITLE = re.compile(r'^(?P<artist>.+?)\s+[—–-]\s+(?P<title>.+)$')


def name_candidates(meta: Dict[str, str], file_path: Optional[str], extra: List[str] = ()) -> List[str]:
    """Titles a song might be listed under: the tag, the file name, an album
    field shaped like "Artist — Title (Official video)" (YouTube rips), and
    any extra guesses (a fingerprint answer whose artist was junk)."""
    out: List[str] = []

    def add(value):
        value = re.sub(r'\s+', ' ', _FILENAME_NOISE.sub('', _clean(value))).strip(' -_')
        if title_known(value) and value.casefold() not in {o.casefold() for o in out}:
            out.append(value)

    def add_split(value):
        # "Karol G & Drake - Ahi", "HanMari — Migration (Official video)"
        m = _ARTIST_DASH_TITLE.match(_FILENAME_NOISE.sub('', _clean(value)))
        if m and meta.get('artist') and (similar(m.group('artist'), meta['artist']) >= MATCH_THRESHOLD
                                         or artists_agree(m.group('artist'), meta['artist'])):
            add(m.group('title'))

    add(meta.get('title'))
    add(title_from_filename(file_path, meta.get('artist')))
    # The folder too: a download's folder is often named "Artist - Title".
    folder = os.path.basename(os.path.dirname(file_path)) if file_path else ''
    for value in (meta.get('title'), meta.get('album'), title_from_filename(file_path, meta.get('artist')),
                  meta.get('artist'), folder):
        add_split(value)
    for value in extra:
        add(value)
    return out


def release_listing(artist: str, title: str, duration: Optional[float], tolerance: float = 3) -> Optional[Dict[str, str]]:
    """This title by this artist on Deezer or Spotify within a few seconds of
    the file's length: the recording is listed, with its album. Returns
    {'album', 'source'}, or None."""
    if not duration or not artist_known(artist) or not title_known(title):
        return None
    try:
        from artwork import _query, _search_artist, result_matches
        from song_dedup import variant_labels
    except Exception:
        return None
    try:
        data = requests.get('https://api.deezer.com/search', params={'q': f'{_search_artist(artist)} {_query(title)}',
                                                                      'limit': 10}, timeout=10).json().get('data') or []
    except Exception:
        data = []
    for r in data:
        names = [(r.get('artist') or {}).get('name', '')] + [c.get('name', '') for c in r.get('contributors') or []]
        album = _REMASTER_SUFFIX.sub('', _clean((r.get('album') or {}).get('title')))
        if r.get('duration') and abs(r['duration'] - duration) <= tolerance and \
                result_matches(artist, title, '', names, r.get('title', ''), album):
            return {'album': album, 'title': _clean(r.get('title')), 'source': 'Deezer'}
    try:
        import spotify_web
        results = spotify_web.get_client().search(f'{_search_artist(artist)} {_query(title)}')
        for track in (getattr(results, 'tracks', None) or [])[:10]:
            flat = spotify_web._track_to_dict(track)
            ms = getattr(track, 'duration_ms', None)
            if flat and ms and abs(ms / 1000 - duration) <= tolerance and \
                    result_matches(artist, title, '', flat['artist'].split(', '), flat['title'], flat['album']):
                # The artist's own release, not a various-artists compilation
                # named like an album ("Sunshine Arabia 2008").
                album_id = getattr(getattr(track, 'album', None), 'id', None)
                album_artist = spotify_web.get_album(album_id)[1] if album_id else ''
                if album_artist and not artists_agree(album_artist, artist):
                    continue
                return {'album': _REMASTER_SUFFIX.sub('', flat['album']), 'title': flat['title'], 'source': 'Spotify'}
    except Exception as exc:
        logger.debug(f"Spotify listing check failed: {exc}")
    return None


from artwork import SPECIAL_RELEASE as _SPECIAL_RELEASE  # noqa: E402  (artwork needs this module only inside functions)


def reference_version(artist: str, title: str) -> Optional[Dict[str, object]]:
    """The true recording of this title by this artist, whatever its length:
    the Deezer release (a compilation only if nothing else lists it), else
    the artist's own YouTube upload for songs that exist only there.
    {'source': 'deezer'|'youtube', 'id', 'duration', 'artist', 'title', 'album'}."""
    if not artist_known(artist) or not title_known(title):
        return None
    try:
        from artwork import _query, _search_artist, own_youtube_upload, result_matches, _title_number
        from song_dedup import variant_labels
        data = requests.get('https://api.deezer.com/search', params={'q': f'{_search_artist(artist)} {_query(title)}',
                                                                      'limit': 15}, timeout=10).json().get('data') or []
    except Exception as exc:
        logger.debug(f"reference lookup failed for {artist} – {title}: {exc}")
        data = []
    found = []
    for r in data:
        names = [(r.get('artist') or {}).get('name', '')] + [c.get('name', '') for c in r.get('contributors') or []]
        album = _REMASTER_SUFFIX.sub('', _clean((r.get('album') or {}).get('title')))
        if r.get('duration') and result_matches(artist, title, '', names, r.get('title', ''), album) \
                and variant_labels(title) == variant_labels(r.get('title', '')):
            found.append({'source': 'deezer', 'id': r.get('id'), 'duration': float(r['duration']),
                          'artist': _clean((r.get('artist') or {}).get('name')), 'title': _clean(r.get('title')),
                          'album': album, 'compilation': looks_like_compilation(album),
                          'special': bool(_SPECIAL_RELEASE.search(album + ' ' + (r.get('title') or ''))),
                          'rank': r.get('rank') or 0, 'cover': (r.get('album') or {}).get('cover_xl')})
    # The original, not a re-recording ("Taylor's Version") the tags don't name.
    rerecord = re.compile(r"taylor'?s version|re-?record", re.I)
    # Radio specials, commentary and interviews are never the recording
    # ("Red (Big Machine Radio Release Special)" has a 58 s "Everything Has
    # Changed" commentary); then the most played listing — the original.
    found = [f for f in found if not f['special'] or _SPECIAL_RELEASE.search(title)]
    order = lambda f: (f['compilation'], bool(rerecord.search(f['title'] + ' ' + f['album']))
                       and not rerecord.search(title), -f['rank'])
    found.sort(key=order)
    if not found or order(found[0])[:2] != (False, False):
        # Only a re-recording, a compilation or nothing: the search can rank
        # the original below a page of karaoke covers, so look for it on the
        # artist's album these editions are named after.
        found = sorted(found + _original_album_versions(artist, title, data), key=order)
    if found:
        return found[0]
    try:
        own = own_youtube_upload(artist, title)
    except Exception:
        own = None
    if own:
        return {'source': 'youtube', 'id': own[0], 'duration': own[1], 'artist': artist, 'title': title, 'album': ''}
    return None


def _original_album_versions(artist: str, title: str, rows: list) -> List[Dict[str, object]]:
    """This title on the artist's original album, for when the search shows
    only later editions of it. Deezer's search lists "Everything Has Changed"
    as "(Taylor's Version)" and as a 58 s commentary on "Red (Big Machine Radio
    Release Special)"; "Red", the album both are named after, has the 245 s
    original but ranks below the karaoke covers. Same fields as
    reference_version's candidates."""
    from artwork import _BRACKETS_RE, _core
    from song_dedup import variant_labels
    want, who = _core(title), _core(artist)
    artist_ids, bases, seen = [], set(), set()
    for r in rows:
        a = r.get('artist') or {}
        if _core(a.get('name')) != who or _core(r.get('title')) != want:
            continue
        album = r.get('album') or {}
        seen.add(album.get('id'))
        if _core(album.get('title')):
            bases.add(_core(album.get('title')))           # "Red (Taylor's Version)" → "red"
        if a.get('id') and a['id'] not in artist_ids:
            artist_ids.append(a['id'])
    out: List[Dict[str, object]] = []
    for artist_id in artist_ids[:2]:
        try:
            albums = requests.get(f'https://api.deezer.com/artist/{artist_id}/albums', params={'limit': 500},
                                  timeout=10).json().get('data') or []
        except Exception as exc:
            logger.debug(f"discography lookup failed for {artist}: {exc}")
            continue
        # The edition named plainly first ("Red" before "Red (Deluxe Edition)"),
        # then the earliest.
        named = sorted((al for al in albums if al.get('id') not in seen and _core(al.get('title')) in bases),
                       key=lambda al: (bool(_BRACKETS_RE.search(al.get('title') or '')), al.get('release_date') or '9999'))
        for al in named[:3]:
            try:
                tracks = requests.get(f"https://api.deezer.com/album/{al['id']}/tracks", params={'limit': 200},
                                      timeout=10).json().get('data') or []
            except Exception:
                continue
            album = _REMASTER_SUFFIX.sub('', _clean(al.get('title')))
            for t in tracks:
                if (t.get('duration') and _core(t.get('title')) == want
                        and variant_labels(title) == variant_labels(t.get('title', ''))
                        and not _SPECIAL_RELEASE.search(album + ' ' + (t.get('title') or ''))):
                    out.append({'source': 'deezer', 'id': t.get('id'), 'duration': float(t['duration']),
                                'artist': _clean((t.get('artist') or {}).get('name')) or artist,
                                'title': _clean(t.get('title')), 'album': album,
                                'compilation': looks_like_compilation(album), 'special': False,
                                'rank': t.get('rank') or 0, 'cover': al.get('cover_xl')})
            if out:
                return out
    return out


def isrc_length(isrc: str) -> Optional[int]:
    """Deezer's length for the recording an ISRC names, or None."""
    if not isrc:
        return None
    try:
        r = requests.get(f'https://api.deezer.com/track/isrc:{isrc}', timeout=10).json()
    except Exception:
        return None
    return r.get('duration') if isinstance(r, dict) and not r.get('error') else None


def listed_length(artist: str, title: str) -> Optional[int]:
    """Deezer's length for this title by this artist (any length), or None."""
    try:
        from artwork import _query, _search_artist, result_matches
        data = requests.get('https://api.deezer.com/search', params={'q': f'{_search_artist(artist)} {_query(title)}',
                                                                      'limit': 10}, timeout=10).json().get('data') or []
    except Exception:
        return None
    for r in data:
        names = [(r.get('artist') or {}).get('name', '')]
        if result_matches(artist, title, '', names, r.get('title', ''), ''):
            return r.get('duration')
    return None


def deezer_lookup_by_name(meta: Dict[str, str], file_path: Optional[str],
                          extra_titles: List[str] = ()) -> Optional[Dict[str, str]]:
    """Find the song on Deezer by its names when fingerprinting couldn't.

    A result counts only if its length is within a few seconds of the file
    and either its artist agrees with the known one, or — when the known
    artist is junk — it is Deezer's top result for that exact title. HanMari's
    "Migration" (title tag: a YouTube description, album: "HanMari —
    Migration (Official video)") and Macklemore's "Thrift Shop" (Shazam:
    "Thrift Shop & I'm Gonna Pop Some Tags") are found this way."""
    duration = audio_duration(file_path) if file_path else None
    if not duration:
        return None
    artist = meta.get('artist') if artist_known(meta.get('artist')) else ''
    try:
        from artwork import _plain, _search_artist
        from song_dedup import variant_labels
    except Exception:
        return None
    for title in name_candidates(meta, file_path, list(extra_titles)):
        queries = ([f'{_search_artist(artist)} {_plain(title)}'] if artist else []) + [_plain(title)]
        for query in queries:
            try:
                data = requests.get('https://api.deezer.com/search', params={'q': query, 'limit': 10},
                                    timeout=10).json().get('data') or []
            except Exception as exc:
                logger.debug(f"Deezer name lookup failed for {query!r}: {exc}")
                return None
            for rank, r in enumerate(data):
                r_artist = (r.get('artist') or {}).get('name', '')
                r_title = r.get('title', '')
                if not r.get('duration') or abs(r['duration'] - duration) > DURATION_TOLERANCE_SECONDS:
                    continue
                if not titles_agree(title, r_title) or variant_labels(title) != variant_labels(r_title):
                    continue
                # A cover act named after the song ("Thrift Shop & I'm Gonna Pop Some Tags")
                core = _norm_match(_core_title(r_title))
                if len(core) >= 4 and core in _norm_match(r_artist):
                    continue
                if artist and not (artists_agree(artist, r_artist) or name_within(artist, r_artist)
                                   or name_within(r_artist, artist)):
                    continue
                if not artist and rank != 0:
                    continue
                return {'artist': main_artist(r_artist), 'title': _clean(r_title),
                        'album': _REMASTER_SUFFIX.sub('', _clean((r.get('album') or {}).get('title')))}
    return None


# ── Local AI ─────────────────────────────────────────────────────────────────

def ask_local_ai(system: str, prompt: str) -> Optional[Dict]:
    """One JSON chat completion from the local AI (LOCAL_AI_API_BASE /
    LOCAL_AI_MODEL). Unlike the downloader's helper this never waits for an
    unreachable AI: a background repair pass just goes without it."""
    base = os.environ.get('LOCAL_AI_API_BASE', '').strip().rstrip('/')
    model = os.environ.get('LOCAL_AI_MODEL', '').strip()
    api_key = os.environ.get('LOCAL_AI_API_KEY', '').strip()
    if not base or not model:
        return None
    try:
        resp = requests.post(
            f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}'} if api_key else {},
            json={
                'model': model,
                'temperature': 0.1,
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
            },
            timeout=90,
        )
        resp.raise_for_status()
        text = resp.json()['choices'][0]['message']['content']
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return json.loads(match.group(0)) if match else None
    except Exception as exc:
        logger.info(f"Local AI unavailable or unparseable ({exc}); continuing without it")
        return None


def ai_choose_album(artist: str, title: str, options: List[str]) -> Optional[str]:
    """Which of `options` (albums fingerprint services returned for this
    audio) the local AI thinks the song belongs on.

    The AI may only choose, never name an album of its own: asked outright,
    a model can confidently answer "Anti" for Rihanna's "S&M" (it is on
    "Loud"), and since Anti is a real Rihanna album no catalogue check would
    catch it.
    """
    reply = ask_local_ai(
        'You are a music librarian. Reply with compact JSON only.',
        f'The song "{title}" by {artist} was matched to these releases: {options}. '
        'Which one is the album the song belongs on (its original album rather than a '
        'single, EP, remix release or compilation, where that choice exists)? '
        'Answer with one of the listed titles exactly. '
        'Reply {"album": "<one of the listed titles>", "confident": true|false}.',
    )
    if not reply or not reply.get('confident'):
        logger.info(f"Local AI not sure which of {options} fits {artist} – {title}; rules decide")
        return None
    for option in options:
        if similar(option, reply.get('album')) >= MATCH_THRESHOLD:
            logger.info(f"Local AI chose {option!r} for {artist} – {title} from {options}")
            return option
    logger.info(f"Local AI answered {reply.get('album')!r}, not one of {options}, for {artist} – {title}; ignored")
    return None


# ── Merging an answer into what is already known ─────────────────────────────

# Collaborators are listed after these; "&" is not one of them because it is
# part of duo names ("Macklemore & Ryan Lewis", "Rauf & Faik").
_ARTIST_SEPARATORS = re.compile(r'\s*(?:,|/|;|\s(?:feat\.?|ft\.?|featuring)\s)\s*', re.IGNORECASE)


def primary_artist(value: Optional[str]) -> str:
    """"T.I., Young Dro" → "T.I."; writing the full collaborator list as the
    artist would give every collaboration its own artist folder in Plex.
    A separator inside brackets is part of a credit, not a list."""
    parts = split_outside_brackets(_clean(value), _ARTIST_SEPARATORS)
    return balanced(parts[0]) if parts else _clean(value)


_KNOWN_ARTIST_CACHE: Dict[str, bool] = {}
KNOWN_ARTIST_MIN_FANS = 50


def is_known_artist(name: Optional[str]) -> bool:
    """One act of exactly this name: an artist Lidarr knows, or a Deezer
    artist with a following. Cached per process."""
    key = _norm_match(name)
    if not key:
        return False
    if key in _KNOWN_ARTIST_CACHE:
        return _KNOWN_ARTIST_CACHE[key]
    known = False
    try:
        from lidarr_catalog import LidarrCatalog, compact
        catalog = _KNOWN_ARTIST_CACHE.get('__catalog__')
        if catalog is None:
            catalog = LidarrCatalog.from_env()
            if catalog:
                catalog.load()
            _KNOWN_ARTIST_CACHE['__catalog__'] = catalog or False
        if catalog:
            known = bool(compact(name)) and compact(name) in catalog._by_artist
    except Exception as exc:
        logger.debug(f"Lidarr artist lookup failed for {name!r}: {exc}")
    if not known:
        try:
            found = requests.get('https://api.deezer.com/search/artist', params={'q': name, 'limit': 5},
                                 timeout=10).json().get('data') or []
            known = any(_norm_match(a.get('name')) == key and (a.get('nb_fan') or 0) >= KNOWN_ARTIST_MIN_FANS
                        for a in found)
        except Exception as exc:
            logger.debug(f"Deezer artist lookup failed for {name!r}: {exc}")
    _KNOWN_ARTIST_CACHE[key] = known
    return known


def is_established_artist(name: Optional[str]) -> bool:
    """A real act, allowing for a rename: "YBN Cordae" is Deezer's "Cordae"
    (72k fans). Only used to tell an original from a re-upload alias."""
    if is_known_artist(name):
        return True
    try:
        found = requests.get('https://api.deezer.com/search/artist', params={'q': name, 'limit': 3},
                             timeout=10).json().get('data') or []
    except Exception:
        return False
    return any((a.get('nb_fan') or 0) >= 10000 and (name_within(a.get('name'), name) or name_within(name, a.get('name')))
               for a in found)


def current_artist_name(name: Optional[str]) -> Optional[str]:
    """The name an act releases under now, when the tagged one is an old
    name of it: "YBN Cordae" is "Cordae" on Deezer, Spotify and his own
    SoundCloud; "Soulja Boy Tell 'Em" is "Soulja Boy". Only for one act that
    is no known artist itself (Lidarr's "Patti Smith Group" stays; Deezer's
    empty "YBN Cordae" feature-credit profile doesn't count), renamed to a
    part of its name ("Ye" is not within "Kanye West"). None otherwise."""
    if not name or ' - ' in name or len(credited_artists(name)) != 1 or is_known_artist(name):
        return None                    # "Artist - Title" text is split elsewhere
    try:
        found = requests.get('https://api.deezer.com/search/artist', params={'q': name, 'limit': 5},
                             timeout=10).json().get('data') or []
    except Exception:
        return None
    for a in found:
        if ((a.get('nb_fan') or 0) >= 10000 and name_within(a.get('name'), name)
                and _norm_match(a.get('name')) != _norm_match(name)):
            return _clean(a.get('name'))
    return None


def main_artist(value: Optional[str]) -> str:
    """The artist a song is filed under. The whole credit when it is one act's
    name ("Earth, Wind & Fire", "Simon & Garfunkel" — primary_artist cut the
    first to "Earth"), otherwise the first credited artist ("Kid Cudi &
    Eminem" → "Kid Cudi"), so a collaboration never becomes an artist folder."""
    name = _clean(value)
    parts = credited_artists(name)
    if len(parts) <= 1 or is_known_artist(name):
        return name
    return parts[0]


_FILENAME_NOISE = re.compile(
    r'\s*[\(\[][^\)\]]*\b(?:official|lyrics?|visuali[sz]er|audio|video|hd|hq|4k|explicit)\b[^\)\]]*[\)\]]', re.I)


def title_from_filename(file_path: Optional[str], artist: Optional[str] = '') -> str:
    """A title from the file name, for songs with no title tag: no track
    number, "Artist - " prefix or "(Official Video)"-style noise. '' if
    nothing meaningful is left."""
    if not file_path:
        return ''
    stem = os.path.splitext(os.path.basename(file_path))[0]
    stem = re.sub(r'^\s*\d{1,2}\s*[.\-_]\s+', '', stem)   # "03 - Forever"; not "528 Hz", "99 Problems"
    if artist and ' - ' in stem and _norm_match(stem.split(' - ', 1)[0]) == _norm_match(artist):
        stem = stem.split(' - ', 1)[1]
    stem = re.sub(r'\s+', ' ', _FILENAME_NOISE.sub('', stem)).strip(' -_')
    return stem if title_known(stem) else ''


def _words(value: Optional[str]) -> List[str]:
    return re.findall(r'\w+', _clean(value).casefold())


def name_within(part: Optional[str], whole: Optional[str]) -> bool:
    """Whole-word containment: "John Summit" is within "John Summit & LAVINIA",
    but "Ye" is not within "Kanye West"."""
    part_words, whole_words = _words(part), _words(whole)
    n = len(part_words)
    return bool(n) and any(whole_words[i:i + n] == part_words for i in range(len(whole_words) - n + 1))


def common_artist(names: List[str]) -> str:
    """The artist every service agrees on: the shortest name contained in all
    the others ("John Summit" for "John Summit" and "John Summit & LAVINIA"),
    so the result doesn't depend on which service answered first. Falls back
    to the first name."""
    names = [n for n in names if n]
    for name in sorted(names, key=len):
        if all(name_within(name, other) for other in names):
            return name
    return names[0] if names else ''


def _core_title(value: Optional[str]) -> str:
    """Title without bracketed extras or a trailing feat. credit, so
    "Can't Hold Us (feat. Ray Dalton)" compares equal to "Can't Hold Us"."""
    text = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', _clean(value))
    return re.sub(r'\s(?:feat\.?|ft\.?|featuring)\s.*$', '', text, flags=re.IGNORECASE)


def titles_agree(known: Optional[str], answer: Optional[str]) -> bool:
    return similar(_core_title(known), _core_title(answer)) >= MATCH_THRESHOLD


_CREDIT_SEPARATORS = re.compile(r'\s*(?:,|/|;|&|\+|\s(?:x|and|with|vs\.?|feat\.?|ft\.?|featuring)\s)\s*', re.IGNORECASE)


def credited_artists(value: Optional[str]) -> List[str]:
    """Every artist named in a credit: "Kid Cudi & Eminem" → [Kid Cudi, Eminem]."""
    return [part for part in (p.strip() for p in _CREDIT_SEPARATORS.split(_clean(value))) if part]


def artists_agree(known: Optional[str], answer: Optional[str]) -> bool:
    """Same artist, allowing for a collaboration credited differently: a
    fingerprint answer of "Kid Cudi & Eminem" agrees with a track tagged
    "Kid Cudi", because Kid Cudi is one of the credited artists."""
    if max(
        similar(known, answer),
        similar(known, primary_artist(answer)),
        similar(primary_artist(known), primary_artist(answer)),
    ) >= MATCH_THRESHOLD:
        return True
    return any(similar(k, a) >= MATCH_THRESHOLD
               for k in credited_artists(known) for a in credited_artists(answer))


def merge_answer(meta: Dict[str, str], answer: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """Merge a Lidarr or fingerprint answer into the current metadata.

    Only unknown fields are filled; known ones are kept as they are. The
    answer is trusted only if it agrees with the known title and artist — a
    fingerprint service that disagrees with a correct title is identifying a
    different recording (a cover, a compilation, or simply a wrong match).
    Returns the merged metadata (which may still lack a field), or None if
    the answer disagrees.
    """
    if not answer:
        return None
    if title_known(meta.get('title')) and not titles_agree(meta.get('title'), answer.get('title')):
        return None
    if artist_known(meta.get('artist')) and not artists_agree(meta.get('artist'), answer.get('artist')):
        return None
    answer_album = answer.get('album') if album_known(answer.get('album')) else ''
    answer_title = answer.get('title') if title_known(answer.get('title')) else ''
    return {
        'artist': meta['artist'] if artist_known(meta.get('artist')) else main_artist(answer.get('artist')),
        'album': meta['album'] if album_known(meta.get('album')) else _clean(answer_album),
        'title': meta['title'] if title_known(meta.get('title')) else _clean(answer_title),
    }


def reconcile(meta: Dict[str, str], answer: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """merge_answer(), accepted only if the result is complete."""
    merged = merge_answer(meta, answer)
    return merged if is_complete(merged) else None


# ── Audio fingerprinting ─────────────────────────────────────────────────────

def resolve_with_audio_id(file_path: str, meta: Dict[str, str]) -> Optional[Dict[str, str]]:
    """Identify the song from its audio. Each service's answer (best first)
    is merged into the known fields; a missing album is then asked of Lidarr.
    Returns the first complete result, or None."""
    try:
        from audio_id import identify_track_by_audio_with_alternatives
        answers = identify_track_by_audio_with_alternatives(file_path)
    except Exception as exc:
        logger.debug(f"Fingerprinting failed for {file_path}: {exc}")
        return None
    agreeing = []
    for answer in answers or ():
        merged = merge_answer(meta, answer)
        if merged is None:
            if answer:
                logger.info(
                    f"Fingerprint says {answer.get('artist')} – {answer.get('title')}, which disagrees "
                    f"with the known {track_label(meta)}; not used"
                )
            continue
        agreeing.append((merged, answer))
    if not agreeing:
        return None
    # The first agreeing service fills artist/title; every agreeing service
    # that names the same artist may suggest the album, and pick_album chooses
    # (the first service's album is often a compilation).
    merged = dict(agreeing[0][0])
    if not artist_known(meta.get('artist')):
        merged['artist'] = common_artist([m['artist'] for m, _ in agreeing])
        if album_known(meta.get('album')):
            merged['artist'] = album_owner(meta['album'], merged['artist']) or merged['artist']
    if not album_known(meta.get('album')):
        suggestions = [
            answer.get('album') for _, answer in agreeing
            if artists_agree(merged['artist'], answer.get('artist')) or name_within(merged['artist'], answer.get('artist'))
        ]
        merged['album'] = pick_album(merged['artist'], merged['title'], suggestions) or ''
    return merged if is_complete(merged) else None


FOLDER_NOISE = {'unknown', 'miscellaneous', 'misc', 'singles', 'albums', 'music', 'various artists', 'untitled'}


def folder_hints(file_path: str, meta: Dict[str, str]) -> Optional[Dict[str, str]]:
    """Fill unknown fields from the Artist/Album folders the file sits in.

    Only ever used as a Lidarr search term: folder names come from earlier
    (possibly wrong) tags, so a hint counts only if Lidarr confirms the
    artist + album pair. Returns None when the folders add nothing.
    """
    album_dir = os.path.basename(os.path.dirname(file_path))
    artist_dir = os.path.basename(os.path.dirname(os.path.dirname(file_path)))
    hinted = dict(meta)
    if not artist_known(hinted.get('artist')) and _norm_label(artist_dir) not in FOLDER_NOISE:
        hinted['artist'] = artist_dir
    if not album_known(hinted.get('album')) and _norm_label(album_dir) not in FOLDER_NOISE:
        hinted['album'] = album_dir
    return hinted if hinted != meta else None


def resolve(file_path: str, meta: Dict[str, str]) -> Tuple[Optional[Dict[str, str]], str]:
    """Lidarr first (with folder-name hints), then audio fingerprinting, then
    a MusicBrainz recording of the same length.
    Every answer only fills fields `meta` is missing (see merge_answer).
    Returns (metadata, source)."""
    by_isrc = deezer_track_by_isrc(file_isrc(file_path), audio_duration(file_path))
    if by_isrc and is_complete(by_isrc):
        return {k: by_isrc[k] for k in ('artist', 'album', 'title')}, 'ISRC'
    resolved = reconcile(meta, resolve_with_lidarr(meta))
    if resolved:
        return resolved, 'lidarr'
    hinted = folder_hints(file_path, meta)
    # A hinted artist must be confirmed together with an album, so a folder
    # name can never supply an artist on the strength of an album title alone.
    if hinted and artist_known(hinted['artist']) and album_known(hinted['album']):
        resolved = reconcile(meta, resolve_with_lidarr(hinted))
        if resolved:
            return resolved, 'lidarr (folder names)'
    resolved = resolve_with_audio_id(file_path, meta)
    if resolved:
        return resolved, 'audio fingerprint'
    resolved = reconcile(meta, resolve_with_musicbrainz(file_path, meta))
    if resolved:
        return resolved, 'musicbrainz (length match)'
    return None, ''


# ── MusicBrainz ──────────────────────────────────────────────────────────────

# A recording counts as this file only if their lengths agree this closely;
# the same title by the same artist is often a different recording (live,
# remix, freestyle) of a different length.
DURATION_TOLERANCE_SECONDS = 3


def audio_duration(file_path: str) -> Optional[float]:
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(file_path)
        return float(audio.info.length) if audio and audio.info else None
    except Exception:
        return None


def resolve_with_musicbrainz(file_path: str, meta: Dict[str, str]) -> Optional[Dict[str, str]]:
    """Album for a track whose artist and title are known: the MusicBrainz
    recording with that artist, title and (within a few seconds) length.

    Covers what Lidarr's catalogue and fingerprinting miss — mixtapes, and
    tracks whose fingerprint resolves to a different song.
    """
    if not (artist_known(meta.get('artist')) and title_known(meta.get('title'))) or album_known(meta.get('album')):
        return None
    length = audio_duration(file_path)
    if not length:
        return None
    try:
        import musicbrainzngs
        musicbrainzngs.set_useragent('MusicAI', '1.0', 'https://github.com/Epik-Win/music-managarr')
        recordings = musicbrainzngs.search_recordings(
            recording=meta['title'], artist=meta['artist'], limit=15)['recording-list']
    except Exception as exc:
        logger.debug(f"MusicBrainz lookup failed for {track_label(meta)}: {exc}")
        global _service_errors
        _service_errors += 1
        return None
    def length_gap(rec):
        return abs(int(rec['length']) / 1000 - length) if rec.get('length') else None

    # Closest length first: a song often appears on several releases a second
    # or two apart, and only the nearest is the same recording.
    timed = [r for r in recordings if length_gap(r) is not None and length_gap(r) <= DURATION_TOLERANCE_SECONDS]
    for rec in sorted(timed, key=length_gap):
        if not (titles_agree(meta['title'], rec.get('title'))
                and artists_agree(meta['artist'], rec.get('artist-credit-phrase'))):
            continue
        releases = [r.get('title') for r in rec.get('release-list') or [] if album_known(r.get('title'))]
        album = pick_album(meta['artist'], meta['title'], releases) if releases else ''
        if album:
            return {'artist': meta['artist'], 'album': album, 'title': meta['title']}
    return None


# ── Library scan ─────────────────────────────────────────────────────────────

def find_candidates(section, skip_keys: Set[str], limit: int) -> List[Tuple[object, str]]:
    """(track, reason) for up to `limit` tracks needing their metadata repaired,
    then up to `limit` albums needing artwork, looking only at albums that
    could contain one so a large library stays cheap to scan. Artwork belongs
    to the album, so one track stands for it; and it has its own budget so a
    library of art-less albums can't crowd out the metadata repairs."""
    candidates, artwork = [], []
    for album in section.albums():
        suspect = (
            not album_known(getattr(album, 'title', ''))
            or not artist_known(getattr(album, 'parentTitle', ''))
            or looks_garbled(getattr(album, 'title', ''))
            or looks_garbled(getattr(album, 'parentTitle', ''))
            or not has_own_art(getattr(album, 'thumb', None), getattr(album, 'ratingKey', None))
            or ' - ' in (getattr(album, 'parentTitle', '') or '')
        )
        if not suspect:
            continue
        art_taken = False
        for track in album.tracks():
            if str(getattr(track, 'ratingKey', '')) in skip_keys:
                continue
            reason = needs_repair(track)
            if reason == NO_ARTWORK:
                if not art_taken and len(artwork) < limit:
                    artwork.append((track, reason))
                    art_taken = True
            elif reason and len(candidates) < limit:
                candidates.append((track, reason))
        if len(candidates) >= limit and len(artwork) >= limit:
            break
    return candidates + artwork


def apply_fix(track, file_path: str, meta: Dict[str, str]) -> bool:
    from fix_metadata import _reorganize_file, _write_tags
    if not _write_tags(file_path, meta):
        return False
    new_path = _reorganize_file(file_path, meta)
    art = 'unchecked'
    try:
        art = ensure_artwork(new_path, meta)
        logger.info(f"  artwork: {art}")
    except Exception as exc:
        logger.warning(f"  artwork check failed for {new_path}: {exc}")
    _refresh_in_plex(track)
    # Truthy either way; 'not found' tells the caller the song still needs a cover.
    return art or True


def park_unidentified(track, file_path: str, meta: Dict[str, str], dry_run: bool = False) -> bool:
    """Tag a song nothing could identify as artist "Unidentified" (title and
    album from what is known, else the file name) and move it under that
    artist. Left as "Unknown", Plex files it under some unrelated real
    artist. Under "Unidentified" Plex groups them where the admin can
    see them, and the scan still treats them as unknown. True if parked (or
    already parked)."""
    if os.path.basename(os.path.dirname(os.path.dirname(file_path))) == UNIDENTIFIED:
        return True
    stem = os.path.splitext(os.path.basename(file_path))[0]
    parked = {'artist': UNIDENTIFIED,
              'album': meta.get('album') if album_known(meta.get('album')) else stem,
              'title': meta.get('title') if title_known(meta.get('title')) else stem}
    if dry_run:
        logger.info(f"[dry run] would park as {track_label(parked)}")
        return True
    if apply_fix(track, file_path, parked):
        logger.info(f"  Parked under {UNIDENTIFIED}: {track_label(parked)}")
        return True
    return False


def ensure_artwork(file_path: str, meta: Dict[str, str], dry_run: bool = False) -> str:
    from artwork import ensure_artwork as _ensure
    return _ensure(file_path, meta, dry_run=dry_run)


def _refresh_in_plex(track) -> None:
    from fix_metadata import _refresh_in_plex as _refresh
    _refresh(track)


def repair_library(section, skip_keys: Set[str], limit: int,
                   dry_run: bool = False) -> Tuple[Dict[str, int], List[Dict[str, str]]]:
    """Fix what can be fixed; return (stats, unresolved) where each unresolved
    entry is {'rating_key', 'label', 'reason', 'source'} for the Manual Fix
    playlist. With dry_run, only log what would be written."""
    from fix_metadata import _file_path

    stats = {'found': 0, 'fixed': 0, 'unresolved': 0}
    unresolved = []
    candidates = find_candidates(section, skip_keys, limit)
    stats['found'] = len(candidates)
    logger.info(f"🔎 Library scan: {len(candidates)} track(s) with missing metadata")
    global _service_errors
    for track, reason in candidates:
        file_path = _file_path(track)
        meta = known_meta(track, file_path) if file_path else track_meta(track)
        entry = {
            'rating_key': str(getattr(track, 'ratingKey', '')),
            'label': track_label(meta),
            'reason': reason,
            'source': 'library scan',
        }
        if not file_path:
            # Usually a Plex entry for a file that has just been moved; the
            # rescan after the move replaces it, so it is not the admin's job.
            logger.info(f"  Skipping {entry['label']}: file not on disk (stale Plex entry)")
            continue
        if reason == NO_ARTWORK:
            art = ensure_artwork(file_path, meta, dry_run=dry_run)
            if art == 'not found':
                entry['reason'] = f'{reason}; none found online'
                unresolved.append(entry)
                continue
            if not dry_run:
                _refresh_in_plex(track)
            stats['fixed'] += 1
            logger.info(f"{'[dry run] would fix' if dry_run else '✓ Fixed'} artwork: {entry['label']} — {art}")
            continue
        if reason == ARTIST_HOLDS_TITLE and is_complete(meta):
            # known_meta already took the real artist out of "Artist - Title".
            if dry_run:
                stats['fixed'] += 1
                logger.info(f"[dry run] would split the artist: {entry['label']}")
                continue
            if apply_fix(track, file_path, meta):
                stats['fixed'] += 1
                logger.info(f"✓ Split the artist out of \"Artist - Title\": {track_label(meta)}")
                continue
        if reason == GARBLED:
            # Mojibake is reversible: no lookup needed when the repaired tags
            # are complete. Otherwise look the song up with the repaired names.
            repaired = repair_fields(meta)
            if repaired != meta and is_complete(repaired):
                if dry_run:
                    stats['fixed'] += 1
                    logger.info(f"[dry run] would repair text: {entry['label']} → {track_label(repaired)}")
                    continue
                if apply_fix(track, file_path, repaired):
                    stats['fixed'] += 1
                    logger.info(f"✓ Repaired text: {entry['label']} → {track_label(repaired)}")
                    continue
            meta = repaired
        _service_errors = 0
        resolved, source = resolve(file_path, meta)
        if resolved and dry_run:
            stats['fixed'] += 1
            logger.info(f"[dry run] would fix via {source}: {entry['label']} → {track_label(resolved)}")
        elif resolved and (applied := apply_fix(track, file_path, resolved)):
            stats['fixed'] += 1
            logger.info(f"✓ Fixed via {source}: {entry['label']} → {track_label(resolved)}")
            if applied == 'not found':
                unresolved.append(dict(entry, label=track_label(resolved),
                                       reason='names fixed, but no cover was found on Deezer, iTunes, Spotify or YouTube'))
        elif _service_errors:
            logger.info(f"  {entry['label']}: Lidarr was unavailable during the lookup; retrying next pass")
        else:
            entry['reason'] = f'{reason}; not found by Lidarr or audio fingerprinting'
            unresolved.append(entry)
            if not artist_known(meta.get('artist')):
                park_unidentified(track, file_path, meta, dry_run=dry_run)
    stats['unresolved'] = len(unresolved)
    return stats, unresolved


# ── Albums filed under the wrong artist ──────────────────────────────────────

MISFILED = 'filed under the wrong artist in Plex'


def fingerprint_side(file_path: str, title: str, plex_artist: str, tagged_artist: str) -> Optional[str]:
    """Which artist the audio belongs to: 'tags', 'plex', or None when the
    services don't identify this song or name neither artist."""
    try:
        from audio_id import identify_track_by_audio_with_alternatives
        answers = identify_track_by_audio_with_alternatives(file_path) or []
    except Exception as exc:
        logger.debug(f"Fingerprinting failed for {file_path}: {exc}")
        return None
    for answer in answers:
        if not answer or (title_known(title) and not titles_agree(title, answer.get('title'))):
            continue
        if artists_agree(tagged_artist, answer.get('artist')):
            return 'tags'
        if artists_agree(plex_artist, answer.get('artist')):
            return 'plex'
    return None


def repair_misfiled(section, skip_keys: Set[str], budget: int,
                    dry_run: bool = False) -> Tuple[Dict[str, int], List[Dict[str, str]]]:
    """Albums Plex files under an artist their files don't name (see
    plex_refile). A sink artist's albums, and albums under a combined credit
    whose files name one artist, are re-filed in Plex from their tags;
    a single stray album is fingerprinted (up to `budget` of them) and either
    re-filed (the tags are right) or re-tagged to Plex's artist (Plex is
    right). Songs whose files name no artist (Plex invented one) are looked
    up, and parked under "Unidentified" when nothing knows them. Returns
    (stats, unresolved) like repair_library."""
    import plex_refile
    from fix_metadata import _file_path

    stats = {'misfiled': 0, 'retagged': 0, 'refiled': 0, 'refile_pending': 0, 'parked': 0, 'unresolved': 0}
    unresolved: List[Dict[str, str]] = []
    if section is None:
        return stats, unresolved
    returned = plex_refile.restore_staged()
    if returned:
        logger.warning(f"Re-file: returned {returned} file(s) an interrupted run left in staging")
    found = plex_refile.find_misfiled(section)
    stats['misfiled'] = len(found)
    logger.info(f"🔎 Library scan: {len(found)} album(s) filed under an artist their files don't name")

    global _service_errors
    to_refile = []
    for m in found:
        keys = {str(getattr(t, 'ratingKey', '')) for t in m.tracks}
        if m.verdict in (plex_refile.SINK, plex_refile.COMBINED):
            to_refile.append(m)
            continue
        if keys & skip_keys:
            continue
        if m.verdict == plex_refile.PLACEHOLDER:
            # Plex invented the artist; identify the song from its audio, or
            # park it so Plex stops filing it under a real artist.
            for track in m.tracks:
                if budget <= 0:
                    break
                path = _file_path(track)
                if not path:
                    continue
                budget -= 1
                meta = known_meta(track, path)
                _service_errors = 0
                resolved, source = resolve(path, meta)
                if resolved and (dry_run or apply_fix(track, path, resolved)):
                    stats['retagged'] += 1
                    logger.info(f"{'[dry run] would fix' if dry_run else '✓ Fixed'} via {source}: {m.label} → {track_label(resolved)}")
                    continue
                if park_unidentified(track, path, meta, dry_run=dry_run):
                    stats['parked'] += 1
                unresolved.append({
                    'rating_key': str(getattr(track, 'ratingKey', '')),
                    'label': track_label(meta),
                    'reason': f"artist unknown (Plex showed {m.plex_artist}); not found by Lidarr or audio fingerprinting",
                    'source': 'library scan',
                })
            continue
        if budget <= 0:
            logger.info(f"  {m.label}: left for the next pass (fingerprint budget used up)")
            continue
        budget -= 1
        first = m.tracks[0]
        file_path = _file_path(first)
        side = fingerprint_side(file_path, getattr(first, 'title', ''), m.plex_artist, m.tagged_artist) if file_path else None
        if side == 'tags':
            to_refile.append(m)
        elif side == 'plex':
            for track in m.tracks:
                path = _file_path(track)
                if not path:
                    continue
                meta = {'artist': main_artist(m.plex_artist),
                        'album': _clean(getattr(m.album, 'title', '')) if album_known(getattr(m.album, 'title', '')) else '',
                        'title': _clean(getattr(track, 'title', ''))}
                if not is_complete(meta):
                    meta, _ = resolve(path, meta)
                if not meta:
                    continue
                if dry_run:
                    logger.info(f"[dry run] would re-tag to Plex's artist: {m.label} → {track_label(meta)}")
                    stats['retagged'] += 1
                elif apply_fix(track, path, meta):
                    logger.info(f"✓ Re-tagged to Plex's artist: {m.label} → {track_label(meta)}")
                    stats['retagged'] += 1
        else:
            unresolved.append({
                'rating_key': str(getattr(first, 'ratingKey', '')),
                'label': track_label({'artist': m.tagged_artist, 'album': getattr(m.album, 'title', ''),
                                      'title': getattr(first, 'title', '')}),
                'reason': f"{MISFILED} ({m.plex_artist}); fingerprinting couldn't tell which artist is right",
                'source': 'library scan',
            })

    if to_refile and (dry_run or not plex_refile.enabled()):
        stats['refile_pending'] = len(to_refile)
        why = 'dry run' if dry_run else 'PLEX_REFILE_ENABLED is off'
        for m in to_refile:
            logger.info(f"  would re-file ({why}): {m.label}")
    elif to_refile:
        report = plex_refile.refile(section, to_refile)
        stats['refiled'] = len(report['refiled'])
        for label, artist in report['refiled']:
            logger.info(f"✓ Re-filed {label} → now under {artist}")
        for label, why in report['failed']:
            logger.warning(f"  Re-file failed for {label}: {why}")
        restored = {k: v for k, v in (report.get('restored') or {}).items() if v}
        if restored:
            logger.info("  Kept on re-filed tracks: " + ', '.join(f"{v} {k}" for k, v in restored.items()))
    stats['unresolved'] = len(unresolved)
    return stats, unresolved
