"""Route around region-locked content by moving the Mullvad exit relay.

The tunnel covers the whole container, so a relay change is global and is
kept as short as possible: a request that hits a geo-block looks up where
the content *is* available, moves the relay to one of those countries for
the rest of that request, and `restore()` puts the configured relay back
once the request is finished. If no Mullvad relay exists in any allowed
country the request simply fails.

Only YouTube exposes its allow-list (`availableCountries` in the watch page),
so that is the only source that can be resolved; other sources just fail.
"""
import logging
import os
import re
import subprocess
import threading
import time
from typing import Iterable, List, Optional, Set

import requests

logger = logging.getLogger('musicai.app')

# yt-dlp reports most region locks only as "Video unavailable"; the watch
# page's allow-list then decides whether it really is one.
GEO_BLOCK_MARKERS = (
    "video unavailable",
    "in your country",
    "in your region",
    "in your location",
    "geo-restricted",
    "geo restricted",
    "georestricted",
    "geoblocked",
    "geo-blocked",
)

# Countries tried first when several relays could serve the content; the
# rest of the intersection is tried alphabetically.
PREFERRED_COUNTRIES = ('us', 'gb', 'de', 'nl', 'se', 'ca', 'fr', 'jp', 'au', 'br')

_YT_VIDEO_ID_RE = re.compile(r'(?:v=|youtu\.be/|/shorts/|/embed/|\[youtube\] )([A-Za-z0-9_-]{11})')
_YT_AVAILABLE_RE = re.compile(r'"availableCountries"\s*:\s*\[((?:\s*"[A-Z]{2}"\s*,?)+)\]')

# YouTube answers some VPN exit IPs with a sign-in wall instead of the
# video; that is a reason to try another country, not a region lock.
BOT_CHECK_MARKERS = ("sign in to confirm", "not a bot")

# Seconds to let routing and DNS settle after the relay moves.
SETTLE_SECONDS = 3

_lock = threading.RLock()
_original_location: Optional[str] = None
_override_country: Optional[str] = None
_tried_countries: Set[str] = set()


def enabled() -> bool:
    return os.environ.get('GEO_RELAY_FALLBACK', 'true').strip().lower() not in ('0', 'false', 'no', 'off')


def is_geo_block_error(text: Optional[str]) -> bool:
    if not text:
        return False
    lowered = str(text).lower()
    return any(marker in lowered for marker in GEO_BLOCK_MARKERS)


def is_bot_check_error(text: Optional[str]) -> bool:
    lowered = str(text or '').lower()
    return any(marker in lowered for marker in BOT_CHECK_MARKERS)


def is_retryable_elsewhere(text: Optional[str]) -> bool:
    """True when a failure might succeed from a different relay."""
    return is_geo_block_error(text) or is_bot_check_error(text)


def max_countries() -> int:
    """How many countries one request may move through before giving up."""
    try:
        return max(1, int(os.environ.get('GEO_RELAY_MAX_COUNTRIES', '3')))
    except ValueError:
        return 3


def youtube_video_id(text: Optional[str]) -> Optional[str]:
    """Video id from a YouTube URL or from a yt-dlp error message."""
    match = _YT_VIDEO_ID_RE.search(text or '')
    return match.group(1) if match else None


# ── Mullvad CLI ──────────────────────────────────────────────────────────────

