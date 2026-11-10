"""fix_metadata.py — Correct metadata for tracks in a user's 'Fix Metadata' Plex playlist.

Users flag a track as wrong by adding it to their own "Fix Metadata" playlist.
fix_metadata_daemon.py calls run_fix_metadata() once a day per user.

Flow
----
1.  Receive the already-connected admin PlexServer from fix_metadata_daemon.py.
2.  plex_admin.switchUser(plex_username)  → user_plex
    (Blank username, or the admin's own name, which switchUser rejects, falls
    back to plex_admin.)
3.  user_plex.library.section(plex_music_section)  → music_library
4.  Look for a playlist named "Fix Metadata" via user_plex.playlists(). If it
    doesn't exist, create it (Plex refuses an empty playlist, so it is created
    with one track which is then removed).
5.  snapshot_tracks = playlist.items()
6.  For each plex_track in snapshot_tracks:
        a. current = {artist, album, title} from track attributes.
        b. file_path from track.media[0].parts[0].file.
        c. best, next_best = identify_track_by_audio_with_alternatives(file_path)
           Fingerprinting comes first here, unlike the library scan in
           metadata_repair.py: the user is saying the current tags are wrong,
           so a Lidarr lookup of those same tags would only confirm them.
           Mojibake in the answers is repaired (text_repair).
        d. Choose correction:
             best differs from current in >=1 field  → use best
             best == current (all fields match)       → use next_best (must differ)
             nothing distinct found                   → the flag may not be about
                 names: garbled current tags are repaired; a track with no
                 artwork gets it (artwork.ensure_artwork); only if neither
                 applies does it go to the admin.
        e. If the correction has no album, choose one with
           metadata_repair.pick_album (Lidarr-confirmed albums first).
        f. Write corrected tags (artist, album artist, album, title).
        g. Reorganize file to correct folder structure: {artist}/{album}/{title}.{ext},
           bringing the old folder's cover art along.
        h. Make sure the track has artwork (embedded and cover.jpg).
        i. Refresh the track and its album in Plex.
        j. playlist.removeItems([plex_track]) — signals item is done.
7.  Return {'fixed', 'skipped', 'failed', 'unresolved'}. 'unresolved' lists the
    tracks nothing could identify (removed from the user's playlist); the
    daemon puts them in the admin's "Manual Fix" playlist (manual_fix.py).

Public symbol used by fix_metadata_daemon.py:
    run_fix_metadata(plex_admin_server, plex_username, plex_music_section) -> dict
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

FIX_PLAYLIST_NAME = "Fix Metadata"
_META_FIELDS = ("artist", "title", "album")


from paths import host_to_container as convert_host_path_to_container_path

# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _all_match(current: dict, candidate: dict) -> bool:
    """True only when all three fields are identical (case-insensitive)."""
    return all(
        _norm(current.get(f, "")) == _norm(candidate.get(f, ""))
        for f in _META_FIELDS
    )


def _known_fields(meta: dict) -> dict:
    """The fields that carry information: placeholders become blanks."""
    from metadata_repair import album_known, artist_known, title_known
    return {
        "artist": meta.get("artist", "") if artist_known(meta.get("artist")) else "",
        "album": meta.get("album", "") if album_known(meta.get("album")) else "",
        "title": meta.get("title", "") if title_known(meta.get("title")) else "",
    }


def _title_core(value: str) -> str:
    from metadata_repair import _core_title, _norm_match
    return _norm_match(_core_title(value))


def _usable_answer(answer: Optional[dict]) -> bool:
    """A fingerprint answer worth using: it names an artist and a title, the
    artist isn't the song's own title padded out (cover uploads), and the
    title isn't an upload's "Song - Artist" name repeating the artist (Shazam
    knows a copy of Amr Mostafa's "Law Fi Hayati" as "Marmar - Arabic (6)" –
    "Law Fy 7aiaty - Marmar - Arabic (6)", the very junk the file carried)."""
    from metadata_repair import artist_known, title_known
    if not answer or not artist_known(answer.get("artist")) or not title_known(answer.get("title")):
        return False
    title, artist = _title_core(answer.get("title", "")), _title_core(answer.get("artist", ""))
    if len(title) >= 4 and title in artist:
        return False
    raw = answer.get("title", "")
    if " - " in raw:
        head, tail = raw.split(" - ", 1)
        first, last = raw.rsplit(" - ", 1)
        if len(artist) >= 3 and any(_title_core(side) == artist for side in (head, tail, first, last)):
            return False
    return True


def _exact(value: str) -> str:
    """A title as written, ignoring case, spacing and quote styles."""
    import unicodedata
    text = unicodedata.normalize("NFKC", value or "").casefold().replace("’", "'").replace("‘", "'")
    return " ".join(text.split())


def _same_title(a: str, b: str) -> bool:
    """Same song title, allowing one to be a longer form of the other
    ("Don't Take Your Guns" / "Don't Take Your Guns to Town"); the shorter
    must be at least five letters so "Go" doesn't match everything."""
    from metadata_repair import name_within, titles_agree
    if not a or not b:
        return False
    if titles_agree(a, b):
        return True
    if "*" in a or "*" in b:
        # Censored spellings ("Sh*t Talk" / "S**t Talk" / "Shit Talk"):
        # each "*" stands for one letter.
        def pattern(v):
            return "".join("." if c == "*" else re.escape(c) for c in re.sub(r"[^\w*]+", "", v.casefold()))
        def letters(v):
            return re.sub(r"[^\w*]+", "", v.casefold())
        x, y = letters(a), letters(b)
        if re.fullmatch(pattern(a), y) or re.fullmatch(pattern(b), x):
            return True
        if len(x) == len(y) and all(p == q or "*" in (p, q) for p, q in zip(x, y)):
            return True
    short, long_ = sorted((a, b), key=lambda v: len(_title_core(v)))
    return len(_title_core(short)) >= 5 and name_within(short, long_)


def _confirms(known: dict, answer: dict) -> bool:
    """The answer is the song the tags already name. That includes a
    re-upload: the same title under an alias nobody follows, when the tagged
    artist is a real act (a known artist's single answered as some unknown
    uploader's — stream-farm copies of the same audio)."""
    from metadata_repair import artists_agree, is_established_artist, is_known_artist
    if not known["title"] or not _same_title(known["title"], answer.get("title", "")):
        return False
    if not known["artist"] or artists_agree(known["artist"], answer.get("artist")):
        return True
    return (answer.get("service") != "ISRC" and not is_known_artist(answer.get("artist"))
            and is_established_artist(known["artist"]))


def _supported(known: dict, answer: dict, answers: list, names: Optional[list] = None) -> bool:
    """An answer naming something other than the tags is trusted when it is
    the same song under other names (same title, or artist and title swapped:
    "Decline – Chiddy Bang" is Chiddy Bang's "Decline"), when the tags had
    no title to contradict it, or when a second service agrees on the title."""
    from metadata_repair import artists_agree
    title = answer.get("title", "")
    if answer.get("unconfirmed"):
        return False                      # a guess no lyrics confirm: a person decides
    if (answer.get("votes") or 1) >= 2:
        return True                       # several slices or services agree
    # The file or folder names the same song ("Karol G & Drake - Ahi/song.mp3"
    # for Shazam's "KAROL G & Drake – Ahí").
    if any(_same_title(n, title) for n in names or ()):
        return True
    if any(known[f] and _same_title(known[f], title) for f in ("title", "artist", "album")):
        return True
    if not known["title"] and (not known["artist"] or artists_agree(known["artist"], answer.get("artist"))):
        return True
    return any(a is not answer and _same_title(title, a.get("title", "")) for a in answers)


