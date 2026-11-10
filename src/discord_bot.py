#!/usr/bin/env python3
from __future__ import annotations
"""
🤖 MUSIC AI DISCORD BOT
Multi-user Discord bot for music requests, metadata fixes, and status queries.

Features:
  - Request songs to download (linked to specific user library)
  - Fix metadata for existing tracks
  - Query system status and statistics
  - Multi-user aware (dynamically loads users from environment)
  - Request tracking and history
"""

import os
import sys
import logging
import asyncio
import glob
import cycle_schedule
import geo_relay
import json
import re
import warnings
import socket
import subprocess
# Suppress DeprecationWarning coming from 'discord.player' importing stdlib 'audioop'
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*audioop.*")
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from pathlib import Path
import time
import shutil
import tempfile

import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp

# Import Plex for library refresh
try:
    from plexapi.server import PlexServer
    PLEX_AVAILABLE = True
except ImportError:
    PLEX_AVAILABLE = False


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Global cleanup coordination state
# When a cleanup is running, `CLEANUP_RUNNING` is True and `CLEANUP_OWNER`
# contains the plex username. Any incoming requests in the music channel will
# be rejected with an error message that is tracked in
# `PENDING_REJECTION_MESSAGES` so we can delete them when the cleanup finishes.
CLEANUP_RUNNING = False
CLEANUP_OWNER: Optional[str] = None
# List[discord.Message] of bot-sent rejection messages to delete later
PENDING_REJECTION_MESSAGES: List[discord.Message] = []
PENDING_REJECTION_LOCK = asyncio.Lock()
# Import link extractor for YouTube/TikTok
try:
    from link_extractor import extract_link_metadata, extract_urls_from_text, parse_primary_artist
except ImportError:
    logger.warning("⚠️ link_extractor module not found; music_requests link extraction disabled")
    extract_link_metadata = None
    extract_urls_from_text = None

# Import downloader manager for lock-based immediate downloads
try:
    from downloader_manager import get_downloader_lock, get_downloader
except ImportError:
    logger.warning("⚠️ downloader_manager not available - immediate downloads disabled")
    get_downloader_lock = None
    get_downloader = None

# ═══════════════════════════════════════════════════════════════════════════════
# Helper: Plex connection helpers
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN', '')
MUSIC_BASE_DIR = os.getenv('MUSIC_BASE_DIR', '/music')
DATA_DIR = os.getenv('DATA_DIR', '/app/data')  # Must be absolute path for database lookups
# Main channel for all bot output: errors, cycle summaries, and music request confirmations
# Can be specified as numeric ID or as a channel name (e.g., 'music_requests')
_DISCORD_MAIN_CHANNEL_RAW = os.getenv('DISCORD_MAIN_CHANNEL', '').strip()

PROCESS_ROLE = os.getenv('PROCESS_ROLE', os.getenv('APP_USER_NAME', 'discord_bot'))
DISCORD_MAIN_CHANNEL = None
DISCORD_MAIN_CHANNEL_NAME = None
if _DISCORD_MAIN_CHANNEL_RAW:
    try:
        DISCORD_MAIN_CHANNEL = int(_DISCORD_MAIN_CHANNEL_RAW)
    except ValueError:
        DISCORD_MAIN_CHANNEL_NAME = _DISCORD_MAIN_CHANNEL_RAW

REQUESTS_DB = os.path.join(DATA_DIR, 'discord_requests.json')

import album_match
import request_history
import users as _users
from paths import host_to_container as convert_host_path_to_container_path

# Discord handle -> Plex username, admin first. Loaded from the Wizarr file.
DISCORD_TO_PLEX_MAPPING: dict = {}
DISCORD_TO_PLEX_MAPPING_ADMIN = os.getenv('DISCORD_TO_PLEX_MAPPING_ADMIN', '').strip()
# Public address of the ARL refresh page (arl_server.py), linked in the admin's alert.
ARL_PUBLIC_URL = os.getenv('ARL_PUBLIC_URL', '').strip().rstrip('/')
WIZARR_MAPPING_FILE = _users.mapping_file_path()

# Plex username -> library sub-folder. Every user shares the single library,
# so each maps to '' (the root of MUSIC_BASE_DIR).
USER_MAPPING: dict = {}


def _parse_admin_override(admin_override: str):
    return _users.parse_admin_override(admin_override)


def _load_mapping_from_file(path: str):
    return _users.load_wizarr_mapping(path)


def reload_wizarr_mapping():
    """Reload DISCORD_TO_PLEX_MAPPING from the Wizarr file.

    Returns True if a mapping was loaded, False if the file is missing/empty,
    or None if DISCORD_TO_PLEX_MAPPING_ADMIN matches nothing in the file.
    """
    global DISCORD_TO_PLEX_MAPPING, USER_MAPPING
    mapping = _users.load_wizarr_mapping(WIZARR_MAPPING_FILE)
    if mapping is None:
        return None
    if mapping:
        DISCORD_TO_PLEX_MAPPING = mapping
        USER_MAPPING = {plex: '' for plex in mapping.values()}
        logger.info(f"🎵 Loaded Discord->Plex mapping from {WIZARR_MAPPING_FILE}: {DISCORD_TO_PLEX_MAPPING}")
        return True
    return False


if reload_wizarr_mapping() is None:
    logger.error(
        "❌ Wizarr mapping file contains an invalid DISCORD_TO_PLEX_MAPPING_ADMIN override. "
        "Please fix plex_invite_targets.json or DISCORD_TO_PLEX_MAPPING_ADMIN."
    )
    sys.exit(1)
if not DISCORD_TO_PLEX_MAPPING:
    logger.info("🎵 No Discord->Plex mapping loaded at import time")


def _canonical_cycle_user(user: str | None) -> str:
    """Normalize a cycle user to a canonical Plex username for status tracking."""
    return _users.canonical_plex_user(user, DISCORD_TO_PLEX_MAPPING)


def _get_known_cycle_users() -> list[str]:
    """All Plex usernames the status report should account for."""
    users = {u for u in DISCORD_TO_PLEX_MAPPING.values() if u}
    users.update(_canonical_cycle_user(k) for k in _CYCLE_USER_STATE.keys() if k)
    return sorted(u for u in users if u)


def infer_plex_user_index(plex_user: str) -> int:
    """Index of a Plex user in the mapping (admin is 0)."""
    return _users.plex_user_index(plex_user, DISCORD_TO_PLEX_MAPPING)

# Onboarding messages are left to other tools; on member join MusicAI only
# reloads the Wizarr mapping so its Discord->Plex mapping stays current.


async def block_interaction_if_cleanup(interaction: discord.Interaction) -> bool:
    """True (after telling the user, privately) when a download cycle or a
    library cleanup is running and the interaction must wait."""
    try:
        _block_reason = None
        if CYCLE_RUNNING:
            _block_reason = (
                f"⏳ A download cycle is currently running for **{CYCLE_USER or 'the system'}**. "
                "Song requests are temporarily blocked until the cycle completes."
            )
        elif CLEANUP_RUNNING:
            _block_reason = (
                f"⏳ A library cleanup is currently running for **{CLEANUP_OWNER or 'another user'}**. "
                "All new requests are temporarily blocked until cleanup finishes."
            )
        if _block_reason:
            note = _block_reason
            try:
                await interaction.response.send_message(note, ephemeral=True)
            except Exception:
                # Already deferred: the followup is private too.
                try:
                    await interaction.followup.send(note, ephemeral=True)
                except Exception:
                    pass
            return True
    except Exception:
        logger.debug("Error while blocking interaction due to cleanup")
    return False

# Store request history
REQUEST_HISTORY: Dict[str, List[Dict]] = {}

# Counters for Discord image-based metadata fixes (updated by bot at runtime).
# Separate from the Plex 'Fix Metadata' playlist, which fix_metadata_daemon.py
# processes daily.
METADATA_FIXES_PROCESSED = 0
METADATA_FIX_ERRORS = 0


def normalize_artist(artist: str) -> str:
    """Normalize an artist string by keeping only the primary artist.

    Splits on common delimiters like `;`, `/`, and `,` and removes any
    trailing feature sections like "feat." or "ft.". Returns the first
    artist, stripped.
    """
    if not artist:
        return artist
    try:
        # Split on common multi-artist delimiters and take the first part
        parts = re.split(r"\s*[;/,]\s*", artist)
        primary = parts[0].strip()

        # Remove feature annotations (e.g., "Artist X feat. Y")
        primary = re.split(r"\s+(?:feat\.?|featuring|ft\.?|&|and)\b", primary, flags=re.IGNORECASE)[0].strip()

        # Strip surrounding quotes and excessive whitespace
        primary = primary.strip('"\'')
        return primary
    except Exception:
        return artist


def sanitize_filename(name: str) -> str:
    """Make a simple filesystem-safe filename from a title or artist.

    Minimal sanitization: replace path separators and control characters,
    collapse repeated whitespace, and strip leading/trailing whitespace.
    """
    if not name:
        return name
    try:
        # Replace path separators and other problematic chars
        s = name.replace('/', '-').replace('\\', '-').replace(':', ' - ')
        # Remove null bytes and control characters
        s = re.sub(r'[\x00-\x1f]', '', s)
        # Collapse multiple spaces
        s = re.sub(r'\s+', ' ', s).strip()
        return s
    except Exception:
        return name



# ═══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def load_request_history():
    """Load request history from disk."""
    global REQUEST_HISTORY
    if os.path.exists(REQUESTS_DB):
        try:
            with open(REQUESTS_DB, 'r') as f:
                REQUEST_HISTORY = json.load(f)
                logger.info(f"📂 Loaded {len(REQUEST_HISTORY)} request histories from {REQUESTS_DB}")
        except Exception as e:
            logger.warning(f"⚠️ Could not load request history from {REQUESTS_DB}: {e}")
            REQUEST_HISTORY = {}
    else:
        logger.debug(f"📂 Request database not found at {REQUESTS_DB}, starting fresh")
        REQUEST_HISTORY = {}

def get_user_library(username: str) -> Optional[str]:
    """Get music library path for user.

    Checks the normal MUSIC_BASE_DIR first, but will prefer a user dir under
    DATA_DIR (used by tests and some deployments) if it exists.
    
    Accepts either a folder name (from USER_MAPPING keys/values) or a Plex username.
    For unified library mode (empty folder name), returns MUSIC_BASE_DIR.
    """
    # First, try direct lookup in USER_MAPPING
    if username in USER_MAPPING:
        folder = USER_MAPPING[username]
    else:
        folder = None

    # If not found, try to find the folder by matching Plex username
    # (a Plex username given instead of a Discord handle)
    if folder is None:
        if username in USER_MAPPING.values():
            folder = username
        else:
            logger.warning(f"⚠️ Unknown user: {username}")
            return None

    # Handle unified library mode (empty folder name)
    if not folder or folder == "":
        logger.debug(f"📁 Unified library mode for '{username}', using MUSIC_BASE_DIR: {MUSIC_BASE_DIR}")
        return MUSIC_BASE_DIR

    primary = os.path.join(MUSIC_BASE_DIR, folder)
    alt = os.path.join(os.environ.get('DATA_DIR', DATA_DIR), folder)
    # Prefer alt if it exists (useful for tests where DATA_DIR is tmpdir)
    if os.path.exists(alt):
        logger.debug(f"📁 Using DATA_DIR-based user library for '{username}': {alt}")
        return alt
    logger.debug(f"📁 User '{username}' library path: {primary}")
    return primary


def get_user_albums_dir(username: str) -> Optional[str]:
    """Return the user-specific albums directory for a Plex username."""
    library_root = get_user_library(username)
    if not library_root:
        return None
    return os.path.join(library_root, 'albums')


def track_request(username: str, artist: str, title: str, request_type: str, **kwargs) -> Dict:
    """Track a user request.
    
    Args:
        username: Username for tracking
        artist: Artist name
        title: Song title
        request_type: Type of request (e.g., 'download', etc.)
        **kwargs: Additional fields to store (e.g., album, extracted_metadata, etc.)
    """
    global REQUEST_HISTORY
    request_entry = {
        'artist': artist,
        'title': title,
        'type': request_type,
        'timestamp': datetime.now().isoformat(),
        'status': 'pending',
    }
    
    # Add any additional fields
    request_entry.update(kwargs)

    try:
        with request_history.editing(REQUESTS_DB) as history:
            history.setdefault(username, []).append(request_entry)
        REQUEST_HISTORY = history
    except (OSError, ValueError) as e:
        logger.error(f"❌ Could not save request history to {REQUESTS_DB}: {e}")
    logger.debug(f"📝 Tracked request for user '{username}': {artist} - {title} ({request_type})")

    return request_entry


def record_request_outcome(username: str, entry: Dict, mode: str, result: Optional[Dict]) -> None:
    """Note how the bot's own attempt went, so the history still says what
    happened once the logs are gone. A failed attempt stays pending: the next
    cycle tries it again."""
    global REQUEST_HISTORY
    details = result if isinstance(result, dict) else {}
    if mode == 'immediate':
        fields = {'status': 'completed', 'result': f"Downloaded: {details.get('file_path')}"}
    elif mode == 'existing':
        fields = {'status': 'completed', 'result': f"Already in the library: {details.get('file_path')}"}
    elif details.get('error'):
        fields = {'result': f"Attempt failed, will retry next cycle: {details['error']}"}
    else:
        fields = {'result': 'Queued for the next cycle'}
    try:
        REQUEST_HISTORY = request_history.update(REQUESTS_DB, username, entry, **fields)
    except (OSError, ValueError) as e:
        logger.error(f"❌ Could not save request history to {REQUESTS_DB}: {e}")


async def try_immediate_download(plex_user: str, artist: str, title: str, album: str, url: str, source: str,
                                 request_context: str = None, discord_user: str = None):
    """Try to acquire the singleton downloader lock and download immediately.

    A Discord request should only queue while an app.py cycle is actively using
    the shared downloader. Once the cycle has stopped, the bot is allowed to
    access the managed singleton via downloader_manager.py and perform the
    immediate download itself.

    Returns a 3-tuple: (success: bool, mode: str, result: dict or None)
    mode can be: 'immediate' (downloaded), 'existing' (file already existed),
    'queue' (queued), 'lock_timeout', etc. The result is the dict returned by
    `downloader.download_track` when available. When the AI is unsure which
    search result is the song, `discord_user` is asked in their thread
    (request_choice).
    """
    if not get_downloader_lock or not get_downloader:
        logger.debug("Downloader manager not available, cannot attempt immediate download")
        return False, "queue", None


    # If an app.py download cycle is running, do not steal the singleton
    # downloader. In that case queueing is the correct behavior.
    if CYCLE_RUNNING or _any_cycle_running_file():
        logger.info(f"Download cycle in progress — queuing {artist} - {title} for next cycle")
        return False, "queue", None

    # The bot process uses the downloader_manager singleton when no cycle is
    # active, so a request is downloaded right away by the same code a cycle
    # uses, instead of waiting in the queue for the next cycle.
    target_dir = get_user_albums_dir(plex_user) or os.environ.get('ALBUM_DIR')
    user_index = infer_plex_user_index(plex_user)
    logger.info(
        f"Attempting immediate download via downloader_manager for {artist} - {title} "
        f"(user={plex_user}, target_dir={target_dir}, user_index={user_index}, role={PROCESS_ROLE})"
    )

    try:
        # Use async_acquire so we never block the event loop / Discord heartbeat.
        lock = get_downloader_lock()
        if not await lock.async_acquire(timeout=30):
            logger.warning(f"Could not acquire downloader lock after 30s timeout")
            return False, "lock_timeout", None
        
        logger.info(f"🔓 Acquired downloader lock - attempting immediate download for {artist} - {title}")
        
        try:
            # Try to get the downloader singleton (should be initialized by app.py by now)
            try:
                downloader = get_downloader()
            except Exception as e:
                logger.debug(f"Downloader not initialized yet: {e}")
                return False, "queue", None
            
            if not downloader:
                logger.debug("Downloader is None (not initialized by app.py yet)")
                return False, "queue", None
            
            # Attempt to download the track
            logger.info(f"📥 Starting immediate download: {artist} - {title}")
            try:
                # Run the blocking download in a thread pool to avoid blocking the event loop
                import concurrent.futures
                loop = asyncio.get_event_loop()
                import request_choice

                def download_asking(*args):
                    with request_choice.asking(discord_user) as requester:
                        result = downloader.download_track(*args)
                        if requester is not None and requester.notes and isinstance(result, dict):
                            result['choices'] = list(requester.notes)
                        return result

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    result = await loop.run_in_executor(
                        executor,
                        download_asking,
                        artist,
                        title,
                        album,
                        url,
                        f'music_request_{source}',
                        plex_user,
                        target_dir,
                        user_index,
                        request_context,
                    )
                
                if result and result.get('success'):
                    logger.info(f"✅ Immediate download succeeded: {artist} - {title}")
                    # An explicit request wins over an earlier dislike or deletion:
                    # left on a blacklist, the song would be purged by the next cycle.
                    try:
                        import library_review
                        if library_review.unblacklist(title, artist):
                            logger.info(f"   🔓 Requested again, off the blacklist: {artist} - {title}")
                    except Exception as exc:
                        logger.warning(f"Could not take {artist} - {title} off the blacklist: {exc}")
                    logger.info(f"   📁 File: {result.get('file_path')}")
                    logger.info(f"   🛠️ Tool: {result.get('download_tool')}")
                    logger.info(f"   📊 Metadata: {result.get('metadata', {}).get('source')}")

                    # Refresh Plex library
                    try:
                        await refresh_plex_library(plex_user)
                    except Exception as e:
                        logger.debug(f"Plex refresh failed: {e}")

                    # If downloader indicated the track already existed and no download/tool was used,
                    # return a special mode so callers can announce that no download was performed.
                    download_tool = result.get('download_tool')
                    metadata_src = result.get('metadata', {}).get('source') if result.get('metadata') else None
                    if download_tool == 'none' and metadata_src == 'existing':
                        return True, "existing", result

                    return True, "immediate", result
                else:
                    error_msg = result.get('error', 'Unknown error') if result else 'No result returned'
                    logger.warning(f"⚠️ Immediate download failed: {error_msg}")
                    return False, "queue", result
            
            except Exception as e:
                logger.warning(f"⚠️ Download error: {e}", exc_info=True)
                return False, "queue", None
        
        finally:
            # Always release the lock
            try:
                lock.release()
                logger.debug(f"🔒 Released downloader lock")
            except Exception as e:
                logger.debug(f"Error releasing lock: {e}")
    
    except Exception as e:
        logger.debug(f"Exception during immediate download attempt: {e}")
        return False, "queue", None


