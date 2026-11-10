import os
import logging
import json
import shutil
import csv
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from plexapi.server import PlexServer
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.neighbors import NearestNeighbors
import requests
import xml.etree.ElementTree as ET
from mutagen.id3 import ID3
from mutagen.easyid3 import EasyID3
from mutagen import File as MutagenFile
import sqlite3
import time
import subprocess
import select
from collections import deque
import socket
import sys
import warnings
# Suppress third-party DeprecationWarning about the stdlib 'audioop' module (comes from discord.player)
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*audioop.*")
import geo_relay
import spotify_web
import difflib
import re
import hashlib

import users
from naming import parse_artist_field
import request_history
from paths import host_to_container as convert_host_path_to_container_path
from rate_limit import (
    rate_limiter, is_rate_limit_error, handle_rate_limit,
    with_rate_limit, requests_get_with_limit, run_subprocess_with_limit,
)



# Get user context from environment (set by entrypoint.py)
APP_USER_NAME = os.environ.get("APP_USER_NAME", "default")
APP_USER_INDEX = os.environ.get("APP_USER_INDEX", "0")
PLEX_USERNAME = os.environ.get("PLEX_USERNAME", "")  # Plex username for this app user (for token lookup)
CYCLE_ON_STARTUP = os.environ.get("CYCLE_ON_STARTUP", "true").strip().lower() in ("true", "1", "yes", "y", "on")
ACTIVE_ANALYZER = None
# The cycle's request stage and the background request processor (both in a
# cycle's process) must not work on the queue at once: each would download
# every pending request, giving duplicate files ("Song (2).mp3").
_DISCORD_QUEUE_LOCK = threading.Lock()

# Setup logging with user context
class UserContextFormatter(logging.Formatter):
    """Custom formatter to include user context in log messages."""
    def format(self, record):
        # Safely add user attribute to record
        if not hasattr(record, 'user'):
            record.user = APP_USER_NAME
        return super().format(record)

# Configure logging with user-aware format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - [USER: %(user)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Apply custom formatter to root logger and all existing handlers
root_logger = logging.getLogger()
formatter = UserContextFormatter('%(asctime)s - [%(levelname)s] - [USER: %(user)s] - %(message)s')
for handler in root_logger.handlers:
    handler.setFormatter(formatter)

# Avoid logging startup context during import. entrypoint.py imports this module
# for class access when initializing Plex libraries before spawning per-user cycles.

# Import Discord notification functions (for error queuing) and allow reloading
# of the Discord->Plex mapping from a Wizarr file during cycles.
try:
    import discord_bot
    notify_discord_error = getattr(discord_bot, 'notify_discord_error', None)
    if notify_discord_error is None:
        def notify_discord_error(msg, delete_after=300):
            logger.debug(f"Discord notify function not found, skipping: {msg}")
except ImportError:
    discord_bot = None
    # Fallback: define a no-op function if discord_bot is not available
    def notify_discord_error(msg, delete_after=300):
        logger.debug(f"Discord not available, skipping error notification: {msg}")






# The VPN itself is driven by downloader.VPNConnector (Mullvad daemon).
def _check_vpn_config():
    mullvad_account = os.environ.get("MULLVAD_ACCOUNT_NUMBER", "").strip()
    if mullvad_account:
        logger.info(f"✓ Mullvad VPN will be used (account: {mullvad_account[:5]}...)")
        return True
    logger.info("ℹ️  No VPN configured — downloads will use standard connection")
    return False

VPN_ENABLED = _check_vpn_config()

# Configuration
PLEX_URL = os.environ.get("PLEX_URL", "http://localhost:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN")
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music/albums")
ALBUM_DIR = os.environ.get("ALBUM_DIR", "/music/albums")
PLEX_MUSIC_SECTION = os.environ.get("PLEX_MUSIC_SECTION", "Music")
# Each cycle fills two playlists for its user: the trending songs it fetched,
# and the songs picked from their taste (similar tracks, albums they love).
TRENDING_PLAYLIST = os.environ.get("TRENDING_PLAYLIST_NAME", "Trending Now for You")
SIMILAR_PLAYLIST = os.environ.get("SIMILAR_PLAYLIST_NAME", "You Might Like")
CYCLE_PLAYLISTS = {'trending': TRENDING_PLAYLIST, 'similar': SIMILAR_PLAYLIST}
# Other titles a cycle playlist is recognised by; it is renamed to its
# configured name in place, so a playlist pinned in Plexamp stays pinned.
PLAYLIST_ALIASES = {'trending': ("Trending Now",)}
SCORE_THRESHOLD = float(os.environ.get("SCORE_THRESHOLD", "0.3"))
# Every song lives in albums/; what is new or soon to go is kept as symlinks
# outside the library (library_state). TEMP_DOWNLOAD_DIR is where MusicAI's
# own picks land — albums/, marked new.
TEMP_DOWNLOAD_DIR = os.environ.get("TEMP_DOWNLOAD_DIR", ALBUM_DIR)
STAGING_DIR = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "staging")
SOON_TO_GO_RESCUE_RATING = float(os.environ.get("SOON_TO_GO_RESCUE_RATING", "8"))  # Plex 0-10: 4 stars
SOON_TO_GO_PLAYLIST = "Soon to go"
# Album completion is for albums a user clearly loves: ALBUM_COMPLETION_MIN_LOVED
# of its songs scoring ALBUM_COMPLETION_SCORE (a graduated song) or rated 4★+.
# Trending picks skip an artist the user clearly doesn't like: rated 1★ or
# less with nothing rated 3★ or more (Plex ratings are 0–10), or several songs
# in the library a month or more and none ever played.
DISLIKED_RATING, LIKED_RATING = 2, 6
UNPLAYED_ARTIST_SONGS, UNPLAYED_ARTIST_DAYS = 5, 30
ALBUM_COMPLETION_SCORE = float(os.environ.get("ALBUM_COMPLETION_SCORE", "0.55"))
ALBUM_COMPLETION_MIN_LOVED = int(os.environ.get("ALBUM_COMPLETION_MIN_LOVED", "3"))
ALBUM_COMPLETION_MAX_ALBUMS = int(os.environ.get("ALBUM_COMPLETION_MAX_ALBUMS", "2"))
ALBUM_COMPLETION_MAX_TRACKS = int(os.environ.get("ALBUM_COMPLETION_MAX_TRACKS", "30"))
EXPORTIFY_DIR = os.environ.get("EXPORTIFY_DIR", "/music/exportify")
DB_PATH = os.environ.get("DB_PATH", "/app/data/music_ai.db")
# A cycle's threads share this database: scoring every track holds one long
# write while queued requests are downloaded alongside. Wait for it rather than
# failing after SQLite's default 5 seconds ("database is locked").
DB_TIMEOUT = 120
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")

# Per-user lock file path based on user name
LOCK_FILE = os.path.join(os.path.dirname(DB_PATH), f"{APP_USER_NAME}_trending_lock.json")
DEEZER_ARL = os.environ.get("DEEZER_ARL", "")
DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "")
LAST_FM_API_KEY = os.environ.get("LAST_FM_API_KEY", "")
DISCORD_ENABLED = os.environ.get("DISCORD_ENABLED", "true").lower() == "true"
PREFERENCE_CONFIDENCE_THRESHOLD = float(os.environ.get("PREFERENCE_CONFIDENCE_THRESHOLD", "0.95"))  # auto-download once taste confidence reaches this
MIN_TRACKS_FOR_CONFIDENCE = int(os.environ.get("MIN_TRACKS_FOR_CONFIDENCE", "50"))  # Minimum tracks analyzed to enable auto-download
ENABLE_TIKTOK_TRENDING = os.environ.get("ENABLE_TIKTOK_TRENDING", "false").lower() == "true"
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "604800"))  # one week
ENABLE_AUTO_DELETE = os.environ.get("ENABLE_AUTO_DELETE", "true").lower() == "true"
USER_ACTIVITY_WINDOW_DAYS = max(1, int(os.environ.get("USER_ACTIVITY_WINDOW_DAYS", "30")))

def _ensure_shared_data_dir() -> str:
    data_dir = os.environ.get("DATA_DIR", "/app/data")
    try:
        os.makedirs(data_dir, exist_ok=True)
    except Exception:
        pass
    return data_dir


def _write_cycle_skip_file(reason: str, next_cycle_hours: float) -> None:
    try:
        data_dir = _ensure_shared_data_dir()
        canonical_user = (PLEX_USERNAME.strip() if PLEX_USERNAME and PLEX_USERNAME.strip() else APP_USER_NAME)
        skip_file = os.path.join(data_dir, f'cycle_skipped_{canonical_user}.json')
        with open(skip_file, 'w') as f:
            json.dump({
                'user': canonical_user,
                'reason': reason,
                'next_cycle_hours': next_cycle_hours,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            }, f)
        logger.debug(f"Wrote cycle skip file: {skip_file}")
    except Exception as e:
        logger.warning(f"Could not write cycle skip file: {e}")


def _remove_cycle_skip_file() -> None:
    try:
        canonical_user = (PLEX_USERNAME.strip() if PLEX_USERNAME and PLEX_USERNAME.strip() else APP_USER_NAME)
        skip_file = os.path.join(_ensure_shared_data_dir(), f'cycle_skipped_{canonical_user}.json')
        if os.path.exists(skip_file):
            os.remove(skip_file)
            logger.debug(f"Removed stale cycle skip file: {skip_file}")
    except Exception as e:
        logger.debug(f"Could not remove cycle skip file: {e}")


# Lidarr instances (lidarr_catalog.lidarr_instances): a user's is chosen by their index.
from lidarr_catalog import lidarr_instances as _lidarr_instances
LIDARR_URLS = [url for url, _key in _lidarr_instances()]
LIDARR_API_KEYS = [key for _url, key in _lidarr_instances()]
# Optional: album requests are handed to Lidarr (it searches and downloads the
# whole album) instead of going song by song; and Lidarr's album names count
# as one more metadata source for a download.
LIDARR_USE_AS_DOWNLOADER = os.environ.get("LIDARR_USE_AS_DOWNLOADER", "false").strip().lower() == "true"


def _lidarr_for_user(user_index):
    from lidarr_catalog import lidarr_for_user
    instances = [(u.strip().rstrip('/'), k.strip()) for u, k in zip(LIDARR_URLS, LIDARR_API_KEYS)]
    return lidarr_for_user(int(user_index) if user_index is not None else 0, instances)


def _followed_on_instance(instance, exclude_user=None):
    """{plex user: [their followed-artist rows]} for every user whose top
    artists live in this Lidarr instance (several users can share one).
    None when a user's list can't be read: an unreadable list must not be
    mistaken for an empty one."""
    followed = {}
    data_dir = os.environ.get('DATA_DIR', '/app/data')
    for index, plex_user in enumerate(users.plex_usernames(users.load_wizarr_mapping() or {})):
        if plex_user == exclude_user or _lidarr_for_user(index) != instance:
            continue
        db_path = os.path.join(data_dir, plex_user, 'music_ai.db')
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(db_path)
            try:
                followed[plex_user] = [
                    {'artist_name': name or '', 'foreign_artist_id': foreign, 'lidarr_artist_id': lidarr_id}
                    for name, foreign, lidarr_id in conn.execute(
                        'SELECT artist_name, foreign_artist_id, lidarr_artist_id FROM lidarr_followed_artists')]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            if 'no such table' not in str(exc):
                logger.warning(f"[LIDARR] Could not read the top artists of {plex_user}: {exc}")
                return None
    return followed
LIDARR_USE_AS_METADATA = os.environ.get("LIDARR_USE_AS_METADATA", "false").strip().lower() == "true"
TOP_ARTIST_LIMIT = max(1, int(os.environ.get("TOP_ARTIST_LIMIT", "5")))

# Admin account token; per-user Home tokens are cached in the database.
# No validation needed here as connect_plex() will handle token retrieval

logger.info(f"Plex URL: {PLEX_URL}")
logger.info(f"Music Directory: {MUSIC_DIR}")
logger.info(f"Plex Music Section: {PLEX_MUSIC_SECTION}")
logger.info(f"Database Path: {DB_PATH}")
logger.info(f"Disk space auto-cleanup: enabled (triggers at 10GB threshold)")
logger.info(f"Auto-download enablement: Intelligent (activates at {PREFERENCE_CONFIDENCE_THRESHOLD*100}% confidence)")
logger.info(f"Minimum tracks for confidence calculation: {MIN_TRACKS_FOR_CONFIDENCE}")
logger.info(f"Temp download directory: {TEMP_DOWNLOAD_DIR}")
logger.info(f"Album completion: Always enabled")

plex = None

# Path conversion utility for Plex playlists


# Lock file functions for trending downloads
def can_download_trending():
    """
    Check if enough time has passed since the last trending download.
    Returns True if it's been at least 2 weeks (14 days) since the last download,
    or if no lock file exists yet.
    """
    logger.info(f"🔍 Checking trending lock file: {LOCK_FILE}")
    
    if not os.path.exists(LOCK_FILE):
        logger.info(f"✓ No trending lock file found at {LOCK_FILE}, allowing first download")
        return True
    
    logger.info(f"📂 Lock file exists, checking timestamps...")
    
    try:
        with open(LOCK_FILE, 'r') as f:
            lock_data = json.load(f)
        
        logger.debug(f"Lock file contents: {lock_data}")
        
        # Lock files on disk use either key name for the timestamp.
        last_run = lock_data.get('last_run') or lock_data.get('last_download')
        if not last_run:
            logger.info(f" ✓ No last_run/last_download in lock file, allowing download")
            return True
        
        logger.info(f"Last trending download: {last_run}")
        
        # Parse the datetime string - handle ISO format with timezone
        try:
            if '+' in last_run or 'Z' in last_run:
                last_time = datetime.fromisoformat(last_run)
            else:
                last_time = datetime.fromisoformat(last_run)
        except:
            last_time = datetime.fromisoformat(last_run.replace('Z', '+00:00'))
        
        # Make comparison timezone-aware if needed
        if last_time.tzinfo:
            time_since_last = datetime.now(timezone.utc) - last_time
        else:
            time_since_last = datetime.now() - last_time
        
        logger.info(f"Time since last trending download: {time_since_last.days} days, {time_since_last.seconds // 3600} hours")
        
        # Check if 2 weeks (14 days) have passed
        if time_since_last >= timedelta(days=14):
            days_since = time_since_last.days
            logger.info(f"✓ 2 weeks have passed since last trending download ({days_since} days ago), allowing new download")
            return True
        else:
            days_remaining = 14 - time_since_last.days
            hours_remaining = (timedelta(days=14) - time_since_last).seconds // 3600
            logger.info(f"⏳ Trending download was {time_since_last.days} days ago. Next eligible in ~{days_remaining}d {hours_remaining}h")
            return False
    
    except Exception as e:
        logger.warning(f"Error reading lock file: {e}. Allowing trending download.")
        return True

def update_trending_lock():
    """
    Update the lock file with the current timestamp.
    """
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        lock_data = {
            'last_run': datetime.now(timezone.utc).isoformat(),
            'last_download': datetime.now(timezone.utc).isoformat(),
            'version': 1
        }
        with open(LOCK_FILE, 'w') as f:
            json.dump(lock_data, f, indent=2)
        logger.info(f"✓ Updated trending lock file: {LOCK_FILE}")
    except Exception as e:
        logger.error(f"Error updating lock file: {e}")


# Helper: parse artist field to primary artist + featured artists

def prefer_explicit_version(search_results):
    """
    Given search results, prefer explicit versions if available.
    If explicit and clean versions exist, choose explicit.
    If only one type exists, use that.
    
    Returns the most preferred result from the list.
    """
    if not search_results:
        return None
    
    if len(search_results) == 1:
        return search_results[0]
    
    # Categorize results
    explicit_tracks = []
    clean_tracks = []
    
    for track in search_results:
        try:
            # Check if track is explicit (different APIs have different fields)
            title = track.get('SNG_TITLE', '')
            is_explicit = track.get('EXPLICIT_LYRICS', False)
            
            # Also check if title contains explicit marker
            if '(Explicit)' in title or 'Explicit' in title:
                is_explicit = True
            
            if is_explicit:
                explicit_tracks.append(track)
            else:
                clean_tracks.append(track)
        except Exception:
            clean_tracks.append(track)
    
    # Prefer explicit if available, otherwise use clean
    if explicit_tracks:
        logger.debug(f"Found {len(explicit_tracks)} explicit version(s), preferring explicit")
        return explicit_tracks[0]
    else:
        return search_results[0]


def merge_similar_albums(album_dir):
    """Merge folders of one album under each artist ("21" and "21 (Deluxe
    Edition)"), with album_artist_merger's album pass: only edition noise is
    ignored, Lidarr's albums are left to Lidarr, and a song of the same name
    already there is compared by sound before anything is quarantined.

    Albums of different artists that share a name (Taylor Swift's "Red" and
    Nelson Rangell's "Red") are different albums and are never merged.
    """
    try:
        from album_artist_merger import build_merger_from_env
        merger = build_merger_from_env()
        merger.album_dir = album_dir
        report = merger.merge_duplicate_album_dirs()
        logger.info(f"📊 Album folders merged: {report.album_dir_merges}")
    except Exception as e:
        logger.error(f"Error merging album folders: {e}")


def find_and_replace_explicit(album_dir):
    """
    Scan album directory for non-explicit versions and try to replace them with explicit versions.
    Uses deemix to search for explicit versions and replace if found.
    """
    if not os.path.isdir(album_dir):
        return
    
    try:
        
        replaced_count = 0
        arl = os.environ.get('DEEZER_ARL', '')
        
        if not arl:
            logger.warning("DEEZER_ARL not set, cannot search for explicit versions")
            return
        
        # Get all audio files in the album directory
        for root, dirs, files in os.walk(album_dir):
            for filename in files:
                if not filename.lower().endswith(('.mp3', '.m4a', '.flac', '.ogg')):
                    continue
                
                file_path = os.path.join(root, filename)
                
                try:
                    # Get track metadata
                    audio = MutagenFile(file_path)
                    if not audio or not hasattr(audio, 'tags'):
                        continue
                    
                    # Extract artist and title
                    artist = None
                    title = None
                    
                    try:
                        artist_tag = audio.tags.get('TPE1', '')
                        if isinstance(artist_tag, list):
                            artist = artist_tag[0] if artist_tag else None
                        else:
                            artist = artist_tag
                    except Exception:
                        pass
                    
                    try:
                        title_tag = audio.tags.get('TIT2', '')
                        if isinstance(title_tag, list):
                            title = title_tag[0] if title_tag else None
                        else:
                            title = title_tag
                    except Exception:
                        pass
                    
                    if not artist or not title:
                        continue
                    
                    # Check if file is already explicit
                    is_explicit = False
                    try:
                        title_str = str(title)
                        if '(Explicit)' in title_str or '[Explicit]' in title_str:
                            is_explicit = True
                    except Exception:
                        pass
                    
                    # Also check if explicit tag exists
                    try:
                        explicit_tag = audio.tags.get('TPE2', '')  # Or TXXX:Explicit
                        if explicit_tag:
                            is_explicit = True
                    except Exception:
                        pass
                    
                    if is_explicit:
                        logger.debug(f"Track already explicit: {artist} - {title}")
                        continue
                    
                    # Search for explicit version on Deezer
                    try:
                        from deezer import Deezer
                        dz = Deezer()
                        if not dz.login_via_arl(arl):
                            logger.debug("Failed to login to Deezer for explicit search")
                            continue
                        
                        search_query = f"{artist} {title} explicit"
                        logger.info(f"🔍 Searching for explicit version: {search_query}")
                        
                        search_results = dz.gw.search_music(search_query, type='TRACK', limit=5)
                        tracks = search_results.get('data', [])
                        
                        if not tracks:
                            # Try without explicit keyword
                            search_query = f"{artist} {title}"
                            search_results = dz.gw.search_music(search_query, type='TRACK', limit=5)
                            tracks = search_results.get('data', [])
                        
                        if not tracks:
                            logger.debug(f"No results found for: {artist} - {title}")
                            continue
                        
                        # Prefer explicit version
                        best_track = prefer_explicit_version(tracks)
                        if not best_track:
                            continue
                        
                        is_explicit_result = best_track.get('EXPLICIT_LYRICS', False)
                        if not is_explicit_result:
                            logger.debug(f"No explicit version found for: {artist} - {title}")
                            continue
                        
                        # Found explicit version! Now download it
                        track_id = best_track.get('SNG_ID')
                        if not track_id:
                            continue
                        
                        track_title = best_track.get('SNG_TITLE', title)
                        track_artist = best_track.get('ART_NAME', artist)
                        
                        logger.info(f"  ✓ Found explicit version: {track_artist} - {track_title}")
                        
                        # Download explicit version using deemix library
                        from deemix.downloader import Downloader
                        from deemix.settings import load as loadSettings
                        from deemix import generateDownloadObject
                        from pathlib import Path as PathlibPath
                        
                        # Create temp directory for download
                        temp_dir = os.path.join(STAGING_DIR, 'explicit_replace')
                        os.makedirs(temp_dir, exist_ok=True)
                        
                        config_folder = PathlibPath.home() / '.config' / 'deemix'
                        settings = loadSettings(config_folder)
                        settings['downloadLocation'] = temp_dir
                        
                        class SimpleListener:
                            def send(self, key, value=None):
                                pass
                        
                        listener = SimpleListener()
                        
                        # Generate and download
                        track_url = f"https://www.deezer.com/track/{track_id}"
                        download_obj = generateDownloadObject(dz, track_url, str(settings.get('maxBitrate', 0)), {}, listener)
                        
                        if download_obj:
                            downloader = Downloader(dz, download_obj, settings, listener)
                            downloader.start()
                            time.sleep(10)  # Wait for download
                            
                            # Find downloaded file and replace
                            for downloaded_file in os.listdir(temp_dir):
                                if downloaded_file.endswith(('.mp3', '.m4a', '.flac', '.ogg')):
                                    downloaded_path = os.path.join(temp_dir, downloaded_file)
                                    
                                    # Backup original
                                    backup_path = file_path + '.clean_backup'
                                    shutil.copy2(file_path, backup_path)
                                    
                                    # Replace with explicit
                                    shutil.move(downloaded_path, file_path)
                                    logger.info(f"  ✓ Replaced with explicit version: {filename}")
                                    replaced_count += 1
                                    break
                            
                            # Cleanup temp directory
                            try:
                                shutil.rmtree(temp_dir)
                            except:
                                pass
                    
                    except Exception as e:
                        logger.debug(f"Error searching for explicit version: {e}")
                        continue
                
                except Exception as e:
                    logger.debug(f"Error processing {filename}: {e}")
                    continue
        
        if replaced_count > 0:
            logger.info(f"✓ Replaced {replaced_count} track(s) with explicit version(s)")
    
    except Exception as e:
        logger.error(f"Error finding explicit versions: {e}")


def sanitize_filename(name):
    """Sanitize a string for use as a directory or filename component."""
    # Replace problematic characters with underscore and strip
    name = name.strip()
    # Remove control chars
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name)
    # Replace path separators and other illegal chars
    name = re.sub(r"[\\/:*?\"<>|]", "-", name)
    # Collapse repeated whitespace
    name = re.sub(r"\s+", " ", name)
    return name


def run_discord_request_processor_loop(analyzer_ref, check_interval_seconds=30):
    """Continuously check for and process pending Discord requests every check_interval_seconds.
    
    This runs in a background thread so Discord requests arriving while a cycle is
    running are handled within seconds instead of waiting for that cycle to finish.
    """
    while True:
        try:
            analyzer = analyzer_ref() if callable(analyzer_ref) else analyzer_ref
            if analyzer is not None:
                try:
                    data_dir = os.environ.get('DATA_DIR', '/app/data')
                    requests_db = os.path.join(data_dir, 'discord_requests.json')
                    if os.path.exists(requests_db):
                        with open(requests_db, 'r') as f:
                            all_requests = json.load(f)
                        pending_count = 0
                        for user_requests in all_requests.values():
                            for req in user_requests:
                                if req.get('status') == 'pending':
                                    pending_count += 1
                        
                        if pending_count > 0:
                            logger.info(f"🎵 Background Discord processor: found {pending_count} pending request(s), processing...")
                            processed = analyzer.process_discord_requests()
                            logger.debug(f"   Processed {processed} Discord request(s)")
                except Exception as check_e:
                    logger.debug(f"Discord request background processor check error: {check_e}")
            time.sleep(check_interval_seconds)
        except Exception as exc:
            logger.warning(f"Discord request background processor error: {exc}")
            time.sleep(check_interval_seconds)


