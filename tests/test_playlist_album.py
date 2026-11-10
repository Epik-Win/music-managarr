"""A playlist that is an album is downloaded as that album.

A YouTube playlist "Final Fantasy IV: Piano Collection": fourteen videos by a
fan's channel, each named only by its track. Each song is filed under the
album's real artist and album, not the channel and a guess from its title; the
linked videos come before a SoundCloud search (where other people's uploads
share the titles "Prologue" and "Theme of Love Orchestrated"); and
"Epilogue" is no copy of "Prologue".
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import album_match
import discord_bot
from downloader import Downloader

PLAYLIST = "Final Fantasy IV: Piano Collection"
VIDEOS = [("vid00000004", "The Prelude", 218), ("flSuItHr0Hg", "Theme of Love", 232), ("c2sQKw9sFIM", "Prologue", 190),
          ("wDcDDHVEtW0", "Welcome to Our Town!", 183), ("hviuwXkDkbY", "Main Theme", 204),
          ("Crc6OGPE89M", "Chocobo Chocobo", 168), ("htX23lpJ2qY", "Into the Darkness", 295), ("QUNn_zZNXnw", "Rydia", 204),
          ("cpTianvRMdw", "Melody of Lute", 218), ("ChD7WfWlOAI", "Golbeza, Clad in the Dark", 5),
          ("mtjkgcSy_50", "Trojan Beauty", 204), ("AJhLCABt6H0", "The Battle", 407), ("vid00000005", "Epilogue", 434),
          ("KEhWUaLuwrY", "Theme of Love Orchestrated", 246)]
ALBUM = [("The Prelude", 217), ("Theme of Love", 238), ("Prologue", 188), ("Welcome to Our Town!", 187),
         ("Main Theme of Final Fantasy IV", 203), ("Chocobo-chocobo", 172), ("Into the Darkness", 297), ("Rydia", 201),
         ("Melody of Lute", 219), ("Golbeza Clad in the Dark", 216), ("Troian Beauty", 205), ("The Battle", 409),
         ("Epilogue", 440), ("Theme of Love (ensemble)", 246)]
FAN_ALBUM = [("The Prelude", 230), ("Theme of Love", 250), ("Prologue", 200), ("Rydia", 210), ("Epilogue", 300)]


def _songs():
    return [{"artist": "PianoFanChannel", "title": t, "album": "", "duration": d,
             "url": f"https://www.youtube.com/watch?v={v}"} for v, t, d in VIDEOS]


def _deezer(path, **params):
    """Deezer's answers, cut down."""
    albums = {501706651: ("Piano Collections FINAL FANTASY IV", "Nobuo Uematsu", ALBUM, 2503849991),
              7001: ("Final Fantasy IV Piano Collections Special Edition", "fan_upload", FAN_ALBUM, 9100),
              501216431: ("Piano Collections FINAL FANTASY IX", "Nobuo Uematsu", [("Eternal Harvest", 185)], 9200)}
    if path == "search/album":
        return {"data": [{"id": i, "title": a[0], "artist": {"name": a[1]}} for i, a in albums.items()]
                if "fantasy" in params["q"].lower() else []}
    title, artist, tracks, first = albums[int(path.split("/")[1])]
    return {"id": int(path.split("/")[1]), "title": title, "artist": {"name": artist}, "nb_tracks": len(tracks),
            "tracks": {"data": [{"id": first + 10 * n, "title": f"Piano: {t}", "duration": d}
                                for n, (t, d) in enumerate(tracks)]}}


@pytest.fixture
def deezer(monkeypatch):
    monkeypatch.setattr(album_match, "_get", _deezer)


