"""discord_requests.json: every Discord request and what became of it.

The bot adds requests and records how its own attempt went; a cycle, in another
process, works through those still pending. Each change is made under a lock,
to the file as it is at that moment, so neither undoes the other's work.
"""
import contextlib
import fcntl
import json
import os


def load(path: str) -> dict:
    """The history on disk. A file that can't be parsed raises: the caller must
    not go on to save over every request on record."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def editing(path: str):
    """The history to change, saved when the block ends (not if it raises)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(f'{path}.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = load(path)
        yield history
        tmp = f'{path}.tmp'
        with open(tmp, 'w') as fh:
            json.dump(history, fh, indent=2, default=str)
        os.replace(tmp, path)


def _key(entry: dict) -> tuple:
    return entry.get('timestamp'), entry.get('artist'), entry.get('title')


def update(path: str, username: str, entry: dict, **fields) -> dict:
    """Set fields on one request. Returns the history as saved."""
    with editing(path) as history:
        for candidate in history.get(username, []):
            if _key(candidate) == _key(entry):
                candidate.update(fields)
    return history


def apply_outcomes(path: str, worked_on: dict) -> None:
    """Save how a cycle's requests ended: `worked_on` maps each user to the
    requests it handled. Anything else in the file, such as a request added
    while the cycle worked, stays as it is."""
    with editing(path) as history:
        for username, entries in worked_on.items():
            current = {_key(entry): entry for entry in history.get(username, [])}
            for entry in entries:
                if entry.get('status') != 'pending' and _key(entry) in current:
                    current[_key(entry)].update(status=entry.get('status'), result=entry.get('result'))
