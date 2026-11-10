"""Audio identification helper using multiple services with fallback.

This module provides a simple `identify_track_by_audio(file_path)` function
that returns canonical metadata when possible. It tries multiple services in order:
1. ACRCloud
2. audd.io
3. Shazamio (unofficial Shazam)
4. AcoustID/MusicBrainz

Each is implemented defensively so tests can monkeypatch without requiring
real network calls or native binaries.

Returned dict format (on success):
{
    'artist': 'Nicki Minaj',
    'title': 'Big Foot (A Cappella)',
    'album': 'Some Album',
    'release_id': '...',
    'cover_art_url': 'https://.../cover.jpg'
}

If identification cannot be performed or fails, returns None.
"""

from typing import Optional, Dict
import os
import logging
import warnings
import requests
import time
import hmac
import hashlib
import base64
import subprocess
import tempfile
import pathlib

from text_repair import dedupe_feat

# Suppress deprecation warnings from audioread about aifc and sunau (Python 3.13+)
# These are warnings from the audioread library using deprecated stdlib modules
warnings.filterwarnings('ignore', category=DeprecationWarning, message='.*aifc.*')
warnings.filterwarnings('ignore', category=DeprecationWarning, message='.*sunau.*')

logger = logging.getLogger(__name__)

# Environment variables for API keys
ACRCLOUD_ACCESS_KEY = os.environ.get('ACRCLOUD_ACCESS_KEY', '').strip()
ACRCLOUD_ACCESS_SECRET = os.environ.get('ACRCLOUD_ACCESS_SECRET', '').strip()
ACRCLOUD_HOST = os.environ.get('ACRCLOUD_HOST', '').strip()

AUDD_API_TOKEN = os.environ.get('AUDD_API_TOKEN', '').strip()

ACOUSTID_API_KEY = os.environ.get('ACOUSTID_API_KEY', '').strip()


def _get_audio_duration(src_path: str) -> Optional[float]:
    """Use ffprobe to return the duration of an audio file in seconds.

    Returns float seconds or None on failure.
    """
    try:
        proc = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                str(src_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        dur_str = proc.stdout.strip()
        return float(dur_str) if dur_str else None
    except Exception as e:
        logger.debug(f"🔧 ffprobe duration failed: {e}")
        return None


def _create_audio_sample(src_path: str, duration: int = 10) -> Optional[str]:
    """Create a short audio sample (WAV) from `src_path` using ffmpeg.

    The snippet is taken from the MIDDLE of the track (if long enough) to
    avoid intros/outros which may be less representative for fingerprinting.

    Returns the path to the temporary sample file or None on failure.
    The caller is responsible for removing the file.
    """
    tmp_path = None
    try:
        # ─── PRE-VALIDATION: Check source file before any processing ───
        # Check if source file exists
        if not os.path.exists(src_path):
            logger.debug(f"🔧 Source file does not exist: {src_path}")
            return None
        
        # Check if it's a regular file (not directory/symlink to missing file)
        if not os.path.isfile(src_path):
            logger.debug(f"🔧 Source path is not a regular file: {src_path}")
            return None
        
        # Validate source file size (>1KB minimum)
        src_size = os.path.getsize(src_path)
        if src_size < 1024:
            logger.debug(f"🔧 Source file too small ({src_size} bytes, minimum 1KB): {src_path}")
            return None
        
        # ─── Validate audio duration ───
        offset = 0
        total = _get_audio_duration(src_path)
        if not total or total <= 0:
            logger.debug(f"🔧 Invalid or zero audio duration for {src_path}: {total}")
            return None
        
        if total > duration:
            offset = max(0, (total / 2) - (duration / 2))
        
        # ─── Create temporary sample file ───
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
        tmp_path = tmp.name
        tmp.close()
        
        cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        ]
        if offset > 0:
            cmd += ['-ss', str(int(offset))]
        cmd += [
            '-i', str(src_path),
            '-t', str(int(duration)),
            '-vn',
            '-ar', '22050',
            '-ac', '1',
            '-f', 'wav',
            str(tmp_path),
        ]
        logger.debug(f"🔧 Creating audio sample with ffmpeg: offset={int(offset)}s, duration={duration}s")
        
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except subprocess.TimeoutExpired:
            logger.debug(f"🔧 ffmpeg timeout creating sample from {src_path} (exceeded 30s)")
            return None
        
        # ─── Validate output sample ───
        if not os.path.exists(tmp_path):
            logger.debug(f"🔧 ffmpeg did not produce output file at {tmp_path}")
            return None
        
        sample_size = os.path.getsize(tmp_path)
        if sample_size == 0:
            logger.debug(f"🔧 ffmpeg produced empty sample file from {src_path}")
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return None
        
        logger.debug(f"🔧 Successfully created audio sample: {sample_size} bytes")
        return tmp_path
        
    except subprocess.TimeoutExpired:
        logger.debug(f"🔧 ffmpeg timeout creating sample from {src_path} (exceeded 30s)")
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return None
    except Exception as e:
        logger.debug(f"🔧 Failed to create audio sample from {src_path}: {type(e).__name__}: {e}")
        if tmp_path:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
        return None


def _json_utf8(resp) -> dict:
    """Parse a JSON reply as UTF-8, whatever charset it is labelled with.

    ACRCloud answers `Content-Type: text/plain` with no charset; `requests`
    then assumes ISO-8859-1 and `resp.json()` turns every non-ASCII title
    into mojibake ("ما أجمل الحياة" → "Ù\x85Ø§ Ø£Ø¬Ù…", "Montréal" →
    "MontrÃ©al"). JSON is UTF-8 by definition (RFC 8259).
    """
    import json
    return json.loads(resp.content.decode("utf-8-sig"))


