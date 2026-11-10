"""geo_relay: region-lock detection, allow-list parsing and relay override."""
import subprocess
from types import SimpleNamespace

import geo_relay


RELAY_GET = """Generic constraints
    Location:               city nyc, us
    Provider(s):            any
"""

RELAY_LIST = """Albania (al)
\tTirana (tia) @ 41.3N, 19.8W
\t\tal-tia-wg-001 (1.2.3.4) - hosted by x

Japan (jp)
\tTokyo (tyo) @ 35.6N, 139.7E
\t\tjp-tyo-wg-001 (1.2.3.5) - hosted by y

USA (us)
\tNew York, NY (nyc) @ 40.7N, 74.0W
\t\tus-nyc-wg-506 (1.2.3.6) - hosted by z
"""


def _completed(stdout='', returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr='')


def test_is_geo_block_error_matches_yt_dlp_messages():
    assert geo_relay.is_geo_block_error("ERROR: [youtube] abc: The uploader has not made this video available in your country")
    assert geo_relay.is_geo_block_error("This video is not available in your region")
    assert geo_relay.is_geo_block_error("ERROR: [youtube] 8G1DPFOykcY: Video unavailable")
    assert not geo_relay.is_geo_block_error("HTTP Error 403: Forbidden")
    assert not geo_relay.is_geo_block_error(None)


def test_youtube_video_id_from_url_and_error():
    assert geo_relay.youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=x") == "dQw4w9WgXcQ"
    assert geo_relay.youtube_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert geo_relay.youtube_video_id("ERROR: [youtube] dQw4w9WgXcQ: not available in your country") == "dQw4w9WgXcQ"
    assert geo_relay.youtube_video_id("ytsearch1:some song") is None


def test_current_location_parses_city_and_country(monkeypatch):
    monkeypatch.setattr(geo_relay, '_run', lambda cmd, timeout=30: _completed(RELAY_GET))
    assert geo_relay.current_location() == 'us nyc'
    assert geo_relay.current_country() == 'us'

    monkeypatch.setattr(geo_relay, '_run', lambda cmd, timeout=30: _completed("    Location:               country se\n"))
    assert geo_relay.current_location() == 'se'

    monkeypatch.setattr(geo_relay, '_run', lambda cmd, timeout=30: _completed("    Location:               any\n"))
    assert geo_relay.current_location() == 'any'
    assert geo_relay.current_country() is None


def test_relay_countries_parses_list(monkeypatch):
    monkeypatch.setattr(geo_relay, '_run', lambda cmd, timeout=30: _completed(RELAY_LIST))
    assert geo_relay.relay_countries() == {'al', 'jp', 'us'}


def test_youtube_available_countries_parses_watch_page(monkeypatch):
    html = 'x"availableCountries":["JP","KR","TW"],"y'
    monkeypatch.setattr(geo_relay.requests, 'get', lambda *a, **k: SimpleNamespace(text=html))
    assert geo_relay.youtube_available_countries('abc') == ['jp', 'kr', 'tw']

    monkeypatch.setattr(geo_relay.requests, 'get', lambda *a, **k: SimpleNamespace(text='no list here'))
    assert geo_relay.youtube_available_countries('abc') is None


def test_pick_country_prefers_configured_order():
    assert geo_relay.pick_country(['jp', 'kr', 'de'], {'jp', 'de', 'us'}) == 'de'
    assert geo_relay.pick_country(['kr', 'tw'], {'kr', 'tw'}) == 'kr'
    assert geo_relay.pick_country(['kr'], {'us'}) is None


