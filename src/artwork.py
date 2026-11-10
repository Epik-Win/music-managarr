"""Album artwork for a track: detect when it is missing, find it, store it.

Plex shows a track's art from the image embedded in the file or from a cover
file in its folder (cover.jpg, folder.jpg …). A file downloaded by yt-dlp has
nothing embedded, so its art lives only in the folder's cover.jpg — and is
lost the moment the file is moved somewhere else. So when art is found it is
both saved as cover.jpg and embedded in the file, which then carries it along.

Sources, in order: a cover link the fingerprint services returned, Deezer's
public search, then iTunes. A search result is only used if its title matches,
its version labels agree (a "Remix" or "Karaoke" release is another cover),
and either the artist (any credited one) or the album matches.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger("musicai.artwork")

COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png",
               "front.jpg", "albumart.jpg", "artwork.jpg")
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".wav"}
MIN_IMAGE_BYTES = 2048
_TIMEOUT = 15


# ── what is there ───────────────────────────────────────────────────────────

def folder_cover(folder: str) -> Optional[str]:
    try:
        names = {n.lower(): n for n in os.listdir(folder)}
    except OSError:
        return None
    for wanted in COVER_NAMES:
        if wanted in names:
            return os.path.join(folder, names[wanted])
    return None


def has_embedded_art(path: str) -> bool:
    try:
        from mutagen import File as MuFile
        audio = MuFile(path)
    except Exception:
        return False
    if audio is None:
        return False
    if getattr(audio, "pictures", None):                       # FLAC
        return True
    tags = getattr(audio, "tags", None)
    if not tags:
        return False
    keys = list(tags.keys())
    return any(str(k).startswith("APIC") for k in keys) or "covr" in keys \
        or "metadata_block_picture" in keys


def embedded_art(path: str) -> Optional[bytes]:
    """The image embedded in the file (front cover first), or None."""
    try:
        from mutagen import File as MuFile
        audio = MuFile(path)
    except Exception:
        return None
    if audio is None:
        return None
    pictures = list(getattr(audio, "pictures", None) or [])          # FLAC
    tags = getattr(audio, "tags", None)
    if tags is not None and not pictures:
        if hasattr(tags, "getall"):                                   # ID3
            pictures = tags.getall("APIC")
        elif "covr" in tags:                                          # MP4
            return bytes(tags["covr"][0])
        elif "metadata_block_picture" in tags:                        # Ogg
            import base64
            from mutagen.flac import Picture
            pictures = [Picture(base64.b64decode(v)) for v in tags["metadata_block_picture"]]
    pictures.sort(key=lambda pic: getattr(pic, "type", 0) != 3)
    data = pictures[0].data if pictures else None
    return data if data and len(data) >= MIN_IMAGE_BYTES else None


# ── storing ─────────────────────────────────────────────────────────────────

def _mime(image: bytes) -> str:
    return "image/png" if image[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def embed_art(path: str, image: bytes) -> bool:
    """Embed the image as the front cover. Ogg/Opus/WAV are left to cover.jpg."""
    ext = os.path.splitext(path)[1].lower()
    mime = _mime(image)
    try:
        if ext == ".mp3":
            from mutagen.id3 import APIC, ID3, ID3NoHeaderError
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image))
            tags.save(path)
        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(path)
            pic = Picture()
            pic.type, pic.mime, pic.data = 3, mime, image
            audio.clear_pictures()
            audio.add_picture(pic)
            audio.save()
        elif ext in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(path)
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(image, imageformat=fmt)]
            audio.save()
        else:
            return False
        return True
    except Exception as exc:
        logger.warning(f"Could not embed artwork in {path}: {exc}")
        return False


def _download(url: str) -> Optional[bytes]:
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        logger.debug(f"Artwork download failed ({url}): {exc}")
        return None
    data = resp.content if resp.status_code == 200 else b""
    if len(data) < MIN_IMAGE_BYTES or not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        return None
    if _is_placeholder(data):
        logger.debug(f"Artwork at {url} is a service's no-cover placeholder")
        return None
    return data


# Services' stand-ins for a release without art: Deezer's grey disc on a
# light background, which would otherwise be taken as a real cover.
# Known by look and size — at 1000 px real covers weigh far more.
_PLACEHOLDER_HASHES = (0xffffc3c3c3c3ffff,)
_PLACEHOLDER_MAX_BYTES = 40000


def _is_placeholder(image: bytes) -> bool:
    if len(image) > _PLACEHOLDER_MAX_BYTES:
        return False
    h = image_hash(image)
    return h is not None and any(bin(h ^ p).count("1") <= 4 for p in _PLACEHOLDER_HASHES)


def _drop_placeholder_art(path: str) -> None:
    """A stored placeholder is no cover: take it off the file
    and out of a folder it has to itself."""
    try:
        embedded = embedded_art(path)
        if embedded and _is_placeholder(embedded):
            _strip_art(path)
            logger.info(f"    removed a no-cover placeholder from {os.path.basename(path)}")
        cover = folder_cover(os.path.dirname(path))
        if cover and _alone_in_folder(path):
            with open(cover, "rb") as fh:
                if _is_placeholder(fh.read()):
                    os.remove(cover)
    except Exception as exc:
        logger.debug(f"placeholder check failed for {path}: {exc}")


# ── finding ─────────────────────────────────────────────────────────────────

_BRACKETS_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.I)
_SPLIT_RE = re.compile(r"\s*(?:,|;|&|/|\band\b|\bx\b|\bfeat\.?|\bft\.?)\s*", re.I)


def _core(text: str) -> str:
    """Comparable form that keeps non-Latin letters (Arabic, Cyrillic …)
    but drops accents ("Pokémon" = "POKEMON")."""
    text = unicodedata.normalize("NFKC", text or "")
    text = _FEAT_RE.sub("", _BRACKETS_RE.sub("", text)).casefold()
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", text)


_DASH_SUFFIX_RE = re.compile(
    r"\s+-\s+(?:live|acoustic|mono|stereo|single version|radio edit|(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?)\b.*$", re.I)


def _plain(text: str) -> str:
    """Search form of a name: no bracketed extras, feat. credit or " - Live"
    suffix. Deezer finds nothing for "Travelin' Soldier (Acoustic)" or
    "Predator (Official Video)"; the version label is still checked on the
    results (result_matches)."""
    text = _DASH_SUFFIX_RE.sub("", text or "")
    text = _FEAT_RE.sub("", _BRACKETS_RE.sub("", text))
    return re.sub(r"\s+", " ", text).strip()


def _query(text: str) -> str:
    """A search query: _plain without censoring stars, which search engines
    treat badly ("Sh*t Talk" finds nothing on SoundCloud, "Sht Talk" does)."""
    return re.sub(r"\s+", " ", _plain(text).replace("*", "")).strip()


def _search_artist(artist: str) -> str:
    """The first credited artist, without a leading "The" ("The Ray Conniff")."""
    first = (_artists(artist) or [artist or ""])[0].strip()
    return re.sub(r"^the\s+", "", first, flags=re.I)


def _same(a: str, b: str, threshold: float = 0.85) -> bool:
    ca, cb = _core(a), _core(b)
    return bool(ca and cb) and (ca == cb or SequenceMatcher(None, ca, cb).ratio() >= threshold)


def _artists(text: str) -> List[str]:
    return [p for p in _SPLIT_RE.split(text or "") if _core(p)]


_NUMBER_RE = re.compile(r"(?:\b(?:part|pt\.?|vol\.?|volume|chapter)\s*)?\b([ivx]+|\d+)\s*$", re.I)


def _title_number(title: str) -> str:
    """The trailing part number of a title ("Big Talk II" → "ii"), or ''."""
    m = _NUMBER_RE.search(_plain(title))
    return m.group(1).casefold() if m and (m.group(1).isdigit() or len(m.group(1)) <= 4) else ""


# Releases that are never a song's own recording or cover unless the tags
# name them: "Red (Big Machine Radio Release Special)" holds a 58 s
# "Everything Has Changed" commentary and its own cover.
SPECIAL_RELEASE = re.compile(r"radio (?:release )?special|commentary|interview|karaoke|instrumental|8d audio|"
                             r"sped up|slowed", re.I)


def _version_names(title: str, ignore: Iterable[str] = ()) -> set:
    """The version parts of a title, as written: brackets or a " - " tail
    that carry a version label ("King Unique Radio Edit", "Radio Edit")."""
    from song_dedup import variant_labels
    parts = re.findall(r"[\(\[]([^\)\]]*)[\)\]]", title or "")
    if " - " in (title or ""):
        parts.append(title.split(" - ", 1)[1])
    return {_core(p) for p in parts if set(variant_labels(f"x ({p})")) - set(ignore)}


def result_matches(artist: str, title: str, album: str,
                   r_artists: Iterable[str], r_title: str, r_album: str, for_cover: bool = False) -> bool:
    if not _same(title, r_title):
        return False
    # "Big Talk" is not "Big Talk II": a part number must agree.
    if _title_number(title) != _title_number(r_title):
        return False
    # Brackets are ignored above, so "RISE (Remix)" would pass for "RISE":
    # a version label must agree, in the title and in the album. For a cover,
    # a clean or explicit edit is the same song on the same album ("Show Me
    # the Money (Clean Version)" is Deezer's "Show Me The Money"); as a
    # recording or a release listing it is not.
    from song_dedup import variant_labels
    edits = {"clean", "explicit"} if for_cover else set()
    labels = lambda text: set(variant_labels(text)) - edits
    if labels(title) != labels(r_title):
        return False
    # And the version itself, not just its kind: "King Unique Radio Edit" is
    # not "Olav Basoski Radio Edit" (a remix must not stand in for another).
    if labels(title) and _version_names(title, edits) != _version_names(r_title, edits):
        return False
    # Only when both name the same release: a track tagged with the wrong
    # album ("A Single Woman" for a remix single) must still find its cover.
    if album and _same(album, r_album) and labels(album) != labels(r_album):
        return False
    if SPECIAL_RELEASE.search(f"{r_title} {r_album}") and not SPECIAL_RELEASE.search(f"{title} {album}"):
        return False
    mine = _artists(artist)
    theirs = [n for a in r_artists for n in _artists(a)]
    performers = _performers(title, album) if for_cover else []
    if performers and not any(_same(p, n) for p in performers for n in theirs) \
            and not any(_core(p) in _core(r_album) for p in performers):
        return False                       # another recording of the work
    # An album named after the song (every single here) matches any other
    # artist's song of that name, so it only counts when it says more.
    album_says_more = bool(album) and not _same(album, title)
    return any(_same(m, t) for m in mine for t in theirs) or (album_says_more and _same(album, r_album))


def _queries(*candidates: str) -> List[str]:
    seen, out = set(), []
    for q in candidates:
        q = re.sub(r"\s+", " ", q or "").strip()
        if q and q.casefold() not in seen:
            seen.add(q.casefold())
            out.append(q)
    return out


def _deezer(artist: str, title: str, album: str) -> Optional[str]:
    a, t = _search_artist(artist), _plain(title)
    for query in _queries(f'artist:"{artist}" track:"{title}"', f'artist:"{a}" track:"{t}"', f"{a} {t}", t):
        try:
            data = requests.get("https://api.deezer.com/search", params={"q": query, "limit": 15},
                                timeout=_TIMEOUT).json().get("data") or []
        except Exception as exc:
            logger.debug(f"Deezer artwork search failed: {exc}")
            return None
        for r in data:
            names = [(r.get("artist") or {}).get("name", "")] + [c.get("name", "") for c in r.get("contributors") or []]
            if result_matches(artist, title, album, names, r.get("title", ""), (r.get("album") or {}).get("title", ""),
                              for_cover=True):
                return (r.get("album") or {}).get("cover_xl") or (r.get("album") or {}).get("cover_big")
    return None


def _itunes(artist: str, title: str, album: str) -> Optional[str]:
    try:
        data = requests.get("https://itunes.apple.com/search",
                            params={"term": f"{_search_artist(artist)} {_plain(title)}", "entity": "song", "limit": 15},
                            timeout=_TIMEOUT).json().get("results") or []
    except Exception as exc:
        logger.debug(f"iTunes artwork search failed: {exc}")
        return None
    for r in data:
        if result_matches(artist, title, album, [r.get("artistName", "")], r.get("trackName", ""),
                          r.get("collectionName", ""), for_cover=True):
            url = r.get("artworkUrl100") or ""
            return url.replace("100x100bb", "1200x1200bb") if url else None
    return None


def _deezer_album(artist: str, title: str, album: str) -> Optional[str]:
    """Art belongs to the album: when no track matches (a live cut, a
    YouTube rip), look the album itself up. Only a release with the same
    name and version labels by one of the credited artists counts."""
    if not album or _same(album, artist):
        return None
    a, al = _search_artist(artist), _plain(album)
    named = _performers(album, title)
    for query in _queries(f'artist:"{a}" album:"{al}"', f"{a} {al}", *([f"{a} {named[0]} {al}"] if named else [])):
        try:
            data = requests.get("https://api.deezer.com/search/album", params={"q": query, "limit": 10},
                                timeout=_TIMEOUT).json().get("data") or []
        except Exception as exc:
            logger.debug(f"Deezer album artwork search failed: {exc}")
            return None
        from song_dedup import variant_labels
        mine = _artists(artist)
        performers = _performers(album, title)
        for r in data:
            name = (r.get("artist") or {}).get("name", "")
            if variant_labels(album) != variant_labels(r.get("title", "")) or not any(_same(m, name) for m in mine):
                continue
            if not performers:
                if _same(album, r.get("title", "")):
                    return r.get("cover_xl") or r.get("cover_big")
                continue
            # The recording these performers made, not another of the work
            # (Perlman's Beethoven concerto with Barenboim and the Berlin Phil
            # is not his with Giulini); its album may add other pieces.
            if _core(al) not in _core(r.get("title", "")):
                continue
            try:
                credits = [c.get("name", "") for c in requests.get(f"https://api.deezer.com/album/{r.get('id')}",
                                                                   timeout=_TIMEOUT).json().get("contributors") or []]
            except Exception:
                continue
            if all(any(_same(p, c) for c in credits) for p in performers):
                return r.get("cover_xl") or r.get("cover_big")
    return None


_WITH_RE = re.compile(r"[\(\[]\s*with\s+([^\)\]]+)[\)\]]", re.I)


def _performers(*texts: str) -> List[str]:
    """Performers a title names in a "(with …)" bracket: "Beethoven: Violin
    Concerto (with Daniel Barenboim, Berliner Philharmoniker)"."""
    names: List[str] = []
    for text in texts:
        for m in _WITH_RE.finditer(text or ""):
            names += [p.strip() for p in re.split(r",|&|\band\b", m.group(1)) if _core(p)]
    return list(dict.fromkeys(names))