async def try_lidarr_playlist_download(playlist_name: str, tracks: list) -> tuple[bool, Optional[dict]]:
    """Start a whole-album Lidarr search when a playlist matches an album."""
    try:
        from lidarr_catalog import lidarr_instances
        instances = lidarr_instances()
        if not instances:
            return False, None

        from downloader import LidarrClient
        client = LidarrClient(*instances[0])
        match = await asyncio.to_thread(client.find_album_for_playlist, playlist_name, tracks)
        if not match:
            logger.info(f"📋 Playlist '{playlist_name}' did not match a Lidarr album")
            return False, None

        started = await asyncio.to_thread(client.download_album, match['artist'], match['album'])
        if started:
            logger.info(
                f"✅ Lidarr whole-album search started: {match['artist']} - {match['album']}"
            )
            return True, match
    except Exception as exc:
        logger.warning(f"Could not try Lidarr playlist album download: {exc}")
    return False, None


async def _resolve_configured_channel(channel_id=None, channel_name=None):
    """Resolve a channel by numeric ID or by name across guilds.

    Returns a channel object or None if it cannot be resolved yet.
    """
    if not bot:
        return None
    resolved = None
    if channel_id:
        try:
            resolved = bot.get_channel(channel_id)
            if resolved:
                logger.debug(f"Resolved Discord main channel by ID: {channel_id} -> {resolved.name} ({resolved.id})")
                return resolved
        except Exception as e:
            logger.debug(f"Could not resolve Discord channel by ID {channel_id}: {e}")

        try:
            resolved = await bot.fetch_channel(channel_id)
            if resolved:
                logger.debug(f"Fetched Discord main channel by ID: {channel_id} -> {resolved.name} ({resolved.id})")
                return resolved
        except discord.NotFound:
            logger.debug(f"Discord channel ID {channel_id} was not found via fetch_channel")
        except discord.Forbidden:
            logger.debug(f"Discord channel ID {channel_id} is forbidden to fetch")
        except Exception as e:
            logger.debug(f"Could not fetch Discord channel by ID {channel_id}: {e}")
    if channel_name:
        try:
            normalized_name = channel_name.strip().lstrip('#').lower()
            for ch in bot.get_all_channels():
                if hasattr(ch, 'name') and ch.name and ch.name.lower() == normalized_name:
                    logger.debug(
                        f"Resolved Discord main channel by name: {channel_name} -> {ch.name} ({getattr(ch, 'id', 'unknown')}) in guild {getattr(getattr(ch, 'guild', None), 'name', 'unknown')}"
                    )
                    return ch
        except Exception as e:
            logger.debug(f"Could not resolve Discord channel by name {channel_name}: {e}")
    logger.warning(f"Unable to resolve Discord main channel (ID: {channel_id}, name: {channel_name}). Ensure the bot has access to the guild/channel and the channel name is exact.")
    return None


async def send_error_to_discord(error_message: str, delete_after: int = 300):
    """Tell the admin about an error, privately (see _send_private_admin_message)."""
    if len(error_message) > 1900:
        error_message = error_message[:1900] + "..."
    await _send_private_admin_message(f"❌ **Error**\n```\n{error_message}\n```")


async def send_cycle_summary(cycle_stats: Dict, delete_after: int = 300) -> None:
    """Update the shared cycle status message with the final cycle summary."""
    if not (DISCORD_MAIN_CHANNEL or DISCORD_MAIN_CHANNEL_NAME) or not bot:
        logger.debug("Discord main channel not configured, skipping cycle summary")
        return

    try:
        channel = await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)
        if not channel:
            logger.warning(f"Could not resolve Discord channel for cycle summary (ID: {DISCORD_MAIN_CHANNEL}, name: {DISCORD_MAIN_CHANNEL_NAME})")
            return

        user = cycle_stats.get('user', 'Unknown')
        downloads = cycle_stats.get('downloads', {})
        total_downloads = downloads.get('total', 0)
        metadata_fixes = cycle_stats.get('metadata_fixes', 0)
        errors = cycle_stats.get('errors', {})
        error_list = errors.get('list', [])
        library_stats = cycle_stats.get('library_stats', {})

        if user not in _CYCLE_USER_STATE:
            _CYCLE_USER_STATE[user] = {}

        _CYCLE_USER_STATE[user].update({
            'running': False,
            'phase': 'Cycle complete',
            'detail': (
                'No downloads performed' if total_downloads == 0
                else f'{total_downloads} track(s) downloaded'
            ),
            'stats': {
                'downloads': downloads,
                'metadata_fixes': metadata_fixes,
                'errors': errors,
                'library_stats': library_stats,
            },
            'complete_info': {
                'total_downloaded': total_downloads,
                'next_cycle_hours': cycle_stats.get('next_cycle_hours', 0.0),
            },
        })

        embed = _build_cycle_status_embed()
        await _ensure_cycle_status_message(channel, embed)
        logger.debug(f"Cycle summary updated in shared Discord message for user {user}")

    except Exception as e:
        logger.warning(f"Could not update cycle summary in Discord: {e}")

async def send_cycle_error(error_message: str, cycle_phase: str = "Analysis", delete_after: int = 300) -> None:
    """Update the shared cycle status message with the latest cycle error."""
    if not (DISCORD_MAIN_CHANNEL or DISCORD_MAIN_CHANNEL_NAME) or not bot:
        logger.debug(f"Discord main channel not configured, error in {cycle_phase}: {error_message}")
        return

    try:
        channel = await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)
        if not channel:
            logger.warning(f"Could not resolve Discord channel for cycle error (ID: {DISCORD_MAIN_CHANNEL}, name: {DISCORD_MAIN_CHANNEL_NAME})")
            return

        target_user = None
        running_users = [user for user, state in _CYCLE_USER_STATE.items() if state.get('running')]
        if running_users:
            target_user = running_users[0]
        elif _CYCLE_USER_STATE:
            target_user = sorted(_CYCLE_USER_STATE)[0]
        else:
            target_user = "System"

        if target_user not in _CYCLE_USER_STATE:
            _CYCLE_USER_STATE[target_user] = {}
        state = _CYCLE_USER_STATE[target_user]
        state.update({
            'running': True,
            'phase': f'{cycle_phase} error',
            'detail': error_message[:240],
            'stats': state.get('stats', {'downloads': {'total': 0}}),
        })

        embed = _build_cycle_status_embed()
        await _ensure_cycle_status_message(channel, embed)
        logger.debug(f"Cycle error updated in shared Discord message for {cycle_phase}")
    except Exception as e:
        logger.warning(f"Could not update cycle error in Discord: {e}")

def count_user_tracks(username: str) -> Tuple[int, int, int]:
    """(new, all, soon to go) songs in the user's library. Every song lives
    in albums/; which are new or soon to go library_state keeps, as symlinks
    outside the library.

    Supports both per-user and unified library structures:
    - Per-user: MUSIC_BASE_DIR/{user folder}/albums
    - Unified: MUSIC_BASE_DIR/albums
    """
    import library_state
    user_folder = USER_MAPPING.get(username)
    if not user_folder:
        if username in USER_MAPPING.values():
            user_folder = username
        else:
            logger.warning(f"⚠️ User '{username}' not found in USER_MAPPING, checking unified library")
            user_folder = None
    albums_dir = os.path.join(MUSIC_BASE_DIR, user_folder, 'albums') if user_folder \
        else os.path.join(MUSIC_BASE_DIR, 'albums')
    songs = new = soon_to_go = 0
    try:
        for root, dirs, files in os.walk(albums_dir):
            songs += sum(1 for f in files if f.lower().endswith(library_state.AUDIO_EXTENSIONS))
        states = library_state.counts()
        new, soon_to_go = states[library_state.PROBATION], states[library_state.SOON_TO_GO]
        logger.debug(f"📊 User '{username}' library stats: {songs} songs, {new} new, {soon_to_go} soon to go")
    except Exception as e:
        logger.error(f"❌ Error counting tracks for user '{username}': {e}")
    return new, songs, soon_to_go

def get_recent_requests(username: str, limit: int = 5) -> List[Dict]:
    """Get recent requests for user."""
    load_request_history()      # a cycle may have settled some since
    if username not in REQUEST_HISTORY:
        logger.debug(f"📝 No request history found for user '{username}'")
        return []
    
    recent = REQUEST_HISTORY[username][-limit:]
    logger.debug(f"📝 Retrieved {len(recent)} recent requests for user '{username}'")
    return recent


def music_section_name(plex_sections_env: str = None) -> str:
    """Name of the Plex music library.

    Every user shares one library. PLEX_MUSIC_SECTIONS may still list several
    colon-separated names; a "Music" entry wins, then the only distinct name.
    """
    env_sections = (plex_sections_env if plex_sections_env is not None else os.environ.get('PLEX_MUSIC_SECTIONS', '')).strip()
    sections_list = [sec.strip() for sec in env_sections.split(':') if sec.strip()]
    for section in sections_list:
        if section.lower() == 'music':
            return section
    unique_sections = list(dict.fromkeys(sections_list))
    if len(unique_sections) == 1:
        return unique_sections[0]
    return 'Music'


def find_music_section(plex):
    """The Plex artist-type library section to use, or None."""
    target = music_section_name()
    artist_sections = [sec for sec in plex.library.sections() if sec.type == 'artist']
    for sec in artist_sections:
        if sec.title == target or sec.title.lower() == 'music':
            return sec
    if artist_sections:
        if len(artist_sections) > 1:
            logger.warning(f"No Plex music library named '{target}'; using '{artist_sections[0].title}'")
        return artist_sections[0]
    return None


def label_album_tracks(tracks: list, album: dict) -> None:
    """Give a playlist's songs the album's names: its artist, its title and
    the Deezer track each song is. A song not on the album keeps its own."""
    for index, track in enumerate(tracks):
        found = album['tracks'].get(index)
        if found:
            track.update(artist=album['artist'], title=found['title'], album=album['album'],
                         deezer_track=found['id'])


def playlist_track_context(source: str, track: dict) -> str:
    """What the downloader is told about one playlist song: its own link (a
    YouTube video comes before searching SoundCloud, and is the recording
    meant), its album and the Deezer track it is."""
    parts = [f"source={source}_playlist"]
    if track.get('album'):
        parts.append(f"album={track['album']}")
    if track.get('url'):
        parts.append(f"url={track['url']}")
    if track.get('deezer_track'):
        parts.append(f"deezer_track={track['deezer_track']}")
    return '; '.join(parts)


def playlist_matches_own_album(playlist_name: str, downloaded_tracks: list) -> bool:
    """Detect a Spotify/YouTube "playlist" that's really just an album (e.g. someone
    shared an artist's album as a playlist, named after that album) so callers can
    skip creating a redundant same-named Plex playlist — the tracks already get
    grouped as that album in the library.

    Requires most tracks (>=70%) to share one resolved album, and that album name
    to closely match the playlist name (fuzzy ratio >=0.82, ignoring case/punctuation).
    """
    albums = [t.get('album', '').strip() for t in downloaded_tracks if t.get('album', '').strip()]
    if not albums:
        return False
    common_album = max(set(albums), key=albums.count)
    consistency = albums.count(common_album) / len(downloaded_tracks)
    if consistency < 0.7:
        return False

    def _norm(s: str) -> str:
        return re.sub(r'[^a-z0-9]', '', s.lower())

    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, _norm(common_album), _norm(playlist_name)).ratio()
    return ratio >= 0.82


async def refresh_plex_library(plex_user: str):
    """Ask Plex to rescan the shared music library after a download."""
    if not PLEX_AVAILABLE:
        logger.debug("PlexAPI not available, skipping library refresh")
        return

    plex_url = os.environ.get('PLEX_URL', '').strip()
    if not plex_url:
        logger.debug("PLEX_URL not configured, skipping refresh")
        return

    def do_refresh():
        try:
            import plex_auth
            plex = PlexServer(plex_url, plex_auth.admin_token())
            music_lib = find_music_section(plex)
            if music_lib is None:
                logger.error(f"❌ No music library found in Plex (requested by {plex_user})")
                return
            music_lib.update()
            logger.info(f"✅ Plex library refresh triggered for '{music_lib.title}'")
        except Exception as exc:
            logger.error(f"❌ Plex refresh error: {exc}", exc_info=True)

    try:
        import concurrent.futures
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            await loop.run_in_executor(executor, do_refresh)
    except Exception as exc:
        logger.error(f"❌ Error during Plex refresh: {exc}", exc_info=True)



# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# DISCORD BOT INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
# Enable message content intent for reading message content
# NOTE: message_content is a PRIVILEGED intent and must be explicitly enabled 
# in the Discord Developer Portal: https://discord.com/developers/applications
# If not enabled there, comment out the line below and the bot will still work
# but won't be able to read message content from users
intents.message_content = True
intents.guilds = True
intents.guild_messages = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    """Bot ready event."""
    logger.info(f"✅ Discord bot logged in as {bot.user}")
    logger.info(f"🤖 Bot is ready to receive commands in {len(bot.guilds)} guild(s)")
    # Start the ARL stale alert polling task
    try:
        if not check_arl_and_notifications.is_running():
            check_arl_and_notifications.start()
            logger.info("Started check_arl_and_notifications background task")
    except Exception as _e:
        logger.warning(f"Could not start ARL check task: {_e}")

    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s) to Discord")
        for cmd in synced:
            logger.debug(f"  ✓ {cmd.name}: {cmd.description}")
    except Exception as e:
        logger.error(f"❌ Failed to sync commands: {e}")

    # Log resolved channels for debugging when configured by name or ID
    try:
        if DISCORD_MAIN_CHANNEL or DISCORD_MAIN_CHANNEL_NAME:
            resolved = await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)
            if resolved:
                logger.info(f"🔔 Resolved DISCORD_MAIN_CHANNEL to #{resolved.name} ({resolved.id}) in guild {resolved.guild.name}")
            else:
                logger.warning(f"⚠️ Could not resolve DISCORD_MAIN_CHANNEL (ID: {DISCORD_MAIN_CHANNEL}, name: {DISCORD_MAIN_CHANNEL_NAME}). Ensure the bot has access to the guild/channel and the name is exact.")
                try:
                    guild_info = []
                    for g in bot.guilds:
                        # report guild name and number of text channels to help debugging
                        ch_count = sum(1 for _ in g.channels if getattr(_, 'type', None) == discord.ChannelType.text or getattr(_, 'type', None) == discord.ChannelType.text)
                        guild_info.append(f"{g.name} (id={g.id}, channels={ch_count})")
                    logger.info(f"Bot is connected to guilds: {guild_info}")
                except Exception as _gerr:
                    logger.debug(f"Could not enumerate bot guilds for debug: {_gerr}")
    except Exception as e:
        logger.debug(f"Could not resolve DISCORD_MAIN_CHANNEL at startup: {e}")

    # Restore what each user's cycle last reported before this restart, so a
    # redeploy/crash doesn't make the status message wrongly claim nobody has
    # any recent cycle activity (see _CYCLE_STATE_PERSIST_FILE).
    try:
        _load_cycle_user_state()
    except Exception as _e:
        logger.warning(f"Could not load persisted cycle status state: {_e}")

    # Bring the existing status message up to date in place. A restart must
    # not post anything new: the message is only re-sent when a request has
    # pushed it off the bottom of the channel, and that is handled by
    # _ensure_cycle_status_message itself.
    try:
        if DISCORD_MAIN_CHANNEL or DISCORD_MAIN_CHANNEL_NAME:
            channel = await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)
            if channel:
                try:
                    embed = _build_cycle_status_embed()
                    await _ensure_cycle_status_message(channel, embed, allow_move=False)
                    logger.info("Cycle status message verified on startup")
                except Exception as _e:
                    logger.warning(f"Could not verify cycle status message on startup: {_e}")
    except Exception:
        pass

    await _share_request_threads_with_admin()
    await _tidy_requests_channel()

# ---- ARL stale alert background task ----------------------------------------
# Polls DATA_DIR/arl_alert.json every 60 s.  When app.py detects a stale ARL it
# writes this file; we read it, send one admin message, then delete the file so
# the alert is only sent once per stale event.
_ARL_ALERT_FILE       = os.path.join(DATA_DIR, 'arl_alert.json')
_ARL_STALE_FLAG       = os.path.join(DATA_DIR, 'arl_stale.flag')
_NOTIF_FILE           = os.path.join(DATA_DIR, 'discord_notifications.jsonl')  # error queue
# Each app.py cycle writes cycle_running_{user}.json / cycle_complete_{user}.json.
_CYCLE_RUNNING_GLOB   = os.path.join(DATA_DIR, 'cycle_running_*.json')
_CYCLE_COMPLETE_GLOB  = os.path.join(DATA_DIR, 'cycle_complete_*.json')


def _any_cycle_running_file() -> bool:
    """True if any user's cycle_running file exists on disk right now.

    Request handlers check this alongside the in-memory CYCLE_RUNNING flag so
    a cycle that started in the last few seconds is caught before the 5s poll
    loop has updated the flag.
    """
    return bool(glob.glob(_CYCLE_RUNNING_GLOB))

# Tracks whether we already sent the stale-ARL alert this cycle so we
# do not spam the channel if discord_bot restarts while the flag is still set.
_arl_alert_sent   = False
_arl_status_shown = None   # whether the status message shows the ARL pause (None: not drawn yet)

# True while app.py has an active download cycle. Set by the polling task
# when it detects cycle_running.json; cleared when the file disappears.
# Checked by all request handlers to block incoming requests.
CYCLE_RUNNING: bool = False           # True if ANY user cycle is running
CYCLE_USER:    str  = ""              # comma-joined names of users with a running cycle, for messages
# Global cycle status state: track active/completed user cycles and a shared
# status message in Discord so the bot updates one message instead of posting
# per-user start/complete embeds.
_CYCLE_USER_STATE: dict = {}          # user -> {'running': bool, 'sections': list, 'complete_info': dict|None, 'phase': str, 'detail': str, 'stats': dict}
_CYCLE_STATUS_MESSAGE: Optional[discord.Message] = None

# Persist _CYCLE_USER_STATE to disk so a discord_bot restart (e.g. a redeploy)
# doesn't lose track of users who already completed/were skipped this cycle
# window — without this, the shared status message would wrongly revert to
# "no cycle activity" for everyone on every restart, even mid-cycle.
_CYCLE_STATE_PERSIST_FILE = os.path.join(DATA_DIR, 'cycle_status_state.json')


def _save_cycle_user_state() -> None:
    try:
        with open(_CYCLE_STATE_PERSIST_FILE, 'w') as _f:
            json.dump(_CYCLE_USER_STATE, _f)
    except Exception as _e:
        logger.debug(f"Could not persist cycle status state: {_e}")


def _load_cycle_user_state() -> None:
    global _CYCLE_USER_STATE
    try:
        if os.path.exists(_CYCLE_STATE_PERSIST_FILE):
            with open(_CYCLE_STATE_PERSIST_FILE, 'r') as _f:
                loaded = json.load(_f)
            if isinstance(loaded, dict):
                # A cycle can only still be "running" if app.py's own signal file
                # says so — the process that was running it may not have survived
                # whatever caused discord_bot to restart. The next poll of
                # check_arl_and_notifications() re-derives 'running' from
                # cycle_running_*.json within 5s anyway, so clearing it here just
                # avoids a few seconds of showing a stale "in progress" state.
                for state in loaded.values():
                    if isinstance(state, dict) and state.get('running'):
                        state['running'] = False
                _CYCLE_USER_STATE = loaded
                logger.info(f"Restored cycle status state for {len(loaded)} user(s) from disk")
    except Exception as _e:
        logger.debug(f"Could not load persisted cycle status state: {_e}")
_PENDING_ERROR_MESSAGES: List[str] = []



def _resolve_admin_discord_handle() -> str | None:
    """The admin's Discord handle: DISCORD_TO_PLEX_MAPPING_ADMIN (`discord:plex`,
    or one name), matched against either side of the mapping, which says which
    name is which; else the mapping's first entry."""
    mapping = DISCORD_TO_PLEX_MAPPING or {}
    admin_discord, admin_plex = _parse_admin_override((DISCORD_TO_PLEX_MAPPING_ADMIN or '').strip())
    names = {str(n).lower() for n in (admin_discord, admin_plex) if n}
    for discord_user, plex_user in mapping.items():
        if str(discord_user).lower() in names or str(plex_user).lower() in names:
            return str(discord_user).strip()
    return admin_discord or next(iter(mapping), None)


def _admin_member(guild) -> Optional[discord.Member]:
    """The admin's member object in a guild (from DISCORD_TO_PLEX_MAPPING_ADMIN
    or the first mapping entry), or None."""
    handle = _resolve_admin_discord_handle()
    if not handle or guild is None:
        return None
    member = guild.get_member_named(handle)
    if member is None:
        member = next((m for m in guild.members
                       if handle.lower() in (m.name.lower(), m.display_name.lower())), None)
    return member


async def _send_private_admin_message(message: str, *, embed: discord.Embed | None = None) -> bool:
    """Tell the admin something privately: in their own requests thread, so
    the requests channel itself only shows the download-cycle status (a DM
    when the thread can't be opened)."""
    if not bot:
        return False
    channel = await _requests_channel()
    guilds = [channel.guild] if channel is not None else list(bot.guilds)
    for guild in guilds:
        admin = _admin_member(guild)
        if admin is None:
            continue
        thread = await _request_thread(channel, admin) if channel is not None else None
        target = thread if thread is not None else admin
        try:
            await target.send(message or None, embed=embed)
            return True
        except Exception as exc:
            logger.warning(f"Could not send the admin a notice: {exc}")
            return False
    logger.warning("Could not find the admin's Discord account for a private notice")
    return False


async def _deliver_admin_notices() -> None:
    """Post, replace or remove the admin's notices (admin_notify.py) in the
    admin's private thread."""
    import admin_notify
    todo = admin_notify.pending()
    if not todo:
        return
    channel = await _requests_channel()
    admin = _admin_member(channel.guild) if channel is not None else None
    thread = await _request_thread(channel, admin) if admin is not None else None
    if thread is None:
        return
    for notice_id, notice in todo.items():
        try:
            if notice.get('message_id'):
                try:
                    await (await thread.fetch_message(int(notice['message_id']))).delete()
                except discord.NotFound:
                    pass
            if notice.get('resolved'):
                admin_notify.removed(notice_id)
            else:
                sent = await thread.send(notice['message'])
                admin_notify.delivered(notice_id, sent.id, notice['message'])
        except Exception as exc:
            logger.warning(f"Could not deliver the admin notice '{notice_id}': {exc}")


# -- asking a requester which song they meant (request_choice.py) --------------

_CHOICE_PREFIX = "mai-choice:"


def _member_for(guild, who: str) -> Optional[discord.Member]:
    """A guild member by Discord id, username or display name."""
    if guild is None or not who:
        return None
    if str(who).isdigit():
        member = guild.get_member(int(who))
        if member is not None:
            return member
    name = str(who).lower()
    return guild.get_member_named(str(who)) or next(
        (m for m in guild.members if name in (m.name.lower(), m.display_name.lower())), None)


def _option_label(option) -> str:
    return option['label'] if isinstance(option, dict) else str(option)


def _option_detail(option) -> str:
    return (option.get('detail') or '') if isinstance(option, dict) else ''


