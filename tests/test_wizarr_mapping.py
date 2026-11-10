import re
import asyncio
from pathlib import Path
import json
import os
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

# Avoid requiring optional runtime dependencies during fast unit tests.
if 'plexapi' not in sys.modules:
    plexapi = types.ModuleType('plexapi')
    sys.modules['plexapi'] = plexapi
else:
    plexapi = sys.modules['plexapi']

plexapi_server = types.ModuleType('plexapi.server')
plexapi_server.PlexServer = type('PlexServer', (), {})
sys.modules['plexapi.server'] = plexapi_server
setattr(plexapi, 'server', plexapi_server)

if 'numpy' not in sys.modules:
    numpy = types.ModuleType('numpy')
    numpy.array = lambda *args, **kwargs: args[0]
    numpy.mean = lambda x: sum(x) / len(x) if x else 0
    sys.modules['numpy'] = numpy

if 'flask' not in sys.modules:
    flask_stub = types.ModuleType('flask')

    class _FakeFlask:
        def __init__(self, *args, **kwargs):
            self.secret_key = ''

        def route(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

    flask_stub.Flask = _FakeFlask
    flask_stub.request = types.SimpleNamespace(
        args={},
        form={},
        method='GET',
    )
    flask_stub.jsonify = lambda *args, **kwargs: {}
    flask_stub.redirect = lambda *args, **kwargs: ''
    flask_stub.url_for = lambda *args, **kwargs: ''
    flask_stub.session = {}
    flask_stub.render_template_string = lambda *args, **kwargs: ''
    sys.modules['flask'] = flask_stub

for module_name in ['sklearn', 'sklearn.preprocessing', 'sklearn.preprocessing.StandardScaler', 'sklearn.neighbors', 'sklearn.neighbors.NearestNeighbors']:
    if module_name not in sys.modules:
        sys.modules[module_name] = types.ModuleType(module_name)

# Provide minimal classes for sklearn dependencies
if 'sklearn.preprocessing' not in sys.modules:
    sys.modules['sklearn.preprocessing'] = types.ModuleType('sklearn.preprocessing')
if 'sklearn.neighbors' not in sys.modules:
    sys.modules['sklearn.neighbors'] = types.ModuleType('sklearn.neighbors')
setattr(sys.modules['sklearn.preprocessing'], 'StandardScaler', type('StandardScaler', (), {'fit_transform': lambda self, x: x}))
setattr(sys.modules['sklearn.neighbors'], 'NearestNeighbors', type('NearestNeighbors', (), {'fit': lambda self, x: None, 'kneighbors': lambda self, x, n_neighbors=1: ([], [])}))
import discord_bot
import entrypoint
import arl_server
import downloader_manager
import downloader



def test_load_wizarr_mapping_file_orders_admin_first(tmp_path, monkeypatch):
    # The file is {discord: plex}; the admin override moves that entry first.
    mapping_file = tmp_path / "plex_invite_targets.json"
    mapping_file.write_text(json.dumps({
        "bob_d": "bob",
        "alice_d": "alice",
        "carol.": "carol",
        "dave_d": "dave",
    }))

    monkeypatch.setenv('WIZARR_MAPPING_FILE', str(mapping_file))
    monkeypatch.setenv('DISCORD_TO_PLEX_MAPPING_ADMIN', 'alice_d:alice')

    manager = entrypoint.MultiUserManager()
    loaded = manager._load_wizarr_mapping_file()

    assert list(loaded.items()) == [
        ('alice_d', 'alice'),
        ('bob_d', 'bob'),
        ('carol.', 'carol'),
        ('dave_d', 'dave'),
    ]


def test_discord_bot_load_mapping_file_rejects_unknown_admin(tmp_path, monkeypatch):
    mapping_file = tmp_path / "plex_invite_targets.json"
    mapping_file.write_text(json.dumps({"alice_d": "alice"}))
    monkeypatch.setenv('DISCORD_TO_PLEX_MAPPING_ADMIN', 'nobody:nobody')
    assert discord_bot._load_mapping_from_file(str(mapping_file)) is None


def test_discord_bot_load_mapping_file_lowercases_discord_handles(tmp_path, monkeypatch):
    mapping_file = tmp_path / "plex_invite_targets.json"
    mapping_file.write_text(json.dumps({"Alice_D": "alice"}))
    monkeypatch.setenv('DISCORD_TO_PLEX_MAPPING_ADMIN', '')
    assert discord_bot._load_mapping_from_file(str(mapping_file)) == {'alice_d': 'alice'}


def test_discord_bot_load_mapping_file_accepts_explicit_discord_to_plex_admin_override(tmp_path, monkeypatch):
    mapping_file = tmp_path / "plex_invite_targets.json"
    mapping_file.write_text(json.dumps({
        "bob_d": "bob",
        "alice_d": "alice",
        "carol.": "carol",
        "dave_d": "dave",
    }))

    monkeypatch.setenv('DISCORD_TO_PLEX_MAPPING_ADMIN', 'alice_d:alice')
    loaded = discord_bot._load_mapping_from_file(str(mapping_file))

    assert loaded == {
        'bob_d': 'bob',
        'alice_d': 'alice',
        'carol.': 'carol',
        'dave_d': 'dave',
    }


def test_discord_bot_load_mapping_file_accepts_single_value_admin_override(tmp_path, monkeypatch):
    mapping_file = tmp_path / "plex_invite_targets.json"
    mapping_file.write_text(json.dumps({
        "bob_d": "bob",
        "alice_d": "alice",
        "carol.": "carol",
        "dave_d": "dave",
    }))

    monkeypatch.setenv('DISCORD_TO_PLEX_MAPPING_ADMIN', 'alice_d')
    loaded = discord_bot._load_mapping_from_file(str(mapping_file))

    assert loaded == {
        'bob_d': 'bob',
        'alice_d': 'alice',
        'carol.': 'carol',
        'dave_d': 'dave',
    }


def test_build_cycle_status_embed_prefers_plex_usernames(monkeypatch):
    monkeypatch.setattr(discord_bot, 'DISCORD_TO_PLEX_MAPPING', {
        'alice': 'alice_d',
        'dave_d': 'dave',
    })
    monkeypatch.setattr(discord_bot, '_CYCLE_USER_STATE', {
        'alice': {
            'running': True,
            'phase': 'Analyzing',
            'detail': 'Testing cycle status',
            'stats': {'downloads': {'total': 1}},
            'sections': ['Music'],
        },
        'dave_d': {
            'running': False,
            'skipped': True,
            'detail': 'Not enough activity',
            'complete_info': {'next_cycle_hours': 12.0},
        },
    })

    embed = discord_bot._build_cycle_status_embed()
    description = embed.description or ''

    assert '**alice_d**' in description
    assert '**dave**' in description
    assert 'Skipped due to lack of activity:' in description
    assert not re.search(r'\balice\b', description)


def test_cycle_user_keys_normalize_to_plex_usernames(monkeypatch):
    monkeypatch.setattr(discord_bot, 'DISCORD_TO_PLEX_MAPPING', {
        'alice_d': 'alice',
        'bob_d': 'bob',
        'dave_d': 'dave',
    })

    assert discord_bot._canonical_cycle_user('alice_d') == 'alice'
    assert discord_bot._canonical_cycle_user('bob_d') == 'bob'
    assert discord_bot._canonical_cycle_user('alice') == 'alice'
    assert discord_bot._canonical_cycle_user('dave') == 'dave'


def test_ensure_cycle_status_message_deletes_duplicate_status_embeds(monkeypatch):
    class FakeFooter:
        def __init__(self, text):
            self.text = text

    class FakeEmbed:
        def __init__(self, title, footer_text=None):
            self.title = title
            self.footer = FakeFooter(footer_text) if footer_text else None

    class FakeMessage:
        def __init__(self, title='⚠️ Download Cycle Status', footer_text='Music AI | Download Cycle Status'):
            self.embeds = [FakeEmbed(title, footer_text)]
            self.deleted = False

        async def delete(self):
            self.deleted = True

    class FakeChannel:
        def __init__(self):
            self.old_messages = [FakeMessage(), FakeMessage()]
            self.sent_messages = []

        async def history(self, limit=25):
            for message in self.old_messages:
                yield message

        async def send(self, **kwargs):
            msg = FakeMessage()
            self.sent_messages.append(msg)
            return msg

    channel = FakeChannel()
    monkeypatch.setattr(discord_bot, '_CYCLE_STATUS_MESSAGE', None)

    async def run_check():
        embed = discord_bot._build_cycle_status_embed()
        await discord_bot._ensure_cycle_status_message(channel, embed)
        assert len([m for m in channel.old_messages if m.deleted]) == 2
        assert len(channel.sent_messages) == 1

    asyncio.run(run_check())


def test_ensure_cycle_status_message_edits_and_pins_when_still_at_bottom(monkeypatch):
    # When the shared status message is still the most recent message in the
    # channel, it should be updated in place (not replaced) and pinned so it
    # stays easy to find.
    class FakeFooter:
        def __init__(self, text):
            self.text = text

    class FakeEmbed:
        def __init__(self, title, footer_text=None):
            self.title = title
            self.footer = FakeFooter(footer_text) if footer_text else None

    class FakeMessage:
        def __init__(self, msg_id, title='⚠️ Download Cycle Status', footer_text='Music AI | Download Cycle Status'):
            self.id = msg_id
            self.embeds = [FakeEmbed(title, footer_text)]
            self.deleted = False
            self.pinned = False
            self.edited_embed = None

        async def edit(self, embed=None):
            self.edited_embed = embed

        async def pin(self, reason=None):
            self.pinned = True

        async def unpin(self, reason=None):
            self.pinned = False

        async def delete(self):
            self.deleted = True

    class FakeChannel:
        def __init__(self, existing_message):
            self.existing_message = existing_message
            self.last_message_id = existing_message.id  # status message IS the latest
            self.sent_messages = []

        async def history(self, limit=25):
            yield self.existing_message

        async def send(self, **kwargs):
            msg = FakeMessage(msg_id=999)
            self.sent_messages.append(msg)
            return msg

    status_message = FakeMessage(msg_id=42)
    channel = FakeChannel(status_message)
    monkeypatch.setattr(discord_bot, '_CYCLE_STATUS_MESSAGE', status_message)

    async def run_check():
        embed = discord_bot._build_cycle_status_embed()
        await discord_bot._ensure_cycle_status_message(channel, embed)
        assert status_message.edited_embed is embed
        assert status_message.pinned is True
        assert status_message.deleted is False
        assert channel.sent_messages == []

    asyncio.run(run_check())


def test_ensure_cycle_status_message_moves_to_bottom_when_superseded(monkeypatch):
    # If newer channel activity has pushed the status message away from the
    # bottom, it should be unpinned/deleted and a fresh copy sent (and
    # pinned) at the bottom instead of being edited where it now sits.
    class FakeFooter:
        def __init__(self, text):
            self.text = text

    class FakeEmbed:
        def __init__(self, title, footer_text=None):
            self.title = title
            self.footer = FakeFooter(footer_text) if footer_text else None

    class FakeMessage:
        def __init__(self, msg_id, title='⚠️ Download Cycle Status', footer_text='Music AI | Download Cycle Status'):
            self.id = msg_id
            self.embeds = [FakeEmbed(title, footer_text)]
            self.deleted = False
            self.pinned = True  # already pinned from a previous update
            self.edited_embed = None

        async def edit(self, embed=None):
            self.edited_embed = embed

        async def pin(self, reason=None):
            self.pinned = True

        async def unpin(self, reason=None):
            self.pinned = False

        async def delete(self):
            self.deleted = True

    class FakeChannel:
        """History is newest-first, like discord.py's channel.history()."""
        def __init__(self, messages):
            self.messages = messages
            # Discord leaves this pointing at deleted messages (e.g. a removed
            # "pinned a message" notice), so it must not drive the decision.
            self.last_message_id = 12345
            self.sent_messages = []

        async def history(self, limit=25):
            for message in self.messages[:limit]:
                yield message

        async def send(self, **kwargs):
            msg = FakeMessage(msg_id=999)
            self.sent_messages.append(msg)
            self.messages.insert(0, msg)
            return msg

    class FakeUserMessage:
        def __init__(self, msg_id):
            self.id = msg_id
            self.embeds = []

    # A member posted after the status message: it must move to the bottom.
    status_message = FakeMessage(msg_id=42)
    channel = FakeChannel([FakeUserMessage(43), status_message])
    monkeypatch.setattr(discord_bot, '_CYCLE_STATUS_MESSAGE', status_message)

    async def superseded():
        embed = discord_bot._build_cycle_status_embed()
        await discord_bot._ensure_cycle_status_message(channel, embed)
        assert status_message.deleted is True
        assert status_message.pinned is False
        assert len(channel.sent_messages) == 1
        assert channel.sent_messages[0].pinned is True

    asyncio.run(superseded())

    # Still the newest real message (a restart, or a stale last_message_id):
    # edit in place, post nothing.
    status_message = FakeMessage(msg_id=42)
    channel = FakeChannel([status_message])
    monkeypatch.setattr(discord_bot, '_CYCLE_STATUS_MESSAGE', None)

    async def still_bottom():
        embed = discord_bot._build_cycle_status_embed()
        await discord_bot._ensure_cycle_status_message(channel, embed)
        assert status_message.deleted is False
        assert status_message.edited_embed is embed
        assert channel.sent_messages == []

    asyncio.run(still_bottom())


def test_build_cycle_status_embed_stays_yellow_until_all_users_have_a_result(monkeypatch):
    monkeypatch.setattr(discord_bot, 'DISCORD_TO_PLEX_MAPPING', {
        'alice_d': 'alice',
        'bob_d': 'bob',
        'carol.': 'carol',
    })
    monkeypatch.setattr(discord_bot, '_CYCLE_USER_STATE', {
        'alice': {
            'running': False,
            'complete_info': {'total_downloaded': 2, 'next_cycle_hours': 168.0},
            'skipped': False,
        },
    })
    monkeypatch.setattr(discord_bot, '_get_known_cycle_users', lambda: ['alice', 'bob', 'carol'])

    embed = discord_bot._build_cycle_status_embed()
    assert embed.title == '⚠️ Download Cycle Status'
    assert embed.color == discord_bot.discord.Color.gold()
    # Wording must not be readable as "no Plex listening activity" — it's
    # specifically about the download cycle not having processed them yet.
    assert "Waiting for this round's download cycle to run:" in (embed.description or '')
    assert 'plex' not in (embed.description or '').lower()


def test_build_cycle_status_embed_is_green_when_users_are_completed_or_skipped(monkeypatch):
    monkeypatch.setattr(discord_bot, 'DISCORD_TO_PLEX_MAPPING', {
        'alice_d': 'alice',
        'bob_d': 'bob',
        'carol.': 'carol',
    })
    monkeypatch.setattr(discord_bot, '_CYCLE_USER_STATE', {
        'alice': {
            'running': False,
            'complete_info': {'total_downloaded': 2, 'next_cycle_hours': 168.0},
            'skipped': False,
        },
        'bob': {
            'running': False,
            'complete_info': {'total_downloaded': 0, 'next_cycle_hours': 168.0},
            'skipped': True,
        },
        'carol': {
            'running': False,
            'complete_info': {'total_downloaded': 0, 'next_cycle_hours': 168.0},
            'skipped': True,
        },
    })
    monkeypatch.setattr(discord_bot, '_get_known_cycle_users', lambda: [
        'alice', 'bob', 'carol'
    ])

    embed = discord_bot._build_cycle_status_embed()
    assert embed.title == '✅ Download Cycle Complete'
    assert embed.color == discord_bot.discord.Color.green()


def test_cycle_user_state_survives_a_restart_via_persisted_file(monkeypatch, tmp_path):
    # A discord_bot restart must not lose which users already completed or
    # were skipped in the current cycle window.
    persist_file = str(tmp_path / 'cycle_status_state.json')
    monkeypatch.setattr(discord_bot, '_CYCLE_STATE_PERSIST_FILE', persist_file)

    monkeypatch.setattr(discord_bot, '_CYCLE_USER_STATE', {
        'alice': {
            'running': False,
            'complete_info': {'total_downloaded': 5, 'next_cycle_hours': 168.0},
            'skipped': False,
        },
        'bob': {
            'running': True,  # a restart shouldn't leave this permanently stuck "running"
            'sections': ['Music'],
            'complete_info': None,
        },
    })
    discord_bot._save_cycle_user_state()

    # Simulate the process restarting: state resets to empty in memory.
    monkeypatch.setattr(discord_bot, '_CYCLE_USER_STATE', {})
    discord_bot._load_cycle_user_state()

    restored = discord_bot._CYCLE_USER_STATE
    assert restored['alice']['complete_info']['total_downloaded'] == 5
    assert restored['alice']['skipped'] is False
    # 'running' is cleared on load; check_arl_and_notifications() re-derives
    # it from cycle_running_*.json within 5s if the cycle is genuinely ongoing.
    assert restored['bob']['running'] is False


def test_build_cycle_status_embed_displays_no_downloads_for_zero_total(monkeypatch):
    monkeypatch.setattr(discord_bot, 'DISCORD_TO_PLEX_MAPPING', {
        'alice_d': 'alice_d',
    })
    monkeypatch.setattr(discord_bot, '_CYCLE_USER_STATE', {
        'alice_d': {
            'running': False,
            'complete_info': {'total_downloaded': 0, 'next_cycle_hours': 12.0},
        },
    })

    embed = discord_bot._build_cycle_status_embed()
    description = embed.description or ''

    assert 'No downloads performed' in description
    # The next cycle is shown once, from the persisted schedule; a per-user
    # hour count goes stale whenever SCAN_INTERVAL changes.
    assert 'next cycle in' not in description


def test_resolve_admin_discord_handle_returns_admin_member(monkeypatch):
    monkeypatch.setattr(discord_bot, 'DISCORD_TO_PLEX_MAPPING_ADMIN', 'alice:alice_d')
    monkeypatch.setattr(discord_bot, 'DISCORD_TO_PLEX_MAPPING', {
        'alice_d': 'alice',
        'bob_d': 'bob',
    })

    assert discord_bot._resolve_admin_discord_handle() == 'alice_d'


def test_send_private_admin_message_uses_dm(monkeypatch):
    class FakeMember:
        def __init__(self, name):
            self.name = name
            self.display_name = name
            self.sent_messages = []

        async def send(self, *args, **kwargs):
            self.sent_messages.append((args, kwargs))
            return None

    member = FakeMember('alice_d')

    class FakeGuild:
        def __init__(self, member):
            self.members = [member]

        def get_member_named(self, name):
            if self.members[0].name == name:
                return self.members[0]
            return None

    class FakeBot:
        def __init__(self, guild):
            self.guilds = [guild]

    monkeypatch.setattr(discord_bot, 'DISCORD_TO_PLEX_MAPPING_ADMIN', 'alice:alice_d')
    monkeypatch.setattr(discord_bot, 'DISCORD_TO_PLEX_MAPPING', {'alice_d': 'alice'})
    monkeypatch.setattr(discord_bot, 'bot', FakeBot(FakeGuild(member)))

    async def run_check():
        sent = await discord_bot._send_private_admin_message('hello world')
        assert sent is True
        assert member.sent_messages

    asyncio.run(run_check())


def test_process_discord_requests_handles_music_request_types(tmp_path, monkeypatch):
    import app

    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    req_file = data_dir / 'discord_requests.json'
    req_file.write_text(json.dumps({
        'alice': [{
            'artist': 'VEGEDREAM',
            'title': 'RAMENEZ LA COUPE A LA MAISON',
            'type': 'music_request_youtube',
            'status': 'pending',
        }]
    }))

    # Set DATA_DIR environment variable so process_discord_requests finds the file
    monkeypatch.setenv('DATA_DIR', str(data_dir))
    monkeypatch.setattr(app, '__file__', str(tmp_path / 'app.py'))
    monkeypatch.setattr(app, 'check_if_track_exists', lambda *args, **kwargs: (False, None, None))

    analyzer = object.__new__(app.MusicAIAnalyzer)
    analyzer.download_recommended_music = lambda *args, **kwargs: [{'artist': 'VEGEDREAM', 'title': 'RAMENEZ LA COUPE A LA MAISON'}]

    processed = analyzer.process_discord_requests()

    assert processed == 1
    with open(req_file, 'r') as fh:
        updated = json.load(fh)
    assert updated['alice'][0]['status'] == 'completed'


def test_try_immediate_download_uses_per_user_target_dir(monkeypatch):
    captured = {}

    class FakeLock:
        async def async_acquire(self, timeout=30):
            return True

        def release(self):
            return None

    class FakeDownloader:
        def download_track(self, *args, **kwargs):
            captured['args'] = args
            return {'success': True, 'download_tool': 'test', 'metadata': {'source': 'direct'}}

    monkeypatch.setattr(discord_bot, 'PROCESS_ROLE', 'discord_bot')
    monkeypatch.setattr(discord_bot, 'CYCLE_RUNNING', False)
    monkeypatch.setattr(discord_bot, '_any_cycle_running_file', lambda: False)
    monkeypatch.setattr(discord_bot, 'get_downloader_lock', lambda: FakeLock())
    monkeypatch.setattr(discord_bot, 'get_downloader', lambda: FakeDownloader())
    monkeypatch.setattr(discord_bot, 'refresh_plex_library', lambda *args, **kwargs: None)
    monkeypatch.setattr(discord_bot, 'infer_plex_user_index', lambda user: 3)
    monkeypatch.setattr(discord_bot, 'get_user_albums_dir', lambda user: f'/music/users/{user}/albums')

    result = asyncio.run(discord_bot.try_immediate_download(
        'alice', 'VEGEDREAM', 'RAMENEZ LA COUPE A LA MAISON', 'Album', 'https://example.com/track', 'youtube'
    ))

    assert result[0] is True
    assert captured['args'][5] == 'alice'
    assert captured['args'][6] == '/music/users/alice/albums'
    assert captured['args'][7] == 3


def test_startup_cycles_are_disabled_when_flag_is_false(monkeypatch):
    monkeypatch.setenv('CYCLE_ON_STARTUP', 'false')
    manager = entrypoint.MultiUserManager()
    assert manager.startup_cycles_enabled() is False


def test_write_arl_persists_project_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / '.env'
    env_file.write_text('DEEZER_ARL=oldtoken\nOTHER=value\n')

    monkeypatch.setattr('arl_server.PROJECT_ENV_FILE', env_file)
    monkeypatch.setattr('arl_server.ARL_CONFIG_FILE', tmp_path / 'deemix.arl')
    monkeypatch.setattr('arl_server.ARL_REFRESH_NONCE', str(tmp_path / 'arl_refresh.nonce'))
    monkeypatch.setattr('arl_server.DATA_DIR', str(tmp_path))

    ok = arl_server.write_arl('newtoken_123456')

    assert ok is True
    assert (tmp_path / 'deemix.arl').read_text() == 'newtoken_123456'
    assert 'DEEZER_ARL=newtoken_123456' in env_file.read_text()
    assert os.environ.get('DEEZER_ARL') == 'newtoken_123456'


def test_download_track_halts_when_arl_is_stale(monkeypatch):
    dl = downloader.Downloader(analyzer=None)

    monkeypatch.setattr(dl, '_ensure_vpn', lambda: None)
    monkeypatch.setattr(dl, '_resolve_effective_download_metadata', lambda **kwargs: {'artist': 'VEGEDREAM', 'title': 'RAMENEZ LA COUPE A LA MAISON', 'album': 'Ramenez la coupe à la maison'})
    monkeypatch.setattr(dl, 'get_metadata_from_sources', lambda *args, **kwargs: {'album': 'Ramenez la coupe à la maison'})
    monkeypatch.setattr(dl, '_find_existing_track', lambda *args, **kwargs: None)
    monkeypatch.setattr(dl, '_is_track_blacklisted', lambda *args, **kwargs: False)
    monkeypatch.setattr(dl, 'check_download_tools', lambda: {'deemix': True, 'soundcloud': True, 'yt-dlp': True})
    monkeypatch.setattr(downloader_manager, 'is_arl_stale', lambda: True)
    monkeypatch.setattr(downloader_manager, 'wait_for_arl_refresh', lambda *args, **kwargs: False)

    attempted = {'soundcloud': 0, 'yt_dlp': 0}

    def mark_soundcloud(*args, **kwargs):
        attempted['soundcloud'] += 1
        return False, None

    def mark_yt_dlp(*args, **kwargs):
        attempted['yt_dlp'] += 1
        return False, None

    monkeypatch.setattr(dl, '_download_with_soundcloud', mark_soundcloud)
    monkeypatch.setattr(dl, '_download_with_yt_dlp', mark_yt_dlp)

    result = dl.download_track(
        artist='VEGEDREAM',
        title='RAMENEZ LA COUPE A LA MAISON',
        album='Ramenez la coupe à la maison',
        target_dir='/tmp/music',
        plex_user='alice',
        request_type='music_request_youtube',
    )

    assert result['success'] is False
    assert 'ARL' in (result['error'] or '')
    assert attempted == {'soundcloud': 0, 'yt_dlp': 0}


def test_arl_health_snapshot_reports_sync_state(tmp_path, monkeypatch):
    project_env = tmp_path / '.env'
    project_env.write_text('DEEZER_ARL=project-token\nOTHER=value\n')

    deemix_dir = tmp_path / '.config' / 'deemix'
    deemix_dir.mkdir(parents=True)
    deemix_file = deemix_dir / '.arl'
    deemix_file.write_text('deemix-token')

    monkeypatch.setenv('DEEZER_ARL', 'project-token')
    monkeypatch.setenv('PROJECT_ENV_FILE', str(project_env))
    monkeypatch.setattr('downloader_manager.Path.home', lambda: tmp_path)
    monkeypatch.setattr('downloader_manager._DATA_DIR', str(tmp_path))
    monkeypatch.setattr('downloader_manager._ARL_STALE_FLAG', str(tmp_path / 'arl_stale.flag'))

    snapshot = downloader_manager.get_arl_health_snapshot(str(project_env))

    assert snapshot['env_matches_project'] is True
    assert snapshot['env_matches_file'] is False
    assert snapshot['project_env_set'] is True
    assert snapshot['file_set'] is True
    assert snapshot['stale'] is False


def test_should_skip_cycle_when_insufficient_activity():
    from app import _should_skip_cycle

    skip, reason = _should_skip_cycle([], 0.0)
    assert skip is True
    assert reason == 'No Plex tracks available'

    # Zero user-specific activity (no plays, skips, personal ratings, or recent listening)
    sample_tracks = [{'features': {'view_count': 0, 'rating': 0, 'skip_count': 0, 'last_played': 0}} for _ in range(100)]
    skip, reason = _should_skip_cycle(sample_tracks, 0.4)
    assert skip is True
    assert 'No Plex listening activity' in reason

    skip, reason = _should_skip_cycle(sample_tracks, 0.6)
    assert skip is True
    assert 'No Plex listening activity' in reason

    # Album rating alone is NOT user-specific activity, so this should SKIP
    album_rated_tracks = [
        {'features': {'view_count': 0, 'rating': 0, 'skip_count': 0, 'last_played': 0, 'album_rating': 5}},
        {'features': {'view_count': 0, 'rating': 0, 'skip_count': 0, 'last_played': 0, 'album_rating': 4}},
    ]
    skip, reason = _should_skip_cycle(album_rated_tracks, 0.6)
    assert skip is True
    assert 'No Plex listening activity' in reason

    # Recent Plex listening activity means DO NOT SKIP.
    import time
    active_tracks = [
        {'features': {'view_count': 1, 'rating': 0, 'skip_count': 0, 'last_played': int(time.time()), 'album_rating': 1}},
        {'features': {'view_count': 0, 'rating': 0, 'skip_count': 0, 'last_played': 0, 'album_rating': None}},
    ]
    skip, reason = _should_skip_cycle(active_tracks, 0.6)
    assert skip is False
    assert reason == ''


def test_main_cycle_once_exits_immediately_for_skipped_user(monkeypatch):
    import app

    removed = []

    class DummyAnalyzer:
        def __init__(self):
            pass

        def analyze_all_tracks(self):
            return []

        def remove_cycle_playlists(self, kinds=None):
            removed.append(True)

        def calculate_preference_confidence(self, analyzed_tracks):
            return 0.0

    def fail_sleep(_seconds):
        raise AssertionError('cycle-once skipped user should not sleep for SCAN_INTERVAL')

    monkeypatch.setattr(app, 'MusicAIAnalyzer', DummyAnalyzer)
    monkeypatch.setattr(app, '_should_skip_cycle', lambda *args, **kwargs: (True, 'No Plex listening activity'))
    monkeypatch.setattr(app, 'discord_bot', None)
    monkeypatch.setattr(app.time, 'sleep', fail_sleep)

    result = app.main(run_once=True)
    assert result == 0
    assert removed == [True]                 # no cycle: no stale cycle playlists


def test_get_user_environment_uses_plex_name_for_app_user_label():
    manager = entrypoint.MultiUserManager()
    manager.user_paths = ['/tmp/music-user']
    manager.user_names = ['alice']
    manager.plex_usernames = ['alice']
    manager.discord_usernames = ['alice_d']
    manager.plex_sections = ['Music']
    manager.admin_plex_username = 'alice_d'

    env = manager.get_user_environment(0)

    assert env['APP_USER_NAME'] == 'alice'
    assert env['PLEX_USERNAME'] == 'alice'
    assert 'DISCORD_USERNAME' not in env


def test_build_worker_plan_uses_admin_first_order(tmp_path, monkeypatch):
    mapping_file = tmp_path / "plex_invite_targets.json"
    mapping_file.write_text(json.dumps({
        "bob": "bob_d",
        "alice": "alice_d",
        "carol": "carol.",
    }))

    monkeypatch.setenv('WIZARR_MAPPING_FILE', str(mapping_file))
    monkeypatch.setenv('DISCORD_TO_PLEX_MAPPING_ADMIN', 'alice:alice_d')

    manager = entrypoint.MultiUserManager()
    plan = manager.build_worker_plan()

    assert [item['plex_username'] for item in plan] == ['alice_d', 'bob_d', 'carol.']
    assert [item['discord_username'] for item in plan] == ['alice', 'bob', 'carol']


def test_initial_schedule_survives_restart_and_resumes(tmp_path, monkeypatch):
    import cycle_schedule
    import entrypoint
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('CYCLE_ON_STARTUP', 'false')
    manager = entrypoint.MultiUserManager()

    # First start: the due time is fixed on disk, not just in memory.
    due_at, done = manager._initial_schedule(3600)
    assert done == [] and due_at > cycle_schedule.now()
    # Restart before it is due: same due time, no new cycle.
    assert manager._initial_schedule(3600) == (due_at, [])

    # Restart in the middle of a pass: resume with the remaining users.
    cycle_schedule.begin_pass()
    cycle_schedule.mark_user_done('alice')
    resume_at, done = manager._initial_schedule(3600)
    assert done == ['alice'] and resume_at <= cycle_schedule.now()

    # After the pass finishes, the next one is a full interval away.
    cycle_schedule.finish_pass(3600)
    due_at, done = manager._initial_schedule(3600)
    assert done == [] and due_at > cycle_schedule.now()


def test_initial_schedule_runs_immediately_when_startup_cycles_enabled(tmp_path, monkeypatch):
    import cycle_schedule
    import entrypoint
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('CYCLE_ON_STARTUP', 'true')
    manager = entrypoint.MultiUserManager()
    due_at, done = manager._initial_schedule(3600)
    assert done == [] and due_at <= cycle_schedule.now()


def test_unchanged_cycle_status_message_is_not_edited(monkeypatch):
    # A restart with no new cycle activity must leave the pinned message
    # completely untouched: editing it only marks it "(edited)" in Discord.
    import asyncio
    import discord_bot

    class FakeFooter:
        def __init__(self, text):
            self.text = text

    class FakeEmbed:
        def __init__(self, title, description, colour, footer_text):
            self.title = title
            self.description = description
            self.colour = colour
            self.footer = FakeFooter(footer_text)

    class FakeMessage:
        def __init__(self, embed):
            self.id = 7
            self.embeds = [embed]
            self.pinned = True
            self.edits = 0
            self.deleted = False

        async def edit(self, embed=None):
            self.edits += 1

        async def pin(self, reason=None):
            self.pinned = True

        async def delete(self):
            self.deleted = True

    class FakeChannel:
        def __init__(self, message):
            self.messages = [message]
            self.sent = []

        async def history(self, limit=25):
            for m in self.messages[:limit]:
                yield m

        async def send(self, **kwargs):
            raise AssertionError("must not post a new message")

    live = discord_bot._build_cycle_status_embed()
    same = FakeEmbed(live.title, live.description, live.colour, live.footer.text)
    message = FakeMessage(same)
    channel = FakeChannel(message)
    monkeypatch.setattr(discord_bot, '_CYCLE_STATUS_MESSAGE', message)

    assert discord_bot._cycle_status_embed_changed(message, live) is False
    asyncio.run(discord_bot._ensure_cycle_status_message(channel, live))
    assert message.edits == 0 and message.deleted is False and channel.sent == []

    # Same, with a member's request sitting below it: still untouched, because
    # moving it to the bottom would notify the channel for no new information.
    class Other:
        id = 99
        embeds = []
    channel.messages.insert(0, Other())
    asyncio.run(discord_bot._ensure_cycle_status_message(channel, live))
    assert message.edits == 0 and message.deleted is False and channel.sent == []
    channel.messages.remove(channel.messages[0])

    # Real cycle activity still gets written through.
    message.embeds = [FakeEmbed('⏳ Download Cycle In Progress', 'something older', live.colour, live.footer.text)]
    assert discord_bot._cycle_status_embed_changed(message, live) is True
    asyncio.run(discord_bot._ensure_cycle_status_message(channel, live))
    assert message.edits == 1


def test_restart_corrects_status_in_place_and_never_reposts(monkeypatch):
    # A request sits below the status message and the content is stale.
    # On restart the bot may edit it (silent), but must not delete it and
    # post a new one — that would look to the channel like a new cycle.
    import asyncio
    import discord_bot

    class Msg:
        def __init__(self, mid, title='✅ Download Cycle Complete', desc='old text'):
            self.id = mid
            self.embeds = [type('E', (), {'title': title, 'description': desc, 'colour': None,
                                          'footer': type('F', (), {'text': 'Music AI | Download Cycle Status'})()})()]
            self.pinned = True
            self.edits = 0
            self.deleted = False

        async def edit(self, embed=None):
            self.edits += 1

        async def pin(self, reason=None):
            pass

        async def delete(self):
            self.deleted = True

    class Chan:
        def __init__(self, msgs):
            self.msgs = msgs
            self.sent = []

        async def history(self, limit=25):
            for m in self.msgs[:limit]:
                yield m

        async def send(self, **kw):
            self.sent.append(kw)
            raise AssertionError("must not post on restart")

    status = Msg(1)
    request = type('R', (), {'id': 2, 'embeds': []})()
    channel = Chan([request, status])
    monkeypatch.setattr(discord_bot, '_CYCLE_STATUS_MESSAGE', status)

    asyncio.run(discord_bot._ensure_cycle_status_message(
        channel, discord_bot._build_cycle_status_embed(), allow_move=False))
    assert status.edits == 1 and status.deleted is False and channel.sent == []