def _identify_with_acrcloud(file_path: str) -> Optional[Dict[str, str]]:
    """Identify the file with ACRCloud's HTTP API.

    The signed request (HMAC-SHA1) is built here rather than through the
    `acrcloud` SDK, which would add a compiled dependency for one call.
    """
    logger.debug(f"🔊 ACRCloud: Starting identification for {file_path}")

    if not all([ACRCLOUD_ACCESS_KEY, ACRCLOUD_ACCESS_SECRET, ACRCLOUD_HOST]):
        logger.debug(f"🔊 ACRCloud: Credentials not configured (key={bool(ACRCLOUD_ACCESS_KEY)}, secret={bool(ACRCLOUD_ACCESS_SECRET)}, host={bool(ACRCLOUD_HOST)}), skipping")
        return None

    # Validate that the file exists and is readable
    if not os.path.exists(file_path):
        logger.warning(f"🔊 ACRCloud: Audio file not found: {file_path}")
        return None

    # create a short sample (10s) to send to remote services to improve
    # robustness and avoid uploading very large files.
    logger.debug(f"🔊 ACRCloud: Creating 10-second audio sample from {file_path}")
    sample_path = _create_audio_sample(file_path, duration=10)
    if not sample_path:
        logger.warning("🔊 ACRCloud: could not create audio sample, skipping")
        return None

    logger.debug(f"🔊 ACRCloud: Sample created at {sample_path} ({os.path.getsize(sample_path)} bytes)")

    timestamp = str(int(time.time()))
    # ACRCloud signature v1 format: includes access_key, data_type, signature_version, timestamp
    string_to_sign = "\n".join([
        "POST",
        "/v1/identify",
        ACRCLOUD_ACCESS_KEY,
        "audio",
        "1",
        timestamp
    ])
    signature = base64.b64encode(
        hmac.new(
            ACRCLOUD_ACCESS_SECRET.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            hashlib.sha1
        ).digest()
    ).decode('utf-8')

    url = f"https://{ACRCLOUD_HOST}/v1/identify"
    logger.debug(f"🔊 ACRCloud: Sending POST request to {url}")
    
    try:
        files = {'sample': open(sample_path, 'rb')}
    except Exception as e:
        logger.warning(f"🔊 ACRCloud: could not open sample file: {e}")
        try:
            os.remove(sample_path)
        except Exception:
            pass
        return None
    
    data = {
        'access_key': ACRCLOUD_ACCESS_KEY,
        'sample_bytes': os.path.getsize(sample_path) if os.path.exists(sample_path) else 0,
        'timestamp': timestamp,
        'signature': signature,
        'data_type': 'audio',
        'signature_version': '1'
    }
    
    logger.debug(f"🔊 ACRCloud: POST data keys: {list(data.keys())}")
    logger.debug(f"🔊 ACRCloud: Sample size: {data['sample_bytes']} bytes")
    
    try:
        logger.debug(f"🔊 ACRCloud: Sending HTTPS POST request...")
        resp = requests.post(url, data=data, files=files, timeout=15)
        logger.debug(f"🔊 ACRCloud: Response status code: {resp.status_code}")
        logger.debug(f"🔊 ACRCloud: Response headers: {dict(resp.headers)}")
        result_dict = _json_utf8(resp)
        logger.debug(f"🔊 ACRCloud: Response JSON keys: {list(result_dict.keys())}")
    except requests.exceptions.Timeout:
        logger.warning(f"🔊 ACRCloud: HTTP request timeout (15s)")
        result_dict = None
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"🔊 ACRCloud: Connection error: {e}")
        result_dict = None
    except Exception as e:
        logger.warning(f"🔊 ACRCloud: HTTP request failed: {type(e).__name__}: {e}")
        result_dict = None
    finally:
        try:
            files['sample'].close()
        except Exception:
            pass
        try:
            os.remove(sample_path)
        except Exception:
            pass

    if not result_dict:
        logger.warning(f"🔊 ACRCloud: No response received")
        return None

    status_code = result_dict.get('status', {}).get('code')
    status_msg = result_dict.get('status', {}).get('msg', '')
    logger.debug(f"🔊 ACRCloud: Status code: {status_code}, msg: {status_msg}")
    
    if status_code != 0:
        logger.debug(f"🔊 ACRCloud: nonzero status {status_code}: {status_msg}")
        return None

    metadata = result_dict.get('metadata', {})
    if not metadata.get('music'):
        logger.debug(f"🔊 ACRCloud: No music metadata in response")
        return None

    music_candidates = metadata['music']
    logger.debug(f"🔊 ACRCloud: music candidate count: {len(music_candidates)}")

    def _album_name_from_provider(provider_data: dict) -> str:
        if not isinstance(provider_data, dict):
            return ''
        album_data = provider_data.get('album', {})
        if isinstance(album_data, dict):
            return (album_data.get('name') or album_data.get('title') or '').strip()
        if isinstance(album_data, str):
            return album_data.strip()
        return ''

    def _resolve_candidate_album(candidate: dict) -> str:
        providers = candidate.get('external_metadata', {}) or {}
        for provider in ('deezer', 'spotify', 'apple_music'):
            album_name = _album_name_from_provider(providers.get(provider, {}))
            if album_name:
                return album_name

        acr_album = candidate.get('album', {})
        if isinstance(acr_album, dict):
            return (acr_album.get('name') or acr_album.get('title') or '').strip()
        if isinstance(acr_album, str):
            return acr_album.strip()

        release_info = candidate.get('release', {})
        if isinstance(release_info, dict):
            return (release_info.get('name') or release_info.get('title') or '').strip()

        return ''

    def _valid_album(candidate: dict, album_name: str) -> bool:
        title = (candidate.get('title') or '').strip().lower()
        artist = ', '.join([a['name'] for a in candidate.get('artists', [])]).strip().lower()
        normalized = album_name.strip().lower()
        return bool(album_name and normalized != title and normalized != artist)

    def _is_compilation_album(album_name: str) -> bool:
        """Check if an album name looks like a compilation/soundtrack album."""
        if not album_name:
            return False
        
        # Common compilation indicators
        compilation_indicators = [
            'hit list', 'greatest hits', 'best of', 'collection', 'anthology',
            'ultimate', 'complete', 'essential', 'gold', 'platinum', 'diamond',
            'vol.', 'volume', 'compilation', 'soundtrack', 'original motion picture',
            'original soundtrack', 'music from', 'songs from', 'from the',
            'the best', 'all time', 'forever', 'legendary', 'iconic',
            'classics', 'masterpiece', 'remastered hits', 'hits collection',
            'top hits', 'mega hits', 'super hits', 'hot hits', 'big hits',
            'chart hits', 'radio hits', 'dance hits', 'pop hits', 'rock hits',
            'hip hop hits', 'r&b hits', 'country hits', 'jazz hits',
            'now that\'s what i call', 'pure hits', 'fresh hits', 'cool hits'
        ]
        
        album_lower = album_name.lower().strip()
        
        # Check for compilation indicators
        for indicator in compilation_indicators:
            if indicator in album_lower:
                logger.debug(f"🔊 ACRCloud: Detected compilation album: '{album_name}' (contains '{indicator}')")
                return True
        
        # Check for patterns like "Artist - Greatest Hits"
        if ' - ' in album_name:
            parts = [p.strip().lower() for p in album_name.split(' - ', 1)]
            if len(parts) == 2:
                # If second part looks like a compilation title
                second_part = parts[1]
                for indicator in ['greatest hits', 'best of', 'collection', 'hits']:
                    if indicator in second_part:
                        logger.debug(f"🔊 ACRCloud: Detected compilation album: '{album_name}' (pattern match)")
                        return True
        
        return False

    def _match_priority(candidate: dict) -> tuple[int, float]:
        score = int(candidate.get('score', 0) or 0)
        album_name = _resolve_candidate_album(candidate)
        priority = 0
        metadata = candidate.get('external_metadata', {}) or {}
        
        # Check each provider's album
        deezer_album = _album_name_from_provider(metadata.get('deezer', {}))
        spotify_album = _album_name_from_provider(metadata.get('spotify', {}))
        apple_album = _album_name_from_provider(metadata.get('apple_music', {}))
        
        # Prefer valid albums that are not compilations
        if _valid_album(candidate, deezer_album) and not _is_compilation_album(deezer_album):
            priority += 1000
        elif _valid_album(candidate, deezer_album):
            priority += 100  # Still prefer over invalid, but much lower priority
        
        if _valid_album(candidate, spotify_album) and not _is_compilation_album(spotify_album):
            priority += 800
        elif _valid_album(candidate, spotify_album):
            priority += 80
        
        if _valid_album(candidate, apple_album) and not _is_compilation_album(apple_album):
            priority += 700
        elif _valid_album(candidate, apple_album):
            priority += 70
        
        if _valid_album(candidate, album_name) and not _is_compilation_album(album_name):
            priority += 100
        elif _valid_album(candidate, album_name):
            priority += 10
        
        return priority, score

    for idx, candidate in enumerate(music_candidates):
        metadata = candidate.get('external_metadata', {}) or {}
        logger.debug(
            f"🔊 ACRCloud candidate {idx}: title={candidate.get('title')} score={candidate.get('score')} "
            f"deezer_album={_album_name_from_provider(metadata.get('deezer', {}))}"
        )

    music = max(music_candidates, key=_match_priority)
    selected_index = music_candidates.index(music)
    if selected_index != 0:
        logger.debug(f"🔊 ACRCloud: Selected candidate index {selected_index} over first result")

    logger.debug(f"🔊 ACRCloud: Full music object keys: {list(music.keys())}")
    logger.debug(f"🔊 ACRCloud: Full music object: {music}")
    logger.debug(f"🔊 ACRCloud: Album data from response: {music.get('album', 'MISSING')}")

    artist = ', '.join([a['name'] for a in music.get('artists', [])])
    title = music.get('title', '')

    # Try to get album from external sources first. Deezer is usually best,
    # but Spotify/Apple Music/ACRCloud release data can also be useful.
    album = ''
    cover_art_url = ''

    external_metadata = music.get('external_metadata', {}) or {}
    logger.debug(f"🔊 ACRCloud: External metadata keys: {list(external_metadata.keys())}")
    
    # Try to get album from external sources first, but avoid compilations
    if external_metadata.get('deezer'):
        deezer_album = _album_name_from_provider(external_metadata['deezer'])
        if deezer_album and not _is_compilation_album(deezer_album):
            album = deezer_album
            logger.debug(f"🔊 ACRCloud: Found album from Deezer: '{album}'")
        elif deezer_album:
            logger.debug(f"🔊 ACRCloud: Skipping compilation album from Deezer: '{deezer_album}'")

    if not album and external_metadata.get('spotify'):
        spotify_album = _album_name_from_provider(external_metadata['spotify'])
        if spotify_album and not _is_compilation_album(spotify_album):
            album = spotify_album
            logger.debug(f"🔊 ACRCloud: Found album from Spotify: '{album}'")
        elif spotify_album:
            logger.debug(f"🔊 ACRCloud: Skipping compilation album from Spotify: '{spotify_album}'")

    if not album and external_metadata.get('apple_music'):
        apple_album = _album_name_from_provider(external_metadata['apple_music'])
        if apple_album and not _is_compilation_album(apple_album):
            album = apple_album
            logger.debug(f"🔊 ACRCloud: Found album from Apple Music: '{album}'")
        elif apple_album:
            logger.debug(f"🔊 ACRCloud: Skipping compilation album from Apple Music: '{apple_album}'")

    if not album:
        acr_album = music.get('album', {})
        logger.debug(f"🔊 ACRCloud: Checking ACRCloud's own album field: {acr_album}")
        if isinstance(acr_album, dict):
            album = (acr_album.get('name') or acr_album.get('title') or '').strip()
        elif isinstance(acr_album, str):
            album = acr_album.strip()
        if album:
            if album.lower() == title.lower() or album.lower() == artist.lower():
                logger.debug(f"🔊 ACRCloud: Ignoring own album value because it matches title/artist: '{album}'")
                album = ''
            elif _is_compilation_album(album):
                logger.debug(f"🔊 ACRCloud: Skipping compilation album from ACRCloud: '{album}'")
                album = ''
            else:
                logger.debug(f"🔊 ACRCloud: Falling back to ACRCloud album data: '{album}'")

    if not album:
        release_info = music.get('release', {})
        logger.debug(f"🔊 ACRCloud: Checking release info: {release_info}")
        if isinstance(release_info, dict):
            release_album = (release_info.get('name') or release_info.get('title') or '').strip()
            if release_album and not _is_compilation_album(release_album):
                album = release_album
                logger.debug(f"🔊 ACRCloud: Found album from release info: '{album}'")
            elif release_album:
                logger.debug(f"🔊 ACRCloud: Skipping compilation album from release info: '{release_album}'")

    # Final fallback: album should never be empty
    if not album:
        # Instead of defaulting to "Single", treat this as a failure so the next
        # service in the pipeline (audd.io, Shazamio, AcoustID) can be tried
        logger.debug(f"🔊 ACRCloud: No valid album info found after checking all sources, treating as identification failure")
        return None

    release_id = music.get('external_ids', {}).get('upc', '')

    logger.info(f"🔊 ACRCloud: Identified as: {artist} - {title}")
    logger.info(f"🔊 ACRCloud: Album extracted: '{album}'")
    # The recording's own code and length: a song the services hear only
    # as a sampled beat is told apart by them (see fix_metadata._sampled).
    isrc = (music.get('external_ids') or {}).get('isrc') or ''
    isrc = (isrc[0] if isinstance(isrc, list) and isrc else isrc if isinstance(isrc, str) else '').strip()
    length = round((music.get('duration_ms') or 0) / 1000) or None
    # Deezer's own track ID: looked up directly it works where Deezer's search
    # hides songs not licensed in the VPN's exit country.
    deezer_id = str((((music.get('external_metadata') or {}).get('deezer') or {}).get('track') or {}).get('id') or '')
    return {
        'artist': artist,
        'title': title,
        'album': album,
        'release_id': release_id,
        'cover_art_url': cover_art_url,
        'isrc': isrc,
        'duration': length,
        'deezer_id': deezer_id,
    }


