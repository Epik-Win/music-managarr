"""Albums Plex files under an artist their own files don't name.

Plex decides a local album's artist once, from the tags of the file that
created the album, and keeps it. Re-tagging the files later does not move the
album, and neither does moving them: Plex treats a moved file as the same
item. Songs that arrive untagged are grouped under whichever artist created
the album, so a few artists can collect dozens of unrelated albums whose
files are tagged correctly later.

Detection (`find_misfiled`): a local (unmatched) album whose Plex artist is not
credited in any artist tag of its first track. Albums Plex matched online are
left alone: their artist comes from Plex's catalogue, which may spell a name
its own way ("Berliner Philharmoniker" for "Berlin Philharmonic").

A combined Plex artist ("Cloonee/Young M.a/Innt") whose files name one of
its artists alone is COMBINED: the album belongs under that artist. An artist
renamed or credited in a longer form ("RÜFÜS" / "RÜFÜS DU SOL") is not
misfiled. Files whose artist tag is missing or a placeholder ("Unknown",
"Lost") are PLACEHOLDER: Plex doesn't file those under an unknown artist but
under some real one, which is how sink artists fill up. metadata_repair
looks them up, or parks them under "Unidentified".

Which side is wrong: tags can be wrong too (an album artist set from folder
names stamps the folder's artist on every song in it). So the verdict (`Misfiled.verdict`) is:
  * SINK — the Plex artist holds misfiled albums from two or more different
    artists. One artist really collecting several unrelated artists' albums
    is how the untagged-file grouping fails, so Plex is wrong.
  * UNCLEAR — a single stray album; metadata_repair fingerprints it and the
    answer decides which side to correct.

Re-filing (`refile`): Plex only regroups a song when it sees a new file. The
albums' files are moved out of the library into a hidden staging folder,
Plex rescans and empties its trash so the old items are gone, then the files
are moved back and Plex rescans again, filing them by their tags. A new item
loses what Plex kept on the old one, so every account's ratings, play counts
and playlist entries (appended at the end) are captured first and put back
by file path, and the old added dates are restored so the albums don't flood
Recently Added. A manifest in the staging folder lets the next run return any
file a crash left there. Off unless PLEX_REFILE_ENABLED=true; otherwise
misfiled albums are only reported.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger('musicai.plex_refile')

SINK = 'sink'
COMBINED = 'combined'
PLACEHOLDER = 'placeholder'
UNCLEAR = 'unclear'
# Tag keys that name artists, besides artist / album artist themselves.
# Taggers such as MusicBrainz Picard write these and nothing here rewrites
# them, so they keep the credits when album-artist tags are rewritten in bulk.
OTHER_ARTIST_KEYS = ('album artist', 'album_artist', 'albumartists', 'albumartist_credit',
                     'artists', 'artist_credit', 'performer')
STAGE_DIRNAME = '.musicai_refile'
MANIFEST = 'manifest.json'
SCAN_TIMEOUT = 900


def enabled() -> bool:
    return os.environ.get('PLEX_REFILE_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes', 'on')


# ── names ───────────────────────────────────────────────────────────────────

def fold(value: Optional[str]) -> str:
    """Case-, accent- and punctuation-insensitive form, without a leading
    "The" ("Donny Benét" = "Donny Benet", "The Smooth Jazz Alley" = "Smooth
    Jazz Alley")."""
    text = unicodedata.normalize('NFKD', value or '')
    text = ''.join(c for c in text if not unicodedata.combining(c)).casefold().strip()
    text = re.sub(r'^the\s+', '', text)
    return re.sub(r'[\W_]+', '', text)


def names(value: Optional[str]) -> Set[str]:
    """The whole credit plus every artist in it, folded."""
    from metadata_repair import credited_artists
    whole = fold(value)
    parts = {fold(p) for p in credited_artists(value)}
    return {n for n in parts | {whole} if n}


def _joined(tags, key) -> str:
    try:
        value = tags.get(key) if tags is not None else None
    except Exception:
        value = None
    if not value:
        return ''
    if isinstance(value, (list, tuple)):
        return '/'.join(str(v) for v in value if v)
    return str(value)


def file_artists(path: str) -> Dict[str, str]:
    """{'albumartist', 'artist', 'other'} as tagged in the file ('' if absent).
    Multi-value tags are joined with '/', so "Becky G/Natti Natasha" stays whole."""
    try:
        from mutagen import File as MuFile
        easy = MuFile(path, easy=True)
        raw = MuFile(path)
    except Exception:
        return {}
    if easy is None:
        return {}
    other = ''
    raw_tags = getattr(raw, 'tags', None)
    for key in OTHER_ARTIST_KEYS:
        other = _joined(raw_tags, key)
        if other:
            break
    return {
        'albumartist': _joined(easy, 'albumartist'),
        'artist': _joined(easy, 'artist'),
        'other': other,
    }


def credits(tags: Dict[str, str]) -> Set[str]:
    found: Set[str] = set()
    for key in ('albumartist', 'artist', 'other'):
        found |= names(tags.get(key))
    return found


# ── detection ───────────────────────────────────────────────────────────────

@dataclass
class Misfiled:
    album: object                      # plexapi Album
    tracks: List[object]
    plex_artist: str
    tagged_artist: str                 # album artist (else artist) the files name
    verdict: str = UNCLEAR
    paths: List[str] = field(default_factory=list)   # container paths

    @property
    def label(self) -> str:
        return f"{getattr(self.album, 'title', '')!r} (Plex: {self.plex_artist}, tags: {self.tagged_artist})"


def _track_paths(track) -> List[str]:
    from paths import host_to_container
    out = []
    for media in getattr(track, 'media', None) or ():
        for part in getattr(media, 'parts', None) or ():
            if getattr(part, 'file', None):
                out.append(host_to_container(part.file))
    return out


def _same_act(a: str, b: str) -> bool:
    """One name inside the other, word for word: an artist's old and new name
    ("RÜFÜS" / "RÜFÜS DU SOL") or a credited orchestra. Not misfiled."""
    from metadata_repair import name_within
    return name_within(a, b) or name_within(b, a)


def classify(plex_artist: str, tags: Dict[str, str]) -> Optional[str]:
    """None if the album sits under an artist its tags credit; PLACEHOLDER if
    the files name no real artist; COMBINED if
    Plex's artist is a combined credit ("Cloonee/Young M.a/Innt") and the
    files name one of those artists alone; otherwise UNCLEAR (a misfiled
    album, which find_misfiled may upgrade to SINK)."""
    from metadata_repair import artist_known
    tagged = tags.get('albumartist') or tags.get('artist') or ''
    if not plex_artist or not artist_known(plex_artist):
        return None
    if not artist_known(tagged):
        # The files name nobody (or "Unknown"); Plex made the artist up.
        return PLACEHOLDER
    if fold(tagged) == fold(plex_artist):
        return None
    # Only a list joined the way Plex joins multi-value tags ("/", ";"):
    # "Dana and the Wolf" or "Grover Washington, Jr." is one act's name.
    parts = [p for p in re.split(r'\s*[/;]\s*', plex_artist) if p.strip()]
    if len(parts) > 1 and fold(tagged) in {fold(p) for p in parts}:
        return COMBINED
    if credits(tags) & names(plex_artist) or _same_act(tagged, plex_artist):
        return None
    return UNCLEAR


def find_misfiled(section, read_tags: Callable[[str], Dict[str, str]] = file_artists) -> List[Misfiled]:
    """Every local album whose Plex artist none of its file's artist tags name."""
    by_album: Dict[str, List[object]] = {}
    for track in section.searchTracks():
        by_album.setdefault(str(getattr(track, 'parentRatingKey', '')), []).append(track)

    found: List[Misfiled] = []
    for album in section.albums():
        if not str(getattr(album, 'guid', '') or '').startswith('local://'):
            continue
        tracks = by_album.get(str(album.ratingKey)) or []
        paths = [p for t in tracks for p in _track_paths(t)]
        first = next((p for p in paths if os.path.exists(p)), None)
        if not first:
            continue
        tags = read_tags(first)
        plex_artist = getattr(album, 'parentTitle', '') or ''
        verdict = classify(plex_artist, tags)
        if verdict is None:
            continue
        tagged = tags.get('albumartist') or tags.get('artist') or ''
        found.append(Misfiled(album, tracks, plex_artist, tagged, verdict=verdict, paths=paths))

    # A Plex artist (by record, since one name can have several records)
    # holding misfiled albums from several different artists is a sink.
    tagged_by_artist: Dict[str, Set[str]] = {}
    for m in found:
        if m.verdict == UNCLEAR:
            tagged_by_artist.setdefault(str(getattr(m.album, 'parentRatingKey', m.plex_artist)), set()).add(fold(m.tagged_artist))
    for m in found:
        if m.verdict == UNCLEAR and len(tagged_by_artist[str(getattr(m.album, 'parentRatingKey', m.plex_artist))]) >= 2:
            m.verdict = SINK
    return found


# ── re-filing ───────────────────────────────────────────────────────────────

def stage_root() -> str:
    from paths import container_music_root
    return os.path.join(container_music_root(), STAGE_DIRNAME)


def _load_manifest(root: str) -> Dict[str, str]:
    try:
        with open(os.path.join(root, MANIFEST), encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_manifest(root: str, manifest: Dict[str, str]) -> None:
    os.makedirs(root, exist_ok=True)
    tmp = os.path.join(root, MANIFEST + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(root, MANIFEST))


def _prepare_stage(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    ignore = os.path.join(root, '.plexignore')
    if not os.path.exists(ignore):
        with open(ignore, 'w') as fh:
            fh.write('*\n')


def restore_staged(root: Optional[str] = None) -> int:
    """Move every staged file back to where it came from. Returns how many."""
    root = root or stage_root()
    manifest = _load_manifest(root)
    returned = 0
    for staged, original in list(manifest.items()):
        if os.path.exists(staged):
            if os.path.exists(original):
                logger.warning(f"  Not returning {staged}: {original} exists again; left in staging")
                continue
            os.makedirs(os.path.dirname(original), exist_ok=True)
            shutil.move(staged, original)
            returned += 1
        manifest.pop(staged)
    if manifest:
        _save_manifest(root, manifest)
    elif os.path.exists(os.path.join(root, MANIFEST)):
        os.remove(os.path.join(root, MANIFEST))
    if os.path.isdir(root):
        for name in os.listdir(root):
            batch = os.path.join(root, name)
            if os.path.isdir(batch) and not os.listdir(batch):
                os.rmdir(batch)
    return returned


def _wait_for_scan(section, timeout: int = SCAN_TIMEOUT) -> bool:
    """Wait until Plex has started and finished scanning the section."""
    time.sleep(5)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            section.reload()
            if not getattr(section, 'refreshing', False):
                return True
        except Exception as exc:
            logger.debug(f"  waiting for Plex scan: {exc}")
        time.sleep(5)
    logger.warning(f"  Plex was still scanning after {timeout}s")
    return False


def _gone(server, key) -> bool:
    try:
        server.fetchItem(int(key))
        return False
    except Exception:
        return True


# ── what a re-created item would lose ──────────────────────────────────────
#
# A re-created local track is a new Plex item: every account's rating, play
# count and playlist entries for the old one are gone, and the album shows up
# as new in Recently Added. All of it is captured first and put back after.

def _chunks(items: List[str], size: int = 100):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def admin_identities(server) -> Set[str]:
    """Every name the admin goes by, casefolded. The server reports the admin
    by email (myPlexUsername), while the user mapping uses the Plex username
    ("alice"), so the account's username, title and email and the
    DISCORD_TO_PLEX_MAPPING_ADMIN entry all count."""
    names = {getattr(server, 'myPlexUsername', '') or ''}
    try:
        import plex_auth
        account = plex_auth.admin_account()
        names |= {getattr(account, key, '') or '' for key in ('username', 'title', 'email')}
    except Exception as exc:
        logger.debug(f"  could not read the admin account: {exc}")
    try:
        import users
        _, plex_admin = users.parse_admin_override(os.environ.get('DISCORD_TO_PLEX_MAPPING_ADMIN'))
        names.add(plex_admin or '')
    except Exception:
        pass
    return {n.strip().casefold() for n in names if n and n.strip()}


def accounts_for(server) -> List[Tuple[str, object]]:
    """(name, PlexServer) for the admin and every Home user in the Wizarr
    mapping: ratings and playlists are kept per account."""
    found = [('admin', server)]
    try:
        import plex_auth
        import users
        from plexapi.server import PlexServer
        admin_names = admin_identities(server)
        url = getattr(server, '_baseurl', None) or os.environ.get('PLEX_URL', '')
        for name in users.plex_usernames(users.load_wizarr_mapping() or {}):
            if not name or name.casefold() in admin_names:
                continue
            token = plex_auth.home_user_token(url, name)
            if token:
                found.append((name, PlexServer(url, token)))
    except Exception as exc:
        logger.warning(f"  Re-file: Home users unreachable ({exc}); only the admin's history is kept")
    return found


def _fetch(server, keys: Iterable[str]) -> List[object]:
    items: List[object] = []
    for chunk in _chunks([str(k) for k in keys]):
        items.extend(server.fetchItems('/library/metadata/' + ','.join(chunk)))
    return items


def capture_history(accounts: List[Tuple[str, object]], track_keys: Iterable[str]) -> Dict[str, Dict[str, dict]]:
    """{account: {'ratings': {path: (rating, plays)}, 'playlists': {path: [playlist]}}}."""
    keys = {str(k) for k in track_keys}
    history: Dict[str, Dict[str, dict]] = {}
    for name, server in accounts:
        ratings: Dict[str, Tuple[Optional[float], int]] = {}
        lists: Dict[str, List[object]] = {}
        try:
            for track in _fetch(server, keys):
                for path in _track_paths(track):
                    ratings[path] = (getattr(track, 'userRating', None), getattr(track, 'viewCount', 0) or 0)
            for playlist in server.playlists(playlistType='audio'):
                if getattr(playlist, 'smart', False):
                    continue
                for item in playlist.items():
                    if str(getattr(item, 'ratingKey', '')) in keys:
                        for path in _track_paths(item):
                            lists.setdefault(path, []).append(playlist)
        except Exception as exc:
            logger.warning(f"  Re-file: could not read {name}'s history: {exc}")
        history[name] = {'ratings': ratings, 'playlists': lists}
    return history


def restore_history(accounts: List[Tuple[str, object]], history: Dict[str, Dict[str, dict]],
                    new_keys: Dict[str, str]) -> Dict[str, int]:
    """Put ratings, plays and playlist entries back on the re-created tracks."""
    done = {'ratings': 0, 'plays': 0, 'playlist entries': 0}
    for name, server in accounts:
        kept = history.get(name) or {}
        ratings, lists = kept.get('ratings', {}), kept.get('playlists', {})
        wanted = {path: key for path, key in new_keys.items()
                  if (ratings.get(path, (None, 0)) != (None, 0)) or lists.get(path)}
        if not wanted:
            continue
        try:
            by_path = {p: t for t in _fetch(server, set(wanted.values())) for p in _track_paths(t)}
        except Exception as exc:
            logger.warning(f"  Re-file: could not restore {name}'s history: {exc}")
            continue
        for path, track in by_path.items():
            rating, plays = ratings.get(path, (None, 0))
            try:
                if rating and getattr(track, 'userRating', None) != rating:
                    track.rate(rating)
                    done['ratings'] += 1
                for _ in range(max(0, plays - (getattr(track, 'viewCount', 0) or 0))):
                    track.markPlayed()      # each scrobble counts one play
                    done['plays'] += 1
                for playlist in lists.get(path, []):
                    playlist.addItems([track])
                    done['playlist entries'] += 1
            except Exception as exc:
                logger.debug(f"  could not restore {name}'s history for {path}: {exc}")
    return done


def _restore_added_dates(server, albums: List['Misfiled'], new_keys: Dict[str, str]) -> None:
    """Keep re-filed albums out of Recently Added: give the new items the old
    albums' and tracks' added dates."""
    try:
        by_path = {p: t for t in _fetch(server, set(new_keys.values())) for p in _track_paths(t)}
    except Exception as exc:
        logger.debug(f"  could not read re-created tracks: {exc}")
        return
    for m in albums:
        old_added = {p: getattr(t, 'addedAt', None) for t in m.tracks for p in _track_paths(t)}
        new_albums = set()
        for path, added in old_added.items():
            track = by_path.get(path)
            if track is None or added is None:
                continue
            try:
                track.editAddedAt(added)
                new_albums.add(str(getattr(track, 'parentRatingKey', '')))
            except Exception as exc:
                logger.debug(f"  could not keep added date of {path}: {exc}")
        album_added = getattr(m.album, 'addedAt', None) or min((a for a in old_added.values() if a), default=None)
        for key in new_albums - {''}:
            try:
                if album_added:
                    server.fetchItem(int(key)).editAddedAt(album_added)
            except Exception as exc:
                logger.debug(f"  could not keep added date of album {key}: {exc}")


def refile(section, albums: Iterable[Misfiled], root: Optional[str] = None,
           accounts: Optional[List[Tuple[str, object]]] = None) -> Dict[str, object]:
    """Make Plex re-create these albums from their tags. Returns
    {'refiled': [(label, new artist)], 'failed': [(label, why)], 'restored': {...}}."""
    from paths import host_to_container
    albums = [m for m in albums if m.paths]
    report: Dict[str, object] = {'refiled': [], 'failed': [], 'restored': {}}
    if not albums:
        return report
    root = root or stage_root()
    restore_staged(root)
    _prepare_stage(root)
    server = getattr(section, '_server', None)
    accounts = accounts if accounts is not None else (accounts_for(server) if server is not None else [])
    history = capture_history(accounts, [str(t.ratingKey) for m in albums for t in m.tracks])

    manifest = _load_manifest(root)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    try:
        for i, m in enumerate(albums):
            for j, path in enumerate(m.paths):
                if not os.path.exists(path):
                    continue
                staged = os.path.join(root, stamp, f'{i:04d}-{j:03d}-{os.path.basename(path)}')
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                shutil.move(path, staged)
                manifest[staged] = path
                _save_manifest(root, manifest)
        logger.info(f"  Re-file: {len(manifest)} file(s) of {len(albums)} album(s) staged; rescanning Plex")
        section.update()
        _wait_for_scan(section)
        section.emptyTrash()
        _wait_for_scan(section)
        for m in albums:
            if server is not None and not _gone(server, m.album.ratingKey):
                report['failed'].append((m.label, 'Plex kept the old album after the rescan'))
    finally:
        returned = restore_staged(root)
        logger.info(f"  Re-file: {returned} file(s) returned; rescanning Plex")
        section.update()
        _wait_for_scan(section)

    paths = {p for m in albums for p in m.paths}
    new_tracks: Dict[str, object] = {}
    for track in section.searchTracks():
        for path in _track_paths(track):
            if host_to_container(path) in paths:
                new_tracks[host_to_container(path)] = track
    new_keys = {p: str(t.ratingKey) for p, t in new_tracks.items()}
    report['restored'] = restore_history(accounts, history, new_keys)
    if server is not None:
        _restore_added_dates(server, albums, new_keys)
    failed = {label for label, _ in report['failed']}
    for m in albums:
        if m.label in failed:
            continue
        now = next((new_tracks[p] for p in m.paths if p in new_tracks), None)
        if now is None:
            report['failed'].append((m.label, 'not back in Plex yet'))
        else:
            report['refiled'].append((m.label, getattr(now, 'grandparentTitle', '?')))
    return report