def _choice_text(question: Dict) -> str:
    escape = discord.utils.escape_markdown
    request, source = escape(question['request']), question['source']
    lines = [f"🤔 I couldn't tell which {source} result is **{request}**. Pick it from the list and press "
             "**Select** (or pick *None of them match*)."]
    for i, option in enumerate(question['options']):
        detail = _option_detail(option)
        guess = "  ← my best guess" if question.get('suggestion') == i else ""
        lines.append(f"**{i + 1}.** {escape(_option_label(option))}" + (f" ({escape(detail)})" if detail else "")
                     + guess)
    minutes = max(1, int(question.get('timeout') or 0) // 60)
    fallback = "my best guess" if question.get('suggestion') is not None else "the closest match"
    lines.append(f"-# No answer within {minutes} min and I'll take {fallback}. This message goes away once "
                 "it's settled; the request's result says what was chosen.")
    return "\n".join(lines)


def _choice_view(question: Dict) -> discord.ui.View:
    """A drop-down of the results plus "None of them match", and a Select
    button that sends the choice back. Interactions are handled in
    on_interaction by custom id, so they keep working after a bot restart."""
    view = discord.ui.View(timeout=None)
    nxt = question.get('next_step')
    options = []
    for i, option in enumerate(question['options']):
        notes = [n for n in (_option_detail(option), "my best guess" if question.get('suggestion') == i else "") if n]
        options.append(discord.SelectOption(label=f"{i + 1}. {_option_label(option)}"[:100], value=str(i),
                                            description=" · ".join(notes)[:100] or None))
    options.append(discord.SelectOption(label="None of them match", value="next",
                                        description=f"Try {nxt} instead" if nxt else "Stop looking"))
    view.add_item(discord.ui.Select(custom_id=f"{_CHOICE_PREFIX}{question['id']}:menu",
                                    placeholder="Which one is it?", options=options))
    view.add_item(discord.ui.Button(label="Select", style=discord.ButtonStyle.success,
                                    custom_id=f"{_CHOICE_PREFIX}{question['id']}:submit"))
    return view


async def _deliver_request_choices() -> None:
    """Post the downloader's questions in the requester's private thread, and
    delete the ones nobody answered in time (the request's result says so)."""
    import request_choice
    questions = request_choice.pending()
    if not questions:
        return
    channel = await _requests_channel()
    for question in questions:
        try:
            if question['status'] == 'new':
                member = _member_for(channel.guild if channel else None, question['discord_user'])
                thread = await _request_thread(channel, member) if member is not None else None
                if thread is None:
                    logger.warning(f"Can't ask {question['discord_user']} which song they meant: no thread")
                    request_choice.unreachable(question['id'])
                    continue
                sent = await thread.send(f"{member.mention} " + _choice_text(question), view=_choice_view(question))
                request_choice.posted(question['id'], thread.id, sent.id)
            elif question['status'] == 'expired':
                if question.get('message_id'):
                    thread = bot.get_channel(question['thread_id']) or await bot.fetch_channel(question['thread_id'])
                    try:
                        await (await thread.fetch_message(question['message_id'])).delete()
                    except discord.NotFound:
                        pass
                request_choice.closed(question['id'])
        except Exception as exc:
            logger.warning(f"Could not deliver the question about '{question.get('request')}': {exc}")


@bot.listen('on_interaction')
async def _on_choice_button(interaction: discord.Interaction) -> None:
    """The requester (or the admin) picked from a question's list or pressed
    its Select button. Once the choice is sent back the question is deleted:
    only the request's result stays in the thread."""
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = str((interaction.data or {}).get('custom_id', ''))
    if not custom_id.startswith(_CHOICE_PREFIX):
        return
    import request_choice
    question_id, _, part = custom_id[len(_CHOICE_PREFIX):].partition(':')
    question = request_choice.load(question_id)
    if question is None or question.get('status') != 'asked':
        await interaction.response.send_message("This question has already been settled.", ephemeral=True)
        await _delete_question_message(interaction)
        return
    requester = str(question['discord_user']).lower()
    if not (_is_admin(interaction) or requester in (interaction.user.name.lower(), str(interaction.user.id))):
        await interaction.response.send_message("Only the person who made the request can choose.", ephemeral=True)
        return

    if part == 'menu':
        value = ((interaction.data or {}).get('values') or [''])[0]
        choice = request_choice.NEXT if value == 'next' else int(value) if value.isdigit() else None
        if choice is None or request_choice.select(question_id, choice, interaction.user.name) is None:
            await interaction.response.send_message("That option isn't available any more.", ephemeral=True)
            return
        await interaction.response.defer()           # the pick shows in the list until Select is pressed
        return

    if part != 'submit':
        return
    choice = question.get('selected')
    if choice is None:
        await interaction.response.send_message("Pick an option from the list first, then press Select.",
                                                ephemeral=True)
        return
    if request_choice.answer(question_id, choice, interaction.user.name) is None:
        await interaction.response.send_message("This question has already been settled.", ephemeral=True)
        await _delete_question_message(interaction)
        return
    if choice == request_choice.NEXT:
        nxt = question.get('next_step')
        ack = "🙋 Got it: none of them match — " + (f"trying {nxt}." if nxt else "stopping here.")
    else:
        label = discord.utils.escape_markdown(_option_label(question['options'][choice]))
        ack = f"✅ Got it: downloading **{label}**."
    # Only the person who pressed Select sees this, and it isn't kept in the thread.
    await interaction.response.send_message(ack, ephemeral=True)
    await _delete_question_message(interaction)


async def _delete_question_message(interaction: discord.Interaction) -> None:
    try:
        await interaction.message.delete()
    except (discord.NotFound, AttributeError):
        pass
    except discord.HTTPException as exc:
        logger.warning(f"Could not delete a settled question: {exc}")


async def _share_request_threads_with_admin() -> None:
    """Add the admin to every user's private requests thread they are not
    in yet."""
    channel = await _requests_channel()
    admin = _admin_member(channel.guild) if channel is not None else None
    if admin is None:
        return
    added = 0
    for user_id, thread_id in _load_request_threads().items():
        if str(admin.id) == user_id:
            continue
        try:
            thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            await thread.add_user(admin)
            added += 1
        except Exception as exc:
            logger.debug(f"Could not add the admin to request thread {thread_id}: {exc}")
    if added:
        logger.info(f"The admin is in {added} private request thread(s)")


async def _tidy_requests_channel() -> None:
    """The requests channel shows only the download-cycle status: requests
    and their replies live in private threads, admin notices in the admin's
    thread. Anything else left in the channel is removed."""
    channel = await _requests_channel()
    if channel is None:
        return
    keep = getattr(_CYCLE_STATUS_MESSAGE, 'id', None)
    if keep is None:
        found = await _find_existing_cycle_status_message(channel)
        keep = getattr(found, 'id', None)
    try:
        removed = await channel.purge(limit=None, check=lambda m: m.id != keep, bulk=True,
                                      reason="The requests channel only shows the download-cycle status")
        if removed:
            logger.info(f"Removed {len(removed)} old message(s) from #{channel.name}")
    except Exception as exc:
        logger.warning(f"Could not tidy #{getattr(channel, 'name', channel)}: {exc}")


async def send_arl_stale_alert(message: str) -> None:
    """Send an ARL-stale alert privately to the admin Discord user."""
    try:
        embed = discord.Embed(
            title="🔴 Deezer ARL Token Expired",
            description=message,
            color=discord.Color.red(),
        )
        embed.add_field(
            name="🔗 ARL Refresh Page",
            value=f"**{ARL_PUBLIC_URL}**" if ARL_PUBLIC_URL else "The ARL page MusicAI serves on `PORT` (set `ARL_PUBLIC_URL` to link it here).",
            inline=False,
        )
        embed.add_field(
            name="Steps",
            value=(
                "1. Open the link above\n"
                "2. Enter your Deezer email + password *(or paste ARL manually)*\n"
                "3. Enter the server passphrase (`ARL_SERVER_SECRET`)\n"
                "4. Hit submit — all paused downloads resume within 60 s"
            ),
            inline=False,
        )
        embed.set_footer(text="Music AI | ARL refresh required")
        sent = await _send_private_admin_message('', embed=embed)
        if not sent:
            logger.warning("ARL stale alert was not sent because the admin Discord user could not be resolved")
        else:
            logger.info("ARL stale alert sent privately to admin Discord user")
    except Exception as _e:
        logger.warning(f"Could not send ARL stale alert: {_e}")


async def _refresh_cycle_status() -> None:
    """Redraw the cycle-status message (it also says when Deezer downloads
    are paused because the ARL expired)."""
    channel = await _requests_channel()
    if channel is not None:
        await _ensure_cycle_status_message(channel, _build_cycle_status_embed())


def _looks_like_cycle_status_message(message) -> bool:
    """Return True when a message looks like the shared cycle-status embed."""
    try:
        if not getattr(message, 'embeds', None):
            return False
        for embed in message.embeds:
            title = getattr(embed, 'title', '') or ''
            footer_text = getattr(getattr(embed, 'footer', None), 'text', '') or ''
            if 'Download Cycle Status' in title or 'Music AI | Download Cycle Status' in footer_text:
                return True
    except Exception:
        return False
    return False


async def _find_existing_cycle_status_message(channel):
    """Find the most recent shared cycle-status message already in the channel."""
    if not channel:
        return None
    try:
        async for message in channel.history(limit=25):
            if _looks_like_cycle_status_message(message):
                return message
    except Exception:
        return None
    return None


async def _prune_duplicate_cycle_status_messages(channel, keep_message=None):
    """Delete stale duplicate shared cycle-status messages, leaving one canonical message."""
    if not channel:
        return
    try:
        async for message in channel.history(limit=25):
            if keep_message and getattr(message, 'id', None) == getattr(keep_message, 'id', None):
                continue
            if not _looks_like_cycle_status_message(message):
                continue
            try:
                await message.delete()
            except Exception:
                pass
    except Exception:
        pass


def _build_cycle_status_embed() -> discord.Embed:
    """Build a single shared embed describing current cycle status."""

    def _state_by_canonical_user() -> dict[str, dict]:
        normalized: dict[str, dict] = {}
        for raw_user, state in _CYCLE_USER_STATE.items():
            canonical = _canonical_cycle_user(raw_user)
            if not canonical:
                continue
            if canonical in normalized:
                merged = dict(normalized[canonical])
                merged.update(state)
                if state.get('running') or merged.get('running'):
                    merged['running'] = True
                if state.get('skipped') or merged.get('skipped'):
                    merged['skipped'] = True
                normalized[canonical] = merged
            else:
                normalized[canonical] = dict(state)
        return normalized

    def _display_user(u: str) -> Optional[str]:
        if not u:
            return None
        us = str(u).strip()
        if re.match(r'^user\d+$', us, flags=re.IGNORECASE):
            return None
        canonical = _canonical_cycle_user(us)
        return canonical or us

    def _normalize_cycle_users(user_keys: list[str]) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        seen = set()
        for u in user_keys:
            display = _display_user(u)
            if not display:
                continue
            key = str(display).lower()
            if key in seen:
                continue
            seen.add(key)
            result.append((u, display))
        return result

    canonical_state = _state_by_canonical_user()
    running_users = _normalize_cycle_users([user for user, state in canonical_state.items() if state.get('running')])
    skipped_users = _normalize_cycle_users([user for user, state in canonical_state.items() if not state.get('running') and state.get('skipped')])
    completed_users = _normalize_cycle_users([user for user, state in canonical_state.items() if not state.get('running') and not state.get('skipped') and state.get('complete_info')])
    all_users = _normalize_cycle_users(_get_known_cycle_users())

    active_display_names = {display for _, display in running_users + skipped_users + completed_users}
    remaining_users = [display for _, display in all_users if display not in active_display_names]

    # Yellow means at least one configured user has not reached a terminal
    # state. Skipped users are terminal outcomes, so an all-skipped/completed
    # pass is still a successful completion rather than a warning.
    if running_users:
        title = "⏳ Download Cycle In Progress"
        color = discord.Color.orange()
    elif remaining_users:
        title = "⚠️ Download Cycle Status"
        color = discord.Color.gold()
    else:
        title = "✅ Download Cycle Complete"
        color = discord.Color.green()

    description_parts = []

    if running_users:
        description_parts.append("**Active cycle updates:**")
        for original_user, display_user in sorted(running_users, key=lambda item: item[1].lower()):
            state = _CYCLE_USER_STATE.get(original_user, {})
            sections = state.get('sections', [])
            section_text = ", ".join(sections) if sections else "All sections"
            phase = state.get('phase') or "Working"
            detail = state.get('detail') or "Progress update"
            stats = state.get('stats') or {}
            downloads = stats.get('downloads', {}) if isinstance(stats, dict) else {}
            total_so_far = downloads.get('total', 0) if isinstance(downloads, dict) else 0
            line = f"• **{display_user}** — {phase}: {detail}"
            if total_so_far:
                line += f" ({total_so_far} track(s) so far)"
            description_parts.append(line + f"\n  ↳ {section_text}")

    if skipped_users:
        description_parts.append("**Skipped users:**")
        for original_user, display_user in sorted(skipped_users, key=lambda item: item[1].lower()):
            state = _CYCLE_USER_STATE.get(original_user, {})
            detail = state.get('detail') or "Not enough listening activity"
            description_parts.append(f"• **{display_user}** — Skipped due to lack of activity: {detail}")

    if completed_users:
        description_parts.append("**Recently completed:**")
        for original_user, display_user in sorted(completed_users, key=lambda item: item[1].lower()):
            complete_info = _CYCLE_USER_STATE.get(original_user, {}).get('complete_info', {})
            total_downloaded = complete_info.get('total_downloaded', 0)
            if total_downloaded == 0:
                description_parts.append(f"• **{display_user}** — No downloads performed")
            else:
                description_parts.append(f"• **{display_user}** — {total_downloaded} track(s) downloaded")

    if remaining_users:
        description_parts.append("**Waiting for this round's download cycle to run:**")
        for user in sorted(remaining_users, key=lambda item: item.lower()):
            description_parts.append(f"• **{user}** — hasn't been processed in this download cycle yet")

    if not description_parts:
        description_parts.append("No cycle activity has been recorded yet.")

    if _PENDING_ERROR_MESSAGES:
        description_parts.append("\n**Recent errors:**")
        for error in _PENDING_ERROR_MESSAGES[-5:]:
            cleaned = error.strip().replace('\n', ' ')
            description_parts.append(f"• {cleaned[:180]}")

    # Discord renders <t:…> timestamps in the description but not in footers.
    next_due = cycle_schedule.next_due()
    if next_due and not running_users:
        description_parts.append(
            f"\n**Next cycle:** {discord.utils.format_dt(next_due, style='R')} "
            f"({discord.utils.format_dt(next_due, style='f')})"
        )

    if os.path.exists(_ARL_STALE_FLAG):
        description_parts.insert(0, "⏸️ **Deezer downloads are paused**: its login (ARL) expired and the "
                                    "admin has been asked to renew it.\n")
    description = "\n".join(description_parts)
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="Music AI | Download Cycle Status")
    return embed


async def _pin_cycle_status_message(message) -> None:
    """Pin the shared cycle-status message so it's easy to find regardless of
    channel scroll position. Best-effort: pin limits/permission issues are
    logged and otherwise ignored — the bottom-of-channel behavior in
    `_ensure_cycle_status_message` is what actually keeps it visible day-to-day.

    Every pin makes Discord post its own "X pinned a message to this channel"
    system notice. Since the status message gets re-pinned each time it's
    moved to stay at the bottom (see `_ensure_cycle_status_message`), those
    notices would otherwise pile up — clean up the one we just triggered.
    """
    try:
        if not getattr(message, 'pinned', False):
            await message.pin(reason="Keep download-cycle status visible")
            try:
                channel = message.channel
                async for recent in channel.history(limit=5):
                    if (
                        getattr(recent, 'type', None) == discord.MessageType.pins_add
                        and bot.user is not None
                        and getattr(recent.author, 'id', None) == bot.user.id
                    ):
                        await recent.delete()
                        break
            except Exception as _notice_err:
                logger.debug(f"Could not clean up pin system notice: {_notice_err}")
    except Exception as _e:
        logger.debug(f"Could not pin cycle status message: {_e}")


def _cycle_status_embed_changed(message, embed: discord.Embed) -> bool:
    """True when `embed` would actually alter the message already posted."""
    existing = (getattr(message, 'embeds', None) or [None])[0]
    if existing is None:
        return True
    try:
        def fields(e):
            colour = getattr(e, 'colour', None) or getattr(e, 'color', None)
            footer = getattr(e, 'footer', None)
            return (
                (e.title or ''),
                (e.description or ''),
                getattr(colour, 'value', colour),
                (getattr(footer, 'text', None) or ''),
            )
        return fields(existing) != fields(embed)
    except Exception:
        return True


async def _ensure_cycle_status_message(channel, embed: discord.Embed, allow_move: bool = True) -> None:
    """Create or update the pinned cycle-status message.

    allow_move=False is for updates that are not cycle activity (a restart):
    an existing message is corrected in place — an edit notifies nobody — and
    is never moved to the bottom, because re-posting it would look to the
    channel like a new download cycle had just run.
    """
    global _CYCLE_STATUS_MESSAGE

    target_message = _CYCLE_STATUS_MESSAGE
    if not target_message:
        target_message = await _find_existing_cycle_status_message(channel)

    if target_message and not allow_move:
        try:
            if _cycle_status_embed_changed(target_message, embed):
                await target_message.edit(embed=embed)
            await _pin_cycle_status_message(target_message)
            _CYCLE_STATUS_MESSAGE = target_message
            return
        except Exception as _e:
            logger.debug(f"Could not update cycle status message in place: {_e}")
            return

    # Nothing new to say: leave the message exactly where and as it is.
    # This is the restart case — re-posting it would notify the channel and
    # re-editing it would mark it "(edited)" for no reason.
    if target_message and not _cycle_status_embed_changed(target_message, embed):
        await _pin_cycle_status_message(target_message)
        _CYCLE_STATUS_MESSAGE = target_message
        return

    # The status message is meant to stay pinned at the *bottom* of the
    # channel — the most recent message — so it isn't buried under newer
    # song-request activity. If something else has been posted since our
    # last update, editing in place would leave it stranded mid-history, so
    # retire it and re-send fresh at the bottom instead of just editing.
    # `channel.last_message_id` is not usable for this: Discord keeps
    # pointing it at deleted messages (such as the "pinned a message"
    # notice removed right after each pin), so ask for the newest message
    # that actually exists.
    is_still_latest = True
    if target_message:
        try:
            async for latest in channel.history(limit=1):
                is_still_latest = latest.id == target_message.id
        except Exception:
            pass

    if target_message and is_still_latest:
        try:
            await _prune_duplicate_cycle_status_messages(channel, keep_message=target_message)
            await target_message.edit(embed=embed)
            await _pin_cycle_status_message(target_message)
            _CYCLE_STATUS_MESSAGE = target_message
            return
        except Exception as _e:
            logger.debug(f"Could not edit existing cycle status message: {_e}")
            target_message = None
            _CYCLE_STATUS_MESSAGE = None

    # Either there's no existing message, it's no longer the bottom-most
    # message in the channel, or editing it failed — retire the old one
    # (unpinning first so Discord doesn't accumulate stale pins) and send a
    # fresh message at the bottom.
    if target_message:
        try:
            if getattr(target_message, 'pinned', False):
                await target_message.unpin(reason="Replacing with updated cycle status message")
        except Exception:
            pass
        try:
            await target_message.delete()
        except Exception:
            pass

    try:
        await _prune_duplicate_cycle_status_messages(channel, keep_message=None)
    except Exception:
        pass

    try:
        new_msg = await channel.send(embed=embed)
        _CYCLE_STATUS_MESSAGE = new_msg
        await _pin_cycle_status_message(new_msg)
        await _prune_duplicate_cycle_status_messages(channel, keep_message=new_msg)
    except Exception as _e:
        logger.warning(f"Could not send cycle status message: {_e}")


async def send_cycle_start(user: str, sections: list) -> None:
    """Update the shared Discord cycle status message when a user cycle begins."""
    if not (DISCORD_MAIN_CHANNEL or DISCORD_MAIN_CHANNEL_NAME) or not bot:
        return
    try:
        canonical_user = _canonical_cycle_user(user) or user
        if canonical_user not in _CYCLE_USER_STATE:
            _CYCLE_USER_STATE[canonical_user] = {}
        _CYCLE_USER_STATE[canonical_user].update({
            'running': True,
            'sections': sections,
            'complete_info': None,
            'phase': 'Starting cycle',
            'detail': 'Initial analysis and download prep',
            'stats': {'downloads': {'total': 0}},
            'skipped': False,
        })
        _save_cycle_user_state()
        channel = await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)
        if not channel:
            return
        embed = _build_cycle_status_embed()
        await _ensure_cycle_status_message(channel, embed)
        logger.info(f"Cycle-start notification updated for user {canonical_user}")
    except Exception as _e:
        logger.warning(f"Could not update cycle-start notification: {_e}")


async def send_cycle_progress_update(user: str, phase: str, detail: str, stats: Optional[Dict] = None) -> None:
    """Update the shared cycle status message with the latest phase and progress."""
    if not (DISCORD_MAIN_CHANNEL or DISCORD_MAIN_CHANNEL_NAME) or not bot:
        return
    try:
        canonical_user = _canonical_cycle_user(user) or user
        if canonical_user not in _CYCLE_USER_STATE:
            _CYCLE_USER_STATE[canonical_user] = {}
        _CYCLE_USER_STATE[canonical_user].update({
            'running': True,
            'phase': phase,
            'detail': detail,
            'stats': stats if stats is not None else _CYCLE_USER_STATE[canonical_user].get('stats', {'downloads': {'total': 0}}),
            'skipped': False,
        })
        _save_cycle_user_state()
        channel = await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)
        if not channel:
            return
        embed = _build_cycle_status_embed()
        await _ensure_cycle_status_message(channel, embed)
    except Exception as _e:
        logger.warning(f"Could not update cycle progress notification for {user}: {_e}")


