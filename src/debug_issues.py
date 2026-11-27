"""Manual Fix songs handed to Debugarr, whose local AI tries them first.

With DEBUGGARR_URL set, every song the metadata repair hands to the admin's
Manual Fix playlist (manual_fix.py) is reported to Debugarr as an issue: what
is missing or wrong, what MusicAI tried, and the evidence it has (Plex's
names, the file's tags, folders and length, what fingerprinting heard, and
what Deezer and Lidarr list under its names). The songs of a pass go in one
batch, which Debugarr works through as one task: the local AI looks for the
right artist, album and title, and once all of it is settled the admin gets
one private message about the whole batch, what was fixed and what needs
them. The notice in the admin's requests thread is not used then. Missing
covers and wrong recordings need the admin, since there is nothing for the
AI to fill in.

The fix's current values are the names Plex shows, which are what Manual
Fix checks; the file's own names are part of the evidence. A fix comes back
here and is applied the way every repair is (tags, folders, cover, Plex
refresh). The local AI's answer is written only when a catalogue backs it:
Deezer or Spotify lists that song by that artist within a few seconds of the
file's length (or, for a song that exists nowhere else, the artist's own
upload does), on that album or on one Lidarr lists for the artist. The
admin's own values are written as given. The fix is reported fixed once Plex
shows the new names, and failed, with what Plex shows, if it doesn't within
VERIFY_SECONDS.

A song that leaves Manual Fix (fixed another way, deleted, taken off by
hand) closes its issue; an issue the admin closes takes the song off.
"""
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

logger = logging.getLogger('musicai.debug_issues')

SOURCE = 'music-managarr'
ISSUE_PREFIX = 'manual-fix-'
MAX_FACT = 300
VERIFY_SECONDS = 3600      # how long Plex gets to show a written fix
BATCH = 100                # Debugarr's largest batch

TITLES = {
    'metadata': 'Song names need a look',
    'artwork': 'Album cover missing',
    'audio': 'Wrong recording in the library',
}
NEEDS_PERSON = {
    'artwork': ("No cover was found for it on Deezer, iTunes, Spotify, YouTube Music or YouTube, so one has to "
                "be added by hand: in Plex, the album's Edit → Poster."),
    'audio': ("The file isn't the released recording, and the release couldn't be downloaded: replace the file "
              "by hand, or delete the song (your Delete playlist) and request it again."),
}
FIX_DESCRIPTION = ("The song's names as its release lists them. Plex shows names that looked wrong; the file's tags "
                   "and folders, fingerprinting and the catalogue listings in the evidence show what the release "
                   "is. MusicAI writes the names to the file's tags, files it under that artist and album, and "
                   "fetches the album's cover.")
FIELD_DESCRIPTIONS = {
    'artist': ("The act that performs the song: a soloist rather than the orchestra or conductor, the main "
               "artist without featured guests."),
    'album': "The release the recording is on; a single's album is named after the single.",
    'title': "The song's title as released, without video words such as Official Video or Lyrics.",
}


# ── Settings and requests ────────────────────────────────────────────────────

def base_url() -> str:
    """DEBUGGARR_URL without a path: the API's own address, or any of its
    endpoints (http://debuggarr:8095/api/notify), works."""
    url = os.environ.get('DEBUGGARR_URL', '').strip().rstrip('/')
    return re.sub(r'/api(?:/.*)?$', '', url)


def _secret() -> str:
    """DEBUGGARR_API_SECRET, or the file DEBUGGARR_API_SECRET_FILE names
    (the secret alone, or Debugarr's own env file with API_SECRET=)."""
    secret = os.environ.get('DEBUGGARR_API_SECRET', '').strip()
    path = os.environ.get('DEBUGGARR_API_SECRET_FILE', '').strip()
    if secret or not path:
        return secret
    try:
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
    except OSError:
        return ''
    for line in text.splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip() == 'API_SECRET':
            return value.strip().strip('"\'')
    return '' if '=' in text else text.strip()


def enabled() -> bool:
    return bool(base_url() and _secret())


