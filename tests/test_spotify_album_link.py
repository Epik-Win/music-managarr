import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import link_extractor as le
import spotify_web


class TestSpotifyAlbumLink:
    """A Spotify album link (https://open.spotify.com/album/...) is
    supported like /track/ and /playlist/ links, through spotify_scraper's
    album lookup.
    """

    def test_album_url_is_recognized_and_routed(self, monkeypatch):
        captured = {}

        async def fake_get_album_async(url):
            captured['url'] = url
            return 'Love Death Immortality', 'The Glitch Mob', [
                {'artist': 'The Glitch Mob', 'title': 'Mind of a Beast', 'album': 'Love Death Immortality'},
                {'artist': 'The Glitch Mob', 'title': 'Skullclub', 'album': 'Love Death Immortality'},
            ]

        def fake_to_thread(func, *args, **kwargs):
            assert func is spotify_web.get_album
            return fake_get_album_async(*args, **kwargs)

        monkeypatch.setattr(asyncio, 'to_thread', fake_to_thread)

        url = 'https://open.spotify.com/album/1g03ut9FlCdvFbOcSvUxjs?si=abc123'
        result = asyncio.run(le.extract_link_metadata(url))

        assert result is not None
        assert result['type'] == 'playlist'
        assert result['name'] == 'The Glitch Mob - Love Death Immortality'
        assert result['source'] == 'spotify'
        assert len(result['tracks']) == 2
        assert result['tracks'][0]['artist'] == 'The Glitch Mob'
        assert captured['url'] == url
        # A direct /album/ URL is a real album by definition — downstream
        # (discord_bot.py) trusts this flag outright to skip creating a
        # redundant same-named Plex playlist, rather than relying solely on
        # playlist_matches_own_album()'s fuzzy name match (which the "Artist -
        # Album" display name below doesn't pass on its own).
        assert result['is_album'] is True
        assert result['album_name'] == 'Love Death Immortality'

    def test_album_url_with_no_tracks_returns_none(self, monkeypatch):
        async def fake_get_album_async(url):
            return '', '', []

        def fake_to_thread(func, *args, **kwargs):
            return fake_get_album_async(*args, **kwargs)

        monkeypatch.setattr(asyncio, 'to_thread', fake_to_thread)

        result = asyncio.run(
            le.extract_spotify_metadata_web('https://open.spotify.com/album/doesnotexist')
        )
        assert result is None
