"""Settings the admin changes from Discord while MusicAI runs.

They live in DATA_DIR/settings.json, shared by every process (the bot writes
them; the one-star watcher and the cycles read them on each pass). A value
set there wins over the environment variable of the same name, which stays
the default until the admin changes it.
"""
import json
import os
from typing import Any

_TRUE = ('1', 'true', 'yes', 'on')
_FALSE = ('0', 'false', 'no', 'off')


def _path() -> str:
    return os.path.join(os.environ.get('DATA_DIR', '/app/data'), 'settings.json')


def _load() -> dict:
    try:
        with open(_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_bool(name: str, default: bool) -> bool:
    """The setting's value: settings.json, else the environment, else default."""
    value = _load().get(name)
    if isinstance(value, bool):
        return value
    raw = str(os.environ.get(name, '')).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def get_value(name: str, default: Any = None) -> Any:
    """A value kept in settings.json, or default when none is."""
    return _load().get(name, default)


def update(values: dict) -> None:
    """Write several settings at once; a value of None removes that setting."""
    data = _load()
    for name, value in values.items():
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = f"{_path()}.{os.getpid()}.tmp"
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, _path())


def set_value(name: str, value: Any) -> None:
    update({name: value})