def _sampled(known: dict, answer: dict, file_path: str) -> Optional[int]:
    """The length of the answer's own recording when it only shares the
    file's music: the tagged song is released at the file's length and the
    song the services name is not. Crack Amico's "Cracker Barrel Diss" (165 s
    on Spotify and his own channel) raps over the beat of ния's 149 s
    "хороша", which Shazam heard on every slice. None when that can't be shown."""
    from artwork import own_upload_matches
    from metadata_repair import audio_duration, is_complete, isrc_length, release_listing
    if answer.get("service") == "ISRC" or not is_complete(known) or _confirms(known, answer):
        return None
    file_len = audio_duration(file_path)
    other = answer.get("duration") or isrc_length(answer.get("isrc"))
    if not file_len or not other or abs(other - file_len) <= TRUE_AUDIO_TOLERANCE:
        return None
    if release_listing(known["artist"], known["title"], file_len) is None \
            and not own_upload_matches(known["artist"], known["title"], file_len):
        return None
    return int(other)


def _enrich_from_deezer(answers: list, file_len: Optional[float]) -> None:
    """Give answers that name their Deezer track (ACRCloud) or ISRC the
    release's own album and cover, looked up directly: Deezer's search can
    hide a song from the VPN's exit country, and a search by names then
    lands on a compilation's cover. The album changes only when the answer
    had none, had a compilation, or named the same release longer."""
    from artwork import _same
    from metadata_repair import artists_agree, credited_artists, deezer_release, looks_like_compilation, name_within
    for a in answers:
        if a.get("service") == "ISRC" or not (a.get("deezer_id") or a.get("isrc")):
            continue
        # Its own link to the recording; an edit of another length shares the
        # release's names and cover (the audio is checked on its own).
        rel = deezer_release(a, file_len, tolerance=30)
        if not rel or not _same_title(rel["title"], a.get("title", "")) \
                or not artists_agree(rel["artist"], a.get("artist")):
            continue
        # ACRCloud splits a duo's name into parts ("Dana, the Wolf"); Deezer
        # names the act ("Dana and the Wolf").
        parts = credited_artists(a.get("artist"))
        if len(parts) > 1 and all(name_within(part, rel["artist"]) for part in parts):
            a["artist"] = rel["artist"]
        mine, theirs = a.get("album", ""), rel["album"]
        if theirs and not looks_like_compilation(theirs) and (
                not mine or looks_like_compilation(mine) or (_same(mine, theirs) and len(theirs) < len(mine))):
            a["album"] = theirs
        if rel["cover_art_url"] and theirs and _exact(a.get("album", "")) == _exact(theirs):
            a["cover_art_url"] = rel["cover_art_url"]     # a deluxe edition's cover can differ


def _merge_answer(known: dict, chosen: dict, answers: list, same_song: bool = False,
                  file_path: Optional[str] = None) -> dict:
    """New tags from the chosen answer, keeping what the answers support:

    * artist — the tagged artist when the answer credits it inside a
      collaboration ("Kid Cudi" for "Kid Cudi & Eminem"), otherwise the
      answer's primary artist (its spelling wins), so a collaboration never
      becomes an artist folder;
    * title — the tagged title when a service returned it exactly (services
      shorten long titles, so the full tagged one is kept when one confirms it);
    * album — pick_album over every service's album for the song, else the
      tagged one, else the release Lidarr has.
    """
    from metadata_repair import (album_known, deezer_albums_for_track, lidarr_album_for_track, main_artist,
                                 name_within, pick_album, titles_agree)
    from metadata_repair import artists_agree as _agree
    answer_artist = (chosen.get("artist") or "").strip()
    main = main_artist(answer_artist) or answer_artist
    reupload = same_song and known["artist"] and not _agree(known["artist"], answer_artist)
    if reupload:
        artist = known["artist"]          # the answer is a re-upload under an alias
    elif main != answer_artist and known["artist"] and name_within(known["artist"], answer_artist):
        artist = known["artist"]          # one of the collaborators, as tagged
    else:
        artist = main
    title = (chosen.get("title") or "").strip() or known["title"]
    # ...returned by a service naming the same song (a losing answer for a
    # different song doesn't vouch for the tagged title).
    from metadata_repair import artists_agree
    vouching = [a for a in answers if a is chosen or (artists_agree(a.get("artist"), chosen.get("artist"))
                                                       and _same_title(a.get("title", ""), chosen.get("title", "")))]
    if known["title"] and (reupload or any(_exact(a.get("title", "")) == _exact(known["title"]) for a in vouching)):
        title = known["title"]
    # A featured credit the tags don't have is not added to the title: Lidarr
    # writes its own title back, so "Song (feat. X)" from Shazam would only
    # flip back and forth. (One the tags have can go: "September (feat. Fire)"
    # is a split "Earth, Wind & Fire".)
    from song_dedup import _FEAT_RE
    if known["title"] and _FEAT_RE.sub("", known["title"]) == known["title"] \
            and _exact(_FEAT_RE.sub("", title)) == _exact(known["title"]):
        title = known["title"]
    # Album, in order: the tagged one when any source names the same album;
    # one Lidarr confirms as a real (official, non-compilation) release;
    # Deezer's first listing for the song (its ranking puts the canonical
    # release first); then pick_album over the fingerprint services' albums.
    # Left to tie-breaks, the local AI can pick a fan compilation over the
    # original album, and Shazam alone often names bootlegs.
    from song_dedup import variant_labels
    from metadata_repair import lidarr_confirms_album, looks_like_compilation
    labels = variant_labels(chosen.get("title", ""))
    fingerprinted = [a.get("album") for a in answers
                     if album_known(a.get("album")) and (a is chosen or (titles_agree(title, a.get("title"))
                                                                        and variant_labels(a.get("title", "")) == labels))]
    from metadata_repair import audio_duration
    duration = audio_duration(file_path) if file_path else None
    deezer = [a for a in deezer_albums_for_track(artist, chosen.get("title") or title, duration)[:3]
              if not looks_like_compilation(a)]
    everything = deezer + fingerprinted
    confirmed = [a for a in everything if lidarr_confirms_album(artist, a)]
    # Brackets are ignored except a version: the tagged "RISE (instrumental)"
    # is not the services' "RISE" for a song that is no instrumental.
    if known["album"] and any(_title_core(a) == _title_core(known["album"])
                              and variant_labels(a) == variant_labels(known["album"]) for a in everything):
        album = known["album"]
    elif confirmed:
        album = pick_album(artist, title, confirmed) or confirmed[0]
    elif deezer:
        album = deezer[0]
    else:
        album = pick_album(artist, title, fingerprinted) or "" if fingerprinted else ""
    if not album:
        # The tagged album belongs to the tagged song: kept first only when
        # the answer is that song; for a different song Lidarr's album for it
        # comes first and the flagged one is the last resort.
        if same_song:
            album = known["album"] or lidarr_album_for_track(artist, title) or ""
        else:
            album = lidarr_album_for_track(artist, title) or known["album"] or ""
    from metadata_repair import _REMASTER_SUFFIX
    return {"artist": artist, "album": _REMASTER_SUFFIX.sub("", album), "title": title}


def _on_probation(file_path: str) -> bool:
    """A song MusicAI picked on its own that has not graduated yet (library_state)."""
    import library_state
    return library_state.state_of(file_path) == library_state.PROBATION


def _is_video_title(text: str) -> bool:
    """An album tag that is a YouTube video's name ("Divine of the Dey
    "BabyFace" [OFFICIAL VIDEO] Directed by Jayonez"): the song is a single
    wherever it was filed."""
    from metadata_repair import _VIDEO_WORDS
    return bool(text and _VIDEO_WORDS.search(text))


def _clean_title(title: str) -> str:
    """A title without "(Official Video)"-style noise."""
    from metadata_repair import _FILENAME_NOISE
    return re.sub(r"\s+", " ", _FILENAME_NOISE.sub("", title or "")).strip(" -_") or (title or "")


TRUE_AUDIO_TOLERANCE = 5           # seconds a file may differ from the release


def _download_reference(ref: dict, staging: str) -> Optional[str]:
    import geo_relay
    from downloader import Downloader
    d = Downloader(None)
    if ref["source"] == "deezer":
        return d.download_deezer_track(ref["id"], staging)
    try:
        ok, path = d._download_with_yt_dlp(f"https://www.youtube.com/watch?v={ref['id']}", staging)
    finally:
        # A bot wall can move the container-wide VPN relay; put it back so
        # the next download does not inherit the detour.
        geo_relay.restore()
    return path if ok else None


