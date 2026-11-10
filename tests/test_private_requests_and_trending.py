"""Song requests stay between the bot and the requester; each user has one
trending playlist, emptied every cycle and filled with that cycle's songs."""
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import discord
import discord_bot


class _Thread(discord.Thread):
    """A stand-in private thread (a discord.Thread subclass, so isinstance holds)."""

    def __init__(self, tid):
        self.id, self.locked, self.sent, self.added = tid, False, [], []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return SimpleNamespace(content=content)

    async def add_user(self, user):
        self.added.append(user.id)

    mention = property(lambda self: f'<#{self.id}>')


class _Channel:
    def __init__(self, cid):
        self.id, self.name, self.sent, self.threads = cid, 'music_requests', [], []
        self.mention, self.guild = f'<#{cid}>', None

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return SimpleNamespace(content=content)

    async def create_thread(self, **kwargs):
        thread = _Thread(1000 + len(self.threads))
        self.threads.append((thread, kwargs))
        return thread


class _Msg:
    def __init__(self, content, author, channel):
        self.content, self.author, self.channel, self.guild, self.id, self.deleted = \
            content, author, channel, object(), 5, False

    async def delete(self):
        self.deleted = True


def _author(uid=7):
    return SimpleNamespace(id=uid, mention=f'<@{uid}>', bot=False, display_name='me', name='me')


def test_replies_stay_in_the_private_thread():
    thread = _Thread(1)
    asyncio.run(discord_bot._ThreadReply(thread).reply("✅ Downloaded", delete_after=300))
    assert thread.sent == [("✅ Downloaded", {})]                     # kept: no auto-delete


def test_a_users_private_thread_is_opened_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot, 'REQUEST_THREADS_FILE', str(tmp_path / 'threads.json'))
    channel, user = _Channel(42), _author()
    first = asyncio.run(discord_bot._request_thread(channel, user))
    monkeypatch.setattr(discord_bot.bot, 'get_channel', lambda tid: first if tid == first.id else None)
    again = asyncio.run(discord_bot._request_thread(channel, user))
    assert again is first and len(channel.threads) == 1
    kwargs = channel.threads[0][1]
    assert kwargs['type'] == discord.ChannelType.private_thread and kwargs['invitable'] is False
    assert first.added == [7] and channel.sent == []                # nothing posted in the channel


def test_a_link_in_the_channel_needs_no_button_and_moves_to_the_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot, 'REQUEST_THREADS_FILE', str(tmp_path / 'threads.json'))
    monkeypatch.setattr(discord_bot, 'DISCORD_MAIN_CHANNEL', 42)
    monkeypatch.setattr(discord_bot, 'extract_urls_from_text', lambda text: ['https://youtu.be/x'])
    handled = []

    async def handle(message, private, requested=False):
        handled.append((message, private, requested))
    monkeypatch.setattr(discord_bot, '_handle_request', handle)
    channel = _Channel(42)
    msg = _Msg('https://youtu.be/x', _author(), channel)
    asyncio.run(discord_bot.on_message(msg))
    thread = channel.threads[0][0]
    assert msg.deleted and channel.sent == []
    assert thread.sent[-1][0] == '📨 <@7> requested: https://youtu.be/x'
    shim, private, requested = handled[0]
    assert requested and private.thread is thread and shim.content == 'https://youtu.be/x'


def test_a_link_in_the_users_own_thread_is_handled_there_and_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot, 'REQUEST_THREADS_FILE', str(tmp_path / 'threads.json'))
    monkeypatch.setattr(discord_bot, 'DISCORD_MAIN_CHANNEL', 42)
    monkeypatch.setattr(discord_bot, 'extract_urls_from_text', lambda text: ['https://youtu.be/x'])
    discord_bot._save_request_threads({'7': 1000})
    handled = []

    async def handle(message, private, requested=False):
        handled.append(private.thread)
    monkeypatch.setattr(discord_bot, '_handle_request', handle)
    thread = _Thread(1000)
    msg = _Msg('https://youtu.be/x', _author(), thread)
    asyncio.run(discord_bot.on_message(msg))
    assert not msg.deleted and handled == [thread] and thread.sent == []