class TestFindingTheAlbum:
    def test_the_playlist_is_the_released_album(self, deezer):
        found = album_match.find_album(PLAYLIST, _songs())
        assert (found["artist"], found["album"], found["id"]) == ("Nobuo Uematsu", "Piano Collections FINAL FANTASY IV",
                                                                  501706651)
        assert found["coverage"] == 1.0
        # Each video is its own track, in order, whatever the titles' spelling.
        assert [found["tracks"][i]["id"] for i in range(14)] == [2503849991 + 10 * n for n in range(14)]
        assert found["tracks"][10]["title"] == "Piano: Troian Beauty"            # "Trojan Beauty"
        assert found["tracks"][13]["title"] == "Piano: Theme of Love (ensemble)"  # same length, 4:06
        assert found["tracks"][9]["title"] == "Piano: Golbeza Clad in the Dark"   # a 5-second video: a clip

    def test_of_two_albums_holding_the_songs_the_one_named_like_the_playlist(self, monkeypatch):
        # Deezer lists a fan's "Special Edition" with the same fourteen titles first.
        def deezer(path, **params):
            if path == "search/album":
                return {"data": [{"id": 7001, "title": "Final Fantasy IV Piano Collections Special Edition"},
                                 {"id": 501706651, "title": "Piano Collections FINAL FANTASY IV"}]}
            fan = path == "album/7001"
            return {"id": int(path.split("/")[1]), "nb_tracks": 14,
                    "title": "Final Fantasy IV Piano Collections Special Edition" if fan else "Piano Collections FINAL FANTASY IV",
                    "artist": {"name": "fan_upload" if fan else "Nobuo Uematsu"},
                    "tracks": {"data": [{"id": n, "title": t, "duration": d} for n, (t, d) in enumerate(ALBUM)]}}
        monkeypatch.setattr(album_match, "_get", deezer)
        assert album_match.find_album(PLAYLIST, _songs())["artist"] == "Nobuo Uematsu"

    def test_names_in_any_order(self):
        assert album_match.name_alike(PLAYLIST, "Piano Collections FINAL FANTASY IV") == 1.0
        assert album_match.name_alike(PLAYLIST, "Piano Collections FINAL FANTASY IX") < 1.0

    def test_a_playlist_that_is_no_album(self, deezer):
        assert album_match.find_album("My Mix", _songs()) is None
        mixed = _songs()[:4] + [{"title": f"Other song {n}", "duration": 200} for n in range(6)]
        assert album_match.find_album(PLAYLIST, mixed) is None            # 4 of 10 on the album

    def test_a_same_named_song_of_another_length_is_another_recording(self):
        tracks = [{"id": 1, "title": "Theme of Love", "duration": 238}]
        assert album_match.match_tracks([{"title": "Theme of Love", "duration": 600}], tracks) == {}
        assert album_match.match_tracks([{"title": "Theme of Love", "duration": 0}], tracks)[0]["id"] == 1

    def test_deezer_failing_is_no_album(self, monkeypatch):
        def down(path, **params):
            raise ConnectionError("down")
        monkeypatch.setattr(album_match, "_get", down)
        assert album_match.find_album(PLAYLIST, _songs()) is None