def _identify_with_audd(file_path: str) -> Optional[Dict[str, str]]:
    """Attempt identification using audd.io via HTTPS POST.

    Requires AUDD_API_TOKEN environment variable.  Uses `requests` instead of
    the external library so the container doesn't need a dependency.
    """
    logger.debug(f"📤 audd.io: Starting identification for {file_path}")
    logger.debug(f"📤 audd.io: API token configured: {bool(AUDD_API_TOKEN)}")
    
    if not AUDD_API_TOKEN:
        logger.debug("📤 audd.io: API token not configured, skipping")
        return None

    # Validate that the file exists and is readable
    if not os.path.exists(file_path):
        logger.warning(f"📤 audd.io: Audio file not found: {file_path}")
        return None

    # send a short audio sample rather than the whole file to reduce failures
    logger.debug(f"📤 audd.io: Creating 10-second audio sample from {file_path}")
    sample_path = _create_audio_sample(file_path, duration=10)
    if not sample_path:
        logger.warning("📤 audd.io: could not create audio sample, skipping")
        return None

    logger.debug(f"📤 audd.io: Sample created at {sample_path} ({os.path.getsize(sample_path)} bytes)")
    
    result = None
    try:
        logger.debug(f"📤 audd.io: Sending HTTPS POST request to https://api.audd.io/")
        with open(sample_path, 'rb') as f:
            files = {'file': f}
            data = {'api_token': AUDD_API_TOKEN}
            logger.debug(f"📤 audd.io: POST data keys: {list(data.keys())}")
            resp = requests.post('https://api.audd.io/', data=data, files=files, timeout=30)
            logger.debug(f"📤 audd.io: Response status code: {resp.status_code}")
            logger.debug(f"📤 audd.io: Response headers: {dict(resp.headers)}")
            result = _json_utf8(resp)
            logger.debug(f"📤 audd.io: Response JSON: {result}")
    except requests.exceptions.Timeout:
        logger.warning(f"📤 audd.io: HTTP request timeout (30s)")
        result = None
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"📤 audd.io: Connection error: {e}")
        result = None
    except Exception as e:
        logger.warning(f"📤 audd.io: HTTP error: {type(e).__name__}: {e}")
        result = None
    finally:
        try:
            os.remove(sample_path)
        except Exception:
            pass

    if not result:
        logger.warning(f"📤 audd.io: No response received")
        return None

    response_status = result.get('status')
    logger.debug(f"📤 audd.io: Response status: {response_status}")
    
    if response_status != 'success':
        error_msg = result.get('error', {})
        if isinstance(error_msg, dict):
            error_msg = error_msg.get('error_message', 'Unknown error')
        logger.debug(f"📤 audd.io: Recognition failed - {error_msg}")
        return None

    match = result.get('result')
    if not match:
        logger.debug("📤 audd.io: No match found in response")
        return None

    artist = match.get('artist', '')
    title = match.get('title', '')
    album = match.get('album', '')

    logger.info(f"📤 audd.io: Identified as: {artist} - {title} ({album})")

    cover_art_url = ''
    release_id = ''
    if 'spotify' in match:
        spotify_data = match['spotify']
        if 'album' in spotify_data and 'images' in spotify_data['album']:
            images = spotify_data['album']['images']
            if images:
                cover_art_url = images[0].get('url', '')
                logger.debug(f"📤 audd.io: Got cover art from Spotify album")
        release_id = spotify_data.get('id', '')
        logger.debug(f"📤 audd.io: Got Spotify release ID: {release_id}")

    return {
        'artist': artist,
        'title': title,
        'album': album,
        'release_id': release_id,
        'cover_art_url': cover_art_url,
    }


