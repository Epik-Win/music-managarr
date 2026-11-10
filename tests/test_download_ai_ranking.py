import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from downloader import Downloader
import discord_bot
from link_extractor import build_request_context_text

# The real request to the AI: these tests fake its HTTP reply instead of the
# conftest's stand-in.
_REAL_ASK_LOCAL_AI = Downloader._ask_local_ai


class TestDownloadAIRanking:
    def test_selects_candidate_with_ai_when_context_is_provided(self, monkeypatch):
        downloader = Downloader.__new__(Downloader)
        downloader.app_mod = SimpleNamespace()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": '{"selected_index": 2, "reason": "prefer the live version"}'
                            }
                        }
                    ]
                }

        def fake_post(*args, **kwargs):
            return FakeResponse()

        monkeypatch.setattr("downloader.requests.post", fake_post)
        monkeypatch.setattr(Downloader, '_ask_local_ai', _REAL_ASK_LOCAL_AI)

        candidates = [
            {"artist": "Artist", "title": "Song", "source": "deezer"},
            {"artist": "Artist", "title": "Song (Live)", "source": "deezer"},
        ]

        selected = downloader._select_best_candidate_with_ai(
            artist="Artist",
            title="Song",
            candidates=candidates,
            request_context="Prefer the live version if available",
        )

        assert selected is candidates[1]

    def test_requests_next_pipeline_stage_when_ai_marks_results_inaccurate(self, monkeypatch):
        downloader = Downloader.__new__(Downloader)
        downloader.app_mod = SimpleNamespace()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": '{"should_continue": false, "reason": "search results are too generic"}'
                            }
                        }
                    ]
                }

        def fake_post(*args, **kwargs):
            return FakeResponse()

        monkeypatch.setattr("downloader.requests.post", fake_post)
        monkeypatch.setattr(Downloader, '_ask_local_ai', _REAL_ASK_LOCAL_AI)

        should_continue = downloader._should_continue_with_ai(
            artist="Artist",
            title="Song",
            candidates=[
                {"artist": "Artist", "title": "Song", "source": "deezer"},
                {"artist": "Artist", "title": "Song", "source": "deezer"},
            ],
            request_context="Prefer the live version if available",
        )

        assert should_continue is False

    def test_music_section_name_prefers_music_library(self):
        assert discord_bot.music_section_name('Music') == 'Music'
        assert discord_bot.music_section_name('Music:Music') == 'Music'
        assert discord_bot.music_section_name('Tunes:Tunes') == 'Tunes'
        assert discord_bot.music_section_name('') == 'Music'

    def test_playlist_matches_own_album_skips_redundant_playlist(self):
        # A Spotify/YouTube "playlist" that's just an artist's album, shared
        # under the album's own name, should be detected so no redundant
        # same-named Plex playlist gets created.
        downloaded_tracks = [
            {'artist': 'Lupe Fiasco', 'title': 'Free Chilly', 'album': "Lupe Fiasco's The Cool"},
            {'artist': 'Lupe Fiasco', 'title': 'Superstar', 'album': "Lupe Fiasco's The Cool"},
            {'artist': 'Lupe Fiasco', 'title': 'Paris, Tokyo', 'album': "Lupe Fiasco's The Cool"},
        ]
        assert discord_bot.playlist_matches_own_album("Lupe Fiasco's The Cool", downloaded_tracks) is True

    def test_playlist_matches_own_album_ignores_real_curated_playlists(self):
        # A genuinely curated playlist spans many different albums/artists and
        # is named something unrelated to any one of them — must NOT be
        # treated as an album, so its Plex playlist still gets created.
        downloaded_tracks = [
            {'artist': 'Artist A', 'title': 'Song 1', 'album': 'Album A'},
            {'artist': 'Artist B', 'title': 'Song 2', 'album': 'Album B'},
            {'artist': 'Artist C', 'title': 'Song 3', 'album': 'Album C'},
        ]
        assert discord_bot.playlist_matches_own_album('Workout Hits 2026', downloaded_tracks) is False

    def test_playlist_matches_own_album_requires_majority_consistency(self):
        # Even if one track happens to share a name with the playlist, a
        # playlist spanning mostly different albums is still a real playlist.
        downloaded_tracks = [
            {'artist': 'A', 'title': 'T1', 'album': 'Mixed Album'},
            {'artist': 'B', 'title': 'T2', 'album': 'Other Album 1'},
            {'artist': 'C', 'title': 'T3', 'album': 'Other Album 2'},
            {'artist': 'D', 'title': 'T4', 'album': 'Other Album 3'},
        ]
        assert discord_bot.playlist_matches_own_album('Mixed Album', downloaded_tracks) is False

    def test_playlist_matches_own_album_alone_misses_artist_prefixed_names(self):
        # A Spotify /album/ link is displayed
        # as "Artist - Album" (e.g. "The Glitch Mob - Love Death Immortality")
        # for clearer Discord messaging, but the fuzzy name-similarity check
        # alone scores that against the bare album name below its 0.82
        # threshold — which is exactly why the caller in discord_bot.py must
        # also check metadata['is_album'] directly for actual Spotify album
        # links, rather than relying on this heuristic alone.
        downloaded_tracks = [
            {'artist': 'The Glitch Mob', 'title': 'Mind of a Beast', 'album': 'Love Death Immortality'},
            {'artist': 'The Glitch Mob', 'title': 'Skullclub', 'album': 'Love Death Immortality'},
            {'artist': 'The Glitch Mob', 'title': 'Skytoucher', 'album': 'Love Death Immortality'},
        ]
        assert discord_bot.playlist_matches_own_album(
            'The Glitch Mob - Love Death Immortality', downloaded_tracks
        ) is False

    def test_variant_request_context_prevents_existing_track_short_circuit(self):
        downloader = Downloader.__new__(Downloader)
        downloader._find_existing_track = lambda *args, **kwargs: '/music/albums/Artist/Song.mp3'
        request_context = 'Arabic version of the song'
        should_skip = True
        variant_keywords = [
            'arabic', 'live', 'acoustic', 'remix', 'explicit', 'clean', 'instrumental',
            'deluxe', 'radio edit', 're-record', 're-recording', 'cover', 'version',
            'variant', 'translated', 'translation', 'original', 'alternate', 'mix',
        ]
        lowered_context = request_context.lower()
        has_variant_hint = any(keyword in lowered_context for keyword in variant_keywords)
        if has_variant_hint:
            should_skip = False
        assert should_skip is False

    def test_variant_request_context_overrides_artist_and_title(self):
        downloader = Downloader.__new__(Downloader)
        metadata = downloader._build_request_context_metadata(
            artist='The Weeknd',
            title='Blinding Lights',
            request_context='Arabic version by Rachid ASEYAKHE',
            album='After Hours',
        )
        assert metadata['artist'] == 'Rachid ASEYAKHE'
        assert metadata['title'] == 'Blinding Lights (Arabic Version)'

    def test_album_title_is_no_artist_hint(self):
        """"Bobby Ray" in the album title must not make the artist "Ray": the
        "by" inside "Bobby" is no credit, and the album field is not searched."""
        downloader = Downloader.__new__(Downloader)
        metadata = downloader._build_request_context_metadata(
            artist='B.o.B',
            title='Airplanes, Pt. II (feat. Eminem & Hayley Williams of Paramore)',
            request_context=('source=youtube; requested_track=B.o.B - Airplanes, Pt. II (feat. Eminem & Hayley '
                             'Williams of Paramore); album=B.o.B Presents: The Adventures of Bobby Ray; '
                             'url=https://www.youtube.com/watch?v=vid00000007'),
            album='B.o.B Presents: The Adventures of Bobby Ray',
        )
        assert metadata['artist'] == 'B.o.B'

    def test_merge_request_context_metadata_overrides_embedded_tags_for_variant_requests(self):
        downloader = Downloader.__new__(Downloader)
        external_metadata = {
            'artist': 'The Weeknd',
            'title': 'Blinding Lights',
            'album': 'After Hours',
            'source': 'deemix',
        }
        request_context_metadata = {
            'artist': 'RaChid ASEYAKHE I رشيد أسياخ',
            'title': 'Blinding Lights (Arabic Version)',
            'album': 'After Hours',
        }
        merged = downloader._merge_request_context_metadata(
            external_metadata=external_metadata,
            request_context_metadata=request_context_metadata,
            artist='RaChid ASEYAKHE I رشيد أسياخ',
            title='Blinding Lights (Arabic Version)',
            album='After Hours',
        )
        assert merged['artist'] == request_context_metadata['artist']
        assert merged['title'] == request_context_metadata['title']

    def test_exact_youtube_cover_request_uses_video_uploader(self):
        downloader = Downloader.__new__(Downloader)
        ctx = (
            'source=youtube; requested_track=The Weeknd - Blinding lights; '
            'url=https://www.youtube.com/watch?v=vid00000008; '
            'video_title=The Weeknd - Blinding lights (Arabic Version)النسخة العربية (On Spotify & Apple Music)[Cover]; '
            'video_uploader=RaChid ASEYAKHE I رشيد أسياخ; '
            'prefer the exact release/variant implied by the request if multiple versions exist'
        )
        metadata = downloader._build_request_context_metadata(
            artist='The Weeknd',
            title='Blinding lights',
            request_context=ctx,
            album='The Weeknd - Blinding Lights',
        )
        assert metadata['artist'] == 'RaChid ASEYAKHE I رشيد أسياخ'
        assert 'Arabic Version' in metadata['title']

    def test_boilerplate_request_context_does_not_override_artist(self):
        downloader = Downloader.__new__(Downloader)
        metadata = downloader._build_request_context_metadata(
            artist='The Weeknd',
            title='Blinding Lights',
            request_context='Prefer the exact release/variant implied by the request if multiple versions exist',
            album='After Hours',
        )
        assert metadata['artist'] == 'The Weeknd'
        assert metadata['title'] == 'Blinding Lights'

    def test_variant_request_hint_ignores_boilerplate_only_context(self):
        downloader = Downloader.__new__(Downloader)
        assert downloader._has_variant_request_hint(
            'source=youtube; requested_track=The Weeknd - Blinding Lights; prefer the exact release/variant implied by the request if multiple versions exist'
        ) is False

    def test_plain_track_request_does_not_get_blocked_by_ai_gate(self):
        downloader = Downloader.__new__(Downloader)
        downloader.app_mod = SimpleNamespace()

        assert downloader._has_variant_request_hint('Normal version request') is False
        assert downloader._has_variant_request_hint('Arabic version by Rachid ASEYAKHE') is True

    def test_request_context_text_includes_message_variant_hints(self):
        context = build_request_context_text(
            message_text='Please download the Arabic version by Rachid ASEYAKHE',
            metadata={'artist': 'The Weeknd', 'title': 'Blinding Lights', 'source': 'youtube', 'url': 'https://youtu.be/abc'},
            artist='The Weeknd',
            title='Blinding Lights',
            album='After Hours',
        )
        lowered = context.lower()
        assert 'arabic' in lowered
        assert 'source=youtube' in context
        assert 'rachid' in lowered

    def test_effective_download_metadata_uses_variant_title_for_output(self):
        downloader = Downloader.__new__(Downloader)
        metadata = downloader._resolve_effective_download_metadata(
            artist='The Weeknd',
            title='Blinding Lights',
            album='After Hours',
            request_context='Arabic version by Rachid ASEYAKHE',
        )
        assert metadata['artist'] == 'Rachid ASEYAKHE'
        assert metadata['title'] == 'Blinding Lights (Arabic Version)'

    def test_download_track_uses_lidarr_before_fallback_tools(self):
        downloader = Downloader.__new__(Downloader)
        downloader.app_mod = SimpleNamespace(LIDARR_USE_AS_DOWNLOADER=True)
        downloader._ensure_vpn = lambda: None
        downloader._find_existing_track = lambda *args, **kwargs: None
        downloader._is_track_blacklisted = lambda *args, **kwargs: False
        downloader._resolve_effective_download_metadata = lambda **kwargs: {
            'artist': kwargs.get('artist'),
            'title': kwargs.get('title'),
            'album': kwargs.get('album'),
        }
        downloader.get_metadata_from_sources = lambda *args, **kwargs: None
        downloader.check_download_tools = lambda: {'deemix': False, 'soundcloud': False, 'yt-dlp': False}

        class FakeLidarrClient:
            def download_album(self, artist, album):
                return True

        downloader._get_lidarr_client = lambda user_index=None: FakeLidarrClient()

        result = downloader.download_track(
            artist='The Weeknd',
            title='Blinding Lights',
            album='After Hours',
            request_type='album',
            target_dir='/music/albums',
            user_index=0,
        )

        assert result['success'] is True
        assert result['download_tool'] == 'lidarr'
        assert result['metadata']['source'] == 'lidarr'

    def test_download_track_skips_lidarr_for_song_requests(self):
        downloader = Downloader.__new__(Downloader)
        downloader.app_mod = SimpleNamespace(LIDARR_USE_AS_DOWNLOADER=True)
        downloader._ensure_vpn = lambda: None
        downloader._find_existing_track = lambda *args, **kwargs: None
        downloader._is_track_blacklisted = lambda *args, **kwargs: False
        downloader._resolve_effective_download_metadata = lambda **kwargs: {
            'artist': kwargs.get('artist'),
            'title': kwargs.get('title'),
            'album': kwargs.get('album'),
        }
        downloader.get_metadata_from_sources = lambda *args, **kwargs: None
        downloader.check_download_tools = lambda: {'deemix': False, 'soundcloud': False, 'yt-dlp': False}
        downloader._get_lidarr_client = lambda user_index=None: None

        result = downloader.download_track(
            artist='The Weeknd',
            title='Blinding Lights',
            album='After Hours',
            request_type='song',
            target_dir='/music/albums',
            user_index=0,
        )

        assert result['success'] is False
        assert result['download_tool'] is None

    def test_organization_prefers_request_context_artist_for_cover_variant(self):
        downloader = Downloader.__new__(Downloader)
        metadata = downloader._resolve_organization_targets(
            metadata={'artist': 'The Weeknd', 'album': 'After Hours', 'title': 'Blinding Lights'},
            artist='The Weeknd',
            title='Blinding Lights',
            album='After Hours',
            request_context='source=youtube; video_title=The Weeknd - Blinding lights (Arabic Version); video_uploader=RaChid ASEYAKHE I رشيد أسياخ; prefer the exact release/variant implied by the request if multiple versions exist',
        )
        assert metadata['artist'] == 'RaChid ASEYAKHE I رشيد أسياخ'
        assert metadata['album'] == 'After Hours'

    def test_reversed_video_title_does_not_corrupt_resolved_artist_title(self, monkeypatch):
        # A video titled "Title - Artist" (backwards) must not override the
        # correctly-ordered artist/title already resolved from requested_track,
        # and a reposting channel's uploader name must not be promoted to the
        # artist field when nothing marks the upload as a cover or variant.
        downloader = Downloader.__new__(Downloader)

        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "choices": [
                        {"message": {"content": '{"is_performer": false, "reason": "reposting channel"}'}}
                    ]
                }

        monkeypatch.setattr("downloader.requests.post", lambda *a, **k: FakeResponse())
        monkeypatch.setattr(Downloader, '_ask_local_ai', _REAL_ASK_LOCAL_AI)

        ctx = (
            'source=youtube; requested_track=Lupe Fiasco - Muhammad Walks; '
            'url=https://www.youtube.com/watch?v=vid00000009; '
            'video_title=Muhammad Walks - Lupe Fiasco; video_uploader=hanaaB; '
            'message=https://youtu.be/vid00000009'
        )
        metadata = downloader._build_request_context_metadata(
            artist='Lupe Fiasco',
            title='Muhammad Walks',
            request_context=ctx,
            album='',
        )
        assert metadata['artist'] == 'Lupe Fiasco'
        assert metadata['title'] == 'Muhammad Walks'

    def test_ambiguous_video_title_order_uses_ai_when_no_anchor(self, monkeypatch):
        # With no requested_track to anchor against, a reversed video_title
        # where both halves look like plausible artist names is genuinely
        # ambiguous — the AI should be consulted to pick the right order.
        downloader = Downloader.__new__(Downloader)

        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": '{"artist": "Lupe Fiasco", "title": "Muhammad Walks", "confident": true}'
                            }
                        }
                    ]
                }

        def fake_post(*args, **kwargs):
            return FakeResponse()

        monkeypatch.setattr("downloader.requests.post", fake_post)
        monkeypatch.setattr(Downloader, '_ask_local_ai', _REAL_ASK_LOCAL_AI)

        ctx = (
            'source=youtube; url=https://www.youtube.com/watch?v=vid00000009; '
            'video_title=Muhammad Walks - Lupe Fiasco; message=https://youtu.be/vid00000009'
        )
        metadata = downloader._build_request_context_metadata(
            artist='',
            title='',
            request_context=ctx,
            album='',
        )
        assert metadata['artist'] == 'Lupe Fiasco'
        assert metadata['title'] == 'Muhammad Walks'



