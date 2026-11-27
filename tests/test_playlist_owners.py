"""Whose playlists: a request's playlist is the requester's own; Manual Fix
(and Delete) are the admin's alone, whoever flagged the song."""
import json
import os
from types import SimpleNamespace

import pytest

import discord_bot
import fix_metadata_daemon as fmd
import manual_fix
import playlist_keeper
import plex_auth

TRACKS = [{'str': 'Artist - Song', 'artist': 'Artist', 'title': 'Song', 'file': '/music/albums/Artist/Song.mp3'}]


class Server:
    def __init__(self, token):
        self.token, self.created = token, []
        self.section = SimpleNamespace(title='Music')

    def playlists(self):
        return []

    def createPlaylist(self, title, items):
        self.created.append((title, [i.ratingKey for i in items]))

    def fetchItem(self, key):
        return SimpleNamespace(ratingKey=key)


@pytest.fixture
def plex(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    servers = {}
    monkeypatch.setattr(discord_bot, 'DISCORD_TO_PLEX_MAPPING', {'admin_discord': 'admin', 'friend_discord': 'friend'})
    monkeypatch.setattr(plex_auth, 'admin_token', lambda: 'ADMIN')
    monkeypatch.setattr(plex_auth, 'home_user_token', lambda url, user: f'TOKEN-{user}')
    monkeypatch.setattr(discord_bot, 'PlexServer', lambda url, token: servers.setdefault(token, Server(token)), raising=False)
    monkeypatch.setattr(discord_bot, 'find_music_section', lambda server: server.section)
    made = []
    monkeypatch.setattr(playlist_keeper, 'make_playlist',
                        lambda server, section, title, files, user: made.append((server.token, title, files, user)) or 'made')
    return SimpleNamespace(made=made, servers=servers, data=tmp_path)


def test_a_users_request_playlist_is_made_on_their_own_account(plex):
    assert discord_bot.make_request_playlist('http://plex', 'friend', 'Mix', TRACKS) == 'made'
    assert plex.made == [('TOKEN-friend', 'Mix', ['/music/albums/Artist/Song.mp3'], 'friend')]


def test_the_admins_request_playlist_is_the_admins(plex):
    discord_bot.make_request_playlist('http://plex', 'admin', 'Mix', TRACKS)
    assert plex.made == [('ADMIN', 'Mix', ['/music/albums/Artist/Song.mp3'], 'admin')]


def test_without_the_users_token_the_admin_never_gets_their_playlist(plex, monkeypatch):
    monkeypatch.setattr(plex_auth, 'home_user_token', lambda url, user: None)
    assert discord_bot.make_request_playlist('http://plex', 'friend', 'Mix', TRACKS) is None
    assert plex.made == [] and all(not s.created for s in plex.servers.values())
    with open(os.path.join(plex.data, 'playlist_places.json')) as fh:
        places = json.load(fh)
    # The metadata daemon makes it as the user once it can.
    assert [(p['user'], p['playlist'], p['path'], p['create']) for p in places] == [
        ('friend', 'Mix', '/music/albums/Artist/Song.mp3', True)]


def test_a_users_unresolved_fix_goes_only_to_the_admins_manual_fix(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    friend = Server('friend')
    admin = Server('admin')
    admin.switchUser = lambda name: friend
    flagged = {'rating_key': '7', 'label': 'Artist - Song', 'reason': 'no service knows it', 'source': 'flagged by friend'}
    monkeypatch.setattr(fmd, 'dry_run', lambda: False)
    monkeypatch.setattr(fmd, 'run_fix_metadata', lambda plex, user, section: {
        'fixed': 0, 'skipped': 0, 'failed': 0, 'unresolved': [flagged] if user == 'friend' else []})
    for step in ('keep_playlists', 'delete_playlist_if_due', 'merge_albums_if_due', 'rescan_if_changed', 'lyrics_if_due'):
        monkeypatch.setattr(fmd, step, lambda *a, **k: None)
    monkeypatch.setattr(fmd, 'repair_library_if_due', lambda *a: [])
    monkeypatch.setattr(manual_fix, 'send_pending_notifications', lambda state, *_: None)

    fmd.run_pass(admin, {'admin_discord': 'admin', 'friend_discord': 'friend'}, 'Music', 86400)

    assert admin.created == [('Manual Fix', [7])]
    assert friend.created == []