def _identify_with_shazamio(file_path: str) -> Optional[Dict[str, str]]:
    """Attempt identification using Shazamio (unofficial Shazam).
    
    Note: This works both inside and outside async event loops by using
    a thread pool executor when needed.
    """
    # Avoid trying to fingerprint empty/too-small files (Shazamio and ACRCloud can't handle 0-byte input).
    try:
        file_size = os.path.getsize(file_path)
        if file_size < 1024:
            logger.warning(f"🎯 Shazamio: Skipping tiny file ({file_size} bytes): {file_path}")
            return None
    except Exception as e:
        logger.debug(f"🎯 Shazamio: Could not stat file: {e}")
        return None

    try:
        from shazamio import Shazam
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        import threading
        
        logger.debug(f"🎯 Shazamio: Starting identification for {file_path}")
        
        async def recognize():
            shazam = Shazam()
            logger.debug("🎯 Shazamio: Initialized Shazam client, calling recognize()...")
            import metadata_proxy
            out = await shazam.recognize(file_path, proxy=metadata_proxy.proxy_for("https://amp.shazam.com/"))
            logger.debug(f"🎯 Shazamio: Raw API response type: {type(out)}")
            return out
        
        # Try to run the async code properly depending on context
        try:
            # Check if we're in an event loop (Discord bot context)
            loop = asyncio.get_running_loop()
            logger.debug("🎯 Shazamio: Detected running event loop, using thread pool executor")
            
            # Run in a thread pool to avoid event loop conflicts
            with ThreadPoolExecutor(max_workers=1) as executor:
                def run_in_thread():
                    # Create a new event loop for this thread
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        return new_loop.run_until_complete(recognize())
                    finally:
                        new_loop.close()
                
                result = executor.submit(run_in_thread).result(timeout=30)
        except RuntimeError:
            # No event loop running, use asyncio.run() directly
            logger.debug("🎯 Shazamio: No running event loop, using asyncio.run()")
            result = asyncio.run(recognize())
        
        # Debug: Log the full response structure
        logger.debug(f"🎯 Shazamio: API Response keys: {result.keys() if result else 'None'}")
        
        if not result:
            logger.warning("🎯 Shazamio: API returned None/empty response")
            return None
        
        # Check if Shazam found any matches
        if 'matches' in result and not result.get('matches'):
            logger.debug(f"🎯 Shazamio: No matches found (matches array is empty)")
            logger.debug(f"🎯 Shazamio: This typically means the song is not in Shazam's database (indie/obscure artist) or audio quality is too poor")
            return None
        
        if 'track' not in result:
            matches_count = len(result.get('matches', []))
            logger.warning(f"🎯 Shazamio: No 'track' in response ({matches_count} partial match(es) found). Available keys: {list(result.keys())}")
            if matches_count == 0:
                logger.debug(f"🎯 Shazamio: No matches found - song likely not in Shazam's database or audio is unrecognizable")
            return None
        
        track = result['track']
        logger.debug(f"🎯 Shazamio: Track found! Track keys: {list(track.keys())}")
        
        artist = track.get('subtitle', '')  # Shazam uses 'subtitle' for artist
        title = track.get('title', '')
        
        logger.info(f"🎯 Shazamio: Identified as: {artist} - {title}")
        
        # Try to get album info from sections
        album = ''
        release_id = track.get('key', '')  # Shazam track key
        cover_art_url = ''
        
        # Get cover art
        if 'images' in track:
            cover_art_url = track['images'].get('coverart', '')
            logger.debug(f"🎯 Shazamio: Got cover art from 'images.coverart'")
        elif 'share' in track and 'image' in track['share']:
            cover_art_url = track['share']['image']
            logger.debug(f"🎯 Shazamio: Got cover art from 'share.image'")
        
        # Try to get album from metadata or sections
        if 'sections' in track:
            logger.debug(f"🎯 Shazamio: Found {len(track['sections'])} sections")
            for section in track['sections']:
                section_type = section.get('type', 'UNKNOWN')
                logger.debug(f"🎯 Shazamio: Section type: {section_type}")
                if section_type == 'SONG':
                    metadata = section.get('metadata', [])
                    logger.debug(f"🎯 Shazamio: Found SONG section with {len(metadata)} metadata items")
                    for item in metadata:
                        if item.get('title') == 'Album':
                            album = item.get('text', '')
                            logger.debug(f"🎯 Shazamio: Got album: {album}")
                            break
        else:
            logger.debug("🎯 Shazamio: No 'sections' in track response")
        
        logger.info(f"✅ Successfully identified track using Shazamio: {artist} - {title}")
        return {
            'artist': artist,
            'title': title,
            'album': album,
            'release_id': release_id,
            'cover_art_url': cover_art_url,
            'isrc': (track.get('isrc') or '').strip(),
        }
        
    except ImportError:
        logger.debug("🎯 Shazamio library not installed")
        return None
    except Exception as e:
        import traceback
        error_type = type(e).__name__
        error_msg = str(e)
        
        # Provide more context for specific errors
        if error_type == 'FailedDecodeJson':
            # Shazam API returned invalid/malformed JSON - usually temporary API issue
            logger.warning(
                f"🎯 Shazamio identification failed: {error_type} "
                f"(Shazam API returned malformed JSON - may be temporary service issue)"
            )
            logger.debug(f"   Error details: {error_msg}")
        else:
            logger.warning(f"🎯 Shazamio identification failed with exception: {error_type}: {error_msg}")
        
        logger.debug(f"🎯 Shazamio traceback: {traceback.format_exc()}")
        return None


