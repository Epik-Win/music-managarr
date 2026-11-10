"""Private notices for the admin, delivered by the Discord bot.

Any process (the metadata daemon, a cycle) records a notice here; the bot
posts it in the admin's private requests thread (a changed notice is posted
again, so the admin is notified, and the old message removed) and deletes the
message once the notice is resolved.
Notices live in DATA_DIR/admin_notices.json, so one written while the bot is
down is delivered when it is back.
"""
import fcntl
import json
import os
import time
from contextlib import contextmanager
from typing import Dict


def _path() -> str:
    return os.path.join(os.environ.get('DATA_DIR', '/app/data'), 'admin_notices.json')


@contextmanager
def _notices():
    """The notices, locked against the bot and other writers; saved on exit."""
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    with open(_path() + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(_path()) as fh:
                notices = json.load(fh)
        except (OSError, ValueError):
            notices = {}
        yield notices
        tmp = _path() + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(notices, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, _path())
        fcntl.flock(lock, fcntl.LOCK_UN)


def notify_admin(message: str, notification_id: str) -> bool:
    """Show the admin this message (replacing an earlier one with the same id)."""
    with _notices() as notices:
        notice = notices.setdefault(notification_id, {})
        notice.update(message=message, resolved=False, updated=time.time())
    return True


def resolve_notification(notification_id: str) -> bool:
    """The notice is out of date: its message is removed."""
    with _notices() as notices:
        if notification_id in notices:
            notices[notification_id].update(resolved=True, updated=time.time())
    return True


def pending() -> Dict[str, dict]:
    """{id: notice} still to post, update or remove (for the bot)."""
    with _notices() as notices:
        return {k: dict(v) for k, v in notices.items()
                if v.get('resolved') or v.get('posted_text') != v.get('message')}


def delivered(notification_id: str, message_id, text: str) -> None:
    """The bot posted (or edited) the notice's message."""
    with _notices() as notices:
        if notification_id in notices:
            notices[notification_id].update(message_id=message_id, posted_text=text)


def removed(notification_id: str) -> None:
    """The bot deleted a resolved notice's message."""
    with _notices() as notices:
        notices.pop(notification_id, None)