def _analyzer(playlists, tracks):
    import app
    analyzer = app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)
    section = SimpleNamespace(update=lambda: None, reload=lambda: None, refreshing=False,
                              searchTracks=lambda **kw: tracks if 'sort' in kw else [])
    created = []
    analyzer.plex = SimpleNamespace(playlists=lambda: playlists,
                                    library=SimpleNamespace(section=lambda name: section),
                                    createPlaylist=lambda title, items: created.append((title, items)))
    analyzer.cycle_downloads, analyzer.cycle_started_at = [], None
    analyzer._wait_for_plex_scan = lambda section, timeout=900: None
    analyzer._music_section_for_scans = lambda: SimpleNamespace(update=lambda: None, reload=lambda: None,
                                                               refreshing=False)
    return analyzer, created


class _Playlist:
    playlistType, smart = 'audio', False

    def __init__(self, items, title='Trending Now for You'):
        self._items, self.title, self.deleted = list(items), title, False

    def delete(self):
        self.deleted = True

    def editTitle(self, title):
        self.title = title

    def items(self):
        return list(self._items)

    def removeItems(self, items):
        self._items = [i for i in self._items if i not in items]

    def addItems(self, items):
        self._items += list(items)


def _track(key, path):
    return SimpleNamespace(ratingKey=key, title=str(key), media=[SimpleNamespace(parts=[SimpleNamespace(file=path)])])


def _download(path, playlist):
    return {'file_path': path, 'artist': 'A', 'title': 'a', 'playlist': playlist}


def test_trending_and_taste_picks_go_in_their_own_playlists(monkeypatch):
    import app
    monkeypatch.setattr(app, 'convert_host_path_to_container_path', lambda p: p)
    old = _track(1, '/music/albums/Old/Old/old.mp3')
    hit, similar = _track(2, '/music/albums/A/A/a.mp3'), _track(3, '/music/albums/B/B/b.mp3')
    trending, liked = _Playlist([old]), _Playlist([old], title='You Might Like')
    analyzer, created = _analyzer([trending, liked], [hit, similar, old])
    analyzer.start_cycle_playlists()
    assert trending.items() == [] and liked.items() == [] and isinstance(analyzer.cycle_started_at, datetime)
    analyzer.cycle_downloads = [_download('/music/albums/A/A/a.mp3', 'trending'),
                                _download('/music/albums/B/B/b.mp3', 'similar')]
    analyzer.fill_cycle_playlists()
    assert [t.ratingKey for t in trending.items()] == [2] and [t.ratingKey for t in liked.items()] == [3]
    assert created == []


def test_a_first_cycle_creates_both_playlists(monkeypatch):
    import app
    monkeypatch.setattr(app, 'convert_host_path_to_container_path', lambda p: p)
    hit, similar = _track(7, '/music/albums/A/A/a.mp3'), _track(8, '/music/albums/B/B/b.mp3')
    analyzer, created = _analyzer([], [hit, similar])
    analyzer.start_cycle_playlists()
    analyzer.cycle_downloads = [_download('/music/albums/A/A/a.mp3', 'trending'),
                                _download('/music/albums/B/B/b.mp3', 'similar')]
    analyzer.fill_cycle_playlists()
    assert created == [('Trending Now for You', [hit]), ('You Might Like', [similar])]


def test_the_old_trending_now_is_renamed_not_recreated(monkeypatch):
    import app
    monkeypatch.setattr(app, 'convert_host_path_to_container_path', lambda p: p)
    pinned = _Playlist([_track(1, '/music/albums/Old/Old/old.mp3')], title='Trending Now')
    analyzer, created = _analyzer([pinned], [])
    analyzer.start_cycle_playlists()
    assert pinned.title == 'Trending Now for You' and pinned.items() == [] and not pinned.deleted