def _call(method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Optional[dict]]:
    """(HTTP status, JSON reply); status 0 when Debugarr can't be reached."""
    try:
        resp = requests.request(method, f'{base_url()}{path}', json=body,
                                headers={'X-API-Key': _secret()}, timeout=20)
    except requests.RequestException as exc:
        logger.warning(f"Debugarr unreachable ({type(exc).__name__}); trying again on the next poll")
        return 0, None
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400:
        error = data.get('error') if isinstance(data, dict) else ''
        logger.warning(f"Debugarr refused {method} {path.split('?')[0]}: HTTP {resp.status_code} {error or ''}")
    return resp.status_code, data


def issue_id(rating_key: str) -> str:
    return f'{ISSUE_PREFIX}{rating_key}'


def _issue_path(rating_key: str) -> str:
    return f'/api/issues/{SOURCE}/{quote(issue_id(rating_key), safe="")}'


# ── What MusicAI knows about a song ──────────────────────────────────────────

def kind_of(reason: str) -> str:
    """What the admin is needed for: names ('metadata'), a cover
    ('artwork'), or the recording itself ('audio')."""
    text = (reason or '').lower()
    if 'the audio is' in text:
        return 'audio'
    if 'artwork' in text or 'no cover' in text:
        return 'artwork'
    return 'metadata'


def _minutes(seconds) -> str:
    try:
        seconds = int(round(float(seconds)))
    except (TypeError, ValueError):
        return '?'
    return f'{seconds // 60}:{seconds % 60:02d}'


def _clip(text: str) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= MAX_FACT else text[:MAX_FACT - 1] + '…'


def _attempts(record: Dict) -> List[Dict[str, str]]:
    """What MusicAI tried before handing the song over, from where it was
    found and why it was handed over (the reason itself is the problem)."""
    reason, source = record.get('reason', ''), record.get('source', '')
    kind = kind_of(reason)
    if kind == 'artwork':
        return [{'method': 'cover search on Deezer, iTunes, Spotify, YouTube Music and YouTube',
                 'result': 'no cover found'}]
    if kind == 'audio':
        return [{'method': 'download of the released recording', 'result': 'it could not be downloaded'}]
    if reason.startswith('filed under the wrong artist'):
        return [{'method': "fingerprinting the album's first song", 'result': "it couldn't tell which artist is right"}]
    if source.startswith('flagged by') or source == 'Fix Metadata':
        return [{'method': ("Fix Metadata check: fingerprinting several parts of the song, Deezer and Spotify "
                            "by its names and length, its release's album and cover"),
                 'result': 'no answer it could trust'}]
    if source == 'library scan':
        return [{'method': ("daily library repair: Deezer by the file's ISRC, Lidarr's catalogue (also with the "
                            "folder names), audio fingerprinting, MusicBrainz by length"),
                 'result': 'no complete match'}]
    return [{'method': source or 'metadata repair', 'result': reason}]


def _source_fact(source: str) -> str:
    if source.startswith('flagged by '):
        return f"Flagged by {source[len('flagged by '):]} in their Fix Metadata playlist"
    if source == 'library scan':
        return 'Found by the daily library scan'
    return f'Handed over by: {source}'


def _file_tags(file_path: str) -> Dict[str, str]:
    try:
        from mutagen import File as MutagenFile
        tags = MutagenFile(file_path, easy=True)
    except Exception:
        return {}
    if not tags:
        return {}
    out = {}
    for key in ('artist', 'albumartist', 'album', 'title', 'date', 'tracknumber', 'isrc'):
        value = tags.get(key)
        if value:
            out[key] = ' / '.join(str(v) for v in value)
    return out


def _deezer(meta: Dict[str, str], file_path: str) -> List[Dict]:
    """What Deezer lists under the song's possible names, top results first.
    Only songs whose title matches one of those names are kept: a search
    also returns unrelated songs that share a word with it."""
    from artwork import _plain, _search_artist
    from fix_metadata import _same_title
    from metadata_repair import artist_known, name_candidates
    artist = meta.get('artist') if artist_known(meta.get('artist')) else ''
    found, seen = [], set()
    for title in name_candidates(meta, file_path)[:3]:
        query = f'{_search_artist(artist)} {_plain(title)}' if artist else _plain(title)
        try:
            rows = requests.get('https://api.deezer.com/search', params={'q': query, 'limit': 5},
                                timeout=10).json().get('data') or []
        except Exception as exc:
            logger.debug(f"Deezer search for {query!r} failed: {exc}")
            continue
        for row in rows:
            if row.get('id') in seen or not _same_title(row.get('title', ''), title):
                continue
            seen.add(row.get('id'))
            found.append({'artist': (row.get('artist') or {}).get('name', ''), 'title': row.get('title', ''),
                          'album': (row.get('album') or {}).get('title', ''), 'length_seconds': row.get('duration')})
    return found[:10]


