"""The library review: which songs stay, run once per full download pass.

Every song lives in albums/. A song MusicAI picks on its own starts on
probation (library_state); disliked songs are put on the soon-to-go list.
The manager runs this review after every user's cycle of a pass has
refreshed that user's scores (`--when end`), and before a pass's downloads
if the disk is already low (`--when start`):

1. **Graduation** — a probationary song whose score reaches GRADUATION_SCORE
   for any user leaves probation and stays for good.
2. **Probation review** (end of a pass) — a probationary song that has been
   in the library for a whole pass and still scores below
   PROBATION_DELETE_SCORE for every user is deleted, whatever the free space.
   Songs downloaded in the pass that just ran wait for the next one, so
   everyone gets a full cycle to listen to them.
3. **Disk space** — when free space is below DISK_LOW_GB, songs are deleted
   until DISK_TARGET_GB is free: soon-to-go songs first, then every other
   song by score, a probationary song's score lowered by PROBATION_PENALTY
   so it goes before a permanent song that scores the same. At most
   DISK_DELETE_MAX songs go per review, since other data on the same disk
   can fill it and the library should not pay for all of it.

Scores are taken per user (each user's own database) and the best one
counts: a song one user loves stays even if the others never play it.
Never deleted: songs Lidarr keeps (files of albums it monitors — it would
download them again), songs scoring GRADUATION_SCORE or more, and songs
Plex does not know yet. Every deleted song is blacklisted for every user so
no cycle downloads it again. Nothing is deleted while ENABLE_AUTO_DELETE is
off, or while Lidarr cannot say which songs it keeps.

    python3 library_review.py --when end [--dry-run]
"""
import argparse
import glob
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

import cycle_schedule
import library_state

logger = logging.getLogger("musicai.library_review")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


GRADUATION_SCORE = _env_float("GRADUATION_SCORE", 0.55)
PROBATION_DELETE_SCORE = _env_float("PROBATION_DELETE_SCORE", 0.15)
PROBATION_PENALTY = _env_float("PROBATION_PENALTY", 0.3)
DISK_LOW_GB = _env_float("DISK_LOW_GB", 10)
DISK_TARGET_GB = _env_float("DISK_TARGET_GB", 20)
DISK_DELETE_MAX = int(_env_float("DISK_DELETE_MAX", 250))
ENABLE_AUTO_DELETE = os.environ.get("ENABLE_AUTO_DELETE", "true").strip().lower() == "true"
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
PLEX_MUSIC_SECTION = (os.environ.get("PLEX_MUSIC_SECTION") or os.environ.get("PLEX_MUSIC_SECTIONS")
                      or "Music").split(",")[0].split(":")[0].strip()


# -- what the review works from ----------------------------------------------

def user_databases() -> Dict[str, str]:
    """{user: path of their music_ai.db} for every user with a database."""
    return {os.path.basename(os.path.dirname(p)): p
            for p in sorted(glob.glob(os.path.join(DATA_DIR, "*", "music_ai.db")))}


def best_scores(keys: Iterable[str], databases: Dict[str, str]) -> Dict[str, float]:
    """{Plex track key: the best score any user gives it}."""
    wanted = set(keys)
    best: Dict[str, float] = {}
    for db in databases.values():
        try:
            conn = sqlite3.connect(db)
            for key, score in conn.execute("SELECT track_id, score FROM track_scores"):
                if key in wanted and score is not None and score > best.get(key, -1):
                    best[key] = float(score)
            conn.close()
        except sqlite3.Error as exc:
            logger.warning(f"Could not read scores from {db}: {exc}")
    return best


def plex_tracks_by_path(section) -> Dict[str, object]:
    from paths import host_to_container
    by_path = {}
    for track in section.searchTracks():
        for media in track.media or []:
            for part in media.parts or []:
                by_path[os.path.normpath(host_to_container(part.file))] = track
    return by_path


def lidarr_kept_paths() -> Optional[Set[str]]:
    """Lidarr's files in albums it monitors; None when Lidarr can't be asked."""
    try:
        from lidarr_catalog import LidarrCatalog
        catalog = LidarrCatalog.from_env()
        return set() if catalog is None else catalog.load().kept_track_file_paths()
    except Exception as exc:
        logger.warning(f"Lidarr can't be asked which songs it keeps: {exc}")
        return None


