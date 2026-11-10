# --- Utility: Extract URLs from text ---
import re
import logging
import asyncio
import functools
import json
import os
import requests
import subprocess
from typing import Optional, Dict, Any, Tuple

import geo_relay
from naming import artist_from_channel, song_in_quotes

# Initialize logger at the module level BEFORE defining any functions that use it
logger = logging.getLogger(__name__)


def normalize_youtube_url(url: str) -> Optional[str]:
    """
    Normalize YouTube URLs to standard format.
    Handles:
    - YouTube Shorts: youtube.com/shorts/VIDEO_ID → youtube.com/watch?v=VIDEO_ID
    - Non-standard formats: youtube.com/source/VIDEO_ID/shorts → youtube.com/watch?v=VIDEO_ID
    - Playlist URLs
    - Standard watch URLs
    
    Returns:
        Normalized URL or None if format is unrecognizable
    """
    if not url or not isinstance(url, str):
        return None
    
    url = url.strip()
    
    # Extract video ID from various YouTube URL formats
    video_id = None
    is_playlist = False
    playlist_id = None
    
    # Check for playlist
    if 'list=' in url:
        is_playlist = True
        # Extract playlist ID from list= parameter
        match = re.search(r'[?&]list=([\w-]+)', url)
        if match:
            playlist_id = match.group(1)
            # Keep as playlist URL
            normalized = f"https://www.youtube.com/playlist?list={playlist_id}"
            logger.info(f"🔄 YouTube URL normalized (playlist): {url[:60]}... → {normalized}")
            return normalized
    
    # Standard watch URL with v= parameter
    match = re.search(r'[?&]v=([\w-]{11})', url)
    if match:
        logger.debug(f"🔄 YouTube URL already standard: {url[:60]}...")
        return url  # Already in standard format
    
    # youtu.be short URL
    match = re.search(r'youtu\.be/([\w-]{11})', url)
    if match:
        video_id = match.group(1)
    
    # Shorts format: youtube.com/shorts/VIDEO_ID
    if not video_id:
        match = re.search(r'youtube\.com/shorts/([\w-]{11})', url)
        if match:
            video_id = match.group(1)
    
    # Non-standard format: youtube.com/source/VIDEO_ID/shorts
    if not video_id:
        match = re.search(r'youtube\.com/(?:source|v)/([\w-]{11})', url)
        if match:
            video_id = match.group(1)
            logger.info(f"🔄 Detected non-standard format: /source/VIDEO_ID")
    
    # Fallback: try to find any 11-character alphanumeric string that looks like a video ID
    if not video_id:
        match = re.search(r'(?:youtube\.com|youtu\.be).*?([\w-]{11})', url)
        if match:
            video_id = match.group(1)
            logger.debug(f"🔄 Fallback regex matched video ID")
    
    if video_id:
        # Validate video ID format (11 chars, alphanumeric plus dash/underscore)
        if re.match(r'^[\w-]{11}$', video_id):
            normalized = f"https://www.youtube.com/watch?v={video_id}"
            logger.info(f"✅ YouTube URL normalized: {url[:60]}... → {normalized}")
            return normalized
    
    logger.warning(f"Could not extract video ID from YouTube URL: {url}")
    return None

async def extract_spotify_metadata_web(url: str) -> Optional[Dict[str, Any]]:
    """Extract Spotify track or playlist metadata from the public web page."""
    try:
        import spotify_web
        if '/track/' in url:
            track = await asyncio.to_thread(spotify_web.get_track, url)
            if track:
                artist = parse_primary_artist(track['artist'])
                title = clean_track_title(track['title'])
                if artist and title:
                    return {
                        'artist': artist,
                        'title': title,
                        'album': track.get('album', ''),
                        'source': 'spotify',
                        'url': url,
                    }
        elif '/playlist/' in url:
            playlist_name, tracks = await asyncio.to_thread(spotify_web.get_playlist, url)
            if tracks:
                return {
                    'type': 'playlist',
                    'name': playlist_name or 'Spotify Playlist',
                    'tracks': [
                        {'artist': t['artist'], 'title': t['title'], 'album': t.get('album', '')}
                        for t in tracks
                    ],
                    'source': 'spotify',
                    'url': url,
                }
        elif '/album/' in url:
            album_name, artist_name, tracks = await asyncio.to_thread(spotify_web.get_album, url)
            if tracks:
                # Reuse the generic 'playlist' shape — downstream handling
                # (discord_bot.py) already treats any multi-track result as a
                # candidate whole-album download via Lidarr before falling
                # back to per-track downloads, which is exactly right for an
                # actual album.
                display_name = f"{artist_name} - {album_name}".strip(' -') or 'Spotify Album'
                return {
                    'type': 'playlist',
                    'name': display_name,
                    'tracks': tracks,
                    'source': 'spotify',
                    'url': url,
                    # This came from an actual Spotify /album/ URL, not a
                    # playlist that merely happens to line up with an album —
                    # callers can skip the fuzzy playlist_matches_own_album()
                    # heuristic entirely and just trust this directly.
                    'is_album': True,
                    'album_name': album_name,
                }
        return None
    except Exception as e:
        logger.error(f"Spotify extraction failed: {e}")
        return None