def test_override_switches_and_restore_puts_relay_back(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if cmd[:3] == ['mullvad', 'relay', 'get']:
            return _completed(RELAY_GET)
        if cmd[:3] == ['mullvad', 'relay', 'list']:
            return _completed(RELAY_LIST)
        if cmd[:2] == ['mullvad', 'status']:
            return _completed("Connected\n    Relay: jp-tyo-wg-001\n")
        return _completed()

    monkeypatch.setattr(geo_relay, '_run', fake_run)
    monkeypatch.setattr(geo_relay, '_original_location', None)
    monkeypatch.setattr(geo_relay, '_override_country', None)
    geo_relay._tried_countries.clear()

    assert geo_relay.override_for(['jp', 'kr']) == 'jp'
    assert geo_relay.active()
    assert ['mullvad', 'relay', 'set', 'location', 'jp'] in calls
    assert ['mullvad', 'connect', '--wait'] in calls

    calls.clear()
    geo_relay.restore()
    assert not geo_relay.active()
    assert ['mullvad', 'relay', 'set', 'location', 'us', 'nyc'] in calls


def test_override_fails_when_no_relay_in_allowed_countries(monkeypatch):
    def fake_run(cmd, timeout=30):
        if cmd[:3] == ['mullvad', 'relay', 'get']:
            return _completed(RELAY_GET)
        if cmd[:3] == ['mullvad', 'relay', 'list']:
            return _completed(RELAY_LIST)
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(geo_relay, '_run', fake_run)
    monkeypatch.setattr(geo_relay, '_original_location', None)
    monkeypatch.setattr(geo_relay, '_override_country', None)
    geo_relay._tried_countries.clear()
    assert geo_relay.override_for(['kr', 'tw']) is None
    assert not geo_relay.active()


def test_override_disabled_by_env(monkeypatch):
    monkeypatch.setenv('GEO_RELAY_FALLBACK', 'false')
    assert geo_relay.override_for(['jp']) is None


def test_resolve_youtube_skips_when_allowed_here(monkeypatch):
    monkeypatch.setattr(geo_relay, 'youtube_available_countries', lambda vid: ['us', 'ca'])
    monkeypatch.setattr(geo_relay, 'current_country', lambda: 'us')
    monkeypatch.setattr(geo_relay, 'override_for', lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not switch")))
    assert geo_relay.resolve_youtube("https://youtu.be/dQw4w9WgXcQ") is None


def test_resolve_youtube_overrides_when_blocked_here(monkeypatch):
    monkeypatch.setattr(geo_relay, 'youtube_available_countries', lambda vid: ['jp', 'kr'])
    monkeypatch.setattr(geo_relay, 'current_country', lambda: 'us')
    monkeypatch.setattr(geo_relay, 'override_for', lambda allowed, reason='': 'jp')
    assert geo_relay.resolve_youtube("ERROR: [youtube] dQw4w9WgXcQ: Video unavailable") == 'jp'


def test_override_moves_to_next_country_and_stops_at_limit(monkeypatch):
    switched = []
    monkeypatch.setattr(geo_relay, 'relay_countries', lambda: {'us', 'gb', 'de', 'nl', 'se'})
    monkeypatch.setattr(geo_relay, 'current_location', lambda: 'us nyc')
    monkeypatch.setattr(geo_relay, 'current_country', lambda: switched[-1] if switched else 'us')
    monkeypatch.setattr(geo_relay, 'switch_to', lambda loc: switched.append(loc) or True)
    monkeypatch.setenv('GEO_RELAY_MAX_COUNTRIES', '2')
    monkeypatch.setattr(geo_relay, '_original_location', None)
    monkeypatch.setattr(geo_relay, '_override_country', None)
    geo_relay._tried_countries.clear()

    allowed = ['gb', 'de', 'nl', 'se']
    assert geo_relay.override_for(allowed) == 'gb'
    assert geo_relay.override_for(allowed) == 'de'      # bot-checked in gb → next country
    assert geo_relay.override_for(allowed) is None      # limit reached
    geo_relay.restore()
    assert switched == ['gb', 'de', 'us nyc']
    assert not geo_relay._tried_countries


def test_resolve_youtube_keeps_moving_while_override_active(monkeypatch):
    monkeypatch.setattr(geo_relay, 'youtube_available_countries', lambda vid: ['gb', 'de'])
    monkeypatch.setattr(geo_relay, 'current_country', lambda: 'gb')
    monkeypatch.setattr(geo_relay, 'active', lambda: True)
    monkeypatch.setattr(geo_relay, 'override_for', lambda allowed, reason='': 'de')
    assert geo_relay.resolve_youtube("https://youtu.be/dQw4w9WgXcQ") == 'de'


def test_bot_check_is_retryable_elsewhere():
    assert geo_relay.is_retryable_elsewhere("Sign in to confirm you’re not a bot")
    assert geo_relay.is_retryable_elsewhere("Video unavailable")
    assert not geo_relay.is_retryable_elsewhere("Requested format is not available")


def test_bot_check_without_allow_list_moves_to_another_country(monkeypatch):
    monkeypatch.setattr(geo_relay, 'youtube_available_countries', lambda vid: None)
    monkeypatch.setattr(geo_relay, 'relay_countries', lambda: {'us', 'gb', 'de'})
    monkeypatch.setattr(geo_relay, 'current_country', lambda: 'us')
    monkeypatch.setattr(geo_relay, 'current_location', lambda: 'us nyc')
    monkeypatch.setattr(geo_relay, 'switch_to', lambda loc: True)
    monkeypatch.setattr(geo_relay, '_original_location', None)
    monkeypatch.setattr(geo_relay, '_override_country', None)
    geo_relay._tried_countries.clear()

    assert geo_relay.resolve_youtube(
        'https://youtu.be/dQw4w9WgXcQ',
        error_text="ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you’re not a bot",
    ) == 'gb'


def test_bot_check_moves_even_when_video_is_allowed_here(monkeypatch):
    monkeypatch.setattr(geo_relay, 'youtube_available_countries', lambda vid: ['us', 'gb'])
    monkeypatch.setattr(geo_relay, 'relay_countries', lambda: {'us', 'gb', 'de'})
    monkeypatch.setattr(geo_relay, 'current_country', lambda: 'us')
    monkeypatch.setattr(geo_relay, 'current_location', lambda: 'us nyc')
    monkeypatch.setattr(geo_relay, 'switch_to', lambda loc: True)
    monkeypatch.setattr(geo_relay, '_original_location', None)
    monkeypatch.setattr(geo_relay, '_override_country', None)
    geo_relay._tried_countries.clear()

    assert geo_relay.resolve_youtube('https://youtu.be/dQw4w9WgXcQ',
                                     error_text='Sign in to confirm you’re not a bot') == 'gb'


def test_plain_failure_without_allow_list_does_not_move(monkeypatch):
    monkeypatch.setattr(geo_relay, 'youtube_available_countries', lambda vid: None)
    monkeypatch.setattr(geo_relay, 'current_country', lambda: 'us')
    monkeypatch.setattr(geo_relay, 'override_for',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not switch')))
    assert geo_relay.resolve_youtube('https://youtu.be/dQw4w9WgXcQ',
                                     error_text='HTTP Error 403: Forbidden') is None


def test_video_id_recovered_from_error_when_target_is_a_search(monkeypatch):
    seen = {}
    monkeypatch.setattr(geo_relay, 'youtube_available_countries',
                        lambda vid: seen.setdefault('vid', vid) and None)
    monkeypatch.setattr(geo_relay, 'relay_countries', lambda: {'us', 'gb'})
    monkeypatch.setattr(geo_relay, 'current_country', lambda: 'us')
    monkeypatch.setattr(geo_relay, 'current_location', lambda: 'us')
    monkeypatch.setattr(geo_relay, 'switch_to', lambda loc: True)
    monkeypatch.setattr(geo_relay, '_original_location', None)
    monkeypatch.setattr(geo_relay, '_override_country', None)
    geo_relay._tried_countries.clear()

    geo_relay.resolve_youtube('ytsearch1:some song',
                              error_text='ERROR: [youtube] 4N1iwQxiHrs: Sign in to confirm you’re not a bot')
    assert seen['vid'] == '4N1iwQxiHrs'