# A cover is shared by every edit of a song: the 184 s music-video cut of
# "Walk Man" takes the cover of the 166 s release. Artist, title and version
# labels still have to match; the length only rules out a different piece.
COVER_LENGTH_TOLERANCE = 30


def _close(seconds, duration, tolerance=COVER_LENGTH_TOLERANCE) -> bool:
    return not duration or not seconds or abs(float(seconds) - float(duration)) <= tolerance


def _spotify(artist: str, title: str, album: str, duration: Optional[float] = None) -> Optional[str]:
    """Spotify has most YouTube-released songs as proper releases."""
    try:
        import spotify_web
        results = spotify_web.get_client().search(f"{_search_artist(artist)} {_query(title)}")
    except Exception as exc:
        logger.debug(f"Spotify artwork search failed: {exc}")
        return None
    for track in (getattr(results, "tracks", None) or [])[:10]:
        flat = spotify_web._track_to_dict(track)
        if not flat or not flat.get("cover_url"):
            continue
        ms = getattr(track, "duration_ms", None)
        if not _close(ms / 1000 if ms else None, duration):
            continue
        if result_matches(artist, title, album, flat["artist"].split(", "), flat["title"], flat["album"],
                          for_cover=True):
            # 300 px search thumbnail → the 640 px cover
            return flat["cover_url"].replace("ab67616d00001e02", "ab67616d0000b273")
    return None