def _lidarr(meta: Dict[str, str]) -> List[Dict]:
    """Albums Lidarr's catalogue has under the song's artist and names."""
    from metadata_repair import lidarr_lookup
    artist = meta.get('artist', '')
    terms = [f'{artist} {meta[k]}'.strip() for k in ('title', 'album') if meta.get(k)]
    found = []
    for term in terms:
        for candidate in lidarr_lookup(term)[:5]:
            item = {'artist': candidate['artist'], 'album': candidate['album'],
                    'kind': ', '.join(candidate.get('secondary') or []) or 'album'}
            if item not in found:
                found.append(item)
    return found[:8]


def _plex_link(admin_plex, rating_key: str) -> str:
    machine = getattr(admin_plex, 'machineIdentifier', '')
    if not machine:
        return ''
    return (f'https://app.plex.tv/desktop/#!/server/{machine}/details'
            f'?key={quote(f"/library/metadata/{rating_key}", safe="")}')


def build_report(admin_plex, rating_key: str, record: Dict) -> Optional[Dict]:
    """The issue for one Manual Fix song, or None when Plex no longer has it."""
    from fix_metadata import _file_path
    from metadata_repair import audio_duration, known_meta, track_label, track_meta
    try:
        track = admin_plex.fetchItem(int(rating_key))
    except Exception as exc:
        logger.info(f"  Manual Fix song {rating_key} is not in Plex ({exc}); not reported")
        return None
    file_path = _file_path(track)
    shown = track_meta(track)
    meta = known_meta(track, file_path) if file_path else shown
    reason = record.get('reason', '')
    kind = kind_of(reason)

    plex = {'title': getattr(track, 'title', '') or '', 'album': getattr(track, 'parentTitle', '') or '',
            'album_artist': getattr(track, 'grandparentTitle', '') or '',
            'track_artist': getattr(track, 'originalTitle', '') or '',
            'year': getattr(track, 'parentYear', None) or getattr(track, 'year', None),
            'length_seconds': round((getattr(track, 'duration', 0) or 0) / 1000)}
    evidence: Dict = {'plex': plex, 'musicai_reading': meta, 'fingerprints': record.get('heard') or []}
    facts = [_source_fact(record.get('source', ''))]
    facts.append(f"Plex shows: {plex['track_artist'] or plex['album_artist'] or '—'} – {plex['title'] or '—'} "
                 f"[{plex['album'] or '—'}]")
    if file_path and not _same(track_label(meta), track_label(shown)):
        facts.append(f"MusicAI reads the file as: {track_label(meta)}")
    if file_path:
        length = audio_duration(file_path)
        tags = _file_tags(file_path)
        folders = file_path.split(os.sep)[-3:]
        evidence['file'] = {'name': folders[-1], 'artist_folder': folders[0] if len(folders) == 3 else '',
                            'album_folder': folders[-2] if len(folders) >= 2 else '',
                            'length_seconds': round(length) if length else None, 'tags': tags}
        facts.append(f"File: {'/'.join(folders)} ({_minutes(length)})")
        if tags:
            facts.append('File tags: ' + ', '.join(f'{k} "{tags[k]}"' for k in ('artist', 'albumartist', 'album', 'title')
                                                   if tags.get(k)))
        if kind == 'metadata':
            evidence['deezer_by_name'] = _deezer(meta, file_path)
            evidence['lidarr_by_name'] = _lidarr(meta)
    heard = record.get('heard') or []
    if heard:
        facts.append('Fingerprinting heard: ' + '; '.join(
            f"{h.get('artist')} – {h.get('title')}" + (f" [{h['album']}]" if h.get('album') else '')
            + (f" ({h['service']})" if h.get('service') else '') for h in heard[:3]))
    listed = evidence.get('deezer_by_name') or []
    if listed:
        facts.append('Deezer lists: ' + '; '.join(
            f"{d['artist']} – {d['title']} [{d['album']}] ({_minutes(d['length_seconds'])})" for d in listed[:3]))

    report = {
        'source': SOURCE,
        'issue_id': issue_id(rating_key),
        'kind': kind,
        'title': TITLES[kind],
        'subject': _clip(record.get('label') or track_label(meta)),
        'problem': reason[:1000] or 'its metadata needs a look',
        'facts': [_clip(f) for f in facts],
        'attempts': [{'method': a['method'][:200], 'result': a['result'][:500]} for a in _attempts(record)],
        'evidence': evidence,
    }
    link = _plex_link(admin_plex, rating_key)
    if link:
        report['link'] = link
    if kind == 'metadata':
        report['fix'] = {'description': FIX_DESCRIPTION, 'fields': [
            {'name': name, 'current': (shown.get(name) or '')[:300], 'required': True,
             'description': FIELD_DESCRIPTIONS[name]}
            for name in ('artist', 'album', 'title')]}
    else:
        report['needs_person'] = NEEDS_PERSON[kind]
    return report


