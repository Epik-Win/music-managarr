"""Spotify access through the public web pages (spotify_scraper).

Spotify's Client Credentials API requires a Premium account for search and
browse endpoints, so nothing here uses it. Everything below reads the same
public pages a browser would, which covers track/playlist metadata, the
editorial charts, and a keyword search.
"""
import logging
import os
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('musicai.app')

# Spotify-curated playlists whose IDs are stable and public. Used as the
# "trending" and "popular playlists" discovery sources.
EDITORIAL_PLAYLISTS: Dict[str, List[Tuple[str, str]]] = {
    'trending': [
        ("Today's Top Hits", '37i9dQZF1DXcBWIGoYBM5M'),
        ('Viral 50 - Global', '37i9dQZEVXbLiRSasKsNU9'),
        ('New Music Friday', '37i9dQZF1DX4JAvHpjipBk'),
    ],
    'popular': [
        ('RapCaviar', '37i9dQZF1DX0XUsuxWHRQd'),
        ('Rock This', '37i9dQZF1DXcF6B6QPhFDv'),
        ('Hot Country', '37i9dQZF1DX1lVhptIYRda'),
        ('mint', '37i9dQZF1DX4dyzvuaRJ0n'),
        ('Pop Rising', '37i9dQZF1DWUa8ZRTfalHk'),
        ('All Out 2010s', '37i9dQZF1DX5Ejj0EkURtP'),
        ('Beast Mode', '37i9dQZF1DX76Wlfdnj7AP'),
        ('Chill Hits', '37i9dQZF1DX4WYpdgoIcn6'),
        ('Are & Be', '37i9dQZF1DX4SBhb3fqCJd'),
    ],
}

_client = None


def get_client():
    global _client
    if _client is None:
        from spotify_scraper import SpotifyClient
        _client = SpotifyClient()
    return _client


def _track_to_dict(track) -> Optional[Dict[str, str]]:
    """Flatten a scraper Track model into the plain dict the pipeline uses."""
    if track is None:
        return None
    name = (getattr(track, 'name', '') or '').strip()
    artists = [a.name for a in (getattr(track, 'artists', None) or []) if getattr(a, 'name', None)]
    if not name or not artists:
        return None
    album = getattr(track, 'album', None)
    cover_url = None
    images = getattr(album, 'images', None) or []
    if images:
        cover_url = getattr(images[0], 'url', None) or (images[0].get('url') if isinstance(images[0], dict) else None)
    return {
        'artist': ', '.join(artists),
        'title': name,
        'album': (getattr(album, 'name', '') or '').strip(),
        'cover_url': cover_url,
        'url': getattr(track, 'url', None) or getattr(track, 'share_url', None),
        'id': getattr(track, 'id', None),
    }


def get_track(url: str) -> Optional[Dict[str, str]]:
    try:
        return _track_to_dict(get_client().get_track(url))
    except Exception as exc:
        logger.debug(f"spotify_web.get_track failed for {url}: {exc}")
        return None


def get_playlist(url_or_id: str) -> Tuple[str, List[Dict[str, str]]]:
    """Return (playlist_name, tracks) for a public playlist URL or ID."""
    url = url_or_id if url_or_id.startswith('http') else f'https://open.spotify.com/playlist/{url_or_id}'
    try:
        playlist = get_client().get_playlist(url)
    except Exception as exc:
        logger.debug(f"spotify_web.get_playlist failed for {url}: {exc}")
        return '', []
    tracks = []
    for item in getattr(playlist, 'tracks', None) or []:
        track = _track_to_dict(getattr(item, 'track', item))
        if track:
            tracks.append(track)
    return (getattr(playlist, 'name', '') or '').strip(), tracks


def get_album(url_or_id: str) -> Tuple[str, str, List[Dict[str, str]]]:
    """Return (album_name, artist_name, tracks) for a public album URL or ID.

    Track objects returned by get_album() have their own `.artists` but a
    None `.album` (the API doesn't redundantly nest the parent album inside
    each track), so this fills in the album name explicitly per track rather
    than reusing `_track_to_dict` (which would read an empty album name off
    `track.album`).
    """
    url = url_or_id if url_or_id.startswith('http') else f'https://open.spotify.com/album/{url_or_id}'
    try:
        album = get_client().get_album(url)
    except Exception as exc:
        logger.debug(f"spotify_web.get_album failed for {url}: {exc}")
        return '', '', []
    if not album:
        return '', '', []
    album_name = (getattr(album, 'name', '') or '').strip()
    artist_name = ', '.join(a.name for a in (getattr(album, 'artists', None) or []) if getattr(a, 'name', None))
    tracks = []
    for track in getattr(album, 'tracks', None) or []:
        title = (getattr(track, 'name', '') or '').strip()
        track_artists = [a.name for a in (getattr(track, 'artists', None) or []) if getattr(a, 'name', None)]
        track_artist = ', '.join(track_artists) if track_artists else artist_name
        if title and track_artist:
            tracks.append({'artist': track_artist, 'title': title, 'album': album_name})
    return album_name, artist_name, tracks


def get_chart(key: str) -> Tuple[str, List[Dict[str, str]]]:
    """Return (chart_name, tracks) for one of the scraper's known chart keys."""
    try:
        chart = get_client().get_chart(key)
    except Exception as exc:
        logger.debug(f"spotify_web.get_chart failed for {key}: {exc}")
        return '', []
    tracks = []
    for item in getattr(chart, 'tracks', None) or []:
        track = _track_to_dict(getattr(item, 'track', item))
        if track:
            tracks.append(track)
    return (getattr(chart, 'name', '') or '').strip(), tracks


def search_track(artist: str, title: str) -> Optional[Dict[str, str]]:
    """Best track match for an artist/title pair, or None."""
    query = f"{artist} {title}".strip()
    if not query:
        return None
    try:
        results = get_client().search(query)
    except Exception as exc:
        logger.debug(f"spotify_web.search failed for {query!r}: {exc}")
        return None
    for track in getattr(results, 'tracks', None) or []:
        flat = _track_to_dict(track)
        if flat:
            return flat
    return None


def editorial_playlist_ids(category: str) -> List[Tuple[str, str]]:
    """Playlists for a category, overridable via SPOTIFY_<CATEGORY>_PLAYLIST_IDS.

    The override is a comma-separated list of playlist IDs; names are filled
    in from the playlist page when fetched.
    """
    env_key = f'SPOTIFY_{category.upper()}_PLAYLIST_IDS'
    override = os.environ.get(env_key, '').strip()
    if override:
        return [('', pid.strip()) for pid in override.split(',') if pid.strip()]
    return EDITORIAL_PLAYLISTS.get(category, [])