def _identify_with_acoustid(file_path: str) -> Optional[Dict[str, str]]:
    """Attempt identification using AcoustID/MusicBrainz."""
    logger.debug(f"🔎 AcoustID: Starting identification for {file_path}")
    
    if not ACOUSTID_API_KEY:
        logger.debug("🔎 AcoustID: API key not configured, skipping")
        return None
    
    try:
        import acoustid
        import musicbrainzngs
    except ImportError:
        logger.debug("🔎 AcoustID: Libraries not available (acoustid/musicbrainzngs not installed)")
        return None

    try:
        logger.debug(f"🔎 AcoustID: Starting fingerprint analysis on {file_path}")
        # Use acoustid lookup helper which returns candidate recordings (as a generator)
        results = list(acoustid.match(ACOUSTID_API_KEY, file_path))  # Convert generator to list
        
        if not results:
            logger.debug(f"🔎 AcoustID: No matches found - song may not be in AcoustID/MusicBrainz database or audio quality too poor")
            return None
            
        logger.debug(f"🔎 AcoustID: Got {len(results)} match(es) from fingerprint")
        
        for idx, (score, rid, title, artists) in enumerate(results):
            logger.debug(f"🔎 AcoustID: Match {idx + 1} - Score: {score:.2f}, Title: {title}, Artists: {artists}")
            try:
                # Query MusicBrainz for recording/release metadata
                logger.debug(f"🔎 AcoustID: Querying MusicBrainz for recording ID: {rid}")
                musicbrainzngs.set_useragent("musicai", "0.1")
                rec = musicbrainzngs.get_recording_by_id(
                    rid, 
                    includes=["artists", "releases"]
                )['recording']
                logger.debug(f"🔎 AcoustID: Got MusicBrainz recording: {rec.get('title', '?')}")

                # Try to get artist from MusicBrainz, fallback to AcoustID
                artist_name = ''
                if rec.get('artist-credit'):
                    artist_list = []
                    for a in rec['artist-credit']:
                        if isinstance(a, dict):
                            artist_list.append(a.get('name', ''))
                    artist_name = ' '.join(artist_list).strip()
                    logger.debug(f"🔎 AcoustID: Got artist from MusicBrainz: {artist_name}")
                
                # If MusicBrainz extraction failed, use the artist from AcoustID
                if not artist_name and artists:
                    artist_name = artists
                    logger.debug(f"🔎 AcoustID: Using artist from AcoustID fingerprint: {artist_name}")
                
                title_name = rec.get('title', title or '')
                release_id = None
                album = ''
                cover_art_url = ''
                
                logger.debug(f"🔎 AcoustID: MusicBrainz recording response keys: {list(rec.keys())}")
                logger.debug(f"🔎 AcoustID: MusicBrainz full recording object: {rec}")
                
                if rec.get('release-list'):
                    logger.debug(f"🔎 AcoustID: Found releases: {len(rec['release-list'])} release(s)")
                    rel = rec['release-list'][0]
                    release_id = rel.get('id')
                    album = rel.get('title', '')
                    logger.debug(f"🔎 AcoustID: Found release: {album} (ID: {release_id})")
                    # Cover Art Archive URL fallback
                    if release_id:
                        cover_art_url = f"https://coverartarchive.org/release/{release_id}/front"
                        logger.debug(f"🔎 AcoustID: Cover art URL constructed")
                else:
                    logger.debug(f"🔎 AcoustID: No releases found in MusicBrainz data")

                logger.info(f"🔎 AcoustID: Identified as: {artist_name} - {title_name}")
                return {
                    'artist': artist_name,
                    'title': title_name,
                    'album': album,
                    'release_id': release_id,
                    'cover_art_url': cover_art_url,
                }
            except Exception as inner:
                logger.debug(f"🔎 AcoustID: Error fetching MusicBrainz data for id {rid}: {inner}")
                import traceback
                logger.debug(f"🔎 AcoustID: Inner exception traceback: {traceback.format_exc()}")
                continue
                
    except Exception as e:
        logger.warning(f"🔎 AcoustID: Fingerprinting failed with exception: {e}")
        import traceback
        logger.debug(f"🔎 AcoustID traceback: {traceback.format_exc()}")
        return None

    logger.debug("🔎 AcoustID: No successful matches after trying all results")
    return None


