"""A song that downloaded is kept when it is the song, and a clip is not.

"The Legend of Ashitaka" by Joe Hisaishi, requested by link: the linked video
is the recording meant, even when its file is called "... / Live" and the
request asks for the original; a SoundCloud 30-second preview is no song.
"""
from types import SimpleNamespace

import pytest

from downloader import Downloader
from naming import artist_from_channel, song_in_quotes

LINK = "https://youtube.com/source/vid00000002/shorts?si=abc"
VIDEO_FILE = 'The Legend of Ashitaka (Symphonic Suite ＂Princess Mononoke＂ ⧸ Live).mp3'
ASHITAKA = ('source=youtube; requested_track=Joe Hisaishi - The Legend of Ashitaka; album=WORKS II -Orchestra Nights-; '
            'url=https://www.youtube.com/watch?v=vid00000002; '
            'video_title=The Legend of Ashitaka (Symphonic Suite "Princess Mononoke" / Live); '
            'video_uploader=Joe Hisaishi Official; video_duration=332s; '
            f'video_description=Provided to YouTube by Universal Music Group; message={LINK}')


def _file(tmp_path, name=VIDEO_FILE):
    path = tmp_path / name
    path.write_bytes(b"")
    return path


def _yt_dlp_returning(d, path):
    d._download_with_yt_dlp = lambda query, target, **kw: (True, str(path))


class TestTheRequestersOwnVideo:
    def test_it_is_kept_whatever_it_is_called(self, tmp_path):
        d = Downloader.__new__(Downloader)
        path = _file(tmp_path, "Some Uploader's Upload Name.mp3")
        _yt_dlp_returning(d, path)
        kept = d._try_tool_download("yt-dlp", "Joe Hisaishi", "The Legend of Ashitaka (Live Version)", LINK,
                                    str(tmp_path), ASHITAKA, None)
        assert kept == str(path) and path.exists()

    def test_a_video_found_by_searching_is_still_checked(self, tmp_path):
        d = Downloader.__new__(Downloader)
        path = _file(tmp_path, "Somebody - Something Else.mp3")
        _yt_dlp_returning(d, path)
        assert d._try_tool_download("yt-dlp", "Joe Hisaishi", "The Legend of Ashitaka", None,
                                    str(tmp_path), ASHITAKA, None) is None
        assert not path.exists()

    def test_any_other_video_is_still_checked(self, tmp_path):
        d = Downloader.__new__(Downloader)
        path = _file(tmp_path, "Somebody - Something Else.mp3")
        _yt_dlp_returning(d, path)
        assert d._try_tool_download("yt-dlp", "Joe Hisaishi", "The Legend of Ashitaka",
                                    "https://www.youtube.com/watch?v=aaaaaaaaaaa", str(tmp_path), ASHITAKA, None) is None

    def test_it_comes_before_searching_soundcloud(self):
        d = Downloader.__new__(Downloader)
        tools = ["deemix", "soundcloud", "yt-dlp"]
        assert d._link_first(tools, LINK, ASHITAKA) == ["deemix", "yt-dlp", "soundcloud"]
        assert d._link_first(tools, None, ASHITAKA) == tools
        assert d._link_first(tools, "https://www.shazam.com/track/1/x", "url=https://www.shazam.com/track/1/x") == tools


def test_a_request_tries_its_link_before_soundcloud_and_asks_for_the_right_artist(monkeypatch, tmp_path):
    import downloader_manager
    d = Downloader(analyzer=None)
    monkeypatch.setattr(downloader_manager, "is_arl_stale", lambda: False)
    monkeypatch.setattr(d, "_ensure_vpn", lambda: None)
    monkeypatch.setattr(d, "get_metadata_from_sources", lambda *a, **k: {"album": "Joe Hisaishi"})
    monkeypatch.setattr(d, "_find_existing_track", lambda *a, **k: None)
    monkeypatch.setattr(d, "_is_track_blacklisted", lambda *a, **k: False)
    monkeypatch.setattr(d, "check_download_tools", lambda: {"deemix": True, "soundcloud": True, "yt-dlp": True})
    tried = []

    def attempt(tool, artist, title, url, target, context, user, album=None):
        tried.append((tool, artist, album))

    monkeypatch.setattr(d, "_try_tool_download", attempt)
    result = d.download_track("Joe Hisaishi", "The Legend of Ashitaka", "WORKS II -Orchestra Nights-", LINK,
                              "music_request_youtube", "u", str(tmp_path), 0, ASHITAKA)
    assert not result["success"]
    assert [t[0] for t in tried] == ["deemix", "yt-dlp", "soundcloud"]
    assert {t[1] for t in tried} == {"Joe Hisaishi"}                  # not "Joe Hisaishi Official"
    assert {t[2] for t in tried} == {"WORKS II -Orchestra Nights-"}   # the link's album, not a lookup's guess


