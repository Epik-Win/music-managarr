from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import app
import discord_bot
import entrypoint
import fix_metadata_daemon


def test_should_run_fix_metadata_workflow_when_interval_elapsed(tmp_path, monkeypatch):
    # Fix Metadata's due-check lives in fix_metadata_daemon.py, a standalone
    # process started by entrypoint.py that covers every user on its own daily
    # schedule, independent of SCAN_INTERVAL.
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    now = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    last_run = now - timedelta(hours=25)
    fix_metadata_daemon.write_last_run('someuser', last_run)

    assert fix_metadata_daemon.is_due('someuser', now, interval=86400)


def test_should_not_run_fix_metadata_workflow_before_interval(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    now = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    last_run = now - timedelta(hours=23)
    fix_metadata_daemon.write_last_run('someuser', last_run)

    assert not fix_metadata_daemon.is_due('someuser', now, interval=86400)


def test_parse_admin_override_forms():
    import users
    assert users.parse_admin_override('alice_d:alice') == ('alice_d', 'alice')
    assert users.parse_admin_override('{"alice_d": "alice"}') == ('alice_d', 'alice')
    assert users.parse_admin_override('alice') == ('alice', 'alice')
    assert users.parse_admin_override('') == (None, None)


def test_lastfm_web_similar_tracks_strips_music_prefix_from_artist(monkeypatch):
    # Last.FM hrefs look like "/music/Artist+Name/_/Track+Title"; the artist
    # must come out without the "music/" path segment attached.
    analyzer = app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)

    class FakeResponse:
        status_code = 200
        text = (
            '<a href="/music/Trevor+Rabin/_/Theme+From+Armageddon">link</a>'
            '<a href="/music/The+Chamber+Orchestra+of+London/_/Violet">link</a>'
        )

    monkeypatch.setattr(app.requests, 'get', lambda *a, **k: FakeResponse())

    results = analyzer._get_similar_tracks_from_lastfm_web('Some Artist', 'Some Title', limit=10)

    assert len(results) == 2
    artists = {r['artist'] for r in results}
    assert artists == {'Trevor Rabin', 'The Chamber Orchestra of London'}
    assert not any(a.startswith('music/') for a in artists)
