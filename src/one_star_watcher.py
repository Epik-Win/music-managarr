"""Delete tracks the admin rates exactly one star, as soon as they do it.

The main per-user cycle (every SCAN_INTERVAL) puts a song on the soon-to-go
list once two users have rated it one star. The admin's one-star rating is a
stronger signal: it means "get this out of the library now". This process
polls Plex as the admin every ONE_STAR_POLL_SECONDS and, for every *track*
with userRating == 2.0 (Plex stores stars ×2, so exactly one star; 0.5 stars
and unrated tracks are ignored, and album ratings are never consulted):

1. deletes the audio file(s) behind it (and the album folder if nothing
   playable is left in it),
2. logs the deletion in every user's `deletions` table,
3. asks Plex to rescan and empty its trash so the entry disappears.

A one-star rating is easy to give by mistake, so it blacklists nothing: the
song can be requested or downloaded again. Only the library review's
deletions (library_review.py) blacklist a song.

Only the admin's own rating counts because PLEX_TOKEN is the admin account's
token; other Home users' ratings are invisible through it.

The admin turns it on and off with /one_star_delete in Discord
(runtime_settings ONE_STAR_DELETE, whose default is the environment
variable); it is checked before every pass, and while it is off one-star
ratings are left alone.

Only ratings given while deletion is on count. The time it was switched on
is kept (runtime_settings ONE_STAR_DELETE_SINCE), and a track whose rating
(Plex's lastRatedAt) is older than that is kept: songs rated one star while
deletion was off are not deleted when it is switched back on. Clearing such a
song's rating and rating it one star again, once deletion is on, deletes it.
"""
import logging
import os
import shutil
import sqlite3
import sys
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple

import library_state
import paths
import plex_auth
import users

logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s - [%(levelname)s] - [one-star] - %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger('musicai.one_star')

ONE_STAR_RATING = 2.0
SINCE_SETTING = 'ONE_STAR_DELETE_SINCE'
AUDIO_EXTENSIONS = ('.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wav', '.aac', '.wma')
# Plex keeps ratings per song match, not per file, and restores an old rating
# when the song is added again. A rating older than the file it sits on was
# not given to that file.
STALE_RATING_GRACE_SECONDS = 60


def rating_predates_file(track) -> bool:
    rated = getattr(track, 'lastRatedAt', None)
    added = getattr(track, 'addedAt', None)
    return bool(rated and added and (added - rated).total_seconds() > STALE_RATING_GRACE_SECONDS)


def library_roots() -> Tuple[List[str], List[str]]:
    album_dir = os.environ.get('ALBUM_DIR', '/music/albums')
    return list(dict.fromkeys([album_dir, os.environ.get('MUSIC_DIR', album_dir)])), [album_dir]


def remaining_copy(file_paths: List[str]):
    """Another file of the same recording that stays in the library, if any."""
    try:
        import song_dedup
        roots, album_roots = library_roots()
        for path in file_paths:
            other = song_dedup.other_copy_exists(path, roots, album_roots)
            if other and other not in file_paths:
                return other
    except Exception as exc:
        logger.debug(f"Duplicate check failed: {exc}")
    return None


def enabled() -> bool:
    import runtime_settings
    return runtime_settings.get_bool('ONE_STAR_DELETE', True)


def deleting_since() -> float:
    """When one-star deletion was switched on (epoch seconds). Deletion is on
    but no time is kept (switched on outside Discord, or the first pass after
    an upgrade): it counts from now, so no earlier rating deletes anything."""
    import runtime_settings
    since = runtime_settings.get_value(SINCE_SETTING)
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        return float(since)
    since = time.time()
    runtime_settings.update({SINCE_SETTING: since})
    logger.info("Counting one-star ratings from now on; songs already rated one star are kept")
    return since


def forget_since() -> None:
    """Deletion is off: drop the switch-on time, so switching it back on
    starts a new one and ratings given meanwhile never count."""
    import runtime_settings
    if runtime_settings.get_value(SINCE_SETTING) is not None and not enabled():
        runtime_settings.update({SINCE_SETTING: None})


def rated_before(track, since: float) -> bool:
    """The track's rating was given before deletion was switched on. Plex
    keeps rating times in whole seconds; a rating without a time cannot be
    shown to be new, so it counts as older."""
    rated = getattr(track, 'lastRatedAt', None)
    if rated is None:
        return True
    try:
        return rated.timestamp() < int(since)
    except (AttributeError, OverflowError, OSError, ValueError):
        return True


def poll_seconds() -> int:
    try:
        return max(15, int(os.environ.get('ONE_STAR_POLL_SECONDS', '60')))
    except ValueError:
        return 60


def user_db_paths(mapping: Dict[str, str]) -> List[str]:
    data_dir = os.environ.get('DATA_DIR', '/app/data')
    return [os.path.join(data_dir, plex_user, 'music_ai.db') for plex_user in mapping.values()]


# ── library changes ──────────────────────────────────────────────────────────

def track_files(track) -> List[str]:
    found = []
    for media in getattr(track, 'media', []) or []:
        for part in getattr(media, 'parts', []) or []:
            host_path = getattr(part, 'file', '') or ''
            if host_path:
                found.append(paths.host_to_container(host_path))
    return found