def _swap_in(old_path: str, new_path: str) -> str:
    """Put the downloaded file where the old one was (same name, the new
    file's extension); the old file goes to quarantine, never deleted."""
    import shutil
    data_dir = os.environ.get("DATA_DIR", "/app/data")
    quarantine = os.path.join(data_dir, "fixmeta_quarantine", "replaced_audio", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(quarantine, exist_ok=True)
    shutil.move(old_path, os.path.join(quarantine, os.path.basename(old_path)))
    target = os.path.splitext(old_path)[0] + os.path.splitext(new_path)[1].lower()
    shutil.move(new_path, target)
    import library_state
    library_state.moved(old_path, target)
    return target


def _replace_wrong_audio(plex_track, fp, known, answers, playlist, stats, label, plex_username,
                         names_fixed: bool = False) -> bool:
    """When the names hold but the audio is not the true recording, replace
    the audio with the release (Deezer) or the artist's own upload, keeping
    the old file in quarantine. True if the song was handled."""
    from artwork import ensure_artwork
    from metadata_repair import audio_duration, reference_version, track_label
    if not (known.get("artist") and known.get("title")):
        return False
    file_len = audio_duration(fp)
    ref = reference_version(known["artist"], known["title"]) if file_len else None
    if not ref or abs(file_len - ref["duration"]) <= TRUE_AUDIO_TOLERANCE:
        return False
    # A different known song in the audio means the names are what's wrong.
    if any(((a.get("votes") or 1) >= 2 or a.get("service") == "ISRC") and not _confirms(known, a) for a in answers):
        return False
    confirms = any(_confirms(known, a) for a in answers)
    if ref["source"] != "deezer" and not confirms and file_len > 2 * ref["duration"]:
        # Nothing vouches for the file and the upload is a fraction of it: an
        # excerpt or a talk about the work more likely than the recording.
        logger.info(f"    the {ref['source']} upload ({ref['duration']:.0f}s) is far shorter than the file "
                    f"({file_len:.0f}s) and nothing identifies the file: not replaced")
        return False
    kind = ("a music-video cut" if confirms and file_len > ref["duration"]
            else "a truncated copy" if file_len < ref["duration"]
            else "not this recording (no service recognises it)")
    logger.info(f"    {'with the names corrected' if names_fixed else 'names hold but'} the audio is {kind}: "
                f"{file_len:.0f}s against the {ref['source']} version's {ref['duration']:.0f}s — fetching the true recording")
    staging = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "fixmeta_staging", time.strftime("%Y%m%d-%H%M%S"))
    new_file = _download_reference(ref, staging)
    new_len = audio_duration(new_file) if new_file and os.path.exists(new_file) else None
    if not new_len or abs(new_len - ref["duration"]) > 3:
        # The names were judged right, so the services' names for this wrong
        # audio must not be written instead (a commentary clip would become
        # "Song (Commentary)"). A person gets the song.
        where = f"Deezer track {ref['id']}" if ref["source"] == "deezer" else f"YouTube video {ref['id']}"
        reason = (f"the names hold but the audio is {kind} ({file_len:.0f}s); the true recording "
                  f"({where}, {ref['duration']:.0f}s) could not be downloaded")
        logger.warning(f"  {label}: {reason} — handing to admin")
        stats["unresolved"].append({
            "rating_key": str(getattr(plex_track, "ratingKey", "")), "label": track_label(known),
            "reason": reason, "source": f"flagged by {plex_username or 'admin'}"})
        try:
            playlist.removeItems([plex_track])
        except Exception as exc:
            logger.debug(f"    removeItems: {exc}")
        return True
    target = _swap_in(fp, new_file)
    meta = dict(known)
    if ref["source"] == "deezer" and ref.get("album"):
        meta["album"] = ref["album"]
    if not meta.get("album"):
        meta["album"] = meta["title"]          # a single, as every single here is filed
    meta, join_dir, join_name = _file_away(target, meta)
    _write_tags(target, meta)
    target = _reorganize_file(target, meta, target_dir=join_dir, target_name=join_name)
    if join_name:
        _tell_lidarr(join_dir)
    # The downloaded release's own cover first: a search by names can land
    # on another edition's.
    art = ensure_artwork(target, meta, [ref["cover"]] if ref.get("cover") else (), replace=True)
    _refresh_in_plex(plex_track)
    try:
        playlist.removeItems([plex_track])
    except Exception as exc:
        logger.debug(f"    removeItems: {exc}")
    stats["fixed"] += 1
    logger.info(f"  DONE {label}: replaced {kind} with the {ref['source']} recording ({new_len:.0f}s) → "
                f"{track_label(meta)}; artwork: {art}")
    if art == "not found":
        stats["unresolved"].append({
            "rating_key": str(getattr(plex_track, "ratingKey", "")), "label": track_label(meta),
            "reason": "audio replaced with the true recording, but no cover was found anywhere",
            "source": f"flagged by {plex_username or 'admin'}"})
    return True


def _file_tags(file_path: str) -> Optional[dict]:
    """artist (the album artist when there is one) / album / title exactly as
    tagged in the file ('' when absent); None if the file can't be read."""
    try:
        from mutagen import File as MutagenFile
        tags = MutagenFile(file_path, easy=True)
    except Exception:
        tags = None
    if tags is None:
        return None
    first = lambda key: ((tags.get(key) or [""])[0] or "").strip()
    return {"artist": first("albumartist") or first("artist"), "album": first("album"), "title": first("title")}


def _current_meta(plex_track) -> dict:
    """Pull artist/album/title off a Plex Track object."""
    return {
        "artist": (getattr(plex_track, "grandparentTitle", "") or "").strip(),
        "album":  (getattr(plex_track, "parentTitle",      "") or "").strip(),
        "title":  (getattr(plex_track, "title",            "") or "").strip(),
    }