def _run(cmd: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def current_location() -> Optional[str]:
    """The relay constraint as `mullvad relay set location` accepts it:
    'any', 'us', 'us nyc' or 'us nyc us-nyc-wg-001'."""
    try:
        out = _run(['mullvad', 'relay', 'get']).stdout
    except Exception as exc:
        logger.debug(f"[GEO] mullvad relay get failed: {exc}")
        return None
    match = re.search(r'^\s*Location:\s*(.+?)\s*$', out, re.M)
    if not match:
        return None
    value = match.group(1).strip().lower()
    if value == 'any':
        return 'any'
    parts = [p.strip() for p in value.split(',')]
    kind, _, first = parts[0].partition(' ')
    if kind == 'country':
        return first
    if kind == 'city' and len(parts) >= 2:
        return f"{parts[-1]} {first}"
    if kind == 'hostname' and len(parts) >= 3:
        return f"{parts[-1]} {parts[-2]} {first}"
    return None


def current_country() -> Optional[str]:
    location = current_location()
    if not location or location == 'any':
        return None
    return location.split()[0]


def relay_countries() -> Set[str]:
    """Country codes Mullvad has relays in."""
    try:
        out = _run(['mullvad', 'relay', 'list'], timeout=60).stdout
    except Exception as exc:
        logger.debug(f"[GEO] mullvad relay list failed: {exc}")
        return set()
    return {m.group(1) for m in re.finditer(r'^\S.*\(([a-z]{2})\)\s*$', out, re.M)}


def switch_to(location: str) -> bool:
    try:
        set_result = _run(['mullvad', 'relay', 'set', 'location', *location.split()])
        if set_result.returncode != 0:
            logger.warning(f"[GEO] Could not set relay location '{location}': {(set_result.stderr or '').strip()}")
            return False
        _run(['mullvad', 'connect', '--wait'], timeout=60)
        # `connect --wait` can return while the daemon still reports
        # "Connecting"; give the tunnel a moment to settle.
        status, connected = '', False
        for _ in range(15):
            status = _run(['mullvad', 'status']).stdout
            connected = status.lstrip().lower().startswith('connected')
            if connected:
                break
            time.sleep(1)
    except Exception as exc:
        logger.warning(f"[GEO] Relay switch to '{location}' failed: {exc}")
        return False
    if connected:
        # The first request straight after a switch often fails while routing
        # and DNS settle, which would look like an unrelated error.
        time.sleep(SETTLE_SECONDS)
        visible = re.search(r'Visible location:\s*(.+)', status)
        logger.info(f"[GEO] Mullvad relay now '{location}'" + (f" ({visible.group(1).strip()})" if visible else ''))
    else:
        logger.warning(f"[GEO] Mullvad did not reach Connected after switching to '{location}'")
    return connected


# ── Content allow-lists ──────────────────────────────────────────────────────

def youtube_available_countries(video_id: str) -> Optional[List[str]]:
    """Lower-case country codes the video may be played in, or None if the
    watch page does not say (which usually means no restriction)."""
    try:
        response = requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en-US,en;q=0.9'},
            cookies={'CONSENT': 'YES+1', 'SOCS': 'CAI'},
            timeout=20,
        )
    except Exception as exc:
        logger.debug(f"[GEO] Could not fetch watch page for {video_id}: {exc}")
        return None
    match = _YT_AVAILABLE_RE.search(response.text or '')
    if not match:
        return None
    return sorted({code.strip().strip('"').lower() for code in match.group(1).split(',') if code.strip()})


def pick_country(available: Iterable[str], relays: Set[str]) -> Optional[str]:
    candidates = set(available) & relays
    if not candidates:
        return None
    for country in PREFERRED_COUNTRIES:
        if country in candidates:
            return country
    return sorted(candidates)[0]


# ── Override lifecycle ───────────────────────────────────────────────────────

def override_for(available: Iterable[str], reason: str = '') -> Optional[str]:
    """Move the relay into one of `available` (country codes). Returns the
    country now in use, or None when no relay can serve the content."""
    global _original_location, _override_country
    if not enabled():
        logger.info("[GEO] Relay fallback disabled (GEO_RELAY_FALLBACK=false)")
        return None
    with _lock:
        if len(_tried_countries) >= max_countries():
            logger.warning(f"[GEO] Already tried {sorted(_tried_countries)} for this request; giving up")
            return None
        available = {c.lower() for c in available}
        here = current_country()
        excluded = _tried_countries | ({here} if here else set())
        choice = pick_country(available - excluded, relay_countries())
        if not choice:
            logger.warning(f"[GEO] No untried Mullvad relay in an allowed country for {reason or 'this content'} "
                           f"(allowed: {', '.join(sorted(available)) or 'none'}; tried: {', '.join(sorted(excluded)) or 'none'})")
            return None
        if _original_location is None:
            _original_location = current_location() or 'any'
        logger.info(f"[GEO] {reason or 'Content'} cannot be fetched from '{here or 'unknown'}'; moving relay to '{choice}'")
        _tried_countries.add(choice)
        if not switch_to(choice):
            return None
        _override_country = choice
        return choice


def resolve_youtube(url_or_error: str, reason: str = '', error_text: Optional[str] = None) -> Optional[str]:
    """Move the relay so a failed YouTube fetch can be retried elsewhere.

    A region lock names the countries the video may be played in, so the
    relay moves into one of them. A bot check names nothing — YouTube simply
    distrusts this exit IP — so any other relay country is worth trying.
    Returns the new country, or None when nothing can be done.
    """
    video_id = youtube_video_id(url_or_error) or youtube_video_id(error_text or '')
    label = video_id or 'this video'
    allowed = youtube_available_countries(video_id) if video_id else None
    here = current_country()

    if allowed:
        if active() or not here or here not in allowed:
            return override_for(allowed, reason or f"YouTube video {label}")
        logger.info(f"[GEO] {label} is allowed in '{here}'; failure is not a region lock")

    if is_bot_check_error(error_text):
        relays = relay_countries()
        if not relays:
            return None
        logger.info(f"[GEO] YouTube bot-checked this exit IP for {label}; trying another country")
        return override_for(relays, reason or f"YouTube bot check for {label}")

    if not allowed:
        logger.info(f"[GEO] YouTube gives no allow-list for {label}; not a region lock")
    return None


def active() -> bool:
    return _override_country is not None


def restore() -> None:
    """Put the configured relay back after a request that moved it."""
    global _original_location, _override_country
    with _lock:
        _tried_countries.clear()
        if _override_country is None:
            return
        target = _original_location or 'any'
        logger.info(f"[GEO] Restoring Mullvad relay to '{target}'")
        switch_to(target)
        _original_location = None
        _override_country = None
