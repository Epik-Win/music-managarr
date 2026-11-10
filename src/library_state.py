"""Where each song in the library stands, kept as symlinks Plex never sees.

Every song lives in albums/, the one library Plex, Lidarr and LRCGET scan.
A song MusicAI picks on its own is on *probation* until its score graduates
it; a disliked song is *soon to go*: first in line when disk space runs low
(library_review.py decides what is kept). Each state is a folder of relative
symlinks that mirror the song's place in albums/:

    $DATA_DIR/library/probation/<Artist>/<Album>/<file>
        -> ../../../../../../music/albums/<Artist>/<Album>/<file>

The folders live outside the music library so a media server never lists a
song twice. The links are relative, so they resolve from the host as well
as from the container, as long as both see the data and music folders side
by side. A link's own mtime is when the song entered that state.

A song that moves (Fix Metadata filing it, Lidarr renaming it) keeps its
inode, so a link a move left dangling is re-pointed by inode (index.json
remembers each link's). A link whose song is gone is dropped; nothing here
ever deletes a song.
"""
import fcntl
import json
import logging
import os
import shutil
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROBATION = "probation"
SOON_TO_GO = "soon_to_go"
STATES = (PROBATION, SOON_TO_GO)

LIBRARY_ROOT = os.environ.get("ALBUM_DIR", "/music/albums")
STATE_DIR = os.environ.get("LIBRARY_STATE_DIR",
                           os.path.join(os.environ.get("DATA_DIR", "/app/data"), "library"))
AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma")
LYRICS_EXTENSIONS = (".lrc", ".txt")     # LRCGET's files, next to the song under its name


def _root() -> str:
    return os.path.normpath(os.path.abspath(LIBRARY_ROOT))


def _rel(path: str) -> Optional[str]:
    """The song's path inside albums/, or None for a file outside it."""
    if not path:
        return None
    rel = os.path.relpath(os.path.normpath(os.path.abspath(path)), _root())
    return None if rel == "." or rel.startswith("..") else rel


def _link(state: str, rel: str) -> str:
    return os.path.join(STATE_DIR, state, rel)