async def send_cycle_complete(user: str, total_downloaded: int, next_cycle_hours: float, delete_after: int = 300) -> None:
    """Update the shared Discord cycle status message when a user cycle completes."""
    if not (DISCORD_MAIN_CHANNEL or DISCORD_MAIN_CHANNEL_NAME) or not bot:
        return
    try:
        canonical_user = _canonical_cycle_user(user) or user
        if canonical_user not in _CYCLE_USER_STATE:
            _CYCLE_USER_STATE[canonical_user] = {}
        _CYCLE_USER_STATE[canonical_user].update({
            'running': False,
            'phase': 'Completed',
            'detail': (
                'No downloads performed' if total_downloaded == 0
                else f'{total_downloaded} track(s) downloaded'
            ),
            'complete_info': {
                'total_downloaded': total_downloaded,
                'next_cycle_hours': next_cycle_hours,
            },
            'stats': _CYCLE_USER_STATE[canonical_user].get('stats', {'downloads': {'total': 0}}),
            'skipped': False,
        })
        if isinstance(_CYCLE_USER_STATE[canonical_user].get('stats'), dict):
            downloads = _CYCLE_USER_STATE[canonical_user]['stats'].get('downloads', {})
            if isinstance(downloads, dict):
                downloads['total'] = total_downloaded
        _save_cycle_user_state()
        channel = await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)
        if not channel:
            return
        embed = _build_cycle_status_embed()
        await _ensure_cycle_status_message(channel, embed)

        if not any(v.get('running') for v in _CYCLE_USER_STATE.values()):
            async with PENDING_REJECTION_LOCK:
                for _rm in list(PENDING_REJECTION_MESSAGES):
                    try:
                        await _rm.delete()
                    except Exception:
                        pass
                PENDING_REJECTION_MESSAGES.clear()

        logger.info(f"Cycle-complete status updated for user {canonical_user}")
    except Exception as _e:
        logger.warning(f"Could not update cycle-complete notification: {_e}")


async def send_cycle_skipped(user: str, reason: str, next_cycle_hours: float) -> None:
    """Update the shared Discord cycle status message when a user cycle is skipped."""
    if not (DISCORD_MAIN_CHANNEL or DISCORD_MAIN_CHANNEL_NAME) or not bot:
        return
    try:
        canonical_user = _canonical_cycle_user(user) or user
        if canonical_user not in _CYCLE_USER_STATE:
            _CYCLE_USER_STATE[canonical_user] = {}
        _CYCLE_USER_STATE[canonical_user].update({
            'running': False,
            'phase': 'Skipped',
            'detail': reason,
            'complete_info': {
                'total_downloaded': 0,
                'next_cycle_hours': next_cycle_hours,
            },
            'stats': {'downloads': {'total': 0}},
            'skipped': True,
        })
        _save_cycle_user_state()
        channel = await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)
        if not channel:
            return
        embed = _build_cycle_status_embed()
        await _ensure_cycle_status_message(channel, embed)
        logger.info(f"Cycle-skip status updated for user {user}")
    except Exception as _e:
        logger.warning(f"Could not update cycle-skip notification: {_e}")


_LAST_SEEN_NEXT_DUE = None


async def _refresh_next_cycle_line() -> None:
    global _LAST_SEEN_NEXT_DUE
    try:
        next_due = cycle_schedule.next_due()
    except Exception:
        return
    if next_due == _LAST_SEEN_NEXT_DUE:
        return
    first_look = _LAST_SEEN_NEXT_DUE is None
    _LAST_SEEN_NEXT_DUE = next_due
    # on_ready already brought the message up to date for the value at start.
    if first_look or CYCLE_RUNNING or not (DISCORD_MAIN_CHANNEL or DISCORD_MAIN_CHANNEL_NAME):
        return
    try:
        channel = await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)
        if channel:
            await _ensure_cycle_status_message(channel, _build_cycle_status_embed(), allow_move=False)
    except Exception as _e:
        logger.debug(f"Could not refresh next-cycle time: {_e}")


@tasks.loop(seconds=5)
async def check_arl_and_notifications() -> None:
    """Periodic task: check for ARL stale alert + drain general error notification queue.

    Runs every 60 seconds.  Two things happen:
      1. If DATA_DIR/arl_alert.json exists (written by app.py / downloader.py),
         send the admin alert embed and delete the file.
      2. If DATA_DIR/arl_stale.flag was cleared (ARL refreshed), send a
         "downloads resumed" confirmation message.
      3. Drain up to 10 entries from discord_notifications.jsonl (error queue
         written by notify_discord_error in app.py) and post them.
    """
    global _arl_alert_sent, _arl_status_shown, CYCLE_RUNNING, CYCLE_USER

    # -- 0. Cycle running flag ---------------------------------------------------
    # Each app.py instance writes cycle_running_{user}.json so concurrent
    # cycles from different Plex users don't clobber each other.
    # We scan for any matching file; if at least one exists a cycle is running.
    _running_files = glob.glob(_CYCLE_RUNNING_GLOB)
    _any_running = bool(_running_files)

    # ── Per-user cycle tracking ──────────────────────────────────────────────
    # Build a set of users that currently have a running cycle file on disk.
    _currently_running_users: set = set()
    for _rf in _running_files:
        try:
            with open(_rf) as _rff:
                _u = json.load(_rff).get('user', '')
            if _u:
                _currently_running_users.add(_u)
        except Exception:
            pass

    # Detect NEW users that just started a cycle
    for _u in _currently_running_users:
        if _u not in _CYCLE_USER_STATE or not _CYCLE_USER_STATE[_u].get('running'):
            # New cycle started for this user
            _sections = []
            _rf_path = os.path.join(DATA_DIR, f'cycle_running_{_u}.json')
            try:
                with open(_rf_path) as _rff:
                    _rd = json.load(_rff)
                _sections = _rd.get('sections', [])
            except Exception:
                pass
            _CYCLE_USER_STATE[_u] = {
                'running': True,
                'sections': _sections,
                'complete_info': None,
            }
            await send_cycle_start(_u, _sections)
            logger.info(f"Cycle started for user: {_u}")

    # Detect users whose cycle just FINISHED (had running state, no longer have a file)
    for _u in list(_CYCLE_USER_STATE.keys()):
        if _CYCLE_USER_STATE[_u].get('running') and _u not in _currently_running_users:
            # This user's cycle just ended — mark stopped before sending complete
            _CYCLE_USER_STATE[_u]['running'] = False
            _cycle_total = 0
            _next_h      = 0.0
            _cf_path = os.path.join(DATA_DIR, f'cycle_complete_{_u}.json')
            if os.path.exists(_cf_path):
                try:
                    with open(_cf_path) as _sf:
                        _sd = json.load(_sf)
                    _cycle_total = _sd.get('total_downloaded', 0)
                    _next_h      = float(_sd.get('next_cycle_hours', 0))
                    os.remove(_cf_path)
                except Exception:
                    pass
            await send_cycle_complete(_u, _cycle_total, _next_h, delete_after=300)
            logger.info(f"Cycle completed for user: {_u}")

    # Update global CYCLE_RUNNING flag for request-blocking checks
    CYCLE_RUNNING = bool(_currently_running_users)
    CYCLE_USER    = ', '.join(sorted(_currently_running_users))

    # The manager sets the next due time only after the last user finishes,
    # so the "Next cycle" line is corrected in place once that happens.
    await _refresh_next_cycle_line()

    # -- 1. Cycle skip detection ------------------------------------------------
    _skip_files = glob.glob(os.path.join(DATA_DIR, 'cycle_skipped_*.json'))
    for _sf in _skip_files:
        try:
            with open(_sf) as _f:
                _skip_payload = json.load(_f)
            _user = _skip_payload.get('user')
            _reason = _skip_payload.get('reason', 'Not enough activity')
            _next_h = float(_skip_payload.get('next_cycle_hours', 0.0))
            if _user and (_user not in _CYCLE_USER_STATE or not _CYCLE_USER_STATE[_user].get('skipped')):
                _CYCLE_USER_STATE[_user] = {
                    'running': False,
                    'phase': 'Skipped',
                    'detail': _reason,
                    'complete_info': {
                        'total_downloaded': 0,
                        'next_cycle_hours': _next_h,
                    },
                    'stats': {'downloads': {'total': 0}},
                    'skipped': True,
                }
                await send_cycle_skipped(_user, _reason, _next_h)
                logger.info(f"Cycle skipped detected for user: {_user}")
            try:
                os.remove(_sf)
            except Exception:
                pass
        except Exception as _e:
            logger.debug(f"Could not read cycle skip file {_sf}: {_e}")

    # -- 2. ARL stale alert -------------------------------------------------------
    if os.path.exists(_ARL_ALERT_FILE):
        try:
            with open(_ARL_ALERT_FILE, 'r') as _f:
                payload = json.load(_f)
            os.remove(_ARL_ALERT_FILE)  # delete BEFORE sending so we never double-send
            msg = payload.get('message', 'Deezer ARL is stale -- please log in to Deezer.')
            if not _arl_alert_sent:
                await send_arl_stale_alert(msg)
                _arl_alert_sent = True
        except Exception as _e:
            logger.debug(f"ARL alert file read error: {_e}")

    _arl_stale_now = os.path.exists(_ARL_STALE_FLAG)
    if _arl_stale_now and _arl_status_shown is not True:
        _arl_status_shown = True
        await _refresh_cycle_status()

    # -- 3. ARL refreshed confirmation -------------------------------------------
    if not _arl_stale_now and _arl_status_shown is not False:
        # The flag was cleared (or the bot started after a refresh): the status
        # message drops its "paused" line and the admin hears it is fixed.
        _had_arl_alert = _arl_alert_sent
        _arl_alert_sent = False
        try:
            await _refresh_cycle_status()
            _arl_status_shown = False
            # Send admin confirmation
            if _had_arl_alert:
                sent = await _send_private_admin_message(
                    "**Deezer ARL refreshed** — downloads have resumed automatically."
                )
                if not sent:
                    logger.debug("ARL resume notification skipped: admin Discord user could not be resolved")
                else:
                    logger.info("ARL refreshed notification sent to admin")
        except Exception as _e:
            logger.debug(f"ARL resume notification error: {_e}")
    elif _arl_stale_now and not _arl_alert_sent:
        logger.debug(f"ARL stale flag exists but alert not yet sent (_arl_alert_sent={_arl_alert_sent})")
    try:
        await _deliver_admin_notices()
    except Exception as _e:
        logger.debug(f"Admin notice delivery error: {_e}")
    try:
        await _deliver_request_choices()
    except Exception as _e:
        logger.debug(f"Request question delivery error: {_e}")

    # -- 3. Drain general error notification queue (notify_discord_error) ----------
    if os.path.exists(_NOTIF_FILE):
        lines_to_keep = []
        sent = 0
        try:
            with open(_NOTIF_FILE, 'r') as _f:
                raw_lines = _f.readlines()
            for line in raw_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    lines_to_keep.append(line)
                    continue

                ntype = entry.get('type', 'error')
                if ntype in ('cycle_start', 'cycle_complete'):
                    # Handled directly by the polling task (section 0 above).
                    # Drop these entries — posting them here would double-post.
                    continue

                if sent < 10:
                    msg = entry.get('message', 'Unknown error')
                    _PENDING_ERROR_MESSAGES.append(msg)
                    sent += 1
                else:
                    lines_to_keep.append(line)

            # Rewrite file with unsent lines
            with open(_NOTIF_FILE, 'w') as _f:
                _f.write('\n'.join(lines_to_keep) + ('\n' if lines_to_keep else ''))

            if _PENDING_ERROR_MESSAGES:
                channel = await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)
                if channel:
                    embed = _build_cycle_status_embed()
                    await _ensure_cycle_status_message(channel, embed)
        except Exception as _e:
            logger.debug(f"Notification drain error: {_e}")


@check_arl_and_notifications.before_loop
async def before_arl_check():
    """Wait until the bot is fully ready before starting the poll task."""
    await bot.wait_until_ready()


@bot.event
async def on_member_join(member: discord.Member):
    """A new member joined: reload the Wizarr mapping so their Discord handle
    maps to their Plex user. MusicAI sends no onboarding messages."""
    if not member or member.bot:
        return
    try:
        reloaded = reload_wizarr_mapping()
        if reloaded:
            logger.info(f"Reloaded Wizarr mapping after member join: {member.display_name} ({member.id})")
        else:
            logger.debug("No Wizarr mapping found to reload on member join")
    except Exception as exc:
        logger.debug(f"Error reloading Wizarr mapping on member join: {exc}")


class _NoMessage:
    """Stands in for a DM that couldn't be delivered, so the request's
    status updates and clean-up become no-ops instead of errors."""
    id = None

    async def edit(self, *args, **kwargs):
        return self

    async def delete(self, *args, **kwargs):
        return None

    async def add_reaction(self, *args, **kwargs):
        return None


REQUEST_THREADS_FILE = os.path.join(DATA_DIR, 'request_threads.json')
_REQUEST_THREAD_LOCK = asyncio.Lock()


def _load_request_threads() -> Dict[str, int]:
    try:
        with open(REQUEST_THREADS_FILE) as fh:
            return {str(k): int(v) for k, v in (json.load(fh) or {}).items()}
    except Exception:
        return {}


def _save_request_threads(threads: Dict[str, int]) -> None:
    tmp = REQUEST_THREADS_FILE + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(threads, fh, indent=1)
    os.replace(tmp, REQUEST_THREADS_FILE)


async def _request_thread(channel, user) -> Optional[discord.Thread]:
    """The user's private requests thread under the requests channel —
    created on their first request, then reused. Only the user and the admin
    are in it (plus anyone with Manage Threads), and everything about their
    downloads stays there. An archived thread reopens when the bot posts in it."""
    async with _REQUEST_THREAD_LOCK:
        threads = _load_request_threads()
        thread = None
        thread_id = threads.get(str(user.id))
        if thread_id:
            thread = bot.get_channel(thread_id)
            if thread is None:
                try:
                    thread = await bot.fetch_channel(thread_id)
                except (discord.NotFound, discord.Forbidden):
                    thread = None
                except Exception as exc:
                    logger.debug(f"Could not fetch request thread {thread_id}: {exc}")
                    thread = None
            if thread is not None and getattr(thread, 'locked', False):
                thread = None
        if thread is not None:
            return thread
        if channel is None:
            return None
        try:
            thread = await channel.create_thread(
                name=f"🎵 {user.display_name}'s requests", type=discord.ChannelType.private_thread,
                invitable=False, auto_archive_duration=10080)
            await thread.add_user(user)
            admin = _admin_member(channel.guild)
            if admin is not None and admin.id != user.id:
                await thread.add_user(admin)
            await thread.send(
                f"🔒 {user.mention}, this is your private requests thread: only you and the admin can see it. "
                f"Paste links here, in {channel.mention}, or use `/request` — everything about your downloads stays here.")
        except Exception as exc:
            logger.warning(f"Could not open a private requests thread for {user}: {exc}")
            return None
        threads[str(user.id)] = thread.id
        _save_request_threads(threads)
        logger.info(f"Opened private requests thread for {user} ({thread.id})")
        return thread


class _ThreadReply:
    """Everything the bot says about a song request goes into the requester's
    private thread and stays there (nothing is auto-deleted)."""

    def __init__(self, thread):
        self.thread = thread

    async def send(self, content=None, **kwargs):
        kwargs.pop("delete_after", None)
        try:
            return await self.thread.send(content, **kwargs)
        except Exception as exc:
            logger.debug(f"Request reply failed: {exc}")
            return _NoMessage()

    reply = send


class _RequestShim:
    """What the request handler reads from a request: the requester, where
    replies go and the text with the links. Deleting it does nothing — a
    link posted in a private thread stays as the request's record."""

    def __init__(self, author, channel, guild, content: str, message_id=None):
        self.author = author
        self.channel = channel
        self.guild = guild
        self.content = content or ""
        self.attachments = []
        self.id = message_id

    async def delete(self, *args, **kwargs):
        return None