def _googleusercontent_full(url: str) -> str:
    url = re.sub(r"=w\d+-h\d+[^/]*$", "", url)
    return url + "=w1200-h1200-l90-rj"


def _ytmusic(kind: str, artist: str, title: str, duration: Optional[float]):
    try:
        from ytmusicapi import YTMusic
        return YTMusic().search(f"{_search_artist(artist)} {_query(title)}", filter=kind)[:10]
    except Exception as exc:
        logger.debug(f"YouTube Music search failed: {exc}")
        return []


def _ytmusic_song(artist: str, title: str, album: str, duration: Optional[float] = None) -> Optional[str]:
    """YouTube Music's song entries carry the official square album art."""
    for r in _ytmusic("songs", artist, title, duration):
        names = [a.get("name", "") for a in r.get("artists") or []]
        r_album = (r.get("album") or {}).get("name", "") if r.get("album") else ""
        thumbs = r.get("thumbnails") or []
        if thumbs and _close(r.get("duration_seconds"), duration) and \
                result_matches(artist, title, album, names, r.get("title", ""), r_album, for_cover=True):
            return _googleusercontent_full(thumbs[-1].get("url", ""))
    return None


def _youtube_thumbnail(artist: str, title: str, album: str, duration: Optional[float] = None) -> Optional[str]:
    """Last resort for songs that only exist on YouTube: the video's own
    thumbnail (cropped square when stored). Only a video by the artist (or
    naming them) with the same title and length counts."""
    from song_dedup import variant_labels
    def ytm_candidates():
        return [(r.get("videoId"), r.get("title", ""), [a.get("name", "") for a in r.get("artists") or []],
                 r.get("duration_seconds")) for r in _ytmusic("videos", artist, title, duration)]

    def youtube_candidates():
        try:
            import json
            import subprocess
            out = subprocess.run(["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings",
                                  f"ytsearch8:{_search_artist(artist)} {_query(title)}"],
                                 capture_output=True, text=True, timeout=60).stdout
            return [(e.get("id"), e.get("title", ""), [e.get("channel") or e.get("uploader") or ""], e.get("duration"))
                    for e in (json.loads(line) for line in out.splitlines() if line.strip())]
        except Exception as exc:
            logger.debug(f"YouTube search failed: {exc}")
            return []

    # Both searches together, so the best match wins rather than the first
    # one YouTube Music returned (a 192 s re-upload over the 207 s official video).
    return _matching_video(ytm_candidates() + youtube_candidates(), artist, title, duration)