# ── Talking to Debugarr ──────────────────────────────────────────────────────

def report_open(admin_plex, state: Dict) -> int:
    """Report every Manual Fix song Debugarr doesn't have yet, all in one
    batch: Debugarr works through a batch as one task and messages the
    admin once about all of it, when it is settled. Returns how many were
    reported; none is marked reported unless Debugarr took the batch."""
    pending = []
    for key, record in state['open'].items():
        if record.get('issue'):
            continue
        payload = build_report(admin_plex, key, record)
        if payload is not None:
            pending.append((key, payload))
    reported = 0
    for start in range(0, len(pending), BATCH):
        chunk = pending[start:start + BATCH]
        status, data = _call('POST', '/api/issues/batch', {'source': SOURCE, 'issues': [p for _, p in chunk]})
        if status != 200:
            break
        codes = {item.get('issue_id'): item.get('code') for item in (data or {}).get('issues') or []}
        for key, payload in chunk:
            state['open'][key]['issue'] = codes.get(payload['issue_id']) or True
            state['open'][key]['notified'] = True
            reported += 1
    if reported:
        logger.info(f"🛠️ Reported {reported} Manual Fix song(s) to Debugarr as one batch")
    return reported


def close_finished(state: Dict) -> None:
    """Close the issues of songs that left Manual Fix (manual_fix queues them)."""
    left = []
    for key, detail in state.get('issues_to_close') or []:
        status, _ = _call('POST', f'{_issue_path(key)}/resolve', {'detail': detail})
        if status == 0 or status >= 500:
            left.append([key, detail])
    state['issues_to_close'] = left


def waiting(state: Dict) -> List[Dict]:
    """Issues Debugarr has for MusicAI to act on: fixes to apply, and songs
    the admin closed. Empty when Debugarr is off or can't be reached."""
    if not enabled():
        return []
    status, data = _call('GET', f'/api/issues?source={SOURCE}&status=fix_ready,closed')
    if status != 200 or not isinstance(data, dict):
        return []
    tracked = {issue_id(key) for key, record in state['open'].items() if record.get('issue')}
    return [issue for issue in data.get('issues') or []
            if issue.get('status') == 'fix_ready' or issue.get('issue_id') in tracked]