async def _handle_request(message, private, requested: bool = False):
    """Handle a music request (links), made with /request or the
    private-request button. Every answer goes through `private` — ephemeral
    replies only the requester sees in the channel."""

    # Guard: queue requests during an active download cycle instead of blocking.
    # Check both the in-memory flag AND the file directly — no polling lag.
    if CYCLE_RUNNING or _any_cycle_running_file():
        # Determine if message is in any request channel
        _in_music_ch = requested
        if requested:
            pass
        elif DISCORD_MAIN_CHANNEL and hasattr(message.channel, 'id'):
            _in_music_ch = (message.channel.id == DISCORD_MAIN_CHANNEL)
        elif DISCORD_MAIN_CHANNEL_NAME and hasattr(message.channel, 'name'):
            _in_music_ch = (message.channel.name.lower() == DISCORD_MAIN_CHANNEL_NAME.lower())

        if _in_music_ch and message.author != bot.user:
            logger.info(f"⏳ Queuing request from {message.author} — download cycle in progress")
            # Don't delete the message - let it be processed but inform user it's queued
            embed = discord.Embed(
                title="⏳ Request Queued — Download Cycle Running",
                description=(
                    f"**{message.author.display_name}**, your request has been queued.\n\n"
                    f"A download cycle is currently running for **{CYCLE_USER or 'the system'}** "
                    "and your request will be processed once the cycle completes."
                ),
                color=discord.Color.orange(),
            )
            embed.set_footer(text="Music AI | Request queued for processing")
            try:
                sent = await private.reply(embed=embed)
                async with PENDING_REJECTION_LOCK:
                    PENDING_REJECTION_MESSAGES.append(sent)
            except Exception:
                pass
            # Continue processing the message normally - it will be queued

    # Guard: if ARL is stale, inform the user and do not queue the download.
    if os.path.exists(_ARL_STALE_FLAG):
        # Only reply if the message is in the music-requests channel
        in_music_ch = requested
        if requested:
            pass
        elif DISCORD_MAIN_CHANNEL and hasattr(message.channel, 'id'):
            in_music_ch = (message.channel.id == DISCORD_MAIN_CHANNEL)
        elif DISCORD_MAIN_CHANNEL_NAME and hasattr(message.channel, 'name'):
            in_music_ch = (message.channel.name.lower() == DISCORD_MAIN_CHANNEL_NAME.lower())
        if in_music_ch:
            await private.reply(
                "Deezer ARL is currently stale -- downloads are paused while the admin "
                "refreshes the token.  Your request has been noted; please resubmit once "
                "downloads resume."
            )
            return

    # Global guard: if a cleanup is running, queue requests instead of blocking.
    # This removes the user's original message and posts a replacement queued message
    # which is tracked so it can be deleted when the cleanup finishes.
    try:
        if CLEANUP_RUNNING:
            # Determine if the message looks like a request
            is_in_music_requests = requested
            if requested:
                pass
            elif DISCORD_MAIN_CHANNEL:
                is_in_music_requests = (message.channel.id == DISCORD_MAIN_CHANNEL)
            elif DISCORD_MAIN_CHANNEL_NAME:
                try:
                    is_in_music_requests = (message.channel.name.lower() == DISCORD_MAIN_CHANNEL_NAME.lower())
                except Exception:
                    is_in_music_requests = False

            # Check for URLs in the message (best-effort)
            urls = []
            try:
                if extract_urls_from_text:
                    urls = extract_urls_from_text(message.content or '')
                else:
                    urls = re.findall(r'https?://[^\s]+', message.content or '') if message.content else []
            except Exception:
                urls = []

            looks_like_request = bool(
                is_in_music_requests or
                message.attachments or
                urls or
                (message.content and message.content.strip().startswith('!'))
            )

            if looks_like_request:
                # Don't delete the message - let it be processed but inform user it's queued
                embed = discord.Embed(
                    title="⏳ Request Queued — Cleanup In Progress",
                    description=(f"A library cleanup is currently running for **{CLEANUP_OWNER or 'another user'}**."
                                 " Your request has been queued and will be processed once cleanup completes."),
                    color=discord.Color.orange()
                )
                try:
                    sent = await private.reply(embed=embed)
                    async with PENDING_REJECTION_LOCK:
                        PENDING_REJECTION_MESSAGES.append(sent)
                except Exception:
                    pass
                # Continue processing the message normally - it will be queued
    except Exception:
        logger.debug("Error during global cleanup queue guard")
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # SECTION 2: HANDLE MUSIC REQUESTS CHANNEL
    # ═══════════════════════════════════════════════════════════════════════════════
    
    is_in_music_requests = requested
    if requested:
        pass
    elif DISCORD_MAIN_CHANNEL:
        is_in_music_requests = message.channel.id == DISCORD_MAIN_CHANNEL
    elif DISCORD_MAIN_CHANNEL_NAME:
        is_in_music_requests = message.channel.name.lower() == DISCORD_MAIN_CHANNEL_NAME.lower()
    
    if is_in_music_requests:
        logger.debug(f"📨 Message received in music_requests channel: {message.content[:50]}")
        if not (extract_link_metadata and extract_urls_from_text):
            logger.error("extract_link_metadata or extract_urls_from_text is not available. Link extraction is disabled.")
            await private.reply("❌ Link extraction is currently unavailable. Please contact the admin.")
            return
        # If a cleanup is running, reject new incoming requests in the music channel.
        try:
            if CLEANUP_RUNNING:
                try:
                    await message.delete()
                except Exception:
                    pass
                embed = discord.Embed(
                    title="⏳ Cleanup In Progress",
                    description=(f"A library cleanup is currently running for **{CLEANUP_OWNER}**."
                                 " New requests are temporarily blocked."),
                    color=discord.Color.red()
                )
                try:
                    sent = await private.send(embed=embed)
                    async with PENDING_REJECTION_LOCK:
                        PENDING_REJECTION_MESSAGES.append(sent)
                except Exception:
                    pass
                return
        except Exception:
            logger.debug("Error handling cleanup guard in music_requests channel")

        # Extract URLs from the message
        try:
            urls = extract_urls_from_text(message.content or '')
        except Exception as e:
            logger.error(f"❌ Error extracting URLs from message: {e}")
            await private.reply("❌ Could not extract URLs from your message.")
            return

        if urls and not requested:
            # The link is the request; it doesn't stay in the channel.
            try:
                await message.delete()
            except Exception as exc:
                logger.debug(f"Could not take the request down from the channel: {exc}")
        if not urls:
            logger.info(f"No URLs found in message from {message.author}")
            await private.reply("❌ No valid links found in your message.")
            return

        # Re-extract URLs from message (for download queueing)
        urls = extract_urls_from_text(message.content)
        
        if urls:
            logger.info(f"🎵 Music request from {message.author} in #{getattr(message.channel, 'name', '?')}: {len(urls)} link(s)")
            
            for url in urls:
                try:
                    # Determine if it's a valid music link (YouTube, TikTok, Shazam, or Spotify)
                    if not any(domain in url.lower() for domain in ['youtube.com', 'youtu.be', 'tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com', 'shazam.com', 'spotify.com']):
                        logger.debug(f"Skipping non-music URL: {url}")
                        continue
                    
                    # Extract metadata from link
                    metadata = None
                    try:
                        metadata = await extract_link_metadata(url, message.content or '')
                    except asyncio.TimeoutError:
                        logger.warning(f"⚠️ Link extraction timeout: {url}")
                        await private.reply(f"⏱️ **Extraction Timeout**: Link took too long to process: {url}\n\nThis usually means:\n• The video/page is not loading properly\n• The service is experiencing issues")
                        continue
                    except Exception as extract_err:
                        logger.warning(f"⚠️ Link extraction error: {extract_err}")
                        await private.reply(f"⚠️ Could not extract song information from: {url}\n\nError: {str(extract_err)[:100]}")
                        continue
                    
                    if not metadata:
                        logger.warning(f"⚠️ Could not extract metadata from link: {url}")
                        # Provide more helpful error message
                        error_msg = f"⚠️ Could not extract song information from: {url}\n\n**Troubleshooting:**\n"
                        if 'youtube.com' in url.lower() or 'youtu.be' in url.lower():
                            error_msg += "• YouTube: Make sure it's a music video (not a playlist, livestream, or private video)\n• Try a direct video link instead of a shortened URL\n"
                        elif 'tiktok.com' in url.lower():
                            error_msg += "• TikTok: Make sure the video is public and not deleted\n"
                        elif 'spotify.com' in url.lower():
                            error_msg += "• Spotify: Make sure it's a direct track, album, or playlist link (not an artist profile)\n"
                        error_msg += "• If the problem persists, contact the admin"
                        await private.reply(error_msg)
                        continue
                    
                    # Check if it's a playlist
                    if metadata.get('type') == 'playlist':
                        logger.info(f"📋 Detected playlist: {metadata.get('name')} with {len(metadata.get('tracks', []))} tracks")
                        
                        # Get user info
                        discord_user = message.author.name
                        plex_user = DISCORD_TO_PLEX_MAPPING.get(discord_user.lower())
                        
                        if not plex_user:
                            logger.warning(f"⚠️ Discord user '{discord_user}' not found in DISCORD_TO_PLEX_MAPPING")
                            await private.reply(f"⚠️ Your Discord user '{discord_user}' is not set up in the music system. Contact admin to add you.")
                            continue
                        
                        # Verify user's library exists
                        user_library = get_user_library(plex_user)
                        if not user_library or not os.path.exists(user_library):
                            logger.error(f"❌ Library not found for user '{plex_user}' at {user_library}")
                            await private.reply(f"❌ Library not found for user '{plex_user}'. Contact admin.")
                            continue
                        
                        # Send initial message
                        playlist_name = metadata.get('name', 'Unknown Playlist')
                        tracks = metadata.get('tracks', [])
                        total_tracks = len(tracks)
                        source = metadata.get('source') or 'link'
                        
                        initial_embed = discord.Embed(
                            title="📋 Playlist Download Started",
                            description=f"**{playlist_name}**\n{total_tracks} tracks",
                            color=discord.Color.blue()
                        )
                        initial_embed.add_field(name="👤 User", value=plex_user, inline=False)
                        initial_embed.add_field(name="⏳ Status", value="Starting downloads...", inline=False)
                        
                        status_message = await private.reply(embed=initial_embed)

                        # A playlist that is an album Deezer has: each song is that
                        # album's track, by its real artist (album_match).
                        deezer_album = await asyncio.to_thread(album_match.find_album, playlist_name, tracks)
                        if deezer_album:
                            label_album_tracks(tracks, deezer_album)
                            lidarr_album_started, lidarr_album_match = False, None
                        else:
                            lidarr_album_started, lidarr_album_match = await try_lidarr_playlist_download(
                                playlist_name, tracks
                            )
                        if lidarr_album_started:
                            initial_embed.set_field_at(
                                1,
                                name="⏳ Status",
                                value=(
                                    f"Lidarr is searching the full album: "
                                    f"{lidarr_album_match['artist']} - {lidarr_album_match['album']}"
                                ),
                                inline=False,
                            )
                            await status_message.edit(embed=initial_embed)
                            logger.info(
                                f"📋 Playlist '{playlist_name}' handled as a whole album by Lidarr; "
                                "skipping per-track fallback downloads"
                            )

                        # Download each track only when the playlist was not accepted as an album.
                        downloaded_tracks = []
                        failed_tracks = []
                        
                        tracks_to_download = [] if lidarr_album_started else tracks
                        for idx, track in enumerate(tracks_to_download, 1):
                            try:
                                artist = track['artist']
                                title = track['title']
                                album = track.get('album', '')
                                
                                logger.info(f"📥 [{idx}/{total_tracks}] Downloading: {artist} - {title}")
                                
                                # Try immediate download — pass the track's own URL if available,
                                # not the outer playlist URL
                                track_url = track.get('url') or track.get('uri') or None
                                download_success, download_mode, download_result = await try_immediate_download(
                                    plex_user, artist, title, album, track_url, f"{source}_playlist",
                                    request_context=playlist_track_context(source, track),
                                    discord_user=discord_user,
                                )
                                
                                if download_success or download_mode == "existing":
                                    # Use the artist/title that download_track actually resolved —
                                    # it may have corrected a swap (e.g. "Bring Me To Life" / "Evanescence"
                                    # → "Evanescence" / "Bring Me To Life") or stripped a fan-channel prefix.
                                    _meta = download_result.get('metadata', {}) if isinstance(download_result, dict) else {}
                                    _resolved_artist = _meta.get('artist') or artist
                                    _resolved_title  = _meta.get('title')  or title
                                    downloaded_tracks.append({
                                        'str': f"{_resolved_artist} - {_resolved_title}",
                                        'artist': _resolved_artist,
                                        'title': _resolved_title,
                                        'album': _meta.get('album') or album,
                                        'file': download_result.get('file_path') if isinstance(download_result, dict) else None,
                                    })
                                    logger.info(f"✅ [{idx}/{total_tracks}] Success: {_resolved_artist} - {_resolved_title}")
                                else:
                                    failed_tracks.append(f"{artist} - {title}")
                                    logger.warning(f"❌ [{idx}/{total_tracks}] Failed: {artist} - {title}")
                                
                                # Update status every 5 tracks or at the end
                                if idx % 5 == 0 or idx == total_tracks:
                                    try:
                                        progress_embed = discord.Embed(
                                            title="📋 Playlist Download In Progress",
                                            description=f"**{playlist_name}**",
                                            color=discord.Color.blue()
                                        )
                                        progress_embed.add_field(
                                            name="📊 Progress",
                                            value=f"{idx}/{total_tracks} processed",
                                            inline=False
                                        )
                                        progress_embed.add_field(
                                            name="✅ Downloaded",
                                            value=str(len(downloaded_tracks)),
                                            inline=True
                                        )
                                        progress_embed.add_field(
                                            name="❌ Failed",
                                            value=str(len(failed_tracks)),
                                            inline=True
                                        )
                                        await status_message.edit(embed=progress_embed)
                                    except Exception as e:
                                        logger.debug(f"Could not update status message: {e}")
                            
                            except Exception as track_err:
                                logger.error(f"❌ Error downloading track {idx}: {track_err}")
                                failed_tracks.append(f"{track.get('artist', 'Unknown')} - {track.get('title', 'Unknown')}")
                        
                        # Trigger final Plex refresh
                        try:
                            logger.info(f"🔄 Triggering final Plex refresh for playlist")
                            await refresh_plex_library(plex_user)
                        except Exception as e:
                            logger.error(f"Could not refresh Plex: {e}")
                        
                        # Send final summary
                        summary_embed = discord.Embed(
                            title=("📀 Album Download Started" if lidarr_album_started else "📋 Playlist Download Complete"),
                            description=f"**{playlist_name}**",
                            color=discord.Color.green() if not failed_tracks else discord.Color.orange()
                        )
                        summary_embed.add_field(
                            name="📊 Summary",
                            value=(
                                f"Lidarr search started for {total_tracks} tracks"
                                if lidarr_album_started else f"Total: {total_tracks} tracks"
                            ),
                            inline=False
                        )
                        summary_embed.add_field(
                            name="✅ Successfully Downloaded",
                            value=str(len(downloaded_tracks)),
                            inline=True
                        )
                        summary_embed.add_field(
                            name="❌ Failed",
                            value=str(len(failed_tracks)),
                            inline=True
                        )
                        
                        # Add list of downloaded tracks (up to 10)
                        if downloaded_tracks:
                            track_list = "\n".join(t['str'] for t in downloaded_tracks[:10])
                            if len(downloaded_tracks) > 10:
                                track_list += f"\n... and {len(downloaded_tracks) - 10} more"
                            summary_embed.add_field(
                                name="🎵 Downloaded Tracks",
                                value=track_list,
                                inline=False
                            )
                        
                        # Add list of failed tracks (up to 5)
                        if failed_tracks:
                            failed_list = "\n".join(failed_tracks[:5])
                            if len(failed_tracks) > 5:
                                failed_list += f"\n... and {len(failed_tracks) - 5} more"
                            summary_embed.add_field(
                                name="❌ Failed Tracks",
                                value=failed_list,
                                inline=False
                            )
                        
                        try:
                            # Delete the status/progress message
                            await status_message.delete()
                            logger.info(f"🗑️ Deleted playlist progress message")
                        except Exception as e:
                            logger.debug(f"Could not delete status message: {e}")
                        
                        # Delete original message
                        try:
                            await message.delete()
                            logger.info(f"🗑️ Deleted playlist request message from {message.author}")
                        except Exception as e:
                            logger.debug(f"Could not delete original message: {e}")
                        
                        # Send final summary as a NEW message (not an edit)
                        try:
                            await private.send(embed=summary_embed, delete_after=300)
                            logger.info(f"✅ Sent final playlist summary")
                        except Exception as e:
                            logger.error(f"Could not send final summary: {e}")
                        
                        # If the "playlist" is really just an album (a Spotify/YouTube
                        # playlist someone made that's just an artist's album track-for-
                        # track, named after that album), skip creating a Plex playlist —
                        # the tracks already get grouped as that album in the library, so
                        # a same-named playlist would just be redundant clutter.
                        #
                        # A direct Spotify /album/ link (metadata['is_album']) is trusted
                        # outright — it's a real album by definition, not a heuristic guess.
                        # Everything else (YouTube playlists, user-made Spotify playlists)
                        # still goes through the fuzzy name/consistency check, since the
                        # display name here is "Artist - Album" for messaging purposes and
                        # won't itself pass that heuristic's name-similarity threshold.
                        skip_playlist_as_album = bool(metadata.get('is_album')) or playlist_matches_own_album(
                            playlist_name, downloaded_tracks
                        ) or bool(deezer_album and deezer_album['coverage'] >= 0.8)
                        if skip_playlist_as_album:
                            logger.info(
                                f"📀 '{playlist_name}' matches its own album name — skipping redundant "
                                f"Plex playlist creation (tracks are already grouped as that album)"
                            )

                        # Create Plex playlist with downloaded tracks
                        if downloaded_tracks and PLEX_AVAILABLE and not skip_playlist_as_album:
                            try:
                                logger.info(f"📋 Creating Plex playlist '{playlist_name}' for user {plex_user}")

                                plex_url_pl = os.environ.get('PLEX_URL', '').strip()
                                if not plex_url_pl:
                                    logger.warning("PLEX_URL unavailable — skipping playlist creation")
                                else:
                                    import plex_auth
                                    plex_pl = PlexServer(plex_url_pl, plex_auth.admin_token())
                                    music_lib_pl = find_music_section(plex_pl)

                                    if not music_lib_pl:
                                        logger.warning(f"Could not find a Plex music library for user {plex_user} — skipping playlist")
                                    else:
                                        logger.info(f"📚 Using library '{music_lib_pl.title}' for playlist creation")

                                        # ── Search for each downloaded track in Plex ──
                                        # Uses fuzzy artist matching (case-insensitive, partial)
                                        # so minor tagging differences don't drop tracks.
                                        from difflib import SequenceMatcher

                                        def _artist_match(plex_track, want_artist: str) -> bool:
                                            candidates = [
                                                plex_track.grandparentTitle or '',
                                                getattr(plex_track, 'originalTitle', '') or '',
                                            ]
                                            want = want_artist.lower().strip()
                                            for c in candidates:
                                                c = c.lower().strip()
                                                if not c:
                                                    continue
                                                if c == want:
                                                    return True
                                                if SequenceMatcher(None, c, want).ratio() >= 0.85:
                                                    return True
                                            return False

                                        plex_tracks = []
                                        for t_info in downloaded_tracks:
                                            artist_name = t_info['artist']
                                            title_name  = t_info['title']
                                            try:
                                                results = music_lib_pl.searchTracks(title=title_name)
                                                match = None
                                                for pt in results:
                                                    if _artist_match(pt, artist_name):
                                                        match = pt
                                                        break
                                                if match:
                                                    plex_tracks.append(match)
                                                    logger.debug(f"  ✓ Plex track: {match.grandparentTitle} – {match.title}")
                                                else:
                                                    logger.warning(f"  ✗ Not found in Plex: {artist_name} – {title_name}")
                                            except Exception as _te:
                                                logger.warning(f"  Error searching Plex for '{artist_name} – {title_name}': {_te}")

                                        if plex_tracks:
                                            try:
                                                # createPlaylist on the admin server scoped
                                                # to the correct section works for all user types
                                                playlist_obj = plex_pl.createPlaylist(playlist_name, items=plex_tracks)
                                                logger.info(
                                                    f"✅ Created Plex playlist '{playlist_name}' "
                                                    f"with {len(plex_tracks)}/{len(downloaded_tracks)} tracks"
                                                )
                                            except Exception as _pe:
                                                logger.error(f"❌ Failed to create Plex playlist '{playlist_name}': {_pe}")
                                        else:
                                            logger.warning(f"No tracks found in Plex for playlist '{playlist_name}' — library scan may still be in progress")

                            except Exception as e:
                                logger.error(f"❌ Error creating Plex playlist for user {plex_user}: {e}", exc_info=True)

                        # Skip to next URL
                        continue
                    
                    # Regular single track handling
                    artist = metadata.get('artist', '').strip()
                    title = metadata.get('title', '').strip()
                    album = metadata.get('album', '').strip()
                    source = metadata.get('source', 'unknown')
                    
                    logger.info(f"✅ Extracted from {source}: {artist} - {title}")
                    
                    if not artist or not title:
                        await private.reply(f"⚠️ Could not extract artist/title from the link.\n\nGot: Artist='{artist}', Title='{title}'")
                        continue

                    # A video titled "Album - Track" ("Final Fantasy VI Piano
                    # Collections - Terra's Theme") names an album, not an
                    # artist: the album's artist and track are used instead of
                    # the video's own title and channel.
                    album_track = None
                    in_album = await asyncio.to_thread(album_match.album_for_song, artist, title,
                                                       metadata.get('video_duration') or 0)
                    if in_album:
                        artist, album = in_album['artist'], in_album['album']
                        if in_album['track']:
                            title, album_track = in_album['track']['title'], in_album['track']['id']
                        metadata.update(artist=artist, title=title, album=album)
                        # The link reader's ready-made context repeats them: rebuilt below.
                        for key in ('video_title', 'raw_title', 'video_uploader',
                                    'variant_context', 'request_context', 'context_note', 'notes'):
                            metadata.pop(key, None)
                    
                    # Get the user who posted the link
                    discord_user = message.author.name
                    logger.info(f"User who posted link: {discord_user}")
                    
                    # Map Discord user to Plex user
                    plex_user = DISCORD_TO_PLEX_MAPPING.get(discord_user.lower())
                    
                    if not plex_user:
                        logger.warning(f"⚠️ Discord user '{discord_user}' not found in DISCORD_TO_PLEX_MAPPING")
                        await private.reply(f"⚠️ Your Discord user '{discord_user}' is not set up in the music system. Contact admin to add you.")
                        continue
                    
                    # Verify user's library exists
                    user_library = get_user_library(plex_user)
                    if not user_library or not os.path.exists(user_library):
                        logger.error(f"❌ Library not found for user '{plex_user}' at {user_library}")
                        await private.reply(f"❌ Library not found for user '{plex_user}'. Contact admin.")
                        continue
                    
                    # Prepare display/requested defaults; actual request_context parsing
                    # happens after we build `request_context` below so we can include
                    # metadata-derived hints (video_title/video_uploader/etc.).
                    display_artist = artist
                    display_title = title
                    requested_artist = None
                    requested_title = None

                    # Final cycle check: abort if cycle started between top guard and here
                    if CYCLE_RUNNING or _any_cycle_running_file():
                        logger.info(f"🚫 Aborting queued request — cycle started mid-processing")
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        embed = discord.Embed(
                            title="🚫 Request Blocked — Download Cycle Running",
                            description=(
                                f"**{message.author.display_name}**, your request was blocked.\n\n"
                                f"A download cycle just started for **{CYCLE_USER or 'the system'}**.\n"
                                "Please resubmit once you see the cycle-complete notification. 🎵"
                            ),
                            color=discord.Color.red(),
                        )
                        embed.set_footer(text="Music AI | Requests temporarily blocked")
                        try:
                            sent = await private.send(embed=embed)
                            async with PENDING_REJECTION_LOCK:
                                PENDING_REJECTION_MESSAGES.append(sent)
                        except Exception:
                            pass
                        continue  # skip to next URL without downloading

                    # Send a temporary pending status message to keep only one final status
                    status_message = None
                    try:
                        status_message = await private.reply("⏳ Processing your request...")
                    except Exception as e:
                        logger.debug(f"Could not send interim processing status: {e}")

                    request_context = (
                        metadata.get('variant_context')
                        or metadata.get('request_context')
                        or metadata.get('context_note')
                        or metadata.get('notes')
                        or ''
                    ).strip()
                    try:
                        from link_extractor import build_request_context_text
                        if request_context:
                            # Preserve any existing metadata-derived context and
                            # append the raw user message for variant hints.
                            message_text = re.sub(r'\s+', ' ', str(message.content)).strip()
                            if message_text and 'message=' not in request_context:
                                request_context = f"{request_context}; message={message_text}"
                        else:
                            request_context = build_request_context_text(
                                message_text=message.content,
                                metadata=metadata,
                                artist=artist,
                                title=title,
                                album=album,
                            )
                    except Exception as _ctx_err:
                        logger.debug(f"Could not build request context: {_ctx_err}")

                    if album_track:
                        request_context = '; '.join(p for p in (request_context, f"deezer_track={album_track}") if p)
                    if request_context:
                        logger.info(f"📌 Built request_context: {request_context}")

                    # Parse the built request_context for explicit requested performer/title
                    def _parse_request_context_fields(req_ctx: str) -> dict:
                        parts = {}
                        if not req_ctx:
                            return parts
                        for p in re.split(r';\s*', req_ctx.strip()):
                            if '=' not in p:
                                continue
                            k, _, v = p.partition('=')
                            parts[k.strip().lower()] = v.strip()
                        return parts

                    parsed_ctx = _parse_request_context_fields(request_context)
                    if parsed_ctx.get('requested_track'):
                        rt = parsed_ctx.get('requested_track')
                        if ' - ' in rt:
                            ra, rtit = rt.split(' - ', 1)
                            display_artist = ra.strip() or display_artist
                            display_title = rtit.strip() or display_title
                            requested_artist = display_artist
                            requested_title = display_title
                        else:
                            requested_title = rt.strip()
                    elif parsed_ctx.get('video_uploader'):
                        vu = parsed_ctx.get('video_uploader')
                        if vu:
                            display_artist = vu
                            requested_artist = vu
                    elif parsed_ctx.get('video_title'):
                        vt = parsed_ctx.get('video_title')
                        if ' - ' in vt:
                            va, vtit = vt.split(' - ', 1)
                            display_artist = va.strip() or display_artist
                            display_title = vtit.strip() or display_title
                            requested_artist = display_artist
                            requested_title = display_title

                    # Record the request (now that request_context and requested_* are known)
                    tr_kwargs = {
                        'album': album,
                        'url': url,
                        'discord_user': discord_user,
                        'request_context': request_context,
                    }
                    if requested_artist:
                        tr_kwargs['requested_artist'] = requested_artist
                    if requested_title:
                        tr_kwargs['requested_title'] = requested_title

                    request_entry = track_request(
                        plex_user,
                        artist,
                        title,
                        f'music_request_{source}',
                        **tr_kwargs,
                    )

                    if requested_artist or requested_title:
                        logger.info(f"🎵 Music request queued: {artist} - {title} (requested: {requested_artist or display_artist} - {requested_title or display_title}) for user {plex_user} (from {source})")
                    else:
                        logger.info(f"🎵 Music request queued: {artist} - {title} for user {plex_user} (from {source})")

                    # Try to download immediately if downloader is available and not in use by app.py
                    download_success, download_mode, download_result = await try_immediate_download(
                        plex_user,
                        artist,
                        title,
                        album,
                        url,
                        source,
                        request_context=request_context,
                        discord_user=discord_user,
                    )
                    record_request_outcome(plex_user, request_entry, download_mode, download_result)

                    # If the downloader resolved a final artist/title, use those
                    # values for the confirmation embed instead of the original request.
                    try:
                        if isinstance(download_result, dict):
                            final_meta = download_result.get('metadata', {}) or {}
                            if final_meta.get('artist'):
                                display_artist = final_meta['artist']
                            if final_meta.get('title'):
                                display_title = final_meta['title']
                    except Exception:
                        pass

                    # Send confirmation with extracted metadata
                    embed = discord.Embed(title="🎵 Music Request Received", color=discord.Color.green())
                    embed.add_field(name="📀 Artist", value=display_artist, inline=False)
                    embed.add_field(name="🎵 Title", value=display_title, inline=False)
                    if album:
                        embed.add_field(name="💿 Album", value=album, inline=False)
                    embed.add_field(name="👤 User", value=plex_user, inline=False)
                    embed.add_field(name="🔗 Source", value=source.upper(), inline=False)
                    choices = download_result.get('choices') if isinstance(download_result, dict) else None
                    if choices:
                        embed.add_field(name="🙋 Your choice", value="\n".join(choices)[:1024], inline=False)
                    
                    if download_mode == "immediate":
                        # Download succeeded immediately
                        embed.add_field(name="✅ Status", value="✅ Downloaded immediately!", inline=False)
                        embed.add_field(name="📝 Note", value="Your track is being processed and will appear in your library shortly.", inline=False)
                        # Show which downloader/tool was used and metadata source
                        try:
                            _tool = download_result.get('download_tool') if isinstance(download_result, dict) else None
                            _meta = download_result.get('metadata', {}).get('source') if isinstance(download_result, dict) and download_result.get('metadata') else None
                            if _tool:
                                embed.add_field(name="🛠️ Tool", value=str(_tool), inline=False)
                            if _meta:
                                embed.add_field(name="📊 Metadata", value=str(_meta), inline=False)
                        except Exception:
                            pass
                        
                        # Delete the original message after successful download
                        try:
                            await message.delete()
                            logger.info(f"🗑️ Deleted music request message from {message.author}")
                        except Exception as e:
                            logger.debug(f"Could not delete message: {e}")
                    elif download_mode == "existing":
                        # Track already exists; inform the user that no download occurred
                        embed.add_field(name="ℹ️ Status", value="Track already exists — no download performed.", inline=False)
                        file_path_display = download_result.get('file_path') if isinstance(download_result, dict) and download_result.get('file_path') else 'See logs'
                        embed.add_field(name="📁 File", value=file_path_display, inline=False)
                        embed.add_field(name="🛠️ Tool", value="none", inline=False)
                        embed.add_field(name="📊 Metadata", value="existing", inline=False)
                        try:
                            await message.delete()
                            logger.info(f"🗑️ Deleted music request message from {message.author}")
                        except Exception as e:
                            logger.debug(f"Could not delete message: {e}")
                    else:
                        # If immediate download attempt returned an error, show it; otherwise show queued
                        if not download_success and isinstance(download_result, dict) and download_result.get('error'):
                            embed.add_field(name="❌ Status", value="Download failed during immediate attempt", inline=False)
                            embed.add_field(name="🧾 Error", value=download_result.get('error'), inline=False)
                        else:
                            embed.add_field(name="⏳ Status", value="⏳ Queued for next analysis cycle", inline=False)
                            embed.add_field(name="📝 Note", value="Processing will begin when app.py's analysis cycle starts. Check back in a moment!", inline=False)
                    
                    embed.set_footer(text=f"Request #{len(REQUEST_HISTORY.get(plex_user, []))}")
                    
                    if status_message:
                        try:
                            # content=None: the "⏳ Processing" text goes, only the result stays.
                            await status_message.edit(content=None, embed=embed)
                        except Exception as edit_exc:
                            logger.debug(f"Could not edit interim status message: {edit_exc}")
                            try:
                                await private.reply(embed=embed)
                            except Exception as reply_exc:
                                # Common case: original message was deleted -> 'In message_reference: Unknown message'
                                try:
                                    if isinstance(reply_exc, discord.HTTPException):
                                        logger.debug(f"Reply failed (HTTPException), falling back to channel.send: {reply_exc}")
                                except Exception:
                                    logger.debug(f"Reply failed, falling back to channel.send: {reply_exc}")

                                try:
                                    await private.send(embed=embed)
                                except Exception as send_exc:
                                    logger.error(f"Failed to send embed to channel as fallback: {send_exc}")
                    else:
                        try:
                            await private.reply(embed=embed)
                        except Exception as reply_exc:
                            # Common case: original message was deleted -> 'In message_reference: Unknown message'
                            try:
                                if isinstance(reply_exc, discord.HTTPException):
                                    logger.debug(f"Reply failed (HTTPException), falling back to channel.send: {reply_exc}")
                            except Exception:
                                logger.debug(f"Reply failed, falling back to channel.send: {reply_exc}")

                            try:
                                await private.send(embed=embed)
                            except Exception as send_exc:
                                logger.error(f"Failed to send embed to channel as fallback: {send_exc}")
                
                except Exception as e:
                    logger.error(f"❌ Error processing music request: {e}", exc_info=True)
                    err_text = f"❌ Error processing request: {str(e)}"
                    try:
                        await private.reply(err_text)
                    except Exception as reply_err:
                        try:
                            if isinstance(reply_err, discord.HTTPException):
                                logger.debug(f"Reply failed (HTTPException), sending plain message to channel: {reply_err}")
                        except Exception:
                            logger.debug(f"Reply failed, sending plain message to channel: {reply_err}")
                        try:
                            await private.send(err_text)
                        except Exception as send_err:
                            logger.error(f"Failed to send error message to channel: {send_err}")
        
            # A request may have moved the Mullvad relay to reach a
            # region-locked video; put the configured relay back.
            if geo_relay.active():
                await asyncio.get_event_loop().run_in_executor(None, geo_relay.restore)

        # Return after processing music-requests messages
        return
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # SECTION 3: HANDLE COMMANDS IN ALL OTHER CHANNELS
    # ═══════════════════════════════════════════════════════════════════════════════
    
    # Process any commands in the message
    await bot.process_commands(message)
    