@contextmanager
def _locked():
    """One writer at a time: the cycle, Fix Metadata and the bot all mark songs."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _index_path() -> str:
    return os.path.join(STATE_DIR, "index.json")


def _load_index() -> Dict[str, Dict[str, int]]:
    try:
        with open(_index_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {state: dict(data.get(state) or {}) for state in STATES}


def _save_index(index: Dict[str, Dict[str, int]]) -> None:
    tmp = _index_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(index, f, indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, _index_path())


def _prune_dirs(folder: str, stop: str) -> None:
    """Remove folders a dropped link left empty, up to the state folder."""
    stop = os.path.normpath(stop)
    while os.path.normpath(folder).startswith(stop + os.sep):
        try:
            os.rmdir(folder)
        except OSError:
            return
        folder = os.path.dirname(folder)


def _make_link(state: str, rel: str, since: float) -> None:
    link = _link(state, rel)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    target = os.path.relpath(os.path.join(_root(), rel), os.path.dirname(link))
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(target, link)
    os.utime(link, (since, since), follow_symlinks=False)


def _drop_link(state: str, rel: str) -> bool:
    link = _link(state, rel)
    if not os.path.lexists(link):
        return False
    os.remove(link)
    _prune_dirs(os.path.dirname(link), os.path.join(STATE_DIR, state))
    return True


def _since_of(state: str, rel: str) -> Optional[float]:
    try:
        return os.lstat(_link(state, rel)).st_mtime
    except OSError:
        return None


def mark(path: str, state: str, since: Optional[float] = None) -> bool:
    """Put a song in a state (a song is in one state at most). `since`
    (epoch seconds) is when it entered it, now by default. False for a file
    outside albums/ or one that does not exist."""
    rel = _rel(path)
    if state not in STATES or rel is None or not os.path.isfile(path):
        return False
    with _locked():
        index = _load_index()
        for other in STATES:
            if other != state and _drop_link(other, rel):
                index[other].pop(rel, None)
        _make_link(state, rel, since if since is not None else time.time())
        index[state][rel] = os.stat(path).st_ino
        _save_index(index)
    return True


def unmark(path: str, state: Optional[str] = None) -> bool:
    """Take a song out of a state (every state when none is named). True if
    it was in one."""
    rel = _rel(path)
    if rel is None:
        return False
    found = False
    with _locked():
        index = _load_index()
        for s in STATES if state is None else (state,):
            if _drop_link(s, rel):
                found = True
            index[s].pop(rel, None)
        _save_index(index)
    return found


def state_of(path: str) -> Optional[str]:
    rel = _rel(path)
    if rel is None:
        return None
    for state in STATES:
        if os.path.lexists(_link(state, rel)):
            return state
    return None


def since(path: str) -> Optional[float]:
    """When the song entered its state (epoch seconds), None if it has none."""
    rel = _rel(path)
    state = state_of(path)
    return _since_of(state, rel) if state else None


def _carry_lyrics(old: str, new: str) -> None:
    old_base, new_base = os.path.splitext(old)[0], os.path.splitext(new)[0]
    if old_base == new_base:
        return
    for ext in LYRICS_EXTENSIONS:
        if os.path.isfile(old_base + ext) and not os.path.exists(new_base + ext):
            shutil.move(old_base + ext, new_base + ext)


def moved(old: str, new: str) -> None:
    """A song moved (or its file was replaced at another path): its lyrics
    files and its state follow it, with the date it entered that state."""
    try:
        _carry_lyrics(old, new)
    except OSError as exc:
        logger.warning(f"Lyrics of {old} could not follow it to {new}: {exc}")
    old_rel, new_rel = _rel(old), _rel(new)
    if old_rel is None or old_rel == new_rel:
        return
    with _locked():
        index = _load_index()
        for state in STATES:
            if not os.path.lexists(_link(state, old_rel)):
                continue
            when = _since_of(state, old_rel) or time.time()
            _drop_link(state, old_rel)
            index[state].pop(old_rel, None)
            if new_rel is not None and os.path.isfile(new):
                _make_link(state, new_rel, when)
                index[state][new_rel] = os.stat(new).st_ino
        _save_index(index)


def _walk_links(state: str):
    base = os.path.join(STATE_DIR, state)
    for folder, _dirs, files in os.walk(base):
        for name in files:
            link = os.path.join(folder, name)
            if os.path.islink(link):
                yield os.path.relpath(link, base)


def _inodes_in_library() -> Dict[int, str]:
    found = {}
    for folder, _dirs, files in os.walk(_root()):
        for name in files:
            if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                path = os.path.join(folder, name)
                try:
                    found.setdefault(os.stat(path).st_ino, path)
                except OSError:
                    pass
    return found


def repair() -> Dict[str, int]:
    """Re-point links whose song moved (same inode, new path) and drop the
    ones whose song is gone. Returns counts of what it did."""
    done = {"repointed": 0, "dropped": 0}
    with _locked():
        index = _load_index()
        dangling = []
        for state in STATES:
            for rel in _walk_links(state):
                path = os.path.join(_root(), rel)
                if os.path.isfile(path):
                    if rel not in index[state]:
                        index[state][rel] = os.stat(path).st_ino
                else:
                    dangling.append((state, rel))
        if dangling:
            by_inode = _inodes_in_library()
            claimed = {rel for state in STATES for rel in _walk_links(state)}
            for state, rel in dangling:
                when = _since_of(state, rel) or time.time()
                ino = index[state].pop(rel, None)
                _drop_link(state, rel)
                new_path = by_inode.get(ino) if ino else None
                new_rel = _rel(new_path) if new_path else None
                if new_rel and new_rel not in claimed:
                    _make_link(state, new_rel, when)
                    index[state][new_rel] = ino
                    claimed.add(new_rel)
                    done["repointed"] += 1
                    logger.info(f"Library state: {state} song moved, link follows it: {rel} → {new_rel}")
                else:
                    done["dropped"] += 1
                    logger.info(f"Library state: {state} song is no longer in albums/, link dropped: {rel}")
        for state in STATES:
            live = set(_walk_links(state))
            index[state] = {rel: ino for rel, ino in index[state].items() if rel in live}
        _save_index(index)
    return done


def songs(state: str) -> List[Tuple[str, float]]:
    """(path in albums/, since) of every song in a state, oldest first,
    after repairing links that moves left dangling."""
    repair()
    out = []
    for rel in _walk_links(state):
        path = os.path.join(_root(), rel)
        when = _since_of(state, rel)
        if os.path.isfile(path) and when is not None:
            out.append((path, when))
    return sorted(out, key=lambda item: item[1])


def counts() -> Dict[str, int]:
    return {state: sum(1 for _ in _walk_links(state)) for state in STATES}
