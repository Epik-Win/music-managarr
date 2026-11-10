"""The admin turns one-star deletion on and off from Discord, and only the
admin's one star deletes songs."""
import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import runtime_settings


def _blacklist_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE blacklist (id INTEGER PRIMARY KEY, user_id TEXT, title TEXT, artist TEXT, '
                 'reason TEXT, rating REAL, UNIQUE(user_id, title, artist))')
    conn.executemany('INSERT INTO blacklist (user_id, title, artist, reason) VALUES (?, ?, ?, ?)', rows)
    conn.commit()
    conn.close()


def test_the_setting_defaults_to_the_environment_and_the_admins_choice_wins(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.delenv('ONE_STAR_DELETE', raising=False)
    assert runtime_settings.get_bool('ONE_STAR_DELETE', True) is True
    monkeypatch.setenv('ONE_STAR_DELETE', 'false')
    assert runtime_settings.get_bool('ONE_STAR_DELETE', True) is False
    runtime_settings.set_value('ONE_STAR_DELETE', True)
    assert runtime_settings.get_bool('ONE_STAR_DELETE', True) is True
    import one_star_watcher
    assert one_star_watcher.enabled() is True


def _interaction(user_id, admin_id, sent):
    class Response:
        async def send_message(self, text, ephemeral=False):
            sent.append((text, ephemeral))
    guild = SimpleNamespace()
    return SimpleNamespace(user=SimpleNamespace(id=user_id, name='u'), guild=guild, response=Response(),
                           channel=SimpleNamespace(id=42, parent_id=None)), guild


def test_only_the_admin_can_switch_it_and_the_reply_is_private(tmp_path, monkeypatch):
    import discord_bot
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setattr(discord_bot, 'DISCORD_MAIN_CHANNEL', 42)
    monkeypatch.setattr(discord_bot, '_admin_member', lambda guild: SimpleNamespace(id=1))
    off = SimpleNamespace(value='off')
    sent = []
    someone, _ = _interaction(7, 1, sent)
    asyncio.run(discord_bot.one_star_delete.callback(someone, off))
    assert 'Only the admin' in sent[-1][0] and sent[-1][1] is True
    assert runtime_settings.get_bool('ONE_STAR_DELETE', True) is True
    admin, _ = _interaction(1, 1, sent)
    asyncio.run(discord_bot.one_star_delete.callback(admin, off))
    assert '**off**' in sent[-1][0] and sent[-1][1] is True
    assert runtime_settings.get_bool('ONE_STAR_DELETE', True) is False


def _analyzer(tmp_path, monkeypatch, rows):
    import app
    db = tmp_path / 'alice' / 'music_ai.db'
    _blacklist_db(db, rows)
    monkeypatch.setattr(app, 'DB_PATH', str(db))
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    return app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)


def _ratings_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE IF NOT EXISTS track_scores (track_id TEXT PRIMARY KEY, title TEXT, artist TEXT, rating INTEGER)')
    conn.executemany('INSERT INTO track_scores VALUES (?, ?, ?, ?)', rows)
    conn.commit()
    conn.close()


def test_a_song_two_users_rate_one_star_goes_on_the_soon_to_go_list_without_blacklisting(tmp_path, monkeypatch):
    import app
    _ratings_db(tmp_path / 'alice' / 'music_ai.db', [('/k/1', 'Both', 'A', 2), ('/k/2', 'Only me', 'A', 2),
                                                     ('/k/3', 'Fine', 'B', 10)])
    _ratings_db(tmp_path / 'bob' / 'music_ai.db', [('/k/1', 'Both', 'A', 1), ('/k/2', 'Only me', 'A', 8)])
    monkeypatch.setattr(app, 'DB_PATH', str(tmp_path / 'alice' / 'music_ai.db'))
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    analyzer = app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)
    set_to_go = []
    analyzer._set_track_to_go = lambda track_id, title, artist: set_to_go.append(title)
    analyzer.set_disliked_songs_to_go()
    assert set_to_go == ['Both']
    tables = [r[0] for r in sqlite3.connect(tmp_path / 'alice' / 'music_ai.db').execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert 'blacklist' not in tables


def test_names_in_other_scripts_do_not_match_each_other(tmp_path, monkeypatch):
    analyzer = _analyzer(tmp_path, monkeypatch, [('alice', 'детство', 'Rauf & Faik', 'Probation over')])
    assert analyzer.is_blacklisted('Rauf & Faik', 'детство')
    assert not analyzer.is_blacklisted('ния', 'хороша')


def test_a_request_takes_the_song_off_every_users_blacklist(tmp_path, monkeypatch):
    import library_review
    for user in ('alice', 'bob'):
        _blacklist_db(tmp_path / user / 'music_ai.db', [(user, 'The Fox', 'Ylvis', 'Admin rated 1 star'),
                                                        (user, 'Other', 'Ylvis', 'Admin rated 1 star')])
    monkeypatch.setattr(library_review, 'DATA_DIR', str(tmp_path))
    assert library_review.unblacklist('the fox', 'YLVIS') == 2
    left = sqlite3.connect(tmp_path / 'bob' / 'music_ai.db').execute('SELECT title FROM blacklist').fetchall()
    assert left == [('Other',)]
