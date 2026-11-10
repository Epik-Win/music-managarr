"""The admin is recognised from the documented setting, and a request's
artist and link survive what YouTube puts around them."""
import asyncio
import os
from types import SimpleNamespace

import discord_bot
from downloader import Downloader

OWNER, MEMBER = 408, 503


def _guild(owner_id=OWNER):
    members = {"carol": SimpleNamespace(id=OWNER, name="carol", display_name="Carol"),
               "dave.": SimpleNamespace(id=MEMBER, name="dave.", display_name="M")}
    return SimpleNamespace(owner_id=owner_id, members=list(members.values()), get_member_named=members.get)


def _interaction(user_id, guild):
    return SimpleNamespace(guild=guild, user=SimpleNamespace(id=user_id, name="x"))


def _configure(monkeypatch, override="carol:carol_plex"):
    monkeypatch.setattr(discord_bot, "DISCORD_TO_PLEX_MAPPING_ADMIN", override)
    monkeypatch.setattr(discord_bot, "DISCORD_TO_PLEX_MAPPING",
                        {"dave.": "dave_plex", "carol": "carol_plex"})


class TestAdmin:
    def test_the_documented_setting_finds_the_admin(self, monkeypatch):
        # `discord:plex` names the Discord member first: the admin is the
        # member "carol", not one called "carol_plex".
        _configure(monkeypatch)
        assert discord_bot._resolve_admin_discord_handle() == "carol"
        guild = _guild(owner_id=999)
        assert discord_bot._admin_member(guild).id == OWNER
        assert discord_bot._is_admin(_interaction(OWNER, guild))
        assert not discord_bot._is_admin(_interaction(MEMBER, guild))

    def test_one_name_or_nothing_set(self, monkeypatch):
        _configure(monkeypatch, "carol_plex")
        assert discord_bot._resolve_admin_discord_handle() == "carol"
        _configure(monkeypatch, "")
        assert discord_bot._resolve_admin_discord_handle() == "dave."      # the first entry

    def test_the_server_owner_is_never_refused(self, monkeypatch):
        _configure(monkeypatch, "dave.:dave_plex")
        guild = _guild()
        assert discord_bot._is_admin(_interaction(OWNER, guild))
        assert discord_bot._is_admin(_interaction(MEMBER, guild))

    def test_admin_commands_are_hidden_from_other_members(self):
        for name in ("one_star_delete", "clean_library"):
            command = discord_bot.bot.tree.get_command(name)
            assert command.default_permissions.manage_guild and command.guild_only, name
        assert discord_bot.bot.tree.get_command("request").default_permissions is None

    def test_the_admin_really_can_use_the_one_star_command(self, monkeypatch, tmp_path):
        # The real command, with its real admin check.
        import runtime_settings
        _configure(monkeypatch)
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setattr(discord_bot, "DISCORD_MAIN_CHANNEL", 1)
        off = SimpleNamespace(value="off")
        command = discord_bot.bot.tree.get_command("one_star_delete")

        def use(user_id, state):
            replies = []

            async def send_message(text, ephemeral=False):
                replies.append(text)

            interaction = SimpleNamespace(guild=_guild(owner_id=999), channel=SimpleNamespace(id=1, parent_id=None),
                                          user=SimpleNamespace(id=user_id, name="x"),
                                          response=SimpleNamespace(send_message=send_message))
            asyncio.run(command.callback(interaction, state))
            return replies[0]

        assert "Only the admin" in use(MEMBER, off)
        assert runtime_settings.get_bool("ONE_STAR_DELETE", True) is True          # refused: nothing changed
        assert "is **off**" in use(OWNER, off)
        assert runtime_settings.get_bool("ONE_STAR_DELETE", True) is False


TOM = ("source=youtube; requested_track=Tom Odell - Another Love; album=Songs from Another Love; "
       "url=https://www.youtube.com/watch?v=vid00000001; video_title=Another Love; video_uploader=Tom Odell; "
       "video_duration=251s; video_description=Provided to YouTube by ITNO/Columbia Another Love · Tom Odell "
       "Mix Engineer: Someone; message=https://youtube.com/source/vid00000001/shorts?si=abc")


def _resolve(artist, title, context):
    d = Downloader.__new__(Downloader)
    return d, d._build_request_context_metadata(artist, title, context)