async def extract_tiktok_metadata(url: str) -> Optional[Dict[str, Any]]:
    """
    Async: Extract TikTok song metadata from TikTok page or APIs.
    Tries TikTok page embedded JSON first, then TikTok item detail endpoint, then Spotify link fallback.
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        def _clean_tiktok_title(title: str, artist: str) -> str:
            title = title.strip()
            # Remove music note prefix
            title = re.sub(r'^♬\s*', '', title)
            # Normalize separator if the right side is the artist
            if ' - ' in title and artist:
                left, right = [segment.strip() for segment in title.rsplit(' - ', 1)]
                if artist.lower() in right.lower() or right.lower() in artist.lower():
                    title = left
            # Replace hyphenated words with spaces for badly formatted titles
            if '-' in title and ' ' not in title:
                title = title.replace('-', ' ')
            return title.strip()

        def _find_music_object(obj):
            if isinstance(obj, dict):
                title = obj.get('title') or obj.get('name')
                artist = obj.get('authorName') or obj.get('artistName')
                if isinstance(title, str) and isinstance(artist, str) and title.strip() and artist.strip():
                    return obj
                for value in obj.values():
                    found = _find_music_object(value)
                    if found:
                        return found
            elif isinstance(obj, list):
                for item in obj:
                    found = _find_music_object(item)
                    if found:
                        return found
            return None

        def _extract_universal_tiktok_json(html_text: str):
            match = re.search(r"<script[^>]+id=['\"]__UNIVERSAL_DATA_FOR_REHYDRATION__['\"][^>]*>(.*?)</script>", html_text, re.S | re.I)
            if not match:
                return None
            try:
                return json.loads(match.group(1))
            except Exception:
                return None

        # Resolve short URLs and final page
        final_resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        final_url = final_resp.url
        html = final_resp.text if final_resp.status_code == 200 else ''

        # Attempt Spotify Add link extraction first from HTML
        spotify_match = None
        if html:
            spotify_match = re.search(r'https?://open\.spotify\.com/track/[A-Za-z0-9]+', html)
            if not spotify_match:
                spotify_match = re.search(r'https?://open\.spotify\.com/album/[A-Za-z0-9]+', html)

        if spotify_match:
            spotify_url = spotify_match.group(0)
            spotify_meta = await extract_spotify_metadata_web(spotify_url)
            if spotify_meta and spotify_meta.get('artist') and spotify_meta.get('title'):
                spotify_meta['source'] = 'tiktok'
                spotify_meta['url'] = final_url
                return spotify_meta

        # Step 1: parse embedded JSON from universal data script
        data = None
        if html:
            data = _extract_universal_tiktok_json(html)

        if data:
            music_info = _find_music_object(data)
            if isinstance(music_info, dict):
                title = (music_info.get('title') or music_info.get('name') or '').strip()
                artist = (music_info.get('authorName') or music_info.get('artistName') or '').strip()
                album = (music_info.get('album') or '').strip() if isinstance(music_info.get('album'), str) else ''
                if title and artist:
                    title = _clean_tiktok_title(title, artist)
                    return {
                        'artist': parse_primary_artist(artist),
                        'title': title,
                        'album': album,
                        'source': 'tiktok',
                        'url': final_url,
                        'music_id': music_info.get('id')
                    }

        # Step 2: if still missing, try clip ID extraction from final URL
        clip_match = re.search(r'/video/(\d+)', final_url)
        if not clip_match:
            clip_match = re.search(r'aweme_id=(\d+)', final_url)

        if clip_match:
            clip_id = clip_match.group(1)
            detail_url = f'https://www.tiktok.com/api/item/detail/?itemId={clip_id}'
            try:
                detail_resp = requests.get(detail_url, headers=headers, timeout=20)
                if detail_resp.status_code == 200 and detail_resp.text:
                    detail_json = detail_resp.json()
                    music_info = detail_json.get('itemInfo', {}).get('itemStruct', {}).get('music')
                    if isinstance(music_info, dict):
                        title = (music_info.get('title') or music_info.get('name') or '').strip()
                        artist = (music_info.get('authorName') or music_info.get('artistName') or '').strip()
                        album = (music_info.get('album') or '').strip() if isinstance(music_info.get('album'), str) else ''
                        if title and artist:
                            title = _clean_tiktok_title(title, artist)
                            return {
                                'artist': parse_primary_artist(artist),
                                'title': title,
                                'album': album,
                                'source': 'tiktok',
                                'url': final_url,
                                'music_id': music_info.get('id')
                            }
            except Exception:
                logger.debug("TikTok item detail API fallback failed")

        # Step 3: oEmbed fallback
        try:
            oembed_url = f'https://www.tiktok.com/oembed?url={final_url}'
            oembed_resp = requests.get(oembed_url, headers=headers, timeout=20)
            if oembed_resp.status_code == 200 and oembed_resp.headers.get('content-type', '').startswith('application/json'):
                oembed_data = oembed_resp.json()
                title = oembed_data.get('title', '').strip()
                author = oembed_data.get('author_name', '').strip()
                if title and author:
                    title = _clean_tiktok_title(title, author)
                    return {
                        'artist': parse_primary_artist(author),
                        'title': title,
                        'album': '',
                        'source': 'tiktok',
                        'url': final_url
                    }
        except Exception:
            logger.debug("TikTok oEmbed fallback failed")

        logger.warning(f"⚠️ TikTok metadata extraction failed for URL: {url}")
        return None

    except Exception as e:
        logger.error(f"❌ TikTok metadata extraction failed: {e}")
        return None

async def extract_instagram_metadata(url: str) -> Optional[Dict[str, Any]]:
    """
    Extract metadata from Instagram links, including reel audio pages.
    Note: Instagram requires authentication for audio metadata, so this is a basic implementation.
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        if resp.status_code != 200 or not resp.text:
            logger.warning(f"⚠️ Instagram page fetch failed ({resp.status_code}) for: {url}")
            return None

        html_text = resp.text
        
        # Try to extract from Open Graph tags
        og_title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.I)
        if og_title_match:
            title = og_title_match.group(1).strip()
        else:
            title = 'Instagram Audio'  # Fallback title

        import html as _html
        title = _html.unescape(title)
        title = clean_track_title(title)

        if not title:
            logger.warning(f"Could not extract valid Instagram metadata from: {url}")
            return None

        logger.info(f"Instagram metadata extracted: <unknown artist> - {title}")
        return {
            'artist': '',
            'title': title,
            'album': '',
            'source': 'instagram',
            'url': url
        }
    except Exception as e:
        logger.error(f"❌ Instagram metadata extraction failed: {e}")
        return None

def build_request_context_text(message_text: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, artist: Optional[str] = None, title: Optional[str] = None, album: Optional[str] = None) -> str:
    """Build a compact, structured request-context string for the downloader and AI."""
    meta = metadata or {}
    ctx_parts = []

    source_name = (meta.get('source') or '').strip()
    if source_name:
        ctx_parts.append(f"source={source_name}")

    extracted_artist = (meta.get('artist') or artist or '').strip()
    extracted_title = (meta.get('title') or title or '').strip()
    if extracted_artist and extracted_title:
        ctx_parts.append(f"requested_track={extracted_artist} - {extracted_title}")

    extracted_album = (meta.get('album') or album or '').strip()
    if extracted_album:
        ctx_parts.append(f"album={extracted_album}")

    if message_text:
        cleaned_message = re.sub(r'\s+', ' ', str(message_text)).strip()
        if cleaned_message:
            ctx_parts.append(f"message={cleaned_message}")

    url = (meta.get('url') or '').strip()
    if url:
        ctx_parts.append(f"url={url}")

    raw_video_title = (meta.get('video_title') or meta.get('raw_title') or '').strip()
    if raw_video_title:
        ctx_parts.append(f"video_title={raw_video_title}")

    raw_video_uploader = (meta.get('video_uploader') or '').strip()
    if raw_video_uploader:
        ctx_parts.append(f"video_uploader={raw_video_uploader}")

    if meta.get('video_duration'):
        ctx_parts.append(f"video_duration={int(meta['video_duration'])}s")

    variant_hint_sources = ' '.join(
        str(value or '') for value in [
            message_text,
            raw_video_title,
            raw_video_uploader,
            extracted_title,
            extracted_artist,
            extracted_album,
        ]
    )
    if re.search(
        r'\b(?:arabic|live|acoustic|remix|explicit|clean|instrumental|deluxe|radio edit|re-record(?:ing)?|translated|translation|alternate|cover|mix|version|variant)\b',
        variant_hint_sources,
        flags=re.IGNORECASE,
    ):
        ctx_parts.append("prefer the exact release/variant implied by the request if multiple versions exist")
    description = re.sub(r'\s+', ' ', str(meta.get('video_description') or '')).replace(';', ',').strip()
    if description:
        ctx_parts.append(f"video_description={description[:300]}")    # last: free text
    return '; '.join(ctx_parts)


