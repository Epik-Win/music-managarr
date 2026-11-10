"""A playlist that is really an album, found on Deezer.

A YouTube playlist can be an album uploaded by a fan: "Final Fantasy IV: Piano
Collection", each video named only by its track ("The Prelude", "Rydia").
Neither the channel nor the titles name the artist, but the playlist's name and
its track list together say which album it is. Deezer has that album
("Piano Collections FINAL FANTASY IV" by Nobuo Uematsu) with an id for each
track, so each song is downloaded as released and filed under its real artist
and album.

An album is accepted only when its name has the playlist's words (in any order,
"Collection" = "Collections") and most of the playlist's songs are on it.

The same goes for one video titled "Album - Track": "Final Fantasy VI Piano
Collections - Terra's Theme" names an album, not an artist (album_for_song).
"""
import logging
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("musicai.album_match")

API = "https://api.deezer.com"
NAME_ALIKE = 0.6        # share of words the album's and the playlist's names have in common
ON_THE_ALBUM = 0.7      # share of the playlist's songs that must be on the album
TITLE_ALIKE = 0.85      # how alike two titles must be to name the same track
SAME_LENGTH = 3         # seconds: "Theme of Love Orchestrated" is "Theme of Love (ensemble)", both 4:06
CLIP = 30               # seconds: a video this short is not the song, whatever it is called
CANDIDATES = 8          # albums looked at, at most
KNOWN_ARTIST_FANS = 50  # a Deezer artist of the exact name with this many fans is a real artist


def _norm(text: str) -> str:
    text = (text or "").casefold()
    try:
        from unidecode import unidecode
        text = unidecode(text).casefold()
    except ImportError:
        pass
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", " ", text)).strip()


def _name_words(name: str) -> set:
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _norm(name).split()} - {"the", "a", "an"}