def _file_path(plex_track) -> Optional[str]:
    """Return the on-disk path of the first media part, or None.

    If the path from Plex doesn't exist (the file was renamed and Plex hasn't
    rescanned), the file in the same directory is used — but only when it is
    the only audio file there and as long as Plex's track, since any other
    choice could be a different song and would get this track's tags written
    onto it. (After one song of a soundtrack is moved out, the other left in
    its folder is not the stale track's file.)
    """
    from metadata_repair import audio_duration
    audio_extensions = {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.opus'}
    plex_seconds = (getattr(plex_track, "duration", None) or 0) / 1000
    try:
        for media in (plex_track.media or []):
            for part in (media.parts or []):
                fp = getattr(part, "file", None)
                if not fp:
                    continue
                container_fp = convert_host_path_to_container_path(fp)
                if os.path.isfile(container_fp):
                    return container_fp
                dir_path = os.path.dirname(container_fp)
                try:
                    audio = [
                        name for name in os.listdir(dir_path)
                        if os.path.splitext(name)[1].lower() in audio_extensions
                    ]
                except OSError:
                    audio = []
                if len(audio) == 1:
                    candidate = os.path.join(dir_path, audio[0])
                    length = audio_duration(candidate)
                    if plex_seconds and length and abs(length - plex_seconds) <= 2:
                        return candidate
                    logger.info(f"    {os.path.basename(container_fp)} is gone and {audio[0]} "
                                f"({length or 0:.0f}s, Plex says {plex_seconds:.0f}s) is not it")
    except Exception as exc:
        logger.debug(f"_file_path: {exc}")
    return None


def _refresh_in_plex(plex_track) -> None:
    """Make Plex re-read the file: the track for its tags, its album (where
    Plex keeps the artwork it shows) and its artist. Plex keeps an artist
    record's old name until that record itself is refreshed, so without the
    last step a fixed track stays listed under the wrong artist."""
    for what, get in (("track", lambda: plex_track),
                      ("album", lambda: plex_track.album()),
                      ("artist", lambda: plex_track.artist())):
        try:
            get().refresh()
        except Exception as exc:
            logger.debug(f"    {what} refresh: {exc}")
    time.sleep(1)


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a directory/file name.
    
    Removes/replaces problematic characters while preserving readability.
    """
    if not name:
        return "Unknown"
    from naming import balanced
    name = balanced(name)
    # Replace problematic characters with underscores
    import re
    name = re.sub(r'[<>:"|?*\\/]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name or "Unknown"


LIBRARY_ROOTS = ("albums",)
AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav")


def _library_root_of(file_path: str):
    """The library root (/music/albums …) a file lives under.

    The root is found from the path rather than by counting three folders
    up, which only works for Artist/Album/song: a song lying directly in an
    artist folder ("albums/Death Cab for Cutie/Riptides.mp3") would give
    /music and be filed outside albums/. A file under the library but outside
    every root belongs in albums/. None for files outside the library."""
    from paths import container_music_root
    music = os.path.normpath(container_music_root())
    rel = os.path.relpath(os.path.normpath(os.path.abspath(file_path)), music)
    if rel == "." or rel.startswith(".."):
        return None
    top = rel.split(os.sep)[0]
    return os.path.join(music, top if top in LIBRARY_ROOTS else "albums")


def _set_aside_leftover_art(folder: str) -> None:
    """A folder a fix emptied of songs but whose cover stayed (the new folder
    had its own) would linger as an empty album in Plex: with only pictures
    left, they go to quarantine so the folder can be removed."""
    import shutil
    try:
        names = os.listdir(folder)
    except OSError:
        return
    if not names or any(os.path.splitext(n)[1].lower() not in (".jpg", ".jpeg", ".png", ".webp") for n in names):
        return
    quarantine = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "fixmeta_quarantine", "leftover_art",
                              time.strftime("%Y%m%d-%H%M%S"), os.path.basename(folder))
    os.makedirs(quarantine, exist_ok=True)
    for name in names:
        shutil.move(os.path.join(folder, name), os.path.join(quarantine, name))
    logger.info(f"    Leftover art of the emptied folder set aside → {quarantine}")


_LIDARR = {}


def _lidarr_catalog():
    """Lidarr's catalog, loaded once per process; None without Lidarr."""
    if "catalog" not in _LIDARR:
        try:
            from lidarr_catalog import LidarrCatalog
            catalog = LidarrCatalog.from_env()
            _LIDARR["catalog"] = catalog.load() if catalog else None
        except Exception as exc:
            logger.debug(f"Lidarr catalog unavailable: {exc}")
            _LIDARR["catalog"] = None
    return _LIDARR["catalog"]


def _lidarr_place(file_path: str, meta: dict) -> Optional[tuple]:
    """(names, folder, file name) Lidarr gives this song when it knows its
    album and the track on it: Lidarr's album and track titles and number,
    its folder for the album within Lidarr's root and its file naming
    ("KAROL G - NO ME ARREPIENTO DE SENTIR TANTO - 11 - BbY WOW.mp3"). Under
    Lidarr's root a file filed this way becomes Lidarr's own, so an upgrade
    to FLAC replaces it rather than adding a copy. None when Lidarr doesn't
    know the song, or its folder already has the track (song_dedup's)."""
    from metadata_repair import artists_agree
    catalog = _lidarr_catalog()
    root = _library_root_of(file_path)
    if catalog is None or root is None or not meta.get("album") or not meta.get("title"):
        return None
    album = catalog.match_album(meta.get("artist", ""), [meta["album"]])
    track = catalog.find_track(album, meta["title"]) if album else None
    if not track:
        return None
    folder = catalog.album_dir(album, allowed_roots=[root])
    if not folder:
        return None
    ext = os.path.splitext(file_path)[1].lower()
    name = os.path.basename(catalog.render_track_path(album, track["title"], track["track"], track["disc"], ext))
    stem = os.path.splitext(name)[0]
    try:
        taken = [n for n in os.listdir(folder) if os.path.splitext(n)[0] == stem
                 and os.path.join(folder, n) != os.path.abspath(file_path)]
    except OSError:
        taken = []
    if taken:
        return None
    names = dict(meta, album=album.title, title=track["title"],
                 artist=album.artist_name if artists_agree(meta.get("artist"), album.artist_name) else meta["artist"])
    if track["track"]:
        names["track"] = track["track"]
    return names, folder, name


def _file_away(fp: str, meta: dict) -> tuple:
    """Where a fixed song goes. (names to write, folder or None, file name or
    None). Every song lives in albums/, Lidarr's root folder — new songs too
    (they are only marked new, outside the library: library_state), so
    Lidarr organizes all of it:

    * a file Lidarr already owns stays put (Lidarr tracks it by path and
      renames it itself) unless the fix shows it is another song; a song
      Lidarr knows gets Lidarr's folder, file name, titles and track number,
      so Lidarr takes it as its own and an upgrade to FLAC replaces it in
      place;
    * otherwise (or outside Lidarr's root, or without Lidarr) MusicAI's own
      layout: the song stays with the rest of its album ("King Daddy" in
      Barrio Fino's folder), or joins the album the artist already has, or
      goes to Artist/Album/Title."""
    catalog = _lidarr_catalog()
    root = _library_root_of(fp)
    lidarr_root = catalog is not None and root is not None and any(
        os.path.normpath(root) == os.path.normpath(r) for r in catalog.root_folders)
    if lidarr_root:
        if os.path.normpath(os.path.abspath(fp)) in catalog.track_file_paths():
            tags = _file_tags(fp) or {}
            if _same_title(tags.get("title", ""), meta.get("title", "")) \
                    and _title_core(tags.get("artist", "")) == _title_core(meta.get("artist", "")):
                logger.info("    Lidarr's own file: it stays where Lidarr keeps it")
                return meta, os.path.dirname(fp), os.path.basename(fp)
            # Another song than the track Lidarr took it for: out of its
            # folder, so Lidarr sees the track missing and fetches the right one.
            logger.info("    Lidarr filed this as another song; it leaves Lidarr's folder")
        placed = _lidarr_place(fp, meta)
        if placed:
            logger.info(f"    filed the way Lidarr files it: {placed[1]}/{placed[2]}")
            return placed
    elif _in_its_album_folder(fp, meta):
        return meta, os.path.dirname(fp), os.path.basename(fp)   # already with the rest of its album
    joined = _existing_album(fp, meta)
    if joined:
        logger.info(f"    joins the album already in the library: \"{joined[0]}\"")
        return dict(meta, album=joined[0]), joined[1], None
    return meta, None, None


def _in_its_album_folder(fp: str, meta: dict) -> bool:
    """The file already sits with other songs of its album ("King Daddy" in
    Barrio Fino's folder)."""
    from artwork import _core
    from mutagen import File as MutagenFile
    folder = os.path.dirname(fp)
    try:
        others = [n for n in os.listdir(folder) if os.path.splitext(n)[1].lower() in AUDIO_EXTENSIONS
                  and os.path.join(folder, n) != fp]
    except OSError:
        return False
    for name in others[:5]:
        try:
            tags = MutagenFile(os.path.join(folder, name), easy=True) or {}
        except Exception:
            continue
        if _core((tags.get("album") or [""])[0]) == _core(meta.get("album", "")):
            return True
    return False


def _tell_lidarr(folder: Optional[str]) -> None:
    """Ask Lidarr to rescan a folder under its root, so it takes the file
    as its track (files elsewhere it doesn't import)."""
    catalog = _lidarr_catalog()
    if folder and catalog is not None and any(
            os.path.normpath(folder).startswith(os.path.normpath(r) + os.sep) for r in catalog.root_folders):
        catalog.rescan([folder])


def _existing_album(file_path: str, meta: dict) -> Optional[tuple]:
    """(album tag, folder) of an album the artist already has that is the
    same release under its own spelling: "Barrio Fino" for "Barrio Fino
    (Bonus Track Version)", Lidarr's "NO ME ARREPIENTO DE SENTIR TANTO (2026)"
    folder for "NO ME ARREPIENTO DE SENTIR TANTO". Without it a fixed song
    starts a second copy of the album in Plex. None when there is no such
    album, or it already holds this song (that copy is song_dedup's)."""
    from artwork import _core
    from metadata_repair import artists_agree
    from mutagen import File as MutagenFile
    album, artist = meta.get("album", ""), meta.get("artist", "")
    root = _library_root_of(file_path)
    if not album or not artist or root is None:
        return None
    current = os.path.dirname(os.path.abspath(file_path))
    found = []
    for base in (root,):
        artist_dir = os.path.join(base, _sanitize_filename(artist))
        try:
            folders = sorted(os.listdir(artist_dir))
        except OSError:
            continue
        for name in folders:
            folder = os.path.join(artist_dir, name)
            if not os.path.isdir(folder) or os.path.abspath(folder) == current:
                continue
            songs = [n for n in os.listdir(folder) if os.path.splitext(n)[1].lower() in AUDIO_EXTENSIONS]
            if not songs:
                continue
            # The folder's own spelling: its most common album tag.
            from collections import Counter
            albums, artists = Counter(), Counter()
            for song in songs[:30]:
                try:
                    tags = MutagenFile(os.path.join(folder, song), easy=True) or {}
                except Exception:
                    continue
                albums[((tags.get("album") or [""])[0] or "").strip()] += 1
                artists[((tags.get("albumartist") or tags.get("artist") or [""])[0] or "").strip()] += 1
            if not albums:
                continue
            tag_album, tag_artist = albums.most_common(1)[0][0], artists.most_common(1)[0][0]
            if not tag_album or _core(tag_album) != _core(album) or not artists_agree(tag_artist, artist):
                continue
            titles = {_core(os.path.splitext(n)[0].split(" - ")[-1]) for n in songs}
            if _core(meta.get("title", "")) in titles:
                return None
            found.append((_exact(tag_album) != _exact(album), tag_album, folder))
    return min(found)[1:] if found else None


def _reorganize_file(file_path: str, meta: dict, base_dir: str = None, target_dir: str = None,
                     target_name: str = None) -> str:
    """Reorganize a file to {base_dir}/{artist}/{album}/{title}.{ext}.
    
    Uses corrected metadata to build the target path.
    If move succeeds, returns new path; otherwise returns original path.
    
    Args:
        file_path: Current path to the audio file
        meta: dict with 'artist', 'album', 'title' keys
        base_dir: Base directory for reorganization (defaults to parent of current file's parent's parent)
    
    Returns:
        New file path if successful, original file_path if failed or skipped
    """
    try:
        artist = _sanitize_filename(meta.get("artist", "Unknown"))
        album = _sanitize_filename(meta.get("album", "Unknown"))
        title = _sanitize_filename(meta.get("title", "Unknown"))
        
        # Determine base directory if not provided
        if base_dir is None:
            base_dir = _library_root_of(file_path)
        if base_dir is None:
            # Outside the music library (tests, one-off tools): derive from
            # the current path structure.
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(file_path)))
            # If path doesn't have enough depth, try to use /music/albums
            if not base_dir or base_dir == "/":
                base_dir = "/music/albums"
        
        # Ensure base_dir exists
        os.makedirs(base_dir, exist_ok=True)
        
        # Build target directory structure
        ext = os.path.splitext(file_path)[1]
        target_dir = target_dir or os.path.join(base_dir, artist, album)
        target_file = os.path.join(target_dir, target_name or f"{title}{ext}")
        
        # If file is already at the target location, skip
        if os.path.abspath(file_path) == os.path.abspath(target_file):
            logger.debug(f"    File already at correct location: {target_file}")
            return file_path
        
        # Create target directory
        os.makedirs(target_dir, exist_ok=True)
        
        # Handle file name conflicts
        final_target = target_file
        counter = 1
        base_target = os.path.splitext(target_file)[0]
        while os.path.exists(final_target) and os.path.abspath(final_target) != os.path.abspath(file_path):
            final_target = f"{base_target}_{counter}{ext}"
            counter += 1
        
        # Move the file
        if os.path.abspath(file_path) != os.path.abspath(final_target):
            import shutil
            from artwork import carry_cover
            old_dir = os.path.dirname(file_path)
            shutil.move(file_path, final_target)
            import library_state
            library_state.moved(file_path, final_target)    # a new song stays new; lyrics follow
            logger.info(
                f"    Reorganized: {os.path.basename(file_path)} → "
                f"{artist}/{album}/{os.path.basename(final_target)}"
            )
            # The cover in the old folder is often the track's only artwork
            # (yt-dlp downloads embed none): it must travel with the file.
            try:
                moved_cover = carry_cover(old_dir, target_dir)
                if moved_cover:
                    logger.info(f"    Cover art brought along → {moved_cover}")
            except Exception as exc:
                logger.warning(f"    Could not bring cover art along from {old_dir}: {exc}")
            roots = {os.path.normpath(p) for p in (base_dir, "/music", "/music/albums")}
            if os.path.normpath(old_dir) not in roots:
                _set_aside_leftover_art(old_dir)
            for leftover in (old_dir, os.path.dirname(old_dir)):
                if os.path.normpath(leftover) in roots:
                    break
                try:
                    os.rmdir(leftover)          # only succeeds when empty
                except OSError:
                    break
            return final_target
        
        return file_path
        
    except Exception as exc:
        logger.warning(f"  Could not reorganize file {os.path.basename(file_path)}: {exc}")
        return file_path