class TestWhichVersionAFileIs:
    def test_a_file_named_live_is_the_live_version(self, tmp_path):
        d = Downloader.__new__(Downloader)
        d._extract_artist_title_from_file = lambda path: ("Joe Hisaishi", "The Legend of Ashitaka")  # brackets gone
        assert d._is_download_match(str(_file(tmp_path)), "Joe Hisaishi", "The Legend of Ashitaka (Live Version)",
                                    request_context=ASHITAKA)

    def test_the_studio_version_still_does_not_pass_for_live(self, tmp_path):
        d = Downloader.__new__(Downloader)
        assert not d._is_download_match(str(_file(tmp_path, "The Legend of Ashitaka.mp3")), "Joe Hisaishi",
                                        "The Legend of Ashitaka (Live Version)", request_context=ASHITAKA)

    def test_a_live_file_does_not_pass_for_the_studio_version(self, tmp_path):
        d = Downloader.__new__(Downloader)
        assert not d._is_download_match(str(_file(tmp_path, "Hello (Live from NYC).mp3")), "Adele", "Hello",
                                        request_context="video_title=Adele - Hello")


class TestAFileNamingNoArtist:
    def test_a_searched_file_naming_no_artist_is_not_taken_on_its_title(self, tmp_path):
        # SoundCloud's "Prologue" by someone else is not Nobuo Uematsu's: such
        # a file gets in only as the video the requester linked.
        d = Downloader.__new__(Downloader)
        assert not d._is_download_match(str(_file(tmp_path, "Prologue.mp3")), "Nobuo Uematsu", "Prologue")

    def test_the_wrong_title_is_not(self, tmp_path):
        d = Downloader.__new__(Downloader)
        assert not d._is_download_match(str(_file(tmp_path, "Goodbye Forever.mp3")), "Adele", "Hello")

    def test_a_file_naming_another_artist_is_still_refused(self, tmp_path):
        d = Downloader.__new__(Downloader)
        assert not d._is_download_match(str(_file(tmp_path, "Xqzw - Hello.mp3")), "Adele", "Hello")


class TestClips:
    PREVIEW = {"title": "The Legend of Ashitaka", "uploader": "Joe Hisaishi", "webpage_url": "https://soundcloud.com/p",
               "formats": [{"format_id": "hls_mp3_0_1_preview"}, {"format_id": "http_mp3_0_1_preview"}]}
    WHOLE = {"title": "The Legend of Ashitaka", "uploader": "Someone", "webpage_url": "https://soundcloud.com/w",
             "formats": [{"format_id": "hls_mp3_0_1"}]}

    def test_only_a_preview_is_recognised(self):
        assert Downloader._only_a_preview(self.PREVIEW)
        assert not Downloader._only_a_preview(self.WHOLE)
        assert not Downloader._only_a_preview({"title": "no formats listed"})

    def _soundcloud(self, monkeypatch, tmp_path, entries):
        import yt_dlp
        downloaded = []

        class Fake:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, target, download=True):
                if download:
                    downloaded.append(target)
                    return {}
                return {"entries": entries}

        monkeypatch.setattr(yt_dlp, "YoutubeDL", Fake)
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        d = Downloader.__new__(Downloader)
        d._download_with_soundcloud("Joe Hisaishi - The Legend of Ashitaka", str(tmp_path))
        return downloaded

    def test_soundcloud_never_downloads_a_preview(self, monkeypatch, tmp_path):
        assert self._soundcloud(monkeypatch, tmp_path, [self.PREVIEW]) == []

    def test_soundcloud_still_downloads_a_whole_track(self, monkeypatch, tmp_path):
        assert self._soundcloud(monkeypatch, tmp_path, [self.WHOLE]) == ["https://soundcloud.com/w"]

    @pytest.mark.parametrize("seconds, refused", [(30.0, True), (165.0, True), (166.0, False), (332.0, False)])
    def test_a_file_far_shorter_than_the_linked_video_is_refused(self, tmp_path, monkeypatch, seconds, refused):
        import mutagen
        monkeypatch.setattr(mutagen, "File", lambda path: SimpleNamespace(info=SimpleNamespace(length=seconds)))
        d = Downloader.__new__(Downloader)
        assert d._cut_short(str(_file(tmp_path)), ASHITAKA) is refused

    def test_a_tools_clip_is_deleted_even_with_the_right_name(self, tmp_path, monkeypatch):
        import mutagen
        monkeypatch.setattr(mutagen, "File", lambda path: SimpleNamespace(info=SimpleNamespace(length=30.0)))
        d = Downloader.__new__(Downloader)
        path = _file(tmp_path)
        d._download_with_soundcloud = lambda query, target, context=None: (True, str(path))
        assert d._try_tool_download("soundcloud", "Joe Hisaishi", "The Legend of Ashitaka (Live Version)", LINK,
                                    str(tmp_path), ASHITAKA, None) is None
        assert not path.exists()

    def test_without_a_stated_length_nothing_is_refused(self, tmp_path):
        d = Downloader.__new__(Downloader)
        assert d._cut_short(str(_file(tmp_path)), "source=youtube") is False


