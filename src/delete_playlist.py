"""The admin's "Delete" playlist: songs the admin wants out of the library.

The admin adds songs to a Plex playlist of their own (other users don't see it).
On each daily pass of the Fix Metadata daemon every song in it is deleted, no
matter what: ratings, requests, probation and Lidarr don't keep it. Its lyrics
files and an album folder left empty go too; Lidarr deletes its own files and
its album is unmonitored, so Lidarr doesn't download the song again. The song is
blacklisted for every user, so no cycle brings it back; asking for it again in
Discord does (library_review.unblacklist). The song leaves the playlist with
its file, and the admin gets a notice of what was deleted.
"""
import logging
import os
from typing import List, Optional

import library_review
import library_state
from paths import container_music_root, host_to_container

logger = logging.getLogger("musicai.delete_playlist")

REASON = "In the admin's Delete playlist"
NOTIFICATION_ID = "delete_playlist"


def playlist_name() -> str:
    return os.environ.get("DELETE_PLAYLIST_NAME", "Delete").strip() or "Delete"


def playlist(admin_plex, section_name: str, create: bool = True):
    """The admin's Delete playlist; created empty when missing and `create`."""
    name = playlist_name()
    for found in admin_plex.playlists():
        if found.title == name:
            return found
    if not create:
        return None
    # Plex refuses an empty playlist: it is created with one song, taken out again.
    first = admin_plex.library.section(section_name).search(libtype="track", maxresults=1)
    if not first:
        return None
    made = admin_plex.createPlaylist(name, items=first)
    made.removeItems(first)
    made.reload()                    # Plex's client keeps the items it was made with until reloaded
    logger.info(f"Created the admin's empty '{name}' playlist")
    return made


def _lidarr():
    try:
        from lidarr_catalog import LidarrCatalog
        catalog = LidarrCatalog.from_env()
        return catalog.load() if catalog else None
    except Exception as exc:
        logger.warning(f"Lidarr can't be asked about its files ({exc}); songs are deleted without it")
        return None


def _files(track) -> List[str]:
    return [host_to_container(part.file) for media in (track.media or []) for part in (media.parts or [])
            if getattr(part, "file", None)]


def run(admin_plex, section_name: str, root: Optional[str] = None, dry_run: bool = False) -> List[str]:
    """Delete every song in the admin's Delete playlist; returns "Artist - Title" of each."""
    found = playlist(admin_plex, section_name, create=False)
    if found is None:
        playlist(admin_plex, section_name)    # made now, so empty: nothing to delete
        return []
    tracks = found.items()
    if not tracks:
        return []
    root = root or library_state._root()
    music = os.path.normpath(container_music_root())
    lidarr = None if dry_run else _lidarr()
    databases = library_review.user_databases()
    done: List[str] = []
    for track in tracks:
        files = _files(track)
        unreachable = [f for f in files if not os.path.normpath(f).startswith(music + os.sep)]
        if unreachable:
            logger.warning(f"🗑️ Can't delete {track.title}: {unreachable[0]} is outside the music folder; "
                           f"it stays in the playlist")
            continue
        present = [f for f in files if os.path.isfile(f)]
        title, artist = library_review._tags(present[0]) if present else (None, None)
        title = title or track.title
        artist = (artist if artist and artist != "Unknown" else None) or track.originalTitle or track.grandparentTitle or "Unknown"
        label = f"{artist} - {title}"
        logger.info(f"🗑️ {'Would delete' if dry_run else 'Deleting'} (admin's Delete playlist): {label}")
        if dry_run:
            done.append(label)
            continue
        for path in present:
            try:
                album = lidarr.forget_track_file(path) if lidarr else None
                if album is not None:
                    logger.info(f"   Lidarr deleted its file and no longer wants '{album}'")
            except Exception as exc:
                logger.warning(f"   Lidarr could not delete {path}: {exc}")
            try:
                library_review.delete_song(path, root, missing_ok=True)
            except OSError as exc:
                logger.warning(f"   Could not delete {path}: {exc}")
        if any(os.path.isfile(f) for f in files):
            continue                       # still there: tried again on the next pass
        library_review.record_deletion(title, artist, REASON, databases)
        try:
            found.removeItems([track])
        except Exception as exc:
            logger.debug(f"   Plex drops {label} from the playlist with its file: {exc}")
        done.append(label)
    if done and not dry_run:
        try:
            section = admin_plex.library.section(section_name)
            section.update()
            section.emptyTrash()
        except Exception as exc:
            logger.debug(f"Plex refresh after deleting failed: {exc}")
        _tell_admin(done)
    return done


def _tell_admin(done: List[str]) -> None:
    """One notice per pass (it replaces the previous pass's notice)."""
    shown = "\n".join(f"• {label}" for label in done[:25])
    if len(done) > 25:
        shown += f"\n… and {len(done) - 25} more"
    try:
        import admin_notify
        admin_notify.notify_admin(f"🗑️ Deleted {len(done)} song(s) in your '{playlist_name()}' playlist:\n{shown}",
                                  NOTIFICATION_ID)
    except Exception as exc:
        logger.debug(f"Could not tell the admin about the deletions: {exc}")
