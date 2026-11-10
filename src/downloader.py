import importlib
import json
import logging
import os
import shutil
import subprocess
import time
import fcntl
from typing import List, Dict, Any, Tuple, Optional
import requests
import functools
from datetime import datetime
import re
import io
from difflib import SequenceMatcher

# write_lock_status is best-effort — import may fail during isolated tests.
try:
    from downloader_manager import write_lock_status as _write_lock_status_dm
except ImportError:
    _write_lock_status_dm = None  # type: ignore

def write_lock_status(track: str, tool: str, stage: str, batch_info: str = "") -> None:
    """Module-level shim — delegates to downloader_manager.write_lock_status."""
    if _write_lock_status_dm is not None:
        try:
            _write_lock_status_dm(track, tool, stage, batch_info)
        except Exception:
            pass

logger = logging.getLogger('musicai.app')

# unidecode is used as a fallback for cross-script filename matching (e.g. a
# Cyrillic expected title "детство" vs a transliterated downloaded filename
# "Detstvo") — best-effort, degrades to direct string comparison if missing.
try:
    from unidecode import unidecode as _unidecode
    _UNIDECODE_AVAILABLE = True
except ImportError:
    _unidecode = None  # type: ignore
    _UNIDECODE_AVAILABLE = False

import geo_relay
import request_choice
from naming import artist_from_channel, balanced, song_in_quotes, split_outside_brackets
from rate_limit import (
    APIRateLimiter, get_rate_limiter, is_rate_limit_error, handle_rate_limit,
    with_rate_limit, requests_get_with_limit, run_subprocess_with_limit,
)




# Global metadata source manager instance
_metadata_source_manager = None


# Where a list of artists is split for the folder: "Artist1, Artist2",
# "A / B", "A & B", "A and B".
_ARTIST_LIST_SEPARATOR = re.compile(r"\s*[/,]\s*|\s+&\s+|\s+and\s+", re.IGNORECASE)


class LidarrClient:
    """Client for lightweight interactions with a Lidarr server."""
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/') if base_url else ''
        self.api_key = api_key

    def _build_url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, path: str, method: str = 'GET', params: dict = None,
                 json_payload: dict = None, timeout: int = 15):
        if not self.base_url or not self.api_key:
            raise ValueError("Lidarr URL/API key are not configured")

        url = self._build_url(path)
        params = dict(params or {})
        params['apikey'] = self.api_key
        headers = {'Accept': 'application/json'}

        try:
            if method.upper() == 'GET':
                resp = requests.get(url, params=params, timeout=timeout, headers=headers)
            else:
                resp = requests.request(method.upper(), url, params=params, json=json_payload,
                                         timeout=timeout, headers=headers)

            if resp.status_code == 401:
                raise ValueError('Lidarr API authentication failed (401)')
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise ValueError(f'Lidarr API request failed {resp.status_code}: {resp.text[:180]}')

            try:
                return resp.json()
            except ValueError:
                return resp.text

        except Exception as e:
            raise

    def get_album_metadata(self, artist: str, album_name: str):
        """Try to retrieve album metadata from Lidarr's lookup endpoints."""
        if not album_name and not artist:
            return None

        candidates = []
        queries = []
        if album_name and artist:
            queries.append(f"{artist} {album_name}")
        if album_name:
            queries.append(album_name)
        if artist:
            queries.append(artist)

        for query in queries:
            try:
                data = self._request('/api/v1/album/lookup', method='GET', params={'term': query})
                if not data:
                    continue

                albums = []
                if isinstance(data, list):
                    albums = data
                elif isinstance(data, dict):
                    albums = data.get('records') or data.get('albums') or []

                for item in albums:
                    parsed = self._parse_album_item(item, artist, album_name)
                    if parsed:
                        candidates.append(parsed)
                if candidates:
                    return candidates[0]
            except Exception:
                continue

        if artist:
            try:
                data = self._request('/api/v1/artist/lookup', method='GET', params={'term': artist})
                if data:
                    artists = []
                    if isinstance(data, list):
                        artists = data
                    elif isinstance(data, dict):
                        artists = data.get('records') or data.get('artists') or []

                    for artist_item in artists:
                        albums = []
                        if isinstance(artist_item, dict):
                            albums = artist_item.get('albums') or artist_item.get('releases') or []
                        for item in albums:
                            parsed = self._parse_album_item(item, artist, album_name)
                            if parsed:
                                candidates.append(parsed)
                    if candidates:
                        return candidates[0]
            except Exception:
                pass

        return None

    def refresh_library(self):
        """Ask Lidarr to rescan its library folders so files written by this cycle are picked up."""
        return self._request('/api/v1/command', method='POST', json_payload={'name': 'RescanFolders'}) is not None


    def _get_existing_artist_id(self, artist_name: str):
        existing_artists = self._request('/api/v1/artist', method='GET')
        if isinstance(existing_artists, list):
            for artist_info in existing_artists:
                if isinstance(artist_info, dict) and artist_info.get('artistName', '').strip().lower() == artist_name.strip().lower():
                    return artist_info.get('id')
        return None

    def _add_artist_to_library(self, artist_info: dict) -> Optional[int]:
        if not isinstance(artist_info, dict):
            return None
        payload = {
            'artistName': artist_info.get('artistName') or artist_info.get('name'),
            'foreignArtistId': artist_info.get('foreignArtistId') or artist_info.get('artistId'),
            # Added only so one album can be searched for. Monitoring is
            # reserved for the users' top artists (see
            # enforce_top_artist_monitoring), so nothing else is followed.
            'monitored': False,
            'monitorNewItems': 'none',
            'rootFolderPath': os.environ.get('LIDARR_ROOT_FOLDER', '/music/albums'),
            'qualityProfileId': int(os.environ.get('LIDARR_QUALITY_PROFILE_ID', '1')),
            'metadataProfileId': int(os.environ.get('LIDARR_METADATA_PROFILE_ID', '1')),
            'albumFolder': True,
            'addOptions': {
                'monitor': 'none',
                'searchForMissingAlbums': False
            }
        }
        result = self._request('/api/v1/artist', method='POST', json_payload=payload)
        if isinstance(result, dict):
            return result.get('id')
        return None

    def _ensure_artist_in_library(self, artist: str):
        artist_id = self._get_existing_artist_id(artist)
        if artist_id:
            return artist_id

        artist_data = self._request('/api/v1/artist/lookup', method='GET', params={'term': artist})
        if not artist_data or not isinstance(artist_data, list) or not artist_data:
            return None

        artist_info = artist_data[0]
        artist_id = artist_info.get('id') if isinstance(artist_info, dict) else None
        if artist_id:
            return artist_id

        return self._add_artist_to_library(artist_info)

    def download_album(self, artist: str, album: str) -> bool:
        """Trigger a Lidarr album search/import for the requested artist/album.

        This keeps Lidarr as the primary automation path for new downloads while
        still allowing MusicAI to fall back to its own tools if Lidarr cannot
        initiate the request.
        """
        try:
            artist_id = self._ensure_artist_in_library(artist)
            if not artist_id:
                raise ValueError(f"Lidarr artist not found: {artist}")

            albums = self._request('/api/v1/album', method='GET', params={'artistId': artist_id})
            album_id = None
            if isinstance(albums, list):
                wanted = self._normalise_album_text(album)
                for item in albums:
                    if not isinstance(item, dict):
                        continue
                    candidate = item.get('title') or item.get('albumTitle') or ''
                    if self._album_similarity(candidate, wanted) >= 0.82:
                        album_id = item.get('id')
                        break

            if not album_id:
                # Searching without an album would fetch the artist's whole
                # catalogue; let the built-in tools handle it instead.
                raise ValueError(f"Lidarr has no album matching '{album}' for {artist}")
            payload = {'name': 'AlbumSearch', 'albumIds': [album_id]}
            result = self._request('/api/v1/command', method='POST', json_payload=payload)
            if result is not None:
                logger.info(f"✅ Lidarr album search initiated for {artist} - {album}")
                return True
        except Exception as e:
            logger.debug(f"Lidarr album search initiation failed: {e}")

        logger.warning(f"Lidarr album search could not be initiated for {artist} - {album}")
        return False

    @staticmethod
    def _normalise_album_text(value: str) -> str:
        return re.sub(r'[^a-z0-9]+', ' ', (value or '').lower()).strip()

    @classmethod
    def _album_similarity(cls, left: str, right: str) -> float:
        return SequenceMatcher(None, cls._normalise_album_text(left), cls._normalise_album_text(right)).ratio()

    def find_album_for_playlist(self, playlist_name: str, tracks: list) -> Optional[dict]:
        """Find a Lidarr album represented by a playlist, conservatively."""
        if not tracks:
            return None

        artists = [str(track.get('artist', '')).strip() for track in tracks if track.get('artist')]
        if not artists:
            return None
        artist = max(set(artists), key=artists.count)
        artist_ratio = artists.count(artist) / len(artists)
        if artist_ratio < 0.7:
            return None

        lookup = self.get_album_metadata(artist, playlist_name)
        if not lookup or self._album_similarity(lookup.get('album', ''), playlist_name) < 0.82:
            return None

        track_count = lookup.get('track_count')
        if track_count and abs(int(track_count) - len(tracks)) > max(2, int(len(tracks) * 0.35)):
            return None

        return {'artist': artist, 'album': lookup['album'], 'track_count': track_count}

    def _parse_album_item(self, item: dict, artist: str, album_name: str):
        if not isinstance(item, dict):
            return None

        album = item.get('title') or item.get('albumTitle') or item.get('name') or album_name
        year = None
        if item.get('year'):
            year = str(item.get('year'))
        elif item.get('releaseDate'):
            year = str(item.get('releaseDate'))[:4]
        genres = item.get('genres') or item.get('tags') or []
        cover_url = item.get('remoteCover') or item.get('coverUrl') or item.get('imageUrl')
        if not cover_url and isinstance(item.get('images'), list) and item['images']:
            first = item['images'][0]
            if isinstance(first, dict):
                cover_url = first.get('url') or first.get('uri')

        return {
            'album': album,
            'track_count': item.get('trackCount') or item.get('totalTrackCount'),
            'year': year,
            'genres': genres if isinstance(genres, list) else [genres] if genres else [],
            'cover_url': cover_url,
            'source': 'lidarr'
        }



# ═══════════════════════════════════════════════════════════════════════════
# VPN CONNECTOR (for secure downloads)
# ═══════════════════════════════════════════════════════════════════════════
class VPNConnector:
    """
    Handle VPN connectivity via the official Mullvad CLI/daemon.

    Architecture
    ────────────
    The Mullvad client tunnels the WHOLE container once connected — that's
    how Mullvad's daemon is designed to work, and the CLI doesn't expose a
    way to scope it to a single process/socket. LAN access (Plex, Lidarr,
    etc. on the local network) is explicitly allowed via `mullvad lan set
    allow` so those integrations keep working; everything else goes through
    the tunnel. geo_relay.py moves the exit relay temporarily when a request
    hits region-locked content.

    Because the tunnel covers the whole container, no separate SOCKS5 proxy
    is needed or started — `Downloader.socks5_proxy` stays `None`, and every
    `--proxy` flag downstream is simply skipped since traffic is already
    tunneled at the OS level.

    The `mullvad-daemon` process itself is expected to already be running —
    started once by the container's entrypoint script before any Python
    process starts. This class only drives the CLI against that daemon.

    Required env vars
    ─────────────────
    MULLVAD_ACCOUNT_NUMBER   Your Mullvad account number (digits only, no spaces)

    Optional env vars
    ──────────────────
    MULLVAD_RELAY_COUNTRY    ISO country code to connect through, e.g. 'us', 'se', 'de'
    MULLVAD_RELAY_CITY       Optional city code within that country, e.g. 'nyc', 'sto'

    System dependencies
    ───────────────────
    mullvad, mullvad-daemon   (apt package: mullvad-vpn)
    """

    _connect_lock_path = "/tmp/.mullvad_connect.lock"

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _run(cmd: list, *, timeout: int = 20) -> subprocess.CompletedProcess:
        """Run a shell command and return the CompletedProcess result (text mode)."""
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def _daemon_reachable() -> bool:
        try:
            return VPNConnector._run(["mullvad", "status"], timeout=5).returncode == 0
        except Exception:
            return False

    # ── public API ─────────────────────────────────────────────────────────────

    @staticmethod
    def initialize_vpn() -> Optional[bool]:
        """
        Log in (if needed), set the requested relay location, and connect via
        the Mullvad CLI.

        Returns True once connected, or None if VPN is not configured or
        fails to connect. Safe to call from multiple processes concurrently —
        all Mullvad CLI calls talk to the one shared daemon and are
        idempotent no-ops if already logged in / connected.
        """
        account_number = os.environ.get("MULLVAD_ACCOUNT_NUMBER", "").strip()
        if not account_number:
            logger.info("Mullvad account not configured — downloads will use the standard connection")
            return None

        if not VPNConnector._daemon_reachable():
            logger.warning(
                "mullvad-daemon is not reachable — it should be started once by the "
                "container's entrypoint before any app process runs. VPN will not be used."
            )
            return None

        try:
            # Serialize the login/connect sequence across the multiple concurrent
            # per-user processes in this container - avoids N processes all
            # issuing `mullvad connect` at the exact same instant.
            with open(VPNConnector._connect_lock_path, "w") as lock_fh:
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_EX)
                except Exception:
                    pass  # fcntl unavailable - proceed unlocked rather than block forever

                status = VPNConnector._run(["mullvad", "status"], timeout=10)
                already_connected = "Connected" in (status.stdout or "")

                if not already_connected:
                    login_result = VPNConnector._run(
                        ["mullvad", "account", "login", account_number], timeout=20
                    )
                    combined = ((login_result.stdout or "") + (login_result.stderr or "")).lower()
                    if login_result.returncode != 0 and "already logged in" not in combined:
                        logger.warning(f"Mullvad login failed: {(login_result.stderr or '').strip()}")
                        return None

                    # Allow LAN traffic through the firewall so Plex/Lidarr/etc.
                    # on the local network stay reachable once the tunnel is up.
                    VPNConnector._run(["mullvad", "lan", "set", "allow"], timeout=10)

                    relay_country = os.environ.get("MULLVAD_RELAY_COUNTRY", "").strip()
                    relay_city = os.environ.get("MULLVAD_RELAY_CITY", "").strip()
                    if relay_country:
                        relay_cmd = ["mullvad", "relay", "set", "location", relay_country]
                        if relay_city:
                            relay_cmd.append(relay_city)
                        r = VPNConnector._run(relay_cmd, timeout=10)
                        if r.returncode != 0:
                            logger.warning(f"Could not set Mullvad relay location: {(r.stderr or '').strip()}")
                        else:
                            logger.info(
                                f"Mullvad relay location set to: {relay_country}"
                                f"{'/' + relay_city if relay_city else ''}"
                            )

                    connect_result = VPNConnector._run(["mullvad", "connect", "--wait"], timeout=30)
                    if connect_result.returncode != 0:
                        logger.warning(f"Mullvad connect failed: {(connect_result.stderr or '').strip()}")
                        return None

                final_status = VPNConnector._run(["mullvad", "status"], timeout=10)
                if "Connected" in (final_status.stdout or ""):
                    first_line = final_status.stdout.strip().splitlines()[0] if final_status.stdout.strip() else "Connected"
                    logger.info(f"✓ Mullvad VPN connected — {first_line}")
                    return True

                logger.warning(f"Mullvad did not report Connected status: {final_status.stdout.strip()}")
                return None

        except Exception as exc:
            logger.warning(f"Mullvad VPN initialisation failed: {exc}. Continuing with standard connection.")
            return None




def _can_import(module_name: str) -> bool:
    """Return True if *module_name* is importable without actually importing it."""
    import importlib.util
    return importlib.util.find_spec(module_name) is not None