def pass_started() -> float:
    """Start of the pass in progress (epoch seconds); songs probation began
    after it have not had a whole pass yet."""
    started = cycle_schedule._parse(cycle_schedule.load().get("started"))
    return started.timestamp() if started else datetime.now().timestamp()


def free_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


# -- deleting -----------------------------------------------------------------

def _tags(path: str) -> Tuple[str, str]:
    from mutagen import File as MutagenFile
    try:
        tags = MutagenFile(path, easy=True) or {}
        return ((tags.get("title") or [os.path.splitext(os.path.basename(path))[0]])[0],
                (tags.get("artist") or ["Unknown"])[0])
    except Exception:
        return os.path.splitext(os.path.basename(path))[0], "Unknown"


def delete_song(path: str, root: str) -> None:
    """Delete a song with its lyrics files; an album folder left with only
    artwork goes too, and so does an artist folder that is then empty."""
    base = os.path.splitext(path)[0]
    os.remove(path)
    for ext in library_state.LYRICS_EXTENSIONS:
        if os.path.isfile(base + ext):
            os.remove(base + ext)
    library_state.unmark(path)
    folder = os.path.dirname(path)
    for _ in range(2):
        if os.path.normpath(folder) == os.path.normpath(root):
            return
        try:
            names = os.listdir(folder)
        except OSError:
            return
        if any(os.path.isdir(os.path.join(folder, n)) or n.lower().endswith(library_state.AUDIO_EXTENSIONS)
               for n in names):
            return
        if not all(n.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) for n in names):
            return
        for n in names:
            os.remove(os.path.join(folder, n))
        os.rmdir(folder)
        folder = os.path.dirname(folder)


def record_deletion(title: str, artist: str, reason: str, databases: Dict[str, str]) -> None:
    """Blacklist the song for every user (no cycle downloads it again) and
    log the deletion in each user's database."""
    for user, db in databases.items():
        try:
            conn = sqlite3.connect(db)
            conn.execute("INSERT OR IGNORE INTO blacklist (user_id, title, artist, reason) VALUES (?, ?, ?, ?)",
                         (user, title, artist, reason))
            conn.execute("INSERT INTO deletions (title, artist, reason, archived) VALUES (?, ?, ?, ?)",
                         (title, artist, reason, False))
            conn.commit()
            conn.close()
        except sqlite3.Error as exc:
            logger.warning(f"Could not record the deletion of {artist} - {title} in {db}: {exc}")


def unblacklist(title: str, artist: str, databases: Optional[Dict[str, str]] = None) -> int:
    """Someone asked for this song again: it comes off every user's blacklist
    (an explicit request wins over an earlier dislike or deletion, and a
    blacklist entry left behind would have the next cycle purge the song).
    Returns how many entries were removed."""
    import re
    key = lambda v: " ".join(re.sub(r"[\W_]+", " ", str(v or "").casefold()).split())
    want = (key(title), key(artist))
    if not want[0]:
        return 0
    removed = 0
    for db in (databases if databases is not None else user_databases()).values():
        try:
            conn = sqlite3.connect(db)
            rows = [rid for rid, t, a in conn.execute("SELECT id, title, artist FROM blacklist")
                    if (key(t), key(a)) == want]
            conn.executemany("DELETE FROM blacklist WHERE id = ?", [(rid,) for rid in rows])
            conn.commit()
            conn.close()
            removed += len(rows)
        except sqlite3.Error as exc:
            logger.warning(f"Could not update the blacklist in {db}: {exc}")
    return removed


# -- the review ---------------------------------------------------------------