def _build_variant_context(metadata: Optional[Dict[str, Any]], url: str, source: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Attach a compact variant note that helps the downloader disambiguate releases."""
    if not isinstance(metadata, dict):
        return metadata
    if metadata.get('variant_context') or metadata.get('request_context'):
        return metadata

    note = build_request_context_text(
        metadata={
            'artist': metadata.get('artist'),
            'title': metadata.get('title'),
            'album': metadata.get('album'),
            'source': metadata.get('source') or source or 'unknown',
            'url': url,
            'video_title': metadata.get('video_title') or metadata.get('raw_title'),
            'video_uploader': metadata.get('video_uploader'),
            'video_duration': metadata.get('video_duration'),
            'video_description': metadata.get('video_description'),
        },
        artist=metadata.get('artist'),
        title=metadata.get('title'),
        album=metadata.get('album'),
    )
    metadata['variant_context'] = note
    metadata['request_context'] = note
    return metadata


def _find_instagram_audio_data(data):
    """Helper function to find audio data in Instagram JSON"""
    if isinstance(data, dict):
        if 'audio' in data and isinstance(data['audio'], dict):
            return data['audio']
        for value in data.values():
            result = _find_instagram_audio_data(value)
            if result:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _find_instagram_audio_data(item)
            if result:
                return result
    return None

async def extract_link_metadata(url: str, message_text: str = '') -> Optional[Dict[str, any]]:
    """
    Async: Dispatch to the correct extractor based on the URL.
    Returns standardized metadata dict or None if extraction fails.
    """
    if not url or not isinstance(url, str):
        logger.warning("extract_link_metadata: Invalid or empty URL")
        return None
    url = url.strip()
    
    # Normalize YouTube URLs to handle shorts, redirects, and non-standard formats
    if 'youtube.com' in url or 'youtu.be' in url:
        url = normalize_youtube_url(url)
        if not url:
            logger.warning("Failed to normalize YouTube URL")
            return None
    
    try:
        if 'youtube.com' in url or 'youtu.be' in url:
            # yt-dlp is sync, so run in thread executor with timeout
            loop = asyncio.get_running_loop()
            # Add timeout of 60 seconds for YouTube extraction
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, functools.partial(extract_youtube_metadata, url)),
                    timeout=60.0
                )
                return _build_variant_context(result, url, 'youtube')
            except asyncio.TimeoutError:
                logger.error(f"❌ YouTube extraction timeout (60s) for: {url}")
                return None
        elif 'tiktok.com' in url or 'vt.tiktok.com' in url:
            # Add timeout for TikTok extraction
            try:
                result = await asyncio.wait_for(extract_tiktok_metadata(url), timeout=60.0)
                return _build_variant_context(result, url, 'tiktok')
            except asyncio.TimeoutError:
                logger.error(f"❌ TikTok extraction timeout (60s) for: {url}")
                return None
        elif 'instagram.com' in url or 'instagr.am' in url:
            # Instagram direct metadata extraction
            try:
                result = await asyncio.wait_for(extract_instagram_metadata(url), timeout=60.0)
                return _build_variant_context(result, url, 'instagram')
            except asyncio.TimeoutError:
                logger.error(f"❌ Instagram extraction timeout (60s) for: {url}")
                return None
        elif 'shazam.com' in url:
            # shazam is sync, so run in thread executor with timeout
            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, functools.partial(extract_shazam_metadata, url, message_text)),
                    timeout=30.0
                )
                return _build_variant_context(result, url, 'shazam')
            except asyncio.TimeoutError:
                logger.error(f"❌ Shazam extraction timeout (30s) for: {url}")
                return None
        elif (
            'open.spotify.com/track/' in url
            or 'open.spotify.com/playlist/' in url
            or 'open.spotify.com/album/' in url
        ):
            # Add timeout for Spotify extraction
            try:
                result = await asyncio.wait_for(extract_spotify_metadata_web(url), timeout=30.0)
                return _build_variant_context(result, url, 'spotify')
            except asyncio.TimeoutError:
                logger.error(f"❌ Spotify extraction timeout (30s) for: {url}")
                return None
        else:
            logger.warning(f"extract_link_metadata: Unsupported URL: {url}")
            return None
    except asyncio.TimeoutError:
        # Already handled above, but catch any missed cases
        logger.error(f"❌ Extraction timeout for: {url}")
        return None
    except Exception as e:
        logger.error(f"extract_link_metadata: Exception for {url}: {e}")
        return None


# ── SongNameSplit integration ──────────────────────────────────────────────
# Used as a targeted fallback when the YouTube uploader is a fan/lyric channel
# (not the real artist) and regex parsing can't reliably separate artist/title.
# Install: pip install SongNameSplit --break-system-packages
try:
    import SongNameSplit as _SNS
    _SNS_AVAILABLE = True
    logger.debug('SongNameSplit available')
except ImportError:
    _SNS = None
    _SNS_AVAILABLE = False

# Patterns that indicate an uploader is NOT the real artist
_FAN_CHANNEL_RE = re.compile(
    r'(?:'
    r'\d{3,}'           # 3+ consecutive digits (fan account numbers)
    r'|lyrics?'          # lyrics channels
    r'|lyric\s*video'
    r'|official\s*lyrics'
    r'|fan\s*(?:channel|made|video|art|edit|lyric)'
    r'|fan\d*$'
    r'|AMV'
    r'|karaoke'
    r'|cover\s*(?:by|channel)'
    r'|reaction'
    r'|nightcore'
    r')',
    re.IGNORECASE,
)


def _looks_like_fan_channel(uploader: str) -> bool:
    """Return True when the channel name looks like a fan/lyric/reaction account."""
    return bool(uploader and _FAN_CHANNEL_RE.search(uploader))


# Many non-English (notably Russian/Ukrainian) music video titles use an
# em-dash "—" or en-dash "–" as the "Artist — Title" separator instead of a
# plain hyphen, e.g. "Rauf & Faik — детство". Every dash-based split/detect
# in this module only looks for a literal " - ", so those titles fall through
# unsplit and the artist name ends up baked into the title. Detect either
# dash style, and normalize to " - " before handing text to a plain-hyphen
# splitter or to SongNameSplit (which itself only recognizes ASCII hyphens
# and raises NonStandardSongTitle on em/en-dash input).
_DASH_SEPARATOR_RE = re.compile(r'\s[-–—]\s')


def _has_dash_separator(text: str) -> bool:
    return bool(text and _DASH_SEPARATOR_RE.search(text))


def _names_roughly_match(a: str, b: str, threshold: float = 0.82) -> bool:
    """Fuzzy-compare two names after stripping punctuation/case — used to check
    whether a video's uploader/channel name is really one half of its title
    split apart by a dash (a strong, cheap signal for which side is the artist)."""
    import difflib
    a_norm = re.sub(r'[^a-z0-9]', '', (a or '').lower())
    b_norm = re.sub(r'[^a-z0-9]', '', (b or '').lower())
    if not a_norm or not b_norm:
        return False
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio() >= threshold



def _normalize_dash_separators(text: str) -> str:
    return _DASH_SEPARATOR_RE.sub(' - ', text) if text else text


def _try_songnamesplit(raw_title: str, timeout: int = 8):
    """Call SongNameSplit.namesplit() with a hard timeout.

    Returns {'artist': str, 'songname': str} on success, None on any failure.
    """
    if not _SNS_AVAILABLE:
        return None
    raw_title = _normalize_dash_separators(raw_title)
    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
            _fut = _pool.submit(_SNS.namesplit, raw_title, True)
            result = _fut.result(timeout=timeout)
        if result and result.get('artist') and result.get('songname'):
            return result
    except concurrent.futures.TimeoutError:
        logger.debug(f'[SNS] Timed out after {timeout}s for: {raw_title!r}')
    except Exception as _e:
        logger.debug(f'[SNS] Failed for {raw_title!r}: {_e}')
    return None


def is_music_video(video_data: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Determine if a YouTube video is music-related.

    Design principle: **accept by default**.  Users post YouTube links to
    request music — the overwhelming majority of links submitted to the bot
    are songs.  This function only rejects when there is *clear, unambiguous*
    evidence the video is NOT music (vlogs, gameplay, tutorials, podcasts).

    Tier 1 signals (fast-accept, checked first):
        1. categoryId == "10" (Music)
        2. Official Artist Channel / VEVO
        3. Title matches "Artist - Title" pattern or has Official/Lyric/feat tags

    Tier 2 signals (soft-accept):
        4. Streaming links in description
        5. Duration 1–8 minutes (generous range covering intros, extended mixes)

    Reject only when:
        - A non-music keyword is found in the title AND
        - No Tier 1 signal is present

    Returns:
        (is_music: bool, reason: str)
    """
    title       = (video_data.get('title')       or '').strip()
    description = (video_data.get('description') or '').strip().lower()
    category_id = str(video_data.get('category_id') or '')
    uploader    = (video_data.get('uploader')    or '').strip().lower()
    duration    = video_data.get('duration') or 0

    title_lower = title.lower()

    # ── Tier 1: fast-accept ──────────────────────────────────────────────────

    if category_id == '10':
        return True, "Tier 1: Music category (ID 10)"

    if any(p in uploader for p in ('vevo', 'official', 'music')):
        return True, f"Tier 1: Official/VEVO channel ({uploader})"

    music_title_patterns = [
        r'\w+\s*[-–—]\s*\w+',                          # "Artist - Title"
        r'\(official\s+(?:video|audio|lyric|music)\)',  # (Official Video/Audio)
        r'\[official\s+(?:video|audio|lyric|music)\]',
        r'\((?:lyric|visualizer|audio)\)',
        r'\b(?:feat|ft)\b\.?\s+\w',                     # feat. / ft.
        r'\bprod\.?\s+by\b',                            # prod. by
    ]
    for pat in music_title_patterns:
        if re.search(pat, title_lower, re.I):
            return True, f"Tier 1: Title pattern matched ({pat!r})"

    # ── Reject filters — run BEFORE soft-accepts so non-music signals win ────
    # Keep this list narrow: only patterns that are unambiguously non-music.

    non_music_patterns = [
        r'\b(?:vlog|day in (?:my|the) life)\b',
        r'\b(?:gameplay|let\s*\'?s play|playthrough)\b',
        r'\b(?:tutorial|how[-\s]to|step[-\s]by[-\s]step)\b',
        r'\b(?:unboxing|haul|review|rating)\b',
        r'\b(?:podcast|episode|ep\.\s*\d)\b',
        r'\b(?:interview|talk show|q\s*&\s*a)\b',
    ]
    for pat in non_music_patterns:
        if re.search(pat, title_lower, re.I):
            return False, f"Rejected: non-music title pattern ({pat!r})"

    # ── Tier 2: soft-accept ───────────────────────────────────────────────────

    streaming_keywords = [
        'spotify', 'apple music', 'itunes', 'deezer', 'tidal',
        'soundcloud', 'bandcamp', 'amazon music', 'youtube music',
        'listen here', 'stream now', 'stream on', 'out now', 'available on',
        'lyrics', 'music video',
    ]
    if any(kw in description for kw in streaming_keywords):
        return True, "Tier 2: Streaming/music keywords in description"

    if 0 < duration <= 8 * 60:   # up to 8 minutes covers extended/intro versions
        return True, f"Tier 2: Duration {duration/60:.1f}m within song range"

    # Default: accept — if we couldn't find strong non-music evidence, treat
    # it as a song.  The downloader's validation step will reject it if deemix/
    # yt-dlp can't match it to a known track.
    return True, "Default accept: no non-music signals detected"


def run_ytdlp(cmd: list, timeout: int = 30):
    """Run a yt-dlp command. A region-locked video is retried once after
    geo_relay moves the Mullvad relay to a country where it is available."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    for _ in range(geo_relay.max_countries()):
        if result.returncode == 0 or not geo_relay.is_retryable_elsewhere(result.stderr):
            break
        target = next((arg for arg in reversed(cmd) if arg.startswith('http')), '') or (result.stderr or '')
        if not geo_relay.resolve_youtube(target, error_text=result.stderr):
            break
        logger.info("🌐 Retrying yt-dlp through the new relay")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result

    return result


def extract_youtube_metadata(url: str) -> Optional[Dict[str, Any]]:
    """
    Extract metadata from a YouTube link using yt-dlp.

    Args:
        url: YouTube URL

    Returns:
        Dict with keys: 'artist', 'title', 'album' or None if extraction fails
        For playlists, returns {'type': 'playlist', 'tracks': [...], 'source': 'youtube', 'url': url}
    """
    try:
        logger.debug(f"🎥 Extracting YouTube metadata from: {url}")
        
        # Check if it's a playlist
        is_playlist = 'list=' in url
        
        if is_playlist:
            # Get playlist-level metadata (title, uploader) using --dump-single-json
            # which returns the playlist container object (not individual entries).
            # --no-playlist must NOT be used here — we need the playlist object.
            cmd_info = [
                'yt-dlp',
                '--quiet',
                '--no-warnings',
                '--flat-playlist',
                '--dump-single-json',
                '--skip-download',
                url
            ]

            result_info = run_ytdlp(cmd_info, timeout=30)
            playlist_name = 'YouTube Playlist'
            if result_info.returncode == 0:
                try:
                    info_data = json.loads(result_info.stdout)
                    # playlist container fields: 'title', 'playlist_title', 'uploader'
                    playlist_name = (
                        info_data.get('title')
                        or info_data.get('playlist_title')
                        or info_data.get('uploader')
                        or 'YouTube Playlist'
                    ).strip()
                    logger.info(f"📋 YouTube playlist name: '{playlist_name}'")
                except Exception as _pie:
                    logger.debug(f"Could not parse playlist info JSON: {_pie}")
            
            # Extract playlist metadata
            cmd = [
                'yt-dlp',
                '--quiet',
                '--no-warnings',
                '--flat-playlist',
                '-j',  # JSON output for each entry
                '--skip-download',
                url
            ]
            
            result = run_ytdlp(cmd, timeout=60)
            
            if result.returncode != 0:
                logger.warning(f"⚠️ yt-dlp failed for playlist {url}: {result.stderr}")
                return None
            
            lines = result.stdout.strip().split('\n')
            tracks = []
            skipped_count = 0
            
            for line in lines:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                
                # Get video ID from flat playlist entry
                video_id = data.get('id')
                if not video_id:
                    continue
                
                # Fetch full metadata for this video to get category, description, duration
                try:
                    video_url = f'https://www.youtube.com/watch?v={video_id}'
                    cmd_full = [
                        'yt-dlp',
                        '--quiet',
                        '--no-warnings',
                        '-j',
                        '--no-playlist',
                        '--skip-download',
                        video_url
                    ]
                    result_full = run_ytdlp(cmd_full, timeout=15)
                    
                    if result_full.returncode != 0:
                        logger.debug(f"Could not fetch full metadata for video {video_id}, using flat data")
                        full_data = data
                    else:
                        try:
                            full_data = json.loads(result_full.stdout)
                        except:
                            full_data = data
                except Exception as e:
                    logger.debug(f"Error fetching full metadata for {video_id}: {e}")
                    full_data = data
                
                # Check if this is a music video
                is_music, reason = is_music_video(full_data)
                if not is_music:
                    logger.debug(f"⏭️ Skipping non-music video: {full_data.get('title', 'Unknown')} ({reason})")
                    skipped_count += 1
                    continue
                
                title = full_data.get('title', '').strip()
                uploader = full_data.get('uploader', '').strip()
                
                # Parse artist and title from the video title
                artist = uploader
                parsed_title = title
                
                # Try to extract artist from title if uploader is not reliable
                if title:
                    parts = _normalize_dash_separators(title).split(' - ', 1)
                    if len(parts) == 2:
                        left = parts[0].strip()
                        right = parts[1].strip()
                        
                        # The same checks as for a single video (below)
                        right_core = re.sub(r'\s*\([^)]*(?:Official|Video|Audio|Visualizer|Lyric|feat\.?|ft\.)[^)]*\)', '', right, flags=re.I).strip()
                        has_features_in_right = bool(re.search(r'\b(?:ft\.?|feat\.?|featuring)\b', right_core, re.I))
                        left_is_caps = left.isupper() and len(left.split()) <= 4
                        
                        if has_features_in_right:
                            parsed_title = left
                            artist = right_core
                        elif left_is_caps:
                            parsed_title = left
                            artist = right_core
                        else:
                            artist = left
                            parsed_title = right
                            parsed_title = re.sub(r'\s*\([^)]*(?:Official|Video|Audio|Visualizer|Lyric|feat\.?|ft\.)[^)]*\)', '', parsed_title, flags=re.I).strip()
                
                # If the uploader is a fan/lyric channel, SongNameSplit can
                # recover the real artist + song from the raw title.
                if _looks_like_fan_channel(uploader):
                    _raw = full_data.get('title', '').strip()
                    logger.debug(f'[SNS] Fan channel playlist: {repr(_raw)}')
                    _sns = _try_songnamesplit(_raw)
                    if _sns:
                        artist       = _sns['artist']
                        parsed_title = _sns['songname']
                        logger.info(f'[SNS] Playlist resolved: artist={repr(artist)}, title={repr(parsed_title)}')

                artist       = parse_primary_artist(artist)
                parsed_title = clean_track_title(parsed_title)

                if parsed_title and artist:
                    tracks.append({
                        'artist': artist,
                        'title': parsed_title,
                        'album': '',
                        'url': video_url,
                        'duration': int(full_data.get('duration') or 0),
                    })
            
            if tracks:
                logger.info(f"✅ YouTube playlist extracted: {len(tracks)} tracks (skipped {skipped_count} non-music videos)")
                return {
                    'type': 'playlist',
                    'name': playlist_name,
                    'tracks': tracks,
                    'source': 'youtube',
                    'url': url
                }
            else:
                logger.warning(f"⚠️ No music videos found in YouTube playlist: {url}")
                return None
        
        # Single video extraction
        cmd = [
            'yt-dlp',
            '--quiet',
            '--no-warnings',
            '-j',  # JSON output
            '--no-playlist',
            '--skip-download',
            url
        ]
        
        result = run_ytdlp(cmd, timeout=30)
        
        if result.returncode != 0:
            logger.warning(f"⚠️ yt-dlp failed for {url}: {result.stderr}")
            return None
        
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ Failed to parse yt-dlp JSON output: {e}")
            return None
        
        # Check if this is a music video
        is_music, reason = is_music_video(data)
        if not is_music:
            logger.warning(f"⚠️ YouTube video is not a music video: {data.get('title', 'Unknown')}")
            logger.debug(f"   Reason: {reason}")
            return None
        
        # Extract metadata
        title = data.get('title', '').strip()
        artist = data.get('artist', '').strip()
        
        # Try to extract artist from title if not provided
        if not artist and title:
            # Try pattern: "Artist - Song Title" or "Song Title - Artist"
            # Need to determine which part is which
            parts = _normalize_dash_separators(title).split(' - ', 1)
            if len(parts) == 2:
                left = parts[0].strip()
                right = parts[1].strip()
                
                # Heuristics to determine which is artist vs title:
                # 1. If right part has "(Official", "(feat", "(Visualizer", etc -> it's probably title
                # 2. If left part looks like all caps and short -> might be the title
                # 3. Artist names are typically not in ALL CAPS, titles often are
                # 0. Strongest signal first: an official artist channel is usually
                #    named after the artist (e.g. uploader="League of Legends" for
                #    "RISE (ft. The Glitch Mob, Mako, and The Word Alive) | Worlds
                #    2018 - League of Legends"). Trust a match here over the
                #    shape-based guesses below, which have no way to know this
                #    convention and get titles like this backwards.
                _uploader_for_match = (data.get('uploader', '') or data.get('channel', '') or '').strip()
                _uploader_matches_right = bool(_uploader_for_match) and _names_roughly_match(_uploader_for_match, right)
                _uploader_matches_left = bool(_uploader_for_match) and _names_roughly_match(_uploader_for_match, left)

                # Remove common suffixes to check the core content
                right_core = re.sub(r'\s*\([^)]*(?:Official|Video|Audio|Visualizer|Lyric|feat\.?|ft\.)[^)]*\)', '', right, flags=re.I).strip()

                # Check if right side has "ft." or "feat" in the core (not in parens)
                has_features_in_right = bool(re.search(r'\b(?:ft\.?|feat\.?|featuring)\b', right_core, re.I))

                # Check if left is ALL CAPS and relatively short (common for song titles)
                left_is_caps = left.isupper() and len(left.split()) <= 4

                # Decision logic:
                if _uploader_matches_right and not _uploader_matches_left:
                    # Uploader channel is named after the artist on the right —
                    # trust it even though the left side isn't ALL CAPS or don't
                    # have "feat." to tip off the shape-based heuristics below.
                    title = left
                    artist = right_core
                    logger.debug(f"Parsed as Title - Artist (uploader matches right): '{artist}' - '{title}'")
                elif _uploader_matches_left and not _uploader_matches_right:
                    artist = left
                    title = right
                    title = re.sub(r'\s*\([^)]*(?:Official|Video|Audio|Visualizer|Lyric|feat\.?|ft\.)[^)]*\)', '', title, flags=re.I).strip()
                    logger.debug(f"Parsed as Artist - Title (uploader matches left): '{artist}' - '{title}'")
                elif has_features_in_right:
                    # "WALL KISSER - Lil Pump ft. SNEAKO (Official Visualizer)"
                    # Right has features -> right is artist, left is title
                    title = left
                    artist = right_core  # Use core without parenthetical extras
                    logger.debug(f"Parsed as Title - Artist (features in right): '{artist}' - '{title}'")
                elif left_is_caps:
                    # "WALL KISSER - Lil Pump (Official Visualizer)"
                    # Left is all caps -> probably title
                    title = left
                    artist = right_core
                    logger.debug(f"Parsed as Title - Artist (left is caps): '{artist}' - '{title}'")
                else:
                    # Default: assume "Artist - Title" format
                    artist = left
                    title = right
                    # Clean title of extra stuff including features
                    title = re.sub(r'\s*\([^)]*(?:Official|Video|Audio|Visualizer|Lyric|feat\.?|ft\.)[^)]*\)', '', title, flags=re.I).strip()
                    logger.debug(f"Parsed as Artist - Title (default): '{artist}' - '{title}'")

                # A trailing " | <event/description>" segment (e.g. "RISE | Worlds
                # 2018", common on official game/movie soundtrack uploads) is
                # video-description noise, not part of the song title — left in,
                # it drags down every downstream search/match score enough to miss
                # even an exact catalog hit. Strip it regardless of which branch
                # above set `title`.
                if title and '|' in title:
                    title = title.split('|', 1)[0].strip()

        # Fallback: use uploader/channel as artist if still no artist
        uploader = data.get('uploader', '').strip() or data.get('channel', '').strip()

        # If we don't have a reliable artist or the title looks ambiguous,
        # attempt SongNameSplit on the raw video title before defaulting to the
        # uploader or naive title split.
        if _SNS_AVAILABLE and data.get('title'):
            _raw_title = data.get('title', '').strip()
            if not data.get('artist') or _looks_like_fan_channel(uploader) or _has_dash_separator(_raw_title):
                logger.debug(f'[SNS] Attempting SongNameSplit on: {repr(_raw_title)}')
                _sns = _try_songnamesplit(_raw_title)
                if _sns and _sns.get('artist') and _sns.get('songname'):
                    artist = _sns['artist']
                    title = _sns['songname']
                    logger.info(f'[SNS] Resolved ambiguous YouTube title: artist={repr(artist)}, title={repr(title)}')

        if not artist:
            artist = uploader

        # If the uploader looks like a fan/lyric channel, SongNameSplit can
        # often recover the real artist and song title from the raw video title.
        if _looks_like_fan_channel(uploader):
            _raw_title = data.get('title', '').strip()
            logger.debug(f'[SNS] Fan channel ({repr(uploader)}) on: {repr(_raw_title)}')
            _sns = _try_songnamesplit(_raw_title)
            if _sns:
                artist = _sns['artist']
                title  = _sns['songname']
                logger.info(f'[SNS] Resolved: artist={repr(artist)}, title={repr(title)}')

        artist = artist_from_channel(parse_primary_artist(artist))
        title  = song_in_quotes(clean_track_title(title), artist)
        raw_title = data.get('title', '').strip()

        if not title or not artist:
            logger.warning(f"Could not extract title/artist from YouTube: title='{title}', artist='{artist}'")
            return None

        logger.info(f'YouTube metadata extracted: {artist} - {title}')

        return {
            'artist': artist,
            'title': title,
            'album': data.get('album', '').strip() or '',
            'source': 'youtube',
            'url': url,
            'video_title': raw_title,
            'video_uploader': uploader,
            # Help pick the version the video is: its length, and how it
            # describes itself ("live at …", "a cappella").
            'video_duration': int(data.get('duration') or 0),
            'video_description': (data.get('description') or '')[:300],
        }
    
    except subprocess.TimeoutExpired:
        logger.error(f"❌ yt-dlp timeout for {url}")
        return None
    except Exception as e:
        logger.error(f"❌ Error extracting YouTube metadata: {e}")
        return None






def clean_track_title(title: str) -> str:
    """Strip YouTube/streaming noise from a song title so the downloader
    and duplicate-checker both work with the canonical name.

    Removes:
    - Quality/format tags:      [HD], [HQ], [4K], [1080p], etc.
    - Label/platform tags:      [Official Video], (Official Audio), (Lyrics),
                                [Official Music Video], (Visualizer), (Audio),
                                (Video), [AMV], etc.
    - Remaster/version tags:    (2013 Remaster), (Deluxe), (Radio Edit), etc.
      — BUT only if they appear at the END of the string, to avoid stripping
        legitimate parts of creative titles.
    - "Topic -" channel prefix: "Topic - Title" → "Title"

    Preserves:
    - (feat. X) / (ft. X) — kept for artist context
    - Parenthetical year alone: (2024) at the END is stripped (label artefact)
    """
    if not title:
        return title
    t = title.strip()

    # Strip "Topic -" prefix
    t = re.sub(r'^topic\s*[-–—]\s*', '', t, flags=re.I).strip()

    # Strip any bracketed/parenthetical tag that does NOT contain "feat" or "ft"
    # Allow up to two passes to handle stacked tags like "[HD] [Official Video]"
    _noise = re.compile(
        r'\s*[\[\(]'
        r'(?!.*?\b(?:feat|ft)\b)'     # negative lookahead — keep feat tags
        r'[^\[\]\(\)]{0,80}'
        r'[\]\)]',
        re.IGNORECASE,
    )
    for _ in range(3):
        cleaned = _noise.sub('', t).strip()
        if cleaned == t:
            break
        t = cleaned

    # Strip any unclosed bracket remainder at end of string
    t = re.sub(r'\s*[\[\(][^\)\]]*$', '', t).strip()

    return t or title.strip()


def parse_primary_artist(artist_str: str) -> str:
    """
    Extract primary artist from a string that may contain features.
    
    Examples:
      'Artist feat. Someone' -> 'Artist'
      'Artist (feat. Someone & Other)' -> 'Artist'
      'Artist1 & Artist2' -> 'Artist1'
      'Artist featuring Someone' -> 'Artist'
    
    Returns:
        Primary artist name or original string if parsing fails
    """
    if not artist_str:
        return ''
    
    artist = artist_str.strip()
    
    # Remove parenthetical featuring: (feat. ...) or (featuring ...) etc.
    artist = re.sub(r'\s*\([^)]*(?:feat(?:uring)?|ft)\.?[^)]*\)', '', artist, flags=re.I).strip()
    
    # Remove inline featuring: " feat. ..." or " featuring ..." etc.
    artist = re.sub(r'\s+(?:feat(?:uring)?|ft)\.?\s+.+$', '', artist, flags=re.I).strip()
    
    # Remove generic "Topic" markers often present in YouTube topic channels
    # e.g., "John Williams - Topic" or "Topic - The Weeknd"
    artist = re.sub(r'\s*[-–—]\s*topic\s*$', '', artist, flags=re.I).strip()
    artist = re.sub(r'^topic\s*[-–—]\s*', '', artist, flags=re.I).strip()

    # Split on common separators and take first part (handles collabs/features)
    for sep in ['/', ' & ', ' and ', ' x ', ',', ';']:
        if sep in artist:
            artist = artist.split(sep)[0].strip()
            break
    
    return artist or artist_str.strip()


def _parse_shared_track_text(message_text: str) -> Tuple[str, str]:
    """Return (title, artist) from a share caption like "<Title> by <Artist>".

    Shazam and Apple Music share a link with exactly that wording, which is
    the only reliable source of the artist when the page itself refuses to
    be fetched.
    """
    text = re.sub(r'https?://\S+', ' ', message_text or '')
    text = re.sub(r'\s+', ' ', text).strip(' -–—:;,')
    match = re.match(r'^(.+?)\s+by\s+(.+?)$', text, re.IGNORECASE)
    if not match:
        return '', ''
    title, artist = match.group(1).strip(), match.group(2).strip(' ,.;:-')
    return (title, artist) if artist else ('', '')


def extract_shazam_metadata(url: str, message_text: str = '') -> Optional[Dict[str, str]]:
    """
    Extract metadata from a Shazam link.
    
    Shazam URLs have the format:
    https://www.shazam.com/track/{track_id}/{song-title-artist-features}
    or
    https://www.shazam.com/song/{track_id}/{song-title-artist-features}
    
    Args:
        url: Shazam URL (e.g., https://www.shazam.com/track/54011268/civil-war-feat-brother-ali-killer-mike-chuck-d)
        
    Returns:
        Dict with keys: 'artist', 'title', 'album' or None if extraction fails
    """
    try:
        logger.debug(f"🎵 Extracting Shazam metadata from: {url}")
        
        # Extract track ID and slug from URL
        url_match = re.search(r'/(?:track|song)/(\d+)/([^?]+)', url)
        if not url_match:
            logger.warning(f"⚠️ Could not parse Shazam URL format: {url}")
            return None
        
        track_id = url_match.group(1)
        slug = url_match.group(2)
        logger.debug(f"Shazam track ID: {track_id}, slug: {slug}")
        
        # Parse the slug to extract song info
        # Format examples:
        # "civil-war-feat-brother-ali-killer-mike-chuck-d" 
        # "song-title-artist-name"
        # "song-title-feat-artist1-artist2"
        
        slug_readable = slug.replace('-', ' ')
        
        # Initialize variables
        title = ''
        artist = ''
        features = []
        
        # Try to split on 'feat' to separate main content from features
        feat_patterns = [
            r'^(.+?)\s+feat\s+(.+)$',
            r'^(.+?)\s+ft\s+(.+)$',
            r'^(.+?)\s+featuring\s+(.+)$',
        ]
        
        main_part = slug_readable
        feat_part = ''
        
        for pattern in feat_patterns:
            match = re.search(pattern, slug_readable, re.I)
            if match:
                main_part = match.group(1).strip()
                feat_part = match.group(2).strip()
                logger.debug(f"Split on features - main: '{main_part}', feat: '{feat_part}'")
                break
        
        # Try multiple strategies to parse artist and title from main_part
        artist = ''
        title = main_part
        
        # Strategy 1: Try to fetch from Shazam API or HTML
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=10)
            
            logger.debug(f"Shazam page fetch status: {response.status_code}")
            
            if response.status_code == 200:
                html = response.text
                logger.debug(f"Shazam HTML response length: {len(html)} bytes")
                
                # Look for JSON-LD structured data
                jsonld_pattern = r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
                jsonld_matches = re.findall(jsonld_pattern, html, re.I | re.DOTALL)
                
                for jsonld_str in jsonld_matches:
                    try:
                        jsonld = json.loads(jsonld_str)
                        if isinstance(jsonld, dict) and jsonld.get('@type') == 'MusicRecording':
                            # Extract from MusicRecording schema
                            title = jsonld.get('name', title).strip()
                            
                            if 'byArtist' in jsonld:
                                artist_data = jsonld['byArtist']
                                if isinstance(artist_data, dict):
                                    artist = artist_data.get('name', '').strip()
                                elif isinstance(artist_data, list) and len(artist_data) > 0:
                                    artist = artist_data[0].get('name', '').strip()
                            
                            logger.info(f"📊 Found in JSON-LD - artist: '{artist}', title: '{title}'")
                            break
                    except json.JSONDecodeError:
                        pass
                
                # Fallback: Try meta tags
                if not artist or not title:
                    title_match = re.search(r'<meta\s+(?:property|name)="(?:og:title|twitter:title)"\s+content="([^"]+)"', html, re.I)
                    if title_match:
                        meta_title = title_match.group(1).strip()
                        logger.debug(f"Found meta title: '{meta_title}'")
                        
                        # Decode HTML entities (e.g., &amp; -> &)
                        import html
                        meta_title = html.unescape(meta_title)
                        
                        # Shazam meta titles often have format "Song by Artist | Shazam"
                        # Remove " | Shazam" suffix
                        meta_title = re.sub(r'\s*\|\s*Shazam\s*$', '', meta_title, flags=re.I).strip()
                        
                        # Check for " by " pattern (most common)
                        if ' by ' in meta_title:
                            parts = meta_title.split(' by ', 1)
                            title = parts[0].strip()
                            artist = parts[1].strip()
                            
                            # Clean up artist: remove promotional text like ": Song Lyrics, Music Videos & Concerts"
                            artist = re.sub(r':\s*(?:Song Lyrics|Music Videos?|Concerts?|Listen).*$', '', artist, flags=re.I).strip()
                            
                            logger.info(f"Parsed from meta title (by): '{artist}' - '{title}'")
                        # Check for " - " pattern
                        elif ' - ' in meta_title:
                            parts = meta_title.split(' - ', 1)
                            title = parts[0].strip()
                            artist = parts[1].strip()
                            
                            # Clean up artist: remove promotional text
                            artist = re.sub(r':\s*(?:Song Lyrics|Music Videos?|Concerts?|Listen).*$', '', artist, flags=re.I).strip()
                            
                            logger.info(f"Parsed from meta title (dash): '{artist}' - '{title}'")
        
        except Exception as e:
            logger.debug(f"Could not fetch Shazam page: {e}")
        
        # Strategy 2: If we still don't have artist/title, parse from slug
        # This is tricky because we don't know where the title ends and artist begins
        # For now, we'll use the whole main_part as title unless we have better data
        if not title:
            title = main_part
        
        # If we have features, add them to the title
        if feat_part:
            # Parse feature artists - handle various separators
            feat_artists = []
            
            # In Shazam slugs, artists are often separated by spaces, not commas
            # Example: "brother ali killer mike chuck d"
            # We need to be smart about this
            
            # First, check if there are explicit separators
            if ',' in feat_part or '&' in feat_part or ' and ' in feat_part.lower():
                # Has explicit separators
                feat_part_normalized = re.sub(r'\s+and\s+', ' & ', feat_part, flags=re.I)
                feat_artists = [f.strip() for f in re.split(r'[,&]', feat_part_normalized) if f.strip()]
            else:
                # No separators - need to parse space-separated full names
                # This is tricky. We'll use a heuristic:
                # - Look for capitalized words as potential name starts
                # - Common artist name patterns (2-3 words typically)
                
                words = feat_part.split()
                
                # If we have an even number of words, assume pairs
                if len(words) % 2 == 0:
                    # Split into pairs: "brother ali killer mike" -> ["brother ali", "killer mike"]
                    feat_artists = [' '.join(words[i:i+2]) for i in range(0, len(words), 2)]
                elif len(words) % 3 == 0:
                    # Split into triplets: "a b c d e f" -> ["a b c", "d e f"]
                    feat_artists = [' '.join(words[i:i+3]) for i in range(0, len(words), 3)]
                else:
                    # Odd number, try to be smart
                    # If 3 words: single artist with full name
                    # If 5+ words: try to split intelligently
                    if len(words) <= 3:
                        feat_artists = [feat_part]
                    else:
                        # Try pairs for most, triplet for last if odd
                        feat_artists = [' '.join(words[i:i+2]) for i in range(0, len(words)-1, 2)]
                        if len(words) % 2 == 1:
                            # Last word alone or with previous
                            if len(feat_artists) > 0:
                                feat_artists[-1] = feat_artists[-1] + ' ' + words[-1]
            
            # Capitalize each artist name properly (title case)
            feat_artists = [artist.title() for artist in feat_artists]
            
            # Format: "Song Title (feat. Artist1, Artist2 & Artist3)"
            if len(feat_artists) == 1:
                title = f"{title} (feat. {feat_artists[0]})"
            elif len(feat_artists) == 2:
                title = f"{title} (feat. {feat_artists[0]} & {feat_artists[1]})"
            else:
                # Multiple artists: "Artist1, Artist2, Artist3 & Artist4"
                feat_str = ', '.join(feat_artists[:-1]) + ' & ' + feat_artists[-1]
                title = f"{title} (feat. {feat_str})"
            
            logger.debug(f"Added features to title: '{title}'")
        
        # Capitalize title properly (title case)
        if title:
            title = title.title()
            # Keep 'feat.' lowercase for standard formatting
            title = re.sub(r'\(Feat\.', '(feat.', title)
        
        # If still no artist, set to Unknown
        if not artist and message_text:
            shared_title, shared_artist = _parse_shared_track_text(message_text)
            if shared_artist:
                artist = shared_artist
                title = shared_title or title
                logger.info(f"📋 Took artist from the shared text: '{artist}' - '{title}'")

        if not artist:
            artist = 'Unknown Artist'
            logger.warning(f"⚠️ Could not determine artist from Shazam link, using: {artist}")
        
        # Clean up and parse primary artist
        artist = parse_primary_artist(artist)
        
        if not title:
            logger.warning(f"⚠️ Could not extract title from Shazam link")
            return None
        
        logger.info(f"✅ Shazam metadata extracted: {artist} - {title}")
        
        return {
            'artist': artist,
            'title': title,
            'album': '',
            'source': 'shazam',
            'url': url
        }
    
    except Exception as e:
        logger.error(f"❌ Error extracting Shazam metadata: {e}")
        return None



def extract_urls_from_text(text: str) -> list:
    """
    Extract URLs from text message.
    
    Returns:
        List of URLs found in text
    """
    if not text:
        return []
    # Pattern to match URLs
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, text)
    return urls
