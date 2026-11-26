"""Error notices in the cycle status message come and go without the bot
editing that message on every poll."""
import asyncio
import json

import pytest

import discord_bot


@pytest.fixture
def bot(tmp_path, monkeypatch):
    queue = tmp_path / 'discord_notifications.jsonl'
    queue.write_text('')
    edits, clock = [], [1000.0]
    monkeypatch.setattr(discord_bot, '_NOTIF_FILE', str(queue))
    monkeypatch.setattr(discord_bot, '_PENDING_ERROR_MESSAGES', [])
    monkeypatch.setattr(discord_bot.time, 'time', lambda: clock[0])

    async def channel(*a):
        return 'channel'

    async def ensure(ch, embed, allow_move=True):
        edits.append([text for text, _until in discord_bot._PENDING_ERROR_MESSAGES])
    monkeypatch.setattr(discord_bot, '_resolve_configured_channel', channel)
    monkeypatch.setattr(discord_bot, '_ensure_cycle_status_message', ensure)
    monkeypatch.setattr(discord_bot, '_build_cycle_status_embed', lambda: 'embed')

    def notice(message, delete_after=300):
        with open(queue, 'a') as f:
            f.write(json.dumps({'type': 'error', 'message': message, 'delete_after': delete_after}) + '\n')

    def poll():
        return asyncio.run(discord_bot._show_error_notices())
    return notice, poll, edits, clock


def test_an_error_is_shown_once_and_goes_when_its_time_is_up(bot):
    notice, poll, edits, clock = bot
    notice('Billboard chart error')
    assert poll() and edits == [['Billboard chart error']]
    for _ in range(20):                                  # polls with nothing new edit nothing
        clock[0] += 5
        assert not poll()
    clock[0] += 300
    assert poll() and edits[-1] == [] and len(edits) == 2
    assert not poll()


def test_an_error_kept_indefinitely_stays_without_edits(bot):
    notice, poll, edits, clock = bot
    notice('Needs a look', delete_after=0)
    poll()
    clock[0] += 10 ** 6
    assert not poll() and edits == [['Needs a look']]
