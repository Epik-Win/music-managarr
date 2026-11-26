"""Lyrics files next to the songs, from LRCLIB.

Plex shows a song's lyrics from a file named like it ("<song>.lrc", synced,
or "<song>.txt", plain) when the library has "Prefer local metadata" on.
MusicAI writes them itself:

- right after a download (downloader.download_track), and
- once a day for songs in the library that have none yet (the metadata
  daemon), at most LYRICS_PER_PASS songs a pass, never-asked songs first.

Plex reads a lyrics file when it scans a new song; for a song it already has,
only refreshing its album's metadata shows the new file. Songs given lyrics by
the daily pass are listed in DATA_DIR/lyrics_to_show.txt until their albums
are refreshed (show_in_plex), so an interrupted pass loses none.

The song's own tags (artist, title, album, length) are looked up on LRCLIB:
its exact match first (/api/get: the same names, length within 2 s), else a
search whose result has the same artist and title and a length within
SAME_LENGTH seconds. Synced lyrics are preferred; plain ones are written as
.txt. An instrumental gets no file. A song LRCLIB has nothing for is asked
again after LYRICS_RETRY_DAYS, since lyrics are added there all the time.
Audio files are never rewritten (nothing is embedded).
"""
import json
import logging
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Dict, Iterator, Optional

import requests

import library_state

logger = logging.getLogger("musicai.lyrics_files")

API = "https://lrclib.net/api"
HEADERS = {"User-Agent": "MusicAI (self-hosted music library)"}
TIMEOUT = 20
SAME_LENGTH = 3        # seconds a searched record's length may differ from the song's
PAUSE = 0.5            # seconds between songs in the daily pass (LRCLIB is a free service)
ERRORS_TO_STOP = 5     # LRCLIB unreachable this many songs in a row: the pass stops


def enabled() -> bool:
    return os.environ.get("LYRICS_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def per_pass() -> int:
    return _env_int("LYRICS_PER_PASS", 1500)


def retry_days() -> int:
    return _env_int("LYRICS_RETRY_DAYS", 30)


# -- what was asked ------------------------------------------------------------

def _state_path() -> str:
    return os.path.join(os.environ.get("DATA_DIR", "/app/data"), "lyrics_lookups.json")


def _load() -> Dict[str, dict]:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: Dict[str, dict]) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def _to_show_path() -> str:
    return os.path.join(os.environ.get("DATA_DIR", "/app/data"), "lyrics_to_show.txt")


def _to_show(path: str) -> None:
    with open(_to_show_path(), "a", encoding="utf-8") as fh:
        fh.write(path + "\n")


def show_in_plex(admin_plex, section, pause: float = 0.2) -> int:
    """Refresh the Plex albums of songs given lyrics since the last call, so
    Plex shows the new files. Returns how many albums were refreshed."""
    try:
        with open(_to_show_path(), encoding="utf-8") as fh:
            waiting = {line.strip() for line in fh if line.strip()}
    except OSError:
        return 0
    if not waiting:
        return 0
    from library_review import plex_tracks_by_path
    by_path = plex_tracks_by_path(section)
    albums = {by_path[p].parentRatingKey for p in waiting if p in by_path}
    refreshed = 0
    for key in sorted(albums):
        try:
            admin_plex.fetchItem(int(key)).refresh()
            refreshed += 1
        except Exception as exc:
            logger.debug(f"Plex could not refresh album {key}: {exc}")
        if pause:
            time.sleep(pause)
    os.remove(_to_show_path())
    logger.info(f"🎤 Plex refreshed {refreshed} album(s) to show their new lyrics")
    return refreshed


def _state_key(path: str) -> str:
    return os.path.relpath(os.path.abspath(path), library_state._root())


# -- the song --------------------------------------------------------------------

def has_lyrics(path: str) -> bool:
    base = os.path.splitext(path)[0]
    return any(os.path.isfile(base + ext) for ext in library_state.LYRICS_EXTENSIONS)


def _song(path: str) -> Optional[dict]:
    """The song's artist, title, album and length (seconds) from its file."""
    from mutagen import File as MutagenFile
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return None
    if audio is None or not getattr(audio, "info", None):
        return None
    first = lambda name: ((audio.get(name) or [""])[0] or "").strip()
    artist, title = first("artist") or first("albumartist"), first("title")
    length = getattr(audio.info, "length", 0) or 0
    if not artist or not title or length <= 0:
        return None
    return {"artist": artist, "title": title, "album": first("album"), "length": float(length)}


def _key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W_]+", " ", text.casefold()).split())


def _same(a: str, b: str) -> bool:
    ka, kb = _key(a), _key(b)
    return bool(ka and kb) and (ka == kb or SequenceMatcher(None, ka, kb).ratio() >= 0.9)


def _main_artist(artist: str) -> str:
    return re.split(r"\s+(?:feat\.?|ft\.?|featuring|with|x|&)\s+|\s*[,;/]\s*", artist or "", 1, flags=re.I)[0]


def _bare_title(title: str) -> str:
    """The title without a version or feature in brackets."""
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title or "").strip()


def _fits(song: dict, record: dict) -> bool:
    if abs(float(record.get("duration") or 0) - song["length"]) > SAME_LENGTH:
        return False
    titles = {song["title"], _bare_title(song["title"])}
    if not any(_same(t, record.get("trackName", "")) or _same(t, _bare_title(record.get("trackName", "")))
               for t in titles):
        return False
    return _same(_main_artist(song["artist"]), _main_artist(record.get("artistName", "")))