def name_alike(left: str, right: str) -> float:
    """The share of words two album names have in common, in any order."""
    a, b = _name_words(left), _name_words(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def _shared_prefix(titles: List[str]) -> str:
    """A label most of an album's titles start with ("Piano: The Prelude")."""
    heads = [t.split(":", 1)[0] for t in titles if ":" in t]
    if not heads:
        return ""
    head = max(set(heads), key=heads.count)
    return head + ":" if heads.count(head) >= 0.8 * len(titles) else ""


def _title_alike(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if f" {a} " in f" {b} " or f" {b} " in f" {a} ":
        return 0.9                      # "Main Theme" is "Main Theme of Final Fantasy IV"
    return SequenceMatcher(None, a, b).ratio()


def _pair(song: Dict, track: Dict, prefix: str) -> float:
    """How surely a playlist song is an album track; 0 when it isn't."""
    title = track["title"][len(prefix):] if prefix and track["title"].startswith(prefix) else track["title"]
    alike = _title_alike(song.get("title", ""), title)
    have, want = int(song.get("duration") or 0), int(track.get("duration") or 0)
    if alike >= TITLE_ALIKE:
        # A same-named song of another length is another recording, unless the
        # video is only a clip.
        if have >= CLIP and want and abs(have - want) > max(20, 0.2 * want):
            return 0.0
        return alike
    if have and want and abs(have - want) <= SAME_LENGTH and alike >= 0.5:
        return TITLE_ALIKE
    return 0.0


def match_tracks(songs: List[Dict], tracks: List[Dict]) -> Dict[int, Dict]:
    """Which album track each playlist song is: {song index: track}, each
    track used once, the surest pairs first."""
    prefix = _shared_prefix([t["title"] for t in tracks])
    pairs = sorted(((_pair(s, t, prefix), -abs(i - j), i, j)
                    for i, s in enumerate(songs) for j, t in enumerate(tracks)), reverse=True)
    matched, used = {}, set()
    for score, _near, i, j in pairs:
        if score <= 0:
            break
        if i not in matched and j not in used:
            matched[i] = tracks[j]
            used.add(j)
    return matched


def _get(path: str, **params):
    response = requests.get(f"{API}/{path}", params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data


def _album(album_id) -> Dict:
    album = _get(f"album/{album_id}")
    tracks = album.get("tracks", {}).get("data", [])
    if len(tracks) < int(album.get("nb_tracks") or 0):
        tracks = _get(f"album/{album_id}/tracks", limit=500).get("data", [])
    return {"id": album["id"], "album": album["title"], "artist": album["artist"]["name"],
            "tracks": [{"id": t["id"], "title": t["title"], "duration": int(t.get("duration") or 0),
                        "position": n} for n, t in enumerate(tracks, 1)]}


def find_album(name: str, songs: List[Dict]) -> Optional[Dict]:
    """The Deezer album a playlist is, or None.

    `songs` are the playlist's entries ({'title', 'duration'}). Returns the
    album's 'artist', 'album' and 'id', 'tracks' ({song index: track}) and
    'coverage', the share of the album's tracks the playlist holds."""
    if not name or not songs:
        return None
    seen, candidates = set(), []
    for query in dict.fromkeys((name, _norm(name))):
        try:
            found = _get("search/album", q=query).get("data", [])
        except Exception as exc:
            logger.info(f"📀 Deezer album search failed for '{query}': {exc}")
            return None
        for item in found:
            if item["id"] not in seen and name_alike(item["title"], name) >= NAME_ALIKE:
                seen.add(item["id"])
                candidates.append(item["id"])
    best = None
    for album_id in candidates[:CANDIDATES]:
        try:
            album = _album(album_id)
        except Exception as exc:
            logger.debug(f"Could not read Deezer album {album_id}: {exc}")
            continue
        matched = match_tracks(songs, album["tracks"])
        rank = (len(matched), name_alike(album["album"], name))
        if len(matched) >= ON_THE_ALBUM * len(songs) and (best is None or rank > best[0]):
            best = (rank, album, matched)
    if best is None:
        logger.info(f"📀 '{name}' is not an album Deezer has")
        return None
    _rank, album, matched = best
    total = len(album["tracks"])
    album.update(tracks=matched, coverage=len(matched) / max(1, total))
    logger.info(f"📀 '{name}' is {album['artist']} – {album['album']} on Deezer: "
                f"{len(matched)} of the playlist's {len(songs)} songs, {total} on the album")
    return album


def _is_artist(name: str) -> bool:
    """A Deezer artist of exactly this name, with a following: "Blue" is a
    band, whatever Joni Mitchell called an album."""
    key = re.sub(r"[^\w]+", "", (name or "").casefold())
    try:
        found = _get("search/artist", q=name, limit=5).get("data", [])
    except Exception as exc:
        logger.debug(f"Deezer artist lookup failed for {name!r}: {exc}")
        return False
    return any(re.sub(r"[^\w]+", "", a.get("name", "").casefold()) == key
               and (a.get("nb_fan") or 0) >= KNOWN_ARTIST_FANS for a in found)


def _song_on(tracks: List[Dict], title: str, duration: int) -> Optional[Dict]:
    """The album's track a song is: by its title, else the only track as long
    as the video to the second ("Terra's Theme" is "Piano: Tina" in Japan's
    naming, both 3:37)."""
    matched = match_tracks([{"title": title, "duration": duration}], tracks)
    if matched:
        return matched[0]
    near = [t for t in tracks if duration and t["duration"] and abs(t["duration"] - int(duration)) <= 1]
    return near[0] if len(near) == 1 else None


def album_for_song(name: str, title: str, duration: int = 0) -> Optional[Dict]:
    """When a video's "artist" is really an album: the album's 'artist',
    'album', 'id' and 'track' (the album's track this song is, or None when
    none is). Only an album named exactly that (any word order), by another
    artist, and only when no real artist goes by that name."""
    if not name or not title or _is_artist(name):
        return None
    try:
        found = _get("search/album", q=name).get("data", [])
    except Exception as exc:
        logger.info(f"📀 Deezer album search failed for '{name}': {exc}")
        return None
    best = None
    for item in [a for a in found if name_alike(a["title"], name) == 1.0][:CANDIDATES]:
        try:
            album = _album(item["id"])
        except Exception as exc:
            logger.debug(f"Could not read Deezer album {item['id']}: {exc}")
            continue
        if _norm(album["artist"]) == _norm(name):
            continue                    # a self-titled album: the name is its artist's
        track = _song_on(album["tracks"], title, int(duration or 0))
        if track:
            best = (album, track)
            break
        best = best or (album, None)
    if best is None:
        return None
    album, track = best
    logger.info(f"📀 '{name}' is the album {album['artist']} – {album['album']}; '{title}' is "
                + (f"its track '{track['title']}'" if track else "none of its tracks"))
    return {"artist": album["artist"], "album": album["album"], "id": album["id"], "track": track}