async def _requests_channel():
    return await _resolve_configured_channel(DISCORD_MAIN_CHANNEL, DISCORD_MAIN_CHANNEL_NAME)


def _in_requests_channel(interaction) -> bool:
    """The command was used in the requests channel or in a thread under it."""
    channel = interaction.channel
    if DISCORD_MAIN_CHANNEL:
        return DISCORD_MAIN_CHANNEL in (getattr(channel, 'id', None), getattr(channel, 'parent_id', None))
    name = getattr(getattr(channel, 'parent', None) or channel, 'name', '') or ''
    return bool(DISCORD_MAIN_CHANNEL_NAME) and name.lower() == DISCORD_MAIN_CHANNEL_NAME.lower()


@bot.event
async def on_message(message: discord.Message):
    """Link requests, without any extra step: a link posted in the requests
    channel is taken down at once and handled in the poster's private
    requests thread (created on their first request); a link posted in that
    thread is handled right there. Requests aren't taken by DM."""
    if message.author == bot.user or message.author.bot or message.guild is None:
        return
    in_requests = False
    if DISCORD_MAIN_CHANNEL:
        in_requests = message.channel.id == DISCORD_MAIN_CHANNEL
    elif DISCORD_MAIN_CHANNEL_NAME:
        in_requests = getattr(message.channel, "name", "").lower() == DISCORD_MAIN_CHANNEL_NAME.lower()
    in_own_thread = isinstance(message.channel, discord.Thread) and \
        _load_request_threads().get(str(message.author.id)) == message.channel.id
    if not (in_requests or in_own_thread):
        await bot.process_commands(message)
        return
    try:
        urls = extract_urls_from_text(message.content or "") if extract_urls_from_text else \
            re.findall(r"https?://[^\s]+", message.content or "")
    except Exception:
        urls = []
    if not urls:
        return
    if in_own_thread:
        thread = message.channel
    else:
        try:
            await message.delete()
        except Exception as exc:
            logger.debug(f"Could not take the request down from the channel: {exc}")
        thread = await _request_thread(message.channel, message.author)
        if thread is None:
            try:
                await message.channel.send(
                    f"{message.author.mention} I couldn't open your private requests thread; please tell the admin.",
                    delete_after=60, allowed_mentions=discord.AllowedMentions(users=[message.author]))
            except Exception:
                pass
            return
        await _ThreadReply(thread).send(f"📨 {message.author.mention} requested: {message.content}",
                                        suppress_embeds=True)
    await _handle_request(_RequestShim(message.author, thread, message.guild, message.content, message.id),
                          _ThreadReply(thread), requested=True)