# Videos about a song rather than of it: "Itzhak Perlman talks about the
# Beethoven Violin Concerto" (109 s) is no recording of the 47-minute concerto.
_NOT_A_RECORDING = re.compile(r"\b(?:talks?|interview|trailer|teaser|behind the scenes|making of|reaction|lesson|"
                              r"tutorial|explains?|documentary|preview|snippet|excerpt|unboxing|announcement)\b", re.I)


def own_youtube_upload(artist: str, title: str) -> Optional[Tuple[str, float]]:
    """(video id, seconds) of the artist's own YouTube upload of this title."""
    try:
        import json
        import subprocess
        out = subprocess.run(["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings",
                              f"ytsearch8:{_search_artist(artist)} {_query(title)}"],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception as exc:
        logger.debug(f"YouTube search failed: {exc}")
        return None
    from song_dedup import variant_labels
    for line in out.splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        v_title = e.get("title", "")
        if e.get("id") and e.get("duration") and _by_artist(e.get("channel") or e.get("uploader") or "", artist) \
                and _core(_plain(title)) in _core(v_title) and variant_labels(title) == variant_labels(v_title) \
                and _title_number(title) == _title_number(v_title) \
                and not (_NOT_A_RECORDING.search(v_title) and not _NOT_A_RECORDING.search(title)):
            return e["id"], float(e["duration"])
    return None


def youtube_source(title: str, duration: Optional[float], tolerance: float = 2) -> Optional[Tuple[str, str]]:
    """(channel, video id) of the YouTube video a rip came from: the very same
    title, word for word, and the same length to within a couple of seconds.
    "528 Hz Música de Sanación Profunda para Dormir | … " (43,189 s) is Soul
    Healing Música's, not the "Sonoterapia" the download was tagged with."""
    if not title or not duration or len(_core(title)) < 12:
        return None
    try:
        import json
        import subprocess
        out = subprocess.run(["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings", f"ytsearch8:{title}"],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception as exc:
        logger.debug(f"YouTube search failed: {exc}")
        return None
    for line in out.splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        channel = re.sub(r"\s*-\s*topic$", "", (e.get("channel") or e.get("uploader") or "").strip(), flags=re.I)
        if e.get("id") and channel and e.get("duration") and abs(float(e["duration"]) - duration) <= tolerance \
                and _core(e.get("title", "")) == _core(title):
            return channel, e["id"]
    return None


def own_upload_matches(artist: str, title: str, duration: Optional[float], tolerance: float = 3) -> bool:
    """The artist's own YouTube or SoundCloud upload of this title, within a
    few seconds of the file: proof of the recording for songs that exist
    nowhere else (Crack Amico's diss tracks)."""
    if not duration:
        return False
    from metadata_repair import title_known
    if not title_known(title):
        return False
    candidates = []
    for r in _ytmusic("videos", artist, title, duration):
        candidates.append((r.get("title", ""), [a.get("name", "") for a in r.get("artists") or []], r.get("duration_seconds")))
    try:
        import json
        import subprocess
        for kind in ("ytsearch6", "scsearch6"):
            out = subprocess.run(["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings",
                                  f"{kind}:{_search_artist(artist)} {_query(title)}"],
                                 capture_output=True, text=True, timeout=60).stdout
            for line in out.splitlines():
                if line.strip():
                    e = json.loads(line)
                    candidates.append((e.get("title", ""), [e.get("channel") or e.get("uploader") or ""], e.get("duration")))
    except Exception as exc:
        logger.debug(f"upload search failed: {exc}")
    for v_title, names, seconds in candidates:
        if seconds and abs(float(seconds) - float(duration)) <= tolerance and _core(_plain(title)) in _core(v_title) \
                and any(_by_artist(n, artist) for n in names):
            return True
    return False


def release_cover_url(path: str, meta: dict) -> Optional[str]:
    """The Cover Art Archive's front cover of the MusicBrainz release the file
    was tagged from (Lidarr and Picard write its ID), when that release is
    still this song's: same album and artist. Withdrawn releases keep theirs
    there after Deezer, iTunes and Spotify drop them (leaving only video
    stills to find elsewhere). None otherwise."""
    try:
        from mutagen import File as MuFile
        tags = MuFile(path, easy=True) or {}
        mbid = ((tags.get("musicbrainz_albumid") or [""])[0] or "").strip()
    except Exception:
        return None
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", mbid):
        return None
    try:
        rel = requests.get(f"https://musicbrainz.org/ws/2/release/{mbid}", params={"inc": "artist-credits", "fmt": "json"},
                           headers={"User-Agent": "MusicAI/1.0 (self-hosted music library)"}, timeout=_TIMEOUT).json()
    except Exception as exc:
        logger.debug(f"MusicBrainz release lookup failed for {mbid}: {exc}")
        return None
    names = [c.get("name", "") for c in rel.get("artist-credit") or [] if isinstance(c, dict)]
    if not (rel.get("cover-art-archive") or {}).get("front") or not _same(rel.get("title", ""), meta.get("album", "")) \
            or not any(_same(n, a) for n in names for a in _artists(meta.get("artist", ""))):
        return None
    return f"https://coverartarchive.org/release/{mbid}/front"


def verify_cover(path: str, meta: dict, dry_run: bool = False) -> str:
    """For a flagged song whose names hold: make its cover the release's.
    Returns 'matches the release', 'replaced from <source>', 'fetched from
    <source>', or 'not found'."""
    try:
        from mutagen import File as MuFile
        duration = MuFile(path).info.length
    except Exception:
        duration = None
    if not dry_run:
        _drop_placeholder_art(path)
    fresh, source = find_artwork(meta.get("artist", ""), meta.get("title", ""), meta.get("album", ""),
                                 [release_cover_url(path, meta)], duration)
    current = embedded_art(path)
    if not fresh:
        return "not found"
    if current and same_image(current, fresh):
        return "matches the release"
    if dry_run:
        return f"would {'replace' if current else 'fetch'} from {source}"
    folder = os.path.dirname(path)
    old_cover = folder_cover(folder)
    if _alone_in_folder(path) or not old_cover:
        if old_cover:
            os.remove(old_cover)
        with open(os.path.join(folder, "cover.jpg" if _mime(fresh) == "image/jpeg" else "cover.png"), "wb") as fh:
            fh.write(fresh)
    embed_art(path, fresh)
    return f"{'replaced' if current else 'fetched'} from {source}"


def _matching_video(candidates, artist: str, title: str, duration: Optional[float]) -> Optional[str]:
    """The artist's own upload first (their channel or "Artist - Topic"),
    then any upload naming them: a re-upload channel's branded thumbnail
    ("Rap Nation" for YBN Cordae) is the last choice."""
    from song_dedup import variant_labels
    own, others = [], []
    for video_id, v_title, names, seconds in candidates:
        if not video_id or not _close(seconds, duration):
            continue
        if _core(_plain(title)) not in _core(v_title) or variant_labels(title) != variant_labels(v_title):
            continue
        # Their own channel, also under a short form of the name ("Thao" for
        # Thao Sam, "Cordae" for YBN Cordae).
        channel_is_artist = any(_by_artist(n, artist) for n in names)
        gap = abs(float(seconds) - float(duration)) if seconds and duration else 0
        if channel_is_artist:
            own.append((gap, video_id))
        elif _core(_search_artist(artist)) in _core(v_title):
            others.append((gap, video_id))
    for _, video_id in sorted(own) + sorted(others):
        return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg|https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return None


def _by_artist(uploader: str, artist: str) -> bool:
    """An upload by the artist's own account ("Cordae" for YBN Cordae, who
    renamed; "THE REAL CRACK AMICO")."""
    from metadata_repair import name_within
    up = re.sub(r"\s*-\s*topic$", "", uploader or "", flags=re.I)
    return any(_same(up, a) or name_within(a, up) or name_within(up, a) for a in _artists(artist) if len(_core(a)) >= 4)


def _soundcloud(artist: str, title: str, album: str, duration: Optional[float] = None,
                own_only: bool = True) -> Optional[str]:
    """SoundCloud keeps the original artwork of songs that left (or never
    reached) the streaming services. Only the artist's own upload counts —
    re-uploaders' artwork is anything."""
    from song_dedup import variant_labels
    try:
        import json
        import subprocess
        out = subprocess.run(["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings",
                              f"scsearch8:{_search_artist(artist)} {_query(title)}"],
                             capture_output=True, text=True, timeout=60).stdout
        entries = [json.loads(line) for line in out.splitlines() if line.strip()]
    except Exception as exc:
        logger.debug(f"SoundCloud search failed: {exc}")
        return None
    best = []
    for e in entries:
        art = ((e.get("thumbnails") or [{}])[-1] or {}).get("url") or e.get("thumbnail") or ""
        if "artworks-" not in art or not _close(e.get("duration"), duration):
            continue
        if not _same(_plain(title), _plain(e.get("title", ""))) and _core(_plain(title)) not in _core(e.get("title", "")):
            continue
        if _title_number(title) != _title_number(e.get("title", "")) or variant_labels(title) != variant_labels(e.get("title", "")):
            continue
        if own_only and not _by_artist(e.get("uploader", ""), artist):
            continue
        gap = abs(float(e.get("duration") or 0) - float(duration)) if duration and e.get("duration") else 0
        best.append((gap, art))
    return sorted(best)[0][1] if best else None


def image_hash(image: bytes) -> Optional[int]:
    """A 64-bit average hash: the same cover at another size or quality
    hashes within a few bits."""
    import subprocess
    try:
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-vf", "scale=8:8,format=gray",
                              "-f", "rawvideo", "pipe:1"], input=image, capture_output=True, timeout=30).stdout
    except Exception:
        return None
    if len(raw) < 64:
        return None
    avg = sum(raw[:64]) / 64
    return sum(1 << i for i, v in enumerate(raw[:64]) if v >= avg)