def _usable(record: Optional[dict]) -> bool:
    return bool(record) and bool(record.get("syncedLyrics") or record.get("plainLyrics") or record.get("instrumental"))


def find(song: dict) -> Optional[dict]:
    """LRCLIB's record of the song, or None. Raises requests.RequestException
    when LRCLIB can't be asked (the song is then asked again later)."""
    params = {"artist_name": song["artist"], "track_name": song["title"], "duration": round(song["length"])}
    if song.get("album"):
        params["album_name"] = song["album"]
    resp = requests.get(f"{API}/get", params=params, headers=HEADERS, timeout=TIMEOUT)
    if resp.status_code != 404:
        resp.raise_for_status()
        if _usable(resp.json()):
            return resp.json()
    for name in dict.fromkeys(t for t in (song["title"], _bare_title(song["title"])) if t):
        resp = requests.get(f"{API}/search", params={"artist_name": _main_artist(song["artist"]), "track_name": name},
                            headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        fits = [r for r in (resp.json() or []) if _usable(r) and _fits(song, r)]
        if fits:
            # Synced first, then the closest length.
            fits.sort(key=lambda r: (not r.get("syncedLyrics"), abs(float(r.get("duration") or 0) - song["length"])))
            return fits[0]
    return None


def write(path: str, record: dict) -> Optional[str]:
    """Write the record's lyrics next to the song; returns the file written."""
    synced, plain = (record.get("syncedLyrics") or "").strip(), (record.get("plainLyrics") or "").strip()
    if record.get("instrumental") or not (synced or plain):
        return None
    target = os.path.splitext(path)[0] + (".lrc" if synced else ".txt")
    with open(target + ".part", "w", encoding="utf-8") as fh:
        fh.write((synced or plain) + "\n")
    os.replace(target + ".part", target)
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            st = os.stat(path)
            os.chown(target, st.st_uid, st.st_gid)     # the song's owner, as Lidarr expects
    except OSError:
        pass
    return target


def fetch_for(path: str, state: Optional[Dict[str, dict]] = None) -> str:
    """Get the song's lyrics file if it has none. Returns what happened:
    "had", "written", "instrumental", "none", "untagged" or "error"."""
    if has_lyrics(path):
        return "had"
    save = state is None
    state = _load() if save else state
    song = _song(path)
    if song is None:
        result = "untagged"
    else:
        try:
            record = find(song)
        except (requests.RequestException, ValueError) as exc:
            logger.debug(f"LRCLIB could not be asked about {song['artist']} - {song['title']}: {exc}")
            return "error"
        if record is None:
            result = "none"
        elif record.get("instrumental") and not (record.get("syncedLyrics") or record.get("plainLyrics")):
            result = "instrumental"
        else:
            written = write(path, record)
            result = "written" if written else "none"
            if written:
                logger.info(f"🎤 Lyrics ({os.path.splitext(written)[1][1:]}): {song['artist']} - {song['title']}")
    key = _state_key(path)
    if result == "written":
        state.pop(key, None)
    else:
        state[key] = {"checked": time.time(), "result": result}
    if save:
        _save(state)
    return result


# -- the daily pass ----------------------------------------------------------------

def _songs_without_lyrics(root: str) -> Iterator[str]:
    for folder, dirs, files in os.walk(root):
        dirs.sort()
        names = set(files)
        for name in sorted(files):
            if not name.lower().endswith(library_state.AUDIO_EXTENSIONS):
                continue
            base = os.path.splitext(name)[0]
            if any(base + ext in names for ext in library_state.LYRICS_EXTENSIONS):
                continue
            yield os.path.join(folder, name)


def backfill(limit: Optional[int] = None, pause: float = PAUSE, root: Optional[str] = None) -> Dict[str, int]:
    """Look up lyrics for up to `limit` songs without a lyrics file: songs never
    asked first, then those asked longest ago (after LYRICS_RETRY_DAYS)."""
    limit = per_pass() if limit is None else limit
    root = root or library_state._root()
    state = _load()
    present = set()
    due = []
    retry_after = retry_days() * 86400
    now = time.time()
    os.makedirs(os.path.dirname(_to_show_path()), exist_ok=True)
    for path in _songs_without_lyrics(root):
        key = _state_key(path)
        present.add(key)
        checked = (state.get(key) or {}).get("checked", 0)
        if now - checked >= retry_after:
            due.append((checked, path))
    for key in [k for k in state if k not in present]:
        del state[key]                    # gone, or has lyrics now
    due.sort()
    counts: Dict[str, int] = {}
    errors_in_a_row = 0
    try:
        for _checked, path in due[:limit]:
            result = fetch_for(path, state)
            counts[result] = counts.get(result, 0) + 1
            if result == "written":
                _to_show(path)
            errors_in_a_row = errors_in_a_row + 1 if result == "error" else 0
            if errors_in_a_row >= ERRORS_TO_STOP:
                logger.warning("LRCLIB isn't answering: lyrics lookups stop until the next pass")
                break
            if pause:
                time.sleep(pause)
    finally:
        _save(state)
    logger.info(f"🎤 Lyrics: {len(due)} song(s) due, " +
                (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none asked"))
    return counts