def audio_id_health_check() -> None:
    """Log the configuration/status of each audio identification service.

    This is intended to be called at startup of a workflow (e.g. from
    `cleanup_albums`) so users can immediately see which services are
    configured, which libraries are installed, and why a particular
    service might be skipped later.
    """
    logger.info("🔧 Audio-ID health check:")

    # ACRCloud (HTTPS-based)
    if all([ACRCLOUD_ACCESS_KEY, ACRCLOUD_ACCESS_SECRET, ACRCLOUD_HOST]):
        logger.info("  🔊 ACRCloud: credentials present (will use HTTPS API)")
        logger.info(f"       Host: {ACRCLOUD_HOST}")
        # Verify HTTPS connectivity and signature capability
        try:
            url = f"https://{ACRCLOUD_HOST}/v1/identify"
            # Try a HEAD request to verify the endpoint is reachable
            resp = requests.head(url, timeout=10)
            logger.info(f"  🔊 ACRCloud: HTTPS endpoint reachable ({resp.status_code})")
            
            # Verify signature generation works
            timestamp = str(int(time.time()))
            string_to_sign = "\n".join([
                "POST",
                "/v1/identify",
                "",
                "",
                ACRCLOUD_ACCESS_KEY,
                "",
                "",
                timestamp
            ])
            signature = base64.b64encode(
                hmac.new(
                    ACRCLOUD_ACCESS_SECRET.encode('utf-8'),
                    string_to_sign.encode('utf-8'),
                    hashlib.sha1
                ).digest()
            ).decode('utf-8')
            logger.info(f"  🔊 ACRCloud: HMAC-SHA1 signature generation OK")
            logger.info(f"  ✓ ACRCloud will work when an audio file is provided")
        except Exception as e:
            logger.warning(f"  🔊 ACRCloud: HTTPS health check failed: {e}")
    else:
        logger.info("  🔊 ACRCloud: credentials not configured")

    # audd.io (HTTPS-based)
    if AUDD_API_TOKEN:
        logger.info("  📤 audd.io: API token present (will use HTTPS API)")
        # Test HTTPS connectivity and token validation with a minimal POST request
        # (without a file, audd.io will return an error about missing file,
        #  but we can verify the token is at least accepted)
        try:
            # Use POST (not GET) since that's what the actual identify function uses
            resp = requests.post(
                'https://api.audd.io/',
                data={'api_token': AUDD_API_TOKEN},
                timeout=10,
            )
            data = _json_utf8(resp)
            
            if data.get('status') == 'error':
                error = data.get('error', {})
                if isinstance(error, dict):
                    err_msg = error.get('error_message', 'unknown')
                else:
                    err_msg = str(error)
                
                # Check for auth/token errors (different from "missing file" errors)
                err_lower = err_msg.lower()
                if 'invalid' in err_lower or 'key' in err_lower or 'token' in err_lower or 'auth' in err_lower:
                    logger.warning(f"  📤 audd.io: Token validation FAILED: {err_msg}")
                else:
                    # Other errors (like "file required") mean token is valid
                    logger.info(f"  ✓ audd.io: HTTPS endpoint reachable, token accepted")
                    logger.info(f"  ✓ audd.io will work when an audio file is provided")
            else:
                logger.info(f"  ✓ audd.io: Unexpected success response (token OK)")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"  📤 audd.io: Cannot reach HTTPS endpoint: {e}")
        except Exception as e:
            logger.warning(f"  📤 audd.io: health check failed: {e}")
    else:
        logger.info("  📤 audd.io: API token not configured")

    # Shazamio
    try:
        import shazamio  # type: ignore
        logger.info("  🎯 Shazamio: library installed")
    except ImportError:
        logger.warning("  🎯 Shazamio: library not installed - audio-id will be disabled")

    # AcoustID/MusicBrainz
    if ACOUSTID_API_KEY:
        try:
            import acoustid  # type: ignore
            import musicbrainzngs  # type: ignore
            logger.info("  🔎 AcoustID: API key set and libraries installed")
        except ImportError as e:
            logger.warning(f"  🔎 AcoustID: API key set but libraries missing ({e})")
    else:
        logger.info("  🔎 AcoustID: API key not configured")