def same_image(a: Optional[bytes], b: Optional[bytes], max_bits: int = 10) -> bool:
    ha, hb = (image_hash(a) if a else None), (image_hash(b) if b else None)
    return ha is not None and hb is not None and bin(ha ^ hb).count("1") <= max_bits


def _square(image: bytes) -> bytes:
    """Centre-crop a video thumbnail to a square cover: keep the 16:9 picture
    of a letterboxed 4:3 thumbnail, trim black bars a widescreen video adds,
    then take the centre square."""
    import subprocess

    def run(args, data):
        return subprocess.run(["ffmpeg", "-loglevel", "info", "-i", "pipe:0", *args], input=data,
                              capture_output=True, timeout=30)
    try:
        wide = run(["-vf", "crop=iw:min(ih\\,iw*9/16)", "-f", "mjpeg", "pipe:1"], image).stdout or image
        probe = run(["-vf", "cropdetect=limit=24:round=2:reset=0:skip=0", "-f", "null", "-"], wide).stderr
        found = re.findall(r"crop=(\d+:\d+:\d+:\d+)", probe.decode(errors="ignore"))
        trim = f"crop={found[-1]}," if found else ""
        out = run(["-vf", f"{trim}crop=min(iw\\,ih):min(iw\\,ih)", "-f", "mjpeg", "pipe:1"], wide).stdout
        return out if len(out) >= MIN_IMAGE_BYTES else image
    except Exception:
        return image