def act(admin_plex, state: Dict, issues: List[Dict]) -> int:
    """Apply each fix (verify_applied reports it once Plex shows it), or
    report why it can't be; take songs the admin closed off Manual Fix.
    Returns how many files were changed."""
    import manual_fix
    changed = 0
    for issue in issues:
        name = issue.get('issue_id') or ''
        key = name[len(ISSUE_PREFIX):] if name.startswith(ISSUE_PREFIX) else ''
        record = state['open'].get(key)
        if record is None or not record.get('issue'):
            if issue.get('status') == 'fix_ready':
                _call('POST', f'/api/issues/{SOURCE}/{quote(name, safe="")}/resolve',
                      {'detail': 'the song is no longer in Manual Fix'})
            continue
        if issue.get('status') == 'closed':
            manual_fix.drop(admin_plex, state, [key])
            logger.info(f"📝 The admin closed {record.get('label')} in Debugarr; taken off '{manual_fix.PLAYLIST_NAME}'")
            continue
        if record.get('applied'):
            continue                     # written; waiting for Plex to show it
        by_person = issue.get('proposed_by') == 'person'
        who = "the admin's values" if by_person else "the local AI's answer"
        applied, detail, names = apply(admin_plex, key, issue.get('solution') or {}, by_person)
        if not applied:
            _call('POST', f'{_issue_path(key)}/result', {'outcome': 'failed', 'detail': detail})
            logger.info(f"🛠️ {record.get('label')}: {who} not applied — {detail}")
            continue
        record['applied'] = {'names': names, 'detail': detail, 'at': time.time()}
        changed += 1
        logger.info(f"🛠️ {record.get('label')}: applied {who} — {detail}")
    return changed


def _same(a: str, b: str) -> bool:
    return ' '.join((a or '').casefold().split()) == ' '.join((b or '').casefold().split())


def _shows(meta: Dict[str, str], names: Dict[str, str]) -> bool:
    """Whether Plex shows the written names: the same title and album, and
    the same artist or Plex's own spelling of it (Plex's agent may spell an
    act its own way, a few letters apart from the tag)."""
    from metadata_repair import MATCH_THRESHOLD, similar
    return (_same(meta.get('title', ''), names['title']) and _same(meta.get('album', ''), names['album'])
            and (_same(meta.get('artist', ''), names['artist'])
                 or similar(meta.get('artist', ''), names['artist']) >= MATCH_THRESHOLD))


def _shown_label(admin_plex, section_name: str, rating_key: str, names: Dict[str, str]) -> str:
    """The expected label when Plex shows the written names (on the song's
    item, or the item of its moved file); else what Plex shows instead: the
    old item, or the moved file's new item ('' when Plex has neither)."""
    from metadata_repair import track_label, track_meta
    expected, shown = track_label(names), ''
    try:
        meta = track_meta(admin_plex.fetchItem(int(rating_key)))
        if _shows(meta, names):
            return expected
        shown = track_label(meta)
    except Exception:
        pass
    try:
        found = admin_plex.library.section(section_name).searchTracks(title=names['title'])
    except Exception as exc:
        logger.debug(f"Plex search for {names['title']!r} failed: {exc}")
        found = []
    for item in found or []:
        meta = track_meta(item)
        if _shows(meta, names):
            return expected
        if not shown and _same(meta.get('title', ''), names['title']) and _same(meta.get('album', ''), names['album']):
            shown = track_label(meta)
    return shown


def verify_applied(admin_plex, state: Dict, section_name: str) -> None:
    """Fixes written earlier: once Plex shows the new names, the issue is
    fixed and the song leaves Manual Fix; if Plex still doesn't after
    VERIFY_SECONDS, the fix failed, saying what Plex shows instead (a song
    Plex files under another artist's album needs re-filing in Plex)."""
    import manual_fix
    from metadata_repair import track_label
    for key, record in list(state['open'].items()):
        applied = record.get('applied')
        if not applied:
            continue
        expected = track_label(applied['names'])
        shown = _shown_label(admin_plex, section_name, key, applied['names'])
        if _same(shown, expected):
            status, _ = _call('POST', f'{_issue_path(key)}/result', {'outcome': 'fixed', 'detail': applied['detail']})
            if status:
                manual_fix.drop(admin_plex, state, [key])
                logger.info(f"✅ Plex shows {expected}: taken off '{manual_fix.PLAYLIST_NAME}'")
            continue
        if time.time() - applied['at'] < VERIFY_SECONDS:
            continue
        minutes = VERIFY_SECONDS // 60
        detail = (f'MusicAI wrote {expected} to the file, but {minutes} minutes later Plex still shows '
                  + (shown or 'nothing for the song'))
        status, _ = _call('POST', f'{_issue_path(key)}/result', {'outcome': 'failed', 'detail': detail})
        if status:
            record.pop('applied', None)
            logger.info(f"🛠️ {detail}")