class TestChannelNames:
    @pytest.mark.parametrize("channel, artist", [
        ("Joe Hisaishi Official", "Joe Hisaishi"),
        ("いきものがかり Official YouTube Channel", "いきものがかり"),
        ("Adele - Topic", "Adele"), ("AdeleVEVO", "Adele"), ("Linkin Park - Official", "Linkin Park"),
        ("Someone Official Music Channel", "Someone"), ("Someone YouTube Channel", "Someone"),
        ("Official髭男dism", "Official髭男dism"),       # part of a band's name
        ("The Channel", "The Channel"), ("Official", "Official"), ("Tom Odell", "Tom Odell")])
    def test_a_channel_becomes_its_artist(self, channel, artist):
        assert artist_from_channel(channel) == artist

    @pytest.mark.parametrize("title, artist, song", [
        ("いきものがかり 『ブルーバード』Music Video", "いきものがかり", "ブルーバード"),
        ("香西かおり「無言坂」", "香西かおり", "無言坂"),
        ("「無言坂」", "香西かおり", "無言坂"),
        ("『ブルーバード』MV", "いきものがかり", "ブルーバード"),
        ("Someone Else 『Song』", "いきものがかり", "Someone Else 『Song』"),   # another name: may be a cover
        ("『Song』Live at Budokan", "A", "『Song』Live at Budokan"),            # may be part of the title
        ("Plain Title", "A", "Plain Title")])
    def test_the_song_in_quotation_marks(self, title, artist, song):
        assert song_in_quotes(title, artist) == song

    def test_a_video_title_and_channel_become_the_song_and_artist(self, monkeypatch):
        import json
        import link_extractor
        monkeypatch.setattr(link_extractor, "run_ytdlp", lambda cmd, timeout=30: SimpleNamespace(
            returncode=0, stderr="", stdout=json.dumps({
                "title": "いきものがかり 『ブルーバード』Music Video", "categories": ["Music"], "duration": 217,
                "uploader": "いきものがかり Official YouTube Channel"})))
        result = link_extractor.extract_youtube_metadata("https://www.youtube.com/watch?v=vid00000003")
        assert (result["artist"], result["title"]) == ("いきものがかり", "ブルーバード")
        assert result["video_uploader"] == "いきものがかり Official YouTube Channel"     # the raw facts stay

    def test_the_downloader_does_not_put_the_video_title_back(self):
        # The song read from the link must not be replaced by the raw video
        # title, or Deezer would be searched for that whole string.
        context = ("source=youtube; requested_track=いきものがかり - ブルーバード; "
                   "url=https://www.youtube.com/watch?v=vid00000003; "
                   "video_title=いきものがかり 『ブルーバード』Music Video; "
                   "video_uploader=いきものがかり Official YouTube Channel")
        d = Downloader.__new__(Downloader)
        d._ai_confirms_uploader_is_performer = lambda *a: pytest.fail("the same artist: nothing to ask")
        meta = d._build_request_context_metadata("いきものがかり", "ブルーバード", context)
        assert (meta["artist"], meta["title"]) == ("いきものがかり", "ブルーバード")

    def test_the_real_request_keeps_the_real_artist(self):
        d = Downloader.__new__(Downloader)
        meta = d._build_request_context_metadata("Joe Hisaishi", "The Legend of Ashitaka", ASHITAKA)
        assert meta["artist"] == "Joe Hisaishi"