def delete_files(file_paths: Iterable[str]) -> int:
    """Remove the files; a folder left with no audio (only cover art or
    nothing) is removed too so Plex does not keep an empty album."""
    removed = 0
    for file_path in file_paths:
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                removed += 1
                logger.info(f"🗑️ Deleted {file_path}")
                library_state.unmark(file_path)
        except Exception as exc:
            logger.warning(f"Could not delete {file_path}: {exc}")
            continue
        folder = os.path.dirname(file_path)
        try:
            if os.path.isdir(folder) and not any(
                name.lower().endswith(AUDIO_EXTENSIONS) for name in os.listdir(folder)
            ):
                shutil.rmtree(folder)
                logger.info(f"🗑️ Removed empty album folder {folder}")
        except Exception as exc:
            logger.debug(f"Could not remove folder {folder}: {exc}")
    return removed


def record_deletion(db_paths: Iterable[str], artist: str, title: str, track_id: str) -> None:
    """Log the deletion in every user's history (no blacklisting). The table
    is created if a user's database does not have it yet."""
    for db_path in db_paths:
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            conn = sqlite3.connect(db_path)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS deletions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id TEXT,
                    title TEXT,
                    artist TEXT,
                    score REAL,
                    reason TEXT,
                    archived BOOLEAN,
                    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute(
                'INSERT INTO deletions (track_id, title, artist, score, reason, archived) VALUES (?, ?, ?, ?, ?, ?)',
                (track_id, title, artist, 0.0, 'Admin rated one star: deleted', False),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"Could not log the deletion in {db_path}: {exc}")


def one_star_tracks(section) -> List:
    """Tracks the admin rated exactly one star. Only track objects are
    searched, so an album rating never qualifies its songs."""
    return section.searchTracks(filters={'userRating': ONE_STAR_RATING})


def process_once(section, admin_user: str, db_paths: List[str], handled: Set[str], since: float,
                 kept: Optional[Dict[str, object]] = None) -> int:
    """Delete every track rated one star since deletion was switched on;
    returns how many were removed. `kept` remembers the older ratings already
    reported, so each is logged once."""
    if kept is None:
        kept = {}
    deleted = 0
    touched = False
    for track in one_star_tracks(section):
        key = str(getattr(track, 'ratingKey', ''))
        if getattr(track, 'userRating', None) != ONE_STAR_RATING or key in handled:
            continue
        artist = getattr(track, 'grandparentTitle', '') or getattr(track, 'originalTitle', '') or ''
        title = getattr(track, 'title', '') or ''
        files = track_files(track)
        if rating_predates_file(track):
            logger.info(f"🧹 {artist} - {title}: 1-star from {track.lastRatedAt} was restored by Plex onto a file "
                        f"added {track.addedAt}; clearing the old rating instead of deleting")
            try:
                track.rate(None)
            except Exception as exc:
                logger.warning(f"Could not clear restored rating on {artist} - {title}: {exc}")
            handled.add(key)
            continue
        if rated_before(track, since):
            rated = getattr(track, 'lastRatedAt', None)
            if key not in kept or kept[key] != rated:
                kept[key] = rated
                logger.info(f"⏸️ {artist} - {title}: rated one star before deletion was switched on "
                            f"({rated or 'time unknown'}); kept")
            continue
        logger.info(f"⭐ Admin rated 1 star: {artist} - {title} ({len(files)} file(s))")
        other = remaining_copy(files)
        removed = delete_files(files)
        if other:
            logger.info(f"   another copy stays ({other}); only this version was removed")
        if removed:
            record_deletion(db_paths, artist, title, key)
        handled.add(key)
        touched = True
        deleted += removed
        if not removed:
            logger.info(f"   file already gone; leaving Plex to drop the entry: {artist} - {title}")

    if touched:
        try:
            section.update()
            time.sleep(5)
            section.emptyTrash()
        except Exception as exc:
            logger.debug(f"Plex refresh after deletion failed: {exc}")
    return deleted


def main() -> int:
    plex_url = os.environ.get('PLEX_URL', '').strip()
    if not plex_url:
        logger.error("PLEX_URL not set; watcher cannot run")
        return 1

    from plexapi.server import PlexServer

    section_name = os.environ.get('PLEX_MUSIC_SECTIONS', 'Music').split(':')[0].strip() or 'Music'
    handled: Set[str] = set()
    kept: Dict[str, object] = {}
    section = None
    was_enabled = None
    logger.info(f"Watching Plex library '{section_name}' for the admin's one-star tracks every {poll_seconds()}s")

    while True:
        on = enabled()
        if on != was_enabled:
            logger.info("One-star deletion is on" if on else "One-star deletion is off: one-star ratings are left alone")
            was_enabled = on
        if not on:
            try:
                forget_since()
            except Exception as exc:
                logger.debug(f"Could not clear the switch-on time: {exc}")
            time.sleep(poll_seconds())
            continue
        try:
            mapping = users.load_wizarr_mapping() or {}
            admin_user = next(iter(mapping.values()), None)
            if not admin_user:
                logger.warning("No admin user in the Wizarr mapping yet; retrying later")
                time.sleep(poll_seconds())
                continue
            if section is None:
                section = PlexServer(plex_url, plex_auth.admin_token()).library.section(section_name)
            process_once(section, admin_user, user_db_paths(mapping), handled, deleting_since(), kept)
        except Exception as exc:
            logger.warning(f"One-star pass failed ({type(exc).__name__}: {exc}); reconnecting on next pass")
            section = None
        time.sleep(poll_seconds())


if __name__ == '__main__':
    sys.exit(main())
