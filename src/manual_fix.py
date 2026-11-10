"""The admin's "Manual Fix" Plex playlist and the admin's notice about it.

Tracks whose metadata neither Lidarr nor audio fingerprinting could work out
(from the library scan, or flagged by a user in their "Fix Metadata" playlist)
are added here for the admin to fix by hand. The playlist is created with the
admin's own token and no switchUser(), so it belongs to the admin and no Home
user can see it.

Each daily pass also removes tracks whose metadata is now complete, so the
playlist is the admin's to-do list. The admin gets one short private notice
(admin_notify.py, in their requests thread) when tracks are added, replacing
the previous one, and it is removed once the playlist is empty. What the
admin was told is kept in DATA_DIR, so nothing is announced twice.
"""
import json
import logging
import os
from typing import Dict, List, Optional

import admin_notify
from metadata_repair import needs_repair, track_label, track_meta

logger = logging.getLogger('musicai.manual_fix')

PLAYLIST_NAME = 'Manual Fix'
NOTIFICATION_ID = 'manual-fix'


def _state_path() -> str:
    return os.path.join(os.environ.get('DATA_DIR', '/app/data'), 'manual_fix_state.json')


def load_state() -> Dict:
    try:
        with open(_state_path(), 'r', encoding='utf-8') as handle:
            state = json.load(handle)
        if isinstance(state, dict):
            state.setdefault('open', {})
            state.setdefault('resolved_unsent', [])
            return state
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"Could not read Manual Fix state, starting fresh: {exc}")
    return {'open': {}, 'resolved_unsent': []}


def save_state(state: Dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def find_playlist(admin_plex):
    for playlist in admin_plex.playlists():
        if playlist.title == PLAYLIST_NAME:
            return playlist
    return None


def playlist_keys(admin_plex) -> set:
    playlist = find_playlist(admin_plex)
    if playlist is None:
        return set()
    return {str(item.ratingKey) for item in playlist.items()}


def add_tracks(admin_plex, entries: List[Dict[str, str]], state: Dict) -> int:
    """Add unresolved tracks to the playlist (creating it on first use, since
    Plex will not create an empty one) and record them as not yet DM'd."""
    playlist = find_playlist(admin_plex)
    seen = {str(item.ratingKey) for item in playlist.items()} if playlist else set()
    new_items, new_entries = [], []
    for entry in entries:
        key = entry['rating_key']
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            new_items.append(admin_plex.fetchItem(int(key)))
            new_entries.append(entry)
        except Exception as exc:
            logger.warning(f"Could not load track {key} for Manual Fix: {exc}")
    if not new_items:
        return 0
    if playlist is None:
        admin_plex.createPlaylist(PLAYLIST_NAME, items=new_items)
    else:
        playlist.addItems(new_items)
    for entry in new_entries:
        state['open'][entry['rating_key']] = {
            'label': entry['label'],
            'reason': entry['reason'],
            'source': entry['source'],
            'notified': False,
        }
    logger.info(f"📝 Added {len(new_items)} track(s) to the admin's '{PLAYLIST_NAME}' playlist")
    return len(new_items)


def prune_fixed(admin_plex, state: Dict) -> int:
    """Remove tracks that were fixed; delete the playlist once it is empty so
    the admin's list does not keep a stale entry.

    Complete-looking names are not enough: a song handed over for a doubtful
    artist (a conductor credited for a soloist's concerto) has complete names
    from the start. It counts as fixed once its names differ from those it
    was handed over with — or, handed over only for missing artwork, once it
    has some."""
    playlist = find_playlist(admin_plex)
    if playlist is None:
        return 0
    items = playlist.items()

    def still_as_flagged(item) -> bool:
        record = state['open'].get(str(item.ratingKey))
        return bool(record) and 'artwork' not in str(record.get('reason', '')).lower() \
            and record.get('label') == track_label(track_meta(item))

    fixed = [item for item in items if needs_repair(item) is None and not still_as_flagged(item)]
    if fixed:
        playlist.removeItems(fixed)
        for item in fixed:
            record = state['open'].pop(str(item.ratingKey), None)
            # Only report a fix for something the admin was told about.
            if record and record.get('notified'):
                state['resolved_unsent'].append(track_label(track_meta(item)))
        logger.info(f"✅ Removed {len(fixed)} fixed track(s) from '{PLAYLIST_NAME}'")
    remaining = {str(item.ratingKey) for item in items} - {str(item.ratingKey) for item in fixed}
    # Tracks Plex dropped from the playlist (deleted files, or removed by the
    # admin by hand) are no longer anything to track.
    for key in list(state['open']):
        if key not in remaining:
            state['open'].pop(key)
    if not remaining:
        playlist.delete()
    return len(fixed)


def send_pending_notifications(state: Dict) -> None:
    """When tracks are added to the playlist, replace the admin's notice with
    a short note saying so (no track list: the playlist is the list). The
    notice is removed once the playlist is empty."""
    unsent = [rec for rec in state['open'].values() if not rec.get('notified')]
    if unsent:
        if not admin_notify.resolve_notification(NOTIFICATION_ID):
            return
        message = (
            f"🎵 {len(unsent)} new track(s) added to your '{PLAYLIST_NAME}' playlist in Plex "
            f"({len(state['open'])} waiting in total)."
        )
        if not admin_notify.notify_admin(message, NOTIFICATION_ID):
            return
        for rec in unsent:
            rec['notified'] = True
    elif not state['open'] and state['resolved_unsent']:
        if not admin_notify.resolve_notification(NOTIFICATION_ID):
            return
    state['resolved_unsent'] = []