def test_duplicate_playlists_are_deleted_and_one_is_kept(monkeypatch):
    import app
    monkeypatch.setattr(app, 'convert_host_path_to_container_path', lambda p: p)
    old = _track(1, '/music/albums/Old/Old/old.mp3')
    kept, copy, earlier = _Playlist([old]), _Playlist([old], title='trending now for you '), _Playlist([old], title='Trending Now')
    other = _Playlist([old], title='Trending Hip-Hop')          # a playlist of the user's own
    analyzer, created = _analyzer([earlier, copy, kept, other], [])
    analyzer.start_cycle_playlists()
    survivors = [p for p in (kept, copy, earlier) if not p.deleted]
    assert len(survivors) == 1 and survivors[0].title.strip().casefold() == 'trending now for you'
    assert not other.deleted


def test_no_songs_of_a_kind_or_no_cycle_removes_its_playlist(monkeypatch):
    import app
    monkeypatch.setattr(app, 'convert_host_path_to_container_path', lambda p: p)
    trending, liked = _Playlist([_track(1, '/x.mp3')]), _Playlist([_track(1, '/x.mp3')], title='You Might Like')
    analyzer, created = _analyzer([trending, liked], [])
    analyzer.remove_cycle_playlists()                            # a skipped cycle
    assert trending.deleted and liked.deleted
    hit = _track(2, '/music/albums/A/A/a.mp3')
    trending, liked = _Playlist([]), _Playlist([], title='You Might Like')
    analyzer, created = _analyzer([trending, liked], [hit])
    analyzer.start_cycle_playlists()
    analyzer.cycle_downloads = [_download('/music/albums/A/A/a.mp3', 'trending')]
    analyzer.fill_cycle_playlists()                              # nothing picked from taste this time
    assert not trending.deleted and liked.deleted and created == []


def test_requests_and_untagged_downloads_go_in_neither(monkeypatch):
    import app
    monkeypatch.setattr(app, 'convert_host_path_to_container_path', lambda p: p)
    analyzer, created = _analyzer([], [_track(9, '/music/albums/R/R/r.mp3')])
    analyzer.cycle_downloads = [_download('/music/albums/R/R/r.mp3', None)]
    analyzer.fill_cycle_playlists()
    assert created == []


def test_a_playlist_ends_with_exactly_this_cycles_songs(monkeypatch):
    import app
    monkeypatch.setattr(app, 'convert_host_path_to_container_path', lambda p: p)
    old, new = _track(1, '/music/albums/Old/Old/old.mp3'), _track(2, '/music/albums/A/A/a.mp3')
    playlist = _Playlist([old], title='You Might Like')          # emptying it at the start failed
    analyzer, created = _analyzer([playlist], [new, old])
    analyzer.cycle_downloads = [_download('/music/albums/A/A/a.mp3', 'similar')]
    analyzer.fill_cycle_playlists()
    assert [t.ratingKey for t in playlist.items()] == [2] and not playlist.deleted


def test_the_admin_is_added_to_every_new_request_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot, 'REQUEST_THREADS_FILE', str(tmp_path / 'threads.json'))
    admin = _author(1)
    monkeypatch.setattr(discord_bot, '_admin_member', lambda guild: admin)
    channel = _Channel(42)
    thread = asyncio.run(discord_bot._request_thread(channel, _author(7)))
    assert thread.added == [7, 1]
    own = asyncio.run(discord_bot._request_thread(channel, admin))
    assert own.added == [1]                                  # not twice in their own thread


def test_errors_go_to_the_admins_thread_not_the_channel(monkeypatch):
    channel, thread = _Channel(42), _Thread(9)
    monkeypatch.setattr(discord_bot, '_requests_channel', lambda: asyncio.sleep(0, channel))
    monkeypatch.setattr(discord_bot, '_admin_member', lambda guild: _author(1))
    monkeypatch.setattr(discord_bot, '_request_thread', lambda ch, user: asyncio.sleep(0, thread))
    monkeypatch.setattr(discord_bot, 'bot', SimpleNamespace(guilds=[]))
    asyncio.run(discord_bot.send_error_to_discord("Deezer said no"))
    assert channel.sent == [] and 'Deezer said no' in thread.sent[0][0]