def identify_track_by_audio(file_path: str) -> Optional[Dict[str, str]]:
    """Attempt to identify an audio file using multiple services with fallback.

    Tries services in order until one succeeds. Fingerprinting is done only once
    through the fallback pipeline to avoid waste.

    Services tried in this order:
    1. ACRCloud (provides album info)
    2. audd.io
    3. Shazamio
    4. AcoustID/MusicBrainz

    Returns metadata dict from first successful service, or None if all fail.
    """
    if not os.path.exists(file_path):
        # Check if it's a broken symlink - if so, remove it and return None
        if os.path.islink(file_path):
            try:
                os.remove(file_path)
                logger.warning(f"Removed broken symlink: {file_path}")
            except Exception as e:
                logger.error(f"File is a broken symlink and could not be removed: {file_path}: {e}")
        else:
            logger.error(f"File not found: {file_path}")
        return None
    
    # Dynamically build the pipeline based on available API keys
    services = []
    if all([ACRCLOUD_ACCESS_KEY, ACRCLOUD_ACCESS_SECRET, ACRCLOUD_HOST]):
        services.append(("ACRCloud", _identify_with_acrcloud))
    if AUDD_API_TOKEN:
        services.append(("audd.io", _identify_with_audd))
    services.append(("Shazamio", _identify_with_shazamio))
    if ACOUSTID_API_KEY:
        services.append(("AcoustID", _identify_with_acoustid))

    # attempt to integrate rate limiting support
    rate_limiter = None
    try:
        # import here to avoid circular dependency when audio_id is used standalone
        from rate_limit import get_rate_limiter
        rate_limiter = get_rate_limiter()
    except Exception:
        rate_limiter = None

    # If the preferred (first) service is currently rate-limited, wait
    if rate_limiter and services:
        first_name = services[0][0].lower().replace(' ', '').replace('.', '')
        delay = rate_limiter.get_delay(f'audio_{first_name}')
        if delay > 0:
            logger.info(f"⏳ Audio-ID service '{services[0][0]}' is rate limited, waiting {delay:.0f}s")
            rate_limiter.wait_if_needed(f'audio_{first_name}')

    # Try services in order until one succeeds
    for service_name, service_func in services:
        logger.info(f"Trying audio identification service: {service_name}...")
        key = f'audio_{service_name.lower().replace(" ","").replace(".","")}'
        max_retries = 3
        for attempt in range(max_retries + 1):
            # before each attempt check its own delay as well
            if rate_limiter:
                dl = rate_limiter.get_delay(key)
                if dl > 0:
                    logger.info(f"⏳ Waiting for audio-id service '{service_name}' ({dl:.0f}s)")
                    rate_limiter.wait_if_needed(key)
            try:
                result = service_func(file_path)
                if result and result.get('title'):
                    result['title'] = dedupe_feat(result['title'])
                if result:
                    logger.info(f"Successfully identified track using {service_name}: {result.get('artist', '')} - {result.get('title', '')} (album: {result.get('album', 'N/A')})")
                    return result
            except Exception as e:
                error_str = str(e).lower()
                is_rate_limit = ('rate limit' in error_str or 
                                'faileddecodejson' in error_str or  # Shazamio rate limit indicator
                                '429' in error_str or  # HTTP 429 Too Many Requests
                                'too many requests' in error_str)
                if is_rate_limit and rate_limiter and attempt < max_retries:
                    logger.warning(f"{service_name} appears rate limited (attempt {attempt+1}/{max_retries+1}), waiting before retry")
                    rate_limiter.on_rate_limit(key)
                    continue  # retry this service
                else:
                    logger.debug(f"{service_name} error: {e}")
                    break  # not rate limit or max retries, skip to next service
        # If this service failed all retries, continue to next service

    # All services failed
    logger.warning(f"All audio identification services failed for {file_path}")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Two-result API — used by fix_metadata.py
# ──────────────────────────────────────────────────────────────────────────────