class Downloader:
    """Downloader encapsulates music download related functionality.

    It holds a reference to the parent analyzer to access helper methods and configuration.
    """
    def __init__(self, analyzer, socks5_proxy: Optional[str] = None):
        self.analyzer = analyzer
        # For convenience, expose module-level config via the analyzer's module
        # If analyzer is None (e.g., for discord_bot direct use), use app module directly
        if analyzer is not None:
            self.app_mod = importlib.import_module(self.analyzer.__module__)
        else:
            # Avoid importing the top-level `app` module during parent/manager
            # initialization (PID 1). Importing `app` here causes module-level
            # initialization to run under the parent process environment which
            # can pick up default values like APP_USER_NAME='default' and
            # produce misleading startup logs. Defer importing `app` until a
            # child process (with a proper per-user env) actually needs it.
            self.app_mod = None
            logger.debug("Downloader initialized without analyzer; deferring app module import")

        # An explicit socks5 proxy is only passed by tests.
        # In normal operation socks5_proxy=None always — VPN is initialized
        # lazily inside _download_with_yt_dlp so no other process or tool is
        # affected at startup.
        if socks5_proxy is not None:
            self.socks5_proxy = socks5_proxy
            self.vpn_enabled  = bool(self.socks5_proxy)
            logger.info(f"✓ Downloader initialized (explicit proxy: {self.socks5_proxy})")
        else:
            self.socks5_proxy = None   # populated lazily when the first download starts
            self.vpn_enabled  = False  # updated after lazy init
            logger.info("✓ Downloader initialized (VPN will connect on first download if configured)")

        # Initialize rate limiter for API calls
        self.rate_limiter = APIRateLimiter()

        # ARL stale instance flag -- set by _download_with_deemix when
        # login_via_arl() fails.  Reset to False before every new deemix attempt.
        # Checked by download_recommended_music to bail out the entire batch
        # immediately rather than retrying for every remaining track.
        self._arl_stale: bool = False
        self._deemix_consecutive_empty: int = 0  # consecutive auth-OK-but-no-file failures

    def _block_until_arl_refresh_if_stale(self, reason: str = 'download') -> bool:
        """Block the current download flow until the admin refreshes the ARL.

        Returns True when the stale-ARL gate is still active and the caller must
        stop processing, False when the ARL is clear or has been refreshed.
        """
        try:
            from downloader_manager import is_arl_stale, set_arl_stale, wait_for_arl_refresh, write_arl_alert

            if not is_arl_stale():
                return False

            logger.warning(
                "🚫 ARL is stale — blocking this %s until the admin refreshes the Deezer token",
                reason,
            )
            set_arl_stale(True)
            write_arl_alert()
            wait_for_arl_refresh(poll_interval=60, timeout=86400)
            return is_arl_stale()
        except Exception as stale_exc:
            logger.warning(f"Could not enforce stale-ARL block for {reason}: {stale_exc}")
            return False

    def _get_lidarr_client(self, user_index: int = 0):
        """Return a configured LidarrClient instance for the specified user or None if not configured."""
        try:
            from lidarr_catalog import lidarr_for_user
            instance = lidarr_for_user(user_index if user_index is not None else 0)
            return LidarrClient(*instance) if instance else None
        except Exception as e:
            logger.debug(f"Could not initialize Lidarr client for user {user_index}: {e}")
            return None

    def _infer_user_index(self, target_dir: str = None, plex_user: str = None) -> int:
        """Index of the user (admin is 0) for a Plex username or a /music/users/<user> path."""
        try:
            import users
            mapping = users.load_wizarr_mapping() or {}
            if plex_user and plex_user in mapping.values():
                return users.plex_user_index(plex_user, mapping)

            if target_dir:
                parts = os.path.normpath(target_dir).split(os.sep)
                for i, part in enumerate(parts):
                    if part == 'users' and i + 1 < len(parts) and parts[i + 1] in mapping.values():
                        return users.plex_user_index(parts[i + 1], mapping)

            return int(os.environ.get('APP_USER_INDEX', '0') or 0)
        except Exception:
            return 0


    def _is_track_blacklisted(self, artist: str, title: str) -> bool:
        """True if this user's blacklist has the track: a song the library
        review deleted (end of probation, disk space). Ratings and one-star
        deletions blacklist nothing. Matching is on normalised artist and
        title, so punctuation and featuring credits don't let it slip through.
        The database is this user's (``DB_PATH``)."""
        try:
            import sqlite3 as _sq3, re as _re

            db_path = os.environ.get("DB_PATH", "")
            if not db_path or not os.path.exists(db_path):
                return False

            def _norm(s: str) -> str:
                s = s.lower().strip()
                s = _re.sub(r"[^\w\s]", " ", s)
                s = _re.sub(r"\s+", " ", s).strip()
                return s

            artist_n = _norm(artist)
            title_n  = _norm(title)

            conn = _sq3.connect(db_path)
            try:
                for row_artist, row_title in conn.execute("SELECT artist, title FROM blacklist").fetchall():
                    if _norm(row_artist or "") == artist_n and _norm(row_title or "") == title_n:
                        return True
            finally:
                conn.close()

        except Exception as _e:
            logger.debug(f"_is_track_blacklisted check failed: {_e}")

        return False

    def _ensure_vpn(self) -> None:
        """Initialize VPN once per Downloader instance, the first time a download starts.

        Called at the top of download_recommended_music and download_track.
        Subsequent calls are instant no-ops.
        """
        if getattr(self, '_vpn_init_attempted', False):
            return
        self._vpn_init_attempted = True
        try:
            connected = VPNConnector.initialize_vpn()
            if connected:
                # Mullvad tunnels the whole container - there's no separate
                # SOCKS5 proxy to pass around. socks5_proxy stays None, so
                # every `--proxy` flag downstream is simply skipped, which is
                # correct: the traffic is already tunneled at the OS level.
                self.vpn_enabled = True
                logger.info("✓ [VPN] Connected via Mullvad — all traffic is tunneled at the OS level")
            else:
                logger.debug("[VPN] Not configured — all tools will use direct connection")
        except Exception as _ve:
            logger.debug(f"[VPN] Init skipped: {_ve}")

    def _write_status(self, track: str, tool: str, stage: str,
                      batch_info: str = "") -> None:
        """Write current download stage to the lock status file.

        Delegates to the module-level write_lock_status so waiting processes
        can see what the lock holder is doing.  Always a no-op on error.
        """
        try:
            write_lock_status(track, tool, stage, batch_info)
        except Exception:
            pass

    def check_download_tools(self) -> Dict[str, bool]:
        """Check presence of external download tools via Python libraries (not CLI).
        Returns True if the Python library can be imported, not just CLI availability.
        """
        tools = {
            'yt-dlp': False,
            'deemix': False,
            'soundcloud': False,
        }
        try:
            # Check yt-dlp library
            try:
                import yt_dlp
                tools['yt-dlp'] = True
            except ImportError:
                logger.debug('yt-dlp library not available')
            
            # Check deemix library (via deezer package)
            try:
                from deezer import Deezer
                tools['deemix'] = True
            except ImportError:
                logger.debug('deezer library not available')
            
            # Check soundcloud library (via scdl)
            try:
                import scdl
                tools['soundcloud'] = True
                logger.debug('soundcloud-downloader library available')
            except ImportError:
                logger.debug('soundcloud-downloader library not available (scdl import failed)')
                try:
                    from scdl_py import SoundCloud
                    tools['soundcloud'] = True
                    logger.debug('soundcloud-downloader available via scdl_py')
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f'soundcloud check error: {e}')
        except Exception:
            logger.debug('Error checking download tools availability', exc_info=True)
        logger.info(f"Download tools availability (Python libraries): {tools}")
        return tools
    

    # ------------------------------------------------------------------
    # Known-good probe track used to disambiguate "ARL stale" from
    # "this particular track is unavailable / search glitch".
    # Shape of You by Ed Sheeran — globally licensed, always on Deezer.
    _PROBE_TRACK_ID = "142986132"

    def _is_arl_genuinely_stale(self, dz=None) -> bool:
        """Check ARL validity via a direct HTTP request to Deezer's GW endpoint.

        Method
        ------
        GET https://www.deezer.com/ajax/gw-light.php
              ?method=deezer.getUserData&api_version=1.0&api_token=
        Cookie: arl=<ARL>

        The ARL is **valid** if and only if the JSON response contains a
        non-empty integer at ``results.USER.USER_ID``.

        The ARL is **stale** if ANY of the following are true:
        - The response body is HTML (login-redirect page)
        - ``results`` is null / missing
        - ``results.USER`` is missing
        - ``results.USER.USER_ID`` is 0, missing, or empty
        - An ``error`` object is present (e.g. code 300 "Invalid token")
        - Any network / parse exception occurs (treat as stale to be safe)

        The ``dz`` parameter is ignored: the probe talks to Deezer's API with
        the ARL cookie directly.

        Returns True if the ARL is stale, False if it is valid.
        """
        # Read the ARL from the same priority order as _download_with_deemix
        from pathlib import Path as _Path
        _arl_file = _Path.home() / ".config" / "deemix" / ".arl"
        arl = ""
        try:
            if _arl_file.exists():
                arl = _arl_file.read_text().strip()
        except Exception:
            pass
        if not arl:
            arl = os.environ.get("DEEZER_ARL", "").strip()

        if not arl:
            logger.warning("[ARL probe] No ARL configured — treating as stale")
            return True

        url = (
            "https://www.deezer.com/ajax/gw-light.php"
            "?method=deezer.getUserData&api_version=1.0&api_token="
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        }
        cookies = {"arl": arl}

        try:
            resp = requests.get(url, headers=headers, cookies=cookies, timeout=10)

            # HTML response = Deezer redirected to login page → stale
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type or resp.text.strip().startswith("<!"):
                logger.warning("[ARL probe] Response is HTML (login redirect) → ARL is stale")
                return True

            data = resp.json()

            # Explicit error object → stale
            if "error" in data and data["error"]:
                err = data["error"]
                code = err.get("code", "?") if isinstance(err, dict) else err
                logger.warning(f"[ARL probe] Error in response (code={code}) → ARL is stale")
                return True

            results = data.get("results") or {}
            if not results:
                logger.warning("[ARL probe] results is null/missing → ARL is stale")
                return True

            user = results.get("USER") or {}
            if not user:
                logger.warning("[ARL probe] results.USER missing → ARL is stale")
                return True

            user_id = user.get("USER_ID", 0)
            try:
                user_id_int = int(user_id)
            except (TypeError, ValueError):
                user_id_int = 0

            if user_id_int <= 0:
                logger.warning(
                    f"[ARL probe] results.USER.USER_ID={user_id!r} (not a positive int) → ARL is stale"
                )
                return True

            logger.info(f"[ARL probe] Valid — USER_ID={user_id_int}")
            return False

        except requests.exceptions.RequestException as _net_err:
            # Network failure — could be transient; don't trigger stale flow
            logger.warning(f"[ARL probe] Network error (treating as transient, not stale): {_net_err}")
            return False
        except Exception as _exc:
            logger.warning(f"[ARL probe] Unexpected error — treating as transient: {_exc}")
            return False

    def _get_ai_ranking_config(self) -> Dict[str, str]:
        """Return OpenAI-compatible local AI settings."""
        api_base = (
            os.environ.get('LOCAL_AI_API_BASE')
            or os.environ.get('OPENAI_API_BASE')
            or os.environ.get('OPENAI_BASE_URL')
            or 'http://localhost:4000/v1'
        ).strip().rstrip('/')
        api_key = os.environ.get('LOCAL_AI_API_KEY') or os.environ.get('OPENAI_API_KEY') or 'test'
        model = (
            os.environ.get('LOCAL_AI_MODEL')
            or os.environ.get('OPENAI_MODEL')
            or 'default'
        ).strip()
        return {'api_base': api_base, 'api_key': api_key, 'model': model}

    def _wait_for_ai_service(self, api_base: str, api_key: str = '') -> None:
        """Block until the local AI service becomes reachable.

        The AI is the final judge of whether a found track really is the one
        requested. If its container is down, stalling is preferable to
        guessing and organizing the wrong file into the library.
        """
        health_url = f"{api_base.rstrip('/')}/models"
        headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
        while True:
            try:
                response = requests.get(health_url, headers=headers, timeout=10)
                if response.status_code == 200:
                    logger.info(f"✅ AI service reachable at {health_url}")
                    return
                if response.status_code in (429, 502, 503, 504):
                    logger.warning(
                        f"AI service unavailable ({response.status_code}) at {health_url}; waiting 10s"
                    )
                    time.sleep(10)
                    continue
                logger.info(
                    f"AI service responded at {health_url} with status {response.status_code}; continuing"
                )
                return
            except requests.exceptions.RequestException as exc:
                logger.warning(
                    f"AI service not reachable at {health_url}: {exc}. Waiting 10s and retrying."
                )
                time.sleep(10)
            except Exception as exc:
                logger.warning(
                    f"Unexpected AI connectivity error at {health_url}: {exc}. Waiting 10s and retrying."
                )
                time.sleep(10)

    def _ask_local_ai(self, system: str, prompt: str) -> Optional[Dict[str, Any]]:
        """Send one chat completion to the local AI and return its JSON reply.

        Waits for the service to be reachable and retries transient 429/5xx
        responses indefinitely, so callers never have to fall back to a guess.
        Returns None only when the AI is misconfigured or its reply is not
        parseable JSON.
        """
        cfg = self._get_ai_ranking_config()
        api_base, api_key, model = cfg['api_base'], cfg['api_key'], cfg['model']
        if not api_base or not model or not api_base.startswith(('http://', 'https://')):
            return None

        while True:
            self._wait_for_ai_service(api_base, api_key)
            try:
                response = requests.post(
                    f"{api_base}/chat/completions",
                    headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
                    json={
                        'model': model,
                        'temperature': 0.1,
                        'messages': [
                            {'role': 'system', 'content': system},
                            {'role': 'user', 'content': prompt},
                        ],
                    },
                    timeout=60,
                )
            except requests.exceptions.RequestException as exc:
                logger.warning(f"AI request failed ({exc}); waiting 10s and retrying")
                time.sleep(10)
                continue

            if response.status_code in (429, 502, 503, 504):
                logger.warning(f"AI service returned {response.status_code}; waiting 10s and retrying")
                time.sleep(10)
                continue
            if response.status_code != 200:
                logger.debug(f"AI request failed: HTTP {response.status_code}")
                return None

            try:
                payload = response.json()
                content = payload['choices'][0]['message']['content']
            except Exception:
                return None
            if not isinstance(content, str) or not content.strip():
                return None

            text = content.strip()
            if text.startswith('```'):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                text = match.group(0)
            try:
                parsed = json.loads(text)
            except Exception:
                logger.debug(f"AI reply was not valid JSON: {text[:200]}")
                return None
            return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _length(candidate: Dict[str, Any]) -> str:
        seconds = int(candidate.get('duration') or 0)
        return f"{seconds // 60}:{seconds % 60:02d}" if seconds else ''

    @classmethod
    def _describe_candidate(cls, candidate: Dict[str, Any]) -> str:
        artist_val = (candidate.get('artist') or '').strip()
        title_val = (candidate.get('title') or '').strip()
        label = f"{artist_val} - {title_val}" if artist_val or title_val else "untitled"
        details = [f"{k}={candidate[k]}" for k in ('album', 'source') if (candidate.get(k) or '').strip()]
        if cls._length(candidate):
            details.append(f"length={cls._length(candidate)}")
        return label + (f" ({', '.join(details)})" if details else '')

    @classmethod
    def _candidate_option(cls, candidate: Dict[str, Any]) -> Dict[str, str]:
        """A candidate as the requester sees it in the list."""
        artist_val = (candidate.get('artist') or '').strip()
        title_val = (candidate.get('title') or '').strip()
        detail = [p for p in ((candidate.get('album') or '').strip(), cls._length(candidate)) if p]
        return {'label': f"{artist_val} - {title_val}" if artist_val else (title_val or "untitled"),
                'detail': " · ".join(detail)}

    @classmethod
    def _describe_candidates(cls, candidates: List[Dict[str, Any]]) -> str:
        return "\n".join(f"{idx + 1}. {cls._describe_candidate(c)}" for idx, c in enumerate(candidates))

    def _select_best_candidate_with_ai(
        self,
        artist: str,
        title: str,
        candidates: List[Dict[str, Any]],
        request_context: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Pick the best download candidate for the request."""
        if not candidates or len(candidates) < 2:
            return candidates[0] if candidates else None
        return self._ai_rank_candidates(artist, title, candidates, request_context)[0]

    def _ai_rank_candidates(self, artist: str, title: str, candidates: List[Dict[str, Any]],
                            request_context: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], float]:
        """The AI's pick among the candidates and how sure it is (0–1; 0 without a pick)."""
        request_context = (request_context or '').strip()
        context_hint = f"\nRequest context: {request_context}" if request_context else ''
        official_hint = (
            "This request is from an official YouTube upload by the requested artist. "
            "Prefer the original song and avoid remixes, alternate mixes, live versions, covers, or other variants "
            "unless the request explicitly asks for them.\n"
            if self._is_official_youtube_request(request_context, artist, title) else ''
        )
        prompt = (
            f"You are choosing the best music download candidate for the request '{artist} - {title}'.\n"
            + official_hint
            + "Choose the candidate that is the same recording and version as the request: the version the "
            + "linked video itself is (its title and description: live, acapella, acoustic, remix, extended, "
            + "instrumental, cover, sped up …), or the original song when the request names no version. "
            + "Each candidate shows its version in its title and its length; a length close to video_duration "
            + "is strong evidence. Never choose a vocals-only, instrumental, karaoke or remix version the "
            + "request did not ask for.\n"
            + "Candidates:\n" + self._describe_candidates(candidates) + context_hint + "\n"
            + "Give \"confidence\" from 0 to 1: how sure you are that the chosen candidate is the requested "
            + "recording and version (near 0 when you are only guessing or none of them looks like it).\n"
            + "Return only valid JSON in the form {\"selected_index\": <integer>, \"confidence\": <number>, "
            + "\"reason\": \"brief explanation\"}."
        )
        parsed = self._ask_local_ai('You rank music download candidates. Return compact JSON only.', prompt) or {}
        selected_index = parsed.get('selected_index')
        if isinstance(selected_index, int) and 0 <= selected_index - 1 < len(candidates):
            try:
                confidence = min(1.0, max(0.0, float(parsed.get('confidence', 1.0))))
            except (TypeError, ValueError):
                confidence = 1.0
            logger.info(f"🤖 AI selected candidate #{selected_index} for '{artist} - {title}' "
                        f"(confidence {confidence:.2f})")
            return candidates[selected_index - 1], confidence
        return None, 0.0

    # Where "None of these" leads from each source.
    _NEXT_SOURCE = {'Deezer': 'SoundCloud', 'SoundCloud': 'YouTube', 'YouTube': None}

    def _choose_or_ask(self, artist: str, title: str, candidates: List[Dict[str, Any]],
                       request_context: Optional[str], source: str,
                       fallback: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """The candidate to download for a requester who can be asked, or None
        to move on to the next source. The AI decides on its own unless its
        confidence is below REQUEST_CHOICE_BELOW (it is only guessing; a
        single result is judged too): then the requester chooses. With no
        answer in time, the AI's pick (else `fallback`) is taken."""
        choice, confidence = self._ai_rank_candidates(artist, title, candidates, request_context)
        if choice is not None and confidence >= request_choice.ask_below():
            return choice
        answer = request_choice.ask(
            f"{artist} - {title}" if artist else title, source,
            [self._candidate_option(c) for c in candidates],
            next_step=self._NEXT_SOURCE.get(source),
            suggestion=candidates.index(choice) if choice in candidates else None)
        if answer == request_choice.NEXT:
            logger.info(f"🙋 None of the {source} results is the song: trying the next source")
            return None
        if isinstance(answer, int):
            return candidates[answer]
        return choice or fallback

    def _should_continue_with_ai(
        self,
        artist: str,
        title: str,
        candidates: List[Dict[str, Any]],
        request_context: Optional[str] = None,
    ) -> bool:
        """Whether the current search results are trustworthy enough to download from."""
        if not candidates:
            return False

        request_context = (request_context or '').strip()
        context_hint = f"\nRequest context: {request_context}" if request_context else ''
        prompt = (
            f"You are judging whether search results are accurate enough to download the track '{artist} - {title}'.\n"
            "If the results look generic, mismatched, or too ambiguous for the request context, return false so the pipeline can fall back to the next downloader.\n"
            "If at least one result looks like a plausible match for the requested variant, return true. "
            "If the request mentions a specific variant like Arabic version, do not accept only a remix candidate unless it explicitly matches that variant.\n"
            "Candidates:\n" + self._describe_candidates(candidates) + context_hint + "\n"
            "Return only valid JSON in the form {\"should_continue\": true|false, \"reason\": \"brief explanation\"}."
        )
        parsed = self._ask_local_ai('You judge search-result quality for download pipelines. Return compact JSON only.', prompt)
        should_continue = (parsed or {}).get('should_continue')
        if isinstance(should_continue, bool):
            logger.info(f"🤖 AI fallback decision for '{artist} - {title}': {'continue' if should_continue else 'fallback'}")
            return should_continue
        return True

    def _looks_like_artist_name(self, s: Optional[str]) -> bool:
        """Weak heuristic: does this string have the shape of an artist name
        (a single word, or a short title-cased phrase) rather than a full title?
        """
        s = (s or '').strip()
        if not s:
            return False
        if ' ' not in s:
            return True
        words = s.split()
        return len(words) <= 3 and all(w[:1].isupper() for w in words if w)

    def _names_similar(self, a: Optional[str], b: Optional[str], threshold: float = 0.82) -> bool:
        """Fuzzy-compare two names after stripping punctuation/case, for detecting
        when one field's value is really just a re-statement of another."""
        a_norm = re.sub(r'[^a-z0-9]', '', (a or '').lower())
        b_norm = re.sub(r'[^a-z0-9]', '', (b or '').lower())
        if not a_norm or not b_norm:
            return False
        return SequenceMatcher(None, a_norm, b_norm).ratio() >= threshold

    def _ai_resolve_artist_title_order(
        self,
        candidate_a: str,
        candidate_b: str,
        context: Optional[str] = None,
    ) -> Optional[Tuple[str, str]]:
        """Decide which of two ambiguous strings is the artist and which the title.

        Used for video titles written "Title - Artist" where both halves are
        short enough that the cheap heuristic cannot tell them apart.
        """
        candidate_a = (candidate_a or '').strip()
        candidate_b = (candidate_b or '').strip()
        if not candidate_a or not candidate_b or candidate_a.lower() == candidate_b.lower():
            return None

        context_hint = f"\nAdditional context: {context.strip()}" if context else ''
        prompt = (
            "Two strings were extracted from an ambiguous music video title, but it's "
            "unclear which one is the performing artist and which one is the track title "
            "(the video title may be written backwards, as \"Title - Artist\" instead of "
            "\"Artist - Title\").\n"
            f'String A: "{candidate_a}"\n'
            f'String B: "{candidate_b}"\n'
            "Use your knowledge of real musicians and songs to decide which string is the "
            "artist and which is the title. If you don't recognize either string confidently "
            "enough to be sure, set \"confident\" to false." + context_hint + "\n"
            "Return only valid JSON in the form "
            '{"artist": "<the string that is the artist>", "title": "<the string that is the title>", '
            '"confident": true|false}.'
        )
        parsed = self._ask_local_ai(
            'You identify which of two strings is a music artist name vs. a song title. Return compact JSON only.',
            prompt,
        )
        if not parsed or not parsed.get('confident', True):
            return None
        ai_artist = (parsed.get('artist') or '').strip()
        ai_title = (parsed.get('title') or '').strip()
        candidates_lower = {candidate_a.lower(), candidate_b.lower()}
        if ai_artist.lower() in candidates_lower and ai_title.lower() in candidates_lower and ai_artist.lower() != ai_title.lower():
            return ai_artist, ai_title
        return None

    def _ai_confirms_uploader_is_performer(
        self,
        uploader: str,
        known_artist: str,
        video_title: str = '',
    ) -> bool:
        """Whether a video's uploader channel is plausibly the performing artist.

        Most channels that re-upload a song (lyric videos, reaction channels,
        fan reposts) are not the performer, so the default when unsure is False.
        """
        uploader = (uploader or '').strip()
        known_artist = (known_artist or '').strip()
        if not uploader or not known_artist:
            return False

        title_hint = f' Video title: "{video_title}".' if video_title else ''
        prompt = (
            f'A YouTube video believed to be the song "{known_artist}" was uploaded by a '
            f'channel named "{uploader}".{title_hint}\n'
            "Most channels that re-upload someone else's song (lyric videos, reaction "
            "channels, fan reposts, compilation channels) are NOT the performing artist. "
            f'Using your knowledge of real musicians, is "{uploader}" plausibly the actual '
            f'performing artist or a known alias/handle of "{known_artist}" — for example a '
            "different real performer covering the song, or the artist's own channel?\n"
            'Return only valid JSON in the form {"is_performer": true|false, "reason": "brief explanation"}.'
        )
        parsed = self._ask_local_ai(
            'You judge whether a YouTube channel name is the actual performing artist of an uploaded song. Return compact JSON only.',
            prompt,
        )
        return bool((parsed or {}).get('is_performer', False))

    def _resolve_effective_download_metadata(self, artist: str, title: str, album: Optional[str] = None, request_context: Optional[str] = None) -> Dict[str, Any]:
        """Resolve artist/title/album used for download decisions and output naming."""
        metadata = self._build_request_context_metadata(
            artist=artist,
            title=title,
            request_context=request_context,
            album=album,
        )
        resolved_artist = (metadata.get('artist') or artist or '').strip()
        resolved_title = (metadata.get('title') or title or '').strip()
        resolved_album = (metadata.get('album') or album or '').strip()
        if self._has_variant_request_hint(request_context):
            if resolved_title and resolved_title.lower() != title.lower():
                logger.info(f"🎯 Variant-aware output metadata: artist='{resolved_artist}', title='{resolved_title}', album='{resolved_album}'")
        return {
            'artist': resolved_artist,
            'title': resolved_title,
            'album': resolved_album,
        }

    def _resolve_organization_targets(
        self,
        metadata: Dict[str, Any],
        artist: str,
        title: str,
        album: Optional[str] = None,
        request_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve final organization metadata, favoring request-context overrides for variants."""
        request_context_metadata = self._resolve_effective_download_metadata(
            artist=artist,
            title=title,
            album=album,
            request_context=request_context,
        )

        final_artist = metadata.get('artist') or artist
        final_album = metadata.get('album') or album or title
        final_title = metadata.get('title') or title

        if self._has_variant_request_hint(request_context) and request_context_metadata.get('artist'):
            final_artist = request_context_metadata['artist']

        if request_context_metadata.get('album'):
            final_album = request_context_metadata['album']

        if request_context_metadata.get('title'):
            final_title = request_context_metadata['title']

        return {
            'artist': final_artist,
            'title': final_title,
            'album': final_album,
        }

    @staticmethod
    def _without_description(request_context: Optional[str]) -> str:
        """The request context without the linked video's description. That is
        text by whoever uploaded it, for the AI to read; text rules acting on
        it take "Provided to YouTube by ITNO/Columbia" for the performer and
        "Mix Engineer: …" for a remix."""
        return re.sub(r'(?:^|;\s*)video_description=[^;]*', '', request_context or '')

    def _has_variant_request_hint(self, request_context: Optional[str] = None) -> bool:
        """Return True when the request context explicitly implies a variant or alternate release."""
        if not request_context:
            return False

        lowered = self._without_description(request_context).strip().lower()
        if not lowered:
            return False

        lowered = re.sub(
            r'prefer the exact release/variant implied by the request if multiple versions exist',
            '',
            lowered,
            flags=re.IGNORECASE,
        ).strip()

        explicit_variant_patterns = [
            r'\b(?:arabic|live|acoustic|remix|explicit|clean|instrumental|deluxe)\b',
            r'\b(?:radio edit|re-record(?:ing)?|translated|translation|alternate|cover|mix)\b',
            r'\b(?:live|acoustic|clean|explicit|instrumental|arabic|deluxe|radio|translated|translation|alternate|cover)\s+(?:version|variant)\b',
        ]
        return any(re.search(pattern, lowered) for pattern in explicit_variant_patterns)

    def _is_official_youtube_request(
        self,
        request_context: Optional[str],
        expected_artist: str = '',
        expected_title: str = '',
    ) -> bool:
        if not request_context:
            return False

        fields = self._parse_request_context_fields(request_context)
        video_title = (fields.get('video_title') or '').strip().lower()
        video_uploader = (fields.get('video_uploader') or '').strip().lower()
        if not video_title or not video_uploader:
            return False

        expected_artist_norm = (expected_artist or '').strip().lower()
        expected_title_norm = (expected_title or '').strip().lower()
        if not expected_artist_norm or not expected_title_norm:
            return False

        if expected_artist_norm != video_uploader:
            return False

        if 'official' not in video_title:
            return False

        return expected_title_norm in video_title

    def _clean_deemix_query(self, query: str) -> str:
        variant_label_re = re.compile(
            r'\b(?:arabic|live|acoustic|remix|explicit|clean|instrumental|deluxe|radio edit|re-recording|re-record|cover|translated|translation|alternate|mix|version)\b',
            flags=re.IGNORECASE,
        )

        def _replacer(match):
            inner = match.group(1)
            return match.group(0) if variant_label_re.search(inner) else ''

        cleaned = re.sub(r'\s*[\[\(]([^\[\]\(\)]{0,60})[\]\)]', _replacer, query, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r'\s*[\[\(][^\)\]]*$', '', cleaned).strip()
        cleaned = re.sub(r"\s*[“\"'\u2018\u2019][^“\"'\u2018\u2019]{0,80}[”\"'\u2018\u2019]$", "", cleaned).strip()
        return cleaned

    def _merge_request_context_metadata(
        self,
        external_metadata: Dict[str, Any],
        request_context_metadata: Dict[str, Any],
        artist: str,
        title: str,
        album: Optional[str] = None,
    ) -> Dict[str, Any]:
        merged = dict(external_metadata or {})
        if not request_context_metadata:
            return merged

        for field in ('artist', 'title', 'album'):
            requested_value = (request_context_metadata.get(field) or '').strip()
            existing_value = (merged.get(field) or '').strip()
            if not requested_value:
                continue

            # The request names a performer and a track, never an album: the
            # album comes from a lookup and is no better than the one the
            # downloader embedded. Replacing it splits an album across two
            # folders whenever the sources spell it differently.
            if field == 'album' and existing_value:
                continue

            # Prefer explicit request-context overrides when the requested
            # value differs from existing metadata or when the external
            # metadata is missing.
            if not existing_value or requested_value.lower() != existing_value.lower():
                merged[field] = requested_value
        return merged

    def _parse_request_context_fields(self, request_context: str) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        if not request_context:
            return fields
        for part in re.split(r';\s*', request_context.strip()):
            if '=' not in part:
                continue
            key, _, value = part.partition('=')
            fields[key.strip().lower()] = value.strip()
        return fields

    def _parse_artist_title_pair(self, text: str) -> Tuple[str, str]:
        text = (text or '').strip()
        # Normalize em-dash/en-dash separators (common in Russian/Ukrainian and
        # other non-English YouTube titles, e.g. "Rauf & Faik — детство") to a
        # plain ASCII hyphen so they split the same way "Artist - Title" does.
        # Without this, video_title parsing in _build_request_context_metadata
        # treats the whole string as an unsplit title, re-gluing the artist
        # name back onto an otherwise-correct title from upstream parsing.
        normalized = re.sub(r'\s[-–—]\s', ' - ', text)
        if ' - ' in normalized:
            artist_part, title_part = normalized.split(' - ', 1)
            return artist_part.strip(), title_part.strip()
        return '', text

    # Whole-word matches only: a bare substring test would turn "The Word
    # Alive" into a "live" request and "Discover" into a "cover" one.
    _EXPLICIT_VARIANT_LABELS = [
        (re.compile(r'\barabic\b', re.I), 'Arabic Version'),
        (re.compile(r'\blive\b', re.I), 'Live Version'),
        (re.compile(r'\bacoustic\b', re.I), 'Acoustic Version'),
        (re.compile(r'\bremix\b', re.I), 'Remix'),
        (re.compile(r'\bexplicit\b', re.I), 'Explicit Version'),
        (re.compile(r'\bclean\b', re.I), 'Clean Version'),
        (re.compile(r'\binstrumental\b', re.I), 'Instrumental Version'),
        (re.compile(r'\bdeluxe\b', re.I), 'Deluxe Version'),
        (re.compile(r'\bradio edit\b', re.I), 'Radio Edit'),
        (re.compile(r'\bre-record(?:ing)?\b', re.I), 'Re-recording'),
        (re.compile(r'\bcover\b', re.I), 'Cover'),
        (re.compile(r'\btranslat(?:ed|ion)\b', re.I), 'Translated Version'),
        (re.compile(r'\balternate\b', re.I), 'Alternate Version'),
        (re.compile(r'\bmix\b', re.I), 'Mix'),
    ]

    # Versions that are not "the song" at all: only vocals, or only the backing.
    _NOT_THE_SONG = {'acapella', 'instrumental'}

    @staticmethod
    def _context_fields(request_context: Optional[str]) -> Dict[str, str]:
        """The `key=value; …` fields of a request context."""
        fields = {}
        for part in (request_context or '').split('; '):
            key, sep, value = part.partition('=')
            if sep and key.strip():
                fields[key.strip()] = value.strip()
        return fields

    @staticmethod
    def _variants_of(title: str) -> set:
        """Version labels in a title's brackets or trailing " - …" part.
        "Original Mix" / "Original Version" name the original, "Cover Art" is
        not a cover, and "Explicit" is the same recording."""
        from song_dedup import variant_labels
        cleaned = re.sub(r'\boriginal (?:mix|version|edit)\b|\bcover art\b', '', title or '', flags=re.I)
        return set(variant_labels(cleaned)) - {'explicit'}

    def _requested_variants(self, title: str, request_context: Optional[str]) -> set:
        """The versions a request asks for: a label in the requested title, in
        the linked video's own title ("Song (Live at Wembley)", "Song -
        Acapella"), or in the requester's message ("the acapella please").
        A word that is just part of a song's name ("Live Your Life") is not one."""
        from song_dedup import _VARIANT_RES
        fields = self._context_fields(request_context)
        video_title = fields.get('video_title', '')
        # "Artist - Title (…)": the part before " - " is the artist, not a
        # label; and in "Song | Live at Wembley" what follows a "|" describes
        # the upload.
        head, *segments = video_title.split('|')
        song = head.split(' - ', 1)[1] if ' - ' in head else head
        wanted = self._variants_of(title) | self._variants_of(' '.join([song] + [f"({p})" for p in segments]))
        message = re.sub(r'https?://\S+', ' ', fields.get('message', ''))
        wanted |= {name for name, rx in _VARIANT_RES.items() if rx.search(message)}
        return wanted - {'explicit'}

    def _get_explicit_variant_label(self, text: Optional[str]) -> Optional[str]:
        text = self._without_description(text)
        if not text:
            return None
        for pattern, label in self._EXPLICIT_VARIANT_LABELS:
            if pattern.search(text):
                return label
        return None

    def _normalize_variant_title(self, title: Optional[str]) -> str:
        if not title:
            return ''
        text = title.strip()

        # Normalize multi-language titles by keeping the English portion only.
        if re.search(r'[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]', text):
            text = re.sub(
                r'([A-Za-z0-9 \-=\(\)\[\],:;!?.&]+?)\s*[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF].*$',
                r'\1',
                text,
            ).strip()

        explicit_label = self._get_explicit_variant_label(text)
        if explicit_label:
            placeholder = '___VARIANT_LABEL___'
            label_pattern = re.compile(
                r'\((?:arabic version|arabic|live version|live|acoustic version|acoustic|remix|explicit version|explicit|clean version|clean|instrumental version|instrumental|deluxe version|deluxe|radio edit|re-recording|re-record|cover|translated version|translated|translation|alternate version|alternate|mix|version)\)',
                flags=re.IGNORECASE,
            )
            # A parenthetical that spells the variant out ("(Power Glove
            # Remix)") says more than the bare label, so keep its wording.
            descriptive = re.search(r'\(([^()]*\b' + re.escape(explicit_label) + r'\b[^()]*)\)', text, re.IGNORECASE)
            if descriptive:
                explicit_label = descriptive.group(1).strip()
                text = text[:descriptive.start()] + placeholder + text[descriptive.end():]
            else:
                text = label_pattern.sub(placeholder, text)
            text = re.sub(rf'{re.escape(placeholder)}.*$', placeholder, text, flags=re.IGNORECASE).strip()
            text = re.sub(r'\s*\[.*?\]\s*$', '', text).strip()
            text = re.sub(r'\s*\([^\)]*\)\s*$', '', text).strip()
            text = text.replace(placeholder, f'({explicit_label})')
        else:
            # Clean trailing noise from titles that do not explicitly name a variant.
            text = re.sub(r'\s*\[[^\]]*\]\s*$', '', text).strip()
            text = re.sub(r'\s*\([^\)]*\)\s*$', '', text).strip()

        # Remove duplicate explicit labels if they appear twice
        if explicit_label:
            normalized_label = f'({explicit_label})'
            text = re.sub(
                rf'(?:{re.escape(normalized_label)}\s*){{2,}}',
                normalized_label,
                text,
                flags=re.IGNORECASE,
            ).strip()
            if text.lower().count(normalized_label.lower()) > 1:
                cleaned = re.sub(re.escape(normalized_label), '', text, flags=re.IGNORECASE).strip()
                text = f'{cleaned} {normalized_label}'.strip()

        return text or title.strip()

    def _clean_multilingual_name(self, text: str) -> str:
        if not text:
            return ''
        cleaned = text.strip()

        if re.search(r'[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]', cleaned):
            cleaned = re.sub(
                r'([A-Za-z0-9 \-=_\(\)\[\],:;!?.&]+?)\s*[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF].*$',
                r'\1',
                cleaned,
            ).strip()

        cleaned = re.sub(r'\s*\[.*?\]\s*$', '', cleaned).strip()
        cleaned = re.sub(r'\s*\([^\)]*\)\s*$', '', cleaned).strip()
        return cleaned or text.strip()

    def _build_request_context_metadata(
        self,
        artist: str,
        title: str,
        request_context: Optional[str] = None,
        album: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Derive a more accurate artist/title from explicit variant hints in the request context."""
        metadata = {
            'artist': artist or '',
            'title': title or '',
            'album': album or '',
        }
        if not request_context:
            return metadata

        ctx = self._without_description(request_context).strip()
        if not ctx:
            return metadata

        lowered = ctx.lower()
        has_variant_hint = self._has_variant_request_hint(ctx)
        # Do not bail early: even when an explicit variant hint isn't present,
        # prefer video_title / video_uploader / requested_track when they exist
        # because they can contain the cover performer or variant label.

        fields = self._parse_request_context_fields(ctx)
        requested_track = fields.get('requested_track', '')
        video_title = fields.get('video_title', '')
        video_uploader = fields.get('video_uploader', '')
        message_text = fields.get('message', '')

        # Prefer explicit artist/title from the structured request context.
        if requested_track:
            requested_artist, requested_title = self._parse_artist_title_pair(requested_track)
            if requested_artist:
                metadata['artist'] = requested_artist
            if requested_title:
                metadata['title'] = self._normalize_variant_title(requested_title)

        if video_title:
            video_artist, parsed_title = self._parse_artist_title_pair(video_title)

            if video_artist and parsed_title:
                # `_parse_artist_title_pair` assumes "Artist - Title" order, but some
                # uploaders write video titles backwards ("Title - Artist"). If we
                # already have a confident artist/title from `requested_track` (built
                # upstream from link_extractor's own swap-aware parsing), detect a
                # reversed video_title by checking whether it's really just the same
                # pair flipped — and if so, keep the already-resolved values instead
                # of clobbering them.
                current_artist = (metadata.get('artist') or '').strip()
                current_title = (metadata.get('title') or '').strip()
                # Compare core names only: a video half like "RISE (ft. …) |
                # Worlds 2018" must still match the resolved title "RISE".
                def _core(s: str) -> str:
                    s = s.split('|', 1)[0]
                    s = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', s)
                    return s.strip()
                reversed_order = bool(
                    current_artist and current_title
                    and self._names_similar(_core(video_artist), _core(current_title))
                    and self._names_similar(_core(parsed_title), _core(current_artist))
                )
                if reversed_order:
                    logger.info(
                        f"🔀 video_title '{video_title}' looks reversed (Title - Artist); "
                        f"keeping resolved '{current_artist} - {current_title}' instead"
                    )
                    video_artist, parsed_title = '', ''
                elif not (current_artist and current_title):
                    # No prior anchor (e.g. no requested_track) to compare against —
                    # if the heuristic can't tell which side is the artist (both look
                    # like plausible artist names, or neither does), ask the AI rather
                    # than guessing the "Artist - Title" default blindly.
                    artist_like = self._looks_like_artist_name(video_artist)
                    title_like = self._looks_like_artist_name(parsed_title)
                    if artist_like == title_like:
                        ai_order = self._ai_resolve_artist_title_order(
                            video_artist, parsed_title, context=video_title,
                        )
                        if ai_order:
                            logger.info(
                                f"🤖 AI resolved ambiguous video title order for '{video_title}': "
                                f"artist='{ai_order[0]}', title='{ai_order[1]}'"
                            )
                            video_artist, parsed_title = ai_order

            if parsed_title:
                parsed_title = song_in_quotes(parsed_title, metadata.get('artist'))
                metadata['title'] = self._normalize_variant_title(parsed_title)
            # If the video's title includes an artist different from the
            # requested artist, prefer it when the request context implies a
            # variant or when the parsed video artist is clearly different.
            if video_artist:
                if (not metadata['artist']) or self._has_variant_request_hint(ctx) or video_artist.strip().lower() not in metadata['artist'].strip().lower():
                    metadata['artist'] = video_artist

        # Prefer the video uploader as the performer for cover/variant uploads.
        # Use uploader when the request context explicitly hints at a variant
        # (e.g. "cover", "remix"), when we have no other artist signal at all,
        # or — when neither of those apply but the uploader still differs from
        # the known artist — when the AI confirms the uploader is plausibly the
        # actual performer rather than a reposting/lyric/fan channel. Most
        # channels that re-upload someone else's song are NOT the performer, so
        # that's the safe default when the AI can't confirm otherwise.
        if video_uploader:
            uploader = artist_from_channel(video_uploader)
            if uploader:
                current_artist = (metadata.get('artist') or '').strip()
                if uploader.lower() not in {current_artist.lower(), artist.lower()}:
                    has_variant_hint_ctx = self._has_variant_request_hint(ctx)
                    should_override = has_variant_hint_ctx or not current_artist
                    if not should_override:
                        should_override = self._ai_confirms_uploader_is_performer(
                            uploader, current_artist, video_title,
                        )
                        if should_override:
                            logger.info(
                                f"🤖 AI confirmed uploader '{uploader}' is plausibly the actual "
                                f"performer (not just a reposting channel)"
                            )
                    if should_override:
                        metadata['artist'] = uploader
                        logger.info(f"🎯 Request context overrides artist to '{uploader}' via video_uploader")
                    else:
                        logger.debug(
                            f"Uploader '{uploader}' looks like a reposting/lyric channel, not the "
                            f"performer — keeping artist '{current_artist}'"
                        )

        # Prefer explicit artist hints when the request context names a performer.
        # Links carry no artist name, and leaving them in lets the "by ..."
        # match run straight into the URL (e.g. "by Amy Lee https").
        cleaned_ctx = re.sub(r'https?://[^\s;]+', ' ', ctx)
        # Only what the requester wrote can name a performer. The link's own
        # fields can't: "album=… Bobby Ray" would make the artist "Ray", and
        # "video_title=Stand by Me" the artist "Me".
        cleaned_ctx = re.sub(r'\b(?!message=)[a-z_]+=[^;]*', ' ', cleaned_ctx, flags=re.IGNORECASE)
        cleaned_ctx = re.sub(r'\bprefer\b.*?\brequest\b', '', cleaned_ctx, flags=re.IGNORECASE).strip()
        cleaned_ctx = re.sub(r'\b(?:source|url|track|album|released?|release|requested|request|implied|exact|variant|version|alternate)\b', '', cleaned_ctx, flags=re.IGNORECASE).strip()
        cleaned_ctx = re.sub(r'\s+', ' ', cleaned_ctx).strip(' ;:')
        # "by" as a word: the "by" inside "Bobby" is no credit.
        # The last one: "Stand by Me by Ben E. King".
        artist_match = re.search(r'.*\bby\s+([A-Za-z0-9 \u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF.&\-_,()]+)', cleaned_ctx, re.IGNORECASE | re.DOTALL)
        if artist_match:
            suggested_artist = artist_match.group(1).strip(' ,.;:-')
            # A typed "Stand by Me" says "by Me": that is the song's own title.
            in_title = self._deemix_norm(suggested_artist) in self._deemix_norm(metadata.get('title') or '')
            if suggested_artist and not in_title and suggested_artist.lower() not in {'the weeknd', 'weeknd'} and not re.search(r'\b(?:version|variant|request|release|prefer|source|url|album|track|implied|exact)\b', suggested_artist, re.IGNORECASE):
                suggested_artist = self._clean_multilingual_name(suggested_artist)
                metadata['artist'] = suggested_artist
                logger.info(f"🎯 Request context overrides artist to '{suggested_artist}'")

        explicit_label = self._get_explicit_variant_label(ctx) or self._get_explicit_variant_label(video_title) or self._get_explicit_variant_label(message_text)
        if explicit_label and metadata.get('title'):
            normalized_title = metadata['title'].strip()
            # A title that already names the variant ("… (Power Glove Remix)")
            # is more precise than the generic label, so leave it alone.
            already_labelled = any(word in normalized_title.lower()
                                   for word in explicit_label.lower().split())
            if not already_labelled:
                title_base = re.sub(r'\s*\((?:arabic version|arabic|live version|live|acoustic version|acoustic|remix|explicit version|explicit|clean version|clean|instrumental version|instrumental|deluxe version|deluxe|radio edit|re-recording|re-record|cover|translated version|translated|translation|alternate version|alternate|mix|version)\)$', '', metadata['title'], flags=re.IGNORECASE).strip()
                title_base = re.sub(r'\s*\[(?:arabic version|arabic|live version|live|acoustic version|acoustic|remix|explicit version|explicit|clean version|clean|instrumental version|instrumental|deluxe version|deluxe|radio edit|re-recording|re-record|cover|translated version|translated|translation|alternate version|alternate|mix|version)\]$', '', title_base, flags=re.IGNORECASE).strip()
                if metadata['title'].lower() == title_base.lower():
                    metadata['title'] = f"{title_base} ({explicit_label})"
                    logger.info(f"🎯 Request context overrides title to '{metadata['title']}'")

        return metadata

    # ── deemix ────────────────────────────────────────────────────────────────

    # Enough to separate the same recording on two releases, too small to
    # outrank a genuinely better artist/title match.
    _ALBUM_MATCH_BONUS = 0.15

    _DEEMIX_POLL_TIMEOUT = 70   # deemix has a ~60 s internal timeout; allow a little slack
    _DEEMIX_RESCUE_ROOTS = ('/downloads', '/tmp', '/music')

    @staticmethod
    def _deemix_config_folder():
        from pathlib import Path
        return Path.home() / '.config' / 'deemix'

    def _deemix_arl(self) -> str:
        """The ARL file is rewritten live by the ARL refresh server, so it wins
        over DEEZER_ARL, which is frozen at container start."""
        arl_file = self._deemix_config_folder() / '.arl'
        try:
            if arl_file.exists():
                arl = arl_file.read_text().strip()
                if arl:
                    return arl
        except Exception:
            pass
        return os.environ.get('DEEZER_ARL', '').strip()

    def _deemix_login(self, arl: str, status_label: str):
        """Return an authenticated Deezer session, or None.

        deemix requests are routed through the VPN SOCKS proxy on the session
        object itself rather than via os.environ, which would leak into other
        threads. A False from login_via_arl does not by itself mean the ARL is
        dead — Deezer sometimes hands back an anonymous session for a valid
        token — so the staleness probe decides whether to alert the admin or
        simply retry on a fresh session.
        """
        from deezer import Deezer

        def new_session():
            dz = Deezer()
            proxy = getattr(self, 'socks5_proxy', None)
            if proxy:
                try:
                    dz.session.proxies = {'http': proxy, 'https': proxy}
                except Exception as exc:
                    logger.debug(f"🎵 [DEEMIX] Could not set session proxy: {exc}")
            return dz

        dz = new_session()
        started = time.time()
        self._write_status(status_label, "deemix", "authenticating")
        if dz.login_via_arl(arl):
            logger.info(f"🎵 [DEEMIX] ARL auth OK in {time.time() - started:.1f}s")
            return dz

        logger.warning(f"[DEEMIX] login_via_arl returned False (len={len(arl)}) — running staleness probe")
        if self._is_arl_genuinely_stale():
            self._arl_stale = True
            try:
                from downloader_manager import set_arl_stale, write_arl_alert
                set_arl_stale(True)
                write_arl_alert()
            except Exception as exc:
                logger.debug(f"Could not signal ARL stale cross-process: {exc}")
            return None

        for attempt in (1, 2):
            time.sleep(attempt * 1.5)
            try:
                dz = new_session()
                if dz.login_via_arl(arl):
                    logger.info(f"[DEEMIX] Re-login succeeded on attempt {attempt}")
                    return dz
            except Exception as exc:
                logger.debug(f"[DEEMIX] Re-login attempt {attempt} error: {exc}")
        logger.warning("[DEEMIX] ARL is valid but every login produced an anonymous session; falling back to next tool")
        return None

    @staticmethod
    def _deemix_norm(s: str) -> str:
        # Letters of any script count: an ASCII-only filter turns every
        # Cyrillic or Arabic title into "" and makes them all match each other.
        s = re.sub(r'[\W_]+', ' ', (s or '').lower())
        return re.sub(r'\s+', ' ', s).strip()

    def _deemix_pick_track(self, dz, query_clean: str, request_context: Optional[str],
                           expected_artist: Optional[str], expected_title: Optional[str],
                           expected_album: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Search Deezer and choose the track to download, or None.

        Deezer's own ranking is not a relevance ranking, so every result is
        scored on title/artist similarity. Results whose artist barely
        resembles the requested one are discarded outright ("8-Bit Arcade" for
        a Green Day request). When several plausible candidates remain, the
        local AI picks between them.
        """
        results = dz.gw.search_music(query_clean, type='TRACK', limit=5)
        tracks = results.get('data', []) or []
        if not tracks:
            logger.debug(f"🎵 [DEEMIX] No tracks found for: {query_clean}")
            return None

        parts = query_clean.split(' - ', 1)
        q_artist = self._deemix_norm(parts[0]) if len(parts) == 2 else ''
        q_title = self._deemix_norm(parts[1]) if len(parts) == 2 else self._deemix_norm(query_clean)
        want_artist = expected_artist or (parts[0] if len(parts) == 2 else '')
        want_title = expected_title or (parts[1] if len(parts) == 2 else query_clean)

        q_album = self._deemix_norm(expected_album or '')
        best_track, best_score, candidates = None, -1.0, []
        wanted = self._requested_variants(want_title, request_context)
        for t in tracks:
            # Deezer keeps a version ("(Acapella)", "(Live)", "(Extended Mix)")
            # apart from the title, so a song's every version shows the same
            # SNG_TITLE. The full title is what gets compared and shown to the AI.
            # Only the version the request names will do: none of the others,
            # and when the video is a live or acapella one, nothing else either
            # (without it on Deezer, the next tool gets the video itself).
            full_title = ' '.join(p for p in (t.get('SNG_TITLE', ''), t.get('VERSION', '')) if p).strip()
            have = self._variants_of(full_title)
            if have - wanted or wanted - have:
                logger.debug(f"🎵 [DEEMIX] Rejected (version {sorted(have) or 'original'}, requested "
                             f"{sorted(wanted) or 'original'}): {t.get('ART_NAME')} - {full_title}")
                continue
            title_sim = SequenceMatcher(None, self._deemix_norm(t.get('SNG_TITLE', '')), q_title).ratio()
            artist_sim = SequenceMatcher(None, self._deemix_norm(t.get('ART_NAME', '')), q_artist).ratio() if q_artist else 0.5
            if q_artist and artist_sim < 0.50:
                logger.debug(f"🎵 [DEEMIX] Rejected (artist mismatch {artist_sim:.2f}): {t.get('ART_NAME')} - {t.get('SNG_TITLE')}")
                continue
            score = 0.65 * title_sim + 0.35 * artist_sim
            # The same recording is often on several releases (an album and
            # the EP that preceded it). When the request already knows which
            # album it belongs to, that release wins, so the tracks of one
            # request stay together instead of splitting across albums.
            if q_album:
                album_sim = SequenceMatcher(None, self._deemix_norm(t.get('ALB_TITLE', '')), q_album).ratio()
                if album_sim >= 0.90:
                    score += self._ALBUM_MATCH_BONUS
            logger.debug(f"🎵 [DEEMIX] Candidate: {t.get('ART_NAME')} - {full_title} score={score:.2f}")
            candidates.append({
                'artist': t.get('ART_NAME', ''), 'title': full_title,
                'duration': int(t.get('DURATION') or 0),
                'album': t.get('ALB_TITLE', ''), 'source': 'deezer', 'raw': t,
            })
            if score > best_score:
                best_score, best_track = score, t

        if not candidates:
            return None

        # Only variant requests get the AI "are these results good enough"
        # gate; ordinary requests should not be blocked on a generic judgement.
        if len(candidates) >= 2 and self._has_variant_request_hint(request_context):
            if not self._should_continue_with_ai(artist=want_artist, title=want_title,
                                                 candidates=candidates, request_context=request_context):
                logger.info("🤖 [DEEMIX] AI deemed the search results too inaccurate; falling back to the next downloader")
                return None

        # A request that came from the artist's own official YouTube upload
        # wants the original recording: take an exact artist+title match
        # before consulting the AI.
        official_request = self._is_official_youtube_request(request_context, want_artist, want_title)
        if official_request:
            compact = lambda s: self._deemix_norm(s).replace(' ', '')
            for candidate in candidates:
                if compact(q_artist) and compact(q_title) and compact(candidate['artist']) == compact(q_artist) \
                        and compact(candidate['title']) == compact(q_title):
                    logger.info("🎯 Official YouTube request: selecting exact original-song candidate")
                    return candidate['raw']

        if request_choice.can_ask():
            fallback = next((c for c in candidates if c['raw'] is best_track), None)
            choice = self._choose_or_ask(want_artist, want_title, candidates, request_context, 'Deezer', fallback)
            return choice['raw'] if choice else None

        if official_request or len(candidates) > 1:
            choice = self._select_best_candidate_with_ai(artist=want_artist, title=want_title,
                                                         candidates=candidates, request_context=request_context)
            if choice and choice.get('raw') is not None:
                logger.info(f"🤖 [DEEMIX] AI selected: {choice['artist']} - {choice['title']}")
                return choice['raw']
        return best_track

    def _deemix_settings(self, target_dir: str):
        """Return (settings, restore) for a download into target_dir.

        deemix reads downloadLocation and overwriteFile from config.json on
        disk at download time, ignoring the in-memory settings, so the file is
        patched for the duration of the download and put back by restore().
        overwriteFile='y' stops deemix consulting its history database and
        silently skipping tracks it downloaded before.
        """
        from deemix.settings import load as load_settings
        folder = self._deemix_config_folder()
        overrides = {
            'downloadLocation': target_dir, 'maxBitrate': '320', 'overwriteFile': 'y',
            'saveArtworkArtist': False, 'saveArtwork': False,
        }
        settings = load_settings(folder)
        settings.update(overrides)

        cfg_file = folder / 'config.json'
        original = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            disk_cfg = json.loads(cfg_file.read_text()) if cfg_file.exists() else {}
            original = {k: disk_cfg.get(k) for k in ('downloadLocation', 'overwriteFile')}
            disk_cfg.update(overrides)
            cfg_file.write_text(json.dumps(disk_cfg, indent=2))
        except Exception as exc:
            logger.warning(f"⚠️ [DEEMIX] Could not patch config.json: {exc}")

        def restore():
            if original is None:
                return
            try:
                disk_cfg = json.loads(cfg_file.read_text())
                for key, value in original.items():
                    if value is None:
                        disk_cfg.pop(key, None)
                    else:
                        disk_cfg[key] = value
                cfg_file.write_text(json.dumps(disk_cfg, indent=2))
            except Exception as exc:
                logger.warning(f"⚠️ [DEEMIX] Could not restore config.json: {exc}")

        return settings, restore

    def _audio_files_under(self, directory: str) -> set:
        found = set()
        try:
            for root, _dirs, files in os.walk(directory):
                for f in files:
                    if f.endswith(self._AUDIO_EXTENSIONS):
                        found.add(os.path.join(root, f))
        except Exception:
            pass
        return found

    def _deemix_rescue(self, query_clean: str, target_dir: str, roots: Tuple[str, ...]) -> Optional[str]:
        """deemix sometimes ignores the patched download location; look for a
        file that names the track under `roots` and move it into target_dir."""
        artist_tok = (query_clean.split(' - ')[0] if ' - ' in query_clean else query_clean)[:12].lower()
        title_tok = (query_clean.split(' - ')[1] if ' - ' in query_clean else query_clean)[:12].lower()
        target_abs = os.path.abspath(target_dir)
        for root_dir in roots:
            if not os.path.isdir(root_dir):
                continue
            try:
                for root, dirs, files in os.walk(root_dir):
                    dirs[:] = [d for d in dirs if os.path.abspath(os.path.join(root, d)) != target_abs]
                    for f in files:
                        fl = f.lower()
                        if not fl.endswith(self._AUDIO_EXTENSIONS) or not (artist_tok in fl or title_tok in fl):
                            continue
                        src, dst = os.path.join(root, f), os.path.join(target_dir, f)
                        try:
                            os.makedirs(target_dir, exist_ok=True)
                            shutil.move(src, dst)
                            logger.warning(f"⚠️ [DEEMIX] File landed in {root_dir!r}; moved to target_dir: {f}")
                            return dst
                        except Exception as exc:
                            logger.warning(f"⚠️ [DEEMIX] Could not move {src}: {exc}")
            except Exception:
                pass
        return None

    def _deemix_wait_for_file(self, target_dir: str, files_before: set, started_at: float, query_clean: str) -> Optional[str]:
        """Poll until deemix has written the track. Because overwriteFile='y'
        may rewrite a path that already existed, a file counts as new when it
        is either absent from files_before or modified after the download
        started."""
        elapsed = 0
        while elapsed < self._DEEMIX_POLL_TIMEOUT:
            time.sleep(1)
            elapsed += 1
            files_after = self._audio_files_under(target_dir)
            new_files = files_after - files_before
            for fp in files_after & files_before:
                try:
                    if os.path.getmtime(fp) >= started_at:
                        new_files.add(fp)
                except OSError:
                    pass
            if new_files:
                path = sorted(new_files)[0]
                logger.info(f"✓ [DEEMIX] Downloaded: {os.path.basename(path)} ({elapsed}s)")
                return path
            # /downloads is deemix's default location and cheap to scan every
            # second; the wider search only runs once the poll has given up.
            rescued = self._deemix_rescue(query_clean, target_dir, ('/downloads',))
            if rescued:
                return rescued
        return self._deemix_rescue(query_clean, target_dir, self._DEEMIX_RESCUE_ROOTS + (os.path.expanduser('~'),))

    def _deemix_record_empty_download(self, query_clean: str) -> None:
        """Three auth-OK-but-empty downloads in a row can mean the ARL is dead
        even though login still succeeds; the probe decides whether that is
        the case or the tracks were simply unavailable/geo-blocked."""
        self._deemix_consecutive_empty += 1
        logger.info(f"✗ [DEEMIX] No audio file after {self._DEEMIX_POLL_TIMEOUT}s for: {query_clean} "
                    f"(consecutive empty: {self._deemix_consecutive_empty})")
        if self._deemix_consecutive_empty < 3:
            return
        if self._is_arl_genuinely_stale():
            self._arl_stale = True
            logger.warning("[DEEMIX] Probe confirmed ARL stale after consecutive empty downloads — pausing for admin refresh")
        else:
            logger.info("[DEEMIX] Probe says ARL is valid — consecutive empties are likely unavailable tracks")
        self._deemix_consecutive_empty = 0

    def _download_with_deemix(self, query: str, target_dir: str, request_context: Optional[str] = None, expected_artist: Optional[str] = None, expected_title: Optional[str] = None, expected_album: Optional[str] = None) -> Tuple[bool, Optional[str], bool]:
        """Download one track from Deezer via the deemix library.

        Returns (success, file_path, is_rate_limited).
        """
        query_clean = self._clean_deemix_query(query)
        logger.debug(f"🎵 [DEEMIX] Query: '{query}' → '{query_clean}'")

        try:
            arl = self._deemix_arl()
            if not arl:
                logger.info('🎵 [DEEMIX] No ARL configured (neither file nor env var), skipping deemix')
                return False, None, False

            dz = self._deemix_login(arl, query_clean)
            if dz is None:
                return False, None, False

            logger.info(f"🎵 [DEEMIX] Searching: {query_clean}")
            self._write_status(query_clean, "deemix", "searching")
            track = self._deemix_pick_track(dz, query_clean, request_context, expected_artist, expected_title, expected_album)
            if not track or not track.get('SNG_ID'):
                return False, None, False
            track_id = track['SNG_ID']
            logger.info(f"🎵 [DEEMIX] Found track: {track.get('ART_NAME', '?')} - {track.get('SNG_TITLE', '?')} (ID: {track_id})")

            return self._deemix_fetch(dz, track_id, target_dir, query_clean)

        except ImportError as exc:
            logger.warning(f"🎵 [DEEMIX] deemix/deezer library not available: {exc}")
            return False, None, False
        except Exception as exc:
            logger.warning(f"🎵 [DEEMIX] Unexpected error: {exc}", exc_info=True)
            return False, None, False


    def _deemix_fetch(self, dz, track_id, target_dir: str, query_clean: str) -> Tuple[bool, Optional[str], bool]:
        """Download one Deezer track by ID into target_dir. Returns
        (success, file_path, is_rate_limited)."""
        from deemix.downloader import Downloader as DeemixDownloader
        from deemix import generateDownloadObject

        os.makedirs(target_dir, exist_ok=True)
        files_before = self._audio_files_under(target_dir)
        settings, restore_config = self._deemix_settings(target_dir)

        class SimpleListener:
            """deemix's event sink; some event payloads have __str__ methods
            that call send() again, hence the recursion guard."""
            _in_send = False

            def send(self, key, value=None):
                if SimpleListener._in_send:
                    return
                SimpleListener._in_send = True
                try:
                    if key and key not in ('updateQueue', 'currentItemUpdating'):
                        try:
                            val_str = str(value)[:120] if value is not None else ''
                        except Exception:
                            val_str = f'<{type(value).__name__}>'
                        logger.debug(f"  [deemix-event] {key}: {val_str}")
                finally:
                    SimpleListener._in_send = False

        listener = SimpleListener()
        track_url = f"https://www.deezer.com/track/{track_id}"
        try:
            download_obj = generateDownloadObject(dz, track_url, str(settings.get('maxBitrate', 320)), {}, listener)
            if not download_obj:
                logger.info(f"✗ [DEEMIX] generateDownloadObject returned None for {track_url}")
                return False, None, False

            self._write_status(query_clean, "deemix", "downloading")
            started_at = time.time()
            DeemixDownloader(dz, download_obj, settings, listener).start()

            downloaded = self._deemix_wait_for_file(target_dir, files_before, started_at, query_clean)
            if downloaded:
                self._deemix_consecutive_empty = 0
                return True, downloaded, False
            self._deemix_record_empty_download(query_clean)
            return False, None, False
        except Exception as exc:
            logger.info(f"✗ [DEEMIX] Download exception for {query_clean}: {type(exc).__name__}: {exc}", exc_info=True)
            text = str(exc).lower()
            return False, None, ('rate limit' in text or '429' in text or 'too many requests' in text)
        finally:
            restore_config()

    def download_deezer_track(self, track_id, target_dir: str) -> Optional[str]:
        """Download this exact Deezer track (no search) into target_dir; used
        to replace a music-video rip or a truncated file with the release."""
        try:
            arl = self._deemix_arl()
            dz = self._deemix_login(arl, f"track {track_id}") if arl else None
            if dz is None:
                return None
            ok, path, _ = self._deemix_fetch(dz, str(track_id), target_dir, f"track {track_id}")
            return path if ok else None
        except Exception as exc:
            logger.warning(f"🎵 [DEEMIX] Could not download track {track_id}: {exc}")
            return None

    @staticmethod
    def _only_a_preview(entry: Dict[str, Any]) -> bool:
        """SoundCloud offers a subscribers' track only as its first 30 seconds."""
        formats = entry.get('formats') or []
        return bool(formats) and all('preview' in str(f.get('format_id') or '').lower() for f in formats)

    def _download_with_soundcloud(self, query: str, target_dir: str,
                                  request_context: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """
        Download track from SoundCloud using yt-dlp.
        Performs fuzzy search to find tracks regardless of uploader. For a
        requester who can be asked (request_choice), up to five results are
        weighed by the AI, and the requester chooses when it is not sure.
        
        Args:
            query: "Artist - Title" search query
            target_dir: Target download directory
        
        Returns:
            (success, file_path) tuple
        """
        
        # Parse artist and title from query
        if ' - ' in query:
            artist_part, title_part = query.split(' - ', 1)
            artist_part = artist_part.strip()
            title_part = title_part.strip()
        else:
            artist_part = ''
            title_part = query.strip()
        
        try:
            from yt_dlp import YoutubeDL
            
            logger.debug(f"🎵 [SOUNDCLOUD] Searching: {query}")
            os.makedirs(target_dir, exist_ok=True)
            
            # Try different search queries in order of preference
            asking = request_choice.can_ask()
            search = "scsearch5" if asking else "scsearch"
            search_queries = [
                f"{search}:{artist_part} {title_part}" if artist_part else f"{search}:{title_part}",
                f"{search}:{title_part}"
            ]
            
            best_url = None
            best_score = 0
            found: List[Dict[str, Any]] = []
            
            for search_query in search_queries:
                try:
                    logger.debug(f"🎵 [SOUNDCLOUD] Trying search: {search_query}")
                    
                    # Get metadata without downloading
                    ydl_opts_info = {
                        'quiet': True,
                        'no_warnings': True,
                        'socket_timeout': 10,
                        'noplaylist': True,
                        'ignoreerrors': True,      # one DRM-locked track must not end the search
                    }
                    
                    with YoutubeDL(ydl_opts_info) as ydl:
                        info = ydl.extract_info(search_query, download=False)
                    if info and 'entries' in info:
                        entries = [e for e in (info['entries'] or []) if e][:5 if asking else 1]
                    else:
                        entries = [info] if info else []

                    for entry in entries:
                        if self._only_a_preview(entry):
                            logger.info(f"🎵 [SOUNDCLOUD] Skipping '{entry.get('title')}': only a preview is offered")
                            continue
                        track_title = (entry.get('title') or '').lower()
                        uploader = (entry.get('uploader') or '').lower()
                        description = (entry.get('description') or '').lower()
                        
                        # Fuzzy match title
                        title_similarity = SequenceMatcher(None, title_part.lower(), track_title).ratio()
                        
                        # Check if artist is mentioned (in title, uploader, or description)
                        combined_text = f"{track_title} {uploader} {description}"
                        artist_present = artist_part.lower() in combined_text if artist_part else True
                        
                        # Calculate score
                        score = title_similarity * (1.2 if artist_present else 1.0)
                        
                        logger.debug(f"🎵 [SOUNDCLOUD] Found: '{entry.get('title', 'Unknown')}' by {entry.get('uploader', 'Unknown')} - score: {score:.2f}")
                        url = entry.get('webpage_url') or entry.get('url')
                        if url and all(f['url'] != url for f in found):
                            found.append({'artist': entry.get('uploader') or '', 'title': entry.get('title') or '',
                                          'duration': int(entry.get('duration') or 0), 'source': 'soundcloud',
                                          'url': url, 'score': score})
                        
                        if score > best_score and score > 0.5:  # Minimum threshold
                            best_score = score
                            best_url = url
                            logger.debug(f"🎵 [SOUNDCLOUD] Selected match with score {score:.2f}")
                    
                except Exception as e:
                    logger.debug(f"🎵 [SOUNDCLOUD] Search failed for {search_query}: {str(e)[:100]}")
                    continue

            if asking and found:
                pool = sorted(found, key=lambda f: -f['score'])[:5]
                fallback = next((f for f in pool if f['url'] == best_url), None)
                chosen = self._choose_or_ask(artist_part, title_part, pool, request_context, 'SoundCloud', fallback)
                best_url = chosen['url'] if chosen else None
            
            if not best_url:
                logger.info(f"🎵 [SOUNDCLOUD] No suitable match found for: {query}")
                return (False, None)
            
            logger.info(f"🎵 [SOUNDCLOUD] Downloading from: {best_url}")
            
            # Now download the selected URL
            _tmp_dir = self._staging_dir('sc_')
            
            try:
                ydl_opts = {
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                    'outtmpl': os.path.join(_tmp_dir, '%(title)s.%(ext)s'),
                    'quiet': True,
                    'no_warnings': True,
                    'socket_timeout': 20,
                    'noplaylist': True,
                }
                
                _proxy = getattr(self, 'socks5_proxy', None)
                if _proxy:
                    ydl_opts['proxy'] = _proxy
                
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(best_url, download=True)
                
                # Find the completed audio file
                audio_exts = ('.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wav')
                tmp_files = []
                try:
                    for f in os.listdir(_tmp_dir):
                        if f.lower().endswith(audio_exts) and not f.endswith('.part'):
                            tmp_files.append(os.path.join(_tmp_dir, f))
                except Exception:
                    pass
                
                if not tmp_files:
                    logger.info(f"🎵 [SOUNDCLOUD] No audio file found for: {query}")
                    return (False, None)
                
                # Move the result file to target_dir
                raw_tmp = tmp_files[0]
                raw_name = os.path.basename(raw_tmp)
                raw = os.path.join(target_dir, raw_name)
                
                shutil.move(raw_tmp, raw)
                
                logger.info(f"✓ [SOUNDCLOUD] Downloaded: {raw_name}")
                
                # Validate that the downloaded file matches the requested track
                if self._is_download_match(raw, artist_part, title_part):
                    logger.info(f"✓ [SOUNDCLOUD] File validated: matches requested track")
                    return (True, raw)
                else:
                    logger.warning(f"⚠️ [SOUNDCLOUD] Downloaded file does not match requested track: {raw_name} - deleting and continuing")
                    try:
                        os.remove(raw)
                    except Exception:
                        pass
                    return (False, None)
                
            finally:
                # Cleanup temp dir
                try:
                    for _lf in os.listdir(_tmp_dir):
                        try:
                            os.remove(os.path.join(_tmp_dir, _lf))
                        except Exception:
                            pass
                    os.rmdir(_tmp_dir)
                except Exception:
                    pass
        
        except ImportError:
            logger.info("🎵 [SOUNDCLOUD] yt-dlp not available")
            return (False, None)
        except Exception as e:
            logger.info(f"🎵 [SOUNDCLOUD] Error: {str(e)[:100]}")
            return (False, None)

    
    def _yt_dlp_try_clients(self, url_to_download: str, client_attempts: List[List[str]], tmp_dir: str,
                            extractor_args: Dict[str, Any]) -> Tuple[Optional[str], Optional[Exception]]:
        """Run yt-dlp with each player-client group until one downloads.
        Returns (thumbnail_url, last_error); last_error is None on success."""
        from yt_dlp import YoutubeDL
        proxy = getattr(self, 'socks5_proxy', None)
        thumbnail: Optional[str] = None
        last_err: Optional[Exception] = None

        for attempt_idx, player_clients in enumerate(client_attempts):
            ydl_opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': os.path.join(tmp_dir, '%(title)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
                'socket_timeout': 20,
                'noplaylist': True,
                'playlist_items': '1',          # one song: never walk a whole list
                'extractor_args': {
                    **extractor_args,
                    'youtube': {'player_client': player_clients},
                },
            }
            if proxy:
                ydl_opts['proxy'] = proxy

            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url_to_download, download=True)
                    if info:
                        entry = info.get('entries', [None])[0] if 'entries' in info else info
                        if entry:
                            thumbnail = entry.get('thumbnail') or (entry.get('thumbnails') or [{}])[-1].get('url')
                return thumbnail, None
            except Exception as dl_err:
                last_err = dl_err
                err_str = str(dl_err).lower()
                if proxy and any(w in err_str for w in ('socks', 'proxy', 'timeout', 'connect')):
                    logger.debug(f"🎵 [YT-DLP] proxy error ({type(dl_err).__name__}) — retrying without proxy")
                    try:
                        with YoutubeDL({k: v for k, v in ydl_opts.items() if k != 'proxy'}) as ydl2:
                            ydl2.extract_info(url_to_download, download=True)
                        return thumbnail, None
                    except Exception as dl_err2:
                        last_err = dl_err2
                        logger.debug(f"🎵 [YT-DLP] no-proxy retry also failed: {dl_err2}")
                else:
                    logger.debug(f"🎵 [YT-DLP] player_client={player_clients} failed: {dl_err}")
                if attempt_idx < len(client_attempts) - 1:
                    logger.info(f"🎵 [YT-DLP] Retrying with next client fallback ({client_attempts[attempt_idx + 1]}) after {player_clients} failed")
        return thumbnail, last_err

    def _youtube_search_choice(self, query: str, artist: Optional[str], title: Optional[str],
                               request_context: Optional[str]) -> Optional[str]:
        """The video to download for a search, chosen by the AI or the
        requester; NEXT when it is none of them, None to keep the first result."""
        try:
            from yt_dlp import YoutubeDL
            with YoutubeDL({'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist',
                            'socket_timeout': 15}) as ydl:
                info = ydl.extract_info(f"ytsearch5:{query}", download=False) or {}
        except Exception as exc:
            logger.debug(f"🎵 [YT-DLP] Search listing failed for '{query}': {exc}")
            return None
        candidates = []
        for entry in (info.get('entries') or [])[:5]:
            url = entry.get('url') or (f"https://www.youtube.com/watch?v={entry['id']}" if entry.get('id') else '')
            if url:
                candidates.append({'artist': entry.get('channel') or entry.get('uploader') or '',
                                   'title': entry.get('title') or '', 'duration': int(entry.get('duration') or 0),
                                   'source': 'youtube', 'url': url})
        if not candidates:
            return None
        chosen = self._choose_or_ask(artist or '', title or query, candidates, request_context, 'YouTube',
                                     fallback=candidates[0])
        return chosen['url'] if chosen else request_choice.NEXT

    def _download_with_yt_dlp(self, query: str, target_dir: str, artist: Optional[str] = None,
                              title: Optional[str] = None,
                              request_context: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """Download audio from YouTube using the yt-dlp library.

        A search takes the first result, unless a requester can be asked
        (request_choice): then the AI weighs the first five, and the requester
        chooses when it is not sure ("None of these" ends the search).

        Args:
            query: Search query (e.g., "Artist - Title") or direct URL
            target_dir: Target directory for download

        Returns:
            Tuple of (success: bool, file_path: str or None)
        """
        self._last_yt_thumbnail = None  # reset before each attempt

        if not (query.startswith('http://') or query.startswith('https://')):
            query_clean = re.sub(
                r'\s*[\[\(].*?(Video|Audio|Official|Lyrics|Remix|Cover).*?[\]\)]',
                '', query, flags=re.IGNORECASE
            ).strip()
        else:
            query_clean = query

        try:
            from yt_dlp import YoutubeDL

            logger.info(f"🎵 [YT-DLP] Starting download: '{query_clean}' → {target_dir}")
            os.makedirs(target_dir, exist_ok=True)

            url_to_download = (
                query_clean if (query_clean.startswith('http://') or query_clean.startswith('https://'))
                else f"ytsearch1:{query_clean}"
            )
            if url_to_download.startswith('ytsearch1:') and request_choice.can_ask():
                picked = self._youtube_search_choice(query_clean, artist, title, request_context)
                if picked == request_choice.NEXT:
                    return False, None
                url_to_download = picked or url_to_download

            # Download into an isolated temp folder so that all partial files
            # (.part, .ytdlp, pre-ffmpeg audio) are contained, the whole folder
            # goes whatever happens, and exactly one file reaches target_dir.
            _tmp_dir = self._staging_dir('ytdlp_')

            try:
                # PO Token provider: if a bgutil-ytdlp-pot-provider sidecar is
                # reachable (see docker-compose.yml's `bgutil-provider` service),
                # register its base_url so yt-dlp can attach proof-of-origin
                # tokens to requests — required by every client below to avoid
                # the 403s YouTube returns to token-less clients. Harmless
                # no-op (yt-dlp just logs a debug warning) if unreachable.
                # NOTE: must be a raw IP, not the container/service name — this
                # container's Mullvad VPN overwrites /etc/resolv.conf once
                # connected, breaking Docker's embedded service-name DNS (same
                # reason Plex/Lidarr are reached by static IP, not hostname).
                _bgutil_base_url = os.environ.get('BGUTIL_POT_PROVIDER_URL', '').strip()
                _yt_extractor_args: Dict[str, Any] = {}
                if _bgutil_base_url:
                    _yt_extractor_args['youtubepot-bgutilhttp'] = {'base_url': [_bgutil_base_url]}

                # Client fallback chain: try each until one produces a working
                # download. 'web' is deliberately not first even though it has
                # the fullest format list: YouTube serves it SABR-only streams
                # that yt-dlp cannot download (yt-dlp issue #12482). 'android_vr'
                # is last because its formats require a PO token yt-dlp does not
                # request for that client, so they are skipped or 403. 'mweb'
                # and 'tv_simply' both accept the PO token and yield a plain
                # downloadable format. 'web_embedded' (the player a site embeds)
                # comes last: some videos answer every other logged-out client
                # with "Please sign in" — Taylor Swift's "Everything Has
                # Changed" video does, from any IP — but still play embedded.
                # Configurable via YTDLP_PLAYER_CLIENTS (comma-separated groups
                # separated by ';', e.g. "mweb;tv_simply").
                _client_env = os.environ.get('YTDLP_PLAYER_CLIENTS', '').strip()
                if _client_env:
                    _client_attempts = [
                        [c.strip() for c in group.split(',') if c.strip()]
                        for group in _client_env.split(';') if group.strip()
                    ]
                else:
                    _client_attempts = [['mweb'], ['tv_simply'], ['android_vr'], ['web_embedded']]

                _yt_thumbnail, _last_dl_err = self._yt_dlp_try_clients(url_to_download, _client_attempts, _tmp_dir, _yt_extractor_args)

                # A region-locked video (or a relay IP YouTube refuses to
                # serve): move the relay to a country where the video is
                # allowed and run the client chain again, a few countries
                # at most.
                for _ in range(geo_relay.max_countries()):
                    if _last_dl_err is None or not geo_relay.is_retryable_elsewhere(str(_last_dl_err)):
                        break
                    target = url_to_download if url_to_download.startswith('http') else str(_last_dl_err)
                    if not geo_relay.resolve_youtube(target, error_text=str(_last_dl_err)):
                        break
                    logger.info("🌐 [YT-DLP] Retrying through the new relay")
                    _yt_thumbnail, _last_dl_err = self._yt_dlp_try_clients(url_to_download, _client_attempts, _tmp_dir, _yt_extractor_args)

                if _last_dl_err is not None:
                    logger.debug(f"🎵 [YT-DLP] all client attempts failed: {_last_dl_err}")
                    return False, None

                # Find the completed audio file in the temp dir
                audio_exts = ('.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wav')
                tmp_files = []
                try:
                    for f in os.listdir(_tmp_dir):
                        if f.lower().endswith(audio_exts) and not f.endswith('.part'):
                            tmp_files.append(os.path.join(_tmp_dir, f))
                except Exception:
                    pass

                if not tmp_files:
                    logger.debug(f"🎵 [YT-DLP] No audio file found in temp dir for: {query_clean}")
                    return False, None

                # Move the (single) result file into target_dir
                raw_tmp  = tmp_files[0]
                raw_name = os.path.basename(raw_tmp)
                raw = os.path.join(target_dir, raw_name)
                try:
                    shutil.move(raw_tmp, raw)
                except Exception as _mv_err:
                    logger.debug(f"🎵 [YT-DLP] Could not move result to target_dir: {_mv_err}")
                    return False, None

            finally:
                shutil.rmtree(_tmp_dir, ignore_errors=True)

            # ── NA prefix repair ─────────────────────────────────────────────
            _na_phs = {'na', 'n/a', 'unknown', 'unknown artist', 'none'}
            _raw_base = os.path.basename(raw)
            _raw_stem, _raw_ext = os.path.splitext(_raw_base)
            _ph_pat = re.compile(
                r'^(' + '|'.join(re.escape(p) for p in _na_phs) + r')'
                r'\s*[-\u2013\u2014]\s*',
                re.IGNORECASE,
            )
            _ph_m = _ph_pat.match(_raw_stem)
            if _ph_m:
                _cln_stem = _raw_stem[_ph_m.end():]
                _cln_name = _cln_stem + _raw_ext
                _cln_path = os.path.join(os.path.dirname(raw), _cln_name)
                try:
                    os.rename(raw, _cln_path)
                    raw = _cln_path
                    logger.info(f"  🔧 [YT-DLP] Removed placeholder prefix: '{_raw_base}' → '{_cln_name}'")
                    _ps = _cln_stem.split(' - ', 1)
                    if len(_ps) == 2:
                        _p_artist = _ps[0].strip()
                        _p_title  = re.sub(r'\s*[\[\(][^\]\)]{0,80}[\]\)]', '', _ps[1]).strip()
                        if _p_artist and _p_artist.lower() not in _na_phs:
                            try:
                                from mutagen import File as _MF
                                _af = _MF(raw, easy=True)
                                if _af is not None:
                                    _af['artist'] = [_p_artist]
                                    if _p_title:
                                        _af['title'] = [_p_title]
                                    _af.save()
                                    logger.info(f"  🔧 [YT-DLP] Fixed tags: artist='{_p_artist}', title='{_p_title}'")
                            except Exception as _te:
                                logger.debug(f"  [YT-DLP] Tag fix failed: {_te}")
                except Exception as _rne:
                    logger.debug(f"  [YT-DLP] Could not rename NA file: {_rne}")

            logger.info(f"  ✓ [YT-DLP] {os.path.basename(raw)}")
            self._last_yt_thumbnail = _yt_thumbnail
            return True, raw

        except ImportError:
            logger.debug("🎵 [YT-DLP] yt-dlp library not available")
            return False, None
        except Exception as e:
            logger.warning(f"🎵 [YT-DLP] Unexpected error: {e}")
            return False, None

    def _download_album_cover(self, album_name: str, artist_name: str, target_dir: str) -> str:
        """Delegate to the module-level `_download_album_cover` implementation for cover download,
        so callers go through the Downloader API and tests can patch either.
        Returns path to cover image or empty string/False on failure depending on underlying implementation.
        """
        try:
            if self.analyzer is None:
                logger.debug('_download_album_cover: analyzer is None, cannot delegate')
                return ""
            app_mod = importlib.import_module(self.analyzer.__module__)
            # app's module-level function takes (artist, album, target_dir), not this method's order.
            result = app_mod._download_album_cover(artist_name, album_name, target_dir)
            return result
        except Exception:
            logger.debug('Error delegating _download_album_cover', exc_info=True)
            return ""

    def download_recommended_music(self, recommendations: List[Dict[str, Any]], preference_confidence: float = 1.0, target_dir: str = None, dl_lock=None, user_index=None) -> Dict[str, Any]:
        """Download a list of recommended tracks using configured download tools.
        
        Args:
            recommendations: List of recommendation dicts with keys: artist, title, album, etc.
            preference_confidence: Confidence threshold for filtering (optional)
            target_dir: Target directory (defaults to ALBUM_DIR from app config)
            user_index: Optional user index for Lidarr multi-user support
            
        Returns:
            dict with keys:
                - total: Total tracks attempted
                - successful: Number of successful downloads
                - failed: List of failed tracks with error info
                - tool_used: Tool used (deemix, soundcloud, yt-dlp)
                - details: List of individual download results
        """
        if user_index is None:
            user_index = self._infer_user_index(target_dir=target_dir)

        if not recommendations:
            logger.warning("No recommendations provided to download_recommended_music")
            return {'total': 0, 'successful': 0, 'failed': [], 'tool_used': None, 'details': []}

        # Ensure VPN is up before we start downloading with any tool
        self._ensure_vpn()

        # Respect target_dir override or default album dir
        if not target_dir:
            target_dir = getattr(self.app_mod, 'ALBUM_DIR', None) or os.environ.get('MUSIC_DIR')
            if not target_dir:
                target_dir = '/music/albums'

        os.makedirs(target_dir, exist_ok=True)
        logger.info(f"📥 Starting download of {len(recommendations)} recommendation(s) to {target_dir}")
        _batch_start = time.time()

        tools = self.check_download_tools()
        results = {
            'total': len(recommendations),
            'successful': 0,
            'failed': [],
            'tool_used': None,
            'details': []
        }
        
        # Priority: deemix (Deezer quality) → soundcloud → yt-dlp (YouTube fallback)
        tool_order = []
        if tools.get('deemix'):
            tool_order.append('deemix')
        if tools.get('soundcloud'):
            tool_order.append('soundcloud')
        if tools.get('yt-dlp'):
            tool_order.append('yt-dlp')

        if not tool_order:
            logger.warning("No download tools available (yt-dlp / deemix not found)")
            results['failed'] = recommendations
            return results

        logger.info(f"Available tools: {tool_order}")

        # ── Per-song fallback pipeline ────────────────────────────────────────
        # For every track we try each tool in priority order.  We only move to
        # the next tool for THAT SONG if the current one fails — we never skip
        # a song because a different song succeeded with a given tool.
        #
        # A stale ARL: when deemix signals one we
        # pause, wait for a refresh, and resume from the same track (i is not
        # advanced).  After a refresh the same track is retried with deemix
        # first; if deemix still fails, the per-song fallback tries the other
        # tools for it.
        # ─────────────────────────────────────────────────────────────────────

        all_details = []
        total_successful = 0

        i = 0
        while i < len(recommendations):
            rec = recommendations[i]
            artist = rec.get('artist', '')
            title  = rec.get('title',  '')
            query  = f"{artist} - {title}"

            _batch_elapsed = int(time.time() - _batch_start)
            logger.info(
                f"🎵 Track {i+1}/{len(recommendations)} | "
                f"batch +{_batch_elapsed}s | {query}"
            )

            if self._block_until_arl_refresh_if_stale(f"track {i+1}/{len(recommendations)} ({query})"):
                results['arl_stale'] = True
                logger.warning(
                    f"  [batch] ARL still stale after waiting for refresh — aborting remaining tracks"
                )
                for skipped in recommendations[i:]:
                    all_details.append({
                        'rec': skipped,
                        'status': 'error',
                        'tool': 'deemix',
                        'error': 'ARL stale — refresh required before download',
                    })
                i = len(recommendations)
                break

            song_success  = False
            song_tool     = None
            song_file     = None
            arl_stale_hit = False

            # Check blacklist before attempting any download tool —
            # skip songs the user has explicitly 1-star deleted.
            if self._is_track_blacklisted(artist, title):
                logger.info(
                    f"  🚫 Skipping '{query}' — previously 1-star deleted by user"
                )
                all_details.append({
                    'rec':    rec,
                    'status': 'skipped',
                    'tool':   None,
                    'error':  '1-star blacklisted',
                })
                i += 1
                continue

            # Duplicate check — skip if the track already exists anywhere in
            # the library.
            _existing = self._find_existing_track(artist, title, target_dir)
            if _existing:
                logger.info(
                    f"  ⏭️  Skipping '{query}' — already exists in library"
                )
                results['already_existed'] = results.get('already_existed', 0) + 1
                all_details.append({
                    'rec':    rec,
                    'status': 'skipped_duplicate',
                    'tool':   None,
                    'file':   _existing,
                })
                i += 1
                continue



            for tool in tool_order:
                if song_success:
                    break

                _track_start = time.time()
                logger.info(f"  ↳ trying [{tool}] ...")
                self._write_status(query, tool, "starting",
                                   f"{i+1}/{len(recommendations)}")

                try:
                    if tool == 'deemix':
                        self._arl_stale = False
                        success, file_path, is_rate_limited = self._download_with_deemix_timeout(
                            query, target_dir, None, artist, title,
                        )

                        if self._arl_stale:
                            # ── ARL stale mid-batch ──────────────────────────
                            logger.warning(
                                f"  [deemix] Stale ARL at track {i+1}/{len(recommendations)}: "
                                f"{query} — pausing for ARL refresh"
                            )
                            results['arl_stale'] = True
                            arl_stale_hit = True

                            if dl_lock is not None:
                                try:
                                    dl_lock.release()
                                    logger.info(
                                        "🔓 [ARL stale] Downloader lock released — "
                                        "other processes can run while waiting for ARL refresh"
                                    )
                                except Exception as _rel:
                                    logger.warning(f"Could not release lock before ARL wait: {_rel}")

                            refreshed = False
                            try:
                                from downloader_manager import (
                                    wait_for_arl_refresh, set_arl_stale, write_arl_alert
                                )
                                set_arl_stale(True)
                                write_arl_alert()
                                refreshed = wait_for_arl_refresh(poll_interval=60, timeout=86400)
                            except Exception as _dm:
                                logger.warning(f"ARL refresh wait error: {_dm}")

                            if dl_lock is not None:
                                try:
                                    logger.info("🔒 [ARL refreshed] Re-acquiring downloader lock...")
                                    _reacq = dl_lock.acquire(timeout=7200)
                                    if _reacq:
                                        logger.info("✓ [ARL refreshed] Lock re-acquired")
                                    else:
                                        logger.warning(
                                            "⚠️ [ARL refreshed] Could not re-acquire lock after 2 h — "
                                            "remaining tracks will be skipped this cycle"
                                        )
                                        refreshed = False
                                except Exception as _ra:
                                    logger.warning(f"Could not re-acquire lock after ARL refresh: {_ra}")

                            if refreshed:
                                results.pop('arl_stale', None)
                                self._arl_stale = False
                                logger.info(
                                    f"  [deemix] ARL refreshed — retrying track {i+1}: {query}"
                                )
                                # Break inner tool loop and retry this song from
                                # the top of the tool_order (i is not advanced).
                                break
                            else:
                                logger.warning(
                                    f"  [deemix] ARL refresh timed out — "
                                    f"marking remaining {len(recommendations) - i} track(s) failed"
                                )
                                for skipped in recommendations[i:]:
                                    all_details.append({
                                        'rec':    skipped,
                                        'status': 'error',
                                        'tool':   'deemix',
                                        'error':  'ARL stale — refresh timed out',
                                    })
                                # Jump past all remaining tracks
                                i = len(recommendations)
                                break

                        if is_rate_limited:
                            logger.warning(f"  [deemix] Rate limited for {query} — waiting before retry")
                            if self.rate_limiter:
                                key = 'audio_deemix'
                                dl = self.rate_limiter.get_delay(key)
                                if dl > 0:
                                    logger.info(f"⏳ Waiting {dl:.0f}s")
                                    self.rate_limiter.wait_if_needed(key)
                            # Break to retry this track with deemix again
                            break

                        elif success and file_path:
                            _dur = int(time.time() - _track_start)
                            logger.info(f"  ✓ [deemix] {query} ({_dur}s)")
                            song_success = True
                            song_tool    = 'deemix'
                            song_file    = file_path
                            # Mark deemix_clean so cli.py skips metadata re-processing
                            try:
                                _batch_user = None
                                _td2 = os.path.normpath(target_dir).split(os.sep)
                                for _pi2, _pp2 in enumerate(_td2):
                                    if _pp2 == 'users' and _pi2 + 1 < len(_td2):
                                        _batch_user = _td2[_pi2 + 1]; break
                                from cli import ensure_cleanup_table, update_cleanup_record, get_database_path as _get_db2
                                _dc_db2 = _get_db2(_batch_user)
                                ensure_cleanup_table(_dc_db2)
                                update_cleanup_record(_dc_db2, file_path, deemix_clean=True, metadata_done=True)
                            except Exception as _dce2:
                                logger.debug(f"Could not mark deemix_clean (batch): {_dce2}")
                        else:
                            _dur = int(time.time() - _track_start)
                            logger.info(f"  ✗ [deemix] {query} failed ({_dur}s) — trying next tool")

                    elif tool == 'soundcloud':
                        self._write_status(query, "soundcloud", "downloading",
                                           f"{i+1}/{len(recommendations)}")
                        success, file_path = self._download_with_soundcloud(query, target_dir)
                        _dur = int(time.time() - _track_start)
                        if success and file_path:
                            logger.info(f"  ✓ [soundcloud] {query} ({_dur}s)")
                            song_success = True
                            song_tool    = 'soundcloud'
                            song_file    = file_path
                        else:
                            logger.info(f"  ✗ [soundcloud] {query} failed ({_dur}s) — trying next tool")

                    elif tool == 'yt-dlp':
                        self._write_status(query, "yt-dlp", "downloading",
                                           f"{i+1}/{len(recommendations)}")
                        success, file_path = self._download_with_yt_dlp(query, target_dir)
                        _dur = int(time.time() - _track_start)
                        if success and file_path:
                            logger.info(f"  ✓ [yt-dlp] {query} ({_dur}s)")
                            song_success = True
                            song_tool    = 'yt-dlp'
                            song_file    = file_path
                        else:
                            logger.info(f"  ✗ [yt-dlp] {query} failed ({_dur}s) — all tools exhausted")

                except Exception as exc:
                    logger.debug(f"  ✗ [{tool}] exception for {query}: {exc}")

            # ARL timed out — i was already set to len(recommendations); stop.
            if i >= len(recommendations):
                break

            # ARL refreshed — retry this same song from top of tool_order.
            if arl_stale_hit and not song_success and i < len(recommendations):
                continue  # do NOT advance i

            # ── Organize successful download into Artist/Title/ subfolder ─────
            # Move the file immediately so every download lands in the correct
            # structure (Fix Metadata files it properly later):
            #   {target_dir}/{Artist}/{Track Title}/{filename}
            if song_success and song_file and os.path.isfile(song_file):
                try:
                    def _san_dir(s):
                        s = re.sub(r'\s*[\[\(][^\]\)]{0,60}[\]\)]', '', s or 'Unknown')
                        return re.sub(r'[<>:"/\\|?*]', '_', s).strip('. ')[:100]
                    _org_dir  = os.path.join(target_dir, _san_dir(artist), _san_dir(title))
                    os.makedirs(_org_dir, exist_ok=True)
                    _org_path = os.path.join(_org_dir, os.path.basename(song_file))
                    if os.path.abspath(song_file) != os.path.abspath(_org_path):
                        shutil.move(song_file, _org_path)
                        song_file = _org_path
                        logger.debug(f"  📁 Organized: {_san_dir(artist)}/{_san_dir(title)}/{os.path.basename(_org_path)}")

                    # Remove duplicate track variants under the same artist tree.
                    try:
                        self._remove_duplicate_track_variants(artist, title, os.path.dirname(_org_path), song_file)
                    except Exception as _dedup_err:
                        logger.debug(f"  Could not deduplicate track directory: {_dedup_err}")
                except Exception as _org_err:
                    logger.debug(f"  Could not organize '{query}': {_org_err}")

            # Record result for this song.
            if song_success:
                all_details.append({
                    'rec':    rec,
                    'status': 'ok',
                    'tool':   song_tool,
                    'files':  [song_file] if song_file else [],
                })
                total_successful += 1
            else:
                all_details.append({
                    'rec':    rec,
                    'status': 'error',
                    'tool':   None,
                    'error':  'All tools failed',
                })
                logger.warning(f"  ❌ All tools failed for: {query}")

            i += 1  # advance to the next song

        results['successful'] = total_successful
        results['details']    = all_details
        results['failed']     = [d for d in all_details if d['status'] != 'ok']
        if any(d.get('tool') == 'lidarr' for d in all_details):
            results['tool_used'] = 'lidarr'
        else:
            results['tool_used'] = 'per-song-fallback'
        return results

    def _find_existing_track(self, artist: str, title: str, target_dir: str,
                             release_title: bool = False) -> Optional[str]:
        """Check if a track already exists anywhere in the library.

        Searches target_dir and ALBUM_DIR, so a song already in the library
        (wherever Fix Metadata or Lidarr filed it) is not downloaded again.

        Matching strategy (in order):
        1. Tag-based: read ID3/MP4/FLAC tags, fuzzy-match artist+title
        2. Filename-based fallback: parse 'Artist - Title.ext' filename pattern

        A combined score (artist 30 %, title 70 %) >= 0.80 counts as a match.
        The title weight is higher because artist tags on files are often slightly
        different (e.g. "Drake" vs "Drake feat. Lil Baby").

        A `release_title` is a release's own name for the track, whose brackets
        name a version: "Theme of Love (ensemble)" is not "Theme of Love".

        Returns the first matching file path, or None.
        """

        def _norm(s: str, brackets: bool = False) -> str:
            """Lowercase, strip parentheticals, feat/ft markers and punctuation."""
            if not s:
                return ""
            s = s.lower()
            if not brackets:
                s = re.sub(r"\s*[\[\(][^\]\)]*[\]\)]", "", s)   # strip (…) / […]
            s = re.sub(r"\bfeat\.?\b.*|\bft\.?\b.*", "", s, flags=re.IGNORECASE)
            s = re.sub(r"[^a-z0-9\s]", " ", s)
            s = re.sub(r"\s+", " ", s).strip()
            return s

        THRESHOLD = 0.80
        SAME_TITLE = 0.9
        AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav")

        target_artist = _norm(artist)
        target_title  = _norm(title, release_title)

        if not target_title:
            return None

        _dirs_to_search = []
        for _d in [
            target_dir,
            os.environ.get("ALBUM_DIR"),
            getattr(self.app_mod, "ALBUM_DIR", None) if self.app_mod else None,
        ]:
            if _d and os.path.isdir(_d) and _d not in _dirs_to_search:
                _dirs_to_search.append(_d)

        logger.debug(
            f"[DEDUP] Checking '{artist} - {title}' across "
            f"{len(_dirs_to_search)} dir(s)"
        )

        for search_dir in _dirs_to_search:
            try:
                for root, _dirs, files in os.walk(search_dir):
                    for filename in files:
                        if not filename.lower().endswith(AUDIO_EXTS):
                            continue
                        filepath = os.path.join(root, filename)
                        try:
                            # Attempt tag extraction using the real class method
                            file_artist, file_title = self._extract_artist_title_from_file(filepath)
                            fa = _norm(file_artist or "")
                            ft = _norm(file_title or "", release_title)

                            # If tags are empty, fall back to filename
                            if not ft:
                                base = os.path.splitext(filename)[0]
                                parts = base.split(" - ", 1)
                                if len(parts) == 2:
                                    fa = _norm(parts[0])
                                    ft = _norm(parts[1], release_title)
                                else:
                                    ft = _norm(base, release_title)
                                    fa = ""

                            if not ft:
                                continue

                            # Fuzzy score
                            title_score  = SequenceMatcher(None, target_title, ft).ratio()
                            artist_score = (
                                SequenceMatcher(None, target_artist, fa).ratio()
                                if target_artist and fa else
                                (1.0 if not target_artist else 0.5)
                            )
                            combined = artist_score * 0.30 + title_score * 0.70

                            # "Epilogue" is not "Prologue" (0.75 alike) by the same artist.
                            if combined >= THRESHOLD and title_score >= SAME_TITLE:
                                logger.info(
                                    f"[DEDUP] Match ({combined:.2f}) — "
                                    f"skipping '{artist} - {title}' "
                                    f"(exists: {os.path.relpath(filepath, search_dir)})"
                                )
                                return filepath

                        except Exception as _fe:
                            logger.debug(f"[DEDUP] Error reading {filename}: {_fe}")
                            continue
            except Exception as _de:
                logger.debug(f"[DEDUP] Error walking {search_dir}: {_de}")
                continue

        logger.debug(f"[DEDUP] No existing track found for '{artist} - {title}'")
        return None

    def _remove_duplicate_track_variants(self, artist: str, title: str, song_dir: str, keep_path: str) -> None:
        """Remove duplicate files for the same artist/title under the same artist tree."""

        if not song_dir or not os.path.isdir(song_dir) or not keep_path:
            return

        def _norm(s: str) -> str:
            if not s:
                return ""
            s = s.lower()
            s = re.sub(r"\s*[\[\(][^\]\)]*[\]\)]", "", s)
            s = re.sub(r"\bfeat\.?\b.*|\bft\.?\b.*", "", s, flags=re.IGNORECASE)
            s = re.sub(r"[^a-z0-9\s]", " ", s)
            return re.sub(r"\s+", " ", s).strip()

        target_artist = _norm(artist)
        target_title = _norm(title)
        if not target_title:
            return

        artist_root = os.path.dirname(song_dir)
        if not os.path.isdir(artist_root):
            return

        keep_size = os.path.getsize(keep_path) if os.path.isfile(keep_path) else 0
        AUDIO_EXTS = ('.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wav')

        for root, _, files in os.walk(artist_root):
            for filename in files:
                filepath = os.path.join(root, filename)
                if os.path.abspath(filepath) == os.path.abspath(keep_path):
                    continue
                if not filename.lower().endswith(AUDIO_EXTS):
                    continue

                try:
                    file_artist, file_title = self._extract_artist_title_from_file(filepath)
                    if not file_title:
                        base = os.path.splitext(filename)[0]
                        parts = base.split(' - ', 1)
                        if len(parts) == 2:
                            file_artist = file_artist or parts[0]
                            file_title = file_title or parts[1]
                        else:
                            file_title = file_title or base

                    file_artist_norm = _norm(file_artist or "")
                    file_title_norm = _norm(file_title or "")

                    title_score = SequenceMatcher(None, target_title, file_title_norm).ratio()
                    artist_score = (
                        SequenceMatcher(None, target_artist, file_artist_norm).ratio()
                        if target_artist and file_artist_norm else
                        (1.0 if not target_artist else 0.5)
                    )
                    combined = artist_score * 0.30 + title_score * 0.70

                    if combined >= 0.85:
                        file_size = os.path.getsize(filepath) if os.path.isfile(filepath) else 0
                        if file_size > keep_size:
                            try:
                                os.remove(keep_path)
                                keep_path = filepath
                                keep_size = file_size
                                logger.info(f"  🗑️ Replaced duplicate with larger file: {filepath}")
                            except Exception as e:
                                logger.debug(f"[DEDUP] Failed to replace keep file: {e}")
                        else:
                            try:
                                os.remove(filepath)
                                logger.info(f"  🗑️ Removed duplicate file: {filepath}")
                            except Exception as e:
                                logger.debug(f"[DEDUP] Failed to remove duplicate file: {e}")
                except Exception as e:
                    logger.debug(f"[DEDUP] Could not evaluate duplicate candidate {filepath}: {e}")

    # ── download_track helpers ────────────────────────────────────────────────

    _AUDIO_EXTENSIONS = ('.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wav')
    _DEEMIX_TRACK_TIMEOUT = 90

    def _resolve_download_target_dir(self, target_dir: Optional[str]) -> Optional[str]:
        """Every user shares one library, so the target is either the caller's
        directory or the configured ALBUM_DIR."""
        return target_dir or getattr(self.app_mod, 'ALBUM_DIR', None) or os.environ.get('ALBUM_DIR')

    _FAN_CHANNEL_RE = re.compile(
        r'(?:[A-Za-z]\d{3,}\b|lyrics?|lyric\s*video|official\s*lyrics'
        r'|fan\s*(?:channel|made|video|art|edit|lyric)|fan\d*$'
        r'|AMV|karaoke|cover\s*(?:by|channel)|reaction|nightcore)',
        re.IGNORECASE,
    )
    _TITLE_VARIANT_SUFFIX_RE = re.compile(
        r'\((?:arabic version|live version|acoustic version|explicit version|clean version|instrumental version|deluxe version|radio edit|re-recording|cover|translated version|translation|alternate version|mix|remix)\)$',
        re.IGNORECASE,
    )
    _TITLE_NOISE_SUFFIX_RE = re.compile(
        r'\s*[\[\(]\s*(?:official video|official audio|official music video|official visualizer|official lyric video|music video|lyrics?|lyric video|audio|video|visualizer|hd|hq|4k|1080p|karaoke|teaser|trailer|snippet|audio only|audio-only|extended|cover|mix|remix|instrumental|clean|explicit|radio edit|live|acoustic)\s*[\]\)]\s*$',
        re.IGNORECASE,
    )

    def _clean_request_names(self, artist: str, title: str) -> Tuple[str, str]:
        """Turn a raw request (often lifted from a video title/uploader) into
        search-ready artist and title strings.

        - YouTube auto-generated channels are named "<Artist> - Topic".
        - Quality/format tags like "[HD]" or "(Official Video)" are noise, but a
          trailing variant label such as "(Live Version)" is meaningful and kept.
        - Fan/lyric channels ("GWLyrics", "Rockbandfan145") are not artists; the
          title alone usually names the real artist, so search on it only.
        - Swapped fields ("Bring Me To Life" / "Evanescence") are flipped only
          when the title is a known artist and the artist is not. The shape of
          the strings alone is no evidence: "Anime de Japan" (a lowercase "de")
          next to a two-word title like "Battle Ignition" would file every
          such track under its title as a fake artist.
        """
        artist = re.sub(r'\s*-\s*Topic\s*$', '', artist or '', flags=re.IGNORECASE).strip()
        title = (title or '').strip()

        if not self._TITLE_VARIANT_SUFFIX_RE.search(title):
            title = self._TITLE_NOISE_SUFFIX_RE.sub('', title).strip()
            title = re.sub(r'\s*[\[\(][^\)\]]*$', '', title).strip()

        if artist and self._FAN_CHANNEL_RE.search(artist):
            logger.info(f"🔧 Fan-channel artist detected ('{artist}') — searching by title only: '{title}'")
            artist = ''

        if (artist and self._looks_like_artist_name(title) and not self._looks_like_artist_name(artist)
                and self._is_known_artist(title) and not self._is_known_artist(artist)):
            logger.info(f"🔀 Artist/title appear swapped ('{artist}' / '{title}') — flipping")
            artist, title = title, artist

        return artist, title

    # An artist with fewer Deezer fans than this is a namesake, not evidence
    # ("Enemy Unseen" exists with 0 fans; "Anime de Japan" has thousands).
    _KNOWN_ARTIST_MIN_FANS = 50

    def _is_known_artist(self, name: str) -> bool:
        """Is `name` a real artist: one Lidarr knows, or a Deezer artist of that
        exact name with a following? Cached per process."""
        cache = self.__dict__.setdefault('_known_artist_cache', {})
        key = re.sub(r'[^\w]+', '', (name or '').casefold())
        if not key:
            return False
        if key in cache:
            return cache[key]
        known = False
        try:
            from lidarr_catalog import LidarrCatalog, compact
            catalog = self.__dict__.get('_lidarr_catalog_for_names')
            if catalog is None:
                catalog = LidarrCatalog.from_env()
                if catalog:
                    catalog.load()
                self._lidarr_catalog_for_names = catalog or False
            if catalog:
                known = bool(compact(name)) and compact(name) in catalog._by_artist
        except Exception as exc:
            logger.debug(f"Lidarr artist lookup failed for {name!r}: {exc}")
        if not known:
            try:
                found = requests.get('https://api.deezer.com/search/artist', params={'q': name, 'limit': 5},
                                     timeout=10).json().get('data') or []
                known = any(re.sub(r'[^\w]+', '', a.get('name', '').casefold()) == key
                            and (a.get('nb_fan') or 0) >= self._KNOWN_ARTIST_MIN_FANS for a in found)
            except Exception as exc:
                logger.debug(f"Deezer artist lookup failed for {name!r}: {exc}")
        cache[key] = known
        return known

    def _download_with_deemix_timeout(self, query: str, target_dir: str, request_context: Optional[str],
                                      artist: str, title: str, album: Optional[str] = None) -> Tuple[bool, Optional[str], bool]:
        """Run deemix in a worker thread so a hung Deezer session cannot stall
        the whole request; a timeout is reported as a plain failure.

        A running thread cannot be cancelled: after a timeout deemix carries
        on, and when it finishes it leaves "Artist - Title.mp3" in the root of
        target_dir while the request has already moved on to another tool —
        a loose file Plex shows as yet another album, and often a second copy
        of a track that was filed properly. So the worker is told it was
        abandoned, and parks a late file outside the library instead."""
        import concurrent.futures as cf
        import contextvars
        import threading
        abandoned = threading.Event()

        def work():
            result = self._download_with_deemix(query, target_dir, request_context, artist, title, album)
            if abandoned.is_set() and result and result[0] and result[1]:
                self._park_late_download(result[1], query)
            return result

        pool = cf.ThreadPoolExecutor(max_workers=1)
        # The worker sees the requester (request_choice) and may wait for their
        # answer; that wait is not Deezer hanging, so it restarts the clock.
        future = pool.submit(contextvars.copy_context().run, work)
        requester = request_choice.current()
        deadline = time.monotonic() + self._DEEMIX_TRACK_TIMEOUT
        try:
            while True:
                try:
                    return future.result(timeout=max(0.0, min(1.0, deadline - time.monotonic())))
                except cf.TimeoutError:
                    if requester is not None and requester.waiting.is_set():
                        deadline = time.monotonic() + self._DEEMIX_TRACK_TIMEOUT
                    elif time.monotonic() >= deadline:
                        raise
        except cf.TimeoutError:
            abandoned.set()
            if future.done():                    # finished just as the timeout fired
                return future.result()
            logger.warning(f"⏱️ [DEEMIX] timed out after {self._DEEMIX_TRACK_TIMEOUT}s: {query} "
                           f"(a late file will be parked outside the library)")
            return False, None, False
        except Exception as exc:
            logger.warning(f"⏱️ [DEEMIX] wrapper error: {exc}")
            return False, None, False
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _park_late_download(self, file_path: str, query: str) -> None:
        """Move a download nobody is waiting for out of the music tree."""
        data_dir = os.environ.get('DATA_DIR', '/app/data')
        dest_dir = os.path.join(data_dir, 'late_downloads', datetime.now().strftime('%Y%m%d'))
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(file_path))
            shutil.move(file_path, dest)
            logger.info(f"📦 [DEEMIX] late download for '{query}' parked outside the library: {dest}")
        except Exception as exc:
            logger.warning(f"Could not park late download {file_path}: {exc}")

    def _mark_deemix_clean(self, file_path: str, plex_user: Optional[str]) -> None:
        """Record that deemix already embedded full tags so the daily metadata
        pass leaves this file alone."""
        try:
            from cli import ensure_cleanup_table, update_cleanup_record, get_database_path
            db_path = get_database_path(plex_user)
            ensure_cleanup_table(db_path)
            update_cleanup_record(db_path, file_path, deemix_clean=True, metadata_done=True)
        except Exception as exc:
            logger.debug(f"Could not mark deemix_clean: {exc}")

    _CATALOGUE_HOSTS = (
        'shazam.com', 'open.spotify.com', 'spotify.com', 'music.apple.com',
        'deezer.com', 'tidal.com', 'genius.com', 'last.fm', 'musicbrainz.org',
        'discogs.com', 'song.link', 'album.link',
    )

    def _is_catalogue_page(self, url: str) -> bool:
        """True for pages that merely describe a track rather than host audio."""
        lowered = (url or '').lower()
        return any(host in lowered for host in self._CATALOGUE_HOSTS)

    @staticmethod
    def _single_video_url(url: str) -> str:
        """A link to one YouTube video as its plain watch URL. yt-dlp treats
        `/source/ID/shorts` as the list of every Short using that sound, and
        would download them all; `&list=` would make it a playlist."""
        match = re.search(r'(?:youtu\.be/|youtube\.com/(?:shorts/|source/|v/|embed/|watch\?(?:[^#]*&)?v=))([\w-]{11})', url or '')
        return f"https://www.youtube.com/watch?v={match.group(1)}" if match else url

    def _requesters_video(self, url: Optional[str], request_context: Optional[str]) -> str:
        """The plain link of the YouTube video a requester linked, when `url` is
        that video; '' otherwise. It names the recording they mean, however it
        is titled, so there is nothing to compare it with."""
        linked = self._single_video_url(self._context_fields(request_context).get('url', ''))
        return linked if 'youtube.com/watch' in linked and linked == self._single_video_url(url or '') else ''

    def _link_first(self, tools: List[str], url: Optional[str], request_context: Optional[str]) -> List[str]:
        """A video the requester linked comes before searching SoundCloud."""
        if self._requesters_video(url, request_context) and {'yt-dlp', 'soundcloud'} <= set(tools):
            tools = [t for t in tools if t != 'yt-dlp']
            tools.insert(tools.index('soundcloud'), 'yt-dlp')
        return tools

    def _cut_short(self, file_path: str, request_context: Optional[str]) -> bool:
        """Far shorter than the video the request links: a preview, not the song."""
        stated = re.fullmatch(r'(\d+)s', self._context_fields(request_context).get('video_duration', ''))
        if not stated:
            return False
        try:
            from mutagen import File
            length = File(file_path).info.length
        except Exception as exc:
            logger.debug(f"Could not read the length of {file_path}: {exc}")
            return False
        return length < int(stated.group(1)) / 2

    @staticmethod
    def _staging_dir(prefix: str) -> str:
        """A temporary folder for one download, outside the music library: a
        download cut short must never leave files where Plex and Lidarr look."""
        import tempfile
        parent = os.path.join(os.environ.get('DATA_DIR', '/app/data'), 'download_staging')
        os.makedirs(parent, exist_ok=True)
        return tempfile.mkdtemp(prefix=prefix, dir=parent)

    def _try_tool_download(self, tool: str, artist: str, title: str, url: Optional[str], target_dir: str,
                           request_context: Optional[str], plex_user: Optional[str],
                           album: Optional[str] = None) -> Optional[str]:
        """Attempt one download tool and return the validated file path, or
        None. A file that downloads but does not match the requested track is
        deleted so it cannot be picked up as a stray later."""
        exact = self._context_fields(request_context).get('deezer_track', '')
        exact = exact if tool == 'deemix' and exact.isdigit() else ''
        if exact:
            # The album's own track, found by album_match: no search.
            logger.info(f"🎵 [DEEMIX] Downloading Deezer track {exact}: '{artist} - {title}'")
            file_path = self.download_deezer_track(exact, target_dir)
            success = bool(file_path)
        elif tool == 'deemix':
            query = f"{artist} - {title}" if artist else title
            logger.info(f"🎵 [DEEMIX] Searching: '{query}'")
            self._arl_stale = False
            success, file_path, _ = self._download_with_deemix_timeout(query, target_dir, request_context, artist, title, album)
        elif tool == 'soundcloud':
            query = f"{artist} - {title}" if artist and title else (title or artist or url or '')
            if not query:
                logger.warning("⚠️ Soundcloud has no query to search, skipping")
                return None
            logger.info(f"🎵 [SOUNDCLOUD] Searching: '{query}'")
            success, file_path = self._download_with_soundcloud(query, target_dir, request_context)
        elif tool == 'yt-dlp':
            # A catalogue page (Shazam, Spotify, Apple Music …) only names the
            # track; yt-dlp's generic extractor cannot fetch audio from it, so
            # search for the track instead of handing over the link.
            direct_url = self._single_video_url(url) if url and not self._is_catalogue_page(url) else ''
            query = direct_url or (f"{artist} {title} official".strip() if artist else f"{title} official")
            logger.info(f"🎵 [YT-DLP] Searching: '{query}'")
            success, file_path = self._download_with_yt_dlp(query, target_dir, artist=artist, title=title,
                                                            request_context=request_context)
        else:
            return None

        if not (success and file_path):
            return None

        own_video = tool == 'yt-dlp' and bool(self._requesters_video(url, request_context))
        try:
            is_match = own_video or bool(exact) or (
                self._is_download_match(file_path, artist, title, request_context=request_context or '')
                and not self._cut_short(file_path, request_context))
        except Exception as exc:
            logger.error(f"❌ {tool} validation exception: {exc}", exc_info=True)
            is_match = False

        if not is_match:
            logger.warning(f"⚠️ {tool} file does not match requested track: {os.path.basename(file_path)} — deleting")
            try:
                os.remove(file_path)
            except Exception:
                pass
            return None

        logger.info(f"✅ {tool} download validated: {os.path.basename(file_path)}")
        if tool == 'deemix':
            self._mark_deemix_clean(file_path, plex_user)
        return file_path

    def _write_basic_tags(self, file_path: str, metadata: Dict[str, Any], fallback_album: Optional[str] = None) -> None:
        """Write artist/title/album/year/genre into an mp3, m4a or flac file."""
        ext = os.path.splitext(file_path)[1].lower()
        album = balanced(metadata.get('album') or fallback_album or '')
        artist, title = balanced(metadata.get('artist') or ''), balanced(metadata.get('title') or '')
        year = metadata.get('year')
        genres = metadata.get('genres') or []

        if ext == '.mp3':
            from mutagen.easyid3 import EasyID3
            from mutagen.mp3 import MP3
            try:
                audio = EasyID3(file_path)
            except Exception:
                audio = MP3(file_path)
                audio.add_tags()
            audio['artist'] = artist
            audio['title'] = title
            audio['album'] = album
            if year:
                audio['date'] = str(year)
            if genres:
                audio['genre'] = genres[0]
            audio.save()
        elif ext == '.m4a':
            from mutagen.mp4 import MP4
            audio = MP4(file_path)
            audio['\xa9ART'] = artist
            audio['\xa9nam'] = title
            audio['\xa9alb'] = album
            if year:
                audio['\xa9day'] = str(year)
            audio.save()
        elif ext == '.flac':
            from mutagen.flac import FLAC
            audio = FLAC(file_path)
            audio['artist'] = artist
            audio['title'] = title
            audio['album'] = album
            if year:
                audio['year'] = str(year)
            if genres:
                audio['genre'] = genres[0]
            audio.save()

    def _read_embedded_tags(self, file_path: str, source: str, artist: str, title: str, album: str) -> Optional[Dict[str, Any]]:
        try:
            from mutagen import File as MutagenFile
            tags = MutagenFile(file_path, easy=True)
        except Exception as exc:
            logger.debug(f"Could not read embedded tags from {source} file: {exc}")
            return None
        if tags is None:
            return None

        def tag(key):
            return (tags.get(key) or [None])[0] or ''

        return {
            'title': tag('title') or title,
            'artist': tag('artist') or artist,
            'album': tag('album') or album or title,
            'year': tag('date') or tag('year') or None,
            'genres': list(tags.get('genre') or []),
            'cover_url': None,
            'source': source,
        }

    def _enrich_downloaded_metadata(self, downloaded_file: str, used_tool: str, seed: Dict[str, Any],
                                    request_context_metadata: Dict[str, Any], artist: str, title: str,
                                    album: str, user_index: int) -> Dict[str, Any]:
        """Return final metadata for the file and make sure its tags match.

        deemix embeds complete Deezer tags, so those are read back and only
        rewritten when the request named a different performer or variant.
        soundcloud / yt-dlp files carry little or nothing, so the external
        metadata pipeline runs and its result is written into the file.
        """
        metadata = dict(seed)

        if used_tool == 'deemix':
            embedded = self._read_embedded_tags(downloaded_file, used_tool, artist, title, album)
            if embedded:
                metadata = embedded
                logger.debug(f"✓ [deemix] Embedded tags: {metadata['artist']} — {metadata['title']}")
                try:
                    merged = self._merge_request_context_metadata(
                        external_metadata=metadata, request_context_metadata=request_context_metadata,
                        artist=artist, title=title, album=album,
                    )
                    if merged.get('artist') != metadata.get('artist') or merged.get('title') != metadata.get('title'):
                        metadata = merged
                        logger.info(f"🎯 Overriding embedded tags with request metadata: {merged.get('artist')} - {merged.get('title')}")
                        self._write_basic_tags(downloaded_file, merged, fallback_album=album)
                except Exception as exc:
                    logger.debug(f"Could not apply request metadata over embedded tags: {exc}")
        else:
            try:
                external = self.get_metadata_from_sources(artist=artist, album_name=album, track_name=title, user_index=user_index)
                if external:
                    metadata = self._merge_request_context_metadata(
                        external_metadata=external, request_context_metadata=request_context_metadata,
                        artist=artist, title=title, album=album,
                    )
                    logger.debug(f"✓ External metadata from {metadata.get('source', 'unknown')}")
            except Exception as exc:
                logger.debug(f"External metadata pipeline failed: {exc}")

            # yt-dlp captured the exact video thumbnail during download; it is
            # a better cover than another YouTube search would produce.
            if used_tool == 'yt-dlp':
                thumb = getattr(self, '_last_yt_thumbnail', None)
                if thumb and not metadata.get('cover_url'):
                    metadata['cover_url'] = thumb

            metadata.setdefault('artist', artist)
            metadata.setdefault('title', title)
            metadata['artist'] = metadata.get('artist') or artist
            metadata['title'] = metadata.get('title') or title
            metadata['album'] = metadata.get('album') or album or title
            try:
                self._write_basic_tags(downloaded_file, {**metadata, 'artist': artist, 'title': title}, fallback_album=album)
                logger.info(f"✓ Metadata applied to {os.path.basename(downloaded_file)}")
            except Exception as exc:
                logger.warning(f"⚠️ Could not apply metadata: {exc}")

        metadata['artist'] = metadata.get('artist') or artist
        metadata['title'] = metadata.get('title') or title
        metadata['album'] = self._clean_album_name(metadata.get('album') or album or title,
                                                   metadata.get('artist') or artist)
        return metadata

    _TITLE_SUFFIX_NOISE = [
        r'\s*\[Official Video\].*', r'\s*\[Official Audio\].*',
        r'\s*\(Official Video\).*', r'\s*\(Official Audio\).*',
        r'\s*\[Lyrics\].*', r'\s*\(Lyrics\).*',
        r'\s*\[Lyric Video\].*', r'\s*\(Lyric Video\).*',
        r'\s*\[Official\].*', r'\s*\(Official\).*',
        r'\s*\[Audio\].*', r'\s*\(Audio\).*',
    ]

    def _filename_title(self, track_title: str) -> str:
        normalized = track_title
        for pattern in self._TITLE_SUFFIX_NOISE:
            normalized = re.sub(pattern, '', normalized, flags=re.IGNORECASE).strip()
        return normalized

    @staticmethod
    def _primary_artist(artist_string: str) -> str:
        """First artist when several are listed; featured artists are already
        part of the title. Separators inside brackets don't count: "RISE (ft.
        The Glitch Mob, Mako, and The Word Alive)" is not cut at its comma."""
        if not artist_string:
            return artist_string
        parts = split_outside_brackets(artist_string, _ARTIST_LIST_SEPARATOR)
        return balanced(parts[0] if parts else artist_string)

    @staticmethod
    def _clean_album_name(album: Optional[str], artist: Optional[str]) -> str:
        """Drop an "<Artist> - " prefix from an album name.

        Discogs and friends return albums as "Artist - Album". Left in place
        it reaches the file's album tag, and Plex groups by that tag, so the
        affected tracks break away from the rest of their album.
        """
        album = (album or '').strip()
        artist = (artist or '').strip()
        if not album or not artist:
            return album
        prefix = f"{artist} - ".lower()
        if album.lower().startswith(prefix):
            return album[len(prefix):].strip() or album
        return album

    @staticmethod
    def _existing_dir_match(parent: str, name: str) -> str:
        """Reuse a directory that already holds this artist or album under a
        different spelling, so sources that disagree on casing or punctuation
        ("Drink the Sea" vs "Drink The Sea") do not split it in two."""
        if not name:
            return name

        def key(value):
            return re.sub(r'[^a-z0-9]', '', value.lower())

        wanted = key(name)
        if not wanted:
            return name
        try:
            for existing in sorted(os.listdir(parent)):
                if key(existing) == wanted and os.path.isdir(os.path.join(parent, existing)):
                    if existing != name:
                        logger.info(f"📁 Using existing folder '{existing}' instead of '{name}'")
                    return existing
        except Exception:
            pass
        return name

    def _align_album_tags(self, file_path: str, album: str, album_artist: str) -> None:
        """Make the file's album and album-artist tags match the album it was
        filed under.

        Plex groups an album by these two tags, not by folder, so a track
        whose downloader credited every contributor ("The Glitch Mob/
        Superposition") or spelled the album differently breaks away into an
        album of its own.
        """
        try:
            ext = os.path.splitext(file_path)[1].lower()
            if ext == '.mp3':
                from mutagen.easyid3 import EasyID3
                audio = EasyID3(file_path)
                keys = ('album', 'albumartist')
            elif ext == '.flac':
                from mutagen.flac import FLAC
                audio = FLAC(file_path)
                keys = ('album', 'albumartist')
            elif ext == '.m4a':
                from mutagen.mp4 import MP4
                audio = MP4(file_path)
                keys = ('\xa9alb', 'aART')
            else:
                return

            wanted = (album, album_artist)
            if all((audio.get(key) or [''])[0] == value for key, value in zip(keys, wanted)):
                return
            for key, value in zip(keys, wanted):
                audio[key] = value
            audio.save()
            logger.info(f"🏷️ Aligned album tags: album='{album}', album artist='{album_artist}'")
        except Exception as exc:
            logger.debug(f"Could not align album tags on {file_path}: {exc}")

    def _organize_downloaded_file(self, downloaded_file: str, target_dir: str, metadata: Dict[str, Any],
                                  artist: str, title: str, album: str, request_context: Optional[str],
                                  request_context_metadata: Dict[str, Any]) -> str:
        """Move the file to <target_dir>/<Artist>/<Album>/<Title>.<ext> and
        return its new path (the original path if the move fails)."""
        try:
            # A cover/variant request is filed under the requested performer,
            # not the original artist the metadata sources may have returned.
            if self._has_variant_request_hint(request_context) and request_context_metadata.get('artist'):
                final_artist = request_context_metadata['artist']
            else:
                final_artist = metadata.get('artist') or artist
            final_album = metadata.get('album') or album

            artist_dir = self._primary_artist(final_artist)
            final_album = balanced(self._clean_album_name(final_album, artist_dir))

            # A slash cannot appear in a directory name but is fine in a tag,
            # so the tag keeps the real name unless an existing folder under a
            # different spelling is reused — then that spelling wins for both.
            def as_dir_name(value):
                return value.replace('/', '_').replace('\\', '_').strip()

            artist_dir = self._existing_dir_match(target_dir, as_dir_name(artist_dir))
            album_dir = self._existing_dir_match(os.path.join(target_dir, artist_dir), as_dir_name(final_album))
            album_tag = final_album if album_dir == as_dir_name(final_album) else album_dir
            organized_dir = os.path.join(target_dir, artist_dir, album_dir)
            os.makedirs(organized_dir, exist_ok=True)

            file_title = balanced(self._filename_title(metadata.get('title') or title))
            if self._has_variant_request_hint(request_context) and file_title.lower() == title.lower():
                variant_label = self._get_explicit_variant_label(request_context) or 'Variant'
                if not any(word in file_title.lower() for word in variant_label.lower().split()):
                    file_title = f"{file_title} ({variant_label})"
            file_ext = os.path.splitext(downloaded_file)[1]
            new_path = os.path.join(organized_dir, f"{file_title.replace('/', '_').replace(chr(92), '_')}{file_ext}")

            if downloaded_file != new_path:
                shutil.move(downloaded_file, new_path)
                logger.info(f"✓ Organized file: {artist_dir}/{album_dir}/{os.path.basename(new_path)}")
                downloaded_file = new_path
            metadata['album'] = album_tag
            self._align_album_tags(downloaded_file, album_tag, artist_dir)
            return downloaded_file
        except Exception as exc:
            logger.warning(f"⚠️ Could not organize {downloaded_file}: {exc}")
        return downloaded_file

    def _save_cover_art(self, downloaded_file: str, cover_url: Optional[str]) -> None:
        if not cover_url:
            return
        cover_path = os.path.join(os.path.dirname(downloaded_file), 'cover.jpg')
        if os.path.exists(cover_path):
            return
        try:
            response = requests.get(cover_url, timeout=15)
            if response.status_code == 200 and response.content:
                with open(cover_path, 'wb') as fh:
                    fh.write(response.content)
                logger.info(f"✓ Cover art saved to {cover_path}")
        except Exception as exc:
            logger.debug(f"Could not download cover art: {exc}")

    def download_track(self, artist: str, title: str, album: str = None, url: str = None,
                      request_type: str = "song", plex_user: str = None, target_dir: str = None, user_index: Optional[int] = None,
                      request_context: Optional[str] = None) -> Dict[str, Any]:
        """Download one track and file it under <target_dir>/<Artist>/<Album>/.

        Pipeline:
        1. Resolve the requested artist/title/album (request context, metadata
           sources, name cleanup) and skip if the track already exists or was
           1-star blacklisted.
        2. Album requests go to Lidarr first when it is configured.
        3. Try deemix → soundcloud → yt-dlp, validating each result against the
           requested track.
        4. Fix up tags, organize the file and save cover art.

        Returns {'success', 'file_path', 'metadata', 'download_tool', 'error', 'plex_user'}.
        """
        result = {
            'success': False,
            'file_path': None,
            'metadata': None,
            'download_tool': None,
            'error': None,
            'plex_user': plex_user,
        }

        try:
            if user_index is None:
                user_index = self._infer_user_index(target_dir=target_dir, plex_user=plex_user)

            if self._block_until_arl_refresh_if_stale('download request'):
                result['error'] = 'Deezer ARL is stale — downloads are paused until admin refreshes the token.'
                return result

            request_context_metadata = self._resolve_effective_download_metadata(
                artist=artist, title=title, album=album, request_context=request_context,
            )
            artist = request_context_metadata.get('artist') or artist
            title = request_context_metadata.get('title') or title

            album_before = album
            metadata_lookup = self.get_metadata_from_sources(artist, title, track_name=title, user_index=user_index)
            if metadata_lookup and metadata_lookup.get('album'):
                album = self._clean_album_name(metadata_lookup['album'], artist)
                logger.info(f"📀 Album from metadata sources: '{album}' (was: '{album_before}')")
            album = request_context_metadata.get('album') or album or title

            target_dir = self._resolve_download_target_dir(target_dir)
            if not target_dir:
                logger.error("No target directory specified and ALBUM_DIR not configured")
                result['error'] = "Target directory not configured"
                return result

            logger.info(f"📥 Downloading track: {artist} - {title} from {album}")
            self._ensure_vpn()

            artist, title = self._clean_request_names(artist, title)
            request_context_metadata = self._resolve_effective_download_metadata(
                artist=artist, title=title, album=album, request_context=request_context,
            )
            artist = request_context_metadata.get('artist') or artist
            title = request_context_metadata.get('title') or title
            result['metadata'] = {
                'title': title, 'artist': artist, 'album': album,
                'year': None, 'genres': [], 'cover_url': None, 'source': 'input',
            }

            existing_file = self._find_existing_track(
                artist, title, target_dir,
                release_title=bool(self._context_fields(request_context).get('deezer_track')))
            if existing_file:
                if self._has_variant_request_hint(request_context):
                    logger.info(f"🔄 Existing track found but a specific variant was requested; downloading anyway: {existing_file}")
                else:
                    logger.info(f"✓ Track already exists, skipping download: {existing_file}")
                    result.update(success=True, file_path=existing_file, download_tool='none')
                    result['metadata']['source'] = 'existing'
                    return result

            if self._is_track_blacklisted(artist, title):
                logger.info(f"🚫 Skipping '{artist} - {title}' — previously 1-star deleted by user")
                result['error'] = '1-star blacklisted'
                return result

            if getattr(self.app_mod, 'LIDARR_USE_AS_DOWNLOADER', False) and request_type == 'album':
                try:
                    lidarr_client = self._get_lidarr_client(user_index)
                    if lidarr_client and lidarr_client.download_album(artist, album or title):
                        logger.info(f"✅ Lidarr download initiated for {artist} - {album or title}")
                        result.update(success=True, download_tool='lidarr')
                        result['metadata']['source'] = 'lidarr'
                        return result
                    logger.debug("Lidarr could not initiate the request; falling back to built-in tools")
                except Exception as exc:
                    logger.debug(f"Lidarr download failed: {exc}")

            # Ordered by audio quality: deemix (Deezer) → soundcloud → yt-dlp.
            tools = self.check_download_tools()
            tools_to_try = [t for t, enabled in (('deemix', tools.get('deemix')), ('soundcloud', True), ('yt-dlp', tools.get('yt-dlp'))) if enabled]

            tools_to_try = self._link_first(tools_to_try, url, request_context)

            downloaded_file = None
            used_tool = None
            for tool in tools_to_try:
                logger.info(f"🎵 Attempting download with {tool}...")
                try:
                    downloaded_file = self._try_tool_download(tool, artist, title, url, target_dir, request_context, plex_user, album)
                except Exception as exc:
                    logger.error(f"❌ {tool} download threw exception: {exc}", exc_info=True)
                    downloaded_file = None
                if downloaded_file:
                    used_tool = tool
                    break

            if not downloaded_file:
                error_msg = f"All download tools failed (available: {[k for k, v in tools.items() if v]})"
                logger.error(f"❌ {error_msg}")
                result['error'] = error_msg
                return result

            result['download_tool'] = used_tool
            result['metadata'] = self._enrich_downloaded_metadata(
                downloaded_file, used_tool, result['metadata'], request_context_metadata,
                artist, title, album, user_index,
            )
            downloaded_file = self._organize_downloaded_file(
                downloaded_file, target_dir, result['metadata'], artist, title, album,
                request_context, request_context_metadata,
            )
            self._save_cover_art(downloaded_file, result['metadata'].get('cover_url'))

            result['success'] = True
            result['file_path'] = downloaded_file
            logger.info(f"✅ Track download complete: {artist} - {title}")
            # Plex refresh is the caller's job (discord_bot.refresh_plex_library).
            return result

        except Exception as exc:
            logger.error(f"❌ Error downloading track {artist} - {title}: {exc}", exc_info=True)
            result['error'] = str(exc)
            return result

    # ═══════════════════════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════════════════
    # API CONFIGURATION CHECK
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def check_api_configuration() -> None:
        """Log the status of every configured API key / service at startup.

        Checks: Last.FM, Discogs, MusicBrainz, AcoustID, ACRCloud, audd.io,
                deemix (ARL), yt-dlp.

        Spotify needs no key: it is read through its public web pages.
        """
        logger.info("=" * 70)
        logger.info("🔑 API / Service configuration check")
        logger.info("=" * 70)

        # ── Last.FM ───────────────────────────────────────────────────────────
        _lastfm = os.environ.get('LAST_FM_API_KEY', '').strip()
        if _lastfm:
            try:
                import pylast
                resp = requests.get(
                    'https://ws.audioscrobbler.com/2.0/',
                    params={'method': 'auth.gettoken', 'api_key': _lastfm, 'format': 'json'},
                    timeout=8,
                )
                data = resp.json()
                if data.get('error'):
                    logger.warning(f"  ❌ Last.FM: API key invalid — {data.get('message', '')}")
                else:
                    logger.info("  ✓ Last.FM: API key valid and reachable")
            except ImportError:
                logger.warning("  ⚠️  Last.FM: pylast not installed — pip install pylast")
            except Exception as _e:
                logger.warning(f"  ⚠️  Last.FM: reachability check failed — {_e}")
        else:
            logger.info("  —  Last.FM: LAST_FM_API_KEY not set (metadata fallback only)")

        # ── Discogs ───────────────────────────────────────────────────────────
        _discogs = os.environ.get('DISCOGS_TOKEN', '').strip()
        if _discogs:
            try:
                resp = requests.get(
                    'https://api.discogs.com/oauth/identity',
                    headers={'Authorization': f'Discogs token={_discogs}',
                             'User-Agent': 'MusicAI/1.0'},
                    timeout=8,
                )
                if resp.status_code == 200:
                    username = resp.json().get('username', 'unknown')
                    logger.info(f"  ✓ Discogs: token valid (user: {username})")
                elif resp.status_code == 401:
                    logger.warning("  ❌ Discogs: token invalid (401 Unauthorized)")
                else:
                    logger.warning(f"  ⚠️  Discogs: unexpected status {resp.status_code}")
            except Exception as _e:
                logger.warning(f"  ⚠️  Discogs: reachability check failed — {_e}")
        else:
            logger.info("  —  Discogs: DISCOGS_TOKEN not set (cover art fallback only)")

        # ── MusicBrainz ───────────────────────────────────────────────────────
        try:
            import musicbrainzngs
            musicbrainzngs.set_useragent("MusicAI", "1.0", "https://localhost")
            musicbrainzngs.search_releases(query="test", limit=1)
            logger.info("  ✓ MusicBrainz: reachable (no auth required)")
        except ImportError:
            logger.warning("  ⚠️  MusicBrainz: musicbrainzngs not installed")
        except Exception as _e:
            logger.warning(f"  ⚠️  MusicBrainz: reachability check failed — {_e}")

        # ── AcoustID ─────────────────────────────────────────────────────────
        _acoustid = os.environ.get('ACOUSTID_API_KEY', '').strip()
        if _acoustid:
            try:
                import acoustid  # type: ignore
                import musicbrainzngs  # type: ignore  # noqa
                logger.info("  ✓ AcoustID: API key set, libraries available")
            except ImportError as _ie:
                logger.warning(f"  ⚠️  AcoustID: key set but library missing — {_ie}")
        else:
            logger.info("  —  AcoustID: ACOUSTID_API_KEY not set")

        # ── ACRCloud ──────────────────────────────────────────────────────────
        _acr_key    = os.environ.get('ACRCLOUD_ACCESS_KEY',    '').strip()
        _acr_secret = os.environ.get('ACRCLOUD_ACCESS_SECRET', '').strip()
        _acr_host   = os.environ.get('ACRCLOUD_HOST',          '').strip()
        if all([_acr_key, _acr_secret, _acr_host]):
            try:
                resp = requests.head(f"https://{_acr_host}/v1/identify", timeout=8)
                logger.info(f"  ✓ ACRCloud: credentials set, endpoint reachable ({resp.status_code})")
            except Exception as _e:
                logger.warning(f"  ⚠️  ACRCloud: credentials set but endpoint unreachable — {_e}")
        elif any([_acr_key, _acr_secret, _acr_host]):
            logger.warning(
                "  ⚠️  ACRCloud: partially configured "
                f"(key={bool(_acr_key)}, secret={bool(_acr_secret)}, host={bool(_acr_host)}) "
                "— all three vars required"
            )
        else:
            logger.info("  —  ACRCloud: ACRCLOUD_* vars not set")

        # ── audd.io ───────────────────────────────────────────────────────────
        _audd = os.environ.get('AUDD_API_TOKEN', '').strip()
        if _audd:
            try:
                resp = requests.post('https://api.audd.io/', data={'api_token': _audd}, timeout=8)
                data = resp.json()
                err = (data.get('error') or {})
                err_msg = (err.get('error_message', '') if isinstance(err, dict) else str(err)).lower()
                if any(w in err_msg for w in ('invalid', 'key', 'token', 'auth')):
                    logger.warning(f"  ❌ audd.io: token invalid — {err_msg}")
                else:
                    logger.info("  ✓ audd.io: token accepted, endpoint reachable")
            except Exception as _e:
                logger.warning(f"  ⚠️  audd.io: reachability check failed — {_e}")
        else:
            logger.info("  —  audd.io: AUDD_API_TOKEN not set")

        # ── Download tools ────────────────────────────────────────────────────
        _tools = {
            'deemix':    bool(os.environ.get('DEEZER_ARL', '').strip()),
            'yt-dlp':    bool(shutil.which('yt-dlp') or _can_import('yt_dlp')),
            'soundcloud': _can_import('soundcloud'),
            'ffmpeg':    bool(shutil.which('ffmpeg')),
        }
        for tool, available in _tools.items():
            icon = '✓' if available else '—'
            level = logger.info if available else logger.warning
            level(f"  {icon}  Download tool [{tool}]: {'available' if available else 'NOT available'}")

        # ── Audio fingerprinting libs ─────────────────────────────────────────
        _fp_libs = ['shazamio', 'acoustid', 'chromaprint']
        for lib in _fp_libs:
            if _can_import(lib):
                logger.info(f"  ✓ Fingerprint lib [{lib}]: installed")
            else:
                logger.debug(f"  —  Fingerprint lib [{lib}]: not installed")

        logger.info("=" * 70)
    # CENTRALIZED METADATA RETRIEVAL
    # ═══════════════════════════════════════════════════════════════════════════════

    def get_metadata_from_sources(self, artist: str, album_name: str, track_name: str = None, user_index: int = 0) -> Dict[str, Any]:
        """
        Comprehensive metadata lookup from multiple sources in priority order:
        1. MusicBrainz / Cover Art Archive
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

        # Collect results from all sources and pick the best one
        # Some sources may have incorrect album names, so we try multiple
        candidates = []

        try:
            # Optional Lidarr metadata lookup when configured
            try:
                if getattr(self.app_mod, 'LIDARR_USE_AS_METADATA', False):
                    lidarr_client = self._get_lidarr_client(user_index)
                    if lidarr_client:
                        logger.debug(f"Searching Lidarr for metadata: {artist} - {album_name}")
                        lidarr_candidate = lidarr_client.get_album_metadata(artist, album_name)
                        if lidarr_candidate:
                            candidates.append(lidarr_candidate)
                            logger.debug(f"✓ Lidarr candidate: {lidarr_candidate['album']}")
            except Exception as e:
                logger.debug(f"Lidarr metadata lookup failed: {e}")

            # 1. Discogs FIRST - often has more accurate album names
            try:
                discogs_token = os.environ.get('DISCOGS_TOKEN')
                if discogs_token:
                    from discogs_client import Client
                    discogs_client = Client('MusicAI/1.0', user_token=discogs_token)
                    logger.debug(f"Searching Discogs for: {artist} - {album_name}")
                    results = discogs_client.search(album_name, artist=artist, type='release')
                    if results.count > 0:
                        release = results[0]
                        candidate = {
                            'album': release.title,
                            'year': str(release.year) if getattr(release, 'year', None) else None,
                            'genres': list(release.genres[:3]) if getattr(release, 'genres', None) else [],
                            'cover_url': release.images[0]['uri'] if getattr(release, 'images', None) else None,
                            'source': 'discogs'
                        }
                        candidates.append(candidate)
                        logger.debug(f"✓ Discogs candidate: {candidate['album']}")
            except Exception as e:
                logger.debug(f"Discogs metadata lookup failed: {e}")

            # 2. MusicBrainz + Cover Art Archive (no auth required)
            try:
                import musicbrainzngs
                musicbrainzngs.set_useragent("MusicAI", "1.0", "https://localhost")
                results = musicbrainzngs.search_releases(
                    query=f'{artist} {album_name}', limit=1
                )
                if results.get('release-list'):
                    release = results['release-list'][0]
                    candidate = {
                        'album': release.get('title', album_name),
                        'year': release['date'].split('-')[0] if release.get('date') else None,
                        'genres': [],
                        'cover_url': None,
                        'source': 'musicbrainz_caa'
                    }
                    mbid = release.get('id')
                    if mbid:
                        caa_url = f"https://coverartarchive.org/release/{mbid}/front-250"
                        try:
                            r = requests.head(caa_url, timeout=5, allow_redirects=True)
                            if r.status_code == 200:
                                candidate['cover_url'] = caa_url
                        except Exception:
                            pass
                    candidates.append(candidate)
                    logger.debug(f"✓ MusicBrainz candidate: {candidate['album']}")
            except Exception as e:
                logger.debug(f"MusicBrainz+CAA metadata lookup failed: {e}")

            # 3. LastFM
            try:
                lastfm_api_key = os.environ.get('LAST_FM_API_KEY')
                if lastfm_api_key:
                    import pylast
                    network = pylast.LastFMNetwork(api_key=lastfm_api_key)
                    logger.debug(f"Searching LastFM for: {artist} - {album_name}")
                    album_obj = network.get_album(artist, album_name)
                    candidate = {
                        'album': album_obj.name,
                        'year': None,
                        'genres': [],
                        'cover_url': album_obj.get_cover_image() if album_obj.get_cover_image() else None,
                        'source': 'lastfm'
                    }
                    try:
                        release_date = album_obj.get_release_date()
                        if release_date:
                            candidate['year'] = str(release_date).split('-')[0]
                    except Exception:
                        pass
                    candidates.append(candidate)
                    logger.debug(f"✓ LastFM candidate: {candidate['album']}")
            except Exception as e:
                logger.debug(f"LastFM metadata lookup failed: {e}")

            # 4. MusicBrainz (detailed release lookup, second pass)
            try:
                import musicbrainzngs
                musicbrainzngs.set_useragent("MusicAI", "1.0", "https://localhost")
                logger.debug(f"Searching MusicBrainz for: {artist} - {album_name}")
                results = musicbrainzngs.search_releases(query=f'{artist} {album_name}', limit=1)

                if results.get('release-list'):
                    release = results['release-list'][0]
                    candidate = {
                        'album': release.get('title', album_name),
                        'year': release['date'].split('-')[0] if release.get('date') else None,
                        'genres': [],
                        'cover_url': None,
                        'source': 'musicbrainz'
                    }
                    if release.get('id'):
                        try:
                            release_detail = musicbrainzngs.get_release_by_id(release['id'], includes=['coverart-front'])
                            if 'release' in release_detail and 'coverart-front' in release_detail['release']:
                                candidate['cover_url'] = release_detail['release']['coverart-front']
                        except Exception:
                            pass
                    candidates.append(candidate)
                    logger.debug(f"✓ MusicBrainz (detailed) candidate: {candidate['album']}")
            except Exception as e:
                logger.debug(f"MusicBrainz metadata lookup failed: {e}")

            # 5. YouTube fallback
            try:
                from yt_dlp import YoutubeDL
                
                search_query = f"{artist} {album_name} album cover"
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': True,
                    'default_search': 'ytsearch1',
                }
                
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(search_query, download=False)
                    if info and info.get('thumbnail'):
                        candidate = {
                            'album': album_name,
                            'year': None,
                            'genres': [],
                            'cover_url': info.get('thumbnail'),
                            'source': 'youtube'
                        }
                        candidates.append(candidate)
                        logger.debug(f"✓ YouTube candidate: {candidate['album']}")
            except Exception as e:
                logger.debug(f"YouTube metadata lookup failed: {e}")

            # Select the best candidate from all sources
            # Prefer Discogs > MusicBrainz > others (Discogs usually has more accurate album names)
            # A release named for another version of the song is not this
            # recording's album: MusicBrainz matched "RISE" to "RISE
            # (instrumental)" for the song with vocals.
            from song_dedup import variant_labels
            wanted = set(variant_labels(track_name or '')) | set(variant_labels(album_name or ''))
            other_versions = [c for c in candidates if not set(variant_labels(c.get('album') or '')) <= wanted]
            for c in other_versions:
                logger.info(f"Ignoring {c.get('source')} album '{c.get('album')}': another version of the song")
            candidates = [c for c in candidates if c not in other_versions]
            if candidates:
                # Sort by source priority: discogs > musicbrainz_caa > others
                source_priority = {'discogs': 0, 'musicbrainz_caa': 1, 'musicbrainz': 2, 'lastfm': 3}
                best = min(candidates, key=lambda c: source_priority.get(c.get('source', ''), 99))
                metadata.update(best)
                logger.info(f"✓ Found album metadata from {metadata['source']}: {metadata['album']}")
                return metadata

            logger.warning(f"No metadata found from any source for: {artist} - {album_name}")

        except Exception as e:
            logger.error(f"Error in metadata pipeline: {e}")

        return metadata

    def download_cover_art(self, artist: str, album_name: str, target_dir: str,
                          force_update: bool = False, user_index: int = 0) -> bool:
        """
        Download and store album cover image in the album directory.
        Uses the metadata fallback pipeline to find cover art.
        Saves as 'cover.jpg' in the album directory.
        """
        try:
            cover_path = os.path.join(target_dir, "cover.jpg")
            
            # Skip if cover already exists (unless force_update)
            if os.path.exists(cover_path) and not force_update:
                logger.debug(f"Cover already exists at {cover_path}")
                return True
            
            # Use metadata pipeline to find cover art
            logger.debug(f"Fetching cover art metadata for: {artist} - {album_name}")
            metadata = self.get_metadata_from_sources(artist, album_name, user_index=user_index)
            
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

                if response.status_code == 200 and response.content:
                    # Verify it's actually an image by checking content-type or magic bytes
                    content_type = response.headers.get('content-type', '').lower()
                    if 'image' not in content_type and not response.content.startswith((b'\xff\xd8', b'\x89\x50\x4e', b'\x47\x49\x46')):
                        logger.debug(f"Content-type is {content_type}, but attempting to save anyway")
                    
                    os.makedirs(target_dir, exist_ok=True)
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
            return False


    def _extract_artist_title_from_file(self, file_path: str) -> Tuple[Optional[str], Optional[str]]:
        """Attempt to extract artist and title from file tags or filename.

        Returns (artist, title) or (None, None) on failure.
        """
        try:
            from mutagen.easyid3 import EasyID3
            from mutagen.mp4 import MP4
            from mutagen.flac import FLAC
        except Exception:
            # Mutagen not available
            logger.debug("mutagen not available for tag extraction")
            return None, None

        artist = None
        title = None
        try:
            ext = os.path.splitext(file_path)[1].lower()
            if ext == '.mp3':
                try:
                    audio = EasyID3(file_path)
                    artist = audio.get('artist', [None])[0]
                    title = audio.get('title', [None])[0]
                except Exception:
                    pass
            elif ext in ('.m4a', '.mp4'):
                try:
                    audio = MP4(file_path)
                    artist = audio.tags.get('\xa9ART', [None])[0] if audio.tags else None
                    title = audio.tags.get('\xa9nam', [None])[0] if audio.tags else None
                except Exception:
                    pass
            elif ext == '.flac':
                try:
                    audio = FLAC(file_path)
                    artist = audio.tags.get('artist', [None])[0] if audio.tags else None
                    title = audio.tags.get('title', [None])[0] if audio.tags else None
                except Exception:
                    pass
        except Exception:
            logger.debug(f"Error reading tags for {file_path}")

        # Fallback to filename parsing: handle variants like
        # 'Artist - Title.ext', 'NA - Title - Artist (Official).ext', etc.
        if not artist or not title:
            base = os.path.basename(file_path)
            name = os.path.splitext(base)[0]
            # Normalize common markers
            def strip_parentheticals(s: str) -> str:
                return re.sub(r"\s*[\[\(].*?[\]\)]", '', s).strip()

            # Split on ' - ' into parts; handle multi-part filenames
            parts = [p.strip() for p in name.split(' - ')]
            if len(parts) == 1:
                # No separator; use whole name as title
                title = title or strip_parentheticals(parts[0])
            elif len(parts) == 2:
                # Common case: Artist - Title
                artist = artist or strip_parentheticals(parts[0])
                title = title or strip_parentheticals(parts[1])
            else:
                # Multi-part: could be 'NA - Title - Artist' or 'Artist - Title - Extra'
                # Heuristic: if first part is a placeholder like 'NA' or 'Unknown', prefer last as artist
                first = parts[0].lower()
                placeholders = ('na', 'unknown', '')
                if first in placeholders:
                    # artist likely last part, title is middle(s)
                    artist = artist or strip_parentheticals(parts[-1])
                    title_candidate = ' - '.join(parts[1:-1])
                    title = title or strip_parentheticals(title_candidate)
                else:
                    # Otherwise assume Artist - Title - Extra → artist=first, title=second
                    artist = artist or strip_parentheticals(parts[0])
                    title = title or strip_parentheticals(parts[1])

        # Final cleanup: normalize whitespace
        if artist:
            artist = artist.strip()
        if title:
            title = title.strip()

        return artist, title


    def _is_download_match(self, file_path: str, expected_artist: str, expected_title: str, threshold: float = 0.65,
                           request_context: Optional[str] = None) -> bool:
        """Compare extracted artist/title from downloaded file vs expected values.

        Uses a weighted similarity (title 60%, artist 40%) and returns True
        if combined score >= threshold.
        """
        logger.info(f"🔍 _is_download_match: file={os.path.basename(file_path)}, expected={expected_artist} - {expected_title}")
        try:
            extracted_artist, extracted_title = self._extract_artist_title_from_file(file_path)
            logger.info(f"🔍 Extracted from file: artist='{extracted_artist}', title='{extracted_title}'")

            # If tags/filename parsing yielded unusable artist (e.g., 'NA', 'Unknown'),
            # attempt a more robust filename parsing that can handle formats like
            # 'Album - Title - Artist' produced by yt-dlp.
            def _parse_filename_fallback():
                try:
                    return self._parse_artist_title_from_filename(file_path, expected_artist, expected_title)
                except Exception:
                    return None, None

            if (not extracted_artist or extracted_artist.lower() in ('na', 'unknown', 'none')) or (extracted_title and expected_artist.lower() in extracted_title.lower()):
                logger.info(f"🔍 Artist looks like placeholder, trying fallback parsing...")
                p_artist, p_title = _parse_filename_fallback()
                logger.info(f"🔍 Fallback parsing returned: artist='{p_artist}', title='{p_title}'")
                if p_artist:
                    extracted_artist = extracted_artist or p_artist
                if p_title:
                    extracted_title = extracted_title or p_title
                logger.info(f"🔍 After fallback: artist='{extracted_artist}', title='{extracted_title}'")
            if not extracted_artist and not extracted_title:
                logger.debug(f"Could not extract artist/title from downloaded file {file_path}")
                return False

            def normalize(s: Optional[str]) -> str:
                if not s:
                    return ''
                s = s.casefold()
                # Remove parenthetical content and common tags
                s = re.sub(r"\s*[\[\(].*?[\]\)]", '', s)
                s = s.replace('official video', '').replace('official audio', '')
                s = s.replace('official visualizer', '').replace('audio', '').replace('video', '')
                # Keep letters and digits of every script (Japanese, Arabic,
                # Cyrillic titles must not collapse to an empty string).
                s = re.sub(r"[^\w\s]|_", ' ', s)
                # Normalize 'feat' markers
                s = re.sub(r"\b(feat|ft)\b.*", '', s)
                s = re.sub(r"\s+", ' ', s).strip()
                return s

            # Bracketed labels are ignored in the comparison below, so another
            # version would score as the song itself: it must be the version
            # the request names. Without a request context, only a vocals- or
            # backing-only version is refused.
            have = self._variants_of(extracted_title) | self._variants_of(os.path.splitext(os.path.basename(file_path))[0])
            wanted = self._requested_variants(expected_title, request_context)
            wrong = (have - wanted) | (wanted - have) if request_context is not None \
                else (have - wanted) & self._NOT_THE_SONG
            if wrong:
                logger.info(f"❌ {os.path.basename(file_path)} is version {sorted(have) or 'original'}, the request "
                            f"asks for {sorted(wanted) or 'the original'}")
                return False

            ea = normalize(extracted_artist)
            et = normalize(extracted_title)
            qa = normalize(expected_artist)
            qt = normalize(expected_title)

            def sim(a: str, b: str) -> float:
                if not a or not b:
                    return 0.0
                direct = SequenceMatcher(None, a, b).ratio()
                # Fall back to transliterated comparison for cross-script matches
                # (e.g. expected title "детство" vs a downloaded file tagged with
                # the Latin transliteration "Detstvo" — same song, different
                # script, so a direct character comparison scores near zero).
                if direct < 1.0 and _UNIDECODE_AVAILABLE:
                    try:
                        translit = SequenceMatcher(None, _unidecode(a), _unidecode(b)).ratio()
                        return max(direct, translit)
                    except Exception:
                        pass
                return direct

            title_score  = sim(et, qt)
            artist_score = sim(ea, qa)

            # When expected_artist is blank (fan-channel was stripped), the downloaded
            # file's artist is by definition unknown — weight title at 100% and also
            # check whether the title contains the artist name (e.g. "Last resort
            # papa roach" contains both "Last Resort" and "Papa Roach").
            if not qa:
                # Title-only mode: also check if file title appears inside expected title
                title_in_query = sim(et, qt)
                # Bonus: if file artist appears anywhere in expected title string
                artist_in_query = sim(ea, qt) if ea else 0.0
                combined = max(title_in_query, (title_in_query + artist_in_query) / 2)
                logger.info(
                    f"Validation scores for {os.path.basename(file_path)} — "
                    f"title-only mode (no expected artist): "
                    f"title: {title_in_query:.2f}, artist-in-title: {artist_in_query:.2f}, "
                    f"combined: {combined:.2f} (threshold: {threshold})"
                )
                return combined >= threshold

            combined     = (0.6 * title_score) + (0.4 * artist_score)

            # Also score the swapped combination — if expected artist/title were
            # transposed, the swapped score will be higher and correctly accepts the file.
            combined_sw = (0.6 * sim(et, qa)) + (0.4 * sim(ea, qt))

            best = max(combined, combined_sw)
            if combined_sw > combined and combined_sw >= threshold:
                logger.info(
                    f"Validation scores for {os.path.basename(file_path)} — "
                    f"normal: {combined:.2f}, swapped: {combined_sw:.2f} "
                    f"(accepted via swapped match — expected values were transposed)"
                )
            else:
                logger.info(
                    f"Validation scores for {os.path.basename(file_path)} — "
                    f"title: {title_score:.2f}, artist: {artist_score:.2f}, "
                    f"combined: {combined:.2f} (threshold: {threshold})"
                )
            return best >= threshold
        except Exception as e:
            logger.debug(f"Error validating downloaded file {file_path}: {e}")
            return False


    def _parse_artist_title_from_filename(self, file_path: str, expected_artist: str = None, expected_title: str = None) -> Tuple[Optional[str], Optional[str]]:
        """Heuristic parser for filenames that extracts (artist, title).

        Handles multi-part filenames like 'NA - Title - Artist' and common
        markers produced by `yt-dlp`.
        """
        try:
            base = os.path.basename(file_path)
            name = os.path.splitext(base)[0]
            parts = [p.strip() for p in name.split(' - ')]

            def clean(s: str) -> str:
                s = re.sub(r"\s*[\[\(].*?[\]\)]", '', s)
                s = re.sub(r"[^\w\s]", ' ', s)
                s = re.sub(r"\s+", ' ', s).strip()
                return s

            if len(parts) == 1:
                return None, clean(parts[0])
            if len(parts) == 2:
                # Prefer Artist - Title
                a = clean(parts[0])
                t = clean(parts[1])
                # If expected_artist looks more like title, swap heuristically
                if expected_artist and expected_artist.lower() in t.lower():
                    return t, a
                return a, t
            # len >= 3
            first_low = parts[0].lower()
            placeholders = ('na', 'unknown', 'untitled', '')
            if first_low in placeholders:
                # Assume format: NA - Title - Artist
                artist = clean(parts[-1])
                title = clean(' - '.join(parts[1:-1]))
                return artist, title
            else:
                # Assume Artist - Title - Extra -> artist=first, title=second
                artist = clean(parts[0])
                title = clean(parts[1])
                return artist, title
        except Exception:
            return None, None

    # ═══════════════════════════════════════════════════════════════════════════════
    # CONVENIENCE METHODS FOR COMMON DOWNLOAD PATTERNS
    # ═══════════════════════════════════════════════════════════════════════════════