def test_the_requests_channel_keeps_only_the_cycle_status(monkeypatch):
    status = SimpleNamespace(id=5)
    messages = [SimpleNamespace(id=i) for i in (1, 5, 8)]
    removed = []

    class Channel(_Channel):
        async def purge(self, limit=None, check=None, bulk=True, reason=None):
            gone = [m for m in messages if check(m)]
            removed.extend(m.id for m in gone)
            return gone
    channel = Channel(42)
    monkeypatch.setattr(discord_bot, '_requests_channel', lambda: asyncio.sleep(0, channel))
    monkeypatch.setattr(discord_bot, '_CYCLE_STATUS_MESSAGE', status)
    asyncio.run(discord_bot._tidy_requests_channel())
    assert removed == [1, 8]


def test_a_revoked_cached_plex_token_is_dropped_not_retried_forever(tmp_path, monkeypatch):
    # Dead tokens cached for the server owner next to a good one: a dead one
    # is dropped, not retried with 401 after 401.
    import sqlite3
    import plexapi.server
    import app
    db = tmp_path / 'music_ai.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE plex_tokens (token TEXT PRIMARY KEY, username TEXT, obtained_at TIMESTAMP)')
    conn.executemany('INSERT INTO plex_tokens VALUES (?, ?, ?)',
                     [('dead-old', 'owner', '2026-09-01'), ('dead-new', 'owner', '2026-10-01')])
    conn.commit()
    conn.close()

    class Server:
        def __init__(self, url, token):
            if token != 'good':
                raise Exception('(401) unauthorized; http://plex:32400/')
            self.friendlyName, self.myPlexUsername = 'Plex', 'owner'

    monkeypatch.setattr(plexapi.server, 'PlexServer', Server)
    monkeypatch.setattr(app, 'DB_PATH', str(db))
    monkeypatch.setattr(app, 'PLEX_USERNAME', 'owner')
    monkeypatch.setattr(app, 'PLEX_TOKEN', 'good')
    monkeypatch.setenv('APP_IS_ADMIN_USER', 'true')
    analyzer = app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)
    analyzer.connect_plex()
    assert analyzer.plex_token == 'good'
    assert sqlite3.connect(db).execute('SELECT COUNT(*) FROM plex_tokens').fetchone()[0] == 0


def test_the_request_queue_is_processed_by_one_caller_at_a_time(monkeypatch):
    # The cycle's request stage and its background processor must not
    # download the same queued requests at once (duplicate files).
    import threading
    import app
    analyzer = app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)
    started, release, calls = threading.Event(), threading.Event(), []

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return 3

    monkeypatch.setattr(analyzer, '_process_discord_requests', slow)
    worker = threading.Thread(target=lambda: calls.append(analyzer.process_discord_requests()))
    worker.start()
    started.wait(5)
    assert analyzer.process_discord_requests() == 0          # the other caller is on it
    release.set()
    worker.join(5)
    assert calls == [1, 3]


def test_the_scan_is_asked_as_the_server_owner(monkeypatch):
    # Plex refuses a scan asked with a Home user's token (403), so a user's
    # new songs would never be found for "You Might Like".
    import app
    monkeypatch.setattr(app, 'convert_host_path_to_container_path', lambda p: p)
    song = _track(5, '/music/albums/R/R/the box.mp3')
    analyzer, created = _analyzer([], [song])
    scans = []

    def forbidden():
        raise Exception('(403) forbidden')

    analyzer.plex.library.section('Music').update = forbidden          # the user's own connection
    analyzer._music_section_for_scans = lambda: SimpleNamespace(update=lambda: scans.append('owner'),
                                                               reload=lambda: None, refreshing=False)
    analyzer.cycle_downloads = [_download('/music/albums/R/R/the box.mp3', 'similar')]
    analyzer.fill_cycle_playlists()
    assert scans == ['owner'] and created == [('You Might Like', [song])]