class MusicAIAnalyzer:
    def __init__(self):
        self.scaler = StandardScaler()
        self.plex = None
        # Every song this cycle downloaded, tagged with the cycle playlist it goes in.
        self.cycle_downloads = []
        self.cycle_started_at = None
        self.init_database()
        self.connect_plex()
    
    def connect_plex(self):
        """Connect to Plex server using the admin token. A cached token Plex
        rejects (revoked or expired) is dropped from the cache and the next
        source is tried, instead of retrying the dead token forever."""
        max_retries = 3
        
        # Respect runtime override for APP_USER_NAME for logging only.
        # Only use PLEX_USERNAME for Plex home user switching.
        current_user = PLEX_USERNAME.strip() if PLEX_USERNAME and PLEX_USERNAME.strip() else None
        logger.info(f"🔐 Initializing Plex connection for user '{APP_USER_NAME}'")
        logger.info(f"   └─ Target Plex username: {PLEX_USERNAME if PLEX_USERNAME else '(using admin account)'}")
        logger.info(f"   └─ Server URL: {PLEX_URL}")
        
        # Token lookup order: the user's cached Home-user token, then (admin
        # only) PLEX_TOKEN or the cached admin token, then mint a fresh one.
        token_to_use = None
        token_source = None
        admin_token = None
        explicit_token = bool(PLEX_TOKEN)

        if current_user and current_user.lower() != 'admin':
            logger.info(f"🔍 Looking for cached Plex token for home user '{current_user}'...")
            try:
                conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
                cursor = conn.cursor()
                cursor.execute('SELECT token FROM plex_tokens WHERE username = ? '
                               'ORDER BY obtained_at DESC, rowid DESC LIMIT 1', (current_user,))
                row = cursor.fetchone()
                if row and row[0]:
                    token_to_use = row[0]
                    token_source = "database cache (user)"
                    logger.info(f"   ✅ Found cached Plex token for user '{current_user}'")
                conn.close()
            except Exception as e:
                logger.debug(f"Could not retrieve cached user token from database: {e}")

        is_admin_user = bool(
            current_user
            and (current_user.lower() == 'admin' or os.environ.get("APP_IS_ADMIN_USER", "").lower() == "true")
        )

        if not token_to_use:
            if explicit_token and is_admin_user:
                token_to_use = PLEX_TOKEN
                token_source = "environment variable"
                logger.info("   ✅ Using explicit PLEX_TOKEN from environment")
            elif explicit_token and not is_admin_user:
                logger.warning(
                    "   ⚠️ Ignoring explicit PLEX_TOKEN for non-admin Plex user "
                    f"'{current_user}' to avoid false-positive admin activity"
                )
            elif current_user and not is_admin_user:
                logger.info(
                    "   ⚠️ Skipping admin token fallback for non-admin Plex user "
                    f"'{current_user}' to avoid false-positive admin activity"
                )
            else:
                logger.info("📋 Checking cached admin token in database...")
                try:
                    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
                    cursor = conn.cursor()
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS plex_tokens (
                            token TEXT PRIMARY KEY,
                            username TEXT,
                            obtained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    cursor.execute('SELECT token FROM plex_tokens WHERE username = ? '
                                   'ORDER BY obtained_at DESC, rowid DESC LIMIT 1', ('_admin',))
                    row = cursor.fetchone()

                    if row and row[0]:
                        admin_token = row[0]
                        token_to_use = admin_token
                        token_source = "database cache (admin)"
                        logger.info("   ✅ Found cached admin token in database")
                    else:
                        logger.info("   ⚠️ No cached admin token found in database")

                    conn.close()
                except Exception as e:
                    logger.debug(f"Could not retrieve admin token from database: {e}")

        # If still no token, mint one from the admin token
        if not token_to_use:
            logger.info("🔑 Minting a fresh token from the admin account...")
            try:
                import plex_auth
                if current_user and not is_admin_user:
                    logger.info(f"   🔄 Creating per-user token for Plex home user '{current_user}'...")
                    user_token = plex_auth.home_user_token(PLEX_URL, current_user)
                    if user_token:
                        token_to_use = user_token
                        token_source = f"fresh Plex.tv home user token ({current_user})"
                    else:
                        logger.error(f"   ❌ Unable to obtain home user token for '{current_user}'")
                else:
                    admin_token = plex_auth.admin_token()
                    token_to_use = admin_token
                    token_source = "PLEX_TOKEN"
            except Exception as e:
                logger.debug(f"Could not mint a token from the admin account: {e}")
        if not token_to_use:
            logger.warning("⚠️  No PLEX_TOKEN available - Plex features will be DISABLED")
            logger.warning("   └─ Library analysis, playlists, and metadata features will be unavailable")
            logger.warning("   └─ Set PLEX_TOKEN (admin account token) or check the database token cache")
            self.plex = None
            return
        
        logger.info(f"🎬 Connecting to Plex server using token from {token_source}...")
        
        for attempt in range(max_retries):
            try:
                logger.info(f"🔗 Attempt {attempt + 1}/{max_retries}: Connecting to {PLEX_URL}...")
                try:
                    import plexapi.server as plex_srv_mod
                    self.plex = plex_srv_mod.PlexServer(PLEX_URL, token_to_use)
                except Exception as e_conn:
                    # Let outer except handle logging and fallback
                    raise
                
                server_name = self.plex.friendlyName
                logger.info(f"✅ Successfully connected to Plex server!")
                logger.info(f"   ├─ Server name: {server_name}")
                logger.info(f"   ├─ Product: {getattr(self.plex, 'product', 'Unknown')}")
                logger.info(f"   ├─ Server version: {getattr(self.plex, 'version', 'Unknown')}")
                logger.info(f"   └─ Connected Plex username: {getattr(self.plex, 'myPlexUsername', '(unknown)')}")
                
                # Store the token that was actually used for connection
                self.plex_token = token_to_use

                # Attempt to enter the requested Plex home-user context for non-admin users.
                # Only do this when the connection was established with an admin token or admin cache.
                if PLEX_USERNAME and PLEX_USERNAME.lower() != 'admin' and not (
                    token_source == "database cache (user)" or
                    (token_source and token_source.startswith("fresh Plex.tv home user token"))
                ):
                    current_username = getattr(self.plex, 'myPlexUsername', None)
                    if current_username and current_username.lower() == PLEX_USERNAME.lower():
                        logger.info(f"   ✅ Already connected as Plex user '{PLEX_USERNAME}'")
                    else:
                        logger.info(f"🔄 Switching to home user: {PLEX_USERNAME}")
                        try:
                            self.plex = self.plex.switchUser(PLEX_USERNAME)
                            logger.info(f"   ✅ Successfully switched to user '{PLEX_USERNAME}'")
                        except Exception as switch_err:
                            logger.error(f"   ❌ Could not switch to Plex home user '{PLEX_USERNAME}': {switch_err}")
                            logger.error("   └─ This user process will not analyze Plex if it cannot enter the requested home context")
                            self.plex = None
                            return

                return
            except Exception as e:
                msg = str(e)
                logger.warning(f"⚠️  Connection attempt {attempt + 1}/{max_retries} failed")
                logger.warning(f"   └─ Error: {msg}")

                if ('401' in msg or 'unauthorized' in msg.lower()) and (token_source or '').startswith("database cache"):
                    logger.warning("   ⚠️ Plex rejects the cached token: dropping it from the cache and trying the next one")
                    try:
                        conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
                        conn.execute('DELETE FROM plex_tokens WHERE token = ?', (token_to_use,))
                        conn.commit()
                        conn.close()
                    except sqlite3.Error as db_err:
                        raise ValueError(f"Plex rejects a cached token that can't be removed: {db_err}")
                    return self.connect_plex()
                if '401' in msg or 'Unauthorized' in msg or 'unauthorized' in msg.lower():
                    logger.error("❌ Server returned 401 Unauthorized")
                    logger.error("   Possible causes:")
                    logger.error("   ├─ Token is invalid or expired")
                    logger.error("   ├─ Server URL is incorrect")
                    logger.error("   └─ Network connectivity issue")
                    raise ValueError(f"Plex unauthorized: verify token and server. Error: {msg}")

                if attempt < max_retries - 1:
                    wait_time = 5 * (attempt + 1)
                    logger.info(f"⏳ Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"❌ Failed to connect to Plex after {max_retries} attempts")
                    logger.error(f"   └─ Server may be offline or unreachable at {PLEX_URL}")
                    raise
    
    
    def init_database(self):
        """Initialize SQLite database for tracking music engagement."""
        conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS track_scores (
                track_id TEXT PRIMARY KEY,
                title TEXT,
                artist TEXT,
                album TEXT,
                album_key TEXT,
                album_rating REAL,
                score REAL,
                view_count INTEGER,
                skip_count INTEGER,
                last_played INTEGER,
                date_added INTEGER,
                rating INTEGER,
                genre TEXT DEFAULT 'Unknown',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT,
                track TEXT,
                reason TEXT,
                confidence REAL,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS deletions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id TEXT,
                title TEXT,
                artist TEXT,
                score REAL,
                reason TEXT,
                archived BOOLEAN,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                reason TEXT,
                rating REAL,
                blacklisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, title, artist)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS temp_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT,
                track_title TEXT,
                file_path TEXT,
                reason TEXT,
                confidence REAL,
                status TEXT,
                is_trending BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS lidarr_followed_artists (
                artist_name TEXT PRIMARY KEY,
                foreign_artist_id TEXT,
                lidarr_artist_id INTEGER,
                artist_score REAL,
                recent_listen_count INTEGER,
                followed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Migrate existing tables if columns don't exist
        try:
            cursor.execute("PRAGMA table_info(temp_downloads)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'is_trending' not in columns:
                logger.info("Adding is_trending column to temp_downloads table")
                cursor.execute("ALTER TABLE temp_downloads ADD COLUMN is_trending BOOLEAN DEFAULT 0")
        except Exception as e:
            logger.debug(f"Could not check/migrate temp_downloads table: {e}")
        
        # Add album and genre columns to track_scores if they don't exist
        try:
            cursor.execute("PRAGMA table_info(track_scores)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'album' not in columns:
                logger.info("Adding album column to track_scores table")
                cursor.execute("ALTER TABLE track_scores ADD COLUMN album TEXT")
            if 'genre' not in columns:
                logger.info("Adding genre column to track_scores table")
                cursor.execute("ALTER TABLE track_scores ADD COLUMN genre TEXT DEFAULT 'Unknown'")
            
            # Add album_key column (Plex parentKey for album)
            if 'album_key' not in columns:
                logger.info("Adding album_key column to track_scores table")
                cursor.execute("ALTER TABLE track_scores ADD COLUMN album_key TEXT")
            
            # Add album_rating column (Plex rating for the album)
            if 'album_rating' not in columns:
                logger.info("Adding album_rating column to track_scores table")
                cursor.execute("ALTER TABLE track_scores ADD COLUMN album_rating REAL")
        except Exception as e:
            logger.debug(f"Could not check/migrate track_scores table: {e}")
        
        # Migrate blacklist table to add user_id column for per-user blacklisting
        try:
            cursor.execute("PRAGMA table_info(blacklist)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'user_id' not in columns:
                logger.info("Adding user_id column to blacklist table for per-user deletion tracking")
                cursor.execute("ALTER TABLE blacklist ADD COLUMN user_id TEXT")
                # Recreate unique constraint with user_id
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS blacklist_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT,
                        title TEXT NOT NULL,
                        artist TEXT NOT NULL,
                        reason TEXT,
                        rating REAL,
                        blacklisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, title, artist)
                    )
                ''')
                cursor.execute('''
                    INSERT INTO blacklist_new (id, user_id, title, artist, reason, rating, blacklisted_at)
                    SELECT id, NULL, title, artist, reason, rating, blacklisted_at FROM blacklist
                ''')
                cursor.execute('DROP TABLE blacklist')
                cursor.execute('ALTER TABLE blacklist_new RENAME TO blacklist')
        except Exception as e:
            logger.debug(f"Could not check/migrate blacklist table: {e}")
        
        conn.commit()
        conn.close()

        # Ensure plex_tokens table exists for caching X-Plex-Token to username mappings
        conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS plex_tokens (
                token TEXT PRIMARY KEY,
                username TEXT,
                obtained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
    
    def refresh_lidarr_library(self, user_index=0):
        """Ask Lidarr to rescan its library folders after a download cycle."""
        instance = _lidarr_for_user(user_index)
        if not instance:
            logger.info("[LIDARR] Not configured; skipping final refresh")
            return False
        lidarr_url, lidarr_api = instance
        try:
            from downloader import LidarrClient
            if LidarrClient(lidarr_url, lidarr_api).refresh_library():
                logger.info(f"[LIDARR] ✓ Library rescan queued for user_index {user_index}")
                return True
        except Exception as e:
            logger.warning(f"[LIDARR] Library rescan error: {e}")
        return False

    
    def check_download_tools(self):
        """Verify that download tools (deemix, spotdl) are available."""
        tools_status = {}
        version_cmds = {
            'deemix': ['deemix', '--help'],  # deemix doesn't support --version
            'spotdl': ['spotdl', '--version'],
            'yt-dlp': ['yt-dlp', '--version']
        }
        
        for tool, cmd in version_cmds.items():
            try:
                result = run_subprocess_with_limit(cmd, service_name=tool, timeout=5)
                if not result:
                    tools_status[tool] = False
                    continue
                
                # Special handling for deemix: accept non-zero exit if help text is present
                if tool == 'deemix':
                    # deemix --help may return non-zero but still output help text
                    # Check if help output is present instead of just exit code
                    has_help_output = 'usage' in result.stdout.lower() or 'usage' in result.stderr.lower()
                    # Also check if it's just a missing ARL issue (still installed but not configured)
                    has_installation = has_help_output or result.returncode == 0 or 'deemix' in result.stderr.lower()
                    tools_status[tool] = has_installation
                    
                    if not tools_status[tool]:
                        logger.debug(f"deemix check - stdout: {result.stdout[:200]}, stderr: {result.stderr[:200]}, code: {result.returncode}")
                    elif result.returncode != 0 and has_installation:
                        logger.debug(f"⚠ deemix installed but returned non-zero (may be missing ARL token or dependencies)")
                else:
                    tools_status[tool] = result.returncode == 0
                
                if tools_status[tool]:
                    logger.info(f"✓ {tool} is available")
                else:
                    logger.warning(f"✗ {tool} returned non-zero exit code")
            except FileNotFoundError:
                tools_status[tool] = False
                logger.warning(f"✗ {tool} not found in PATH")
            except subprocess.TimeoutExpired:
                tools_status[tool] = False
                logger.warning(f"✗ {tool} check timed out")
            except Exception as e:
                tools_status[tool] = False
                logger.warning(f"✗ {tool} check failed: {e}")
        
        return tools_status
    
    def check_api_availability(self):
        """Check availability of all metadata and external APIs."""
        api_status = {}
        
        # Check MusicBrainz API
        try:
            import musicbrainzngs
            # MusicBrainz doesn't require authentication, just test connectivity
            api_status['musicbrainz'] = True
            logger.info("✓ MusicBrainz API is available")
        except Exception as e:
            api_status['musicbrainz'] = False
            logger.warning(f"✗ MusicBrainz API check failed: {e}")
        
        # Check Discogs API
        try:
            if DISCOGS_TOKEN:
                api_status['discogs'] = True
                logger.info("✓ Discogs API is available and authenticated")
            else:
                api_status['discogs'] = False
                logger.info("ℹ Discogs API not configured (optional)")
        except Exception as e:
            api_status['discogs'] = False
            logger.warning(f"✗ Discogs API check failed: {e}")
        
        # Check Plex API
        try:
            if self.plex:
                # Test connection
                _ = self.plex.library.section('Music') if 'Music' in [s.title for s in self.plex.library.sections()] else None
                api_status['plex'] = True
                logger.info("✓ Plex API is available and connected")
            else:
                api_status['plex'] = False
                logger.warning("✗ Plex API not connected")
        except Exception as e:
            api_status['plex'] = False
            logger.warning(f"✗ Plex API check failed: {e}")
        
        # Check Mutagen (file metadata reading)
        try:
            import mutagen
            api_status['mutagen'] = True
            logger.info("✓ Mutagen audio metadata library is available")
        except Exception as e:
            api_status['mutagen'] = False
            logger.warning(f"✗ Mutagen library check failed: {e}")
        
        # Check Billboard API
        try:
            import billboard
            # Test by fetching a small chart to verify connectivity
            chart = billboard.ChartData('hot-100', fetch=False)  # Don't fetch immediately
            api_status['billboard'] = True
            logger.info("✓ Billboard API is available")
        except ImportError:
            api_status['billboard'] = False
            logger.warning("✗ Billboard library not installed (optional)")
        except Exception as e:
            api_status['billboard'] = False
            logger.warning(f"✗ Billboard API check failed: {e}")
        
        # Check TikTok API (TikTok API availability check)
        try:
            # Test TikTok API by doing a simple request
            # We'll use a generic TikTok API endpoint to check connectivity
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.head('https://api.tiktok.com/', headers=headers, timeout=5)
            api_status['tiktok'] = True
            logger.info("✓ TikTok API connectivity verified")
        except ImportError:
            api_status['tiktok'] = False
            logger.warning("✗ Requests library not available for TikTok API")
        except Exception as e:
            api_status['tiktok'] = False
            logger.debug(f"ℹ TikTok API check failed (may not be available in your region): {e}")
        
        # Summary
        available = sum(1 for v in api_status.values() if v)
        logger.info(f"🔍 API Health Check: {available}/{len(api_status)} APIs available")
        return api_status
    
    def get_all_tracks(self):
        """Retrieve all tracks from Plex music library."""
        try:
            if not self.plex:
                logger.info("No Plex connection available for this user process; skipping track retrieval until a per-user Plex context is reachable.")
                return []
            # Use configured library section name (env: PLEX_MUSIC_SECTION)
            section_name = PLEX_MUSIC_SECTION
            music_library = None
            try:
                music_library = self.plex.library.section(section_name)
            except Exception:
                # Try to match by title case-insensitively among available sections
                try:
                    sections = self.plex.library.sections()
                    for s in sections:
                        if s.title.lower() == section_name.lower():
                            music_library = s
                            break
                except Exception:
                    logger.exception("Unable to list Plex library sections")

            if not music_library:
                try:
                    available = [s.title for s in self.plex.library.sections()]
                except Exception:
                    available = []
                logger.error(f"Plex library section '{section_name}' not found. Available sections: {available}")
                return []

            all_items = music_library.all()

            # Build a list of track objects. Plex may return albums/artists; try to expand them.
            all_tracks = []
            for item in all_items:
                try:
                    # If item already looks like a track (has media/parts or explicit type), keep it
                    item_type = getattr(item, 'type', None)
                    if item_type == 'track' or (hasattr(item, 'media') and getattr(item, 'media')):
                        all_tracks.append(item)
                        continue

                    # Try to expand albums/artists into their tracks
                    if hasattr(item, 'tracks') and callable(getattr(item, 'tracks')):
                        try:
                            sub = item.tracks()
                            if sub:
                                all_tracks.extend(sub)
                                continue
                        except Exception:
                            pass

                    # Fallback to .items() or .children() if available
                    if hasattr(item, 'items') and callable(getattr(item, 'items')):
                        try:
                            sub = item.items()
                            if sub:
                                all_tracks.extend(sub)
                                continue
                        except Exception:
                            pass

                    if hasattr(item, 'children') and callable(getattr(item, 'children')):
                        try:
                            sub = item.children()
                            if sub:
                                all_tracks.extend(sub)
                                continue
                        except Exception:
                            pass

                    # If nothing worked, skip this item
                except Exception as e:
                    logger.debug(f"Error while expanding Plex item to tracks: {e}")

            # Deduplicate by key and ensure items have media
            dedup = {}
            for t in all_tracks:
                try:
                    key = getattr(t, 'key', None) or getattr(t, 'ratingKey', None) or str(t)
                    if key and key not in dedup and hasattr(t, 'media') and getattr(t, 'media'):
                        dedup[key] = t
                except Exception:
                    continue

            filtered_tracks = list(dedup.values())
            logger.info(f"Retrieved {len(filtered_tracks)} tracks from Plex (expanded from {len(all_items)} items)")

            if len(filtered_tracks) == 0 and len(all_items) > 0:
                # Log debug info about the first item to help diagnose library structure
                first = all_items[0]
                logger.debug(f"First Plex item class: {first.__class__.__name__}, type: {getattr(first, 'type', None)}, attrs: {[a for a in dir(first) if not a.startswith('_')][:10]}")

            return filtered_tracks
        except Exception as e:
            logger.error(f"Error retrieving tracks from Plex: {e}")
            return []
    
    def _fetch_plex_item_cached(self, key):
        """fetchItem() with a per-analysis cache keyed by Plex metadata key.

        A library has far fewer albums/artists than tracks, so caching cuts the
        per-track round-trips in analyze_all_tracks() from thousands to a few
        hundred. Failed fetches are cached as None so they aren't retried.
        """
        if not key or not self.plex:
            return None
        cache = getattr(self, '_plex_item_cache', None)
        if cache is None:
            cache = self._plex_item_cache = {}
        if key in cache:
            return cache[key]
        try:
            item = self.plex.fetchItem(key)
        except Exception as fetch_e:
            logger.debug(f"Could not fetch Plex item {key}: {fetch_e}")
            item = None
        cache[key] = item
        return item

    def extract_track_features(self, track):
        """Extract engagement metrics and metadata from track."""
        try:
            view_count = getattr(track, 'viewCount', None)
            skip_count = getattr(track, 'skipCount', None)
            last_played = getattr(track, 'lastViewedAt', None)

            def _normalize_plex_datetime(value):
                if isinstance(value, datetime):
                    try:
                        return int(value.timestamp())
                    except Exception:
                        return int(time.time())
                if isinstance(value, (int, float)):
                    return int(value)
                return 0

            view_count = int(view_count or 0)
            skip_count = int(skip_count or 0)
            last_played = _normalize_plex_datetime(last_played)

            # Some Plex track objects store per-user engagement on nested media/parts.
            if view_count == 0 and skip_count == 0 and last_played == 0 and hasattr(track, 'media'):
                try:
                    media_items = track.media if isinstance(track.media, (list, tuple)) else [track.media]
                    for media in media_items:
                        if not media:
                            continue
                        view_count = view_count or int(getattr(media, 'viewCount', 0) or 0)
                        skip_count = skip_count or int(getattr(media, 'skipCount', 0) or 0)
                        nested_last_played = _normalize_plex_datetime(getattr(media, 'lastViewedAt', None))
                        if nested_last_played:
                            last_played = nested_last_played

                        if hasattr(media, 'parts'):
                            parts = media.parts if isinstance(media.parts, (list, tuple)) else [media.parts]
                            for part in parts:
                                if not part:
                                    continue
                                view_count = view_count or int(getattr(part, 'viewCount', 0) or 0)
                                skip_count = skip_count or int(getattr(part, 'skipCount', 0) or 0)
                                part_last_played = _normalize_plex_datetime(getattr(part, 'lastViewedAt', None))
                                if part_last_played:
                                    last_played = part_last_played
                except Exception as _e:
                    logger.debug(f"Could not extract nested Plex media metrics: {_e}")

            rating = getattr(track, 'userRating', None) or getattr(track, 'rating', None) or 0
            
            # Get file tags for additional metadata
            duration = getattr(track, 'duration', 0) or 180000
            year = getattr(track, 'year', 2024) or 2024
            
            # The performing artist: originalTitle (set when it differs from the
            # album artist, as on compilations), else the album artist
            # (grandparentTitle). Both come with the track; track.artist() would
            # be one more Plex request per track and returns an object, not a name.
            artist_name = (getattr(track, 'originalTitle', None) or getattr(track, 'grandparentTitle', None)
                           or "Unknown")
            if not isinstance(artist_name, str):
                artist_name = getattr(artist_name, 'title', None) or str(artist_name)

            # Normalize date_added if it's a datetime
            date_added_val = getattr(track, 'addedAt', None) or int(time.time())
            if isinstance(date_added_val, datetime):
                try:
                    date_added_val = int(date_added_val.timestamp())
                except Exception:
                    date_added_val = int(time.time())

            # Genre lives on the album (or artist) object in Plex, not the
            # track, and one album is shared by many tracks — fetch it once.
            genre = "Unknown"
            album = self._fetch_plex_item_cached(getattr(track, 'parentKey', None))
            if album is not None:
                if getattr(album, 'genre', None):
                    genre = album.genre
                elif getattr(album, 'genres', None):
                    genre = ', '.join([g.tag for g in album.genres]) if isinstance(album.genres, list) else str(album.genres)
            if genre == "Unknown":
                artist_item = self._fetch_plex_item_cached(getattr(track, 'grandparentKey', None))
                if artist_item is not None and getattr(artist_item, 'genre', None):
                    genre = artist_item.genre

            album_title = getattr(track, 'parentTitle', None) or getattr(track, 'grandparentTitle', None)
            album_key = getattr(track, 'parentKey', None) or getattr(track, 'grandparentKey', None)

            album_rating = getattr(track, 'parentRating', None) or getattr(track, 'albumRating', None) or getattr(track, 'rating', None)
            if album_key and album_rating is None:
                album_for_rating = album if album is not None and album_key == getattr(track, 'parentKey', None) else self._fetch_plex_item_cached(album_key)
                if album_for_rating is not None:
                    album_rating = getattr(album_for_rating, 'userRating', None) or getattr(album_for_rating, 'rating', None)

            features = {
                'track_id': getattr(track, 'key', None),
                'title': getattr(track, 'title', 'Unknown'),
                'artist': artist_name,
                'album': album_title,
                'album_key': album_key,
                'album_rating': album_rating,
                'view_count': view_count,
                'skip_count': skip_count,
                'last_played': last_played,
                'rating': rating,
                'duration': duration,
                'year': year,
                'date_added': date_added_val,
                'genre': genre
            }
            return features
        except Exception as e:
            logger.warning(f"Error extracting features from track: {e}")
            return None
    
    def calculate_engagement_score(self, features):
        """
        Calculate track engagement score based on multiple factors.
        Score ranges from 0 to 1.
        """
        if not features:
            return 0

        view_count = features.get('view_count', 0) or 0
        skip_count = features.get('skip_count', 0) or 0
        rating = features.get('rating', 0) or 0
        album_rating = features.get('album_rating', 0) or 0
        last_played = features.get('last_played', 0) or 0

        # Do not reward tracks with no user engagement signals.
        # This prevents empty accounts from appearing confident simply because the
        # library is large.
        if view_count == 0 and last_played == 0 and rating == 0:
            return 0

        # Normalize components
        view_score = min(view_count / 100, 1.0) * 0.4  # 40% weight
        skip_penalty = max(0, (1 - min(skip_count / 10, 1.0)) * 0.3)  # 30% weight with penalty
        # Use track rating when available, otherwise fall back to album rating
        effective_rating = rating if rating > 0 else album_rating
        rating_score = (effective_rating / 10) * 0.2 if effective_rating > 0 else 0  # 20% weight

        # Recency bonus (played within last 7 days gets boost)
        now = int(time.time())
        days_since_played = (now - last_played) / 86400 if last_played else float('inf')
        recency_bonus = 0.1 if days_since_played < 7 else 0
        
        total_score = view_score + skip_penalty + rating_score + recency_bonus
        return min(total_score, 1.0)
    
    def analyze_all_tracks(self):
        """Analyze all tracks and store scores."""
        self._plex_item_cache = {}
        tracks = self.get_all_tracks()
        if not tracks:
            logger.warning("No tracks found to analyze")
            return []
        
        analyzed_tracks = []
        conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
        cursor = conn.cursor()
        
        for track in tracks:
            features = self.extract_track_features(track)
            if not features:
                continue
            
            score = self.calculate_engagement_score(features)
            
            cursor.execute('''
                INSERT OR REPLACE INTO track_scores
                (track_id, title, artist, album, album_key, album_rating, score, view_count, skip_count, last_played, date_added, rating, genre)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                features['track_id'], features['title'], features['artist'],
                features.get('album'), features.get('album_key'), features.get('album_rating'),
                score, features['view_count'], features['skip_count'],
                features['last_played'], features['date_added'], features['rating'],
                features.get('genre', 'Unknown')
            ))
            
            analyzed_tracks.append({
                'track': track,
                'features': features,
                'score': score
            })
        
        conn.commit()
        conn.close()
        logger.info(f"Analyzed {len(analyzed_tracks)} tracks")
        return analyzed_tracks
    
    
    def get_user_top_genres(self, limit=5):
        """
        Identify the top N genres the user likes based on high-engagement tracks.
        
        Returns: List of (genre, track_count, avg_score) tuples
        """
        try:
            conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
            cursor = conn.cursor()
            
            # Get genres from high-engagement tracks
            cursor.execute('''
                SELECT genre, COUNT(*) as count, AVG(score) as avg_score
                FROM track_scores
                WHERE score >= ?
                GROUP BY genre
                ORDER BY avg_score DESC, count DESC
                LIMIT ?
            ''', (SCORE_THRESHOLD, limit))
            
            genres = cursor.fetchall()
            conn.close()
            
            logger.info(f"User's top {len(genres)} genres: {[g[0] for g in genres]}")
            return genres
        
        except Exception as e:
            logger.warning(f"Error getting top genres: {e}")
            return []
    
    def get_user_top_artists(self, limit=20, score_threshold=0.6):
        """
        Identify the top N artists the user likes based on listen count and track scores.
        Calculates artist score as: (average_track_score * 0.6) + (listen_count_normalized * 0.4)
        
        Args:
            limit: Number of top artists to return
            score_threshold: Minimum track score to count a listen
        
        Returns: List of (artist, listen_count, avg_score, artist_score) tuples
        """
        try:
            conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
            cursor = conn.cursor()
            
            # Get artists with high-engagement tracks
            cursor.execute('''
                SELECT artist,
                       SUM(CASE WHEN view_count > 0 THEN view_count ELSE 1 END) as listen_count,
                       AVG(score) as avg_score
                FROM track_scores
                WHERE score >= ? AND (view_count > 0 OR last_played > 0)
                GROUP BY artist
                ORDER BY listen_count DESC, avg_score DESC
                LIMIT ?
            ''', (score_threshold, limit))
            
            artists = cursor.fetchall()
            conn.close()
            
            if not artists:
                logger.warning(f"No artists found with score >= {score_threshold}")
                return []
            
            # Calculate artist scores with normalization
            max_listen_count = artists[0][1] if artists else 1
            artist_scores = []
            
            for artist, listen_count, avg_score in artists:
                # Normalize listen count (0-1 scale)
                normalized_listen_count = listen_count / max(max_listen_count, 1)
                # Combined score: 60% track score, 40% listen frequency
                artist_score = (avg_score * 0.6) + (normalized_listen_count * 0.4)
                artist_scores.append((artist, listen_count, avg_score, artist_score))
            
            logger.info(f"Top {len(artist_scores)} artists: {[(a[0], a[3]) for a in artist_scores[:10]]}")
            return artist_scores
        
        except Exception as e:
            logger.warning(f"Error getting top artists: {e}")
            return []

    def get_recency_weighted_top_artists(self, limit=5, recency_days=30, score_threshold=0.6):
        """
        Identify the top N artists based on RECENT listening activity, so that an
        artist you've only just started listening to can outrank an artist you
        listened to heavily months ago but have since moved on from.

        Unlike get_user_top_artists() (which is an all-time ranking), this only
        considers plays within the last `recency_days` and adds a "freshness"
        bonus for artists whose most recent play is very recent. This is what
        powers the rotating top-5 Lidarr follow list.

        Score formula: (avg_score * 0.4) + (normalized_recent_listens * 0.4) + (freshness * 0.2)

        Returns: List of (artist, recent_listen_count, avg_score, artist_score, most_recent_played)
                 tuples, sorted best-first.
        """
        try:
            conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
            cursor = conn.cursor()

            now = int(time.time())
            cutoff = now - (recency_days * 86400)

            cursor.execute('''
                SELECT artist,
                       SUM(CASE WHEN view_count > 0 THEN view_count ELSE 1 END) as recent_listen_count,
                       AVG(score) as avg_score,
                       MAX(last_played) as most_recent_played
                FROM track_scores
                WHERE score >= ? AND last_played >= ?
                GROUP BY artist
                ORDER BY recent_listen_count DESC, avg_score DESC
            ''', (score_threshold, cutoff))

            artists = cursor.fetchall()
            conn.close()

            if not artists:
                logger.warning(f"No artists found with recent activity (score >= {score_threshold}, last {recency_days} days)")
                return []

            max_listen_count = max((a[1] for a in artists), default=1)
            artist_scores = []

            for artist, recent_listen_count, avg_score, most_recent_played in artists:
                normalized_listens = recent_listen_count / max(max_listen_count, 1)

                days_since_played = (now - most_recent_played) / 86400 if most_recent_played else recency_days
                freshness = max(0.0, 1 - (days_since_played / recency_days))

                artist_score = (avg_score * 0.4) + (normalized_listens * 0.4) + (freshness * 0.2)
                artist_scores.append((artist, recent_listen_count, avg_score, artist_score, most_recent_played))

            artist_scores.sort(key=lambda a: a[3], reverse=True)
            top = artist_scores[:limit]

            logger.info(f"Top {len(top)} recency-weighted artists (last {recency_days}d): {[(a[0], round(a[3], 3)) for a in top]}")
            return top

        except Exception as e:
            logger.warning(f"Error getting recency-weighted top artists: {e}")
            return []

    def _lidarr_find_artist(self, artist_name, lidarr_url, headers):
        """Search Lidarr for an artist and return the raw search result dict, or None."""
        try:
            if isinstance(artist_name, str) and artist_name.startswith('<Artist:'):
                artist_name = artist_name.rsplit(':', 1)[-1].rstrip('>')
            search_url = f"{lidarr_url}/api/v1/search?term=" + requests.utils.quote(artist_name)
            resp = requests.get(search_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"[LIDARR] Search failed for {artist_name}: {resp.status_code}")
                return None
            results = resp.json()
            if not results:
                return None
            # Lidarr search results wrap the artist metadata under `artist`.
            # Normalize that shape so add/monitor receives foreignArtistId.
            result = results[0]
            return result.get('artist', result) if isinstance(result, dict) else None
        except Exception as e:
            logger.warning(f"[LIDARR] Error searching for {artist_name}: {e}")
            return None

    def _lidarr_add_or_monitor_artist(self, artist_name, artist_data, lidarr_url, headers):
        """
        Ensure an artist exists in Lidarr and is monitored with monitorNewItems='all'.
        Adds the artist if missing, or re-enables monitoring if it already exists but
        was previously unmonitored (e.g. it was evicted from the top-5 before).

        Returns: (foreign_artist_id, lidarr_artist_id) on success, (None, None) on failure.
        """
        foreign_artist_id = artist_data.get('foreignArtistId')
        if not foreign_artist_id:
            logger.warning(f"[LIDARR] No foreignArtistId for {artist_name}")
            return None, None

        try:
            get_url = f"{lidarr_url}/api/v1/artist/{foreign_artist_id}"
            get_resp = requests.get(get_url, headers=headers, timeout=10)

            # Lidarr's artist GET endpoint expects its internal numeric ID, not
            # the MusicBrainz foreignArtistId returned by search. Resolve an
            # already-added artist before attempting POST, otherwise Lidarr
            # rejects the duplicate and the rotation never monitors it.
            if get_resp.status_code != 200:
                artists_resp = requests.get(f"{lidarr_url}/api/v1/artist", headers=headers, timeout=15)
                if artists_resp.status_code == 200:
                    for existing in artists_resp.json():
                        if existing.get('foreignArtistId') == foreign_artist_id:
                            get_resp = requests.get(
                                f"{lidarr_url}/api/v1/artist/{existing.get('id')}",
                                headers=headers,
                                timeout=10,
                            )
                            break

            if get_resp.status_code == 200:
                existing = get_resp.json()
                lidarr_artist_id = existing.get('id')
                changes = []
                if not existing.get('monitored', False) or existing.get('monitorNewItems') != 'all':
                    existing['monitored'] = True
                    existing['monitorNewItems'] = 'all'
                    changes.append('monitoring')
                # An artist Lidarr picked up from an import may be rooted in
                # another folder; it is moved to the library's root folder so
                # whatever Lidarr fetches lands in the library. Files already
                # on disk stay where they are (moveFiles=false).
                current_root = (existing.get('rootFolderPath') or '').rstrip('/')
                if current_root and current_root != ALBUM_DIR.rstrip('/'):
                    folder_name = os.path.basename((existing.get('path') or '').rstrip('/')) or artist_name
                    existing['rootFolderPath'] = ALBUM_DIR
                    existing['path'] = os.path.join(ALBUM_DIR, folder_name)
                    changes.append(f"root folder {current_root} → {ALBUM_DIR}")
                if changes:
                    put_url = f"{lidarr_url}/api/v1/artist/{lidarr_artist_id}"
                    put_resp = requests.put(put_url, headers=headers, params={'moveFiles': 'false'}, json=existing, timeout=15)
                    if put_resp.status_code in (200, 202):
                        logger.info(f"[LIDARR] ✓ Updated {artist_name}: {', '.join(changes)}")
                    else:
                        logger.warning(f"[LIDARR] Failed to update {artist_name}: {put_resp.status_code} - {put_resp.text[:200]}")
                else:
                    logger.info(f"[LIDARR] ✓ Already monitoring: {artist_name}")
                return foreign_artist_id, lidarr_artist_id

            # Artist doesn't exist yet in Lidarr, add it
            artist_payload = {
                'foreignArtistId': foreign_artist_id,
                'artistName': artist_data.get('artistName', artist_name),
                'monitored': True,
                'monitorNewItems': 'all',
                'qualityProfileId': artist_data.get('qualityProfileId') or 1,
                'metadataProfileId': artist_data.get('metadataProfileId') or 1,
                'rootFolderPath': artist_data.get('rootFolderPath') or ALBUM_DIR,
                'addOptions': {
                    'searchForMissingAlbums': True
                }
            }
            artists_endpoint = f"{lidarr_url}/api/v1/artist"
            add_resp = requests.post(artists_endpoint, headers=headers, json=artist_payload, timeout=15)
            if add_resp.status_code in (200, 201):
                lidarr_artist_id = add_resp.json().get('id')
                logger.info(f"[LIDARR] ✓ Added: {artist_name} (will monitor new + back-catalog)")
                return foreign_artist_id, lidarr_artist_id
            else:
                logger.warning(f"[LIDARR] Failed to add {artist_name}: {add_resp.status_code} - {add_resp.text[:200]}")
                return None, None

        except Exception as e:
            logger.warning(f"[LIDARR] Error adding/monitoring {artist_name}: {e}")
            return None, None

    def _lidarr_unmonitor_artist(self, artist_name, lidarr_artist_id, foreign_artist_id, lidarr_url, headers):
        """
        Stop monitoring an artist that's been evicted from the top-5 (does NOT delete
        the artist or any already-downloaded files - just stops watching for new releases).
        """
        try:
            lookup_id = lidarr_artist_id
            if not lookup_id and foreign_artist_id:
                get_url = f"{lidarr_url}/api/v1/artist/{foreign_artist_id}"
                get_resp = requests.get(get_url, headers=headers, timeout=10)
                if get_resp.status_code == 200:
                    lookup_id = get_resp.json().get('id')

            if not lookup_id:
                logger.warning(f"[LIDARR] Could not resolve Lidarr artist id to unmonitor: {artist_name}")
                return False

            get_url = f"{lidarr_url}/api/v1/artist/{lookup_id}"
            get_resp = requests.get(get_url, headers=headers, timeout=10)
            if get_resp.status_code != 200:
                logger.warning(f"[LIDARR] Could not fetch artist to unmonitor: {artist_name}")
                return False

            existing = get_resp.json()
            existing['monitored'] = False
            existing['monitorNewItems'] = 'none'

            put_url = f"{lidarr_url}/api/v1/artist/{lookup_id}"
            put_resp = requests.put(put_url, headers=headers, json=existing, timeout=15)
            if put_resp.status_code in (200, 202):
                logger.info(f"[LIDARR] ⤵ Unmonitored (evicted from top-5): {artist_name}")
                return True
            else:
                logger.warning(f"[LIDARR] Failed to unmonitor {artist_name}: {put_resp.status_code} - {put_resp.text[:200]}")
                return False

        except Exception as e:
            logger.warning(f"[LIDARR] Error unmonitoring {artist_name}: {e}")
            return False

    def enforce_top_artist_monitoring(self, user_index=0):
        """Keep Lidarr monitoring exactly the artists in the users' top-N lists.

        Lidarr adds an artist whenever it identifies that artist's files in
        one of its root folders, and with a monitoring default other than
        "none" it then searches for the artist's whole discography. The
        rotation tables of all users are the only artists that should be
        monitored, so each of those is (re)monitored, every other artist is
        unmonitored (files untouched), and the root folders' defaults are held
        at "none".

        Returns the number of artists unmonitored, or None if skipped.
        """
        instance = _lidarr_for_user(user_index)
        if not instance:
            return None
        lidarr_url = instance[0]
        headers = {'X-Api-Key': instance[1], 'Content-Type': 'application/json'}

        followed = _followed_on_instance(instance)
        if followed is None:
            return None
        keep = {row['lidarr_artist_id'] for rows in followed.values() for row in rows if row['lidarr_artist_id']}

        try:
            for folder in requests.get(f"{lidarr_url}/api/v1/rootfolder", headers=headers, timeout=15).json():
                if folder.get('defaultMonitorOption') != 'none' or folder.get('defaultNewItemMonitorOption') != 'none':
                    folder['defaultMonitorOption'] = 'none'
                    folder['defaultNewItemMonitorOption'] = 'none'
                    requests.put(f"{lidarr_url}/api/v1/rootfolder/{folder['id']}",
                                 headers=headers, json=folder, timeout=15)
                    logger.info(f"[LIDARR] Root folder {folder.get('path')}: new artists are now added unmonitored")

            artists = requests.get(f"{lidarr_url}/api/v1/artist", headers=headers, timeout=60).json()
            unmonitor = [a['id'] for a in artists if a.get('monitored') and a['id'] not in keep]
            remonitor = [a['id'] for a in artists if not a.get('monitored') and a['id'] in keep]
            if unmonitor:
                requests.put(f"{lidarr_url}/api/v1/artist/editor", headers=headers, timeout=60,
                             json={'artistIds': unmonitor, 'monitored': False, 'monitorNewItems': 'none'})
            if remonitor:
                requests.put(f"{lidarr_url}/api/v1/artist/editor", headers=headers, timeout=60,
                             json={'artistIds': remonitor, 'monitored': True, 'monitorNewItems': 'all'})
            logger.info(f"[LIDARR] Monitoring limited to {len(keep)} top artist(s): "
                        f"unmonitored {len(unmonitor)}, re-monitored {len(remonitor)}")
            return len(unmonitor)
        except Exception as exc:
            logger.warning(f"[LIDARR] Could not enforce top-artist monitoring: {exc}")
            return None

    def sync_top_artists_to_lidarr(self, top_n=None, recency_days=30, min_artist_score=0.6, user_index=0):
        """
        Keep exactly `top_n` artists followed in Lidarr at all times, based on RECENT
        listening activity. Artists compete for these slots: as a new artist climbs
        into the top N, whichever currently-followed artist has fallen the furthest
        is evicted (unmonitored in Lidarr, but its already-downloaded music is left
        alone).

        Returns: Dict with statistics {'top_artists': [...], 'added': int, 'evicted': int,
                                        'unchanged': int, 'errors': int}
        """
        top_n = min(top_n if top_n is not None else TOP_ARTIST_LIMIT, TOP_ARTIST_LIMIT)
        stats = {'top_artists': [], 'added': 0, 'evicted': 0, 'unchanged': 0, 'errors': 0}

        instance = _lidarr_for_user(user_index)
        if not instance:
            logger.warning("[LIDARR] Not configured")
            return stats
        lidarr_url, lidarr_api = instance

        headers = {'X-Api-Key': lidarr_api, 'Content-Type': 'application/json'}

        # 1. Compute the current top N based on recent listening
        new_top = self.get_recency_weighted_top_artists(
            limit=top_n, recency_days=recency_days, score_threshold=min_artist_score
        )
        if not new_top:
            logger.info("[LIDARR] No recently-active artists found; leaving current top-5 as-is")
            return stats

        new_top_names = {a[0] for a in new_top}
        stats['top_artists'] = [a[0] for a in new_top]

        # 2. Load who we're currently following (as tracked by this rotation, not Lidarr's full library)
        conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
        cursor = conn.cursor()
        cursor.execute('SELECT artist_name, foreign_artist_id, lidarr_artist_id FROM lidarr_followed_artists')
        currently_followed = {row[0]: {'foreign_artist_id': row[1], 'lidarr_artist_id': row[2]} for row in cursor.fetchall()}

        to_add = new_top_names - set(currently_followed.keys())
        to_evict = set(currently_followed.keys()) - new_top_names
        to_keep = new_top_names & set(currently_followed.keys())

        logger.info(f"[LIDARR] Top-{top_n} rotation: keep {len(to_keep)}, add {len(to_add)}, evict {len(to_evict)}")

        # 3. Evict artists that fell out of the top N (unmonitor only, files
        # untouched). One Lidarr can follow several users' top artists: an
        # artist another user still follows stays monitored, and only leaves
        # this user's rotation.
        others = _followed_on_instance(instance, exclude_user=APP_USER_NAME) or {}
        for artist_name in to_evict:
            info = currently_followed[artist_name]
            still_followed_by = [user for user, rows in others.items() if any(
                (info.get('lidarr_artist_id') and row['lidarr_artist_id'] == info.get('lidarr_artist_id'))
                or (info.get('foreign_artist_id') and row['foreign_artist_id'] == info.get('foreign_artist_id'))
                or row['artist_name'].casefold() == artist_name.casefold() for row in rows)]
            if still_followed_by:
                logger.info(f"[LIDARR] {artist_name} leaves this user's top {top_n} but stays monitored: "
                            f"{', '.join(still_followed_by)} still follow(s) it")
                success = True
            else:
                success = self._lidarr_unmonitor_artist(
                    artist_name, info.get('lidarr_artist_id'), info.get('foreign_artist_id'), lidarr_url, headers
                )
            if success:
                cursor.execute('DELETE FROM lidarr_followed_artists WHERE artist_name = ?', (artist_name,))
                stats['evicted'] += 1
            else:
                stats['errors'] += 1
        conn.commit()

        # 4. Add newly-qualifying artists
        artist_lookup = {a[0]: a for a in new_top}
        for artist_name in to_add:
            artist_data = self._lidarr_find_artist(artist_name, lidarr_url, headers)
            if not artist_data:
                logger.info(f"[LIDARR] Artist not found in search: {artist_name}")
                stats['errors'] += 1
                continue

            foreign_artist_id, lidarr_artist_id = self._lidarr_add_or_monitor_artist(
                artist_name, artist_data, lidarr_url, headers
            )
            if foreign_artist_id:
                _, recent_listen_count, _, artist_score, _ = artist_lookup[artist_name]
                cursor.execute('''
                    INSERT OR REPLACE INTO lidarr_followed_artists
                    (artist_name, foreign_artist_id, lidarr_artist_id, artist_score, recent_listen_count, followed_at, last_updated)
                    VALUES (?, ?, ?, ?, ?, COALESCE((SELECT followed_at FROM lidarr_followed_artists WHERE artist_name = ?), CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
                ''', (artist_name, foreign_artist_id, lidarr_artist_id, artist_score, recent_listen_count, artist_name))
                stats['added'] += 1
            else:
                stats['errors'] += 1
        conn.commit()

        # 5. Refresh scores for artists that stayed in the top N
        for artist_name in to_keep:
            _, recent_listen_count, _, artist_score, _ = artist_lookup[artist_name]
            cursor.execute('''
                UPDATE lidarr_followed_artists
                SET artist_score = ?, recent_listen_count = ?, last_updated = CURRENT_TIMESTAMP
                WHERE artist_name = ?
            ''', (artist_score, recent_listen_count, artist_name))
            stats['unchanged'] += 1
        conn.commit()
        conn.close()

        logger.info(
            f"[LIDARR] Top-{top_n} rotation complete: {stats['added']} added, "
            f"{stats['evicted']} evicted, {stats['unchanged']} unchanged, {stats['errors']} errors"
        )
        logger.info(f"[LIDARR] Currently following: {sorted(new_top_names)}")
        return stats

    def download_from_top_artists(self, top_artists=None, limit_per_artist=20, min_artist_score=0.6):
        """
        Download top songs from the user's most-listened artists using Discogs API.

        Args:
            top_artists: List from get_user_top_artists() (fetches if None)
            limit_per_artist: Max songs to download per artist
            min_artist_score: Minimum artist score threshold

        Returns: Number of songs downloaded
        """
        if top_artists is None:
            top_artists = self.get_user_top_artists(limit=50)

        if not top_artists:
            logger.warning("No top artists found for download")
            return 0

        # Filter by minimum artist score
        qualified_artists = [a for a in top_artists if a[3] >= min_artist_score]

        if not qualified_artists:
            logger.warning(f"No artists meet minimum score threshold of {min_artist_score}")
            return 0

        logger.info(f"📥 Downloading from {len(qualified_artists)} top artists (min score: {min_artist_score})")

        all_artist_tracks = []

        for artist_name, listen_count, avg_score, artist_score in qualified_artists:
            try:
                logger.info(f"🎤 Fetching top songs from '{artist_name}' (score: {artist_score:.2f}, listens: {listen_count})")

                # Use Discogs API to find artist albums/tracks
                if not DISCOGS_TOKEN:
                    logger.warning("Discogs API not configured (DISCOGS_TOKEN missing)")
                    continue
                
                try:
                    from discogs_client import Client
                    discogs_client = Client('MusicAI/1.0', user_token=DISCOGS_TOKEN)
                except ImportError:
                    logger.warning("discogs-client library not installed")
                    continue
                
                # Search for artist on Discogs
                logger.debug(f"Searching Discogs for artist: {artist_name}")
                artist_results = discogs_client.search(artist_name, type='artist')
                
                if not artist_results:
                    logger.debug(f"Artist not found on Discogs: {artist_name}")
                    continue
                
                artist_obj = artist_results[0]
                
                try:
                    # Get releases for this artist
                    artist_releases = artist_obj.releases
                    track_count = 0
                    
                    for release in artist_releases:
                        if track_count >= limit_per_artist:
                            break
                        
                        try:
                            release_title = release.title if hasattr(release, 'title') else str(release)
                            logger.debug(f"  Processing release: {release_title}")
                            
                            # Try to get tracklist from the release
                            if hasattr(release, 'tracklist'):
                                for track in release.tracklist:
                                    if track_count >= limit_per_artist:
                                        break
                                    
                                    try:
                                        track_title = track.title if hasattr(track, 'title') else str(track)
                                        
                                        # Skip non-track items (like info or dividers)
                                        if not track_title or len(track_title.strip()) == 0:
                                            continue
                                        
                                        rec = {
                                            'artist': artist_name,
                                            'title': track_title,
                                            'album': release_title,
                                            'confidence': 0.90,  # High confidence for artist's own releases
                                            'reason': f'Top song from artist {artist_name} (score: {artist_score:.2f})',
                                            'source': 'top_artists',
                                            'type': 'song'
                                        }
                                        all_artist_tracks.append(rec)
                                        track_count += 1
                                        logger.debug(f"    Added track: {track_title}")
                                    except Exception as e:
                                        logger.debug(f"    Error processing track: {e}")
                                        continue
                        
                        except Exception as e:
                            logger.debug(f"  Error processing release {release_title}: {e}")
                            continue
                
                except Exception as e:
                    logger.debug(f"Could not fetch releases for {artist_name}: {e}")
                    continue
                
            except Exception as e:
                logger.warning(f"Error processing artist {artist_name}: {e}")
                continue
            
        if all_artist_tracks:
            # Limit to top 100 tracks
            all_artist_tracks = all_artist_tracks[:100]
            logger.info(f"📥 Downloading {len(all_artist_tracks)} songs from top artists (via Discogs)...")

            # Download directly to albums directory
            # Note: Actual download happens via download_recommended_music in main workflow
            return len(all_artist_tracks)
        else:
            logger.warning("No tracks found from top artists")
            return 0
    
    def get_recently_played_tracks(self, limit=100):
        """
        Get the most recently played/added tracks from the library.
        
        Returns: List of (artist, title, track_id) tuples
        """
        try:
            conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT artist, title, track_id
                FROM track_scores
                ORDER BY last_played DESC, date_added DESC
                LIMIT ?
            ''', (limit,))
            
            tracks = cursor.fetchall()
            conn.close()
            
            logger.info(f"Retrieved {len(tracks)} recently played tracks")
            return tracks
        
        except Exception as e:
            logger.warning(f"Error getting recently played tracks: {e}")
            return []
    
    def get_similar_tracks_from_lastfm(self, artist, title, limit=10):
        """
        Use Last.FM API to find similar tracks.
        
        Args:
            artist: Artist name (string or Plex object)
            title: Track title (string or Plex object)
            limit: Number of similar tracks to return
        
        Returns: List of track recommendations
        """
        if not LAST_FM_API_KEY:
            logger.info("Last.FM API key not configured; trying public web fallback")
            return self._get_similar_tracks_from_deezer(artist, title, limit)
        
        recommendations = []
        
        try:
            import pylast
            import traceback
            
            # Convert Plex objects to clean strings
            artist_str = str(artist)
            title_str = str(title)
            
            # Handle Plex object format: "<Artist:7573:Manal>" -> extract just the name
            if artist_str.startswith('<') and ':' in artist_str:
                # Extract the last part which is the actual name
                parts = artist_str.split(':')
                artist_str = parts[-1].rstrip('>')
            
            if title_str.startswith('<') and ':' in title_str:
                # Extract the last part which is the actual name
                parts = title_str.split(':')
                title_str = parts[-1].rstrip('>')
            
            # Strip whitespace
            artist_str = artist_str.strip()
            title_str = title_str.strip()
            
            if not artist_str or not title_str:
                logger.debug(f"Invalid artist or title after conversion: artist='{artist_str}', title='{title_str}'")
                return []
            
            logger.debug(f"🔍 Last.FM lookup: Getting similar tracks to '{artist_str}' - '{title_str}'")

            network = pylast.LastFMNetwork(api_key=LAST_FM_API_KEY)
            artist_candidates = list(dict.fromkeys([
                artist_str,
                artist_str.replace('-', ' '),
                artist_str.replace('_', ' '),
                re.sub(r'\s+', ' ', artist_str.replace('-', ' ')).strip(),
            ]))
            track_obj = None
            lookup_artist = artist_str
            for candidate in artist_candidates:
                try:
                    candidate_track = network.get_track(candidate, title_str)
                    candidate_track.info
                    track_obj = candidate_track
                    lookup_artist = candidate
                    if candidate != artist_str:
                        logger.info(f"✓ Last.FM artist normalization matched '{artist_str}' as '{candidate}'")
                    break
                except Exception as lookup_error:
                    logger.debug(f"Last.FM lookup failed for artist variant '{candidate}': {lookup_error}")

            if track_obj is None:
                logger.warning(f"❌ Last.FM track not found for '{artist_str}' - '{title_str}'")
                return self._get_similar_tracks_from_deezer(artist_str, title_str, limit)
            
            logger.debug(f"  ✓ Found track object: {track_obj}")
            
            # Check if track actually exists on Last.FM before trying to get similar
            try:
                track_info = track_obj.info
                if not track_info:
                    logger.debug(f"  Track '{lookup_artist}' - '{title_str}' not found on Last.FM")
                    return self._get_similar_tracks_from_deezer(lookup_artist, title_str, limit)
                logger.debug(f"  ✓ Confirmed track exists on Last.FM (listeners: {getattr(track_info, 'listeners', 'N/A')})")
            except Exception as e_info:
                logger.debug(f"  Could not verify track existence on Last.FM: {repr(e_info)}")
                # Continue anyway - might still work
            
            # Attempt to retrieve similar tracks and provide detailed diagnostics on failure
            try:
                similar_tracks = track_obj.get_similar(limit=limit)
                logger.info(f"📊 Last.FM returned {len(similar_tracks)} similar track(s) for '{artist_str} - {title_str}'")
                
                if not similar_tracks:
                    logger.info(f"⚠️ No similar tracks found on Last.FM for '{artist_str}' - '{title_str}' (track may be too obscure)")
                    return self._get_similar_tracks_from_deezer(artist_str, title_str, limit)
            except Exception as e_sim:
                # Diagnostic: attempt to fetch artist presence to give more context
                logger.debug(f"  get_similar raised: {repr(e_sim)}")
                try:
                    artist_obj = network.get_artist(artist_str)
                    logger.debug(f"  Artist object found: {artist_obj}")
                except Exception as e_artist:
                    logger.debug(f"  get_artist failed: {repr(e_artist)}")

                # Return empty result but keep rich logs for troubleshooting
                logger.warning(f"❌ Could not get similar tracks from Last.FM for '{artist_str}' - '{title_str}': {repr(e_sim)}")
                logger.debug(traceback.format_exc())
                return self._get_similar_tracks_from_deezer(artist_str, title_str, limit)
            
            for idx, similar_track in enumerate(similar_tracks, 1):
                try:
                    artist_name = str(similar_track.item.artist.name)
                    track_title = str(similar_track.item.title)
                    match_score = float(similar_track.match)
                    
                    # Use the raw match_score as confidence; allow caller to filter by threshold
                    rec = {
                        'artist': artist_name,
                        'title': track_title,
                        'album': 'Unknown',
                        'confidence': match_score,
                        'reason': f'Similar to {artist_str} - {title_str} (match: {match_score:.1%})',
                        'source': 'lastfm_similar',
                        'type': 'song'
                    }
                    recommendations.append(rec)
                    logger.debug(f"  {idx}. {artist_name} - {track_title} (match: {match_score:.1%})")
                except Exception as track_err:
                    logger.debug(f"  Error parsing similar track {idx}: {track_err}")
                    continue
            
            if recommendations:
                logger.info(f"✓ Found {len(recommendations)} similar track(s) for '{artist_str} - {title_str}'")
            else:
                logger.debug(f"  No similar tracks could be parsed (similar_tracks list might be empty or malformed)")
            
            return recommendations
        
        except Exception as e:
            # Provide richer diagnostic information for easier troubleshooting
            import traceback
            logger.warning(f"❌ Could not get similar tracks from Last.FM: {repr(e)}")
            logger.debug(traceback.format_exc())
            return []
    
    def download_from_similar_tracks(self, recent_tracks=None, limit=100):
        """
        Download songs similar to the user's recently played tracks using Last.FM API.
        
        Args:
            recent_tracks: List from get_recently_played_tracks() (fetches if None)
            limit: Maximum total similar tracks to download
        
        Returns: Number of songs downloaded
        """
        if recent_tracks is None:
            recent_tracks = self.get_recently_played_tracks(limit=100)
        
        if not recent_tracks:
            logger.warning("⚠️ No recently played tracks found")
            return 0
        
        logger.info(f"📊 Fetching recently played tracks for similar track discovery...")
        logger.info(f"🎵 Finding similar tracks from {len(recent_tracks)} recently played songs...")
        
        all_similar_tracks = []
        successful_lookups = 0
        failed_lookups = 0
        
        for idx, (artist, title, track_id) in enumerate(recent_tracks[:20], 1):  # Limit to first 20 to avoid API spam
            try:
                logger.debug(f"  [{idx}/20] Looking up: {artist} - {title}")
                
                similar = self.get_similar_tracks_from_lastfm(artist, title, limit=5)
                
                if similar:
                    logger.debug(f"    ✓ Got {len(similar)} similar track(s)")
                    all_similar_tracks.extend(similar)
                    successful_lookups += 1
                else:
                    logger.debug(f"    ℹ️ No similar tracks found for this song")
                    failed_lookups += 1
                    
            except Exception as e:
                logger.debug(f"    ❌ Error getting similar tracks: {e}")
                failed_lookups += 1
                continue
        
        logger.info(f"📊 Similar track lookup summary: {successful_lookups} successful, {failed_lookups} failed")
        
        if all_similar_tracks:
            # Debug: show a summary of candidate matches and confidences
            try:
                sample = all_similar_tracks[:20]
                summary_str = ", ".join([f"{t.get('artist','?')} - {t.get('title','?')} ({t.get('confidence',0):.2f})" for t in sample])
                logger.debug(f"🔎 Similar candidates: {summary_str}")
            except Exception:
                pass
            # Limit to requested amount
            all_similar_tracks = all_similar_tracks[:limit]
            logger.debug(f"📋 Limiting to {len(all_similar_tracks)} tracks (limit: {limit})")
            
            # Filter by confidence threshold (configurable via env LASTFM_SIMILARITY_THRESHOLD)
            try:
                threshold = float(os.environ.get('LASTFM_SIMILARITY_THRESHOLD', '0.85'))
            except Exception:
                threshold = 0.85
            filtered_tracks = [t for t in all_similar_tracks if t.get('confidence', 0) >= threshold]
            logger.info(f"✓ {len(filtered_tracks)} track(s) meet confidence threshold of {threshold}")
            
            if filtered_tracks:
                logger.info(f"📥 Downloading {len(filtered_tracks)} similar tracks to {ALBUM_DIR}...")
                
                # Download directly to albums directory
                result = self.download_recommended_music(
                    filtered_tracks,
                    preference_confidence=0.85,
                    playlist='similar',
                )
                
                # Handle return value - could be count, list, or None
                if isinstance(result, (int, float)):
                    result_count = int(result)
                elif isinstance(result, list):
                    result_count = len(result)
                elif result is not None:
                    result_count = 1
                else:
                    result_count = len(filtered_tracks)
                
                logger.info(f"✓ Similar tracks download complete: {result_count} track(s) processed")
                return result_count
            else:
                logger.warning(f"⚠️ No similar tracks met confidence threshold of 0.85")
                return 0
        else:
            logger.warning("⚠️ No similar tracks found")
            return 0
    

    @with_rate_limit('billboard', max_retries=3)
    def get_billboard_hot_100(self, limit=20):
        """
        Get current Billboard Hot 100 chart tracks.
        
        Returns: List of track recommendations from current Hot 100 chart
        """
        recommendations = []
        
        try:
            import billboard
            
            # Get current Hot 100 chart
            chart = billboard.ChartData('hot-100')
            
            logger.info(f"📊 Fetched Billboard Hot 100 chart ({len(chart)} tracks)")
            
            # Extract top tracks
            for idx, track in enumerate(chart[:limit]):
                # Parse artist and title from track
                artist = track.artist if hasattr(track, 'artist') else 'Unknown'
                title = track.title if hasattr(track, 'title') else 'Unknown'
                rank = track.rank if hasattr(track, 'rank') else idx + 1
                
                # Billboard confidence based on rank (top ranks = higher confidence)
                # Rank 1 = 1.0 confidence, Rank 100 = 0.0 confidence
                confidence = max(0.0, min(1.0, 1.0 - (rank - 1) / 100.0))
                
                rec = {
                    'artist': artist,
                    'title': title,
                    'rank': rank,
                    'genre': 'Billboard',  # Mark as Billboard chart music
                    'reason': f'Billboard Hot 100 #{rank}',
                    'confidence': confidence,
                    'source': 'billboard_hot_100',
                    'type': 'song'
                }
                recommendations.append(rec)
                logger.info(f"📊 Billboard Hot 100 #{rank}: {artist} - {title} (confidence: {confidence:.0%})")
            
            return recommendations
        
        except ImportError:
            error_msg = "billboard-py library not installed. Cannot fetch Billboard charts."
            logger.error(error_msg)
            try:
                notify_discord_error(f"📊 Billboard Hot 100 Import Error\n{error_msg}")
            except Exception:
                pass
            return recommendations
        except Exception as e:
            error_msg = f"Error fetching Billboard Hot 100: {str(e)}"
            logger.warning(error_msg)
            try:
                notify_discord_error(f"📊 Billboard Hot 100 Unexpected Error\n{error_msg}")
            except Exception:
                pass
            return recommendations
    
    @with_rate_limit('billboard', max_retries=3)
    def get_billboard_top_by_genres(self, user_genres, limit_per_genre=10):
        """
        Get top Billboard chart tracks for each of the user's favorite genres.
        
        Args:
            user_genres: List of (genre_name, track_count, avg_score) tuples from get_user_top_genres()
            limit_per_genre: Number of tracks to fetch per genre (default 10)
        
        Returns: List of track recommendations organized by genre
        """
        all_recommendations = []
        
        try:
            import billboard
            
            # Map common genres to Billboard chart names
            genre_chart_mapping = {
                'pop': 'hot-100',  # Pop = Main Hot 100
                'rap': 'rap',      # Rap/Hip-hop
                'hip-hop': 'rap',
                'hip hop': 'rap',
                'r&b': 'r-b-hip-hop',  # R&B/Hip-hop
                'rb': 'r-b-hip-hop',
                'rock': 'rock',    # Rock
                'alternative': 'alternative-songs',
                'alt': 'alternative-songs',
                'country': 'country',
                'electronic': 'dance-electronic-songs',
                'edm': 'dance-electronic-songs',
                'dance': 'dance-electronic-songs',
                'latin': 'latin-songs',
                'latino': 'latin-songs',
                'rnb': 'r-b-hip-hop',
                'soundtrack': 'hot-100',  # Use main chart for soundtracks
            }
            
            # Process each user genre
            for genre_name, track_count, avg_score in user_genres:
                if not genre_name or track_count == 0:
                    continue
                
                # Find matching Billboard chart
                chart_name = genre_chart_mapping.get(genre_name.lower(), 'hot-100')
                
                logger.info(f"🎵 Fetching Billboard charts for genre: {genre_name} (using chart: {chart_name})")
                
                try:
                    chart = billboard.ChartData(chart_name)
                    logger.info(f"  ✓ Found {len(chart)} tracks in {chart_name} chart")
                    
                    # Extract top tracks for this genre
                    genre_recs = []
                    for idx, track in enumerate(chart[:limit_per_genre]):
                        artist = track.artist if hasattr(track, 'artist') else 'Unknown'
                        title = track.title if hasattr(track, 'title') else 'Unknown'
                        rank = track.rank if hasattr(track, 'rank') else idx + 1
                        
                        # Calculate confidence: Higher rank in genre chart + user's genre affinity
                        rank_confidence = max(0.0, 1.0 - (rank - 1) / 100.0)
                        genre_confidence = min(1.0, avg_score * 1.5)  # Boost by user's liking
                        confidence = (rank_confidence * 0.6) + (genre_confidence * 0.4)
                        
                        rec = {
                            'artist': artist,
                            'title': title,
                            'rank': rank,
                            'genre': genre_name,
                            'reason': f'Billboard Trending {genre_name} #{rank}',
                            'confidence': confidence,
                            'source': f'billboard_{genre_name.lower()}',
                            'type': 'song'
                        }
                        genre_recs.append(rec)
                        logger.info(f"  📊 {genre_name} #{rank}: {artist} - {title} (confidence: {confidence:.0%})")
                    
                    all_recommendations.extend(genre_recs)
                
                except Exception as e:
                    error_msg = f"Could not fetch {chart_name} chart: {str(e)}"
                    logger.warning(f"  ✗ {error_msg}")
                    try:
                        from discord_bot import send_error_to_discord
                        notify_discord_error(f"📊 Billboard Chart Error ({chart_name})\n{error_msg}")
                    except Exception:
                        pass
                    continue
            
            logger.info(f"🎵 Total Billboard recommendations by genre: {len(all_recommendations)}")
            return all_recommendations
        
        except ImportError:
            error_msg = "billboard-py library not installed. Cannot fetch Billboard charts."
            logger.error(error_msg)
            try:
                notify_discord_error(f"📊 Billboard Import Error\n{error_msg}")
            except Exception:
                pass
            return all_recommendations
        except Exception as e:
            error_msg = f"Error fetching Billboard charts by genre: {str(e)}"
            logger.warning(error_msg)
            try:
                notify_discord_error(f"📊 Billboard Unexpected Error\n{error_msg}")
            except Exception:
                pass
            return all_recommendations
    
    def get_tiktok_trending_music(self, limit=20):
        """
        Get trending music from TikTok.
        Fetches trending songs from TikTok's music charts.
        
        Returns: List of track recommendations from TikTok trending
        """
        recommendations = []
        
        try:
            
            logger.info(f"🎵 Fetching {limit} trending songs from TikTok...")
            
            # TikTok trending API endpoint (unofficial but commonly used)
            # Using a public TikTok trending music endpoint
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            # Method 1: Try to use a TikTok trending endpoint
            try:
                # This uses TikTok's web API for trending sounds
                url = "https://api.tiktok.com/v1/share/discover/sounds/"
                params = {
                    'cursor': '0',
                    'count': limit,
                    'type': 'music'
                }
                response = requests_get_with_limit(url, service_name='tiktok', headers=headers, params=params, timeout=10)
                if not response:
                    raise Exception("No response from TikTok trending endpoint")
                response.raise_for_status()
                
                data = response.json()
                tracks = data.get('data', {}).get('sounds', [])
                
                for idx, track in enumerate(tracks[:limit]):
                    try:
                        track_id = track.get('id', str(idx))
                        track_name = track.get('title', 'Unknown')
                        artist_name = track.get('author', 'Unknown')
                        
                        # TikTok uses view count as popularity metric
                        play_count = track.get('plays', 0)
                        
                        # Calculate confidence based on play count
                        # Normalize play count (assume millions of plays for top tracks)
                        popularity = min(1.0, (play_count / 10000000))  # 10M plays = max confidence
                        
                        rec = {
                            'artist': artist_name,
                            'title': track_name,
                            'rank': idx + 1,
                            'genre': 'TikTok Trending',
                            'reason': f'TikTok Trending #{idx + 1}',
                            'confidence': max(0.5, popularity),  # Min 0.5 for any trending track
                            'source': 'tiktok_trending',
                            'popularity': play_count,
                            'type': 'song'
                        }
                        recommendations.append(rec)
                        logger.info(f"🎵 TikTok Trending #{idx + 1}: {artist_name} - {track_name} (popularity: {play_count:,})")
                    except Exception as e:
                        logger.debug(f"Error processing TikTok track: {e}")
                        continue
                
                logger.info(f"✓ Fetched {len(recommendations)} TikTok trending recommendations")
                
            except Exception as e:
                error_msg = f"Error fetching TikTok trending data: {str(e)}"
                logger.warning(error_msg)
                # Try to send error to Discord if available
                try:
                    notify_discord_error(f"🎵 TikTok API Error\n{error_msg}")
                except Exception:
                    pass
                
            return recommendations
        
        except ImportError:
            error_msg = "Requests library not available. Cannot fetch TikTok trending data."
            logger.warning(error_msg)
            try:
                notify_discord_error(f"🎵 TikTok Import Error\n{error_msg}")
            except Exception:
                pass
            return recommendations
        except Exception as e:
            error_msg = f"Unexpected error fetching TikTok trending: {str(e)}"
            logger.warning(error_msg)
            try:
                notify_discord_error(f"🎵 TikTok Unexpected Error\n{error_msg}")
            except Exception:
                pass
            return recommendations
    

    def get_spotify_trending_music(self, limit=20):
        """Trending tracks from Spotify's public charts and editorial playlists.

        Returns: list of recommendation dicts (artist/title/album/confidence/...)
        """
        recommendations = []
        seen = set()
        sources = [('chart', key) for key in ('top-50-global', 'todays-top-hits')]
        sources += [('playlist', pid) for _, pid in spotify_web.editorial_playlist_ids('trending')]

        for kind, ref in sources:
            if len(recommendations) >= limit:
                break
            name, tracks = spotify_web.get_chart(ref) if kind == 'chart' else spotify_web.get_playlist(ref)
            if not tracks:
                logger.debug(f"Spotify {kind} {ref} returned no tracks")
                continue
            for rank, track in enumerate(tracks, 1):
                if len(recommendations) >= limit:
                    break
                key = (track['artist'].casefold(), track['title'].casefold())
                if key in seen:
                    continue
                seen.add(key)
                # Chart position is the only popularity signal on the public page.
                popularity = max(0.5, 1.0 - (rank - 1) / max(len(tracks), 1) * 0.5)
                recommendations.append({
                    'artist': track['artist'],
                    'title': track['title'],
                    'album': track.get('album', ''),
                    'rank': len(recommendations) + 1,
                    'genre': 'Spotify Trending',
                    'reason': f'Spotify Trending - {name}',
                    'confidence': max(0.7, popularity),
                    'source': 'spotify_trending',
                    'popularity': popularity,
                    'type': 'song',
                })
        logger.info(f"✓ Fetched {len(recommendations)} Spotify trending recommendations")
        return recommendations

    def get_popular_spotify_playlists(self, limit=10):
        """Spotify's editorial genre/mood playlists, read from their public pages.

        Returns:
            list: [{'name': str, 'id': str, 'track_count': int, 'tracks': [...]}, ...]
        """
        popular_playlists = []
        for _, playlist_id in spotify_web.editorial_playlist_ids('popular'):
            if len(popular_playlists) >= limit:
                break
            name, tracks = spotify_web.get_playlist(playlist_id)
            if not tracks:
                continue
            logger.info(f"📋 Popular Playlist: {name} ({len(tracks)} tracks)")
            popular_playlists.append({
                'name': name,
                'id': playlist_id,
                'track_count': len(tracks),
                'tracks': [
                    {
                        'id': t.get('id'),
                        'name': t['title'],
                        'artist': t['artist'],
                        'album': t.get('album') or 'Unknown',
                        'genres': [],
                    }
                    for t in tracks[:100]
                ],
            })
        logger.info(
            f"✓ Fetched {len(popular_playlists)} popular Spotify playlists "
            f"({sum(p['track_count'] for p in popular_playlists)} total tracks)"
        )
        return popular_playlists

    def process_discord_requests(self):
        """The queued Discord requests, processed by one caller at a time; 0
        when another is already on them (they will be done by it)."""
        if not _DISCORD_QUEUE_LOCK.acquire(blocking=False):
            logger.info("Queued Discord requests are already being processed")
            return 0
        try:
            return self._process_discord_requests()
        finally:
            _DISCORD_QUEUE_LOCK.release()

    def _process_discord_requests(self):
        """
        Process pending Discord music requests from the JSON file created by discord_bot.py.
        Processes both song requests and metadata fix requests.
        
        Returns: Number of processed requests
        """
        try:
            # Load Discord requests from JSON file (must use DATA_DIR to match discord_bot.py)
            data_dir = os.environ.get('DATA_DIR', '/app/data')
            requests_db_path = os.path.join(data_dir, 'discord_requests.json')
            
            if not os.path.exists(requests_db_path):
                logger.debug(f"No Discord requests file found at {requests_db_path}")
                return 0
            
            with open(requests_db_path, 'r') as f:
                all_user_requests = json.load(f)
            
            if not all_user_requests:
                logger.debug("Discord requests file is empty")
                return 0
            
            logger.info(f"🔍 Processing Discord requests from {len(all_user_requests)} user(s)")
            processed_count = 0
            worked_on = {}
            
            # Iterate through each user's requests
            for username, user_requests in all_user_requests.items():
                logger.debug(f"  User '{username}': {len(user_requests)} request(s)")
                
                for idx, req in enumerate(user_requests):
                    # Skip already processed requests
                    if req.get('status') != 'pending':
                        logger.debug(f"    Skipping request #{idx} (status: {req.get('status')})")
                        continue
                    worked_on.setdefault(username, []).append(req)

                    try:
                        req_type = req.get('type')
                        artist = req.get('artist', 'Unknown')
                        title = req.get('title', 'Unknown')

                        normalized_req_type = str(req_type or '').strip()
                        if normalized_req_type.startswith('music_request_'):
                            normalized_req_type = 'download_song'

                        logger.info(f"📋 Processing request #{idx}: type={req_type}, artist={artist}, title={title}")

                        # Handle all download types: download, download_song, download_album, download_artist,
                        # plus queued bot requests saved as `music_request_<source>`
                        if normalized_req_type in ['download', 'download_song', 'download_album', 'download_artist']:
                            download_type = normalized_req_type.replace('download_', '') or 'song'
                            logger.info(f"🎵 Processing {download_type.upper()} download request from {username}: {artist} - {title}")
                            
                            # Check if track already exists
                            try:
                                exists, existing_path, similarity = check_if_track_exists(artist, title)
                                if exists and similarity >= 0.90:
                                    logger.info(f"⏭️  Track already exists: {existing_path}")
                                    req['status'] = 'completed'
                                    req['result'] = f"Track already exists in library"
                                    processed_count += 1
                                    continue
                            except Exception as check_e:
                                logger.debug(f"Could not check if track exists: {check_e}")
                            
                            # An explicit request wins over an earlier dislike or deletion.
                            import library_review
                            if library_review.unblacklist(title, artist):
                                logger.info(f"🔓 Requested again, off the blacklist: {artist} - {title}")
                            
                            # Create recommendation and download
                            rec = {
                                'artist': artist,
                                'title': title,
                                'album': req.get('album'),
                                'url': req.get('url'),
                                'request_context': req.get('request_context'),
                                'reason': f'Discord {download_type} request from {username}',
                                'confidence': 0.85,
                                'type': download_type
                            }
                            
                            # Try to download with better error handling
                            try:
                                logger.info(f"  📥 Attempting to download ({download_type}): {artist} - {title}")
                                logger.debug(f"  📥 Using download recommendation: {rec}")
                                
                                # A requested song is wanted by someone: straight into the
                                # library, never on probation, and in no cycle playlist (the
                                # requester knows it is there). A cycle never asks anyone which
                                # song they meant (request_choice): it takes the best match, and
                                # a wrong one can go in the Fix Metadata playlist.
                                results = self.download_recommended_music([rec], preference_confidence=0.85,
                                                                          target_dir=ALBUM_DIR)
                                
                                logger.debug(f"  📥 Download returned: {results}")
                                
                                if results and len(results) > 0:
                                    logger.info(f"✅ Successfully downloaded via Discord: {artist} - {title}")
                                    req['status'] = 'completed'
                                    req['result'] = f"Downloaded successfully ({len(results)} result(s))"
                                else:
                                    logger.warning(f"❌ No results from download attempt for: {artist} - {title}")
                                    req['status'] = 'failed'
                                    req['result'] = 'Download returned no results - may be rate limited or unavailable'
                            except Exception as download_e:
                                logger.error(f"❌ Download exception for {artist} - {title}: {download_e}", exc_info=True)
                                req['status'] = 'failed'
                                req['result'] = f'Download error: {str(download_e)[:100]}'
                            
                            processed_count += 1
                        
                        else:
                            logger.warning(f"⚠️  Unknown Discord request type: {req_type}")
                            req['status'] = 'failed'
                            req['result'] = f"Unknown request type: {req_type}"
                    
                    except Exception as e:
                        logger.error(f"Error processing Discord request: {e}", exc_info=True)
                        req['status'] = 'failed'
                        req['result'] = str(e)
                        processed_count += 1
            
            # Save updated requests back to JSON
            try:
                request_history.apply_outcomes(requests_db_path, worked_on)
                logger.info(f"✅ Saved Discord request status updates ({processed_count} processed)")
            except Exception as save_e:
                logger.error(f"Could not save Discord request updates: {save_e}")
            
            return processed_count
        
        except Exception as e:
            logger.error(f"Error processing Discord requests: {e}", exc_info=True)
            return 0
        finally:
            # A request may have moved the Mullvad relay to reach a
            # region-locked video; put the configured relay back.
            geo_relay.restore()
    
    def clear_database(self, force=False, db_path=None):
        """
        Clear main database tables so the app can start from a clean state.
        This deletes data from `track_scores`, `recommendations`, and `temp_downloads`.
        Use with caution. If `force` is False, the operation will ask for interactive confirmation when run from CLI.

        Args:
            force: must be True to run without interactive confirmation
            db_path: explicit path to the sqlite database file to clear; if None, uses module DB_PATH
        """
        try:
            if not force:
                # If called programmatically without force, require explicit confirmation via argument
                logger.warning("clear_database called without --force. To run non-interactively, pass --force from CLI.")
                return False

            # Determine which DB file to operate on
            target_db = db_path or os.environ.get('DB_PATH', DB_PATH)
            if not target_db:
                logger.warning("No DB_PATH specified; nothing to clear")
                return False

            if not os.path.exists(target_db):
                logger.warning(f"Database file not found: {target_db}")
                return False

            conn = sqlite3.connect(target_db)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM track_scores')
            cursor.execute('DELETE FROM recommendations')
            cursor.execute('DELETE FROM temp_downloads')
            conn.commit()
            # VACUUM to reclaim space and reset sqlite internals
            cursor.execute('VACUUM')
            conn.close()
            logger.info(f"✓ Cleared database tables in {target_db}: track_scores, recommendations, temp_downloads")

            # Recreate full schema so this DB is usable for subsequent lookups
            try:
                conn = sqlite3.connect(target_db)
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS track_scores (
                        track_id TEXT PRIMARY KEY,
                        title TEXT,
                        artist TEXT,
                        album TEXT,
                        album_key TEXT,
                        album_rating REAL,
                        score REAL,
                        view_count INTEGER,
                        skip_count INTEGER,
                        last_played INTEGER,
                        date_added INTEGER,
                        rating INTEGER,
                        genre TEXT DEFAULT 'Unknown',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS recommendations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        artist TEXT,
                        track TEXT,
                        reason TEXT,
                        confidence REAL,
                        status TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS temp_downloads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        artist TEXT,
                        track_title TEXT,
                        file_path TEXT,
                        reason TEXT,
                        confidence REAL,
                        status TEXT,
                        is_trending BOOLEAN DEFAULT 0
                    )
                ''')
                conn.commit()
                conn.close()
                logger.info(f"ℹ️ Re-created empty schema in {target_db} (ready for future population)")
            except Exception as e:
                logger.warning(f"Could not re-create schema in {target_db}: {e}")

            return True
        except Exception as e:
            logger.error(f"Failed to clear database ({db_path or DB_PATH}): {e}")
            return False
            return False
        
        except Exception as e:
            logger.error(f"Error managing genre playlist: {e}")
            return None
    
    def _get_similar_tracks_from_deezer(self, artist, title, limit=10):
        """Use Deezer's public API when Last.FM has no track similarity data."""
        try:
            query = f'artist:"{artist}" track:"{title}"'
            response = requests.get(
                'https://api.deezer.com/search/track',
                params={'q': query, 'limit': 5},
                timeout=20,
            )
            matches = response.json().get('data', []) if response.ok else []
            if not matches:
                logger.warning(f"Deezer fallback could not find '{artist} - {title}'")
                return self._get_similar_tracks_from_lastfm_web(artist, title, limit)

            seed = matches[0]
            related = requests.get(
                f"https://api.deezer.com/track/{seed['id']}/related",
                timeout=20,
            )
            related_tracks = related.json().get('data', []) if related.ok else []

            # Deezer sometimes has no track-level neighbors. In that case use
            # related artists and their public top tracks instead.
            if not related_tracks:
                related_artists = requests.get(
                    f"https://api.deezer.com/artist/{seed['artist']['id']}/related",
                    timeout=20,
                )
                for related_artist in (related_artists.json().get('data', []) if related_artists.ok else [])[:5]:
                    top_response = requests.get(
                        f"https://api.deezer.com/artist/{related_artist['id']}/top",
                        params={'limit': min(5, limit)},
                        timeout=20,
                    )
                    related_tracks.extend(top_response.json().get('data', []) if top_response.ok else [])
                    if len(related_tracks) >= limit:
                        break

            recommendations = []
            seen = set()
            for track in related_tracks:
                track_artist = (track.get('artist') or {}).get('name', 'Unknown')
                track_title = track.get('title')
                key = (track_artist.casefold(), (track_title or '').casefold())
                if not track_title or key in seen:
                    continue
                seen.add(key)
                recommendations.append({
                    'artist': track_artist,
                    'title': track_title,
                    'album': (track.get('album') or {}).get('title', 'Unknown'),
                    'confidence': 0.86,
                    'reason': f'Deezer fallback similar to {artist} - {title}',
                    'source': 'deezer_similar_fallback',
                    'type': 'song',
                })
                if len(recommendations) >= limit:
                    break

            logger.info(f"Deezer fallback returned {len(recommendations)} similar track(s) for '{artist} - {title}'")
            return recommendations or self._get_similar_tracks_from_lastfm_web(artist, title, limit)
        except Exception as fallback_error:
            logger.warning(f"Deezer similar-track fallback failed for '{artist} - {title}': {fallback_error}")
            return self._get_similar_tracks_from_lastfm_web(artist, title, limit)

    def _get_similar_tracks_from_lastfm_web(self, artist, title, limit=10):
        """Scrape Last.FM's public similar-track page without Spotify Premium."""
        try:
            from bs4 import BeautifulSoup

            artist_path = requests.utils.quote(str(artist).strip().replace(' ', '+'), safe='+')
            title_path = requests.utils.quote(str(title).strip().replace(' ', '+'), safe='+')
            url = f"https://www.last.fm/music/{artist_path}/_/{title_path}/+similar"
            response = requests.get(
                url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml',
                },
                timeout=20,
            )
            if response.status_code != 200:
                logger.warning(f"Last.FM web fallback returned HTTP {response.status_code} for '{artist} - {title}'")
                return []

            soup = BeautifulSoup(response.text, 'html.parser')
            recommendations = []
            seen = set()
            for link in soup.select('a[href*="/_/"]'):
                href = link.get('href', '')
                parts = href.strip('/').split('/_/')
                if len(parts) != 2 or '/music/' not in href:
                    continue
                # parts[0] comes from the stripped href: "music/Artist+Name",
                # with no leading slash, so it is split on 'music/'.
                similar_artist = requests.utils.unquote(parts[0].split('music/', 1)[-1]).replace('+', ' ').strip()
                similar_title = requests.utils.unquote(parts[1].split('/', 1)[0]).replace('+', ' ').strip()
                key = (similar_artist.casefold(), similar_title.casefold())
                if not similar_artist or not similar_title or key in seen:
                    continue
                seen.add(key)
                recommendations.append({
                    'artist': similar_artist,
                    'title': similar_title,
                    'album': 'Unknown',
                    'confidence': 0.86,
                    'reason': f'Last.FM web similar to {artist} - {title}',
                    'source': 'lastfm_web_similar',
                    'type': 'song',
                })
                if len(recommendations) >= limit:
                    break

            logger.info(f"Last.FM web fallback returned {len(recommendations)} similar track(s) for '{artist} - {title}'")
            return recommendations
        except Exception as fallback_error:
            logger.warning(f"Last.FM web fallback failed for '{artist} - {title}': {fallback_error}")
            return []
    
    def _plex_tracks_by_path(self):
        """Every track of the music section by its file (container path),
        fetched at most every ten minutes."""
        cached = getattr(self, '_tracks_by_path', None)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        by_path = {}
        if self.plex:
            try:
                section = self.plex.library.section(PLEX_MUSIC_SECTION)
                for track in section.searchTracks():
                    for media in track.media or []:
                        for part in media.parts or []:
                            by_path[os.path.normpath(convert_host_path_to_container_path(part.file))] = track
            except Exception as e:
                logger.warning(f"Could not list the Plex library's tracks: {e}")
                return {}
        self._tracks_by_path = (time.time(), by_path)
        return by_path

    def _paths_of_track(self, track_id):
        """The files of a Plex track (by its key, as track_scores stores it)."""
        if not self.plex or not track_id:
            return []
        try:
            track = self.plex.fetchItem(track_id)
        except Exception as e:
            logger.debug(f"Plex has no track {track_id}: {e}")
            return []
        paths = []
        for media in getattr(track, 'media', None) or []:
            for part in media.parts or []:
                path = os.path.normpath(convert_host_path_to_container_path(part.file))
                if os.path.isfile(path):
                    paths.append(path)
        return paths

    def _lidarr_kept_paths(self):
        """Lidarr's files in albums it monitors — deleted, Lidarr would download
        them again, so they never go. None when Lidarr can't be asked."""
        cached = getattr(self, '_lidarr_kept', None)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        kept = None
        try:
            from lidarr_catalog import LidarrCatalog
            catalog = LidarrCatalog.from_env()
            kept = set() if catalog is None else catalog.load().kept_track_file_paths()
        except Exception as e:
            logger.warning(f"Lidarr can't be asked which songs it keeps: {e}")
        self._lidarr_kept = (time.time(), kept)
        return kept

    def _set_to_go(self, path, why):
        """Put a song on the soon-to-go list: first to be deleted when disk
        space runs low, unless someone rescues it (rates it
        SOON_TO_GO_RESCUE_RATING or more).
        Lidarr's songs of albums it monitors never go there. True if marked."""
        import library_state
        path = os.path.normpath(path)
        if library_state.state_of(path) == library_state.SOON_TO_GO:
            return False
        kept = self._lidarr_kept_paths()
        if kept and path in kept:
            logger.info(f"Not set to go, Lidarr monitors its album: {_in_library(path)}")
            return False
        if library_state.mark(path, library_state.SOON_TO_GO):
            logger.info(f"⏳ Soon to go ({why}): {_in_library(path)}")
            return True
        return False
    
    def set_disliked_songs_to_go(self):
        """A song two users have rated one star or less (Plex stores stars ×2,
        so ≤ 2.0) goes on the soon-to-go list: it stays playable and is the
        first to be deleted when disk space runs low. A rating blacklists
        nothing — one star is easy to give by mistake. A low rating among an
        artist's songs this user rates well on average is left alone."""
        import glob
        try:
            conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
            artist_avg = dict(conn.execute('SELECT artist, AVG(rating) FROM track_scores GROUP BY artist').fetchall())
            mine = conn.execute('SELECT track_id, title, artist FROM track_scores '
                                'WHERE rating > 0 AND rating <= 2.0').fetchall()
            conn.close()
            if not mine:
                return
            # The same Plex item has the same key in every user's database.
            dislikes = {}
            for db in glob.glob(os.path.join(os.environ.get('DATA_DIR', '/app/data'), '*', 'music_ai.db')):
                try:
                    other = sqlite3.connect(db)
                    for (track_id,) in other.execute('SELECT track_id FROM track_scores WHERE rating > 0 AND rating <= 2.0'):
                        dislikes[track_id] = dislikes.get(track_id, 0) + 1
                    other.close()
                except sqlite3.Error as exc:
                    logger.debug(f"Could not read ratings from {db}: {exc}")
            set_to_go = 0
            for track_id, title, artist in mine:
                average = artist_avg.get(artist)
                if average and float(average) >= 4.0:
                    continue
                if dislikes.get(track_id, 0) >= 2:
                    self._set_track_to_go(track_id, title, artist)
                    set_to_go += 1
            if set_to_go:
                logger.info(f"✓ {set_to_go} song(s) two users rated one star are on the soon-to-go list")
        except Exception as e:
            logger.error(f"Error checking disliked songs: {e}")

    def _set_track_to_go(self, track_id, title, artist):
        """Two users rated the song one star: it goes on the soon-to-go list."""
        for path in self._paths_of_track(track_id):
            self._set_to_go(path, f"disliked by every user: {artist} - {title}")

    
    def disliked_artists(self) -> set:
        """Artists this user clearly doesn't like (primary-artist keys): one of
        their songs rated DISLIKED_RATING (1★) or less and none LIKED_RATING
        (3★) or more, or UNPLAYED_ARTIST_SONGS songs here for
        UNPLAYED_ARTIST_DAYS days or more and none ever played. Picks the user
        never asked for (trending charts) leave them out."""
        from lidarr_catalog import compact
        from metadata_repair import primary_artist
        try:
            conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
            rows = conn.execute('SELECT artist, rating, view_count, date_added FROM track_scores').fetchall()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Could not read the scores for disliked artists: {e}")
            return set()
        old = time.time() - UNPLAYED_ARTIST_DAYS * 86400
        stats: Dict[str, Dict[str, int]] = {}
        for artist, rating, plays, added in rows:
            if not artist or artist.startswith('<'):
                continue                    # a Plex object's text ("<Artist:…>"), not a name
            key = compact(primary_artist(artist))
            s = stats.setdefault(key, {'low': 0, 'high': 0, 'played': 0, 'old_unplayed': 0})
            rating = rating or 0
            s['low'] += 0 < rating <= DISLIKED_RATING
            s['high'] += rating >= LIKED_RATING
            s['played'] += (plays or 0) > 0
            s['old_unplayed'] += not plays and bool(added) and added <= old
        return {key for key, s in stats.items() if key and not s['high'] and
                (s['low'] or (not s['played'] and s['old_unplayed'] >= UNPLAYED_ARTIST_SONGS))}

    def without_disliked_artists(self, tracks: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
        from lidarr_catalog import compact
        from metadata_repair import primary_artist
        disliked = self.disliked_artists()
        kept = [t for t in tracks if compact(primary_artist(t.get('artist') or '')) not in disliked]
        skipped = sorted({t.get('artist') for t in tracks} - {t.get('artist') for t in kept})
        if skipped:
            logger.info(f"⏭️ {source}: skipping {len(tracks) - len(kept)} song(s) by artists this user "
                        f"doesn't like: {', '.join(map(str, skipped[:10]))}")
        return kept

    def find_albums_to_complete(self) -> List[Dict[str, Any]]:
        """Albums this user clearly loves that the library holds only part of,
        with the songs it is missing: [{'artist', 'album', 'missing', 'loved'}].

        An album qualifies with ALBUM_COMPLETION_MIN_LOVED songs scoring
        ALBUM_COMPLETION_SCORE or rated 4★ or more. Its real track list comes
        from Deezer, from the release whose tracks include most of the songs
        already here (so "Food & Liquor II" is not taken for "Food & Liquor",
        and the standard edition is preferred to a deluxe one). Compilations,
        and albums missing more than ALBUM_COMPLETION_MAX_TRACKS songs, are
        left alone. At most ALBUM_COMPLETION_MAX_ALBUMS albums per cycle.
        """
        if not self.plex:
            return []
        try:
            conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
            rows = conn.execute(
                'SELECT album_key, COUNT(*) FROM track_scores '
                'WHERE album_key IS NOT NULL AND (score >= ? OR rating >= 8) '
                'GROUP BY album_key HAVING COUNT(*) >= ? ORDER BY COUNT(*) DESC',
                (ALBUM_COMPLETION_SCORE, ALBUM_COMPLETION_MIN_LOVED)).fetchall()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Could not read the scores for album completion: {e}")
            return []

        from lidarr_catalog import compact
        albums = []
        for album_key, loved in rows:
            if len(albums) >= ALBUM_COMPLETION_MAX_ALBUMS:
                break
            try:
                album = self.plex.fetchItem(album_key)
                present = [t.title for t in album.tracks()]
            except Exception as e:
                logger.debug(f"Album {album_key} is not in Plex any more: {e}")
                continue
            artist = album.parentTitle or ''
            if compact(artist) in ('variousartists', 'va', 'various'):
                continue
            tracklist = _deezer_album_tracklist(artist, album.title, present)
            if not tracklist:
                logger.info(f"📀 '{artist} - {album.title}': no matching release found, not completing it")
                continue
            have = {_song_identity(t) for t in present}
            missing = [t for t in tracklist if _song_identity(t) not in have]
            if not missing:
                continue
            if len(missing) > ALBUM_COMPLETION_MAX_TRACKS:
                logger.info(f"📀 '{artist} - {album.title}' would need {len(missing)} songs; left alone")
                continue
            logger.info(f"📀 Completing '{artist} - {album.title}': {loved} loved, {len(present)} here, "
                        f"{len(missing)} missing")
            albums.append({'artist': artist, 'album': album.title, 'missing': missing, 'loved': loved})
        return albums

    def download_recommended_music(self, recommendations, preference_confidence=1.0,  target_dir=None,
                                   playlist=None, limit=10):
        """Download recommended music using a smart fallback pipeline with metadata verification.

        For each recommended artist:
        1. Check if artist already exists in Plex (skip duplicates)
        2. Query each tool (deemix, spotdl, yt-dlp) for available tracks via metadata search
        3. Verify matches against MusicBrainz using musicbrainzngs
        4. Only download files when a confident match is found
        5. Score metadata richness and select the best result
        6. Fall back to next tool if no match is found
        
        Args:
            recommendations: List of recommendation dicts
            preference_confidence: Confidence level in user's music preference (0-1.0)
            playlist: 'trending' or 'similar': the cycle playlist (CYCLE_PLAYLISTS)
                the downloads go in at the end of the cycle; None for none
        """
        if not recommendations:
            logger.info("❌ No recommendations to download")
            return []

        logger.info(f"📥 Starting download of {len(recommendations)} recommendation(s) (confidence: {preference_confidence:.0%})")
        
        # Without a target directory these are MusicAI's own picks: they land
        # in albums/ like everything else, on probation (library_state) until
        # the library review graduates or deletes them.
        on_probation = not target_dir
        download_dir = target_dir if target_dir else TEMP_DOWNLOAD_DIR
        os.makedirs(download_dir, exist_ok=True)
        results = []

        try:
            from downloader_manager import get_downloader
        except Exception as e:
            logger.error(f"Unable to import downloader_manager: {e}")
            return []

        downloader = get_downloader(self)
        if downloader is None:
            logger.error("Unable to obtain downloader instance from downloader_manager")
            return []
        try:
            user_index = int(APP_USER_INDEX)
        except Exception:
            user_index = 0

        for idx, rec in enumerate(recommendations[:limit]):
            artist = rec.get('artist', 'Unknown')
            title = rec.get('title', '')
            album = rec.get('album')
            reason = rec.get('reason', 'Similar artist')
            confidence = rec.get('confidence', 0.5)
            rec_type = rec.get('type', 'artist')  # 'artist' or 'album' or 'song'

            logger.info(
                f"🔄 Processing recommendation [{idx+1}/{min(len(recommendations), limit)}]: "
                f"{artist} - {title} (type: {rec_type}, confidence: {confidence:.2f}, reason: {reason})"
            )

            if confidence < preference_confidence and preference_confidence > 0.85:
                logger.info(f"⏭️ Skipping {artist} - {title}: Confidence {confidence:.2f} below threshold {preference_confidence:.2f}")
                continue
            
            # Check if track is blacklisted (prevents re-downloading 1-star or deleted tracks)
            if title and self.is_blacklisted(artist, title):
                logger.info(f"⏭️ Skipping {artist} - {title}: Track is blacklisted (previously deleted)")
                continue

            if title and (rec_type == 'song' or 'trending' in reason.lower() or 'billboard' in reason.lower()):
                track_exists, existing_path, similarity = check_if_track_exists(artist, title)
                if track_exists and similarity >= 0.90:
                    logger.info(f"⏭️ Skipping {artist} - {title}: Already exists in library (similarity: {similarity:.0%})")
                    continue

            if rec_type == 'album':
                search_query = album or artist
            elif title and (rec_type == 'song' or 'trending' in reason.lower() or 'billboard' in reason.lower()):
                search_query = f"{artist} - {title}"
            else:
                search_query = artist

            logger.info(f"🎵 Attempting download for {rec_type}: {search_query}")

            result = downloader.download_track(
                artist=artist,
                title=title,
                album=album,
                url=rec.get('url'),
                request_type=rec_type,
                target_dir=download_dir,
                user_index=user_index,
                request_context=rec.get('request_context'),
            )

            if not result or not result.get('success') or not result.get('file_path'):
                logger.info(f"❌ Download failed for {artist} - {title}: {result.get('error', 'unknown error') if isinstance(result, dict) else 'no result returned'}")
                continue

            file_path = result['file_path']
            tool_name = result.get('download_tool', result.get('tool', 'unknown'))

            if 'trending' in reason.lower() or 'billboard' in reason.lower():
                try:
                    file_path = save_trending_single(
                        self,
                        artist,
                        title if title else os.path.splitext(os.path.basename(file_path))[0],
                        file_path,
                        download_dir
                    )
                except Exception as e:
                    logger.debug(f"Error handling trending storage for {artist} - {title}: {e}")

            if on_probation:
                import library_state
                library_state.mark(file_path, library_state.PROBATION)

            results.append({
                'artist': artist,
                'file_path': file_path,
                'reason': reason,
                'confidence': confidence,
                'tool': tool_name,
                'score': result.get('score', 0)
            })
            if playlist:
                self.cycle_downloads.append({'file_path': file_path, 'artist': artist, 'title': title,
                                             'playlist': playlist})

        # Persist results to DB
        conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
        cursor = conn.cursor()
        for dl in results:
            # Track if this is trending music
            is_trending = 'trending' in dl['reason'].lower()
            cursor.execute('''
                INSERT INTO temp_downloads (artist, track_title, file_path, reason, confidence, status, is_trending)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                dl['artist'],
                os.path.basename(dl['file_path']),
                dl['file_path'],
                dl['reason'],
                dl['confidence'],
                'temporary',
                is_trending
            ))
        conn.commit()
        conn.close()

        logger.info(f"Downloaded and tracked {len(results)} new tracks")
        # The cycle playlists are filled once the cycle ends
        # (fill_cycle_playlists), when the files have settled.
        return results
    
    
    def _cycle_playlists(self, kind):
        """Every copy of this user's cycle playlist of `kind` ('trending' or
        'similar'), under its name or an alias; a failed run can leave more
        than one."""
        names = {n.strip().casefold() for n in (CYCLE_PLAYLISTS[kind],) + PLAYLIST_ALIASES.get(kind, ())}
        return [p for p in self.plex.playlists()
                if p.title.strip().casefold() in names and getattr(p, 'playlistType', 'audio') == 'audio'
                and not getattr(p, 'smart', False)]

    def _cycle_playlist(self, kind):
        """The one cycle playlist of `kind` kept (under its current name, extra
        copies deleted). Emptied rather than recreated, so a pin in Plexamp
        keeps working."""
        name = CYCLE_PLAYLISTS[kind]
        playlists = sorted(self._cycle_playlists(kind), key=lambda p: p.title.strip() != name)
        for extra in playlists[1:]:
            extra.delete()
            logger.info(f"🗑️  Deleted a duplicate '{name}' playlist")
        playlist = playlists[0] if playlists else None
        if playlist is not None and playlist.title.strip() != name:
            logger.info(f"✏️  Renamed '{playlist.title}' to '{name}'")
            playlist.editTitle(name)
        return playlist

    def start_cycle_playlists(self):
        """A new cycle: empty this user's cycle playlists and start counting
        the songs the cycle downloads (they go in at the end)."""
        self.cycle_downloads = []
        self.cycle_started_at = datetime.now()
        if not self.plex:
            return
        for kind, name in CYCLE_PLAYLISTS.items():
            try:
                playlist = self._cycle_playlist(kind)
                if playlist is not None:
                    items = playlist.items()
                    if items:
                        playlist.removeItems(items)
                    logger.info(f"🧹 Emptied '{name}' ({len(items)} song(s) from the last cycle)")
            except Exception as e:
                logger.warning(f"Could not empty '{name}': {e}")

    def remove_cycle_playlists(self, kinds=None):
        """No new songs of a kind for this user this time (no cycle, or nothing
        found): that playlist goes, so it never shows an older cycle's songs."""
        if not self.plex:
            return
        for kind in kinds or CYCLE_PLAYLISTS:
            try:
                for playlist in self._cycle_playlists(kind):
                    playlist.delete()
                    logger.info(f"🗑️  Deleted '{playlist.title}': no new songs for it this cycle")
            except Exception as e:
                logger.warning(f"Could not delete '{CYCLE_PLAYLISTS[kind]}': {e}")

    def _music_section_for_scans(self):
        """The music library as the server owner sees it: Plex refuses a
        library scan from a Home user's token (403), so a user's cycle that
        asked with its own connection would wait for a scan that never ran."""
        import plex_auth
        from plexapi.server import PlexServer
        return PlexServer(PLEX_URL, plex_auth.admin_token()).library.section(PLEX_MUSIC_SECTION)

    def _wait_for_plex_scan(self, section, timeout: int = 900) -> None:
        deadline = time.time() + timeout
        time.sleep(10)
        while time.time() < deadline:
            try:
                section.reload()
                if not section.refreshing:
                    return
            except Exception:
                return
            time.sleep(10)

    def _plex_track_for(self, section, download, by_path):
        """The Plex track of a song the cycle downloaded: by its file, or —
        when a later step moved or renamed the file — by artist and title
        among the tracks Plex added during this cycle."""
        path = download.get('file_path') or ''
        if path in by_path:
            return by_path[path]
        title = download.get('title') or ''
        if not title:
            return None
        try:
            from metadata_repair import artists_agree, titles_agree
            for track in section.searchTracks(title=title):
                added = getattr(track, 'addedAt', None)
                if (self.cycle_started_at is None or (added and added >= self.cycle_started_at)) \
                        and titles_agree(track.title, title) \
                        and artists_agree(track.originalTitle or track.grandparentTitle, download.get('artist')):
                    return track
        except Exception as e:
            logger.debug(f"Plex lookup failed for {download.get('artist')} - {title}: {e}")
        return None

    def fill_cycle_playlists(self):
        """Put the songs this cycle downloaded into the user's cycle playlists
        (emptied when the cycle began), once Plex has scanned them: trending
        songs in TRENDING_PLAYLIST, songs picked from their taste in
        SIMILAR_PLAYLIST. A playlist with no songs this cycle is removed."""
        if not self.plex:
            return
        if not self.cycle_downloads:
            logger.info("No new songs this cycle for the cycle playlists")
            self.remove_cycle_playlists()
            return
        try:
            section = self.plex.library.section(PLEX_MUSIC_SECTION)
            try:
                scanning = self._music_section_for_scans()
                scanning.update()
                self._wait_for_plex_scan(scanning)
            except Exception as e:
                logger.warning(f"Plex scan request failed: {e}")
            by_path = {}
            for track in section.searchTracks(sort='addedAt:desc', maxresults=max(500, 4 * len(self.cycle_downloads))):
                for media in track.media:
                    for part in media.parts:
                        by_path[convert_host_path_to_container_path(part.file)] = track
        except Exception as e:
            logger.warning(f"Could not read this cycle's songs from Plex: {e}")
            return
        for kind, name in CYCLE_PLAYLISTS.items():
            downloads = [d for d in self.cycle_downloads if d.get('playlist') == kind]
            try:
                self._fill_cycle_playlist(kind, name, section, downloads, by_path)
            except Exception as e:
                logger.warning(f"Could not fill '{name}': {e}")

    def _fill_cycle_playlist(self, kind, name, section, downloads, by_path):
        found, seen, missing = [], set(), []
        for download in downloads:
            track = self._plex_track_for(section, download, by_path)
            if track is None:
                missing.append(f"{download.get('artist')} - {download.get('title')}")
            elif track.ratingKey not in seen:
                seen.add(track.ratingKey)
                found.append(track)
        if not found:
            logger.info(f"No new songs for '{name}' this cycle" +
                        (f" ({len(downloads)} download(s) not in Plex yet)" if downloads else ""))
            self.remove_cycle_playlists([kind])
            return
        playlist = self._cycle_playlist(kind)
        if playlist is None:
            self.plex.createPlaylist(name, items=found)
        else:
            # Exactly this cycle's songs, even if emptying it at the start failed.
            wanted = {t.ratingKey for t in found}
            items = playlist.items()
            stale = [item for item in items if item.ratingKey not in wanted]
            if stale:
                playlist.removeItems(stale)
            have = {item.ratingKey for item in items}
            new = [t for t in found if t.ratingKey not in have]
            if new:
                playlist.addItems(new)
        logger.info(f"✓ '{name}' now holds this cycle's {len(found)} new song(s)"
                    + (f"; not in Plex: {', '.join(missing[:5])}" if missing else ""))

    def refresh_soon_to_go_playlist(self):
        """This user's "Soon to go" playlist holds exactly the songs on the
        soon-to-go list. A song on it the user rates SOON_TO_GO_RESCUE_RATING
        (4★) or more is rescued: it comes off the list and stays."""
        import library_state
        if not self.plex:
            return
        try:
            by_path = self._plex_tracks_by_path()
            tracks = []
            for path, _since in library_state.songs(library_state.SOON_TO_GO):
                track = by_path.get(os.path.normpath(path))
                if track is None:
                    continue
                rating = getattr(track, 'userRating', 0) or 0
                if rating >= SOON_TO_GO_RESCUE_RATING:
                    library_state.unmark(path, library_state.SOON_TO_GO)
                    logger.info(f"🛟 Rescued (rated {rating / 2:.1f}★): {_in_library(path)}")
                    continue
                tracks.append(track)
            playlist = next((p for p in self.plex.playlists() if p.title == SOON_TO_GO_PLAYLIST
                             and getattr(p, 'playlistType', 'audio') == 'audio' and not getattr(p, 'smart', False)), None)
            if playlist is None:
                if tracks:
                    self.plex.createPlaylist(SOON_TO_GO_PLAYLIST, items=tracks)
                    logger.info(f"Created '{SOON_TO_GO_PLAYLIST}' with {len(tracks)} song(s)")
                return
            current = {item.ratingKey: item for item in playlist.items()}
            wanted = {t.ratingKey for t in tracks}
            gone = [item for key, item in current.items() if key not in wanted]
            added = [t for t in tracks if t.ratingKey not in current]
            if gone:
                playlist.removeItems(gone)
            if added:
                playlist.addItems(added)
            if gone or added:
                logger.info(f"'{SOON_TO_GO_PLAYLIST}': {len(added)} added, {len(gone)} removed, {len(tracks)} in all")
        except Exception as e:
            logger.warning(f"Could not refresh '{SOON_TO_GO_PLAYLIST}': {e}")

    

    def is_blacklisted(self, artist, title):
        """True if the track is on this user's blacklist (a song the library
        review deleted; ratings blacklist nothing)."""
        try:
            # Normalize the artist name if it's a Plex object representation
            if artist and artist.startswith('<Artist:'):
                try:
                    artist = artist.split(':')[-1].rstrip('>')
                except:
                    pass
            
            def normalize(value):
                # Letters of every script: a Cyrillic or Arabic name must not
                # collapse to an empty string that matches every other one.
                value = str(value or '').casefold().strip()
                value = re.sub(r'[\W_]+', ' ', value)
                return ' '.join(value.split())

            artist_normalized = normalize(artist)
            title_normalized = normalize(title)
            conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
            cursor = conn.cursor()

            if not title_normalized:
                return False
            cursor.execute('SELECT title, artist FROM blacklist')
            result = next(
                (
                    row for row in cursor.fetchall()
                    if normalize(row[0]) == title_normalized and normalize(row[1]) == artist_normalized
                ),
                None,
            )
            conn.close()
            
            return result is not None
        except Exception as e:
            logger.debug(f"Error checking blacklist: {e}")
            return False

    def fix_all_music_metadata(self):
        """
        Comprehensive metadata fixing for every music file in the library.
        Ensures every file has proper artist/title/album tags extracted from filename if needed.
        This prevents Plex from showing "Various Artists" for files with incomplete metadata.
        """
        
        fixed_count = 0
        supported_ext = ('.mp3', '.flac', '.m4a', '.opus', '.ogg', '.wma', '.wav')
        
        scan_dirs = [(ALBUM_DIR, "Albums")] if os.path.isdir(ALBUM_DIR) else []
        
        if not scan_dirs:
            logger.warning("No music directories found to scan for metadata fixes")
            return 0
        
        logger.info(f"🔧 Scanning {len(scan_dirs)} directories for metadata fixes")
        
        for scan_dir, dir_type in scan_dirs:
            logger.debug(f"Scanning {dir_type}: {scan_dir}")
            
            for root, dirs, files in os.walk(scan_dir):
                for filename in files:
                    if not filename.lower().endswith(supported_ext):
                        continue
                    
                    file_path = os.path.join(root, filename)
                    try:
                        # Read metadata
                        audio = MutagenFile(file_path, easy=True)
                        
                        # Check what metadata is missing
                        has_artist = False
                        has_title = False
                        has_album = False
                        current_artist = ""
                        current_title = ""
                        
                        if audio and audio.tags:
                            has_artist = bool(audio.tags.get('artist', [None])[0]) if audio.tags.get('artist') else False
                            has_title = bool(audio.tags.get('title', [None])[0]) if audio.tags.get('title') else False
                            has_album = bool(audio.tags.get('album', [None])[0]) if audio.tags.get('album') else False
                            
                            if has_artist:
                                current_artist = audio.tags.get('artist', [None])[0]
                            if has_title:
                                current_title = audio.tags.get('title', [None])[0]
                        
                        # If metadata is missing or incomplete, extract from filename
                        if not has_artist or not has_title or not has_album:
                            # Extract from filename: "Artist - Title.ext"
                            name_without_ext = os.path.splitext(filename)[0]
                            
                            # Handle patterns like "Artist - Title_SubTitle.mp3" → use "Artist - Title"
                            if '_' in name_without_ext and ' - ' in name_without_ext:
                                base_name = name_without_ext.split('_')[0]
                            else:
                                base_name = name_without_ext
                            
                            artist_from_filename = ""
                            title_from_filename = ""
                            
                            if ' - ' in base_name:
                                parts = base_name.split(' - ', 1)
                                artist_from_filename = parts[0].strip()
                                title_from_filename = parts[1].strip() if len(parts) > 1 else ''
                            else:
                                # No " - " separator, try to use first part as artist
                                artist_from_filename = base_name.strip()
                                title_from_filename = current_title if has_title else ""
                            
                            # Only update if we have extracted values and metadata was actually missing
                            if (artist_from_filename or title_from_filename) and (not has_artist or not has_title):
                                try:
                                    # Use EasyID3 for robust ID3 tag handling
                                    audio = EasyID3(file_path)
                                    modified = False
                                    
                                    if not has_artist and artist_from_filename:
                                        try:
                                            audio['artist'] = [artist_from_filename]
                                        except:
                                            audio['artist'] = artist_from_filename
                                        modified = True
                                    
                                    if not has_title and title_from_filename:
                                        try:
                                            audio['title'] = [title_from_filename]
                                        except:
                                            audio['title'] = title_from_filename
                                        modified = True
                                    
                                    if not has_album and artist_from_filename:
                                        try:
                                            audio['album'] = [artist_from_filename]
                                        except:
                                            audio['album'] = artist_from_filename
                                        modified = True
                                    
                                    if modified:
                                        audio.save()
                                        fixed_count += 1
                                        logger.debug(f"✅ Fixed metadata: {filename} → artist: {artist_from_filename}, title: {title_from_filename}")
                                
                                except Exception as e:
                                    logger.debug(f"Could not fix metadata for {filename}: {type(e).__name__}")
                    
                    except Exception as e:
                        logger.debug(f"Error reading {file_path}: {type(e).__name__}")
        
        if fixed_count > 0:
            logger.info(f"✓ Fixed metadata for {fixed_count} file(s)")
        return fixed_count


    def calculate_preference_confidence(self, analyzed_tracks):
        """Calculate confidence in user music preference based on high-engagement tracks.
        
        Returns:
            float: Confidence value between 0 and 1.0
                - 0 to 0.5: Not enough data
                - 0.5 to 0.95: Building confidence with analyzed tracks
                - 0.95+: High confidence, ready for aggressive recommendations
        """
        if not analyzed_tracks:
            return 0.0
        
        total_tracks = len(analyzed_tracks)
        
        # Need minimum tracks to establish confidence
        if total_tracks < MIN_TRACKS_FOR_CONFIDENCE:
            confidence = min(0.5, (total_tracks / MIN_TRACKS_FOR_CONFIDENCE) * 0.5)
            logger.info(f"Building user profile: {total_tracks}/{MIN_TRACKS_FOR_CONFIDENCE} tracks ({confidence*100:.1f}% confidence)")
            return confidence
        
        try:
            conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
            cursor = conn.cursor()
            
            # Get high-engagement tracks count
            cursor.execute("SELECT COUNT(*) FROM track_scores WHERE score >= ?", (SCORE_THRESHOLD,))
            high_engagement_count = cursor.fetchone()[0]
            
            # Get average score
            cursor.execute("SELECT AVG(score) FROM track_scores")
            avg_score = cursor.fetchone()[0] or 0
            
            # Get score variance (how consistent likes/dislikes are)
            cursor.execute("SELECT COUNT(*) FROM track_scores WHERE score >= 0.7")
            very_high_count = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM track_scores WHERE score < 0.2")
            very_low_count = cursor.fetchone()[0]
            
            conn.close()
            
            # Calculate confidence:
            # 1. Base on high-engagement track percentage (40% weight)
            high_engagement_ratio = min(1.0, high_engagement_count / max(total_tracks, 1))
            engagement_confidence = high_engagement_ratio * 0.4
            
            # 2. Base on average score (30% weight) - higher avg = better understanding
            score_confidence = (avg_score / 1.0) * 0.3
            
            # 3. Base on preference consistency (30% weight) - clear likes/dislikes
            consistency_ratio = min(1.0, (very_high_count + very_low_count) / max(total_tracks, 1))
            consistency_confidence = consistency_ratio * 0.3
            
            total_confidence = engagement_confidence + score_confidence + consistency_confidence
            final_confidence = min(0.99, total_confidence)

            # For large libraries, allow a modest confidence boost when there is
            # clear engagement and average score activity even if the raw signal
            # is spread over many tracks.
            if total_tracks >= 5000 and avg_score >= 0.20 and high_engagement_count >= 100:
                final_confidence = max(final_confidence, 0.55)
            elif total_tracks >= 2000 and avg_score >= 0.25 and high_engagement_count >= 50:
                final_confidence = max(final_confidence, 0.50)

            logger.info(
                f"Preference confidence: {final_confidence*100:.1f}% "
                f"(High-engagement: {high_engagement_ratio*100:.1f}%, "
                f"Avg score: {avg_score:.2f}, "
                f"Consistency: {consistency_ratio*100:.1f}%)"
            )
            return final_confidence

        except Exception as e:
            logger.warning(f"Error calculating preference confidence: {e}")
            return 0.5

    def get_stats(self):
        """Get database statistics."""
        conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM track_scores")
        total_tracks = cursor.fetchone()[0]
        
        cursor.execute("SELECT AVG(score) FROM track_scores")
        avg_score = cursor.fetchone()[0] or 0
        
        cursor.execute("SELECT COUNT(*) FROM recommendations WHERE status='pending'")
        pending_recs = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM deletions")
        total_deleted = cursor.fetchone()[0]
        
        conn.close()
        
        stats = {
            'total_tracks': total_tracks,
            'avg_score': round(avg_score, 2),
            'pending_recommendations': pending_recs,
            'total_archived': total_deleted,
            'timestamp': datetime.now().isoformat()
        }
        
        logger.info(f"Stats: {json.dumps(stats, indent=2)}")
        return stats

def download_from_exportify(exportify_dir=None, album_dir=None):
    """
    Download all songs from CSV files in exportify directory and organize them
    in album_dir by artist/album structure.
    
    Ex. python /app/app.py download-exportify
    
    Args:
        exportify_dir: Path to directory containing exported Spotify playlist CSV files
                      (defaults to EXPORTIFY_DIR env var or /music/exportify)
        album_dir: Target directory to organize downloaded files (artist/album structure)
                  (defaults to ALBUM_DIR env var or /music/albums)
    """
    # Use environment variables if not provided
    if exportify_dir is None:
        exportify_dir = EXPORTIFY_DIR
    if album_dir is None:
        album_dir = ALBUM_DIR
    
    logger.info(f"Starting download from exportify directory: {exportify_dir}")
    logger.info(f"Target album directory: {album_dir}")
    
    if not os.path.isdir(exportify_dir):
        logger.error(f"Exportify directory not found: {exportify_dir}")
        return
    
    if not os.path.isdir(album_dir):
        logger.error(f"Album directory not found: {album_dir}")
        return
    
    # Find all CSV files
    csv_files = [f for f in os.listdir(exportify_dir) if f.endswith('.csv')]
    if not csv_files:
        logger.warning(f"No CSV files found in {exportify_dir}")
        return
    
    logger.info(f"Found {len(csv_files)} CSV files to process")
    
    total_tracks = 0
    downloaded_count = 0
    failed_count = 0
    skipped_count = 0
    
    # Track already processed songs to avoid duplicates
    processed_songs = set()
    
    for csv_file in csv_files:
        csv_path = os.path.join(exportify_dir, csv_file)
        logger.info(f"Processing CSV file: {csv_file}")
        
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                
                if reader.fieldnames is None:
                    logger.warning(f"Empty or invalid CSV file: {csv_file}")
                    continue
                
                for row in reader:
                    total_tracks += 1
                    
                    try:
                        track_name = row.get('Track Name', '').strip()
                        album_name = row.get('Album Name', '').strip()
                        artist_name = row.get('Artist Name(s)', '').strip()
                        
                        if not track_name or not artist_name:
                            logger.warning(f"Skipping row with missing track or artist name in {csv_file}")
                            skipped_count += 1
                            continue
                        
                        # Use artist/album/track as unique identifier
                        song_key = (artist_name, album_name, track_name)
                        if song_key in processed_songs:
                            logger.debug(f"Skipping duplicate: {artist_name} - {track_name}")
                            skipped_count += 1
                            continue
                        
                        processed_songs.add(song_key)
                        
                        # Parse artist field to get primary artist and featured artists
                        primary_artist, featured = parse_artist_field(artist_name)

                        # Create artist directory using primary artist only
                        artist_dir_name = sanitize_filename(primary_artist)
                        artist_dir = os.path.join(album_dir, artist_dir_name)
                        os.makedirs(artist_dir, exist_ok=True)

                        # Create album subdirectory (or use "Singles" if no album name)
                        if album_name:
                            album_subdir_name = sanitize_filename(album_name)
                            album_subdir = os.path.join(artist_dir, album_subdir_name)
                        else:
                            album_subdir = os.path.join(artist_dir, "Singles")
                        os.makedirs(album_subdir, exist_ok=True)

                        # Append featured artists to the track display title when present
                        if featured:
                            track_display_title = f"{track_name} (feat. {featured})"
                        else:
                            track_display_title = track_name

                        logger.info(f"Downloading: {primary_artist} - {track_display_title} (Album: {album_name or 'Unknown'})")
                        
                        # Try to download using the smart pipeline (query -> verify -> download)
                        success = _download_song_to_album(
                            artist=primary_artist,
                            track_name=track_display_title,
                            album_name=album_name or "Unknown",
                            target_dir=album_subdir,
                        )
                        
                        if success:
                            downloaded_count += 1
                            logger.info(f"Successfully downloaded: {artist_name} - {track_name}")
                        else:
                            failed_count += 1
                            logger.warning(f"Failed to download: {artist_name} - {track_name}")
                    
                    except Exception as e:
                        logger.error(f"Error processing row in {csv_file}: {e}")
                        failed_count += 1
        
        except Exception as e:
            logger.error(f"Error reading CSV file {csv_file}: {e}")
            continue
    
    # Summary
    logger.info("=" * 60)
    logger.info(f"Exportify Download Summary:")
    logger.info(f"  Total tracks processed: {total_tracks}")
    logger.info(f"  Successfully downloaded: {downloaded_count}")
    logger.info(f"  Failed downloads: {failed_count}")
    logger.info(f"  Skipped (duplicates/missing data): {skipped_count}")
    logger.info(f"  Success rate: {(downloaded_count/max(total_tracks-skipped_count, 1)*100):.1f}%")
    logger.info("=" * 60)

def _download_song_to_album(artist, track_name, album_name, target_dir, timeout=300):
    """
    Download a single song using the smart pipeline (query -> verify -> download).
    Also downloads and stores album cover image if available.
    
    Returns True if successful, False otherwise.
    """
    try:
        import musicbrainzngs
        
        # Step 1: Query metadata first (no full download)
        logger.debug(f"Querying for: {artist} - {track_name}")
        
        mb_recording = None
        mb_release = None
        
        # Try yt-dlp first
        try:
            search_query = f"{artist} {track_name}"
            cmd = ['yt-dlp', '--dump-json', '-x', f"ytsearch1:{search_query}"]
            proc = run_subprocess_with_limit(cmd, service_name='yt-dlp', timeout=30)
            if proc and proc.returncode == 0:
                try:
                    info = json.loads(proc.stdout)
                    logger.debug(f"Found on yt-dlp: {info.get('title', 'Unknown')}")
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"yt-dlp query failed: {e}")
        
        # Step 2: Verify against MusicBrainz and get release info for cover art
        logger.debug(f"Verifying against MusicBrainz: {artist} - {track_name}")
        try:
            musicbrainzngs.set_useragent("MusicAI", "1.0", "https://localhost")
            results = musicbrainzngs.search_recordings(query=f"{artist} {track_name}", limit=1)
            if results and results.get('recording-list'):
                recording = results['recording-list'][0]
                mb_recording = recording
                logger.info(f"MusicBrainz verified: {recording.get('title')} by {artist}")
                
                # Get release info for cover art
                if 'release-list' in recording:
                    release_id = recording['release-list'][0].get('id')
                    if release_id:
                        try:
                            mb_release = musicbrainzngs.get_release_by_id(release_id, includes=['coverart-front'])
                            logger.debug(f"Retrieved MusicBrainz release info for cover art")
                        except Exception as e:
                            logger.debug(f"Could not retrieve release info: {e}")
            else:
                logger.debug(f"MusicBrainz verification not found for: {artist} - {track_name}")
        except Exception as e:
            logger.debug(f"MusicBrainz lookup failed: {e}")
        
        # Step 3: Download the track
        logger.info(f"Downloading {artist} - {track_name} to {target_dir}")
        
        # Use yt-dlp for download
        output_template = os.path.join(target_dir, f"{track_name}.%(ext)s")
        cmd = [
            'yt-dlp',
            '-x',
            '--audio-format', 'mp3',
            '--audio-quality', '192',
            '-o', output_template,
            f"ytsearch1:{artist} {track_name} official"
        ]
        
        proc = run_subprocess_with_limit(cmd, service_name='yt-dlp', timeout=timeout)
        if not proc:
            logger.warning(f"yt-dlp did not return a process (rate-limited or failed)")
            return False

        if proc.returncode == 0:
            logger.info(f"Download successful: {artist} - {track_name}")
            
            # Find the downloaded file
            downloaded_file = None
            for ext in ['mp3', 'wav', 'm4a', 'flac']:
                potential_file = os.path.join(target_dir, f"{track_name}.{ext}")
                if os.path.exists(potential_file):
                    downloaded_file = potential_file
                    
                    # Add metadata tags using mutagen
                    try:
                        audio = EasyID3(potential_file)
                        audio['artist'] = artist
                        audio['title'] = track_name
                        audio['album'] = album_name
                        audio.save()
                        logger.debug(f"Added metadata tags to {potential_file}")
                    except Exception as e:
                        logger.debug(f"Could not add metadata tags: {e}")
                    
                    break
            
            # Step 4: Download and store album cover image
            _download_album_cover(artist, album_name, target_dir, mb_release)
            
            if downloaded_file:
                return True
            else:
                logger.warning(f"Download succeeded but file not found for: {artist} - {track_name}")
                return True  # File might be in unexpected format
        else:
            logger.warning(f"Download failed for {artist} - {track_name}: {proc.stderr}")
            return False
    
    except Exception as e:
        logger.error(f"Error downloading song {artist} - {track_name}: {e}")
        return False

def _get_metadata_from_sources(artist, album_name, track_name=None):
    """
    Comprehensive metadata lookup from multiple sources in priority order:
    1. Spotify (public web pages)
    2. Discogs (via discogs-client)
    3. LastFM (via pylast)
    4. MusicBrainz (via musicbrainzngs)
    5. YouTube (fallback thumbnail)

    Returns dict with keys: title, artist, album, year, genres, cover_url, source
    """
    metadata = {
        'title': track_name or 'Unknown',
        'artist': artist,
        'album': album_name,
        'year': None,
        'genres': [],
        'cover_url': None,
        'source': None
    }

    try:
        # 1. Spotify
        spotify_track = spotify_web.search_track(artist, track_name or album_name)
        if spotify_track and spotify_track.get('album'):
            metadata['album'] = spotify_track['album']
            metadata['cover_url'] = spotify_track.get('cover_url')
            metadata['source'] = 'spotify'
            logger.info(f"Found album metadata on Spotify: {spotify_track['album']}")
            return metadata

        # 2. Discogs
        if DISCOGS_TOKEN:
            try:
                from discogs_client import Client
                discogs_client = Client('MusicAI/1.0', user_token=DISCOGS_TOKEN)
                logger.debug(f"Searching Discogs for: {artist} - {album_name}")
                results = discogs_client.search(album_name, artist=artist, type='release')

                if results.count > 0:
                    release = results[0]
                    metadata['album'] = release.title
                    if getattr(release, 'year', None):
                        metadata['year'] = str(release.year)
                    if getattr(release, 'genres', None):
                        metadata['genres'] = release.genres[:3]
                    if getattr(release, 'images', None):
                        metadata['cover_url'] = release.images[0]['uri']
                    metadata['source'] = 'discogs'
                    logger.info(f"Found album metadata on Discogs: {release.title}")
                    return metadata
            except Exception as e:
                logger.debug(f"Discogs metadata lookup failed: {e}")

        # 3. LastFM
        if LAST_FM_API_KEY:
            try:
                import pylast
                network = pylast.LastFMNetwork(api_key=LAST_FM_API_KEY)
                logger.debug(f"Searching LastFM for: {artist} - {album_name}")
                album_obj = network.get_album(artist, album_name)
                metadata['album'] = album_obj.name
                if album_obj.get_cover_image():
                    metadata['cover_url'] = album_obj.get_cover_image()
                try:
                    release_date = album_obj.get_release_date()
                    if release_date:
                        metadata['year'] = str(release_date).split('-')[0]
                except Exception:
                    pass
                metadata['source'] = 'lastfm'
                logger.info(f"Found album metadata on LastFM: {album_obj.name}")
                return metadata
            except Exception as e:
                logger.debug(f"LastFM metadata lookup failed: {e}")

        # 4. MusicBrainz
        try:
            import musicbrainzngs
            musicbrainzngs.set_useragent("MusicAI", "1.0", "https://localhost")
            logger.debug(f"Searching MusicBrainz for: {artist} - {album_name}")
            results = musicbrainzngs.search_releases(query=f'{artist} {album_name}', limit=1)

            if results.get('release-list'):
                release = results['release-list'][0]
                metadata['album'] = release.get('title', album_name)
                if release.get('date'):
                    metadata['year'] = release['date'].split('-')[0]
                if release.get('id'):
                    try:
                        release_detail = musicbrainzngs.get_release_by_id(release['id'], includes=['coverart-front'])
                        if 'release' in release_detail and 'coverart-front' in release_detail['release']:
                            metadata['cover_url'] = release_detail['release']['coverart-front']
                    except Exception:
                        pass
                metadata['source'] = 'musicbrainz'
                logger.info(f"Found album metadata on MusicBrainz: {release.get('title')}")
                return metadata
        except Exception as e:
            logger.debug(f"MusicBrainz metadata lookup failed: {e}")

        # 5. YouTube fallback
        try:
            search_query = f"{artist} {album_name} album cover"
            cmd = ['yt-dlp', '--dump-json', '--no-warnings', f'ytsearch1:{search_query}']
            proc = run_subprocess_with_limit(cmd, service_name='yt-dlp', timeout=30)
            if proc and proc.returncode == 0:
                info = json.loads(proc.stdout)
                if info.get('thumbnail'):
                    metadata['cover_url'] = info['thumbnail']
                    metadata['source'] = 'youtube'
                    logger.info(f"Found cover art on YouTube for: {artist} - {album_name}")
                    return metadata
        except Exception as e:
            logger.debug(f"YouTube metadata lookup failed: {e}")

        logger.warning(f"No metadata found from any source for: {artist} - {album_name}")

    except Exception as e:
        logger.error(f"Error in metadata pipeline: {e}")
        return metadata

    # Sort by confidence and return top candidates
    return metadata


def _embed_cover_art(file_path, cover_url):
    """
    Download cover art from URL and embed it into the audio file's ID3 tags.
    
    Args:
        file_path: Path to audio file (MP3, M4A, FLAC)
        cover_url: URL to cover art image
    
    Returns:
        True if successful, False otherwise
    """
    try:
        from mutagen.id3 import ID3, APIC
        
        if not os.path.exists(file_path):
            logger.warning(f"File not found for cover art embedding: {file_path}")
            return False
        
        # Download the cover art
        try:
            response = requests_get_with_limit(cover_url, service_name='cover', timeout=15, allow_redirects=True)
            if not response or response.status_code != 200:
                logger.warning(f"Failed to download cover from {cover_url}")
                return False
            
            cover_data = response.content
            if not cover_data:
                logger.warning(f"Empty cover data from {cover_url}")
                return False
        
        except Exception as e:
            logger.warning(f"Error downloading cover art: {e}")
            return False
        
        # Embed cover into file based on format
        try:
            if file_path.endswith('.mp3'):
                # MP3 with ID3
                audio = MutagenFile(file_path)
                if audio is None or not hasattr(audio, 'tags'):
                    logger.warning(f"Cannot read ID3 tags from {file_path}")
                    return False
                
                # Remove existing APIC frames
                audio.tags.delall('APIC')
                
                # Add new cover art
                audio.tags.add(APIC(
                    encoding=3,  # UTF-8
                    mime='image/jpeg',
                    type=3,  # Front cover
                    desc='Cover',
                    data=cover_data
                ))
                audio.save()
                logger.debug(f"✓ Embedded cover art into {os.path.basename(file_path)} (MP3)")
                return True
            
            elif file_path.endswith('.m4a'):
                # M4A (iTunes)
                audio = MutagenFile(file_path)
                if audio is None:
                    logger.warning(f"Cannot read tags from {file_path}")
                    return False
                
                from mutagen.mp4 import MP4Cover
                audio.tags['covr'] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
                audio.save()
                logger.debug(f"✓ Embedded cover art into {os.path.basename(file_path)} (M4A)")
                return True
            
            elif file_path.endswith('.flac'):
                # FLAC
                audio = MutagenFile(file_path)
                if audio is None:
                    logger.warning(f"Cannot read FLAC tags from {file_path}")
                    return False
                
                from mutagen.flac import Picture
                picture = Picture()
                picture.data = cover_data
                picture.mime = 'image/jpeg'
                picture.type = 3  # Front cover
                picture.desc = 'Cover'
                
                audio.add_picture(picture)
                audio.save()
                logger.debug(f"✓ Embedded cover art into {os.path.basename(file_path)} (FLAC)")
                return True
            
            else:
                logger.debug(f"Unsupported format for cover embedding: {file_path}")
                return False
        
        except Exception as e:
            logger.warning(f"Error embedding cover art into {os.path.basename(file_path)}: {e}")
            return False
    
    except Exception as e:
        logger.error(f"Error in _embed_cover_art: {e}")
        return False


def _download_album_cover(artist, album_name, target_dir, mb_release=None, force_update=False):
    """
    Download and store album cover image in the album directory.
    Uses the metadata fallback pipeline to find cover art.
    Saves as 'cover.jpg' in the album directory.
    
    Args:
        artist: Artist name
        album_name: Album name
        target_dir: Directory to save cover to
        mb_release: ignored (kept so callers can pass what they have)
        force_update: If True, re-download even if cover exists
    """
    try:
        cover_path = os.path.join(target_dir, "cover.jpg")
        
        # Skip if cover already exists (unless force_update)
        if os.path.exists(cover_path) and not force_update:
            logger.debug(f"Cover already exists at {cover_path}")
            return True
        
        # Use metadata pipeline to find cover art
        logger.debug(f"Fetching cover art metadata for: {artist} - {album_name}")
        metadata = _get_metadata_from_sources(artist, album_name)
        
        if not metadata:
            logger.warning(f"Metadata pipeline returned None for {artist} - {album_name}")
            return False
        
        cover_url = metadata.get('cover_url')
        source = metadata.get('source', 'unknown')
        
        if not cover_url:
            logger.info(f"No cover URL found from any source ({source}) for {artist} - {album_name}")
            return False
        
        logger.debug(f"Attempting to download cover from {source}: {cover_url[:100]}...")
        
        try:
            response = requests_get_with_limit(cover_url, service_name='cover', timeout=15, allow_redirects=True)
            if not response:
                logger.warning(f"Failed to download cover from {cover_url}")
                return False
            if response.status_code == 200 and response.content:
                # Verify it's actually an image by checking content-type or magic bytes
                content_type = response.headers.get('content-type', '').lower()
                if 'image' not in content_type and not response.content.startswith((b'\xff\xd8', b'\x89\x50\x4e', b'\x47\x49\x46')):
                    # Not JPG, PNG, or GIF - but try anyway
                    logger.debug(f"Content-type is {content_type}, but attempting to save anyway")
                
                with open(cover_path, 'wb') as f:
                    f.write(response.content)
                logger.info(f"Successfully downloaded cover art from {source} ({len(response.content)} bytes) to {cover_path}")
                return True
            else:
                logger.warning(f"HTTP {response.status_code} downloading cover from {source}")
                return False
        
        except requests.Timeout:
            logger.warning(f"Timeout downloading cover from {source}")
            return False
        except Exception as e:
            logger.warning(f"Failed to download cover from {source}: {e}")
            return False
    
    except Exception as e:
        logger.error(f"Error in cover download for {artist} - {album_name}: {e}")


def save_trending_single(analyzer_self, artist, title, file_path, download_dir):
    """A trending download stays where the downloader filed it
    (albums/<Artist>/<Title>/); its folder gets cover art when it has none.
    Returns the file's path."""
    folder = os.path.dirname(file_path)
    if not any(os.path.exists(os.path.join(folder, n)) for n in ('cover.jpg', 'folder.jpg')):
        try:
            _download_album_cover(artist, title, folder, mb_release=None)
        except Exception as e:
            logger.debug(f"Could not download cover for trending track {artist} - {title}: {e}")
    return file_path


def _in_library(path):
    """A song's path inside albums/, for logs."""
    rel = os.path.relpath(path, ALBUM_DIR)
    return path if rel.startswith('..') else rel


def normalize_string(s):
    """Normalize string for comparison: lowercase, remove special chars."""
    s = s.lower().strip()
    # Remove special characters but keep spaces
    s = re.sub(r"[^a-z0-9\s]", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s

def calculate_string_similarity(s1, s2):
    """Calculate similarity score between two strings (0-1)."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, s1, s2).ratio()

def get_audio_fingerprint(file_path):
    """Generate a fingerprint of audio file: (duration, bitrate, sample_rate)."""
    try:
        audio = MutagenFile(file_path)
        if audio and hasattr(audio, 'info'):
            duration = getattr(audio.info, 'length', 0)
            bitrate = getattr(audio.info, 'bitrate', 0)
            sample_rate = getattr(audio.info, 'sample_rate', 0)
            return (round(duration), bitrate, sample_rate)
    except Exception as e:
        logger.debug(f"Could not get fingerprint for {file_path}: {e}")
    return None

def find_duplicate_tracks(directories, similarity_threshold=0.85):
    """
    Find duplicate tracks across multiple directories.
    
    Args:
        directories: List of directories to scan for audio files
        similarity_threshold: Similarity score required to consider as potential duplicate (0-1)
    
    Returns:
        List of duplicate groups, each group contains duplicate track info
    """
    
    # Scan all directories for audio files
    audio_files = {}
    file_tracks = {}  # Map file path to (artist, title, duration)
    
    for directory in directories:
        if not os.path.isdir(directory):
            logger.warning(f"Directory not found: {directory}")
            continue
        
        # Walk through directory structure: artist/album/track.mp3
        for root, dirs, files in os.walk(directory):
            for filename in files:
                if not filename.endswith(('.mp3', '.m4a', '.flac', '.opus', '.ogg', '.wav')):
                    continue
                
                file_path = os.path.join(root, filename)
                
                try:
                    audio = MutagenFile(file_path)
                    
                    if not audio:
                        continue
                    
                    # Extract metadata
                    title = None
                    artist = None
                    
                    # Try different metadata tag formats
                    if hasattr(audio, 'tags') and audio.tags:
                        title = audio.tags.get('TIT2', [None])[0] if 'TIT2' in audio.tags else None
                        artist = audio.tags.get('TPE1', [None])[0] if 'TPE1' in audio.tags else None
                        
                        # Fallback to common tag names
                        if not title:
                            title = audio.tags.get('TITLE', [None])[0] if 'TITLE' in audio.tags else None
                        if not artist:
                            artist = audio.tags.get('ARTIST', [None])[0] if 'ARTIST' in audio.tags else None
                    
                    # Fallback to filename if metadata missing
                    if not title:
                        title = os.path.splitext(filename)[0]
                    if not artist:
                        # Try to extract from directory structure
                        parts = file_path.split(os.sep)
                        if len(parts) >= 2:
                            artist = parts[-3]  # Assume artist/album/track.mp3 structure
                        else:
                            artist = "Unknown"
                    
                    # Get duration and other fingerprint data
                    duration = getattr(audio.info, 'length', 0) if hasattr(audio, 'info') else 0
                    fingerprint = get_audio_fingerprint(file_path)
                    
                    # Store track info
                    key = (normalize_string(artist), normalize_string(title))
                    if key not in audio_files:
                        audio_files[key] = []
                    
                    audio_files[key].append({
                        'path': file_path,
                        'artist': artist,
                        'title': title,
                        'duration': duration,
                        'fingerprint': fingerprint,
                        'filename': filename
                    })
                    file_tracks[file_path] = (artist, title, duration)
                
                except Exception as e:
                    logger.debug(f"Could not process {file_path}: {e}")
    
    # Find duplicates
    duplicates = []
    exact_match_groups = {}
    
    # First pass: Exact matches by fingerprint and metadata
    for (artist_norm, title_norm), files in audio_files.items():
        if len(files) < 2:
            continue
        
        # Group by fingerprint
        fingerprint_groups = {}
        for f in files:
            fp = f['fingerprint']
            if fp not in fingerprint_groups:
                fingerprint_groups[fp] = []
            fingerprint_groups[fp].append(f)
        
        # Add groups with multiple matches
        for fp, group in fingerprint_groups.items():
            if len(group) >= 2:
                exact_match_groups[(artist_norm, title_norm, fp)] = group
                duplicates.append({
                    'type': 'exact',
                    'similarity': 1.0,
                    'group': group
                })
    
    # Second pass: Fuzzy matches for similar artist/title combinations
    processed_pairs = set()
    all_keys = list(audio_files.keys())
    
    for i, (artist1, title1) in enumerate(all_keys):
        files1 = audio_files[(artist1, title1)]
        if len(files1) < 1:
            continue
        
        for j, (artist2, title2) in enumerate(all_keys[i+1:], start=i+1):
            files2 = audio_files[(artist2, title2)]
            if len(files2) < 1:
                continue
            
            pair_key = (min(i, j), max(i, j))
            if pair_key in processed_pairs:
                continue
            processed_pairs.add(pair_key)
            
            # Calculate similarity
            title_sim = calculate_string_similarity(title1, title2)
            artist_sim = calculate_string_similarity(artist1, artist2)
            
            # Combined similarity (weight title more heavily)
            combined_sim = (title_sim * 0.7) + (artist_sim * 0.3)
            
            if combined_sim >= similarity_threshold:
                # Check if duration is similar (within 5% or 5 seconds)
                for f1 in files1:
                    for f2 in files2:
                        if f1['path'] == f2['path']:
                            continue
                        
                        duration_diff = abs(f1['duration'] - f2['duration'])
                        duration_diff_pct = duration_diff / max(f1['duration'], f2['duration']) if max(f1['duration'], f2['duration']) > 0 else 0
                        
                        if duration_diff_pct <= 0.05 or duration_diff <= 5:
                            # Likely duplicate
                            group = [f1, f2]
                            
                            # Check if already in exact matches
                            already_exists = any(
                                set(dup['group']) == set(group) if dup['type'] == 'fuzzy' else False
                                for dup in duplicates
                            )
                            
                            if not already_exists:
                                duplicates.append({
                                    'type': 'fuzzy',
                                    'similarity': combined_sim,
                                    'title_similarity': title_sim,
                                    'artist_similarity': artist_sim,
                                    'group': group
                                })
    
    # Remove duplicate entries from duplicates list
    unique_duplicates = []
    seen_sets = set()
    for dup in duplicates:
        file_paths = tuple(sorted([f['path'] for f in dup['group']]))
        if file_paths not in seen_sets:
            seen_sets.add(file_paths)
            unique_duplicates.append(dup)
    
    return unique_duplicates

def check_if_track_exists(artist, title, search_dirs=None):
    """
    Check if a track already exists in the library before downloading.
    Prevents duplicate downloads.
    
    Args:
        artist: Artist name to search for
        title: Track title to search for
        search_dirs: List of directories to search (defaults to the library)
    
    Returns:
        Tuple of (exists: bool, existing_path: str or None, similarity_score: float)
    """
    try:
        if not search_dirs:
            search_dirs = list(dict.fromkeys([MUSIC_DIR, ALBUM_DIR]))
        
        # Normalize search terms
        search_artist_norm = normalize_string(artist)
        search_title_norm = normalize_string(title)
        
        logger.debug(f"Checking if track exists: {artist} - {title}")
        
        for directory in search_dirs:
            if not os.path.isdir(directory):
                continue
            
            for root, dirs, files in os.walk(directory):
                for filename in files:
                    if not filename.endswith(('.mp3', '.m4a', '.flac', '.opus', '.ogg', '.wav')):
                        continue
                    
                    file_path = os.path.join(root, filename)
                    
                    try:
                        audio = MutagenFile(file_path)
                        
                        if not audio or not hasattr(audio, 'tags'):
                            continue
                        
                        # Extract metadata from existing file
                        existing_artist = audio.tags.get('TPE1', [None])[0] if hasattr(audio.tags, 'get') and 'TPE1' in audio.tags else None
                        existing_title = audio.tags.get('TIT2', [None])[0] if hasattr(audio.tags, 'get') and 'TIT2' in audio.tags else None
                        
                        if not existing_artist or not existing_title:
                            # Try filename
                            existing_title = os.path.splitext(filename)[0]
                            existing_artist = os.path.basename(os.path.dirname(file_path))
                        
                        # Compare
                        existing_artist_norm = normalize_string(existing_artist)
                        existing_title_norm = normalize_string(existing_title)
                        
                        # Calculate similarity
                        title_sim = calculate_string_similarity(search_title_norm, existing_title_norm)
                        artist_sim = calculate_string_similarity(search_artist_norm, existing_artist_norm)
                        combined_sim = (title_sim * 0.7) + (artist_sim * 0.3)
                        
                        # If high similarity match, consider it a duplicate
                        if combined_sim >= 0.90:
                            logger.info(f"✓ Track exists: {existing_artist} - {existing_title} (similarity: {combined_sim:.0%})")
                            return (True, file_path, combined_sim)
                    
                    except Exception as e:
                        logger.debug(f"Error checking file {file_path}: {e}")
                        continue
        
        logger.debug(f"Track not found in library")
        return (False, None, 0.0)
    
    except Exception as e:
        logger.warning(f"Error checking if track exists: {e}")
        return (False, None, 0.0)

















def _push_cycle_status(phase: str, detail: str, stats: dict | None = None) -> None:
    """Send a progress update to the shared Discord cycle status embed."""
    if not DISCORD_ENABLED or not discord_bot:
        return
    try:
        asyncio.run(
            discord_bot.send_cycle_progress_update(APP_USER_NAME, phase, detail, stats)
        )
    except Exception as e:
        logger.debug(f"Could not update Discord cycle status: {e}")


def _should_skip_cycle(analyzed_tracks: list, preference_confidence: float) -> tuple[bool, str]:
    """Decide whether the current cycle should be skipped based on activity.

    CRITICAL: Only check USER-SPECIFIC activity signals.
    Do NOT check album_rating — it's shared across all users, not user-personal.

    Returns:
        (skip, reason)
    """
    track_count = len(analyzed_tracks)
    if track_count == 0:
        return True, "No Plex tracks available"

    def _has_positive_signal(value):
        return (value or 0) > 0

    # Require a recent Plex play. Historical view counts and ratings remain in
    # Plex indefinitely, so they must not make an inactive user eligible for a
    # new download cycle. Album ratings are shared metadata and are excluded.
    activity_cutoff = int(time.time()) - (USER_ACTIVITY_WINDOW_DAYS * 86400)
    user_activity_tracks = [
        t for t in analyzed_tracks
        if (
            _has_positive_signal(t.get('features', {}).get('last_played'))
            and t.get('features', {}).get('last_played', 0) >= activity_cutoff
        )
    ]

    if not user_activity_tracks:
        logger.info(
            f"User {PLEX_USERNAME or APP_USER_NAME} has no Plex activity in the last "
            f"{USER_ACTIVITY_WINDOW_DAYS} days"
        )
        return True, f"No Plex listening activity in the last {USER_ACTIVITY_WINDOW_DAYS} days"

    # Only skip when the library is small AND preference confidence is low.
    # Low confidence alone is not enough: a large library with many low-scoring
    # tracks legitimately produces a conservative confidence figure and should
    # still get a cycle.
    if track_count < MIN_TRACKS_FOR_CONFIDENCE and preference_confidence <= 0.5:
        return True, f"Only {track_count} track(s) analyzed, confidence {preference_confidence:.2f}"

    return False, ''



def _log_banner(text: str) -> None:
    logger.info("=" * 70)
    logger.info(text)
    logger.info("=" * 70)


def _record_stage_error(cycle_stats: dict, key: str, message: str) -> None:
    logger.warning(message)
    if key in cycle_stats['errors']:
        cycle_stats['errors'][key] = message
    cycle_stats['errors']['list'].append(message)


def _pending_discord_request_count() -> int:
    requests_db = os.path.join(os.environ.get('DATA_DIR', '/app/data'), 'discord_requests.json')
    try:
        if not os.path.exists(requests_db):
            return 0
        with open(requests_db, 'r') as f:
            all_requests = json.load(f)
        return sum(1 for reqs in all_requests.values() for req in reqs if req.get('status') == 'pending')
    except Exception as exc:
        logger.debug(f"Could not check for pending Discord requests: {exc}")
        return 0


def _process_pending_discord_requests(analyzer) -> None:
    """Handle queued Discord requests right away rather than at the next scheduled cycle."""
    try:
        pending = _pending_discord_request_count()
        if pending > 0:
            logger.info(f"🎵 PRIORITY: Processing {pending} pending Discord request(s)")
            processed = analyzer.process_discord_requests()
            if processed > 0:
                logger.info(f"✓ Processed {processed} Discord request(s) immediately")
    except Exception as exc:
        logger.warning(f"Error checking/processing Discord requests: {exc}")


def _canonical_cycle_user() -> str:
    return PLEX_USERNAME.strip() if PLEX_USERNAME and PLEX_USERNAME.strip() else APP_USER_NAME


def _cycle_running_file_path() -> str:
    return os.path.join(_ensure_shared_data_dir(), f'cycle_running_{_canonical_cycle_user()}.json')


def _write_cycle_running_file(cycle_start_time) -> None:
    """Signal discord_bot that this user's cycle is underway."""
    try:
        with open(_cycle_running_file_path(), 'w') as f:
            json.dump({
                'user': _canonical_cycle_user(),
                'start_time': cycle_start_time.isoformat(),
                'pid': os.getpid(),
                'sections': [PLEX_MUSIC_SECTION],
            }, f)
        _remove_cycle_skip_file()
        if DISCORD_ENABLED and discord_bot:
            try:
                asyncio.run(discord_bot.send_cycle_start(APP_USER_NAME, [PLEX_MUSIC_SECTION]))
            except Exception as exc:
                logger.debug(f"Could not send initial Discord cycle start update: {exc}")
    except Exception as exc:
        logger.warning(f"Could not write cycle_running file: {exc}")


def _remove_cycle_running_file() -> None:
    try:
        path = _cycle_running_file_path()
        if os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        logger.warning(f"Could not remove cycle_running file: {exc}")


def _write_cycle_complete_file(total_downloaded: int) -> None:
    try:
        path = os.path.join(_ensure_shared_data_dir(), f'cycle_complete_{_canonical_cycle_user()}.json')
        with open(path, 'w') as f:
            json.dump({
                'user': _canonical_cycle_user(),
                'end_time': datetime.now().isoformat(),
                'total_downloaded': total_downloaded,
                'next_cycle_hours': SCAN_INTERVAL / 3600.0,
            }, f)
    except Exception as exc:
        logger.warning(f"Could not write cycle_complete file: {exc}")


def _new_cycle_stats(cycle_start_time) -> dict:
    return {
        'user': APP_USER_NAME,
        'start_time': cycle_start_time,
        'downloads': {
            'trending': 0, 'user_playlists': 0, 'top_artists': 0,
            'similar_tracks': 0, 'album_completion': 0, 'discord_requests': 0, 'total': 0,
        },
        'metadata_fixes': 0,
        'errors': {'list': [], 'billboard': None, 'tiktok': None, 'spotify': None,
                   'metadata_fix': None, 'discord_requests': None},
        'library_stats': {},
    }


def _stagger_start(user_index: int) -> None:
    """Offset each user's start slightly so they don't hit the same APIs at once."""
    stagger_seconds = int(os.environ.get('CYCLE_STAGGER_SECONDS', '30'))
    if user_index > 0:
        delay = min(user_index * stagger_seconds, 120)
        logger.info(f"⏳ Staggering start for user {APP_USER_NAME} (index {user_index}): waiting {delay}s")
        time.sleep(delay)


def _song_identity(title: str):
    """A song's title as album completion compares it: case, punctuation and
    "(feat. …)" ignored, versions ("Live", "Remix") kept apart."""
    from song_dedup import title_core, variant_labels
    return title_core(title), variant_labels(title)


_EDITION_WORDS = {'collectors', 'collector', 's', 'edition', 'deluxe', 'expanded', 'anniversary', 'remastered',
                  'remaster', 'version', 'special', 'bonus', 'track', 'tracks', 'super', 'complete', 'th'}


def _same_album_title(candidate: str, album_title: str) -> bool:
    """A release title that is this album's: a fuzzy match, or the title
    followed only by edition words ("DAMN. COLLECTORS EDITION.")."""
    from lidarr_catalog import normalize_words, titles_match
    if titles_match(candidate, album_title)[0]:
        return True
    cand, mine = normalize_words(candidate), normalize_words(album_title)
    if not mine or not cand.startswith(mine + ' '):
        return False
    return all(w in _EDITION_WORDS or w.isdigit() or re.fullmatch(r'\d+(st|nd|rd|th)', w)
               for w in cand[len(mine) + 1:].split())


def _deezer_album_tracklist(artist: str, album_title: str, present: List[str]) -> List[str]:
    """The track titles of the Deezer release that is this album: same artist,
    the same title (or that title plus edition words), and most of the songs
    already here on it (at least two, and 60 %). Releases come from an album
    search and from the artist's own discography (a search for a famous title
    returns mostly covers). The most songs found, then an exact title, then
    the shorter edition wins."""
    from lidarr_catalog import _title_as_written, name_similarity
    get = lambda url, **params: requests.get(url, params=params, timeout=15).json().get('data') or []
    try:
        found = get('https://api.deezer.com/search/album', q=f'artist:"{artist}" album:"{album_title}"', limit=10)
    except Exception as e:
        logger.debug(f"Deezer album search failed for {artist} - {album_title}: {e}")
        return []
    releases = {a['id']: a for a in found
                if name_similarity((a.get('artist') or {}).get('name', ''), artist) >= 0.85}
    for artist_id in {a['artist']['id'] for a in releases.values() if (a.get('artist') or {}).get('id')}:
        try:
            for a in get(f'https://api.deezer.com/artist/{artist_id}/albums', limit=200):
                releases.setdefault(a['id'], a)
        except Exception as e:
            logger.debug(f"Deezer discography failed for artist {artist_id}: {e}")
    have = {_song_identity(t) for t in present}
    needed = max(2, -(-len(have) * 6 // 10))
    best = None
    candidates = [a for a in releases.values()
                  if a.get('record_type') != 'compile' and _same_album_title(a.get('title', ''), album_title)]
    # An exact title and a full album first: only the first few track lists are fetched.
    candidates.sort(key=lambda a: (_title_as_written(a['title']) != _title_as_written(album_title),
                                   a.get('record_type') != 'album'))
    for item in candidates[:8]:
        try:
            titles = [t['title'] for t in get(f"https://api.deezer.com/album/{item['id']}/tracks", limit=200)
                      if t.get('title')]
        except Exception as e:
            logger.debug(f"Deezer track list failed for album {item.get('id')}: {e}")
            continue
        overlap = len(have & {_song_identity(t) for t in titles})
        if overlap < min(needed, len(have)) or overlap < 2:
            continue
        rank = (overlap, _title_as_written(item['title']) == _title_as_written(album_title), -len(titles))
        if best is None or rank > best[0]:
            best = (rank, titles)
    return best[1] if best else []


def _stage_album_completion(analyzer, cycle_stats: dict) -> None:
    _log_banner("📀 ALBUM COMPLETION WORKFLOW 📀")
    logger.info(f"🎵 Looking for loved albums ({ALBUM_COMPLETION_MIN_LOVED}+ songs scoring "
                f"{ALBUM_COMPLETION_SCORE} or rated 4★) that are missing songs...")
    albums = analyzer.find_albums_to_complete()
    if not albums:
        logger.info("No loved album is missing songs.")
        return
    _push_cycle_status("Album completion", f"Completing {len(albums)} album(s)")
    downloaded = 0
    for album in albums:
        recommendations = [
            {'artist': album['artist'], 'title': title, 'album': album['album'],
             'reason': f"Completing album: {album['album']}", 'confidence': 1.0, 'type': 'song'}
            for title in album['missing']]
        downloaded += len(analyzer.download_recommended_music(recommendations, preference_confidence=1.0,
                                                              playlist='similar', limit=len(recommendations)) or [])
    cycle_stats['downloads']['album_completion'] = downloaded


def _collect_trending_tracks(analyzer, cycle_stats: dict) -> list:
    """Billboard (genre-aware), TikTok, and Spotify trending, in that order."""
    tracks = []

    logger.info("\n📊 BILLBOARD TRENDING MUSIC DISCOVERY 📊")
    try:
        top_user_genres = analyzer.get_user_top_genres(limit=5)
        if top_user_genres:
            logger.info(f"🎵 User's top 5 genres: {[g[0] for g in top_user_genres]}")
            found = analyzer.get_billboard_top_by_genres(top_user_genres, limit_per_genre=30)
        else:
            found = analyzer.get_billboard_hot_100(limit=150)
        if found:
            logger.info(f"📥 Fetched {len(found)} Billboard trending songs")
            tracks.extend(found)
    except Exception as exc:
        _record_stage_error(cycle_stats, 'billboard', f"Billboard trending error: {str(exc)[:100]}")

    if ENABLE_TIKTOK_TRENDING:
        logger.info("\n🎵 TIKTOK TRENDING MUSIC DISCOVERY 🎵")
        try:
            found = analyzer.get_tiktok_trending_music(limit=175)
            if found:
                logger.info(f"📥 Fetched {len(found)} TikTok trending songs")
                tracks.extend(found)
            else:
                logger.warning("No TikTok trending data available")
        except Exception as exc:
            _record_stage_error(cycle_stats, 'tiktok', f"TikTok trending error: {str(exc)[:100]}")
    else:
        logger.info("TikTok trending skipped because ENABLE_TIKTOK_TRENDING=false")

    logger.info("\n🎵 SPOTIFY TRENDING MUSIC DISCOVERY 🎵")
    try:
        found = analyzer.get_spotify_trending_music(limit=175)
        if found:
            logger.info(f"📥 Fetched {len(found)} Spotify trending songs")
            tracks.extend(found)
        else:
            logger.warning("No Spotify trending data available")
    except Exception as exc:
        _record_stage_error(cycle_stats, 'spotify', f"Spotify trending error: {str(exc)[:100]}")

    return tracks


def _stage_trending(analyzer, cycle_stats: dict) -> None:
    _log_banner("🎵 UNIFIED TRENDING MUSIC WORKFLOW (Billboard + TikTok + Spotify - 500 songs) 🎵")
    if not can_download_trending():
        logger.info("⏳ Skipping trending download - last download was less than 2 weeks ago")
        return

    all_trending = _collect_trending_tracks(analyzer, cycle_stats)
    if not all_trending:
        logger.warning("No trending tracks fetched from any source (Billboard, TikTok, or Spotify)")
        return

    all_trending = analyzer.without_disliked_artists(all_trending, "Trending")
    _push_cycle_status("Trending", "Downloading trending music recommendations")
    to_download = all_trending[:500]
    logger.info(f"\n📥 Downloading {len(to_download)} total trending songs from all sources...")
    for track in to_download:
        track['confidence'] = max(0.75, track.get('confidence', 0.75))
    # download_recommended_music also collects them for "Trending Now for You"
    # from whatever it manages to download.
    analyzer.download_recommended_music(to_download, preference_confidence=0.75, playlist='trending')
    cycle_stats['downloads']['trending'] = len(to_download)
    update_trending_lock()



def _stage_discord_requests(analyzer, cycle_stats: dict) -> None:
    _log_banner("🤖 PROCESSING DISCORD MUSIC REQUESTS 🤖")
    try:
        _push_cycle_status("Discord requests", "Processing queued music requests")
        processed = analyzer.process_discord_requests()
        cycle_stats['downloads']['discord_requests'] = processed
        logger.info(f"✓ Processed {processed} Discord request(s)" if processed else "No pending Discord requests")
    except Exception as exc:
        _record_stage_error(cycle_stats, 'discord_requests', f"Discord request processing error: {str(exc)[:100]}")


def _stage_popular_playlists(analyzer, cycle_stats: dict) -> None:
    _log_banner("🎵 POPULAR PLAYLISTS DISCOVERY & DOWNLOAD 🎵")
    try:
        popular_playlists = analyzer.get_popular_spotify_playlists(limit=15)
        if not popular_playlists:
            logger.info("No popular playlists found or unable to fetch from Spotify")
            return
        recommendations = []
        for playlist_info in popular_playlists:
            playlist_name = playlist_info.get('name', 'Unknown')
            playlist_tracks = playlist_info.get('tracks', [])
            logger.info(f"📋 Processing '{playlist_name}' ({len(playlist_tracks)} tracks)")
            for track in playlist_tracks:
                recommendations.append({
                    'artist': track.get('artist', 'Unknown'),
                    'title': track.get('name', 'Unknown'),
                    'album': track.get('album', 'Unknown'),
                    'confidence': 0.90,
                    'reason': f'From popular playlist: {playlist_name}',
                    'source': 'popular_playlists',
                    'type': 'song',
                })
        recommendations = analyzer.without_disliked_artists(recommendations, "Popular playlists")
        if not recommendations:
            logger.warning("No tracks extracted from popular playlists")
            return
        _push_cycle_status("User playlists", "Downloading popular playlist tracks")
        logger.info(f"📥 Downloading {len(recommendations)} tracks from popular playlists...")
        analyzer.download_recommended_music(recommendations, preference_confidence=0.85, target_dir=ALBUM_DIR,
                                            playlist='trending')
        cycle_stats['downloads']['user_playlists'] = len(recommendations)
    except Exception as exc:
        _record_stage_error(cycle_stats, 'list', f"Popular playlists error: {str(exc)[:100]}")


def _download_top_artists_directly(analyzer, cycle_stats: dict) -> None:
    top_artists = analyzer.get_user_top_artists(limit=TOP_ARTIST_LIMIT, score_threshold=0.6)
    if not top_artists:
        logger.info("No top artists found in listening history")
        return
    _push_cycle_status("Top artists", "Fetching and downloading top-artist recommendations")
    downloaded = analyzer.download_from_top_artists(top_artists=top_artists, limit_per_artist=20, min_artist_score=0.6)
    cycle_stats['downloads']['top_artists'] = downloaded
    logger.info(f"✓ Top artists download complete: {downloaded} tracks downloaded")


def _stage_top_artists(analyzer, cycle_stats: dict, user_index: int) -> None:
    _log_banner("🎤 TOP 5 ARTISTS ROTATION & DOWNLOAD 🎤")
    try:
        if not (LIDARR_URLS and LIDARR_API_KEYS):
            logger.info("Lidarr not configured, downloading top artists directly")
            _download_top_artists_directly(analyzer, cycle_stats)
            return
        logger.info("Recomputing top 5 based on recent listening activity (last 30 days)...")
        try:
            stats = analyzer.sync_top_artists_to_lidarr(
                top_n=TOP_ARTIST_LIMIT, recency_days=30, min_artist_score=0.6, user_index=user_index,
            )
            logger.info(f"✓ Lidarr top-5 sync: {stats['added']} added, {stats['evicted']} evicted, {stats['unchanged']} unchanged")
            if stats['top_artists']:
                logger.info(f"✓ Currently following: {stats['top_artists']}")
            else:
                logger.info("No recently-active artists qualified; falling back to direct top-artist downloads")
                _download_top_artists_directly(analyzer, cycle_stats)
        except Exception as exc:
            logger.warning(f"Error syncing top 5 artists to Lidarr: {str(exc)[:100]}")
            _download_top_artists_directly(analyzer, cycle_stats)
        # Runs whatever the rotation did, so anything Lidarr added on its own
        # since the last cycle is unmonitored again.
        analyzer.enforce_top_artist_monitoring(user_index)
    except Exception as exc:
        _record_stage_error(cycle_stats, 'list', f"Top artists error: {str(exc)[:100]}")


def _stage_similar_tracks(analyzer, cycle_stats: dict) -> None:
    _log_banner("🎵 SIMILAR TRACKS DISCOVERY (Last.FM) 🎵")
    if not LAST_FM_API_KEY:
        logger.debug("Last.FM API key not configured, skipping similar tracks discovery")
        return
    try:
        downloaded = analyzer.download_from_similar_tracks(recent_tracks=None, limit=100)
        cycle_stats['downloads']['similar_tracks'] = downloaded
        logger.info(f"✓ Similar tracks download complete: {downloaded} tracks downloaded" if downloaded
                    else "No similar tracks met confidence threshold or API unavailable")
    except Exception as exc:
        _record_stage_error(cycle_stats, 'list', f"Similar tracks error: {str(exc)[:100]}")


def _library_stats() -> dict:
    """Songs in the library, and how many of them are new or soon to go."""
    import library_state
    try:
        songs = sum(1 for _, _, files in os.walk(ALBUM_DIR)
                    for f in files if f.lower().endswith(library_state.AUDIO_EXTENSIONS))
        states = library_state.counts()
        return {'songs': songs, 'probation': states[library_state.PROBATION],
                'soon_to_go': states[library_state.SOON_TO_GO]}
    except Exception as exc:
        logger.debug(f"Could not capture library stats: {exc}")
        return {'songs': 0, 'probation': 0, 'soon_to_go': 0}


def _stage_finalize(analyzer, cycle_stats: dict, user_index: int) -> None:
    _push_cycle_status("Finalizing", "Disliked songs and the soon-to-go playlist")

    # Graduation, probation and disk space are decided once per pass, after
    # every user's cycle has refreshed their scores (library_review.py, run
    # by the manager); only what depends on this user's own ratings is here.
    _log_banner("⚠️  DISLIKED SONGS ⚠️")
    analyzer.set_disliked_songs_to_go()
    analyzer.refresh_soon_to_go_playlist()
    # Unknown album/artist repair runs daily in fix_metadata_daemon.py.
    analyzer.get_stats()

    _log_banner("🎵 MERGING SIMILAR ALBUM DIRECTORIES 🎵")
    try:
        merge_similar_albums(ALBUM_DIR)
    except Exception as exc:
        logger.warning(f"Could not merge similar albums: {exc}")

    # The Lidarr-first album reconcile runs daily in fix_metadata_daemon.py.

    # Artist tags are never set from folder names: albums/ is Lidarr's and its
    # tags win, and a folder named "<Artist> - <Title>" would become the
    # songs' artist. Fix Metadata sets names from what identifies the song.

    cycle_stats['library_stats'] = _library_stats()
    cycle_stats['downloads']['total'] = sum(
        cycle_stats['downloads'][k] for k in
        ('trending', 'user_playlists', 'top_artists', 'similar_tracks', 'album_completion', 'discord_requests')
    )

    _log_banner("📝 FIXING ALL MUSIC METADATA 📝")
    try:
        analyzer.fix_all_music_metadata()
    except Exception as exc:
        logger.warning(f"Could not fix music metadata: {exc}")

    # Lidarr and Plex each rescan once, after every file change of the cycle.
    try:
        analyzer.refresh_lidarr_library(user_index=user_index)
    except Exception as exc:
        logger.warning(f"Could not trigger final Lidarr library refresh: {exc}")

    _log_banner("🔄 FINAL PLEX LIBRARY SCAN 🔄")
    try:
        analyzer._music_section_for_scans().update()
        logger.info("✓ Plex library scan requested")
    except Exception as exc:
        logger.warning(f"Could not trigger final Plex scan: {exc}")


def _report_cycle_complete(cycle_stats: dict) -> None:
    _push_cycle_status(
        "Completed", f"Cycle finished with {cycle_stats['downloads']['total']} download(s)",
        stats={k: cycle_stats[k] for k in ('downloads', 'metadata_fixes', 'errors', 'library_stats')},
    )
    if DISCORD_ENABLED and discord_bot:
        try:
            asyncio.run(discord_bot.send_cycle_summary(cycle_stats, delete_after=300))
        except Exception as exc:
            logger.debug(f"Could not send Discord cycle summary: {exc}")
    logger.info(f"\n✓ Analysis cycle complete. Waiting {SCAN_INTERVAL}s ({SCAN_INTERVAL/86400:.1f} days) for next cycle...")


def _report_cycle_error(exc: Exception) -> None:
    logger.error(f"Error during analysis cycle: {exc}", exc_info=True)
    if DISCORD_ENABLED and discord_bot:
        try:
            asyncio.run(discord_bot.send_cycle_error(
                f"Unexpected error in analysis cycle: {str(exc)[:500]}", cycle_phase="Analysis Cycle",
            ))
        except Exception as discord_err:
            logger.debug(f"Could not send Discord error: {discord_err}")
    logger.info("Will retry on next cycle")


def _plex_reachable(analyzer) -> bool:
    plex = getattr(analyzer, 'plex', None)
    if plex is None:
        return True
    try:
        plex.library.sections()
        return True
    except Exception:
        return False


def _run_cycle(analyzer, run_once: bool):
    """One full analysis/download cycle for this user.

    Returns an exit code when the caller should stop (cycle-once mode), or
    None to continue with the regular schedule.
    """
    cycle_start_time = datetime.now()
    logger.info("Starting analysis cycle...")
    try:
        if discord_bot and hasattr(discord_bot, 'reload_wizarr_mapping'):
            discord_bot.reload_wizarr_mapping()
    except Exception as exc:
        logger.debug(f"Could not reload Wizarr mapping: {exc}")

    analyzed_tracks = analyzer.analyze_all_tracks()
    if not analyzed_tracks and not _plex_reachable(analyzer):
        # Plex being down is not "no listening activity": don't record a skip
        # for it, and in cycle-once mode exit so the caller can retry later.
        logger.warning("No tracks returned and Plex is unreachable; aborting this cycle")
        return 1 if run_once else None
    preference_confidence = analyzer.calculate_preference_confidence(analyzed_tracks)
    should_skip, reason = _should_skip_cycle(analyzed_tracks, preference_confidence)
    if should_skip:
        logger.info(f"Skipping download cycle for {APP_USER_NAME}: insufficient activity or confidence ({reason})")
        analyzer.remove_cycle_playlists()
        _write_cycle_skip_file(reason, SCAN_INTERVAL / 3600.0)
        return 0 if run_once else None

    _write_cycle_running_file(cycle_start_time)
    cycle_stats = _new_cycle_stats(cycle_start_time)
    try:
        user_index = int(APP_USER_INDEX)
    except ValueError:
        user_index = 0
    _stagger_start(user_index)

    tools_status = analyzer.check_download_tools()
    if not tools_status.get('deemix'):
        logger.warning(f"⚠ Download tool status: {tools_status}")
    analyzer.check_api_availability()

    analyzer.start_cycle_playlists()
    try:
        _push_cycle_status("Analysis", "Inspecting library and low-rated content")
        _stage_album_completion(analyzer, cycle_stats)
        _stage_trending(analyzer, cycle_stats)
        _stage_discord_requests(analyzer, cycle_stats)
        _stage_popular_playlists(analyzer, cycle_stats)
        _stage_top_artists(analyzer, cycle_stats, user_index)
        _stage_similar_tracks(analyzer, cycle_stats)
        _stage_finalize(analyzer, cycle_stats, user_index)
        _push_cycle_status("Finalizing", f"Filling '{TRENDING_PLAYLIST}' and '{SIMILAR_PLAYLIST}'")
        analyzer.fill_cycle_playlists()
        cycle_stats['metadata_fixes'] = 0
        _report_cycle_complete(cycle_stats)
        _remove_cycle_running_file()
        _write_cycle_complete_file(cycle_stats['downloads']['total'])
    except Exception as exc:
        _remove_cycle_running_file()
        _report_cycle_error(exc)
        # The songs downloaded before the failure are still this cycle's.
        analyzer.fill_cycle_playlists()

    return 0 if run_once else None


def main(run_once: bool = False):
    """Main execution loop."""
    logger.info("Starting Music AI Analyzer")
    if run_once:
        logger.info("Running single cycle (cycle-once mode)")

    analyzer = None
    while not analyzer:
        try:
            analyzer = MusicAIAnalyzer()
        except Exception as exc:
            logger.error(f"Failed to initialize Music AI Analyzer: {exc}")
            logger.info("Retrying in 30 seconds...")
            time.sleep(30)

    global ACTIVE_ANALYZER
    ACTIVE_ANALYZER = analyzer
    # Daily metadata repair is not started here: this process only lives for
    # one cycle, so it runs in fix_metadata_daemon.py instead.
    threading.Thread(
        target=run_discord_request_processor_loop, args=(lambda: ACTIVE_ANALYZER,),
        daemon=True, name=f"discord-processor-{APP_USER_NAME}",
    ).start()
    logger.info("✓ Discord request background processor started (checks every 30s)")

    try:
        first_cycle = True
        while True:
            _process_pending_discord_requests(analyzer)

            if first_cycle and not CYCLE_ON_STARTUP and not run_once:
                logger.info("CYCLE_ON_STARTUP=false; skipping first analysis cycle and waiting for next scheduled cycle")
                first_cycle = False
                time.sleep(SCAN_INTERVAL)
                continue
            first_cycle = False

            exit_code = _run_cycle(analyzer, run_once)
            if exit_code is not None:
                logger.info("Single cycle finished; exiting cycle-once mode")
                return exit_code
            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Shutting down Music AI Analyzer")
        return 0
    except Exception as exc:
        logger.error(f"Fatal error in main loop: {exc}", exc_info=True)
        time.sleep(30)
        return main(run_once=run_once)
import sys

# Only interpret CLI arguments and run the analyzer loop when app.py is executed directly.
if __name__ == "__main__":
    import metadata_proxy
    metadata_proxy.install()          # lookups outside the VPN, downloads in it
    if len(sys.argv) > 1:
        logger.info(f"CLI args: {sys.argv}")
        command = sys.argv[1].strip().lower()
        logger.info(f"Executing command: {command!r}")
    
    if command == "download-exportify":
        # Download all songs from exportify CSV files
        # Uses EXPORTIFY_DIR and ALBUM_DIR environment variables
        download_from_exportify()
        logger.info("Exportify download completed")
    
    elif command == "cleanup-albums":
        # Enhanced cleanup with selective options
        # Usage: python app.py cleanup-albums [target_dir] [--options]
        target_dir = None
        options = {
            'organize': True,           # Reorganize directories and sanitize names
            'metadata': True,           # Update metadata from pipeline
            'covers': False,            # Download album covers
            'replace_youtube': True,    # Replace low-quality YouTube audio
            'detect_music_videos': True,# Detect and flag music video audio
            'find_duplicates': False,   # Find and report duplicate tracks
            'remove_duplicates': False, # Automatically remove detected duplicates
            'merge_albums': True,       # Merge similar album names together
            'force': False,             # Force operations even if skippable
        }
        
        # Parse arguments
        for arg in sys.argv[2:]:
            if arg.startswith('--'):
                # The explicit-version search always runs; these flags are accepted and ignored.
                if arg.lower() in ['--find-explicit', '--no-find-explicit']:
                    logger.debug(f"Ignoring {arg}: the explicit-version search always runs")
                    continue
                flag = arg[2:].lower().replace('-', '_')  # Convert hyphens to underscores
                if flag in options:
                    options[flag] = True
                elif flag.startswith('no_'):
                    base_flag = flag[3:]
                    if base_flag in options:
                        options[base_flag] = False
            elif not arg.startswith('-') and not target_dir:
                target_dir = arg

        
        if not target_dir:
            target_dir = ALBUM_DIR
        
        if not os.path.isdir(target_dir):
            logger.error(f"Album directory not found: {target_dir}")
            print(f"Usage: python app.py cleanup-albums [target_dir] [options]")
            print(f"Options:")
            print(f"  --organize              - Reorganize directories and sanitize names (default: on)")
            print(f"  --metadata              - Update metadata from pipeline (default: on)")
            print(f"  --covers                - Download missing album covers")
            print(f"  --replace-youtube       - Replace low-quality YouTube audio (default: on)")
            print(f"  --detect-music-videos   - Detect and flag music video audio (default: on)")
            print(f"  --merge-albums          - Merge similar album names together (default: on)")
            print(f"  --find-duplicates       - Find and report duplicate tracks")
            print(f"  --remove-duplicates     - Remove detected duplicates (requires --find-duplicates)")
            print(f"Note: Explicit version search is now automatic and always enabled.")
            print(f"  --force                 - Force operations")
            print(f"Example: python app.py cleanup-albums /music/albums --covers --find-duplicates")
            print(f"Example: python app.py cleanup-albums /music/albums --merge-albums")
            print(f"Example: python app.py cleanup-albums /music/albums --find-duplicates --remove-duplicates")
            sys.exit(1)
        
        logger.info(f"Starting cleanup of albums directory: {target_dir}")
        logger.info(f"Enabled options: {[k for k, v in options.items() if v]}")
        
        # Stats counters
        stats = {
            'artists_reorganized': 0,
            'items_renamed': 0,
            'metadata_updated': 0,
            'covers_downloaded': 0,
            'youtube_replaced': 0,
            'music_videos_flagged': 0,
            'duplicates_found': 0,
            'duplicates_removed': 0,
            'errors': 0,
            'db_cleared': 0,
        }

    elif command == 'cycle-once':
        return_code = main(run_once=True)
        sys.exit(return_code if isinstance(return_code, int) else 0)

    elif command.startswith('clear-db'):
        # Usage: python app.py clear-db [--force]
        force = False
        for arg in sys.argv[2:]:
            if arg == '--force':
                force = True
        analyzer = None
        try:
            analyzer = MusicAIAnalyzer()
        except Exception as e:
            logger.warning(f"Could not initialize MusicAIAnalyzer (continuing without instance): {e}")
            analyzer = None

        logger.info(f"Running clear-db CLI (force={force})")
        if not force:
            confirm = input('Are you sure you want to clear the database tables (track_scores, recommendations, temp_downloads)? Type YES to confirm: ')
            if confirm.strip() != 'YES':
                logger.info('Aborting clear-db')
                sys.exit(0)

        # Clear all discovered DBs under the data root (e.g., /app/data/*/music_ai.db)
        # Derive data root from configured DB_PATH
        db_path_default = DB_PATH
        candidate1 = os.path.dirname(db_path_default)
        if os.path.basename(candidate1) == 'data':
            data_root = candidate1
        else:
            candidate2 = os.path.dirname(candidate1)
            if os.path.basename(candidate2) == 'data':
                data_root = candidate2
            else:
                # Fall back to parent directory of DB_PATH
                data_root = candidate1

        # Collect DB paths: root DB (data_root/music_ai.db) plus any user subfolders with music_ai.db
        logger.info(f"Data root for DB discovery: {data_root}")
        db_paths = []
        root_db = os.path.join(data_root, 'music_ai.db')
        if os.path.exists(root_db):
            db_paths.append(root_db)

        try:
            entries = os.listdir(data_root)
            logger.debug(f"Entries in {data_root}: {entries}")
            for entry in entries:
                sub_db = os.path.join(data_root, entry, 'music_ai.db')
                if os.path.exists(sub_db):
                    db_paths.append(sub_db)
        except Exception as e:
            # If listing fails, proceed with whatever we have
            logger.error(f"Error listing data root {data_root}: {e}")

        db_paths = sorted(set(db_paths))
        logger.info(f"Discovered database files to clear: {db_paths}")

        if not db_paths:
            logger.warning(f"No databases found to clear in {data_root}")
            sys.exit(0)

        cleared = 0
        for p in db_paths:
            logger.info(f"Clearing DB: {p}")
            try:
                # Prefer instance method if analyzer available, otherwise call on class
                if analyzer:
                    success = analyzer.clear_database(force=True, db_path=p)
                else:
                    success = MusicAIAnalyzer.clear_database(MusicAIAnalyzer, force=True, db_path=p)

                if success:
                    cleared += 1
            except Exception as e:
                logger.error(f"Error clearing {p}: {e}")

        logger.info(f"Cleared {cleared} database(s)")
        if cleared:
            logger.info('Database cleared successfully')
            sys.exit(0)
        else:
            logger.error('Failed to clear any databases')
            sys.exit(1)
        def detect_silence_regions(file_path, threshold_db=-40):
            """
            Detect silence or very low volume regions at start/end of audio file.
            YouTube music videos often have intro/outro silence or low-volume sections.
            Returns (has_start_silence, has_end_silence)
            """
            try:
                import wave
                import struct
                
                if not file_path.endswith('.wav'):
                    # For non-WAV files, we can check file structure
                    return False, False
                
                with wave.open(file_path, 'rb') as wav_file:
                    frames = wav_file.readframes(wav_file.getnframes())
                    n_channels = wav_file.getnchannels()
                    sample_width = wav_file.getsampwidth()
                    framerate = wav_file.getframerate()
                    
                    # Calculate RMS for first and last 2 seconds
                    samples_per_second = framerate * n_channels
                    check_samples = samples_per_second * 2  # 2 seconds
                    
                    # Check start
                    start_chunk = frames[:check_samples * sample_width]
                    end_chunk = frames[-check_samples * sample_width:] if len(frames) > check_samples * sample_width else frames
                    
                    def calculate_rms(audio_data):
                        if len(audio_data) < 2:
                            return 0
                        samples = struct.unpack(f'<{len(audio_data)//sample_width}h', audio_data)
                        mean_square = sum(s**2 for s in samples) / len(samples)
                        return (mean_square ** 0.5) / (2**15)  # Normalized
                    
                    start_rms = calculate_rms(start_chunk)
                    end_rms = calculate_rms(end_chunk)
                    
                    # Very low RMS suggests silence or near-silence
                    has_start_silence = start_rms < 0.05
                    has_end_silence = end_rms < 0.05
                    
                    return has_start_silence, has_end_silence
            
            except Exception as e:
                logger.debug(f"Could not analyze audio waveform: {e}")
                return False, False
        
        def is_music_video_audio(file_path):
            """
            Detect if audio file is from a music video with added intro/outro audio.
            Enhanced detection checks for:
            - Unusual silence at start/end (video intro/outro)
            - Metadata tags indicating video
            - Duration anomalies (too long or too short)
            - YouTube-specific artifacts and bitrate patterns
            - ID3 tag corruption patterns common in YouTube extraction
            - Album art metadata (YouTube videos often have video thumbnails)
            """
            try:
                import struct
                
                audio = MutagenFile(file_path)
                if not audio or not hasattr(audio, 'info'):
                    return False
                
                info = audio.info
                duration = getattr(info, 'length', 0)
                bitrate = getattr(info, 'bitrate', 0)
                filename = os.path.basename(file_path).lower()
                
                # Check filename patterns typical of YouTube extractions
                youtube_filename_patterns = [
                    'official music video', 'official video', 'music video',
                    'official video hd', 'videoclip', 'video clip',
                    'directed by', 'visualizer', '[official', '[video',
                    'youtube', 'yt-dlp', 'ytdl', '.webm.mp3'  # WebM converted to MP3 is common YouTube pattern
                ]
                if any(pattern in filename for pattern in youtube_filename_patterns):
                    logger.debug(f"Detected YouTube-style filename pattern: {filename}")
                    return True
                
                # Check for unusually long durations (extended for video)
                if duration > 480:  # 8 minutes - unusually long for standard track
                    logger.debug(f"Detected unusually long duration ({duration/60:.1f}min) in {filename}")
                    return True
                
                # Check for too-short durations (YouTube intros/outros)
                if duration < 120:  # Less than 2 minutes - likely just intro/outro
                    logger.debug(f"Detected very short duration ({duration:.0f}s) in {filename}")
                    return True
                
                # Check if duration is suspiciously longer than typical song length
                # Most songs are 3-5 minutes, but YouTube videos often have 1-2 minutes added intro/outro
                # Flagging songs that are >30% longer than typical 3.5 min baseline
                typical_song_length = 210  # 3.5 minutes in seconds
                if duration > typical_song_length * 1.3:  # More than 4.55 minutes
                    # This could be a YouTube video with intro/outro added
                    logger.debug(f"Detected suspiciously long duration for typical song ({duration:.0f}s vs typical ~{typical_song_length}s) in {filename}")
                    return True
                
                # Check for silence at start/end (common in YouTube music video extractions)
                if file_path.endswith('.wav'):
                    has_start_silence, has_end_silence = detect_silence_regions(file_path)
                    if has_start_silence or has_end_silence:
                        logger.debug(f"Detected silence region in {filename} (start: {has_start_silence}, end: {has_end_silence})")
                        return True
                
                # Check metadata for video indicators
                if hasattr(audio, 'tags') and audio.tags:
                    tags_str = str(audio.tags).lower()
                    video_indicators = [
                        'music video', 'official video', 'video clip', 'directed by', 
                        'visualizer', 'youtube', 'official music video', 'music videos',
                        'video version', 'video release'
                    ]
                    if any(indicator in tags_str for indicator in video_indicators):
                        logger.debug(f"Detected video indicator in metadata: {filename}")
                        return True
                    
                    # Check for album art that looks like a video thumbnail (specific dimensions)
                    # YouTube thumbnails are often 1280x720 or similar video dimensions
                    if hasattr(audio.tags, 'getall'):
                        try:
                            apic_frames = audio.tags.getall('APIC')
                            for frame in apic_frames:
                                if hasattr(frame, 'data'):
                                    # Check if image has video-like aspect ratio (16:9 or similar)
                                    img_data = frame.data
                                    if len(img_data) > 50:
                                        # Look for JPEG or PNG headers with video dimensions
                                        if (b'\xff\xd8' in img_data[:100] or b'\x89PNG' in img_data[:100]):
                                            logger.debug(f"Detected potential video thumbnail in {os.path.basename(file_path)}")
                                            return True
                        except Exception:
                            pass
                
                # Check for YouTube-specific bitrate patterns
                # YouTube audio extraction typically uses 128kbps AAC or similar
                if bitrate and (bitrate == 128000 or bitrate == 192000):
                    logger.debug(f"Detected YouTube-standard bitrate ({bitrate/1000:.0f}kbps) in {os.path.basename(file_path)}")
                    return True
                
                # Try to detect silence at start/end (video intro/outro)
                try:
                    if file_path.endswith('.mp3'):
                        with open(file_path, 'rb') as f:
                            # Read file to check for silence patterns
                            data = f.read()
                            
                            # Look for silence markers (ID3v2 frames with minimal audio data)
                            if b'SILNT' in data or len(data) > 0 and data.count(b'\x00') > len(data) * 0.1:
                                logger.debug(f"Detected silence marker in {os.path.basename(file_path)}")
                                return True
                            
                            # Check for frame sync markers at unusual positions (sign of extraction artifact)
                            sync_marker = b'\xff\xfb'  # MP3 frame sync marker
                            sync_count = data.count(sync_marker)
                            expected_syncs = int(duration * 50 / 1000)  # ~50 syncs per second in MP3
                            
                            if sync_count > 0 and abs(sync_count - expected_syncs) > expected_syncs * 0.5:
                                logger.debug(f"Detected unusual MP3 sync pattern ({sync_count} vs expected ~{expected_syncs}) in {os.path.basename(file_path)}")
                                return True
                except Exception as e:
                    logger.debug(f"Could not analyze MP3 structure: {e}")
                
                return False
            except Exception as e:
                logger.debug(f"Could not check if {file_path} is music video: {e}")
                return False
        
        
        def replace_youtube_audio(file_path, artist, track_name, album_name):
            """Replace YouTube audio with better quality from deemix or spotdl."""
            try:
                logger.info(f"Attempting to replace YouTube audio: {artist} - {track_name}")
                
                # Create temp directory for downloads
                temp_replace_dir = os.path.join(STAGING_DIR, 'yt_replace')
                os.makedirs(temp_replace_dir, exist_ok=True)
                
                # Try spotdl first (faster and more reliable for replacing)
                # Use much shorter timeout (30s) to avoid hanging
                try:
                    search_query = f"{artist} {track_name}"
                    logger.debug(f"Searching with spotdl: {search_query}")
                    cmd = ['spotdl', 'download', search_query, '--output', temp_replace_dir, '--format', 'mp3']
                    try:
                        proc = run_subprocess_with_limit(cmd, service_name='spotdl', timeout=30)
                        if not proc:
                            raise RuntimeError('spotdl failed or was rate-limited')

                        logger.debug(f"spotdl returncode: {proc.returncode}")
                        logger.debug(f"spotdl stdout: {proc.stdout[:200] if proc.stdout else 'None'}")
                        logger.debug(f"spotdl stderr: {proc.stderr[:200] if proc.stderr else 'None'}")

                        if proc.returncode == 0:
                            # Find downloaded file
                            for f in os.listdir(temp_replace_dir):
                                new_path = os.path.join(temp_replace_dir, f)
                                if os.path.isfile(new_path) and f.endswith(('.mp3', '.m4a', '.flac', '.ogg')):
                                    try:
                                        # Backup original first
                                        backup_path = file_path + '.youtube_backup'
                                        shutil.copy2(file_path, backup_path)
                                        logger.debug(f"Created backup: {backup_path}")
                                        
                                        # Replace with new version
                                        shutil.move(new_path, file_path)
                                        logger.info(f"✓ Replaced YouTube audio with spotdl version: {track_name}")
                                        
                                        # Cleanup temp directory
                                        shutil.rmtree(temp_replace_dir, ignore_errors=True)
                                        return True
                                    except Exception as e:
                                        logger.warning(f"Failed to move downloaded file: {e}")
                                        if os.path.exists(new_path):
                                            os.remove(new_path)
                    except (subprocess.TimeoutExpired, KeyboardInterrupt) as e:
                        logger.debug(f"spotdl process timed out or interrupted ({type(e).__name__}), skipping replacement")
                except Exception as e:
                    logger.debug(f"spotdl replacement attempt failed: {e}")
                
                # Try deemix as fallback
                # Use deemix Python library instead of CLI for proper ARL support
                if DEEZER_ARL:
                    try:
                        from deezer import Deezer
                        
                        search_query = f"{artist} {track_name}"
                        logger.info(f"🎵 [DEEMIX-REPLACE] Replacing YouTube audio via Deezer: {search_query}")
                        
                        # Initialize Deezer
                        dz = Deezer()
                        if not dz.login_via_arl(DEEZER_ARL):
                            logger.warning(f"✗ [DEEMIX-REPLACE] Failed to authenticate with Deezer (ARL token invalid or expired)")
                            raise Exception("Deezer ARL authentication failed")
                        
                        # Search for track
                        try:
                            logger.debug(f"[DEEMIX-REPLACE] Searching Deezer for: '{search_query}'")
                            search_results = dz.gw.search_music(search_query, type='TRACK', limit=5)
                            tracks = search_results.get('data', [])
                            
                            if not tracks:
                                # Try just the artist name
                                logger.debug(f"[DEEMIX-REPLACE] Full query returned no results, trying artist only: '{artist}'")
                                search_results = dz.gw.search_music(artist, type='TRACK', limit=5)
                                tracks = search_results.get('data', [])
                            
                            if tracks:
                                logger.info(f"✓ [DEEMIX-REPLACE] Found {len(tracks)} Deezer track(s)")
                                
                                # Use first result (prefer_explicit_version if available)
                                track = prefer_explicit_version(tracks)
                                track_id = track.get('SNG_ID')
                                track_title = track.get('SNG_TITLE', 'Unknown')
                                
                                if track_id:
                                    logger.debug(f"[DEEMIX-REPLACE] Selected track: {track_title} (ID: {track_id})")
                                    # Download the track
                                    try:
                                        from deemix.downloader import Downloader
                                        from deemix.settings import load as loadSettings
                                        from deemix import generateDownloadObject
                                        
                                        config_folder = Path.home() / '.config' / 'deemix'
                                        settings = loadSettings(config_folder)
                                        settings['downloadLocation'] = temp_replace_dir
                                        
                                        class SimpleListener:
                                            def send(self, key, value=None):
                                                pass
                                        
                                        listener = SimpleListener()
                                        track_url = f"https://www.deezer.com/track/{track_id}"
                                        logger.debug(f"[DEEMIX-REPLACE] Generating download object for {track_url}")
                                        download_obj = generateDownloadObject(dz, track_url, str(settings.get('maxBitrate', 320)), {}, listener)
                                        
                                        if download_obj:
                                            logger.debug(f"[DEEMIX-REPLACE] Starting deemix download...")
                                            downloader = Downloader(dz, download_obj, settings, listener)
                                            downloader.start()
                                            logger.debug(f"[DEEMIX-REPLACE] Download initiated for track {track_id}")
                                            
                                            # Check if file was created
                                            time.sleep(2)
                                            if os.path.exists(temp_replace_dir):
                                                files = os.listdir(temp_replace_dir)
                                                logger.debug(f"[DEEMIX-REPLACE] Temp dir contains: {files}")
                                                for f in files:
                                                    new_path = os.path.join(temp_replace_dir, f)
                                                    if os.path.isfile(new_path) and f.endswith(('.mp3', '.m4a', '.flac', '.ogg')):
                                                        try:
                                                            # Backup original first
                                                            backup_path = file_path + '.youtube_backup'
                                                            shutil.copy2(file_path, backup_path)
                                                            logger.debug(f"Created backup: {backup_path}")
                                                            
                                                            # Replace with new version
                                                            shutil.move(new_path, file_path)
                                                            logger.info(f"✓ [DEEMIX-REPLACE-SUCCESS] Replaced YouTube audio with Deezer quality: {track_name}")
                                                            
                                                            # Cleanup temp directory
                                                            shutil.rmtree(temp_replace_dir, ignore_errors=True)
                                                            return True
                                                        except Exception as e:
                                                            logger.warning(f"✗ [DEEMIX-REPLACE-MOVE-ERROR] Failed to move downloaded file: {e}")
                                                            if os.path.exists(new_path):
                                                                os.remove(new_path)
                                            else:
                                                logger.warning(f"✗ [DEEMIX-REPLACE] Download completed but no files found in temp directory")
                                        else:
                                            logger.warning(f"✗ [DEEMIX-REPLACE] Failed to generate download object for track {track_id}")
                                    except Exception as e:
                                        logger.warning(f"✗ [DEEMIX-REPLACE] Download error: {e}")
                                else:
                                    logger.warning(f"✗ [DEEMIX-REPLACE] Search result missing track ID")
                            else:
                                logger.info(f"ℹ️  [DEEMIX-REPLACE] No tracks found on Deezer for '{search_query}'")
                        except Exception as e:
                            logger.warning(f"✗ [DEEMIX-REPLACE] Deezer search error: {e}")
                    except ImportError as e:
                        logger.debug(f"[DEEMIX-REPLACE] Deezer library not available: {e}")
                    except Exception as e:
                        logger.warning(f"✗ [DEEMIX-REPLACE] Unexpected error: {e}")
                
                logger.info(f"⚠ Could not replace YouTube audio for {track_name} - keeping original")
                shutil.rmtree(temp_replace_dir, ignore_errors=True)
                return False
            except (KeyboardInterrupt, Exception) as e:
                if isinstance(e, KeyboardInterrupt):
                    logger.warning(f"YouTube audio replacement interrupted by user")
                else:
                    logger.error(f"Error replacing YouTube audio: {e}")
                return False
        
        # Main cleanup loop
        try:
            for artist_dir_name in os.listdir(target_dir):
                artist_path = os.path.join(target_dir, artist_dir_name)
                
                if not os.path.isdir(artist_path):
                    continue
                
                # Parse and sanitize artist name
                primary_artist, featured = parse_artist_field(artist_dir_name)
                sanitized_artist = sanitize_filename(primary_artist)
                
                # Handle artist directory reorganization
                if options['organize'] and sanitized_artist != artist_dir_name:
                    new_artist_path = os.path.join(target_dir, sanitized_artist)
                    
                    if os.path.exists(new_artist_path):
                        logger.info(f"Merging {artist_dir_name} into {sanitized_artist}")
                        for album_name in os.listdir(artist_path):
                            src = os.path.join(artist_path, album_name)
                            dst = os.path.join(new_artist_path, album_name)
                            
                            if os.path.isdir(src):
                                if not os.path.exists(dst):
                                    try:
                                        shutil.move(src, dst)
                                    except Exception as e:
                                        logger.warning(f"Failed to move {src} -> {dst}: {e}")
                                else:
                                    for root, dirs, files in os.walk(src):
                                        rel_root = os.path.relpath(root, src)
                                        target_root = os.path.join(dst, rel_root) if rel_root != '.' else dst
                                        os.makedirs(target_root, exist_ok=True)
                                        for f in files:
                                            src_file = os.path.join(root, f)
                                            dst_file = os.path.join(target_root, f)
                                            if not os.path.exists(dst_file):
                                                try:
                                                    shutil.move(src_file, dst_file)
                                                except Exception:
                                                    try:
                                                        shutil.copy2(src_file, dst_file)
                                                        os.remove(src_file)
                                                    except Exception as e:
                                                        logger.warning(f"Failed to move {src_file}: {e}")
                                    try:
                                        shutil.rmtree(src)
                                    except Exception as e:
                                        logger.debug(f"Could not remove {src}: {e}")
                        
                        try:
                            os.rmdir(artist_path)
                        except OSError:
                            remaining = os.listdir(artist_path) if os.path.exists(artist_path) else []
                            if remaining:
                                logger.warning(f"Source artist dir not empty: {remaining}")
                    else:
                        logger.info(f"Renaming artist: {artist_dir_name} -> {sanitized_artist}")
                        os.rename(artist_path, new_artist_path)
                    
                    artist_path = new_artist_path
                    stats['artists_reorganized'] += 1
                
                # Process all albums in this artist
                for album_name in os.listdir(artist_path):
                    album_path = os.path.join(artist_path, album_name)
                    
                    if not os.path.isdir(album_path):
                        continue
                    
                    # Sanitize album directory name
                    if options['organize']:
                        sanitized_album = sanitize_filename(album_name)
                        if sanitized_album != album_name:
                            new_album_path = os.path.join(artist_path, sanitized_album)
                            if not os.path.exists(new_album_path):
                                logger.info(f"Renaming album: {album_name} -> {sanitized_album}")
                                os.rename(album_path, new_album_path)
                                album_path = new_album_path
                                stats['artists_reorganized'] += 1
                    
                    # Download covers if requested
                    if options['covers']:
                        try:
                            success = _download_album_cover(
                                artist=primary_artist,
                                album_name=album_name,
                                target_dir=album_path,
                                force_update=options['force']
                            )
                            if success:
                                stats['covers_downloaded'] += 1
                        except Exception as e:
                            logger.debug(f"Could not download cover: {e}")
                    
                    # Process files in album
                    for filename in os.listdir(album_path):
                        file_path = os.path.join(album_path, filename)
                        
                        if not os.path.isfile(file_path):
                            continue
                        
                        if not filename.endswith(('.mp3', '.m4a', '.flac', '.opus', '.ogg')):
                            continue
                        
                        # Sanitize filename
                        if options['organize']:
                            name, ext = os.path.splitext(filename)
                            sanitized_name = sanitize_filename(name)
                            sanitized_filename = sanitized_name + ext
                            
                            if sanitized_filename != filename:
                                new_file_path = os.path.join(album_path, sanitized_filename)
                                if not os.path.exists(new_file_path):
                                    logger.info(f"Renaming file: {filename} -> {sanitized_filename}")
                                    os.rename(file_path, new_file_path)
                                    file_path = new_file_path
                                    stats['items_renamed'] += 1
                        
                        # Update metadata
                        if options['metadata']:
                            try:
                                from mutagen.easyid3 import EasyID3
                                metadata = _get_metadata_from_sources(primary_artist, album_name, filename.split('.')[0])
                                if metadata and metadata.get('source'):
                                    audio = EasyID3(file_path)
                                    audio['artist'] = metadata.get('artist', primary_artist)
                                    audio['album'] = metadata.get('album', album_name)
                                    audio['title'] = metadata.get('title', filename)
                                    if metadata.get('year'):
                                        audio['date'] = str(metadata['year'])
                                    audio.save()
                                    stats['metadata_updated'] += 1
                            except Exception as e:
                                logger.debug(f"Could not update metadata: {e}")
                        
                        # Detect music video audio and replace
                        if options['detect_music_videos']:
                            try:
                                if is_music_video_audio(file_path):
                                    logger.warning(f"Detected music video audio: {os.path.join(primary_artist, album_name, filename)}")
                                    stats['music_videos_flagged'] += 1
                                    
                                    # Tag the file with a comment
                                    try:
                                        from mutagen.easyid3 import EasyID3
                                        audio = EasyID3(file_path)
                                        audio['comment'] = 'MUSIC_VIDEO_DETECTED'
                                        audio.save()
                                    except Exception:
                                        pass
                                    
                                    # Try to replace with original if --replace-youtube flag used
                                    if options['replace_youtube']:
                                        if replace_youtube_audio(file_path, primary_artist, filename.split('.')[0], album_name):
                                            stats['youtube_replaced'] += 1
                            except Exception as e:
                                logger.debug(f"Error checking for music video: {e}")
            
            # Duplicate detection phase (after all file processing)
            if options['find_duplicates']:
                logger.info("=" * 60)
                logger.info("🔍 DUPLICATE DETECTION PHASE 🔍")
                logger.info("=" * 60)
                
                # Collect directories to scan
                dirs_to_scan = [target_dir]
                
                # Also scan MUSIC_DIR when it is another folder
                if MUSIC_DIR and MUSIC_DIR != target_dir:
                    dirs_to_scan.append(MUSIC_DIR)
                
                logger.info(f"Scanning {len(dirs_to_scan)} directory/directories for duplicates...")
                
                # Find duplicates
                duplicates = find_duplicate_tracks(dirs_to_scan, similarity_threshold=0.85)
                
                if duplicates:
                    logger.info(f"Found {len(duplicates)} duplicate groups:")
                    
                    stats['duplicates_found'] = len(duplicates)
                    
                    for idx, dup_group in enumerate(duplicates, 1):
                        dup_type = dup_group.get('type', 'unknown')
                        similarity = dup_group.get('similarity', 0)
                        group = dup_group.get('group', [])
                        
                        logger.info(f"\n  Duplicate Group {idx} ({dup_type}, similarity: {similarity:.2%}):")
                        
                        # Sort by file size (keep largest, remove smaller)
                        sorted_group = sorted(group, key=lambda x: os.path.getsize(x['path']) if os.path.exists(x['path']) else 0, reverse=True)
                        
                        for track_idx, track in enumerate(sorted_group):
                            file_path = track['path']
                            file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                            duration = track.get('duration', 0)
                            artist = track.get('artist', 'Unknown')
                            title = track.get('title', 'Unknown')
                            
                            keep_mark = "✓ KEEP" if track_idx == 0 else "✗ REMOVE"
                            logger.info(f"    [{keep_mark}] {artist} - {title}")
                            logger.info(f"         Path: {file_path}")
                            logger.info(f"         Size: {file_size/1024/1024:.1f}MB | Duration: {duration:.0f}s")
                        
                        # Remove duplicates if --remove-duplicates flag is set
                        if options['remove_duplicates']:
                            logger.info(f"  Removing {len(sorted_group)-1} duplicate(s)...")
                            
                            for track in sorted_group[1:]:  # Skip first (keep) one
                                file_path = track['path']
                                try:
                                    # Set aside outside the library (Plex scans all of /music)
                                    archive_backup = os.path.join(os.environ.get("DATA_DIR", "/app/data"),
                                                                  "merge_quarantine", "duplicates_removed")
                                    os.makedirs(archive_backup, exist_ok=True)
                                    backup_filename = os.path.basename(file_path)
                                    backup_path = os.path.join(archive_backup, backup_filename)
                                    
                                    # Set aside outside the library instead of deleting
                                    if os.path.exists(file_path):
                                        shutil.move(file_path, backup_path)
                                        logger.info(f"    ✓ Set aside: {backup_path}")
                                        stats['duplicates_removed'] += 1
                                except Exception as e:
                                    logger.error(f"    ✗ Failed to remove duplicate: {e}")
                else:
                    logger.info("✓ No duplicates found!")
                
                logger.info("=" * 60)
        
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
            stats['errors'] += 1
        
        # Print summary
        logger.info("=" * 60)
        logger.info("Cleanup Summary:")
        logger.info(f"  Artists reorganized: {stats['artists_reorganized']}")
        logger.info(f"  Items renamed: {stats['items_renamed']}")
        logger.info(f"  Metadata updated: {stats['metadata_updated']}")
        logger.info(f"  Covers downloaded: {stats['covers_downloaded']}")
        logger.info(f"  YouTube audio replaced: {stats['youtube_replaced']}")
        logger.info(f"  Music videos flagged: {stats['music_videos_flagged']}")
        logger.info(f"  Duplicates found: {stats['duplicates_found']}")
        logger.info(f"  Duplicates removed: {stats['duplicates_removed']}")
        logger.info(f"  Errors: {stats['errors']}")
        
        # Merge similar albums if requested
        if options['merge_albums']:
            logger.info("=" * 60)
            logger.info("🎵 Merging similar album names...")
            logger.info("=" * 60)
            merge_similar_albums(target_dir)
        
        # Find and replace with explicit versions (automatic, always enabled)
        logger.info("=" * 60)
        logger.info("🎵 Searching for explicit versions to replace...")
        logger.info("=" * 60)
        find_and_replace_explicit(target_dir)
        
        logger.info("=" * 60)
        
        print(f"✓ Cleanup completed:")
        for key, value in stats.items():
            print(f"  {key}: {value}")

    
    else:
        logger.error(f"Unknown command: {command}")
        print(f"Usage: python app.py [command]")
        print(f"  download-exportify       - Download from Spotify exportify CSVs using container env vars")
        print(f"  cleanup-albums [dir] [options] - Comprehensive album cleanup")
        print(f"    Options:")
        print(f"      --organize              - Reorganize and sanitize names (default: on)")
        print(f"      --metadata              - Update metadata tags (default: on)")
        print(f"      --covers                - Download missing album covers")
        print(f"      --replace-youtube       - Replace low-quality YouTube audio (default: on)")
        print(f"      --detect-music-videos   - Detect music video audio artifacts (default: on)")
        print(f"      --merge-albums          - Merge similar album names together (default: on)")
        print(f"      --find-duplicates       - Find and report duplicate tracks")
        print(f"      --remove-duplicates     - Remove detected duplicates (requires --find-duplicates)")
        print(f"      --force                 - Force operations even if skippable")
        print(f"    Note: Explicit version search is now automatic and always enabled.")
        print(f"    Example: python app.py cleanup-albums /music/albums --covers --detect-music-videos --force")
        print(f"    Example: python app.py cleanup-albums /music/albums --merge-albums")
        print(f"    Example: python app.py cleanup-albums /music/albums --find-duplicates")
        print(f"    Example: python app.py cleanup-albums /music/albums --find-duplicates --remove-duplicates")
        sys.exit(1)

# When imported by other modules (e.g. discord_bot.py), do not start
# the Music AI analyzer automatically.
# The app.py analyzer loop runs only when executed as a script.