def find_artwork(artist: str, title: str, album: str = "",
                 hint_urls: Iterable[Optional[str]] = (), duration: Optional[float] = None) -> Tuple[Optional[bytes], str]:
    """(image bytes, source) or (None, ''). Sources in order: a cover link the
    fingerprint service returned or the Cover Art Archive's for the file's
    own MusicBrainz release, Deezer, iTunes, Spotify, the album on Deezer,
    YouTube Music's song entry, the artist's own SoundCloud upload, and last
    the YouTube video thumbnail.
    Nothing is invented: a song nothing matches has no cover (and goes to
    Manual Fix)."""
    for url in hint_urls:
        if url:
            image = _download(url)
            if image:
                return image, ("Cover Art Archive" if "coverartarchive.org" in url else "fingerprint service")
    finders = (("Deezer", lambda: _deezer(artist, title, album)),
               ("iTunes", lambda: _itunes(artist, title, album)),
               ("Spotify", lambda: _spotify(artist, title, album, duration)),
               ("Deezer album", lambda: _deezer_album(artist, title, album)),
               ("YouTube Music", lambda: _ytmusic_song(artist, title, album, duration)),
               ("SoundCloud", lambda: _soundcloud(artist, title, album, duration)),
               ("YouTube thumbnail", lambda: _youtube_thumbnail(artist, title, album, duration)))
    for name, finder in finders:
        found = finder()
        for url in (found.split("|") if found else []):
            image = _download(url)
            if image:
                return (_square(image) if name == "YouTube thumbnail" else image), name
    return None, ""