def review(section, when: str, dry_run: bool = False) -> Dict[str, List[str]]:
    root = library_state._root()
    databases = user_databases()
    by_path = plex_tracks_by_path(section)
    scores = best_scores((t.key for t in by_path.values()), databases)
    score_of = lambda path: scores.get(by_path[path].key, 0.0) if path in by_path else 0.0
    started = pass_started()
    done: Dict[str, List[str]] = {"graduated": [], "probation_deleted": [], "space_deleted": [], "kept_by_lidarr": []}
    rel = lambda p: os.path.relpath(p, root)

    kept = lidarr_kept_paths()
    probation = library_state.songs(library_state.PROBATION)

    for path, _since in probation:
        if score_of(path) >= GRADUATION_SCORE:
            done["graduated"].append(path)
            logger.info(f"🎓 Graduated (score {score_of(path):.2f}): {rel(path)}")
            if not dry_run:
                library_state.unmark(path, library_state.PROBATION)
        elif kept and path in kept:
            # Lidarr monitors its album: it is part of the permanent library.
            done["kept_by_lidarr"].append(path)
            if not dry_run:
                library_state.unmark(path, library_state.PROBATION)

    can_delete = ENABLE_AUTO_DELETE and kept is not None
    if not ENABLE_AUTO_DELETE:
        logger.info("ENABLE_AUTO_DELETE is off: nothing is deleted")
    elif kept is None:
        logger.warning("Lidarr can't say which songs it keeps: nothing is deleted this time")

    def remove(path: str, reason: str, bucket: str) -> bool:
        title, artist = _tags(path)
        done[bucket].append(path)
        logger.info(f"🗑️  {reason}: {artist} - {title} ({rel(path)})")
        if dry_run:
            return True
        try:
            delete_song(path, root)
        except OSError as exc:
            logger.warning(f"Could not delete {rel(path)}: {exc}")
            done[bucket].pop()
            return False
        record_deletion(title, artist, reason, databases)
        return True

    if can_delete and when == "end":
        for path, since in probation:
            if path in done["graduated"] or path in done["kept_by_lidarr"] or since >= started:
                continue
            if path in by_path and score_of(path) < PROBATION_DELETE_SCORE:
                remove(path, f"Probation over, score {score_of(path):.2f} < {PROBATION_DELETE_SCORE}",
                       "probation_deleted")

    if can_delete and free_gb(root) < DISK_LOW_GB:
        logger.warning(f"Disk space low ({free_gb(root):.1f} GB < {DISK_LOW_GB} GB): freeing up to "
                       f"{DISK_TARGET_GB} GB, at most {DISK_DELETE_MAX} songs")
        gone = set(done["probation_deleted"])
        to_go = {p for p, _ in library_state.songs(library_state.SOON_TO_GO)}
        on_probation = {p for p, _ in library_state.songs(library_state.PROBATION)}
        candidates = []
        for path, track in by_path.items():
            if path in gone or (kept and path in kept) or not os.path.isfile(path):
                continue
            score = score_of(path)
            added = getattr(track, "addedAt", None)
            added = added.timestamp() if added else 0
            if score >= GRADUATION_SCORE or added >= started:
                continue                      # loved, or new this pass: not a candidate
            effective = score - PROBATION_PENALTY if path in on_probation else score
            # Soon-to-go songs first, then the lowest (penalised) score, then the oldest.
            candidates.append((0 if path in to_go else 1, effective, added, path))
        candidates.sort()
        # Free space is estimated from file sizes, so a dry run reports what a
        # real run would delete.
        missing = (DISK_TARGET_GB - free_gb(root)) * 1024 ** 3
        for _group, effective, _added, path in candidates[:DISK_DELETE_MAX]:
            if missing <= 0:
                break
            size = os.path.getsize(path)
            if remove(path, f"Disk space low, score {effective:.2f}", "space_deleted"):
                missing -= size

    if not dry_run and (done["probation_deleted"] or done["space_deleted"]):
        try:
            section.update()
        except Exception as exc:
            logger.debug(f"Plex scan request failed: {exc}")
    logger.info("Library review: " + ", ".join(f"{len(v)} {k.replace('_', ' ')}" for k, v in done.items()))
    return done


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--when", choices=("start", "end"), default="end",
                        help="start: before a pass's downloads (disk space only); end: after every user's cycle")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen, change nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s - [%(levelname)s] - %(message)s")
    import plex_auth
    from plexapi.server import PlexServer
    plex = PlexServer(os.environ["PLEX_URL"], plex_auth.admin_token(), timeout=300)
    review(plex.library.section(PLEX_MUSIC_SECTION), args.when, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