def _write_tags(file_path: str, meta: dict) -> bool:
    """Write artist/album/title tags using mutagen, and the album artist too:
    Plex lists a track under its album artist, so a stale one would survive
    a fix that only sets the artist.

    Handles MP3 (ID3), FLAC, M4A/AAC/MP4, OGG Vorbis, and OGG Opus.
    Returns True on success.
    """
    try:
        from mutagen import File as MutagenFile
        from naming import balanced

        meta = {k: (balanced(v) if k in ("artist", "album", "title") and isinstance(v, str) else v)
                for k, v in meta.items()}
        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".mp3":
            from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, ID3NoHeaderError
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                tags = ID3()
            if meta.get("title"):  tags["TIT2"] = TIT2(encoding=3, text=meta["title"])
            if meta.get("artist"): tags["TPE1"] = TPE1(encoding=3, text=meta["artist"])
            if meta.get("album"):  tags["TALB"] = TALB(encoding=3, text=meta["album"])
            if meta.get("artist"): tags["TPE2"] = TPE2(encoding=3, text=meta["artist"])
            tags.save(file_path)

        elif ext in (".m4a", ".aac", ".mp4"):
            from mutagen.mp4 import MP4
            tags = MP4(file_path)
            if meta.get("title"):  tags["\xa9nam"] = [meta["title"]]
            if meta.get("artist"): tags["\xa9ART"] = [meta["artist"]]
            if meta.get("album"):  tags["\xa9alb"] = [meta["album"]]
            if meta.get("artist"): tags["aART"] = [meta["artist"]]
            tags.save()

        elif ext == ".flac":
            from mutagen.flac import FLAC
            tags = FLAC(file_path)
            if meta.get("title"):  tags["title"]  = [meta["title"]]
            if meta.get("artist"): tags["artist"] = [meta["artist"]]
            if meta.get("album"):  tags["album"]  = [meta["album"]]
            if meta.get("artist"): tags["albumartist"] = [meta["artist"]]
            tags.save()

        elif ext == ".ogg":
            from mutagen.oggvorbis import OggVorbis
            tags = OggVorbis(file_path)
            if meta.get("title"):  tags["title"]  = [meta["title"]]
            if meta.get("artist"): tags["artist"] = [meta["artist"]]
            if meta.get("album"):  tags["album"]  = [meta["album"]]
            if meta.get("artist"): tags["albumartist"] = [meta["artist"]]
            tags.save()

        elif ext == ".opus":
            from mutagen.oggopus import OggOpus
            tags = OggOpus(file_path)
            if meta.get("title"):  tags["title"]  = [meta["title"]]
            if meta.get("artist"): tags["artist"] = [meta["artist"]]
            if meta.get("album"):  tags["album"]  = [meta["album"]]
            if meta.get("artist"): tags["albumartist"] = [meta["artist"]]
            tags.save()

        else:
            # Generic EasyID3-style fallback
            audio = MutagenFile(file_path, easy=True)
            if audio is None:
                logger.warning(f"  mutagen could not open: {file_path}")
                return False
            if meta.get("title"):  audio["title"]  = meta["title"]
            if meta.get("artist"): audio["artist"] = meta["artist"]
            if meta.get("album"):  audio["album"]  = meta["album"]
            if meta.get("artist"): audio["albumartist"] = meta["artist"]
            audio.save()

        if meta.get("track"):
            # Lidarr's track number, so the file sorts and names like its own.
            numbered = MutagenFile(file_path, easy=True)
            if numbered is not None:
                numbered["tracknumber"] = str(meta["track"])
                numbered.save()

        logger.info(
            f"  tags written → {os.path.basename(file_path)}: "
            f"'{meta.get('artist', '')} – {meta.get('title', '')}' "
            f"[{meta.get('album', '')}]"
        )
        return True

    except Exception as exc:
        logger.warning(f"  tag write failed for {file_path}: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Playlist helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_create_fix_playlist(user_plex, plex_music_section: str):
    """Return the 'Fix Metadata' playlist for user_plex, creating it if absent."""
    try:
        for pl in user_plex.playlists():
            if pl.title == FIX_PLAYLIST_NAME:
                count = len(pl.items())
                logger.info(
                    f"  Found existing '{FIX_PLAYLIST_NAME}' playlist "
                    f"({count} track(s))"
                )
                return pl
    except Exception as exc:
        logger.debug(f"  playlists() error: {exc}")

    # Plex refuses to create an empty playlist ("Must include items to add"),
    # so create it with one track and remove that track straight away.
    logger.info(f"  '{FIX_PLAYLIST_NAME}' not found — creating it now")
    try:
        section = user_plex.library.section(plex_music_section)
        dummy   = section.search(libtype="track", maxresults=1)
        if not dummy:
            logger.warning("  Cannot create playlist: library has no tracks yet")
            return None
        pl = user_plex.createPlaylist(FIX_PLAYLIST_NAME, items=dummy)
        pl.removeItems(dummy)
        logger.info(f"  Created empty '{FIX_PLAYLIST_NAME}' playlist")
        return pl
    except Exception as exc:
        logger.warning(f"  Could not create '{FIX_PLAYLIST_NAME}' playlist: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_fix_metadata(
    plex_admin_server,
    plex_username: str,
    plex_music_section: str,
) -> Dict[str, int]:
    """Process every track in the user's 'Fix Metadata' Plex playlist.

    Parameters
    ----------
    plex_admin_server
        Already-connected PlexServer (admin token).
    plex_username
        Plex home-user name to switch into (PLEX_USERNAME / APP_USER_NAME).
        Pass empty string for single-user servers.
    plex_music_section
        Name of the Plex music library section, e.g. "Music".

    Returns {'fixed': int, 'skipped': int, 'failed': int, 'unresolved': list}
    where each unresolved entry is {'rating_key', 'label', 'reason', 'source'}.
    """
    from artwork import ensure_artwork
    from audio_id import identify_by_votes
    from artwork import own_upload_matches, verify_cover, youtube_source
    from metadata_repair import artists_agree, name_within
    from metadata_repair import (audio_duration, current_artist_name, deezer_lookup_by_name, deezer_track_by_isrc,
                                 file_isrc, is_complete, known_meta, listed_length, looks_like_compilation,
                                 name_candidates, release_listing, track_label)
    from text_repair import repair_fields

    stats: Dict = {"fixed": 0, "skipped": 0, "failed": 0, "unresolved": []}

    def hand_to_admin(plex_track, current: dict, reason: str) -> None:
        """Nothing could identify this track: take it off the user's playlist
        and report it so the daemon adds it to the admin's Manual Fix list."""
        stats["unresolved"].append({
            "rating_key": str(getattr(plex_track, "ratingKey", "")),
            "label": track_label(current),
            "reason": reason,
            "source": f"flagged by {plex_username or 'admin'}",
        })
        try:
            playlist.removeItems([plex_track])
        except Exception as exc:
            logger.debug(f"    removeItems failed: {exc}")

    if plex_admin_server is None:
        logger.warning("fix_metadata: no Plex connection — skipping")
        return stats

    # ── Switch to the Home user so the playlist is theirs ────────────────────
    if plex_username:
        try:
            user_plex = plex_admin_server.switchUser(plex_username)
            logger.info(f"  Switched to Plex user '{plex_username}'")
        except Exception as exc:
            logger.warning(
                f"  switchUser('{plex_username}') failed: {exc}. "
                "Falling back to admin context."
            )
            user_plex = plex_admin_server
    else:
        user_plex = plex_admin_server
        logger.info("  PLEX_USERNAME not set — using admin Plex context")

    # ── Open the music library ───────────────────────────────────────────────
    try:
        _library = user_plex.library.section(plex_music_section)
        logger.info(f"  Opened library '{plex_music_section}'")
    except Exception as exc:
        logger.warning(f"  Could not open library '{plex_music_section}': {exc}")
        # Non-fatal — playlist operations don't require a library reference

    # ── Get or create the playlist ───────────────────────────────────────────
    logger.info(f"  Checking for '{FIX_PLAYLIST_NAME}' playlist…")
    playlist = _get_or_create_fix_playlist(user_plex, plex_music_section)
    if playlist is None:
        logger.warning("  Could not get/create playlist — aborting")
        return stats

    # ── Snapshot the track list before modifying the playlist ────────────────
    try:
        tracks_to_fix = list(playlist.items())
    except Exception as exc:
        logger.warning(f"  Could not list playlist items: {exc}")
        return stats

    if not tracks_to_fix:
        logger.info(f"  '{FIX_PLAYLIST_NAME}' is empty — nothing to fix this cycle")
        return stats

    logger.info(f"  {len(tracks_to_fix)} track(s) to process")

    # ── Process each track ───────────────────────────────────────────────────
    for plex_track in tracks_to_fix:
        current = _current_meta(plex_track)
        label   = f"'{current['artist']} – {current['title']}'"

        logger.info(f"  Processing {label}")
        logger.info(
            f"    current  artist='{current['artist']}' | "
            f"album='{current['album']}' | title='{current['title']}'"
        )

        # Resolve file path
        fp = _file_path(plex_track)
        if not fp:
            logger.warning(f"  {label}: audio file not found on disk — removing from playlist")
            try:
                playlist.removeItems([plex_track])
                logger.info(f"    Removed {label} from '{FIX_PLAYLIST_NAME}' because file is missing")
            except Exception as exc:
                logger.debug(f"    removeItems failed for missing file: {exc}")
            stats["skipped"] += 1
            continue

        # The file's ISRC names the exact recording; otherwise several slices
        # are fingerprinted and the answers ranked by how many agree.
        logger.info(f"    fingerprinting: {os.path.basename(fp)}")
        try:
            by_isrc = deezer_track_by_isrc(file_isrc(fp), audio_duration(fp))
            voted = [by_isrc] if by_isrc else identify_by_votes(fp)
        except Exception as exc:
            logger.warning(f"  {label}: fingerprint exception: {exc} — handing to admin")
            hand_to_admin(plex_track, current, f"fingerprinting failed ({exc})")
            continue
        voted = [repair_fields(a) for a in voted if a]
        _enrich_from_deezer(voted[:3], audio_duration(fp))
        best = voted[0] if voted else None
        next_best = voted[1] if len(voted) > 1 else None
        # A service's answer can arrive as mojibake (a reply without a
        # charset); repair it so garbled text is never written back.
        def agreeing_hints(meta):
            # Only a cover link from an answer naming these very names: a
            # one-vote wrong answer would bring another song's cover.
            return [a.get("cover_art_url") for a in voted[:3]
                    if a and a.get("cover_art_url") and _confirms(_known_fields(meta), a)]

        # Decide which result to apply. Placeholders ("Miscellaneous",
        # "Unknown") are blanks; an answer whose artist contains its own title
        # ("Thrift Shop & I'm Gonna Pop Some Tags") is a cover upload's name,
        # not an artist.
        # What the file says, not what Plex shows: Plex files songs under
        # whichever artist first claimed their album, so its artist can be
        # wrong (a conductor credited for a soloist's concerto). known_meta
        # trusts the file's tags, then
        # Plex only where Plex can be trusted, then the file name for a
        # missing title (YouTube rips often have none).
        tagged = _file_tags(fp) or current
        known = _known_fields(known_meta(plex_track, fp))
        answers = [a for a in voted[:3] if a.get("service") == "ISRC" or _usable_answer(a)]
        # A re-upload outvoting the original: answers naming the same song
        # as one that has the file's artist count for that one (three
        # Shazam votes for a stream-farm alias, one for the real artist).
        for a in list(answers):
            if _confirms(known, a):
                for b in list(answers):
                    if b is not a and _same_title(b.get("title", ""), a.get("title", "")) and not _confirms(known, b):
                        a["votes"] = (a.get("votes") or 1) + (b.get("votes") or 1)
                        answers.remove(b)
                break
        names = name_candidates(dict(known, **{k: v for k, v in tagged.items() if v and not known.get(k)}), fp)
        # The most agreed-on answer first; among equals, the one that fits
        # what is known (Shazam's "Tiny Meat Gang – Walk Man" for a file
        # tagged "TMG – Walk Man", ahead of an unrelated answer). A wrong tag
        # confirmed by one slice never outranks six that agree.
        answers.sort(key=lambda a: (-(a.get("votes") or 1),
                                    0 if _confirms(known, a) else 1 if _supported(known, a, answers, names) else 2))
        # First: are the names right and only the audio wrong? A music-video
        # rip with its intro ("Walk Man", 184 s against the 166 s release), a
        # truncated download ("Cracker Barrel", 75 s of 165 s), or audio that
        # isn't the song at all (a 20-minute radio special tagged "Everything
        # Has Changed") — when the true recording exists, it replaces the file.
        if _replace_wrong_audio(plex_track, fp, known, answers, playlist, stats, label, plex_username):
            continue

        chosen:       Optional[dict] = answers[0] if answers else None
        chosen_why:   str            = ""
        admin_reason: str            = ""
        renamed_from: Optional[str]  = None
        if chosen is None:
            admin_reason = "fingerprinting could not identify the song"
        elif _confirms(known, chosen):
            # The names are right; only what is missing or wrong besides them
            # (usually the album) is taken from the answers. A second service
            # naming a different song is a disagreement, not a correction.
            chosen_why = "fingerprint confirms the names; filling the rest"
        elif (sample_len := _sampled(known, chosen, fp)) is not None:
            # However many slices name it: the tagged song is released at this
            # length and theirs is not, so they hear a sampled beat.
            logger.info(f"    ignoring {chosen.get('artist')} – {chosen.get('title')} ({sample_len}s): the tagged "
                        f"recording is listed at this length, so the services hear its sample")
            chosen, admin_reason = None, "fingerprinting confirms the current tags"
        elif _supported(known, chosen, answers, names):
            chosen_why = "fingerprint identifies the song"
        elif chosen.get("unconfirmed") and not known["artist"] and (
                not known["title"] or _title_core(chosen.get("title", "")) in _title_core(known["title"])):
            # Someone put it in Fix Metadata and it has no artist: Genius's
            # answer for its sung lines, which its own name agrees with
            # ("Rain" in "Unknown ♫Rain♫"), beats "Unidentified". The daily
            # repair of the whole library still only suggests it.
            chosen_why = "the sung lyrics identify the song (Genius); the file had no artist and its name agrees"
        elif is_complete(known) and (release_listing(known["artist"], known["title"], audio_duration(fp)) is not None
                                     or own_upload_matches(known["artist"], known["title"], audio_duration(fp))):
            # The tagged recording exists, same length, as a release or the
            # artist's own upload: one service naming something else is the
            # noise.
            logger.info(f"    ignoring the lone answer {chosen.get('artist')} – {chosen.get('title')}: "
                        f"the tagged recording is listed at this length")
            chosen, admin_reason = None, "fingerprinting confirms the current tags"
        else:
            # One service naming an unrelated song, with nothing to back it,
            # is wrong as often as right (a Japanese "ライフ" answered as an
            # English band's "Life"): a person decides.
            admin_reason = (f"the sung lyrics suggest {chosen.get('artist')} – {chosen.get('title')} (Genius; "
                            f"no lyrics to compare)" if chosen.get("unconfirmed") else
                            f"one fingerprint service suggests {chosen.get('artist')} – {chosen.get('title')}, "
                            f"which nothing confirms")
            chosen = None

        new_meta = _merge_answer(known, chosen, answers, same_song=_confirms(known, chosen), file_path=fp) if chosen else None
        if new_meta is not None and not is_complete(new_meta):
            admin_reason = f"identified as {track_label(new_meta)}, but no album was found"
            new_meta = None
        elif new_meta is not None and _all_match(tagged, new_meta):
            admin_reason = "fingerprinting confirms the current tags"
            new_meta = None

        if new_meta is None and not admin_reason.startswith("fingerprinting confirms"):
            # Fingerprinting didn't settle it: look the song up by its names,
            # trusting a result only if its length matches the file.
            found = deezer_lookup_by_name(known, fp, [a.get("title", "") for a in voted[:3]])
            if found and is_complete(found):
                if _all_match(tagged, found):
                    admin_reason = "the tags are right (Deezer lists this exact recording)"
                    if _known_fields(current)["artist"] and current["artist"] != tagged["artist"]:
                        admin_reason += f"; Plex shows it under {current['artist']} and needs re-filing"
                else:
                    new_meta, chosen_why, admin_reason = found, "found on Deezer by its names and length", ""
            elif (known["title"] and not any(((a.get("votes") or 1) >= 2 and not _confirms(known, a))
                                             or a.get("service") == "ISRC" for a in answers)
                  # A performer the title itself names ("NO play …", "Itzhak
                  # Perlman – Beethoven: …") is not replaced by whoever
                  # uploaded the video (a fan, a label's channel).
                  and _title_core(known["artist"]) == _title_core(tagged.get("artist", ""))
                  and not (known["artist"] and name_within(known["artist"], tagged.get("title") or ""))
                  and not (known["artist"] and own_upload_matches(known["artist"], known["title"], audio_duration(fp)))
                  and (source := youtube_source(tagged.get("title") or known["title"], audio_duration(fp)))
                  and not artists_agree(source[0], known["artist"])):
                # A YouTube rip no service knows, tagged with someone else's
                # name: the video it was ripped from (same title, same length
                # to the second) names the uploader.
                plain = _clean_title(known["title"])
                new_meta = {"artist": source[0], "album": plain, "title": plain}
                chosen_why, admin_reason = f"the YouTube upload it was ripped from ({source[0]}, video {source[1]})", ""
            elif ((_on_probation(fp) or _is_video_title(tagged.get("album", ""))) and known["artist"] and known["title"]
                  and not known["album"]
                  and not any((a.get("votes") or 1) >= 2 and not _confirms(known, a) for a in answers)
                  and not any(a.get("service") == "ISRC" for a in answers)):
                # Nothing knows an album for this song (YouTube-only diss
                # tracks), and nothing says the names are wrong: file it the
                # way every single is filed, album named after it.
                plain = _clean_title(known["title"])
                new_meta = {"artist": known["artist"], "album": plain, "title": plain}
                chosen_why, admin_reason = "nothing identifies it; filed as a single (album named after the song)", ""

        if new_meta is None and is_complete(known) and admin_reason in (
                "", "fingerprinting confirms the current tags", "fingerprinting could not identify the song"):
            # A flagged song always has something wrong. With the names
            # holding, check the rest against the release: its album, then
            # its cover. Only a recording proven to be this song is touched.
            file_len = audio_duration(fp)
            proven = (any(_confirms(known, a) for a in answers)
                      or release_listing(known["artist"], known["title"], file_len) is not None
                      or own_upload_matches(known["artist"], known["title"], file_len)
                      # the very video it was ripped from: same title, same length
                      or youtube_source(tagged.get("title") or "", file_len) is not None)
            if not proven:
                listed = listed_length(known["artist"], known["title"])
                admin_reason = ("no service recognises the audio"
                                + (f", and Deezer's \"{known['title']}\" is {listed // 60}:{listed % 60:02d} while this file is "
                                   f"{int(file_len) // 60}:{int(file_len) % 60:02d} — it isn't that recording" if listed and file_len else ""))
            else:
                listing = release_listing(known["artist"], known["title"], file_len)
                if listing and not looks_like_compilation(listing["album"]) and (
                        (listing["album"] and _exact(listing["album"]) != _exact(known["album"]))
                        or (listing.get("title") and _exact(listing["title"]) != _exact(known["title"]))):
                    # The release's own names: "2Bears1Grave (BERT & TOM DISS)" is
                    # "2Bears1Grave", and a single's album is its release's.
                    new_meta = dict(known, album=listing["album"] or known["album"],
                                    title=listing.get("title") or known["title"])
                    chosen_why, admin_reason = (f"the release ({listing['source']}) is "
                                                f"\"{new_meta['title']}\" on \"{new_meta['album']}\""), ""
                elif current_artist_name(known["artist"]):
                    # Everything else holds but the artist goes by a new name.
                    new_meta, chosen_why, admin_reason = dict(known), "the artist's current name", ""
                elif not _all_match(tagged, known):
                    # The tags' own names, untangled ("THAO: Remember Me (…)"
                    # by "Thao Sam" is THAO's "Remember Me (…)").
                    new_meta, chosen_why, admin_reason = dict(known), "the tags' own names, cleaned up", ""
                else:
                    art = verify_cover(fp, known)
                    if art.startswith(("replaced", "fetched")):
                        logger.info(f"    names and album hold; the cover was wrong — {art}")
                        _refresh_in_plex(plex_track)
                        try:
                            playlist.removeItems([plex_track])
                        except Exception as exc:
                            logger.debug(f"    removeItems: {exc}")
                        stats["fixed"] += 1
                        logger.info(f"  DONE {label}")
                        continue
                    if art == "matches the release":
                        admin_reason = ("names, album and cover all match the release; "
                                        "what is wrong could not be found")
                    else:
                        admin_reason = ((admin_reason or "the names are confirmed")
                                        + "; the album holds and no release cover was found to compare")

        if new_meta is None:
            # The flag can be about something other than wrong names: the
            # text may be garbled, the title may only be in the file name, or
            # the track may have no artwork. Fix those before deciding a
            # person has to look at it.
            repaired = repair_fields(tagged)
            title_only_in_name = (not _known_fields(tagged)["title"] and known["title"]
                                  and is_complete(known) and not admin_reason.startswith(("identified as", "one fingerprint")))
            if repaired != tagged and is_complete(_known_fields(repaired)):
                new_meta, chosen_why = repaired, "current tags were garbled text (mojibake); repaired"
            elif title_only_in_name:
                new_meta, chosen_why = dict(known), "title taken from the file name"
            else:
                disputed = admin_reason.startswith(("one fingerprint", "identified as"))
                art = ensure_artwork(fp, known, [] if disputed else agreeing_hints(known))
                names_complete = is_complete(known)
                if art not in ("present", "not found") and names_complete and not disputed:
                    logger.info(f"    tags are right; artwork was the problem — {art}")
                    _refresh_in_plex(plex_track)
                    try:
                        playlist.removeItems([plex_track])
                        logger.info(f"    Removed from '{FIX_PLAYLIST_NAME}'")
                    except Exception as exc:
                        logger.debug(f"    removeItems: {exc}")
                    stats["fixed"] += 1
                    logger.info(f"  DONE {label}")
                    continue
                # Art alone doesn't fix a song with no title or a placeholder
                # album: such a song is not counted as done.
                if not names_complete and not admin_reason.startswith("identified as"):
                    admin_reason = (admin_reason or "names incomplete") + "; the names are still incomplete"
                logger.info(f"  {label}: {admin_reason} (artwork: {art}) — handing to admin")
                hand_to_admin(plex_track, current, admin_reason)
                continue

        # The act's current name, not an old one ("YBN Cordae" is "Cordae").
        current_name = current_artist_name(new_meta["artist"])
        if current_name:
            renamed_from, new_meta = new_meta["artist"], dict(new_meta, artist=current_name)
            chosen_why += f"; \"{renamed_from}\" is now \"{current_name}\""

        # With the names corrected, the audio may still not be the release: a
        # production-library edit of "Close Enough" is 228 s, Dana and the
        # Wolf's single 216 s. Same check as before the names were looked at.
        if not (_same_title(tagged.get("title", ""), new_meta["title"])
                and _title_core(tagged.get("artist", "")) == _title_core(new_meta["artist"])) \
                and _replace_wrong_audio(plex_track, fp, new_meta, answers, playlist, stats, label, plex_username,
                                         names_fixed=True):
            continue

        logger.info(
            f"    applying ({chosen_why}): "
            f"artist='{new_meta['artist']}' | "
            f"album='{new_meta['album']}' | "
            f"title='{new_meta['title']}'"
        )

        new_meta, join_dir, join_name = _file_away(fp, new_meta)

        # Write tags to file
        if not _write_tags(fp, new_meta):
            logger.warning(f"  {label}: tag write failed — NOT removing from playlist")
            stats["failed"] += 1
            continue

        # Reorganize file to correct folder structure based on corrected metadata
        new_fp = _reorganize_file(fp, new_meta, target_dir=join_dir, target_name=join_name)
        if join_name:
            _tell_lidarr(join_dir)
        if new_fp != fp:
            logger.info(f"    File reorganized to new location: {new_fp}")
            fp = new_fp  # Update path for subsequent operations

        # Artwork is part of the fix: a track filed under its correct album
        # with no cover still looks broken in Plex.
        # A renamed act is still the same song, and its cover stays.
        before_rename = renamed_from or new_meta["artist"]
        identity_changed = not (_same_title(tagged.get("title", ""), new_meta["title"])
                                and _title_core(tagged.get("artist", "")) == _title_core(before_rename))
        art = ensure_artwork(fp, new_meta, agreeing_hints(new_meta), replace=identity_changed)
        if art in ("present", "embedded folder cover", "folder cover from embedded art"):
            # The cover kept may belong to other names the file once had: it
            # is checked against the release's.
            checked = verify_cover(fp, new_meta)
            art = checked if checked.startswith(("replaced", "fetched")) else art
        logger.info(f"    artwork: {art}")
        if art == "not found":
            # Names fixed, but no cover anywhere (Deezer, iTunes, Spotify,
            # YouTube Music, YouTube): a person has to supply one.
            stats["unresolved"].append({
                "rating_key": str(getattr(plex_track, "ratingKey", "")),
                "label": track_label(new_meta),
                "reason": "names fixed, but no cover was found on Deezer, iTunes, Spotify or YouTube",
                "source": f"flagged by {plex_username or 'admin'}",
            })

        # Refresh Plex item so server re-reads updated tags
        _refresh_in_plex(plex_track)

        # Removing the track from the playlist marks it as done
        try:
            playlist.removeItems([plex_track])
            logger.info(f"    Removed from '{FIX_PLAYLIST_NAME}'")
        except Exception as exc:
            logger.debug(f"    removeItems: {exc}")

        stats["fixed"] += 1
        logger.info(f"  DONE {label}")

    logger.info(
        f"  Fix Metadata summary: "
        f"{stats['fixed']} fixed | "
        f"{stats['skipped']} skipped | "
        f"{stats['failed']} failed | "
        f"{len(stats['unresolved'])} handed to admin"
    )
    return stats