def identify_track_by_audio_with_alternatives(
    file_path: str,
) -> "tuple[Optional[Dict[str, str]], Optional[Dict[str, str]]]":
    """Run ALL configured fingerprint services and return (best, next_best).

    Unlike ``identify_track_by_audio`` which stops at the first hit, this
    function continues querying every service so it can surface a *second*
    distinct result.  fix_metadata.py needs that second result for cases
    where the best match happens to agree with the (known-wrong) existing
    metadata.

    Rules
    -----
    best      — first non-None result from any service.
    next_best — first subsequent result that differs from *best* in at least
                one of {artist, title, album} (case-insensitive comparison).

    Both values may be None if fewer than two services returned usable results.
    """
    if not os.path.exists(file_path):
        # Check if it's a broken symlink - if so, remove it and return None
        if os.path.islink(file_path):
            try:
                os.remove(file_path)
                logger.warning(f"Removed broken symlink: {file_path}")
            except Exception as e:
                logger.error(f"File is a broken symlink and could not be removed: {file_path}: {e}")
        else:
            logger.error(f"File not found: {file_path}")
        return None, None

    # Build ordered service list (same priority as identify_track_by_audio)
    services = []
    if all([ACRCLOUD_ACCESS_KEY, ACRCLOUD_ACCESS_SECRET, ACRCLOUD_HOST]):
        services.append(("ACRCloud", _identify_with_acrcloud))
    if AUDD_API_TOKEN:
        services.append(("audd.io",  _identify_with_audd))
    services.append(("Shazamio", _identify_with_shazamio))
    if ACOUSTID_API_KEY:
        services.append(("AcoustID", _identify_with_acoustid))

    # Optional rate-limiter (as in identify_track_by_audio)
    rate_limiter = None
    try:
        from rate_limit import get_rate_limiter
        rate_limiter = get_rate_limiter()
    except Exception:
        pass

    def _rl_key(name: str) -> str:
        return f'audio_{name.lower().replace(" ", "").replace(".", "")}'

    def _differs(a: dict, b: dict) -> bool:
        """True when a and b differ in >= 1 of artist/title/album."""
        for field in ("artist", "title", "album"):
            if (a.get(field) or "").strip().lower() != (b.get(field) or "").strip().lower():
                return True
        return False

    best: Optional[Dict[str, str]]      = None
    next_best: Optional[Dict[str, str]] = None

    for service_name, service_func in services:
        key = _rl_key(service_name)
        if rate_limiter:
            delay = rate_limiter.get_delay(key)
            if delay > 0:
                logger.info(f"⏳ Audio-ID '{service_name}' rate-limited — waiting {delay:.0f}s")
                rate_limiter.wait_if_needed(key)

        try:
            result = service_func(file_path)
            if result and result.get('title'):
                result['title'] = dedupe_feat(result['title'])
        except Exception as exc:
            logger.debug(f"{service_name} error: {exc}")
            if rate_limiter and "rate limit" in str(exc).lower():
                rate_limiter.on_rate_limit(key)
            continue

        if result is None:
            continue

        if best is None:
            best = result
            logger.info(
                f"[audio_id] Best match ({service_name}): "
                f"{result.get('artist', '?')} – {result.get('title', '?')}"
            )
        elif next_best is None and _differs(best, result):
            next_best = result
            logger.info(
                f"[audio_id] Next-best match ({service_name}): "
                f"{result.get('artist', '?')} – {result.get('title', '?')}"
            )
            break   # we have both — no need to try more services

    if best is None:
        logger.info("[audio_id] with_alternatives: no matches from any service")
    elif next_best is None:
        logger.info(
            "[audio_id] with_alternatives: only one distinct result found "
            "(all responding services agreed on the same metadata)"
        )

    return best, next_best


# ── Several slices, majority vote ────────────────────────────────────────────

def _vote_key(answer: Dict[str, str]) -> tuple:
    import re
    import unicodedata

    def fold(value):
        text = unicodedata.normalize('NFKD', value or '').casefold()
        text = ''.join(c for c in text if not unicodedata.combining(c))
        text = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', text)
        text = re.sub(r'\s(?:feat\.?|ft\.?|featuring|with)\s.*$', '', text)
        return re.sub(r'[\W_]+', '', text)
    first_artist = re.split(r'\s*(?:,|&|/|;|\sx\s|\sfeat\.?\s|\sft\.?\s)\s*', answer.get('artist') or '', maxsplit=1)[0]
    return fold(first_artist), fold(answer.get('title'))


def identify_by_votes(file_path: str, slice_seconds: int = 20) -> "list[Dict[str, str]]":
    """Fingerprint several parts of the file and return the distinct answers,
    the most agreed-on first, each with a 'votes' count.

    One sample often matches an intro, a sample or a re-upload: the first
    seconds of Kiasmos' "Swept" match a Kings of Tomorrow song. Shazam is
    asked about three or four slices, ACRCloud about two, AcoustID about the
    whole file.
    """
    duration = None
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(file_path)
        duration = audio.info.length if audio else None
    except Exception:
        pass
    if not duration:
        best, next_best = identify_track_by_audio_with_alternatives(file_path)
        return [dict(a, votes=1) for a in (best, next_best) if a]
    offsets = sorted({0, 30, 60, max(0, int(duration // 2) - slice_seconds // 2)}) if duration > 80 else [0]
    answers: list = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, off in enumerate(offsets):
            seg = os.path.join(tmp, f'slice{off}.mp3')
            subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-ss', str(off), '-t', str(slice_seconds),
                            '-i', file_path, '-ac', '2', '-ar', '44100', '-b:a', '192k', seg], check=False)
            if not os.path.exists(seg):
                continue
            services = [('Shazamio', _identify_with_shazamio)]
            if i in (0, len(offsets) - 1):
                services.append(('ACRCloud', _identify_with_acrcloud))
            for name, func in services:
                try:
                    result = func(seg)
                except Exception as exc:
                    logger.debug(f"{name} slice @{off}s failed: {exc}")
                    result = None
                if result and result.get('artist') and result.get('title'):
                    result['title'] = dedupe_feat(result['title'])
                    answers.append(dict(result, service=name, offset=off))
    try:
        whole = _identify_with_acoustid(file_path)
        if whole and whole.get('artist') and whole.get('title'):
            answers.append(dict(whole, service='AcoustID', offset='whole'))
    except Exception as exc:
        logger.debug(f"AcoustID failed: {exc}")
    groups: Dict[tuple, Dict[str, str]] = {}
    for a in answers:
        key = _vote_key(a)
        if key not in groups:
            groups[key] = dict(a, votes=0)
        groups[key]['votes'] += 1
        if not groups[key].get('album') and a.get('album'):
            groups[key]['album'] = a['album']
        if not groups[key].get('cover_art_url') and a.get('cover_art_url'):
            groups[key]['cover_art_url'] = a['cover_art_url']
        for field in ('isrc', 'duration', 'deezer_id'):
            if not groups[key].get(field) and a.get(field):
                groups[key][field] = a[field]
    ranked = sorted(groups.values(), key=lambda g: -g['votes'])
    if not ranked:
        # Every fingerprint service came up empty: what is sung in it may
        # still name the song (lyrics_id; only when words are heard).
        try:
            import lyrics_id
            by_lyrics = lyrics_id.identify(file_path)
        except Exception as exc:
            logger.info(f"[audio_id] lyrics identification failed: {exc}")
            by_lyrics = None
        if by_lyrics:
            ranked = [by_lyrics]
    for g in ranked:
        logger.info(f"[audio_id] {g['votes']} vote(s): {g.get('artist')} – {g.get('title')} [{g.get('album') or ''}]")
    return ranked
