"""The request history says what really happened, and the bot and a cycle, in
separate processes, don't undo each other: a song that downloaded is not left
"pending", where a failed request would look the same as a finished one."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import discord_bot


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "discord_requests.json"
    monkeypatch.setattr(discord_bot, "REQUESTS_DB", str(path))
    monkeypatch.setattr(discord_bot, "REQUEST_HISTORY", {})
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return path


def _saved(db, user="u"):
    return json.loads(db.read_text())[user]


@pytest.mark.parametrize("mode, result, status, text", [
    ("immediate", {"file_path": "/music/a.mp3"}, "completed", "Downloaded: /music/a.mp3"),
    ("existing", {"file_path": "/music/a.mp3"}, "completed", "Already in the library: /music/a.mp3"),
    ("queue", {"error": "All tools failed"}, "pending", "All tools failed"),       # the next cycle retries it
    ("queue", None, "pending", "Queued"),
    ("lock_timeout", None, "pending", "Queued")])
def test_the_outcome_of_the_bots_attempt_is_recorded(db, mode, result, status, text):
    entry = discord_bot.track_request("u", "A", "One", "music_request_youtube")
    discord_bot.record_request_outcome("u", entry, mode, result)
    [saved] = _saved(db)
    assert saved["status"] == status and text in saved["result"]


def test_only_that_request_changes(db):
    first = discord_bot.track_request("u", "A", "One", "music_request_youtube")
    discord_bot.track_request("u", "B", "Two", "music_request_youtube")
    discord_bot.record_request_outcome("u", first, "immediate", {"file_path": "/x.mp3"})
    assert [r["status"] for r in _saved(db)] == ["completed", "pending"]


def test_a_new_request_keeps_what_a_cycle_saved(db):
    discord_bot.track_request("u", "A", "One", "music_request_youtube")
    history = json.loads(db.read_text())                     # a cycle, in its own process
    history["u"][0]["status"] = "completed"
    db.write_text(json.dumps(history))
    discord_bot.track_request("u", "B", "Two", "music_request_youtube")
    assert [(r["title"], r["status"]) for r in _saved(db)] == [("One", "completed"), ("Two", "pending")]
    assert [r["status"] for r in discord_bot.get_recent_requests("u")] == ["completed", "pending"]


def test_a_cycle_keeps_a_request_added_while_it_worked(db, monkeypatch):
    import app
    discord_bot.track_request("u", "A", "One", "music_request_youtube")
    monkeypatch.setattr(app, "check_if_track_exists", lambda *a, **k: (False, None, None))
    analyzer = object.__new__(app.MusicAIAnalyzer)

    def download(recs, **kwargs):
        discord_bot.track_request("u", "B", "Arrived meanwhile", "music_request_youtube")
        return recs

    analyzer.download_recommended_music = download
    assert analyzer.process_discord_requests() == 1
    assert [(r["title"], r["status"]) for r in _saved(db)] == [("One", "completed"), ("Arrived meanwhile", "pending")]


def test_a_cycle_leaves_requests_it_did_not_handle_alone(db, monkeypatch):
    import app
    done = discord_bot.track_request("u", "A", "Settled", "music_request_youtube")
    discord_bot.record_request_outcome("u", done, "immediate", {"file_path": "/x.mp3"})
    discord_bot.track_request("u", "B", "Waiting", "music_request_youtube")
    monkeypatch.setattr(app, "check_if_track_exists", lambda *a, **k: (False, None, None))
    analyzer = object.__new__(app.MusicAIAnalyzer)
    analyzer.download_recommended_music = lambda recs, **kwargs: []
    assert analyzer.process_discord_requests() == 1
    settled, waiting = _saved(db)
    assert settled["result"] == "Downloaded: /x.mp3" and waiting["status"] == "failed"


def test_a_damaged_file_is_not_saved_over(db):
    db.write_text("{not json")
    entry = discord_bot.track_request("u", "A", "One", "music_request_youtube")
    discord_bot.record_request_outcome("u", entry, "immediate", {"file_path": "/x.mp3"})
    assert db.read_text() == "{not json"


TOM_LINK = "https://youtube.com/source/vid00000001/shorts?si=abc"
TOM_METADATA = {
    "type": "track", "artist": "Tom Odell", "title": "Another Love", "album": "Songs from Another Love",
    "source": "youtube", "url": "https://www.youtube.com/watch?v=vid00000001", "uploader": "Tom Odell",
    "video_title": "Another Love", "description": "Provided to YouTube by ITNO/Columbia Another Love · Tom Odell"}


def _request_a_song(db, monkeypatch, tmp_path, answer):
    """A link goes through the real request handler, its history and its final
    message; only reading the link and the download itself are stand-ins."""
    calls, replies = [], []

    class Status:
        async def edit(self, content=None, embed=None):
            replies.append(embed)

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
        def download_track(self, *args):
            calls.append(args)
            return answer

    async def read_link(url, text=""):
        return dict(TOM_METADATA)

    async def no_scan(user):
        pass

    (tmp_path / "albums").mkdir(exist_ok=True)
    monkeypatch.setattr(discord_bot, "extract_link_metadata", read_link)
    monkeypatch.setattr(discord_bot, "refresh_plex_library", no_scan)
    monkeypatch.setattr(discord_bot, "get_downloader_lock", lambda: Lock())
    monkeypatch.setattr(discord_bot, "get_downloader", lambda: Fake())
    monkeypatch.setattr(discord_bot, "get_user_library", lambda user: str(tmp_path))
    monkeypatch.setattr(discord_bot, "DISCORD_TO_PLEX_MAPPING", {"me": "u"})
    monkeypatch.setattr(discord_bot, "CYCLE_RUNNING", False)
    monkeypatch.setattr(discord_bot, "_any_cycle_running_file", lambda: False)
    author = SimpleNamespace(id=7, mention="<@7>", bot=False, display_name="me", name="me")
    shim = discord_bot._RequestShim(author, SimpleNamespace(id=1), object(), TOM_LINK, 5)
    asyncio.run(discord_bot._handle_request(shim, Private(), requested=True))
    return calls, replies


def test_a_request_that_downloads_ends_completed(db, monkeypatch, tmp_path):
    calls, replies = _request_a_song(db, monkeypatch, tmp_path, {
        "success": True, "file_path": "/music/albums/Tom Odell/Another Love.mp3", "download_tool": "yt-dlp",
        "metadata": {"artist": "Tom Odell", "title": "Another Love", "source": "deezer"}})
    assert calls[0][:2] == ("Tom Odell", "Another Love")
    [saved] = _saved(db)
    assert saved["status"] == "completed" and saved["result"] == "Downloaded: /music/albums/Tom Odell/Another Love.mp3"
    assert any(f.name == "✅ Status" for f in replies[-1].fields)


def test_a_song_already_there_is_reported_so(db, monkeypatch, tmp_path):
    _calls, replies = _request_a_song(db, monkeypatch, tmp_path, {
        "success": True, "file_path": "/music/albums/Tom Odell/Another Love.mp3", "download_tool": "none",
        "metadata": {"artist": "Tom Odell", "title": "Another Love", "source": "existing"}})
    [saved] = _saved(db)
    assert saved["status"] == "completed" and saved["result"].startswith("Already in the library")
    assert any("already exists" in f.value for f in replies[-1].fields)


def test_a_failed_download_says_so_and_stays_queued_with_the_reason(db, monkeypatch, tmp_path):
    _calls, replies = _request_a_song(db, monkeypatch, tmp_path, {"success": False, "error": "All tools failed"})
    [saved] = _saved(db)
    assert saved["status"] == "pending" and "All tools failed" in saved["result"]
    assert any(f.value == "All tools failed" for f in replies[-1].fields)