class TestArtistFromContext:
    def test_the_label_in_a_video_description_is_not_the_artist(self):
        d, meta = _resolve("Tom Odell", "Another Love", TOM)
        assert meta["artist"] == "Tom Odell" and meta["title"] == "Another Love"

    def test_a_description_names_no_version(self):
        d = Downloader.__new__(Downloader)
        assert not d._has_variant_request_hint(TOM)
        assert d._get_explicit_variant_label(TOM) is None

    def test_a_song_title_with_by_in_it_names_no_artist(self):
        ctx = "source=youtube; requested_track=Ben E. King - Stand by Me; video_title=Stand by Me; video_uploader=Ben E. King"
        assert _resolve("Ben E. King", "Stand by Me", ctx)[1]["artist"] == "Ben E. King"
        typed = "source=youtube; requested_track=Ben E. King - Stand by Me; message=Stand by Me https://youtu.be/x"
        assert _resolve("Ben E. King", "Stand by Me", typed)[1]["artist"] == "Ben E. King"

    def test_what_the_requester_wrote_still_counts(self):
        assert _resolve("Someone", "Song", "Arabic version by Rachid ASEYAKHE")[1]["artist"] == "Rachid ASEYAKHE"
        shazam = "source=shazam; message=Stand by Me by Ben E. King https://www.shazam.com/track/1"
        assert _resolve("", "Stand by Me", shazam)[1]["artist"] == "Ben E. King"


class TestOneVideo:
    def test_every_form_of_a_link_is_the_one_video(self):
        want = "https://www.youtube.com/watch?v=vid00000001"
        for url in ("https://youtube.com/source/vid00000001/shorts?si=abc",
                    "https://youtube.com/shorts/vid00000001", "https://youtu.be/vid00000001?si=abc",
                    "https://www.youtube.com/watch?v=vid00000001&list=PLabc&index=3",
                    "https://music.youtube.com/watch?si=abc&v=vid00000001"):
            assert Downloader._single_video_url(url) == want, url
        other = "https://soundcloud.com/a/b"
        assert Downloader._single_video_url(other) == other

    def test_the_tool_is_handed_the_one_video(self):
        d = Downloader.__new__(Downloader)
        seen = {}
        d._download_with_yt_dlp = lambda query, target, **k: seen.update(query=query) or (False, None)
        d._is_catalogue_page = lambda url: False
        d._try_tool_download("yt-dlp", "Tom Odell", "Another Love",
                             "https://youtube.com/source/vid00000001/shorts", "/music/albums", TOM, None)
        assert seen["query"] == "https://www.youtube.com/watch?v=vid00000001"

    def test_a_failed_download_leaves_nothing_behind(self, tmp_path, monkeypatch):
        # A failed download leaves no ytdlp_* folder in the library.
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        library = tmp_path / "albums"
        library.mkdir()
        d = Downloader.__new__(Downloader)
        d._ensure_vpn = lambda: None

        def fail_after_writing(url, clients, tmp_dir, args):
            open(os.path.join(tmp_dir, "a short.mp3"), "wb").close()
            return None, Exception("HTTP Error 403: Forbidden")

        d._yt_dlp_try_clients = fail_after_writing
        monkeypatch.setattr("downloader.geo_relay.is_retryable_elsewhere", lambda e: False)
        assert d._download_with_yt_dlp("https://www.youtube.com/watch?v=vid00000001", str(library)) == (False, None)
        assert list(library.iterdir()) == []
        assert os.listdir(tmp_path / "data" / "download_staging") == []

    def test_a_download_leaves_only_the_song(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        library = tmp_path / "albums"
        library.mkdir()
        d = Downloader.__new__(Downloader)

        def write_one(url, clients, tmp_dir, args):
            assert not tmp_dir.startswith(str(library))                  # never inside the library
            open(os.path.join(tmp_dir, "Tom Odell - Another Love.mp3"), "wb").close()
            return None, None

        d._yt_dlp_try_clients = write_one
        ok, path = d._download_with_yt_dlp("https://www.youtube.com/watch?v=vid00000001", str(library))
        assert ok and [p.name for p in library.iterdir()] == [os.path.basename(path)]
        assert os.listdir(tmp_path / "data" / "download_staging") == []

    def test_yt_dlp_never_walks_a_whole_list(self, monkeypatch, tmp_path):
        import yt_dlp
        options = []

        class Fake:
            def __init__(self, opts):
                options.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=True):
                return {"thumbnail": None}

        monkeypatch.setattr(yt_dlp, "YoutubeDL", Fake)
        d = Downloader.__new__(Downloader)
        d._yt_dlp_try_clients("https://www.youtube.com/watch?v=vid00000001", [["mweb"]], str(tmp_path), {})
        assert options[0]["playlist_items"] == "1"