# ── Applying a fix ───────────────────────────────────────────────────────────

def confirm(meta: Dict[str, str], duration: Optional[float]) -> Tuple[bool, str]:
    """Whether a catalogue backs these names for a file of this length: the
    song listed at this length, on this album or one Lidarr lists for the
    artist. (confirmed, what backs it or why nothing does)."""
    from artwork import own_upload_matches
    from metadata_repair import MATCH_THRESHOLD, lidarr_confirms_album, release_listing, similar, titles_agree
    artist, album, title = meta['artist'], meta['album'], meta['title']
    if not duration:
        return False, "the file's length could not be read, so no catalogue can confirm the names"
    listing = release_listing(artist, title, duration)
    if listing:
        where = listing['source']
        if similar(listing['album'], album) >= MATCH_THRESHOLD:
            return True, f"{where} lists it on that album at this length"
        if lidarr_confirms_album(artist, album):
            return True, f"{where} lists the song at this length and Lidarr lists the album"
        return False, (f'{where} lists the song at this length, but on "{listing["album"]}", and Lidarr lists '
                       f'no "{album}" by {artist}')
    if own_upload_matches(artist, title, duration):
        if titles_agree(album, title) or lidarr_confirms_album(artist, album):
            return True, "the artist's own upload is this length"
        return False, f'only the artist\'s own upload has it, and Lidarr lists no "{album}" by {artist}'
    return False, (f'neither Deezer nor Spotify lists "{title}" by {artist} at the file\'s length '
                   f'({_minutes(duration)}), and no upload by the artist matches it')


def write(track, file_path: str, meta: Dict[str, str]) -> Tuple[bool, str, Dict[str, str]]:
    """Write the names and file the song the way a Fix Metadata fix does: a
    file Lidarr owns stays where Lidarr keeps it, a song Lidarr knows gets
    Lidarr's folder, naming and titles, anything else joins its album.
    (written, the artwork outcome, the names written)."""
    from artwork import ensure_artwork
    from fix_metadata import (_file_away, _file_tags, _refresh_in_plex, _reorganize_file, _same_title,
                              _tell_lidarr, _title_core, _write_tags)
    tagged = _file_tags(file_path) or {}
    names, join_dir, join_name = _file_away(file_path, meta)
    if not _write_tags(file_path, names):
        return False, '', names
    new_path = _reorganize_file(file_path, names, target_dir=join_dir, target_name=join_name)
    if join_name:
        _tell_lidarr(join_dir)
    # A song whose identity changed gets its own release's cover.
    changed = not (_same_title(tagged.get('title', ''), names['title'])
                   and _title_core(tagged.get('artist', '')) == _title_core(names['artist']))
    try:
        art = ensure_artwork(new_path, names, (), replace=changed)
    except Exception as exc:
        logger.warning(f"  Artwork check failed for {new_path}: {exc}")
        art = 'unchecked'
    _refresh_in_plex(track)
    return True, art, names


def apply(admin_plex, rating_key: str, solution: Dict[str, str],
          by_person: bool) -> Tuple[bool, str, Dict[str, str]]:
    """Write a fix's artist, album and title to the song. (applied, what was
    done or why not, the names written)."""
    from fix_metadata import _file_path
    from metadata_repair import audio_duration, is_complete, track_label
    meta = {name: ' '.join(str(solution.get(name) or '').split()) for name in ('artist', 'album', 'title')}
    try:
        track = admin_plex.fetchItem(int(rating_key))
    except Exception:
        return False, 'the song is no longer in Plex', meta
    file_path = _file_path(track)
    if not file_path:
        return False, "the song's file is not on disk", meta
    if not is_complete(meta):
        return False, 'artist, album and title are all needed (a placeholder such as "Unknown" does not count)', meta
    backing = 'as you gave them'
    if not by_person:
        confirmed, backing = confirm(meta, audio_duration(file_path))
        if not confirmed:
            return False, backing, meta
    written, art, names = write(track, file_path, meta)
    if not written:
        return False, "the tags could not be written to the file", meta
    detail = f'wrote {track_label(names)} ({backing})'
    if art == 'not found':
        # The daily library scan hands the album over again for its cover.
        detail += '; no cover was found yet'
    return True, detail, names
