import urllib.request

import pytest
import requests
from requests.adapters import BaseAdapter

import metadata_proxy as mp

PROXY = "http://172.21.0.248:8888"


@pytest.fixture
def routed(monkeypatch):
    """install() with METADATA_PROXY set; everything it patches is put back."""
    import musicbrainzngs.compat
    import pylast
    monkeypatch.setenv("METADATA_PROXY", PROXY)
    monkeypatch.delenv("METADATA_PROXY_HOSTS", raising=False)
    monkeypatch.setattr(mp, "_installed", False)
    monkeypatch.setattr(requests.Session, "request", requests.Session.request)
    monkeypatch.setattr(musicbrainzngs.compat, "build_opener", musicbrainzngs.compat.build_opener)
    monkeypatch.setattr(pylast._Network, "__init__", pylast._Network.__init__)
    assert mp.install()


class _Recorder(BaseAdapter):
    """A transport that records the proxies each request was sent with."""

    def __init__(self, fail_through_proxy=False):
        super().__init__()
        self.sent, self.fail_through_proxy = [], fail_through_proxy

    def send(self, request, proxies=None, **kwargs):
        proxy = (proxies or {}).get("https")
        if proxy and self.fail_through_proxy:
            raise requests.exceptions.ProxyError("proxy down")
        self.sent.append((request.url, proxy))
        response = requests.Response()
        response.status_code, response.url, response.request = 200, request.url, request
        return response

    def close(self):
        pass


def _session(adapter):
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", adapter)
    return session


def test_only_metadata_services_use_the_proxy(monkeypatch):
    monkeypatch.setenv("METADATA_PROXY", PROXY)
    monkeypatch.delenv("METADATA_PROXY_HOSTS", raising=False)
    for url in ("https://api.genius.com/search", "https://lrclib.net/api/get", "https://musicbrainz.org/ws/2",
                "https://ia800.us.archive.org/cover.jpg", "https://identify-eu-west-1.acrcloud.com/v1/identify",
                "https://open.spotify.com/track/x"):
        assert mp.proxy_for(url) == PROXY, url
    # Where downloads come from stays in the tunnel, lookups included.
    for url in ("https://api.deezer.com/search", "https://www.deezer.com/ajax/gw-light.php",
                "https://e-cdns-proxy-1.dzcdn.net/mobile/1/x", "https://www.youtube.com/watch?v=x",
                "https://rr3---sn-x.googlevideo.com/videoplayback", "https://api-v2.soundcloud.com/tracks",
                "https://notgenius.com.evil.example/", "http://172.21.0.249:4000/v1/models"):
        assert mp.proxy_for(url) is None, url


def test_nothing_changes_without_a_proxy(monkeypatch):
    monkeypatch.delenv("METADATA_PROXY", raising=False)
    monkeypatch.setattr(mp, "_installed", False)
    before = requests.Session.request
    assert not mp.install() and requests.Session.request is before
    assert mp.proxy_for("https://api.genius.com/search") is None


def test_requests_to_metadata_services_go_through_the_proxy(routed):
    adapter = _Recorder()
    session = _session(adapter)
    session.get("https://api.genius.com/search", params={"q": "rain"})
    session.get("https://api.deezer.com/search")
    session.get("https://lrclib.net/api/get", proxies={})            # the caller's own choice stands
    assert [p for _u, p in adapter.sent] == [PROXY, None, None]


def test_a_proxy_that_is_down_falls_back_to_the_tunnel(routed):
    adapter = _Recorder(fail_through_proxy=True)
    assert _session(adapter).get("https://api.genius.com/search").status_code == 200
    assert adapter.sent == [("https://api.genius.com/search", None)]


def test_musicbrainz_goes_through_the_proxy(routed):
    import musicbrainzngs.compat
    opener = musicbrainzngs.compat.build_opener(urllib.request.HTTPHandler())
    proxies = [h.proxies for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert {"http": PROXY, "https": PROXY} in proxies


def test_lastfm_goes_through_the_proxy(routed):
    import pylast
    assert PROXY in pylast.LastFMNetwork(api_key="x").proxy.values()     # httpx form: {"https://": …}
