"""Songs keep their places in Plex playlists.

Plex knows a song by its file. A song MusicAI files elsewhere (a metadata
fix, an album merge) or replaces with a better recording is a new song to
Plex, and the old one leaves every playlist it was in once Plex empties its
trash. A song downloaded for a playlist may also not be in Plex yet when the
playlist is made. So songs are *placed*: a placement is a song (its file)
that belongs in a user's playlist after another song, and the metadata
daemon puts each one there once Plex knows the file.

- Every move is noted, by any process (library_state.moved → note_move).
- Before its daily work the daemon notes every user's playlists, in order
  (snapshot). A moved song in a noted playlist becomes a placement, after the
  song it followed.
- A playlist made from downloads (discord_bot) places what Plex hadn't
  scanned yet.

Placements Plex never shows a file for are dropped after KEEP_DAYS.
Playlists MusicAI keeps itself (Fix Metadata, Manual Fix, Delete, Soon to go)
are left alone.
"""
import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger("musicai.playlist_keeper")

KEEP_DAYS = 3
LIST_EVERY = 600       # seconds: Plex's songs are listed at most this often while songs wait
_last_listed = 0.0
_FILES = {"moves": "moved_songs.json", "snapshot": "playlist_snapshot.json", "places": "playlist_places.json"}


def _path(name: str) -> str:
    return os.path.join(os.environ.get("DATA_DIR", "/app/data"), _FILES[name])


@contextmanager
def _state(name: str, default):
    """The named state, locked against other processes; saved on exit."""
    path = _path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = default
        holder = [data]
        yield holder
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(holder[0], fh, ensure_ascii=False)
        os.replace(path + ".tmp", path)


def _norm(path: str) -> str:
    return os.path.normpath(path) if path else path


def kept_by_musicai() -> set:
    """Playlists MusicAI fills and empties itself."""
    import delete_playlist
    return {"Fix Metadata", "Manual Fix", "Soon to go", delete_playlist.playlist_name()}


# -- noting ---------------------------------------------------------------------

def note_move(old: str, new: str) -> None:
    """A song's file moved (or was replaced at another path)."""
    old, new = _norm(old), _norm(new)
    if not old or not new or old == new:
        return
    with _state("moves", {}) as moves:
        for entry in moves[0].values():            # a song moved twice: one move
            if entry["to"] == old:
                entry["to"] = new
        moves[0][old] = {"to": new, "at": time.time()}


def place(user: str, playlist: str, path: str, after: Optional[str] = None, index: int = 0) -> None:
    """The song at `path` belongs in the user's playlist (a title), after the
    song at `after` (None: first); `index` is its position there."""
    _add_places([_entry(user, playlist, path, after, index)])


def place_playlist(user: str, title: str, files: List[str], only: Optional[Iterable[str]] = None) -> None:
    """Place a playlist's songs (those in `only`, else all) in the user's
    playlist of that title, in order; the playlist is made if it is missing."""
    files = [_norm(f) for f in files if f]
    only = None if only is None else {_norm(f) for f in only}
    _add_places([_entry(user, title, f, files[i - 1] if i else None, i, create=True)
                 for i, f in enumerate(files) if only is None or f in only])


def _entry(user: str, playlist: str, path: str, after: Optional[str], index: int, create: bool = False) -> dict:
    """A placement; with `create`, a playlist that doesn't exist yet is made for it."""
    return {"user": user, "playlist": playlist, "path": _norm(path), "after": _norm(after),
            "index": index, "create": create, "at": time.time()}


def _add_places(entries: List[dict], renamed: Optional[Dict[str, str]] = None) -> None:
    with _state("places", []) as places:
        for old, new in (renamed or {}).items():     # a song placed after a song that moved since
            for p in places[0]:
                if p["after"] == old:
                    p["after"] = new
        keys = {(e["user"], e["playlist"], e["path"]) for e in entries}
        places[0] = [p for p in places[0] if (p["user"], p["playlist"], p["path"]) not in keys] + entries


def _files(track) -> List[str]:
    from paths import host_to_container
    return [_norm(host_to_container(part.file)) for media in (track.media or []) for part in (media.parts or [])
            if getattr(part, "file", None)]


def snapshot(servers: Dict[str, object]) -> int:
    """Note every user's playlists of songs, in order. Returns how many."""
    skip = kept_by_musicai()
    noted: Dict[str, Dict[str, List[str]]] = {}
    for user, server in servers.items():
        try:
            lists = [p for p in server.playlists() if getattr(p, "playlistType", "audio") == "audio"
                     and not getattr(p, "smart", False) and p.title not in skip]
        except Exception as exc:
            logger.warning(f"Could not read {user}'s playlists: {exc}")
            continue
        noted[user] = {}
        for playlist in lists:
            try:
                noted[user][playlist.title] = [f for track in playlist.items() for f in _files(track)[:1]]
            except Exception as exc:
                logger.debug(f"Could not read {user}'s playlist '{playlist.title}': {exc}")
    with _state("snapshot", {}) as state:
        state[0] = {"at": time.time(), "users": noted}
    return sum(len(v) for v in noted.values())


