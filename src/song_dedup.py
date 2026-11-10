"""Library-wide duplicate songs: find them, keep one copy, remove the rest
from the library, and purge what Plex remembers about the removed copies.

A duplicate is the same recording filed more than once — in two albums, or
twice in one folder. Two files are the same song
when their artist and title match (the fuzzy normalisation in lidarr_catalog:
punctuation, "(feat. …)" and "(Remastered)" ignored), they carry the same
version labels ("Live", "Remix", "Acoustic" … stay separate songs) and their
lengths agree within DURATION_TOLERANCE seconds or DURATION_TOLERANCE_SHARE
of the length, whichever is more (a YouTube rip of a song often carries a few
more seconds of silence than the release). Nothing is removed on
metadata alone: the audio fingerprints (Chromaprint, fpcalc) must match too,
which tells a re-encode apart from a different take of similar length.

Which copy survives, in order: a file Lidarr owns; a file under an album
root rather than elsewhere; the better Lidarr quality group; the higher
bitrate; a name without a copy number ("Song (2).mp3" is the second
download of "Song.mp3"); the older file. Files Lidarr owns are never removed — Lidarr would download a monitored
track again — so two Lidarr copies of a song (album + compilation) both stay.

Removed copies are moved out of the music tree into a quarantine folder
(Plex never sees them again; they can be restored by moving them back).

Plex keeps ratings and play history per song match, not per file, and
restores them when the song is re-added. When a removed copy was matched to a
different Plex song than the copy that stays, its rating and play count are
cleared first so they cannot come back later; when both copies share one Plex
song, that data belongs to the surviving copy and is left alone. Plex is then
rescanned and its trash emptied so the removed copies' entries disappear.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import library_state
from lidarr_catalog import LIDARR_QUALITY_ORDER, compact, local_quality
from naming import split_outside_brackets

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav", ".wma", ".aac"}
DURATION_TOLERANCE = 3.0          # seconds
DURATION_TOLERANCE_SHARE = 0.02   # of the shorter copy's length

# Words in a bracket or " - " suffix that make a different version of a song.
_VARIANT_PATTERNS = {
    "live": r"\blive\b|\bunplugged\b",
    "remix": r"\bre-?mix(?:ed)?\b|\bmix(?:ed)?\b|\brework\b|\bvip\b",
    "acoustic": r"\bacoustic\b",
    "instrumental": r"\binstrumental\b|\bkaraoke\b",
    "acapella": r"\ba ?cappella\b|\bacapella\b",
    "demo": r"\bdemo\b",
    "edit": r"\bradio edit\b|\bedit\b",
    "extended": r"\bextended\b",
    "cover": r"\bcover\b",
    "sped": r"\bsped up\b|\bnightcore\b",
    "slowed": r"\bslowed\b|\breverb\b",
    "rerecord": r"\bre-?record(?:ed|ing)?\b|\btaylor.s version\b",
    "clean": r"\bclean\b",
    "explicit": r"\bexplicit\b",
    "orchestral": r"\borchestral\b|\bsymphonic\b",
}
_VARIANT_RES = {name: re.compile(p, re.I) for name, p in _VARIANT_PATTERNS.items()}
_BRACKET_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_FEAT_RE = re.compile(r"\s*[\(\[]?\s*\b(?:feat\.?|ft\.?|featuring|with)\s+[^\)\]]*[\)\]]?", re.I)
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|;|&|\bx\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\band\b|/)\s*", re.I)
_TEMP_DIR_RE = re.compile(r"^ytdlp_")


@dataclass
class SongFile:
    path: str
    artist: str
    title: str
    duration: float
    track: Optional[int]
    bitrate: int
    quality_rank: int
    in_album_root: bool
    lidarr_owned: bool
    mtime: float
    key: Tuple[str, str, Tuple[str, ...]] = field(default=("", "", ()))


# ── identity ────────────────────────────────────────────────────────────────

def variant_labels(title: str) -> Tuple[str, ...]:
    """Version labels found in brackets or a trailing " - …" part of a title."""
    parts = _BRACKET_RE.findall(title or "")
    if " - " in (title or ""):
        parts.append(title.split(" - ", 1)[1])
    found = {name for name, rx in _VARIANT_RES.items() for p in parts if rx.search(p)}
    return tuple(sorted(found))


def title_core(title: str) -> str:
    t = _FEAT_RE.sub("", title or "")
    if " - " in t:
        head, tail = t.split(" - ", 1)
        if variant_labels(f"x - {tail}") or re.search(r"remaster|version|mono|stereo|\d{4}", tail, re.I):
            t = head
    return compact(t)          # brackets, punctuation and case removed


def primary_artist(artist: str) -> str:
    parts = split_outside_brackets(artist or "", _ARTIST_SPLIT_RE)
    return compact(parts[0] if parts else artist)


def song_key(artist: str, title: str) -> Tuple[str, str, Tuple[str, ...]]:
    return primary_artist(artist), title_core(title), variant_labels(title)


def same_recording(a: SongFile, b: SongFile) -> bool:
    if a.key != b.key or not a.key[1]:
        return False
    if not a.duration or not b.duration:
        return False            # can't tell edits apart without lengths
    tolerance = max(DURATION_TOLERANCE, DURATION_TOLERANCE_SHARE * min(a.duration, b.duration))
    if abs(a.duration - b.duration) > tolerance:
        return False
    # Two numbered tracks in one folder with the same name ("Interlude") are
    # different songs; "Song.mp3" and "Song_1.mp3" are not.
    if os.path.dirname(a.path) == os.path.dirname(b.path) and a.track and b.track and a.track != b.track:
        return False
    return True


# ── scanning ────────────────────────────────────────────────────────────────

def read_song_file(path: str, album_roots: Iterable[str], lidarr_owned: Set[str]) -> Optional[SongFile]:
    try:
        from mutagen import File as MuFile  # type: ignore
        mf = MuFile(path, easy=True)
    except Exception:
        mf = None
    if mf is None:
        return None

    def _get(*keys):
        for k in keys:
            try:
                v = mf.get(k) or []
            except Exception:
                v = []
            if v:
                return str(v[0]).strip()
        return ""

    p = Path(path)
    title = _get("title") or re.split(r"\s+-\s+\d{1,3}\s+-\s+", p.stem)[-1]
    artist = _get("artist", "albumartist") or p.parent.parent.name
    info = getattr(mf, "info", None)
    track_m = re.match(r"\s*(\d+)", _get("tracknumber"))
    quality = local_quality(path)
    norm = os.path.normpath(path)
    sf = SongFile(
        path=path,
        artist=artist,
        title=title,
        duration=float(getattr(info, "length", 0) or 0),
        track=int(track_m.group(1)) if track_m else None,
        bitrate=int(getattr(info, "bitrate", 0) or 0),
        quality_rank=LIDARR_QUALITY_ORDER.index(quality) if quality in LIDARR_QUALITY_ORDER else 0,
        in_album_root=any(norm.startswith(os.path.normpath(r) + os.sep) for r in album_roots),
        lidarr_owned=norm in lidarr_owned,
        mtime=p.stat().st_mtime,
    )
    sf.key = song_key(artist, title)
    return sf


def scan(roots: Iterable[str], album_roots: Iterable[str], lidarr_owned: Set[str],
         artist_filter: Optional[Set[str]] = None) -> List[SongFile]:
    album_roots = list(album_roots)
    files: List[SongFile] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for artist_dir in sorted(Path(root).iterdir()):
            if not artist_dir.is_dir() or _TEMP_DIR_RE.match(artist_dir.name):
                continue
            if artist_filter and compact(artist_dir.name) not in artist_filter:
                continue
            for p in artist_dir.rglob("*"):
                if p.suffix.lower() in AUDIO_EXTENSIONS and p.is_file() and not p.is_symlink():
                    sf = read_song_file(str(p), album_roots, lidarr_owned)
                    if sf:
                        files.append(sf)
    return files


# ── grouping and choice ─────────────────────────────────────────────────────

_COPY_NUMBER_RE = re.compile(r"(?: \(\d+\)|_\d+)$")


def keeper_rank(f: SongFile) -> tuple:
    """Lower is better."""
    copy = 1 if _COPY_NUMBER_RE.search(Path(f.path).stem) else 0
    return (0 if f.lidarr_owned else 1, 0 if f.in_album_root else 1, -f.quality_rank, -f.bitrate, copy, f.mtime)


def duplicate_groups(files: List[SongFile]) -> List[List[SongFile]]:
    by_key: Dict[tuple, List[SongFile]] = {}
    for f in files:
        if f.key[0] and f.key[1]:
            by_key.setdefault(f.key, []).append(f)
    groups: List[List[SongFile]] = []
    for same_key in by_key.values():
        if len(same_key) < 2:
            continue
        # Split by recording: start from the best copy and gather its duplicates.
        remaining = sorted(same_key, key=keeper_rank)
        while remaining:
            keeper = remaining.pop(0)
            dups = [f for f in remaining if same_recording(keeper, f)]
            remaining = [f for f in remaining if f not in dups]
            if dups:
                groups.append([keeper] + dups)
    return groups


FINGERPRINT_SECONDS = 120
FINGERPRINT_MATCH = 0.80     # share of matching fingerprint bits at the best alignment
_fp_cache: Dict[str, Optional[List[int]]] = {}
_fpcalc_missing_logged = False


def fingerprint(path: str) -> Optional[List[int]]:
    """Chromaprint raw fingerprint (fpcalc), cached per file."""
    global _fpcalc_missing_logged
    if path in _fp_cache:
        return _fp_cache[path]
    if not shutil.which("fpcalc"):
        if not _fpcalc_missing_logged:
            logger.warning("fpcalc not found — duplicates cannot be confirmed by audio, none will be removed")
            _fpcalc_missing_logged = True
        return None
    fp = None
    try:
        out = subprocess.run(["fpcalc", "-raw", "-length", str(FINGERPRINT_SECONDS), path],
                             capture_output=True, text=True, timeout=120).stdout
        for line in out.splitlines():
            if line.startswith("FINGERPRINT="):
                fp = [int(x) for x in line.split("=", 1)[1].split(",") if x]
    except Exception as exc:
        logger.debug(f"fpcalc failed for {path}: {exc}")
    _fp_cache[path] = fp
    return fp


def fingerprint_similarity(a: List[int], b: List[int], max_offset: int = 40) -> float:
    """Best share of equal bits over the overlap, trying small alignment shifts
    (one copy may start a fraction of a second later)."""
    best = 0.0
    for off in range(-max_offset, max_offset + 1):
        xs, ys = (a[off:], b) if off >= 0 else (a, b[-off:])
        n = min(len(xs), len(ys))
        if n < 50:
            continue
        diff = sum(bin(x ^ y).count("1") for x, y in zip(xs[:n], ys[:n]))
        best = max(best, 1.0 - diff / (32.0 * n))
    return best


def same_audio(a: str, b: str) -> bool:
    """Two files are the same recording by sound — separates re-encodes (≈0.95)
    from other takes, live versions or mis-tagged songs (usually well under 0.7)."""
    fa, fb = fingerprint(a), fingerprint(b)
    if not fa or not fb:
        return False
    score = fingerprint_similarity(fa, fb)
    if score < FINGERPRINT_MATCH:
        logger.info(f"  Not a duplicate by sound ({score:.2f}): {a} vs {b}")
    return score >= FINGERPRINT_MATCH


def audio_matches(a: SongFile, b: SongFile) -> bool:
    return same_audio(a.path, b.path)


def plan(files: List[SongFile], verify=audio_matches) -> List[Tuple[SongFile, List[SongFile]]]:
    """(keeper, copies to remove) per duplicate group. Lidarr-owned files stay,
    and a copy is only removed once `verify` confirms it sounds the same."""
    out = []
    for group in duplicate_groups(files):
        keeper, rest = group[0], group[1:]
        doomed = [f for f in rest if not f.lidarr_owned and verify(keeper, f)]
        if doomed:
            out.append((keeper, doomed))
    return out


def other_copy_exists(path: str, roots: Iterable[str], album_roots: Iterable[str]) -> Optional[str]:
    """Another file in the library that is the same recording as `path`."""
    album_roots = list(album_roots)
    me = read_song_file(path, album_roots, set())
    if not me:
        return None
    artist_filter = {me.key[0], compact(Path(path).parent.parent.name)}
    for f in scan(roots, album_roots, set(), artist_filter):
        if os.path.normpath(f.path) != os.path.normpath(path) and same_recording(me, f) and audio_matches(me, f):
            return f.path
    return None


# ── Plex ────────────────────────────────────────────────────────────────────

def _host_path(path: str) -> str:
    try:
        import paths
        return paths.container_to_host(path)
    except Exception:
        return path


def find_plex_track(section, f: SongFile):
    if section is None:
        return None
    host = _host_path(f.path)
    try:
        candidates = section.searchTracks(title=f.title[:60]) if f.title else []
    except Exception:
        candidates = []
    for t in candidates:
        for media in getattr(t, "media", []) or []:
            for part in getattr(media, "parts", []) or []:
                if part.file == host or part.file.endswith(f.path):
                    return t
    return None


def purge_plex_memory(dup_track, keeper_track) -> List[str]:
    """Clear rating/history of a copy being removed unless it is the same Plex
    song as the copy that stays (then the data is the survivor's)."""
    done: List[str] = []
    if dup_track is None:
        return done
    if keeper_track is not None and getattr(dup_track, "guid", None) == getattr(keeper_track, "guid", None):
        return done
    try:
        if getattr(dup_track, "userRating", None):
            dup_track.rate(None)
            done.append("rating")
        if getattr(dup_track, "viewCount", 0):
            dup_track.markUnplayed()
            done.append("play history")
    except Exception as exc:
        logger.debug(f"Could not purge Plex data for {getattr(dup_track, 'title', '?')}: {exc}")
    return done


def refresh_plex(section) -> None:
    """Rescan so Plex notices the missing files, then empty its trash so the
    removed copies' entries disappear instead of lingering as unavailable."""
    if section is None:
        return
    try:
        section.update()
        time.sleep(15)
        section.emptyTrash()
        logger.info("Plex rescanned and trash emptied")
    except Exception as exc:
        logger.warning(f"Plex refresh after duplicate removal failed: {exc}")


# ── the pass ────────────────────────────────────────────────────────────────

@dataclass
class DedupResult:
    removed: List[Tuple[str, str, str]] = field(default_factory=list)   # (removed, kept, quarantined at)
    purged: List[Tuple[str, List[str]]] = field(default_factory=list)
    errors: List[Tuple[str, str]] = field(default_factory=list)

    def kept_for(self, path: str) -> Optional[str]:
        norm = os.path.normpath(path)
        return next((k for d, k, _ in self.removed if os.path.normpath(d) == norm), None)


def quarantine(path: str, quarantine_dir: str, stamp: str) -> str:
    """Move a file out of the music tree, keeping its path so it can be restored."""
    dest = Path(quarantine_dir) / stamp / Path(path).resolve().as_posix().lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(path, dest)
    return str(dest)


def tidy_folder(folder: Path, roots: Iterable[str], quarantine_dir: str, stamp: str) -> None:
    """A folder left with no audio goes to quarantine too (cover art and all)
    so Plex does not keep an empty album; then empty parent folders are removed."""
    stops = {os.path.normpath(r) for r in roots}
    if not folder.is_dir() or os.path.normpath(str(folder)) in stops:
        return
    if any(p.suffix.lower() in AUDIO_EXTENSIONS for p in folder.rglob("*")):
        return
    for p in [p for p in folder.rglob("*") if p.is_file()]:
        quarantine(str(p), quarantine_dir, stamp)
    for d in sorted([d for d in folder.rglob("*") if d.is_dir()], reverse=True) + [folder]:
        try:
            d.rmdir()
        except OSError:
            return
    parent = folder.parent
    if os.path.normpath(str(parent)) not in stops:
        try:
            parent.rmdir()        # only succeeds if empty
        except OSError:
            pass


def dedup(roots: List[str], album_roots: List[str], lidarr_owned: Set[str] = frozenset(),
          plex_section=None, dry_run: bool = False, artist_filter: Optional[Set[str]] = None,
          quarantine_dir: str = "/app/data/merge_quarantine", verify=audio_matches) -> DedupResult:
    result = DedupResult()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    files = scan(roots, album_roots, set(lidarr_owned), artist_filter)
    work = plan(files, verify)
    logger.info(f"Song dedup: {len(files)} files scanned, {len(work)} duplicate group(s)")
    touched = False
    for keeper, doomed in work:
        keeper_track = find_plex_track(plex_section, keeper)
        for f in doomed:
            why = (f"same recording as {keeper.path} "
                   f"({'Lidarr' if keeper.lidarr_owned else 'album root' if keeper.in_album_root else 'elsewhere'}, "
                   f"{LIDARR_QUALITY_ORDER[keeper.quality_rank]} vs {LIDARR_QUALITY_ORDER[f.quality_rank]})")
            if dry_run:
                logger.info(f"  [DRY RUN] would remove {f.path} — {why}")
                result.removed.append((f.path, keeper.path, ""))
                continue
            try:
                purged = purge_plex_memory(find_plex_track(plex_section, f), keeper_track)
                if purged:
                    result.purged.append((f.path, purged))
                doomed_state = library_state.state_of(f.path)
                where = quarantine(f.path, quarantine_dir, stamp)
                library_state.unmark(f.path)
                if doomed_state is None:
                    # The copy that goes was in the library for good: so is the one kept.
                    library_state.unmark(keeper.path)
                result.removed.append((f.path, keeper.path, where))
                touched = True
                logger.info(f"  🗑️ Removed duplicate {f.path} — {why}"
                            + (f"; cleared Plex {', '.join(purged)}" if purged else ""))
                tidy_folder(Path(f.path).parent, roots, quarantine_dir, stamp)
            except Exception as exc:
                result.errors.append((f.path, str(exc)))
                logger.warning(f"  Could not remove duplicate {f.path}: {exc}")
    if touched:
        refresh_plex(plex_section)
    return result
