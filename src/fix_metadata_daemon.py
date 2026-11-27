"""Daily metadata repair for the whole music library, independent of the main
per-user download/discovery cycle (SCAN_INTERVAL).

Once a day it:

1. Processes each user's "Fix Metadata" playlist (tracks the user flagged as
   wrong) via fix_metadata.run_fix_metadata — every user in the Wizarr mapping,
   each on their own last-run timestamp.
2. Scans the library for tracks Plex is missing metadata for (unknown album,
   unknown artist, Various Artists album without a track artist) and fixes them
   via Lidarr first, then audio fingerprinting (metadata_repair.py).
3. Puts anything neither could identify into the admin-only "Manual Fix"
   playlist, removes tracks from it once their metadata is complete, and
   keeps the admin's notice about it up to date (manual_fix.py). With
   Debugarr configured, each such track is reported there for its local AI
   to try, and the fixes it sends back are applied on every poll
   (debug_issues.py).
4. Reconciles albums (album_artist_merger.lidarr_reconcile): folds copies of
   an album into Lidarr's version, removes duplicate songs, and merges albums
   Plex has split in two. It runs after step 2 so albums whose tags were just
   repaired are merged the same day.
5. Deletes every song in the admin's "Delete" playlist (delete_playlist.py).
6. Refills each user's "Picked for You" playlist: library songs they haven't
   heard that sound like the ones they love (taste.py).
7. Writes lyrics files for songs that have none (lyrics_files.py).

Before that work it notes every user's playlists, and on every poll it puts
songs moved since back in their playlists (playlist_keeper.py).

It runs as a standalone companion process (spawned by entrypoint.py alongside
discord_bot.py, arl_server.py and one_star_watcher.py) rather than inside a
per-user `app.py cycle-once` subprocess: that subprocess only exists once per
SCAN_INTERVAL pass, so anything living in it runs at SCAN_INTERVAL's cadence,
not daily.
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List

import debug_issues
import library_owner
import manual_fix
import metadata_repair
import playlist_keeper
import plex_auth
import users
from fix_metadata import run_fix_metadata

logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s - [%(levelname)s] - [fix-metadata] - %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger('musicai.fix_metadata_daemon')

# Schedule keys for the library-wide jobs, which are shared rather than per user.
LIBRARY_KEY = '__library__'
MERGE_KEY = '__album_merge__'
DELETE_KEY = '__delete_playlist__'
LYRICS_KEY = '__lyrics__'
FOR_YOU_KEY = '__picked_for_you__'


def interval_seconds() -> int:
    try:
        return max(3600, int(os.environ.get('FIX_METADATA_INTERVAL_SECONDS', '86400')))
    except ValueError:
        return 86400


def poll_seconds(interval: int) -> int:
    """How often to check whether anything is due — frequent enough that a
    newly-added user (or a container restart) doesn't wait a full extra day,
    but not so tight that it's pointless busywork."""
    return min(300, max(30, interval // 24))


def dry_run() -> bool:
    """METADATA_REPAIR_DRY_RUN=true: nothing is written — user playlists are
    left alone, the library scan only logs what it would change, and the
    Manual Fix playlist is not touched. Defaults to true, so repairs only
    start writing once someone has turned this off on purpose."""
    return os.environ.get('METADATA_REPAIR_DRY_RUN', 'true').strip().lower() not in ('0', 'false', 'no', 'off')


def library_scan_limit() -> int:
    """Tracks repaired per library pass; bounds the fingerprinting API usage."""
    try:
        return max(1, int(os.environ.get('METADATA_REPAIR_MAX_TRACKS', '100')))
    except ValueError:
        return 100


def last_run_path(key: str) -> str:
    data_dir = os.environ.get('DATA_DIR', '/app/data')
    name = key.strip() or 'default'
    return os.path.join(data_dir, f'fix_metadata_last_run_{name}.json')


def read_last_run(key: str):
    path = last_run_path(key)
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            raw = data.get('last_run') if isinstance(data, dict) else None
            if raw:
                return datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except Exception as exc:
        logger.debug(f"Could not read last-run state for '{key}': {exc}")
    return None


def write_last_run(key: str, when: datetime) -> None:
    path = last_run_path(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'last_run': when.isoformat()}, handle)
    except Exception as exc:
        logger.debug(f"Could not write last-run state for '{key}': {exc}")


def is_due(key: str, now: datetime, interval: int) -> bool:
    last_run = read_last_run(key)
    if last_run is None:
        return True
    return (now - last_run).total_seconds() >= interval


# Files written or moved during the current pass. Plex need not watch the
# folders (and may rescan only every few hours), so a song a fix moved would
# show as unavailable until then; run_pass asks for a scan when this is non-zero.
_files_changed = 0


def _count_changes(n: int) -> None:
    global _files_changed
    _files_changed += max(0, int(n or 0))


def rescan_if_changed(admin_plex, plex_section: str) -> None:
    """Ask Plex to scan the library after a pass that wrote or moved files,
    so moved songs appear under their new album and artist right away (Plex
    empties its trash after the scan, dropping the old entries)."""
    if not _files_changed or dry_run():
        return
    try:
        admin_plex.library.section(plex_section).update()
        logger.info(f"🔄 Plex scan requested after {_files_changed} file change(s)")
    except Exception as exc:
        logger.warning(f"Could not ask Plex to scan after the repairs: {exc}")


def process_due_users(admin_plex, mapping, plex_section: str, interval: int) -> List[Dict[str, str]]:
    """Run each due user's Fix Metadata playlist; return the tracks nothing
    could identify, for the admin's Manual Fix playlist."""
    now = datetime.now(timezone.utc)
    unresolved = []
    if dry_run():
        return unresolved
    for plex_username in users.plex_usernames(mapping):
        if not is_due(plex_username, now, interval):
            continue
        logger.info(f"🧭 Fix Metadata due for '{plex_username}' — processing playlist")
        try:
            stats = run_fix_metadata(admin_plex, plex_username, plex_section)
            unresolved.extend(stats.get('unresolved', []))
            _count_changes(stats.get('fixed', 0))
            logger.info(
                f"✓ Fix Metadata complete for '{plex_username}': "
                f"fixed={stats.get('fixed', 0)} skipped={stats.get('skipped', 0)} "
                f"failed={stats.get('failed', 0)} handed to admin={len(stats.get('unresolved', []))}"
            )
        except Exception as exc:
            logger.warning(f"Fix Metadata failed for '{plex_username}': {exc}")
        write_last_run(plex_username, now)
    return unresolved


def repair_library_if_due(admin_plex, plex_section: str, interval: int, state: Dict) -> List[Dict[str, str]]:
    """Once per interval: drop fixed tracks from Manual Fix, then scan the
    library for missing metadata. Returns the tracks still unresolved."""
    now = datetime.now(timezone.utc)
    if not is_due(LIBRARY_KEY, now, interval):
        return []
    logger.info("🔎 Library metadata scan due" + (" (dry run — nothing will be written)" if dry_run() else ""))
    if not dry_run():
        manual_fix.prune_fixed(admin_plex, state)
    # Tracks already waiting on the admin are skipped: they failed before and
    # re-fingerprinting them every day would only burn the API quota.
    skip_keys = manual_fix.playlist_keys(admin_plex)
    section = admin_plex.library.section(plex_section)
    stats, unresolved = metadata_repair.repair_library(section, skip_keys, library_scan_limit(), dry_run=dry_run())
    _count_changes(0 if dry_run() else stats['fixed'])
    logger.info(
        f"✓ Library scan complete: found={stats['found']} "
        f"{'would fix' if dry_run() else 'fixed'}={stats['fixed']} unresolved={stats['unresolved']}"
    )
    try:
        mstats, munresolved = metadata_repair.repair_misfiled(section, skip_keys, library_scan_limit(), dry_run=dry_run())
        unresolved.extend(munresolved)
        _count_changes(0 if dry_run() else mstats['retagged'] + mstats.get('parked', 0))
        logger.info(
            f"✓ Misfiled albums: found={mstats['misfiled']} re-filed={mstats['refiled']} "
            f"re-tagged={mstats['retagged']} parked={mstats.get('parked', 0)} "
            f"awaiting re-file={mstats['refile_pending']} unresolved={mstats['unresolved']}"
        )
    except Exception as exc:
        logger.warning(f"Misfiled-album pass failed: {exc}")
    write_last_run(LIBRARY_KEY, now)
    return [] if dry_run() else unresolved


def merge_albums_if_due(admin_plex, interval: int) -> None:
    """Once per interval: reconcile albums across the shared library.

    Uses the admin connection because managed users cannot merge Plex items.
    MERGER_DRY_RUN=true makes it report without changing anything.
    """
    now = datetime.now(timezone.utc)
    if not is_due(MERGE_KEY, now, interval):
        return
    logger.info("📀 Album reconcile due")
    from album_artist_merger import build_merger_from_env
    try:
        build_merger_from_env(plex=admin_plex).lidarr_reconcile()
    finally:
        # A failing reconcile is retried tomorrow, not on every poll.
        write_last_run(MERGE_KEY, now)


def delete_playlist_if_due(admin_plex, plex_section: str, interval: int) -> None:
    """Once per interval: delete every song in the admin's Delete playlist
    (and create that playlist, empty, when the admin has none yet)."""
    now = datetime.now(timezone.utc)
    if not is_due(DELETE_KEY, now, interval):
        return
    import delete_playlist
    try:
        deleted = delete_playlist.run(admin_plex, plex_section)
        logger.info(f"🗑️ Admin's '{delete_playlist.playlist_name()}' playlist: {len(deleted)} song(s) deleted")
    finally:
        write_last_run(DELETE_KEY, now)


def lyrics_if_due(admin_plex, plex_section: str, interval: int) -> None:
    """Once per interval: lyrics files for songs that have none (lyrics_files.py),
    then their albums refreshed in Plex so the new files show."""
    import lyrics_files
    if not lyrics_files.enabled():
        return
    now = datetime.now(timezone.utc)
    if not is_due(LYRICS_KEY, now, interval):
        return
    try:
        lyrics_files.backfill()
    finally:
        write_last_run(LYRICS_KEY, now)
    try:
        lyrics_files.show_in_plex(admin_plex, admin_plex.library.section(plex_section))
    except Exception as exc:
        logger.warning(f"Could not have Plex show the new lyrics: {exc}")


def picked_for_you_if_due(admin_plex, mapping, plex_section: str, interval: int) -> None:
    """Once per interval: every user's "Picked for You" playlist, made on their
    own account from their own plays and ratings (taste.refresh_for_you)."""
    import taste
    if not taste.for_you_enabled():
        return
    now = datetime.now(timezone.utc)
    if not is_due(FOR_YOU_KEY, now, interval):
        return
    names = users.plex_usernames(mapping)
    try:
        for username in names:
            server = _server_for(admin_plex, names, username)
            if server is None:
                logger.info(f"🎯 No Plex connection as '{username}': their '{taste.for_you_playlist_name()}' waits")
                continue
            try:
                taste.refresh_for_you(admin_plex, server, plex_section, username, is_admin=username == names[0])
            except Exception as exc:
                logger.warning(f"'{taste.for_you_playlist_name()}' failed for '{username}': {exc}")
    finally:
        write_last_run(FOR_YOU_KEY, now)


def _work_due(mapping, interval: int) -> bool:
    """Whether a job that moves songs runs in this pass."""
    now = datetime.now(timezone.utc)
    keys = [LIBRARY_KEY, MERGE_KEY] + ([] if dry_run() else users.plex_usernames(mapping))
    return any(is_due(key, now, interval) for key in keys)


def _server_for(admin_plex, names: List[str], username: str):
    """A user's own Plex connection: the admin's for the mapping's first user,
    the Home user's otherwise; None when Plex won't switch to them."""
    if not names or username == names[0]:
        return admin_plex
    try:
        return admin_plex.switchUser(username)
    except Exception as exc:
        logger.debug(f"switchUser('{username}') failed: {exc}")
        return None


def keep_playlists(admin_plex, mapping, plex_section: str, interval: int, moving: bool = False) -> None:
    """Songs moved since the last poll go back in their playlists once Plex
    knows their new files; before a pass that moves songs (`moving`: fixes
    from Debugarr are about to be applied), every user's playlists are noted
    (playlist_keeper.py)."""
    names = users.plex_usernames(mapping)

    def server_for(username: str):
        return _server_for(admin_plex, names, username)

    playlist_keeper.settle(server_for, admin_plex.library.section(plex_section))
    if moving or _work_due(mapping, interval):
        servers = {name: server_for(name) for name in names}
        noted = playlist_keeper.snapshot({k: v for k, v in servers.items() if v is not None})
        logger.info(f"📋 Noted {noted} playlist(s), so songs moved in this pass keep their places")


def run_pass(admin_plex, mapping, plex_section: str, interval: int) -> None:
    global _files_changed
    _files_changed = 0
    state = manual_fix.load_state()
    try:
        # Fixes Debugarr has for Manual Fix songs, and songs the admin closed there.
        waiting = [] if dry_run() else debug_issues.waiting(state)
        try:
            keep_playlists(admin_plex, mapping, plex_section, interval,
                           moving=any(issue.get('status') == 'fix_ready' for issue in waiting))
        except Exception as exc:
            logger.warning(f"Playlist upkeep failed: {exc}")
        if waiting:
            try:
                _count_changes(debug_issues.act(admin_plex, state, waiting))
            except Exception as exc:
                logger.warning(f"Applying Debugarr's fixes failed: {exc}")
        if not dry_run() and debug_issues.enabled():
            try:
                debug_issues.verify_applied(admin_plex, state, plex_section)
            except Exception as exc:
                logger.warning(f"Checking Debugarr's fixes in Plex failed: {exc}")
        try:
            delete_playlist_if_due(admin_plex, plex_section, interval)
        except Exception as exc:
            logger.warning(f"Delete playlist pass failed: {exc}")
        unresolved = process_due_users(admin_plex, mapping, plex_section, interval)
        unresolved.extend(repair_library_if_due(admin_plex, plex_section, interval, state))
        if unresolved:
            manual_fix.add_tracks(admin_plex, unresolved, state)
        try:
            merge_albums_if_due(admin_plex, interval)
        except Exception as exc:
            logger.warning(f"Album reconcile failed: {exc}")
        # After Manual Fix has taken the entries it needs (the scan drops the
        # moved songs' old Plex items).
        rescan_if_changed(admin_plex, plex_section)
    finally:
        # In a dry run nothing goes to Debugarr: its fixes would not be applied.
        manual_fix.send_pending_notifications(state, None if dry_run() else admin_plex)
        manual_fix.save_state(state)
    try:
        picked_for_you_if_due(admin_plex, mapping, plex_section, interval)
    except Exception as exc:
        logger.warning(f"Picked for You pass failed: {exc}")
    # Last: the lyrics lookups take a while and hold nothing above back.
    try:
        lyrics_if_due(admin_plex, plex_section, interval)
    except Exception as exc:
        logger.warning(f"Lyrics pass failed: {exc}")


def main() -> int:
    plex_url = os.environ.get('PLEX_URL', '').strip()
    if not plex_url:
        logger.error("PLEX_URL not set; fix-metadata daemon cannot run")
        return 1

    from plexapi.server import PlexServer

    interval = interval_seconds()
    section_name = os.environ.get('PLEX_MUSIC_SECTIONS', 'Music').split(':')[0].strip() or 'Music'
    wait = poll_seconds(interval)
    logger.info(
        f"Daily metadata repair on library '{section_name}': every {interval}s "
        f"(checking every {wait}s); up to {library_scan_limit()} library tracks per pass"
    )

    admin_plex = None
    while True:
        try:
            # Downloads and moves since the last poll were made as root; Lidarr
            # can only rename and re-tag them once they are the owner's again.
            library_owner.adopt()
        except Exception as exc:
            logger.warning(f"Could not hand new library files to their owner: {exc}")
        try:
            mapping = users.load_wizarr_mapping() or {}
            if not mapping:
                logger.warning("No users in the Wizarr mapping yet; retrying later")
                time.sleep(wait)
                continue
            if admin_plex is None:
                admin_plex = PlexServer(plex_url, plex_auth.admin_token())
            run_pass(admin_plex, mapping, section_name, interval)
        except Exception as exc:
            logger.warning(f"Metadata repair pass failed ({type(exc).__name__}: {exc}); reconnecting on next pass")
            admin_plex = None
        time.sleep(wait)


if __name__ == '__main__':
    import metadata_proxy
    metadata_proxy.install()          # lookups outside the VPN, downloads in it
    sys.exit(main())