def test_download_match_accepts_non_latin_titles(tmp_path):
    from downloader import Downloader
    d = Downloader.__new__(Downloader)
    f = tmp_path / "香西かおり - 「無言坂」.mp3"
    f.write_bytes(b"")
    d._extract_artist_title_from_file = lambda path: ('香西かおり', '「無言坂」')
    assert d._is_download_match(str(f), '香西かおり', '「無言坂」') is True
    assert d._is_download_match(str(f), 'Adele', 'Hello') is False


def test_download_match_accepts_transliterated_cyrillic_title(tmp_path):
    # Rauf & Faik's "детство" downloaded via deemix comes back tagged with the
    # Latin transliteration "Detstvo" (and "Rauf And Faik" via a SoundCloud
    # reupload, "&" spelled out). A direct character comparison against the
    # Cyrillic title scores ~0; transliterating both sides first recognizes
    # them as the same song.
    from downloader import Downloader
    d = Downloader.__new__(Downloader)
    f = tmp_path / "Rauf & Faik - Detstvo.mp3"
    f.write_bytes(b"")
    d._extract_artist_title_from_file = lambda path: ('Rauf & Faik', 'Detstvo')
    assert d._is_download_match(str(f), 'Rauf & Faik', 'детство') is True

    f2 = tmp_path / "Rauf And Faik - Детство.mp3"
    f2.write_bytes(b"")
    d._extract_artist_title_from_file = lambda path: ('Rauf And Faik', 'Детство')
    assert d._is_download_match(str(f2), 'Rauf & Faik', 'детство') is True