@bot.tree.command(name="request", description="Request a song, album or playlist by link — handled in your private thread")
@app_commands.describe(link="A YouTube, Spotify, TikTok, Instagram or Shazam link")
async def request_command(interaction: discord.Interaction, link: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    own = _load_request_threads().get(str(interaction.user.id))
    channel = interaction.channel if isinstance(interaction.channel, discord.Thread) and \
        interaction.channel.id == own else None
    thread = channel or await _request_thread(await _requests_channel(), interaction.user)
    if thread is None:
        await interaction.followup.send("I couldn't open your private requests thread; please tell the admin.",
                                        ephemeral=True)
        return
    await interaction.followup.send(f"🔒 On it — follow it in your private thread: {thread.mention}", ephemeral=True)
    await _ThreadReply(thread).send(f"📨 {interaction.user.mention} requested: {link}", suppress_embeds=True)
    await _handle_request(_RequestShim(interaction.user, thread, interaction.guild, link, interaction.id),
                          _ThreadReply(thread), requested=True)


@bot.tree.command(name="library_status", description="Check library status for your Plex account")
async def library_status(interaction: discord.Interaction):
    """Show library statistics for the requesting user's Plex account."""
    discord_user = interaction.user.name
    logger.debug(f"📊 /library_status invoked by {discord_user}")

    # Block if a cleanup is running
    try:
        if await block_interaction_if_cleanup(interaction):
            return
    except Exception:
        logger.debug("Error while checking cleanup state for interaction")
    
    # Map Discord user to Plex user
    plex_user = DISCORD_TO_PLEX_MAPPING.get(discord_user.lower())
    
    if not plex_user:
        logger.warning(f"⚠️ Discord user '{discord_user}' not found in DISCORD_TO_PLEX_MAPPING")
        await interaction.response.send_message(
            f"❌ Your Discord user '{discord_user}' is not set up in the music system.\nPlease contact admin to add you.",
            ephemeral=True
        )
        return
    
    new, songs, soon_to_go = count_user_tracks(plex_user)
    recent_requests = get_recent_requests(plex_user, limit=3)
    
    # Get actual music directory for display (not DATA_DIR)
    user_folder = USER_MAPPING.get(plex_user)
    if not user_folder and plex_user in USER_MAPPING.values():
        user_folder = plex_user
    
    # For unified libraries, use MUSIC_BASE_DIR directly
    if user_folder:
        music_library_path = os.path.join(MUSIC_BASE_DIR, user_folder)
    else:
        music_library_path = MUSIC_BASE_DIR

    # Trigger a Plex library refresh so counts reflect the latest state
    refresh_status = ""
    try:
        await refresh_plex_library(plex_user)
        refresh_status = " *(library refreshed)*"
        logger.info(f"🔄 Plex library refresh triggered for '{plex_user}' via /library_status")
    except Exception as _re:
        logger.debug(f"Could not refresh Plex library for '{plex_user}': {_re}")

    logger.info(f"📊 Library status retrieved for user '{plex_user}': {songs} songs, {new} new, {soon_to_go} soon to go")
    
    embed = discord.Embed(title=f"📊 {plex_user.capitalize()}'s Library Status{refresh_status}", color=discord.Color.blue())
    embed.add_field(name="🆕 On probation", value=str(new), inline=True)
    embed.add_field(name="⏳ Soon to go", value=str(soon_to_go), inline=True)
    embed.add_field(name="🎵 Total Tracks", value=str(songs), inline=False)
    
    if recent_requests:
        requests_list = "\n".join([f"  • {req['artist']} - {req['title']}" for req in recent_requests[-3:]])
        embed.add_field(name="📝 Recent Requests", value=requests_list, inline=False)
    
    embed.set_footer(text=f"Library Path: {music_library_path or 'Not Found'}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


def _cleanup_summary(log_file: str) -> List[str]:
    """The counts of the merger's closing line ("AlbumArtistMerger completed in
    12.0s — artist normalisations: 22, album dir merges: 0, …"), the ones
    that did something."""
    try:
        with open(log_file, errors='replace') as fh:
            last = [line for line in fh if 'AlbumArtistMerger completed in' in line][-1]
    except (OSError, IndexError):
        return []
    counts = [part.strip() for part in last.split('—', 1)[-1].split(',')]
    return [c for c in counts if not c.endswith(': 0')] or ["nothing to do"]


async def _run_clean_library(interaction: discord.Interaction, preview: bool) -> None:
    """Admin only: tidy the shared library in the background
    (album_artist_merger.py, every pass) and report in the admin's private
    thread. A preview only reports what would change. One cleanup runs at a
    time; song requests wait while it does."""
    global CLEANUP_RUNNING, CLEANUP_OWNER
    if not _in_requests_channel(interaction):
        await interaction.response.send_message("❌ Use this command in the music requests channel.", ephemeral=True)
        return
    if not _is_admin(interaction):
        await interaction.response.send_message("❌ Only the admin can clean up the shared library.", ephemeral=True)
        return
    if CLEANUP_RUNNING:
        await interaction.response.send_message(
            f"❌ A library cleanup is already running for **{CLEANUP_OWNER}**. Please try again later.", ephemeral=True)
        return
    CLEANUP_RUNNING, CLEANUP_OWNER = True, interaction.user.name
    await interaction.response.defer(ephemeral=True)
    thread = await _request_thread(interaction.channel, interaction.user)
    reply = _ThreadReply(thread) if thread is not None else None
    title = "🧹 Library cleanup" + (" preview" if preview else "")
    log_file = os.path.join(DATA_DIR, 'library_cleanup.log')
    logger.info(f"{title} started by {interaction.user.name}")
    try:
        started = discord.Embed(
            title=f"{title} started", color=discord.Color.orange(),
            description=("Nothing is changed: this lists what a cleanup would do. " if preview else "")
                        + "One folder per artist and per album (Lidarr's names win; Lidarr moves its own files), "
                          "loose files filed, albums matched to Lidarr's, duplicate songs (same recording by "
                          "sound) moved to the quarantine folder, Plex's split albums joined."
                        + ("" if preview else " Song requests wait until it is done."))
        status = await reply.send(embed=started) if reply else _NoMessage()
        await interaction.followup.send(f"🧹 Started — follow it in {thread.mention}." if thread else "🧹 Started.",
                                        ephemeral=True)
        cmd = ['python3', '/app/album_artist_merger.py', '--pass', 'all'] + (['--dry-run'] if preview else [])
        with open(log_file, 'wb') as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, close_fds=True, env=os.environ.copy())
            await asyncio.get_event_loop().run_in_executor(None, proc.wait)
        summary = _cleanup_summary(log_file)
        ok = proc.returncode == 0
        done = discord.Embed(title=f"{title} finished" + ("" if ok else " with errors"),
                             color=discord.Color.green() if ok else discord.Color.red(),
                             timestamp=discord.utils.utcnow())
        done.add_field(name="📊 Would do" if preview else "📊 Done", inline=False,
                       value="\n".join(f"• {line}" for line in summary[:10]) or "No summary in the log.")
        done.add_field(name="📝 Log", value=f"`{log_file}`", inline=False)
        try:
            await status.edit(embed=done)
        except Exception:
            if reply:
                await reply.send(embed=done)
    except Exception as e:
        logger.error(f"❌ Library cleanup failed: {e}", exc_info=True)
        if reply:
            await reply.send(embed=discord.Embed(title=f"{title} failed", description=str(e)[:1000],
                                                 color=discord.Color.red()))
    finally:
        CLEANUP_RUNNING, CLEANUP_OWNER = False, None
        async with PENDING_REJECTION_LOCK:
            for message in list(PENDING_REJECTION_MESSAGES):
                try:
                    await message.delete()
                except Exception:
                    pass
            PENDING_REJECTION_MESSAGES.clear()


def _is_admin(interaction: discord.Interaction) -> bool:
    """The configured admin, or the server's owner: Discord shows the owner
    every command, and a command they can see should not refuse them."""
    guild = interaction.guild
    if guild is None:
        return False
    if getattr(guild, 'owner_id', None) == interaction.user.id:
        return True
    admin = _admin_member(guild)
    return admin is not None and admin.id == interaction.user.id


@bot.tree.command(name="one_star_delete", description="Admin: whether rating a song one star in Plex deletes it")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)   # hidden from members without Manage Server
@app_commands.describe(state="on: your one-star rating deletes the song; off: it is left alone. Empty: show the setting")
@app_commands.choices(state=[app_commands.Choice(name="on", value="on"),
                             app_commands.Choice(name="off", value="off")])
async def one_star_delete(interaction: discord.Interaction, state: Optional[app_commands.Choice[str]] = None):
    """The admin turns one-star deletion on or off (runtime_settings
    ONE_STAR_DELETE): the one-star watcher checks it before every pass.
    Without a state, the current setting is shown."""
    import runtime_settings
    if not _in_requests_channel(interaction):
        await interaction.response.send_message("❌ Use this command in the music requests channel.", ephemeral=True)
        return
    if not _is_admin(interaction):
        await interaction.response.send_message("❌ Only the admin can change this.", ephemeral=True)
        return
    if state is not None:
        runtime_settings.set_value('ONE_STAR_DELETE', state.value == 'on')
        logger.info(f"⭐ One-star deletion turned {state.value} by {interaction.user.name}")
    if runtime_settings.get_bool('ONE_STAR_DELETE', True):
        text = ("⭐ One-star deletion is **on**: a song you rate exactly one star in Plex is deleted within a "
                "minute, for everyone. Nothing is blacklisted, so it can be requested or downloaded again.")
    else:
        text = ("⭐ One-star deletion is **off**: your one-star ratings delete nothing. A song two users rate "
                "one star is still first to go when disk space runs low.")
    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="clean_library", description="Admin: tidy the library (artists, albums, duplicates, Lidarr)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)   # hidden from members without Manage Server
@app_commands.describe(preview="True: only list what would change")
async def clean_library(interaction: discord.Interaction, preview: bool = False):
    await _run_clean_library(interaction, preview=preview)


async def main():
    """Run the Discord bot with improved DNS error handling."""
    if not DISCORD_BOT_TOKEN:
        logger.error("❌ DISCORD_BOT_TOKEN not set in environment!")
        return
    
    logger.info("🤖 Starting Music AI Discord Bot...")
    logger.info(f"🧩 Process role: {PROCESS_ROLE}")
    logger.info(f"🎮 APP_USER_NAME: {os.getenv('APP_USER_NAME', '')}")
    logger.info(f"🎵 Music base directory: {MUSIC_BASE_DIR}")
    logger.info(f"💾 Data directory: {DATA_DIR}")
    logger.info(f"📂 Request database: {REQUESTS_DB}")
    load_request_history()
    
    # The VPN comes up with the container, so the first logins can fail on
    # DNS; keep trying (backoff capped at a minute) until Discord answers.
    retry_count = 0
    while True:
        try:
            logger.info("🔗 Connecting to Discord...")
            async with bot:
                await bot.start(DISCORD_BOT_TOKEN)
            break
        except discord.errors.LoginFailure:
            logger.error("❌ Failed to login with Discord token. Check DISCORD_BOT_TOKEN.")
            break
        except (OSError, aiohttp.ClientConnectorDNSError, socket.gaierror) as e:
            retry_count += 1
            wait_time = min(2 ** retry_count, 60)
            logger.warning(f"⚠️ Network error (attempt {retry_count}): {type(e).__name__}")
            logger.info(f"🔄 Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)
            # Leaving `async with bot` closed its HTTP connector; a retry on
            # it fails with "Session is closed" — the bot needs a fresh one.
            bot.http.connector = discord.utils.MISSING
            bot.clear()
        except Exception as e:
            logger.error(f"❌ Discord bot error: {e}", exc_info=True)
            break

def notify_discord_error(error_message: str, delete_after: int = 300):
    """
    Queue an error message to Discord for asynchronous sending.
    This is a synchronous wrapper around send_error_to_discord for use in non-async contexts.
    Writes to a JSON line file that is processed by the background check_arl_and_notifications task.
    
    Args:
        error_message: Error text to send
        delete_after: Seconds after which to delete the message (0 to keep indefinitely; default 300s = 5 minutes)
    """
    try:
        # Queue the error in the notifications file
        entry = {
            'type': 'error',
            'message': error_message,
            'delete_after': delete_after,
            'timestamp': datetime.now().isoformat()
        }
        # Append to notification file (creating if necessary)
        _notif_file = os.path.join(DATA_DIR, 'discord_notifications.jsonl')
        os.makedirs(os.path.dirname(_notif_file), exist_ok=True)
        with open(_notif_file, 'a') as f:
            f.write(json.dumps(entry) + '\n')
        logger.debug(f"Queued Discord error notification (will auto-delete after {delete_after}s)")
    except Exception as e:
        logger.debug(f"Could not queue Discord error notification: {e}")

if __name__ == '__main__':
    import metadata_proxy
    metadata_proxy.install()          # lookups outside the VPN, downloads in it

    logger.info("=" * 60)
    logger.info("🎵 Music AI Discord Bot - Initialization")
    logger.info("=" * 60)
    asyncio.run(main())
