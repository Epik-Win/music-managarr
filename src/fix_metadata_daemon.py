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
   keeps the admin's notice about it up to date (manual_fix.py).
4. Reconciles albums (album_artist_merger.lidarr_reconcile): folds copies of
   an album into Lidarr's version, removes duplicate songs, and merges albums
   Plex has split in two. It runs after step 2 so albums whose tags were just
   repaired are merged the same day.

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

import library_owner
import manual_fix
import metadata_repair
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


def run_pass(admin_plex, mapping, plex_section: str, interval: int) -> None:
    global _files_changed
    _files_changed = 0
    state = manual_fix.load_state()
    try:
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
        manual_fix.send_pending_notifications(state)
        manual_fix.save_state(state)


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
