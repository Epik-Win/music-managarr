"""Metadata lookups leave outside the VPN; downloads stay in it.

MusicAI's container runs Mullvad, so everything it fetches would go through
the tunnel. Downloads should (VPN_REQUIRED), but looking up tags, lyrics,
cover art and fingerprints doesn't need to. With METADATA_PROXY set — a
forward proxy on the Docker network, which the tunnel lets through like the
LAN (docker/metadata-proxy) — requests to the metadata services below go
through it.

It is an allowlist: only these hosts use the proxy; everything else stays
in the tunnel. Deezer, YouTube and SoundCloud are not on it even for
lookups, since they are where downloads come from.

    METADATA_PROXY=http://172.21.0.248:8888
    METADATA_PROXY_HOSTS=genius.com,lrclib.net,…   (replaces the list below)

Each process calls install() once at start: requests (most lookups, and the
Discogs, AcoustID and ACRCloud clients), musicbrainzngs (urllib) and pylast
(httpx) are routed there; Shazam takes `proxy_for()` per call.
"""
import logging
import os
import threading
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("musicai.metadata_proxy")

DEFAULT_HOSTS: Tuple[str, ...] = (
    "genius.com", "lrclib.net",                                   # lyrics
    "musicbrainz.org", "coverartarchive.org", "archive.org",      # tags, cover art
    "discogs.com", "audioscrobbler.com", "last.fm",
    "itunes.apple.com", "mzstatic.com",
    "acoustid.org", "acrcloud.com", "audd.io", "shazam.com",      # fingerprints
    "spotify.com", "scdn.co", "billboard.com",                    # links, charts
)

_installed = False
_lock = threading.Lock()


def proxy_url() -> str:
    return os.environ.get("METADATA_PROXY", "").strip()


def hosts() -> Tuple[str, ...]:
    raw = os.environ.get("METADATA_PROXY_HOSTS", "").strip()
    if not raw:
        return DEFAULT_HOSTS
    return tuple(h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip())


def uses_proxy(url: str) -> bool:
    """Whether a request to `url` goes through METADATA_PROXY."""
    if not proxy_url():
        return False
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts())


def proxy_for(url: str) -> Optional[str]:
    """METADATA_PROXY for a metadata service's URL, else None (the tunnel)."""
    return proxy_url() if uses_proxy(url) else None


def install() -> bool:
    """Route this process's metadata lookups through METADATA_PROXY; does
    nothing when it isn't set. True once installed."""
    global _installed
    with _lock:
        if _installed or not proxy_url():
            return _installed
        _route_requests()
        _route_musicbrainz()
        _route_lastfm()
        _installed = True
    logger.info(f"Metadata lookups go through {proxy_url()}, outside the VPN; downloads stay in the tunnel")
    return True


def _route_requests() -> None:
    import requests
    original = requests.Session.request
    warned = []

    def request(self, method, url, *args, **kwargs):
        # proxies is Session.request's ninth parameter after url: a caller
        # that passes it, by position or name, keeps its own choice.
        proxy = proxy_for(str(url)) if len(args) < 9 and kwargs.get("proxies") is None else None
        if not proxy:
            return original(self, method, url, *args, **kwargs)
        try:
            return original(self, method, url, *args, **dict(kwargs, proxies={"http": proxy, "https": proxy}))
        except requests.exceptions.ProxyError as exc:
            # The proxy is down: the lookup still works through the tunnel.
            if not warned:
                warned.append(True)
                logger.warning(f"Metadata proxy {proxy} unreachable ({exc}); using the VPN for lookups")
            return original(self, method, url, *args, **kwargs)

    request.__wrapped__ = original
    requests.Session.request = request


def _route_musicbrainz() -> None:
    try:
        import urllib.request
        import musicbrainzngs
        from musicbrainzngs import compat
    except ImportError:
        return
    original = compat.build_opener

    def build_opener(*handlers):
        proxy = proxy_for(f"https://{musicbrainzngs.musicbrainz.hostname}/")
        if proxy:
            handlers = (urllib.request.ProxyHandler({"http": proxy, "https": proxy}),) + handlers
        return original(*handlers)

    compat.build_opener = build_opener


def _route_lastfm() -> None:
    try:
        import pylast
    except ImportError:
        return
    original = pylast._Network.__init__

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        proxy = proxy_for(f"https://{getattr(self, 'ws_server', ('ws.audioscrobbler.com',))[0]}/")
        if proxy:
            self.enable_proxy(proxy)

    pylast._Network.__init__ = __init__