class TestDownloadingIt:
    CONTEXT = "source=youtube_playlist; album=Piano Collections FINAL FANTASY IV; " \
              "url=https://www.youtube.com/watch?v=vid00000004; deezer_track=2503849991"

    def test_the_albums_track_is_downloaded_without_a_search(self, tmp_path):
        d = Downloader.__new__(Downloader)
        path = tmp_path / "Piano The Prelude.mp3"
        asked = []

        def fetch(track_id, target):
            asked.append(track_id)
            path.write_bytes(b"")
            return str(path)

        d.download_deezer_track = fetch
        d._download_with_deemix_timeout = lambda *a, **k: pytest.fail("no search for a known track")
        got = d._try_tool_download("deemix", "Nobuo Uematsu", "Piano: The Prelude",
                                   "https://www.youtube.com/watch?v=vid00000004", str(tmp_path), self.CONTEXT, None)
        assert asked == ["2503849991"] and got == str(path)

    def test_after_deezer_comes_the_linked_video_then_soundcloud(self):
        d = Downloader.__new__(Downloader)
        tools = d._link_first(["deemix", "soundcloud", "yt-dlp"], "https://www.youtube.com/watch?v=vid00000004",
                              self.CONTEXT)
        assert tools == ["deemix", "yt-dlp", "soundcloud"]

    def test_epilogue_is_not_prologue(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALBUM_DIR", raising=False)
        d = Downloader.__new__(Downloader)
        d.app_mod = None
        (tmp_path / "Prologue.mp3").write_bytes(b"")
        d._extract_artist_title_from_file = lambda path: ("Nobuo Uematsu", "Prologue")
        assert d._find_existing_track("Nobuo Uematsu", "Epilogue", str(tmp_path)) is None
        assert d._find_existing_track("Nobuo Uematsu", "Prologue", str(tmp_path)) == str(tmp_path / "Prologue.mp3")

    def test_a_releases_version_in_brackets_is_another_recording(self, tmp_path, monkeypatch):
        # The 14th track, "Piano: Theme of Love (ensemble)", is not a copy of
        # "Piano: Theme of Love", the 2nd.
        monkeypatch.delenv("ALBUM_DIR", raising=False)
        d = Downloader.__new__(Downloader)
        d.app_mod = None
        (tmp_path / "Theme of Love.mp3").write_bytes(b"")
        d._extract_artist_title_from_file = lambda path: ("Nobuo Uematsu", "Piano: Theme of Love")
        assert d._find_existing_track("Nobuo Uematsu", "Piano: Theme of Love (ensemble)", str(tmp_path),
                                      release_title=True) is None
        # A request's title still finds the song whatever its brackets say.
        assert d._find_existing_track("Nobuo Uematsu", "Piano: Theme of Love (Official Video)", str(tmp_path))

    def test_a_known_deezer_track_is_checked_as_a_release_title(self, monkeypatch, tmp_path):
        import downloader_manager
        d = Downloader(analyzer=None)
        seen = []
        monkeypatch.setattr(downloader_manager, "is_arl_stale", lambda: False)
        monkeypatch.setattr(d, "_ensure_vpn", lambda: None)
        monkeypatch.setattr(d, "get_metadata_from_sources", lambda *a, **k: None)
        monkeypatch.setattr(d, "_find_existing_track", lambda *a, **k: seen.append(k) or "/music/x.mp3")
        d.download_track("Nobuo Uematsu", "Piano: Theme of Love (ensemble)", "Piano Collections FINAL FANTASY IV",
                         None, "music_request_youtube_playlist", "u", str(tmp_path), 0, self.CONTEXT)
        d.download_track("Adele", "Hello", None, None, "music_request_youtube", "u", str(tmp_path), 0,
                         "source=youtube")
        assert [k.get("release_title") for k in seen] == [True, False]


def test_the_request_downloads_the_album_tracks_by_their_artist(deezer, monkeypatch, tmp_path):
    """The whole request, through the real handler: only reading the link, the
    download itself and Plex are stand-ins."""
    calls, replies = [], []

    class Status:
        async def edit(self, content=None, embed=None):
            replies.append(embed)

        async def delete(self):
            pass

    class Private:
        async def reply(self, text=None, embed=None, **kwargs):
            replies.append(embed or text)
            return Status()

        send = reply

    class Lock:
        async def async_acquire(self, timeout=30):
            return True

        def release(self):
            pass

    class Fake:
        def download_track(self, artist, title, album, url, kind, user, target, index, context):
            calls.append((artist, title, album, url, kind, context))
            return {"success": True, "file_path": f"/music/albums/{artist}/{album}/{title}.mp3", "download_tool": "deemix",
                    "metadata": {"artist": artist, "title": title, "album": album, "source": "deezer"}}

    async def read_link(url, text=""):
        return {"type": "playlist", "name": PLAYLIST, "tracks": _songs(), "source": "youtube", "url": url}

    async def nothing(*a, **k):
        return None

    async def lidarr(*a):
        pytest.fail("an album Deezer has is not handed to Lidarr")

    (tmp_path / "albums").mkdir()
    monkeypatch.setattr(discord_bot, "REQUESTS_DB", str(tmp_path / "requests.json"))
    monkeypatch.setattr(discord_bot, "extract_link_metadata", read_link)
    monkeypatch.setattr(discord_bot, "refresh_plex_library", nothing)
    monkeypatch.setattr(discord_bot, "try_lidarr_playlist_download", lidarr)
    monkeypatch.setattr(discord_bot, "get_downloader_lock", lambda: Lock())
    monkeypatch.setattr(discord_bot, "get_downloader", lambda: Fake())
    monkeypatch.setattr(discord_bot, "get_user_library", lambda user: str(tmp_path))
    monkeypatch.setattr(discord_bot, "DISCORD_TO_PLEX_MAPPING", {"me": "u"})
    monkeypatch.setattr(discord_bot, "CYCLE_RUNNING", False)
    monkeypatch.setattr(discord_bot, "_any_cycle_running_file", lambda: False)
    monkeypatch.setattr(discord_bot, "PLEX_AVAILABLE", True)
    monkeypatch.setenv("PLEX_URL", "")                  # a Plex playlist would be logged as skipped, not made
    made = []
    monkeypatch.setattr(discord_bot.logger, "info", lambda msg, *a, **k: made.append(msg))
    author = SimpleNamespace(id=7, mention="<@7>", bot=False, display_name="me", name="me")
    link = "https://youtube.com/playlist?list=PLexampleAlbumPlaylist0000"
    asyncio.run(discord_bot._handle_request(discord_bot._RequestShim(author, SimpleNamespace(id=1), object(), link, 5),
                                            Private(), requested=True))
    assert len(calls) == 14
    assert {c[0] for c in calls} == {"Nobuo Uematsu"} and {c[2] for c in calls} == {"Piano Collections FINAL FANTASY IV"}
    artist, title, album, url, kind, context = calls[12]
    assert title == "Piano: Epilogue" and url == "https://www.youtube.com/watch?v=vid00000005"
    assert kind == "music_request_youtube_playlist"
    assert "deezer_track=2503850111" in context and f"url={url}" in context
    assert not any("Creating Plex playlist" in m for m in made)     # the album is enough


# ── One video titled "Album - Track" ─────────────────────────────────────────
# "Final Fantasy VI Piano Collections - Terra's Theme" (217 s) names an album,
# which must not be filed as its artist.

TERRA = "https://youtu.be/vid00000006?si=abc"
FF6 = [("Tina", 217), ("Gau", 141), ("Cefca", 220), ("Spinach Rag", 152), ("Stragus", 202),
       ("The Mystic Forest", 203), ("Kids Run Through The City Corner", 195), ("Johnny C Bad", 215),
       ("Mystery Train", 156), ("The Decisive Battle", 153), ("Coin Song", 286), ("Celes", 187),
       ("Waltz de Chocobo", 156)]


def _ff6_deezer(artists=()):
    def get(path, **params):
        if path == "search/artist":
            return {"data": [{"name": n, "nb_fan": f} for n, f in artists]}
        if path == "search/album":
            q = params["q"].lower()
            data = []
            if "final fantasy vi" in q:
                data = [{"id": 501706691, "title": "Piano Collections FINAL FANTASY VI"}]
            if q == "blue":
                data = [{"id": 1971, "title": "Blue"}]
            return {"data": data}
        if path == "album/1971":
            return {"id": 1971, "title": "Blue", "artist": {"name": "Joni Mitchell"}, "nb_tracks": 2,
                    "tracks": {"data": [{"id": 1, "title": "All I Want", "duration": 214},
                                        {"id": 2, "title": "River", "duration": 245}]}}
        return {"id": 501706691, "title": "Piano Collections FINAL FANTASY VI", "artist": {"name": "Nobuo Uematsu"},
                "nb_tracks": len(FF6), "tracks": {"data": [{"id": 2503850281 + 10 * n, "title": f"Piano: {t}",
                                                            "duration": d} for n, (t, d) in enumerate(FF6)]}}
    return get


class TestAVideoNamedAfterItsAlbum:
    def test_the_album_its_artist_and_the_track(self, monkeypatch):
        monkeypatch.setattr(album_match, "_get", _ff6_deezer())
        found = album_match.album_for_song("Final Fantasy VI Piano Collections", "Terra's Theme", 217)
        assert (found["artist"], found["album"]) == ("Nobuo Uematsu", "Piano Collections FINAL FANTASY VI")
        assert (found["track"]["title"], found["track"]["id"]) == ("Piano: Tina", 2503850281)   # same 3:37

    def test_a_title_on_the_album_wins_over_a_length(self, monkeypatch):
        monkeypatch.setattr(album_match, "_get", _ff6_deezer())
        # 220 s is exactly "Cefca"'s length; the title says "Celes" (187 s, close enough).
        assert album_match.album_for_song("Final Fantasy VI Piano Collections", "Celes", 220)["track"]["title"] \
            == "Piano: Celes"

    def test_no_track_when_the_length_is_not_one_tracks(self, monkeypatch):
        monkeypatch.setattr(album_match, "_get", _ff6_deezer())
        # 216 s: "Tina" (217) and "Johnny C Bad" (215) are both a second away.
        found = album_match.album_for_song("Final Fantasy VI Piano Collections", "Terra's Theme", 216)
        assert found["artist"] == "Nobuo Uematsu" and found["track"] is None

    def test_a_real_artist_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(album_match, "_get", _ff6_deezer(artists=[("Blue", 900000)]))
        assert album_match.album_for_song("Blue", "All Rise", 214) is None

    def test_a_name_that_is_only_part_of_an_album_name(self, monkeypatch):
        monkeypatch.setattr(album_match, "_get", _ff6_deezer())
        assert album_match.album_for_song("Final Fantasy VI", "Terra's Theme", 217) is None

    def test_deezer_failing_changes_nothing(self):
        assert album_match.album_for_song("Final Fantasy VI Piano Collections", "Terra's Theme", 217) is None


def test_the_single_request_downloads_the_albums_track(monkeypatch, tmp_path):
    calls = []

    class Status:
        async def edit(self, content=None, embed=None):
            pass

    class Private:
        async def reply(self, text=None, embed=None, **kwargs):
            return Status()

        send = reply

    class Lock:
        async def async_acquire(self, timeout=30):
            return True

        def release(self):
            pass

    class Fake:
        def download_track(self, artist, title, album, url, kind, user, target, index, context):
            calls.append((artist, title, album, context))
            return {"success": True, "file_path": "/music/x.mp3", "download_tool": "deemix",
                    "metadata": {"artist": artist, "title": title, "source": "deezer"}}

    async def read_link(url, text=""):
        import link_extractor
        return link_extractor._build_variant_context({
            "type": "track", "artist": "Final Fantasy VI Piano Collections", "title": "Terra's Theme", "album": "",
            "source": "youtube", "url": "https://www.youtube.com/watch?v=vid00000006",
            "video_title": "Final Fantasy VI Piano Collections - Terra's Theme", "video_uploader": "SomeUploader",
            "video_duration": 217,
            "video_description": "Nobuo Uematsu composed and produced. Terra's Theme for Squaresoft's Final "
                                 "Fantasy VI role-playing game. Piano Collection."}, url, "youtube")

    async def nothing(*a, **k):
        return None

    (tmp_path / "albums").mkdir()
    monkeypatch.setattr(album_match, "_get", _ff6_deezer())
    monkeypatch.setattr(discord_bot, "REQUESTS_DB", str(tmp_path / "requests.json"))
    monkeypatch.setattr(discord_bot, "extract_link_metadata", read_link)
    monkeypatch.setattr(discord_bot, "refresh_plex_library", nothing)
    monkeypatch.setattr(discord_bot, "get_downloader_lock", lambda: Lock())
    monkeypatch.setattr(discord_bot, "get_downloader", lambda: Fake())
    monkeypatch.setattr(discord_bot, "get_user_library", lambda user: str(tmp_path))
    monkeypatch.setattr(discord_bot, "DISCORD_TO_PLEX_MAPPING", {"me": "u"})
    monkeypatch.setattr(discord_bot, "CYCLE_RUNNING", False)
    monkeypatch.setattr(discord_bot, "_any_cycle_running_file", lambda: False)
    author = SimpleNamespace(id=7, mention="<@7>", bot=False, display_name="me", name="me")
    asyncio.run(discord_bot._handle_request(discord_bot._RequestShim(author, SimpleNamespace(id=1), object(), TERRA, 5),
                                            Private(), requested=True))
    [(artist, title, album, context)] = calls
    assert (artist, title, album) == ("Nobuo Uematsu", "Piano: Tina", "Piano Collections FINAL FANTASY VI")
    assert "deezer_track=2503850281" in context and "video_title" not in context and "video_uploader" not in context
    assert "Final Fantasy VI Piano Collections -" not in context
    # The downloader keeps those names: nothing in the context names the album as an artist again.
    d = Downloader.__new__(Downloader)
    d._ai_confirms_uploader_is_performer = lambda *a: pytest.fail("no channel left to ask about")
    meta = d._resolve_effective_download_metadata(artist, title, album, context)
    assert (meta["artist"], meta["title"], meta["album"]) == ("Nobuo Uematsu", "Piano: Tina",
                                                              "Piano Collections FINAL FANTASY VI")