# -- placing ----------------------------------------------------------------------

def _moves_to_places() -> None:
    """Each noted move of a song in a noted playlist becomes a placement."""
    with _state("moves", {}) as moves, _state("snapshot", {}) as snap:
        if not moves[0]:
            return
        users = (snap[0] or {}).get("users", {})
        entries: List[dict] = []
        renamed = {old: entry["to"] for old, entry in moves[0].items()}
        for old, new in renamed.items():
            for user, lists in users.items():
                for title, files in lists.items():
                    if old in files:
                        at = files.index(old)
                        files[at] = new
                        entries.append(_entry(user, title, new, files[at - 1] if at else None, at))
        for e in entries:                            # the song before it moved too
            e["after"] = renamed.get(e["after"], e["after"])
        _add_places(entries, renamed)
        moves[0] = {}


def settle(servers_for, section, now: Optional[float] = None) -> int:
    """Put every placed song Plex now knows into its playlist. `servers_for`
    gives a user's Plex connection. Returns how many songs were placed."""
    global _last_listed
    _moves_to_places()
    now = now or time.time()
    with _state("places", []) as places:
        waiting = places[0]
        if not waiting or now - _last_listed < LIST_EVERY:
            return 0
        _last_listed = now
        from library_review import plex_tracks_by_path
        try:
            by_path = plex_tracks_by_path(section)
        except Exception as exc:
            logger.warning(f"Plex's songs could not be listed; playlists are settled later: {exc}")
            return 0
        placed, left = 0, []
        servers: Dict[str, object] = {}
        playlists: Dict[tuple, object] = {}
        # In each playlist, earlier songs first: a song goes after one placed before it.
        for entry in sorted(waiting, key=lambda e: (e["user"], e["playlist"], e.get("index") or 0)):
            track = by_path.get(entry["path"])
            if track is None:
                if now - entry["at"] < KEEP_DAYS * 86400:
                    left.append(entry)
                else:
                    logger.info(f"📋 {entry['path']} never showed in Plex; not put in "
                                f"{entry['user']}'s '{entry['playlist']}'")
                continue
            key = (entry["user"], entry["playlist"])
            if entry["user"] not in servers:
                servers[entry["user"]] = servers_for(entry["user"])
            if key not in playlists:
                server = servers[entry["user"]]
                playlists[key] = next((p for p in server.playlists() if p.title == entry["playlist"]), None) \
                    if server is not None else None
            playlist = playlists[key]
            if playlist is None and entry.get("create") and servers.get(entry["user"]) is not None:
                # A playlist from downloads Plex showed none of at first.
                playlists[key] = servers[entry["user"]].createPlaylist(entry["playlist"], items=[track])
                placed += 1
                logger.info(f"📋 Made {entry['user']}'s playlist '{entry['playlist']}'")
                continue
            if playlist is None:
                continue                                    # the playlist is gone: nothing to do
            try:
                items = playlist.items()
                if any(entry["path"] in _files(t) for t in items):
                    continue
                playlist.addItems([track])
                playlist.reload()                           # the items are cached
                items = playlist.items()
                after = next((t for t in items if entry["after"] and entry["after"] in _files(t)), None)
                if after is not None or entry["after"] is None:
                    playlist.moveItem(track, after=after)   # else it stays last
                placed += 1
                logger.info(f"📋 Put {track.title} back in {entry['user']}'s '{entry['playlist']}'")
            except Exception as exc:
                logger.warning(f"Could not put {entry['path']} in {entry['user']}'s '{entry['playlist']}': {exc}")
                left.append(entry)
        places[0] = left
    return placed


def make_playlist(server, section, title: str, files: Iterable[str], user: str, wait: float = 180,
                  poll: float = 15):
    """Make the playlist from these files in order, once Plex has scanned them
    (asking for a scan, waiting up to `wait` seconds). Files Plex hasn't shown
    by then are placed: the metadata daemon adds them later. Returns the
    playlist, or None when Plex shows none of the files yet (all are placed)."""
    from library_review import plex_tracks_by_path
    files = [_norm(f) for f in files if f]
    try:
        section.update()
    except Exception as exc:
        logger.debug(f"Plex scan request failed: {exc}")
    deadline = time.time() + wait
    while True:
        by_path = plex_tracks_by_path(section)
        missing = [f for f in files if f not in by_path]
        if not missing or time.time() >= deadline:
            break
        time.sleep(poll)
    tracks, seen = [], set()
    for f in files:
        track = by_path.get(f)
        if track is not None and track.ratingKey not in seen:
            seen.add(track.ratingKey)
            tracks.append(track)
    playlist = None
    if tracks:
        playlist = next((p for p in server.playlists() if p.title == title), None)
        if playlist is None:
            playlist = server.createPlaylist(title, items=tracks)
        else:
            have = {t.ratingKey for t in playlist.items()}
            new = [t for t in tracks if t.ratingKey not in have]
            if new:
                playlist.addItems(new)
    place_playlist(user, title, files, only=missing)
    if missing:
        logger.info(f"📋 '{title}': {len(missing)} song(s) not in Plex yet; added once Plex shows them")
    return playlist