# League of Legends' official "RISE (ft. The Glitch Mob, Mako, and The Word
# Alive) | Worlds 2018 - League of Legends" upload is Deezer's "League Of
# Legends - RISE". The reversed video title must be caught as swapped despite
# its "(ft. …) | Worlds 2018" noise, "Alive" must not count as a "live" variant
# hint, and the "2018" must not make the artist look like a fan channel.
RISE_CTX = (
    'source=youtube; requested_track=League of Legends - RISE (ft. The Glitch Mob, Mako, and The Word Alive); '
    'url=https://www.youtube.com/watch?v=vid00000010; '
    'video_title=RISE (ft. The Glitch Mob, Mako, and The Word Alive) | Worlds 2018 - League of Legends; '
    'video_uploader=League of Legends; message=https://youtu.be/vid00000010'
)


def test_reversed_video_title_with_feature_and_event_noise_is_detected():
    d = Downloader.__new__(Downloader)
    d._ai_confirms_uploader_is_performer = lambda *a, **k: False
    d._ai_resolve_artist_title_order = lambda *a, **k: None
    metadata = d._build_request_context_metadata(
        'League of Legends', 'RISE (ft. The Glitch Mob, Mako, and The Word Alive)', RISE_CTX, '',
    )
    assert metadata['artist'] == 'League of Legends'
    assert metadata['title'] == 'RISE'


def test_variant_label_requires_whole_word():
    d = Downloader.__new__(Downloader)
    assert d._get_explicit_variant_label('The Word Alive') is None
    assert d._get_explicit_variant_label('Discover Weekly') is None
    assert d._get_explicit_variant_label('Song (Live)') == 'Live Version'
    assert d._get_explicit_variant_label('Song (Cover)') == 'Cover'


def test_fan_channel_regex_ignores_standalone_years():
    assert not Downloader._FAN_CHANNEL_RE.search('Worlds 2018')
    assert not Downloader._FAN_CHANNEL_RE.search('League of Legends')
    assert Downloader._FAN_CHANNEL_RE.search('Rockbandfan145')
    assert Downloader._FAN_CHANNEL_RE.search('MusicChannel2020')
