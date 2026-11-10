"""Shared test setup.

Modules such as downloader_manager resolve DATA_DIR at import time, so the
environment is isolated here — before any test module is imported — rather
than in a fixture. Without this a stray arl_stale.flag on the developer's
machine would make every download test block waiting for an ARL refresh.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

_TEST_DATA_DIR = tempfile.mkdtemp(prefix='musicai-tests-')
os.environ['DATA_DIR'] = _TEST_DATA_DIR
os.environ.setdefault('MUSIC_BASE_DIR', os.path.join(_TEST_DATA_DIR, 'music'))
os.environ.setdefault('WIZARR_MAPPING_FILE', os.path.join(_TEST_DATA_DIR, 'plex_invite_targets.json'))
os.environ.pop('DISCORD_TO_PLEX_MAPPING_ADMIN', None)


@pytest.fixture(autouse=True)
def no_local_ai(monkeypatch):
    """The downloader deliberately stalls until the local AI is reachable;
    tests must never depend on that service existing. No test reaches a real
    AI: without a stand-in of its own, a test gets no answer (None)."""
    import downloader
    monkeypatch.setattr(downloader.Downloader, '_wait_for_ai_service', lambda self, api_base, api_key='': None)
    monkeypatch.setattr(downloader.Downloader, '_ask_local_ai', lambda self, system, prompt: None)


@pytest.fixture(autouse=True)
def no_artwork_lookups(monkeypatch):
    """Fixes look for missing artwork online; tests must not reach
    Deezer/iTunes. Tests that exercise the lookup patch these themselves."""
    import artwork
    monkeypatch.setattr(artwork, '_deezer', lambda *a: None)
    monkeypatch.setattr(artwork, '_itunes', lambda *a: None)
    if not hasattr(artwork, '_real_deezer_album'):
        artwork._real_deezer_album = artwork._deezer_album
    monkeypatch.setattr(artwork, '_deezer_album', lambda *a: None)
    monkeypatch.setattr(artwork, '_spotify', lambda *a, **k: None)
    monkeypatch.setattr(artwork, '_ytmusic_song', lambda *a, **k: None)
    if not hasattr(artwork, '_real_youtube_thumbnail'):
        artwork._real_youtube_thumbnail = artwork._youtube_thumbnail
    monkeypatch.setattr(artwork, '_youtube_thumbnail', lambda *a, **k: None)
    monkeypatch.setattr(artwork, '_soundcloud', lambda *a, **k: None)
    monkeypatch.setattr(artwork, '_download', lambda url: None)


@pytest.fixture(autouse=True)
def no_artist_lookups(monkeypatch):
    """The swap check asks Lidarr/Deezer whether a name is a real artist;
    offline in tests (nothing is known) unless a test says otherwise."""
    import downloader
    monkeypatch.setattr(downloader.Downloader, '_is_known_artist', lambda self, name: False)


@pytest.fixture(autouse=True)
def no_known_artist_lookups(monkeypatch):
    """main_artist asks Lidarr/Deezer whether a credit is one act's name;
    offline, only the names a test lists are known."""
    import metadata_repair
    monkeypatch.setattr(metadata_repair, 'is_established_artist', lambda name: metadata_repair.is_known_artist(name))
    monkeypatch.setattr(metadata_repair, 'is_known_artist',
                        lambda name: (name or '').strip().casefold() in {'earth, wind & fire', 'simon & garfunkel'})


@pytest.fixture(autouse=True)
def no_deezer_album_lookups(monkeypatch):
    """Album choice asks Deezer which albums list a song; offline in tests."""
    import metadata_repair
    monkeypatch.setattr(metadata_repair, 'deezer_albums_for_track', lambda artist, title, duration=None: [])


@pytest.fixture(autouse=True)
def no_album_match_lookups(monkeypatch):
    """album_match never reaches Deezer from a test; tests that need its
    answers give their own."""
    import album_match

    def offline(path, **params):
        raise ConnectionError("no Deezer in tests")
    monkeypatch.setattr(album_match, '_get', offline)


@pytest.fixture(autouse=True)
def no_deezer_name_lookups(monkeypatch):
    """The name lookup searches Deezer; offline in tests unless a test sets it."""
    import metadata_repair
    if not hasattr(metadata_repair, '_real_deezer_lookup_by_name'):
        metadata_repair._real_deezer_lookup_by_name = metadata_repair.deezer_lookup_by_name
    monkeypatch.setattr(metadata_repair, 'deezer_lookup_by_name', lambda meta, path, extra=(): None)


@pytest.fixture(autouse=True)
def no_isrc_lookups(monkeypatch):
    """ISRC lookups go to Deezer; offline in tests unless a test sets them."""
    import metadata_repair
    monkeypatch.setattr(metadata_repair, 'deezer_track_by_isrc', lambda isrc, duration=None: None)


@pytest.fixture(autouse=True)
def no_reference_lookups(monkeypatch):
    """The true-audio check and the flagged-song verification look songs up
    on Deezer, Spotify and YouTube; offline in tests unless a test sets them."""
    import artwork
    import metadata_repair
    if not hasattr(metadata_repair, '_real_reference_version'):
        metadata_repair._real_reference_version = metadata_repair.reference_version
    monkeypatch.setattr(metadata_repair, 'reference_version', lambda artist, title: None)
    if not hasattr(metadata_repair, '_real_release_listing'):
        metadata_repair._real_release_listing = metadata_repair.release_listing
    monkeypatch.setattr(metadata_repair, 'release_listing', lambda artist, title, duration, tolerance=3: None)
    monkeypatch.setattr(metadata_repair, 'listed_length', lambda artist, title: None)
    monkeypatch.setattr(metadata_repair, 'isrc_length', lambda isrc: None)
    monkeypatch.setattr(metadata_repair, 'deezer_release', lambda answer, duration=None, tolerance=None: None)
    if not hasattr(metadata_repair, '_real_current_artist_name'):
        metadata_repair._real_current_artist_name = metadata_repair.current_artist_name
    monkeypatch.setattr(metadata_repair, 'current_artist_name', lambda name: None)
    monkeypatch.setattr(artwork, 'own_upload_matches', lambda artist, title, duration, tolerance=3: False)
    monkeypatch.setattr(artwork, 'verify_cover', lambda path, meta, dry_run=False: 'not found')
    if not hasattr(artwork, '_real_release_cover_url'):
        artwork._real_release_cover_url = artwork.release_cover_url
    monkeypatch.setattr(artwork, 'release_cover_url', lambda path, meta: None)
    monkeypatch.setattr(artwork, 'youtube_source', lambda title, duration, tolerance=2: None)
    import lyrics_id
    if not hasattr(lyrics_id, '_real_identify'):
        lyrics_id._real_identify = lyrics_id.identify
    monkeypatch.setattr(lyrics_id, 'identify', lambda path, transcript=None: None)
    import fix_metadata
    if not hasattr(fix_metadata, '_real_existing_album'):
        fix_metadata._real_existing_album = fix_metadata._existing_album
    monkeypatch.setattr(fix_metadata, '_existing_album', lambda file_path, meta: None)
    monkeypatch.setattr(fix_metadata, '_lidarr_catalog', lambda: None)