def _strip_art(path: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3
            tags = ID3(path)
            tags.delall("APIC")
            tags.save(path)
        elif ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(path)
            audio.clear_pictures()
            audio.save()
        elif ext in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4
            audio = MP4(path)
            audio.pop("covr", None)
            audio.save()
    except Exception as exc:
        logger.debug(f"Could not strip artwork from {path}: {exc}")


def _alone_in_folder(path: str) -> bool:
    try:
        names = os.listdir(os.path.dirname(path))
    except OSError:
        return False
    return [n for n in names if os.path.splitext(n)[1].lower() in AUDIO_EXTENSIONS] == [os.path.basename(path)]


def ensure_artwork(path: str, meta: dict, hint_urls: Iterable[Optional[str]] = (),
                   dry_run: bool = False, replace: bool = False) -> str:
    """Make sure the track shows artwork. Returns what happened:
    'present', 'embedded folder cover', 'folder cover from embedded art',
    'fetched from <source>', or 'not found'.

    replace=True is for a song whose identity just changed: the art it
    carries belongs to the old (wrong) names — often a re-upload's random
    picture — so a fresh cover is
    looked up, and with none found the old one is taken off (the song then
    goes to Manual Fix rather than keeping a wrong cover)."""
    folder = os.path.dirname(path)
    if replace and not os.path.exists(path):
        return "not found"
    if not dry_run:
        _drop_placeholder_art(path)
    if replace:
        from metadata_repair import artist_known
        image, source = (None, "")
        if artist_known(meta.get("artist")):
            try:
                from mutagen import File as MuFile
                duration = MuFile(path).info.length
            except Exception:
                duration = None
            image, source = find_artwork(meta.get("artist", ""), meta.get("title", ""), meta.get("album", ""),
                                         list(hint_urls) + [release_cover_url(path, meta)], duration)
        if dry_run:
            return f"would replace from {source}" if image else "not found"
        alone = _alone_in_folder(path)
        old_cover = folder_cover(folder)
        if image:
            if alone or not old_cover:
                if old_cover:
                    os.remove(old_cover)
                with open(os.path.join(folder, "cover.jpg" if _mime(image) == "image/jpeg" else "cover.png"), "wb") as fh:
                    fh.write(image)
            embed_art(path, image)
            return f"replaced from {source}"
        _strip_art(path)
        if alone and old_cover:
            os.remove(old_cover)
        return "not found"
    cover = folder_cover(folder)
    if has_embedded_art(path):
        if cover:
            return "present"
        # Plex picks an album's art from the folder more reliably than from
        # whichever track it reads first; without a folder cover an album can
        # show its artist's picture, or another album's art, instead.
        image = embedded_art(path)
        if not image:
            return "present"
        if not dry_run:
            with open(os.path.join(folder, "cover.jpg" if _mime(image) == "image/jpeg" else "cover.png"), "wb") as fh:
                fh.write(image)
        return "folder cover from embedded art"
    if cover:
        # The folder has art but the file doesn't: embed it so the art
        # survives the file being moved on its own.
        if not dry_run:
            with open(cover, "rb") as fh:
                embed_art(path, fh.read())
        return "embedded folder cover"
    from metadata_repair import artist_known
    if not artist_known(meta.get("artist")):
        # "Unidentified – Tenderly (2011)" would get some other "Tenderly"'s cover.
        return "not found"
    try:
        from mutagen import File as MuFile
        duration = MuFile(path).info.length
    except Exception:
        duration = None
    image, source = find_artwork(meta.get("artist", ""), meta.get("title", ""), meta.get("album", ""),
                                 list(hint_urls) + [release_cover_url(path, meta)], duration)
    if not image:
        return "not found"
    if not dry_run:
        with open(os.path.join(folder, "cover.jpg" if _mime(image) == "image/jpeg" else "cover.png"), "wb") as fh:
            fh.write(image)
        embed_art(path, image)
    return f"fetched from {source}"


def carry_cover(old_folder: str, new_folder: str) -> Optional[str]:
    """Bring the old folder's cover along when the last track leaves it.
    A folder still holding other songs keeps its cover: in a shared folder
    ("Artist/Artist") the cover belongs to whichever song wrote it, and
    copying it would give the moved song another song's cover."""
    import shutil
    cover = folder_cover(old_folder)
    if not cover or os.path.normpath(old_folder) == os.path.normpath(new_folder) or folder_cover(new_folder):
        return None
    if any(os.path.splitext(n)[1].lower() in AUDIO_EXTENSIONS for n in os.listdir(old_folder)):
        return None
    target = os.path.join(new_folder, os.path.basename(cover).lower())
    shutil.move(cover, target)
    return target
