"""album_artist_merger.py — Album and artist merging / deduplication for MusicAI.

Responsibilities
----------------
1. **Artist normalisation** — resolve spelling variants, featured-artist noise,
   and different capitalisation to a single canonical name.
2. **Album deduplication** — detect duplicate albums on disk (same artist+title,
   multiple directories) and merge them into one directory, keeping the best
   quality files (the lower-quality duplicate is removed, never symlinked).
3. **Lidarr reconcile and song dedup** — see `lidarr_reconcile`. All users
   share one library, so duplicates are removed, never linked.
4. **Plex library merge** — after the file-level merge, optionally trigger a
   Plex library scan and use the ``/library/sections/{id}/all`` API to detect
   Plex-level duplicate albums (split by metadata mismatch) and merge them via
   ``Album.mergeInto()``.
5. **Reporting** — every merge/dedup action is logged and optionally stored in
   the music_ai.db ``merges`` table so it can be reviewed and undone.

Usage from app.py
-----------------
    from album_artist_merger import AlbumArtistMerger

    merger = AlbumArtistMerger(
        plex=self.plex,                 # PlexServer instance (or None)
        plex_music_section=PLEX_MUSIC_SECTION,
        album_dir=ALBUM_DIR,
        db_path=DB_PATH,
    )
    report = merger.run_full_merge()    # returns MergeReport dataclass
    logger.info(report.summary())

All methods are safe to call repeatedly — the merger is fully idempotent.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import library_owner
import library_state

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / tunables
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav", ".wma", ".aac"}

# Artist/album directory names that should NEVER be merged or renamed.
# These are catch-all or placeholder names with no real semantic value.
_JUNK_ARTIST_NAMES = {
    "various artists", "various", "va", "v.a.", "unknown", "unknown artist",
    "unknown artists", "",
}
_JUNK_ALBUM_NAMES = {
    "unknown", "unknown album", "untitled", "miscellaneous", "",
}

def _is_junk_artist(name: str) -> bool:
    """Return True if this artist name is a catch-all placeholder."""
    return _normalise_for_compare(name) in _JUNK_ARTIST_NAMES

def _is_junk_album(name: str) -> bool:
    """Return True if this album name is a meaningless placeholder."""
    return _normalise_for_compare(name) in _JUNK_ALBUM_NAMES


# Similarity threshold above which two artist names are considered the same
ARTIST_SIMILARITY_THRESHOLD = 0.85

# Similarity threshold above which two album titles are considered the same

# When comparing file quality, prefer this order (index 0 = best)
FORMAT_QUALITY_ORDER = [".flac", ".m4a", ".opus", ".ogg", ".mp3", ".aac", ".wma", ".wav"]

# Regex fragments stripped from artist/album names before comparison
_STRIP_PATTERNS = [
    r"\s*\(feat(?:uring)?\.?\s[^)]+\)",   # (feat. X)
    r"\s*\[feat(?:uring)?\.?\s[^\]]+\]",  # [feat. X]
    r"\s+feat(?:uring)?\.?\s+\S.*$",      # " feat. X" at end
    r"\s+ft\.?\s+\S.*$",                  # " ft. X" at end
    r"\s*-\s*single$",                    # "- Single"
    r"\s*-\s*ep$",                        # "- EP"
    r"\s*\(\d+\)$",                        # "(1)", "(2)" — filesystem numbered copies
    r"\s*\(explicit\)$",                  # "(Explicit)"
    r"\s*\(clean\)$",                     # "(Clean)"
    r"\s*\(deluxe\s*(?:edition)?\)$",     # "(Deluxe Edition)"
    r"\s*\(expanded\s*(?:edition)?\)$",
    r"\s*\(remastered(?:\s+\d{4})?\)$",
    r"\s*\(\d{4}\s*remaster(?:ed)?\)$",
    r"\s*\[\d{4}\s*remaster(?:ed)?\]$",
    # Same patterns with dash separator instead of parentheses
    r"\s*[-–—]\s*single$",
    r"\s*[-–—]\s*ep$",
    r"\s*[-–—]\s*explicit$",
    r"\s*[-–—]\s*deluxe(?:\s+edition)?$",
    r"\s*[-–—]\s*expanded(?:\s+edition)?$",
    r"\s*[-–—]\s*remaster(?:ed)?(?:\s+\d{4})?$",
    r"\s*[-–—]\s*\d{4}\s*remaster(?:ed)?$",
]
_STRIP_RE = re.compile("|".join(_STRIP_PATTERNS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MergeAction:
    """Single merge/dedup action performed by the merger."""
    action:     str   # "dedup_file", "merge_album_dir", "merge_plex_album", "normalize_artist"
    source:     str   # path / name being removed/replaced
    target:     str   # canonical path / name
    reason:     str   # human-readable explanation
    success:    bool  = True
    error:      str   = ""


@dataclass
class MergeReport:
    """Accumulated results of a full merge run."""
    started_at:   float = field(default_factory=time.time)
    finished_at:  float = 0.0
    actions:      List[MergeAction] = field(default_factory=list)

    # Counters
    artist_normalizations:  int = 0
    album_dir_merges:       int = 0
    file_dedups:            int = 0
    plex_album_merges:      int = 0
    lidarr_moves:           int = 0
    song_dedups:            int = 0
    loose_filed:            int = 0
    errors:                 int = 0

    def record(self, action: MergeAction) -> None:
        self.actions.append(action)
        if not action.success:
            self.errors += 1
        elif action.action == "normalize_artist":
            self.artist_normalizations += 1
        elif action.action == "merge_album_dir":
            self.album_dir_merges += 1
        elif action.action == "dedup_file":
            self.file_dedups += 1
        elif action.action == "merge_plex_album":
            self.plex_album_merges += 1
        elif action.action == "lidarr_move":
            self.lidarr_moves += 1
        elif action.action == "song_dedup":
            self.song_dedups += 1
        elif action.action == "file_loose":
            self.loose_filed += 1

    def summary(self) -> str:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return (
            f"AlbumArtistMerger completed in {elapsed:.1f}s — "
            f"artist normalisations: {self.artist_normalizations}, "
            f"album dir merges: {self.album_dir_merges}, "
            f"file dedups: {self.file_dedups}, "
            f"Lidarr moves: {self.lidarr_moves}, "
            f"duplicate songs removed: {self.song_dedups}, "
            f"loose tracks filed: {self.loose_filed}, "
            f"Plex album merges: {self.plex_album_merges}, "
            f"errors: {self.errors}"
        )


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

def _nfc(s: str) -> str:
    """Unicode NFC normalisation."""
    return unicodedata.normalize("NFC", s)


def _fold(s: str) -> str:
    """Lowercase, NFC-normalise, strip leading/trailing whitespace."""
    return _nfc(s).lower().strip()


def _strip_noise(s: str) -> str:
    """Remove featuring clauses, edition suffixes, etc. before comparison."""
    return _STRIP_RE.sub("", s).strip()


def _normalise_for_compare(s: str) -> str:
    """Fully normalised string for fuzzy comparison (no punctuation, folded)."""
    s = _strip_noise(_fold(s))
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio on fully normalised strings."""
    return SequenceMatcher(None, _normalise_for_compare(a), _normalise_for_compare(b)).ratio()


def _canonical_artist_name(raw: str) -> str:
    """Return the primary artist (strip featuring artists, normalise spacing).

    'Artist feat. Someone'  → 'Artist'
    Strips trailing/leading whitespace and collapses internal spaces.
    """
    s = _strip_noise(raw.strip())
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s or raw.strip()


_FEAT_CREDIT_RE = re.compile(r"\s*[\(\[]?\s*(?<!\w)(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)


def _repair_mojibake(name: str) -> str:
    """'Montr\u00c3\u00a9al' (UTF-8 read as Latin-1) → 'Montréal'; other names unchanged."""
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _artist_folder_key(name: str) -> str:
    """Folders of one artist share this key: the whole name with case, accents,
    punctuation and broken encoding ignored, and a "feat." credit dropped.

    A name is never split at "&", "," or "and": "Earth, Wind & Fire",
    "Simon & Garfunkel" and "Dana and the Wolf" are artists, not the first
    word of a list.
    """
    from lidarr_catalog import compact
    name = _repair_mojibake(name)
    return compact(_FEAT_CREDIT_RE.sub("", name).strip() or name)


# Bracketed or trailing words that only name an edition of the same album.
_EDITION_NOISE_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(?:deluxe|edition|remaster(?:ed)?|expanded|bonus|anniversary|version|explicit|clean|"
    r"\d{4})\b[^\)\]]*[\)\]]|\s+-\s+(?:single|ep)\s*$", re.IGNORECASE)


def _album_folder_key(name: str) -> str:
    """Folders of one album share this key: the name with edition noise
    ("(Deluxe Edition)", "(2019)", "- Single") dropped, case and punctuation
    ignored. Every other word and every number must match: "The Blueprint 3",
    "Bleach, Vol. 2" and "Life Is Like A Boat (Classics Tokyo Sessions)" are
    albums of their own."""
    from lidarr_catalog import compact
    previous = None
    while previous != name:
        previous, name = name, _EDITION_NOISE_RE.sub("", name).strip()
    return compact(re.sub(r"[\(\[\]\)]", " ", name))


def _normalize_album_name(raw: str) -> str:
    """Aggressively normalise an album name for duplicate detection.

    Strips featuring clauses, ALL parenthetical/bracketed content,
    '- Single', and '- Original Motion Picture Score' suffixes.

    Examples::
        'Vendetta (Non-Choir)'               → 'Vendetta'
        'Thriller (Deluxe Edition)'          → 'Thriller'
        'Lover - Single'                     → 'Lover'
        'Interstellar - Original … Score'    → 'Interstellar'
    """
    album = raw.strip()
    # Remove featuring patterns first (greedy OK since feat clause ends at ) or ])
    album = re.sub(
        r"\s*[\(\[]?\s*(feat\.?|featuring|ft\.?|with)\s+.*?[\)\]]?\s*",
        "", album, flags=re.IGNORECASE,
    )
    # Strip ALL remaining parenthetical / bracketed content
    album = re.sub(r"\s*[\(\[].*?[\)\]]", "", album)
    # Remove "- Single" suffix
    album = re.sub(r"\s*-\s*Single\s*$", "", album, flags=re.IGNORECASE)
    # Remove "- Original Motion Picture Score" variations
    album = re.sub(
        r"\s*-\s*Original\s+Motion\s+Picture\s+Score\s*$",
        "", album, flags=re.IGNORECASE,
    )
    result = _sanitize_dirname(album.strip())
    return result if result else _sanitize_dirname(raw.strip())


def _file_quality_rank(path: str) -> int:
    """Lower rank = higher quality.  Unknown extensions get rank len(ORDER)."""
    ext = Path(path).suffix.lower()
    try:
        return FORMAT_QUALITY_ORDER.index(ext)
    except ValueError:
        return len(FORMAT_QUALITY_ORDER)



def _read_audio_tags(path: str) -> Dict[str, str]:
    """Read artist / title / album from an audio file using mutagen.

    Returns a dict with keys: 'artist', 'title', 'album' (all strings,
    empty string if not available).
    """
    result = {"artist": "", "title": "", "album": ""}
    try:
        from mutagen import File as MuFile  # type: ignore
        mf = MuFile(path, easy=True)
        if mf is None:
            return result
        def _get(k):
            v = mf.get(k) or []
            return str(v[0]).strip() if v else ""
        result["artist"] = _get("artist") or _get("albumartist")
        result["title"]  = _get("title")
        result["album"]  = _get("album")
    except Exception as exc:
        logger.debug(f"_read_audio_tags({path}): {exc}")
    return result


def _first_int(value) -> Optional[int]:
    m = re.match(r"\s*(\d+)", str(value or ""))
    return int(m.group(1)) if m else None


def _read_track_tags(path: str) -> Dict[str, object]:
    """Album-identity tags for one file (empty values when unreadable)."""
    out: Dict[str, object] = {"title": "", "album": "", "albumartist": "", "track": None,
                              "disc": None, "year": None, "albumid": "", "releasegroupid": ""}
    try:
        from mutagen import File as MuFile  # type: ignore
        mf = MuFile(path, easy=True)
        if mf is None:
            return out

        def _get(*keys):
            for k in keys:
                try:
                    v = mf.get(k) or []
                except Exception:
                    v = []
                if v:
                    return str(v[0]).strip()
            return ""

        out["title"] = _get("title")
        out["album"] = _get("album")
        out["albumartist"] = _get("albumartist", "artist")
        out["track"] = _first_int(_get("tracknumber"))
        out["disc"] = _first_int(_get("discnumber"))
        out["year"] = _first_int(_get("originaldate", "date"))
        out["albumid"] = _get("musicbrainz_albumid")
        out["releasegroupid"] = _get("musicbrainz_releasegroupid")
    except Exception as exc:
        logger.debug(f"_read_track_tags({path}): {exc}")
    return out


def _track_key(tags: Dict[str, object], path: Path) -> str:
    """Identity of a track within an album: its title, ignoring punctuation and (skit)-style brackets."""
    from lidarr_catalog import compact
    title = str(tags.get("title") or "")
    if not title:
        # "Artist - Album - 06 - My Band" or "My Band"
        title = re.split(r"\s+-\s+\d{1,3}\s+-\s+", path.stem)[-1]
    return compact(title)


def _most_common(values: List[str]) -> List[str]:
    """Distinct values, most frequent first."""
    counts: Dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return sorted(counts, key=lambda v: -counts[v])


def _write_album_tags(path: str, album) -> None:
    """Tag a file with Lidarr's album title, album artist and selected release."""
    from mutagen import File as MuFile  # type: ignore
    mf = MuFile(path, easy=True)
    if mf is None:
        raise ValueError(f"unreadable audio file: {path}")
    mf["album"] = album.title
    mf["albumartist"] = album.artist_name
    for key, value in (("musicbrainz_releasegroupid", album.release_group_id),
                       ("musicbrainz_albumid", album.selected_release_id)):
        if value:
            try:
                mf[key] = value
            except Exception:
                pass  # container without a slot for this tag (e.g. some MP4s)
    mf.save()


def _sanitize_dirname(name: str) -> str:
    """Sanitize a string for use as a directory name component."""
    name = _nfc(name).strip()
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name)
    name = re.sub(r"[\\/:*?\"<>|]", "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200] or "Unknown"


# ---------------------------------------------------------------------------
# Core merger class
# ---------------------------------------------------------------------------

class AlbumArtistMerger:
    """Detects and resolves duplicate artists and albums on disk and in Plex.

    Parameters
    ----------
    plex:
        A ``plexapi.server.PlexServer`` instance.  Pass ``None`` to skip all
        Plex-level operations (file-level merging still runs).
    plex_music_section:
        Name of the Plex music library section (default ``"Music"``).
    album_dir:
        Root albums directory (e.g. ``/music/albums``).
    db_path:
        Path to the SQLite database.  A ``merges`` table is created if absent.
    dry_run:
        When ``True`` every action is logged but NO files are moved or
        deleted and Plex is not modified.  Default ``False``.
    """

    def __init__(
        self,
        plex=None,
        plex_music_section: str = "Music",
        album_dir: str = "/music/albums",
        db_path: str = "/app/data/music_ai.db",
        dry_run: bool = False,
        lidarr=None,
        quarantine_dir: str = "/app/data/merge_quarantine",
    ) -> None:
        self.plex               = plex
        self.plex_music_section = plex_music_section
        self.album_dir          = album_dir
        self.db_path            = db_path
        self.dry_run            = dry_run
        self.lidarr             = lidarr      # LidarrCatalog or None
        self.quarantine_dir     = quarantine_dir
        self._quarantine_stamp  = time.strftime("%Y%m%d-%H%M%S")
        self._lidarr_rescan_targets: set = set()
        self.artist_filter: Optional[set] = None   # compact artist names; None = all
        self._ensure_db()

    def _artist_selected(self, name: str) -> bool:
        if not self.artist_filter:
            return True
        from lidarr_catalog import compact
        return compact(name) in self.artist_filter

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_full_merge(self) -> MergeReport:
        """Run all merge / dedup passes in the correct order.

        Pass order:
        0. File loose songs lying in a library root.
        1. Normalise artist directory names on disk.
        2. Merge duplicate album directories under the same artist.
        3. Lidarr reconcile (folder names Lidarr decides).
        4. Song-level duplicates (same recording by sound) to the quarantine.
        5. Plex-level album merge (split albums → single album object).

        Returns a :class:`MergeReport` with counts and details of every action.
        """
        report = MergeReport()
        logger.info("=== AlbumArtistMerger: starting full merge run ===")
        if self.dry_run:
            logger.info("  [DRY RUN] no files or Plex objects will be modified")

        try:
            self._pass_file_loose_tracks(report)
        except Exception as exc:
            logger.error(f"Loose-track filing pass failed: {exc}", exc_info=True)

        try:
            self._pass_normalise_artist_dirs(report)
        except Exception as exc:
            logger.error(f"Artist normalisation pass failed: {exc}", exc_info=True)

        try:
            self._pass_merge_duplicate_album_dirs(report)
        except Exception as exc:
            logger.error(f"Album dir merge pass failed: {exc}", exc_info=True)

        try:
            self._pass_lidarr_reconcile(report)
        except Exception as exc:
            logger.error(f"Lidarr reconcile pass failed: {exc}", exc_info=True)

        try:
            self._pass_song_dedup(report)
        except Exception as exc:
            logger.error(f"Song dedup pass failed: {exc}", exc_info=True)

        try:
            self._pass_plex_merge_split_albums(report)
        except Exception as exc:
            logger.error(f"Plex album merge pass failed: {exc}", exc_info=True)

        report.finished_at = time.time()
        logger.info(f"=== AlbumArtistMerger: {report.summary()} ===")
        self._persist_report(report)
        return report

    def normalise_artist_dirs(self) -> MergeReport:
        """Run only the artist-name normalisation pass (all album roots)."""
        report = MergeReport()
        self._pass_normalise_artist_dirs(report)
        report.finished_at = time.time()
        self._persist_report(report)
        return report

    def merge_duplicate_album_dirs(self) -> MergeReport:
        """Run only the duplicate album directory merge pass (all album roots)."""
        report = MergeReport()
        self._pass_merge_duplicate_album_dirs(report)
        report.finished_at = time.time()
        self._persist_report(report)
        return report

    def plex_merge_split_albums(self) -> MergeReport:
        """Run only the Plex-level split-album merge pass."""
        report = MergeReport()
        self._pass_plex_merge_split_albums(report)
        report.finished_at = time.time()
        self._persist_report(report)
        return report

    def lidarr_reconcile(self) -> MergeReport:
        """File loose tracks, fold album copies into Lidarr's version, remove
        duplicate songs, then merge Plex splits."""
        report = MergeReport()
        if self.dry_run:
            logger.info("  [DRY RUN] no files or Plex objects will be modified")
        try:
            self._pass_file_loose_tracks(report)
        except Exception as exc:
            logger.error(f"Loose-track filing pass failed: {exc}", exc_info=True)
        try:
            self._pass_lidarr_reconcile(report)
        except Exception as exc:
            logger.error(f"Lidarr reconcile pass failed: {exc}", exc_info=True)
        try:
            self._pass_song_dedup(report)
        except Exception as exc:
            logger.error(f"Song dedup pass failed: {exc}", exc_info=True)
        try:
            self._pass_plex_merge_split_albums(report)
        except Exception as exc:
            logger.error(f"Plex album merge pass failed: {exc}", exc_info=True)
        report.finished_at = time.time()
        logger.info(report.summary())
        self._persist_report(report)
        return report

    # ------------------------------------------------------------------
    # Loose tracks
    # ------------------------------------------------------------------

    @staticmethod
    def _existing_child(parent: Path, name: str) -> Optional[Path]:
        """A folder in `parent` that is `name` up to case and punctuation
        ("ORANGE RANGE" for "Orange Range"), so filing doesn't fork it.
        Bracketed words count: "Life is Like a Boat (Classics Tokyo Sessions)"
        is another release than "Life Is Like A Boat"."""
        def key(s: str) -> str:
            return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s).casefold())

        if not parent.is_dir():
            return None
        want = key(name) or name
        for child in parent.iterdir():
            if child.is_dir() and (key(child.name) or child.name) == want:
                return child
        return None

    def _pass_file_loose_tracks(self, report: MergeReport) -> None:
        """File audio lying directly in a library root into Artist/Album/.

        Downloads that finish after the downloader gave up on them (a deemix
        thread outliving its timeout) land as "Artist - Title.mp3" in the
        root, where Plex shows each as an album of its own. They are filed
        from their own tags (album artist, album, title). A file whose tags
        lack an artist or title is left where it is.
        """
        from fix_metadata import _sanitize_filename
        for root in self._all_album_roots():
            for path in sorted(Path(root).iterdir()):
                if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                tags = _read_track_tags(str(path))
                artist, title = str(tags["albumartist"] or ""), str(tags["title"] or "")
                if not self._artist_selected(artist or path.stem):
                    continue
                if not artist or not title:
                    logger.info(f"  Loose file left in place (no artist/title tags): {path}")
                    continue
                folder = str(tags["album"] or title)
                artist_dir = self._existing_child(Path(root), artist) or Path(root) / _sanitize_filename(artist)
                album_dir = self._existing_child(artist_dir, folder) or artist_dir / _sanitize_filename(folder)
                dest = album_dir / f"{_sanitize_filename(title)}{path.suffix.lower()}"
                if dest.exists():
                    dest = album_dir / path.name
                action = MergeAction(action="file_loose", source=str(path), target=str(dest),
                                     reason="Loose file in a library root")
                if dest.exists():
                    action.success, action.error = False, "a file of that name is already there"
                    logger.info(f"  Loose file not filed, {dest} exists: {path}")
                elif self.dry_run:
                    logger.info(f"  [DRY RUN] would file {path} → {dest}")
                else:
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(path), str(dest))
                        library_state.moved(str(path), str(dest))
                        logger.info(f"  Filed loose track {path.name} → {dest}")
                    except Exception as exc:
                        action.success, action.error = False, str(exc)
                        logger.warning(f"  Could not file {path}: {exc}")
                report.record(action)

    # ------------------------------------------------------------------
    # Lidarr-first reconcile
    # ------------------------------------------------------------------

    def _pass_lidarr_reconcile(self, report: MergeReport) -> None:
        """Fuzzy-match every album folder to Lidarr and let Lidarr's setup win.

        For a local album that is a copy of a Lidarr album (matched by
        MusicBrainz ID, else by artist + fuzzy title):

        * tracks Lidarr's folder already has are left for the song-dedup pass;
        * other tracks move into Lidarr's folder, renamed with Lidarr's track
          format and tagged with its album title/release (a song on probation
          stays on probation there).
        """
        if not self.lidarr:
            logger.debug("Lidarr reconcile: no Lidarr configured — skipping")
            return
        logger.info("Pass 3: Lidarr-first album reconcile")
        self.lidarr.load()

        for root in self._all_album_roots():
            for artist_dir in sorted(Path(root).iterdir()):
                if not artist_dir.is_dir() or _is_junk_artist(artist_dir.name):
                    continue
                if not self._artist_selected(artist_dir.name):
                    continue
                for album_dir in sorted(artist_dir.iterdir()):
                    if not album_dir.is_dir() or _is_junk_album(album_dir.name):
                        continue
                    try:
                        self._reconcile_album_dir(album_dir, report)
                    except Exception as exc:
                        logger.warning(f"  Lidarr reconcile failed for {album_dir}: {exc}")
                if not self.dry_run:
                    self._rmdir_if_empty(artist_dir)

        if self._lidarr_rescan_targets and not self.dry_run:
            # The moved files are root's; Lidarr must own them to rename and tag them.
            for root in self._all_album_roots():
                library_owner.adopt(root)
            self.lidarr.rescan(self._lidarr_rescan_targets)

    def _reconcile_album_dir(self, album_dir: Path, report: MergeReport) -> bool:
        files = [p for p in sorted(album_dir.rglob("*"))
                 if p.is_file() and not p.is_symlink() and p.suffix.lower() in AUDIO_EXTENSIONS]
        if not files:
            return False
        tags = [_read_track_tags(str(p)) for p in files]

        album_titles = _most_common([t["album"] for t in tags if t["album"]]) + [album_dir.name]
        artist = (_most_common([t["albumartist"] for t in tags if t["albumartist"]]) or [album_dir.parent.name])[0]
        mbids = {m for t in tags for m in (t["releasegroupid"], t["albumid"]) if m}
        years = [t["year"] for t in tags if t["year"]]
        year = years[0] if years else None
        la = self.lidarr.match_album(artist, album_titles, mbids, year)
        if la is None and artist != album_dir.parent.name:
            la = self.lidarr.match_album(album_dir.parent.name, album_titles, (), year)
        if la is None:
            return False
        mbid_agrees = bool(mbids & ({la.release_group_id} | set(la.release_ids)))
        if not self.lidarr.folder_agrees(album_dir.name, album_dir.parent.name, la, mbid_agrees):
            logger.info(f"  Lidarr match rejected: tags of '{album_dir}' say '{la.title}' but the folder disagrees")
            return False

        canonical_path = self.lidarr.album_dir(la, allowed_roots=self._all_album_roots())
        if not canonical_path:
            return False
        canonical = Path(canonical_path)
        if album_dir.resolve() == canonical.resolve():
            return False
        logger.info(f"  Lidarr match: '{album_dir}' is '{la.artist_name} - {la.title}' → {canonical}")

        existing: Dict[str, Path] = {}
        if canonical.is_dir():
            for p in canonical.iterdir():
                if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                    existing[_track_key(_read_track_tags(str(p)), p)] = p

        changed = False
        for path, t in zip(files, tags):
            if _track_key(t, path) in existing:
                # A track Lidarr's folder already has: the song-dedup pass that
                # follows decides whether it is the same recording and removes it.
                continue
            # Lidarr's own track title and number (its selected release), as
            # Fix Metadata files a song; the file's tags only without one.
            lt = self.lidarr.find_track(la, t["title"] or path.stem)
            title, number, disc = ((lt["title"], lt["track"], lt["disc"]) if lt and lt["track"]
                                   else (t["title"] or path.stem, t["track"], t["disc"]))
            rel = self.lidarr.render_track_path(la, title, number, disc, path.suffix.lower())
            self._lidarr_move(path, canonical / os.path.basename(rel), la, report)
            changed = True

        if changed and not self.dry_run:
            self._lidarr_rescan_targets.add(la.artist_path)
            self._rmdir_if_empty(album_dir)
        return changed

    # ------------------------------------------------------------------
    # Song-level duplicates
    # ------------------------------------------------------------------

    def _plex_section(self):
        if not self.plex:
            return None
        try:
            return self.plex.library.section(self.plex_music_section)
        except Exception as exc:
            logger.warning(f"  Could not open Plex music section: {exc}")
            return None

    def _pass_song_dedup(self, report: MergeReport):
        """Remove duplicate copies of a song anywhere in the library (see song_dedup)."""
        import song_dedup
        logger.info("Pass 4: song-level duplicates")
        album_roots = self._all_album_roots()
        roots = album_roots
        lidarr_owned = set()
        if self.lidarr:
            try:
                self.lidarr.load()
                lidarr_owned = self.lidarr.track_file_paths()
            except Exception as exc:
                logger.warning(f"  Lidarr unavailable — its files cannot be protected, skipping: {exc}")
                return None
        result = song_dedup.dedup(
            roots, album_roots, lidarr_owned,
            plex_section=None if self.dry_run else self._plex_section(),
            dry_run=self.dry_run, artist_filter=self.artist_filter,
            quarantine_dir=self.quarantine_dir,
        )
        for removed, kept, where in result.removed:
            report.record(MergeAction(action="song_dedup", source=removed, target=where or kept,
                                      reason=f"Duplicate of {kept}"))
        for path, error in result.errors:
            report.record(MergeAction(action="song_dedup", source=path, target="", reason="",
                                      success=False, error=error))
        return result

    def dedup_songs(self) -> MergeReport:
        """Run only the song-level duplicate pass."""
        report = MergeReport()
        self._pass_song_dedup(report)
        report.finished_at = time.time()
        logger.info(report.summary())
        self._persist_report(report)
        return report

    def _lidarr_move(self, src: Path, dst: Path, la, report: MergeReport) -> None:
        action = MergeAction(action="lidarr_move", source=str(src), target=str(dst),
                             reason=f"Belongs to Lidarr album '{la.artist_name} - {la.title}'")
        if self.dry_run:
            logger.info(f"  [DRY RUN] would move {src} → {dst}")
            report.record(action)
            return
        try:
            if dst.exists():
                raise FileExistsError(f"{dst} already exists")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            library_state.moved(str(src), str(dst))
            _write_album_tags(str(dst), la)
            logger.info(f"    Moved {src} → {dst}")
        except Exception as exc:
            action.success, action.error = False, str(exc)
            logger.warning(f"    Could not move {src}: {exc}")
        report.record(action)

    # ------------------------------------------------------------------
    # Pass 1: normalise artist directory names
    # ------------------------------------------------------------------

    def _pass_normalise_artist_dirs(self, report: MergeReport) -> None:
        """Pass 1: one folder per artist ("Linkin Park" and "LINKIN PARK",
        "Two Steps from Hell" and Lidarr's "Two Steps From Hell", a mis-encoded
        "MontrÃ©al" and "Montréal", "Artist feat. X" and "Artist").

        Which folder stays: Lidarr's folder for that artist; otherwise the
        correctly encoded name holding the most songs (not ALL CAPS when there
        is a choice). Folders that are two different Lidarr artists stay apart.
        """
        logger.info("Pass 1: normalise artist directory names")
        for root in self._all_album_roots():
            if not os.path.isdir(root):
                continue
            self._flatten_nested_artist_dirs(root, report)
            self._normalise_artist_dirs_under(root, report)

    def _lidarr_ownership(self) -> Tuple[Dict[str, int], Set[str]]:
        """(Lidarr's artist folders → artist id, the files Lidarr owns); empty without Lidarr."""
        if not self.lidarr:
            return {}, set()
        self.lidarr.load()
        return self.lidarr.artist_folders(), self.lidarr.track_file_paths()

    def _normalise_artist_dirs_under(self, root: str, report: MergeReport) -> None:
        lidarr_folders, owned = self._lidarr_ownership()
        groups: Dict[str, List[Path]] = {}
        for entry in sorted(Path(root).iterdir()):
            if entry.is_dir() and not _is_junk_artist(entry.name):
                key = _artist_folder_key(entry.name)
                if key:
                    groups.setdefault(key, []).append(entry)
        lidarr_names: Dict[str, Set[str]] = {}
        for folder in lidarr_folders:
            if os.path.dirname(folder) == os.path.normpath(root):
                lidarr_names.setdefault(_artist_folder_key(os.path.basename(folder)), set()).add(os.path.basename(folder))

        strays: Set[str] = set()
        for key, dirs in groups.items():
            names = lidarr_names.get(key, set())
            if len(names) > 1:
                logger.info(f"  Artist folders {sorted(d.name for d in dirs)} are {len(names)} Lidarr artists: left apart")
                continue
            if names:
                canonical_name = next(iter(names))
            else:
                def rank(d: Path):
                    songs = sum(1 for p in d.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS)
                    return (_repair_mojibake(d.name) == d.name, songs, not d.name.isupper(), d.name)
                best = max(dirs, key=rank)
                canonical_name = _repair_mojibake(best.name)
            if all(d.name == canonical_name for d in dirs):
                continue
            canonical_path = Path(root) / canonical_name
            logger.info(f"  Artist: {[d.name for d in dirs]} → '{canonical_name}'")
            for src_dir in dirs:
                if src_dir.name == canonical_name:
                    continue
                strays |= self._merge_dir_into(src_dir, canonical_path, report, action_type="normalize_artist",
                                               reason=f"Artist name normalised to '{canonical_name}'", owned=owned)
        self._hand_strays_to_lidarr(strays)

    def _hand_strays_to_lidarr(self, strays: Set[str]) -> None:
        """Lidarr's files left behind by a merge: Lidarr moves them itself."""
        if not strays or not self.lidarr:
            return
        if self.dry_run:
            logger.info(f"  [DRY RUN] would ask Lidarr to move {len(strays)} of its file(s) into its folders")
            return
        for root in self._all_album_roots():
            library_owner.adopt(root)
        self.lidarr.move_files_home(strays)

    def _flatten_nested_artist_dirs(self, root: str, report: MergeReport) -> None:
        """Flatten artist dirs with exactly one nested artist-level child.

        Only collapses when:
        * The artist folder has **exactly one** subdirectory.
        * The artist folder itself contains **no** audio files.
        * The child subdirectory also has no audio files at its top level
          **but** does have further subdirectories (i.e. it is another artist
          level, not a regular album directory).
        """
        if not os.path.isdir(root):
            return
        for artist in list(os.listdir(root)):
            artist_dir = os.path.join(root, artist)
            if not os.path.isdir(artist_dir):
                continue
            subdirs = [
                d for d in os.listdir(artist_dir)
                if os.path.isdir(os.path.join(artist_dir, d))
            ]
            files = [
                f for f in os.listdir(artist_dir)
                if Path(os.path.join(artist_dir, f)).suffix.lower() in AUDIO_EXTENSIONS
            ]
            if len(subdirs) != 1 or files:
                continue
            child      = subdirs[0]
            child_path = os.path.join(artist_dir, child)
            child_files = [
                f for f in os.listdir(child_path)
                if Path(os.path.join(child_path, f)).suffix.lower() in AUDIO_EXTENSIONS
            ]
            grand_subs = [
                d for d in os.listdir(child_path)
                if os.path.isdir(os.path.join(child_path, d))
            ]
            if child_files or not grand_subs:
                # Child is a normal album dir (has audio or no grand-subdirs) — skip
                continue
            logger.info(f"  Flattening nested artist dir: {artist}/{child} → {artist}")
            if not self.dry_run:
                try:
                    for item in os.listdir(child_path):
                        shutil.move(os.path.join(child_path, item), artist_dir)
                    shutil.rmtree(child_path)
                    report.record(MergeAction(
                        action="normalize_artist",
                        source=child_path,
                        target=artist_dir,
                        reason=f"Flattened nested artist dir {artist}/{child}",
                    ))
                except Exception as exc:
                    logger.error(f"  Error flattening {artist}/{child}: {exc}")
                    report.record(MergeAction(
                        action="normalize_artist",
                        source=child_path,
                        target=artist_dir,
                        reason="Flatten failed",
                        success=False,
                        error=str(exc),
                    ))

    # ------------------------------------------------------------------
    # Pass 2: merge duplicate album directories under the same artist
    # ------------------------------------------------------------------

    def _pass_merge_duplicate_album_dirs(self, report: MergeReport) -> None:
        """Merge folders of one album under an artist: names equal once edition
        noise is dropped (see _album_folder_key). Folders Lidarr holds as albums
        of its own are left to Lidarr's reconcile.

        Example::

            /music/albums/Adele/21/
            /music/albums/Adele/21 (Deluxe Edition)/       ← merged into 21/
            /music/albums/JAY-Z/The Blueprint 3/            ← stays: another album
        """
        logger.info("Pass 2: merge duplicate album directories")
        all_roots = self._all_album_roots()

        for root in all_roots:
            if not os.path.isdir(root):
                continue
            for artist_dir in sorted(Path(root).iterdir()):
                if not artist_dir.is_dir():
                    continue
                # Never merge albums under catch-all artist dirs (e.g. "Various Artists")
                if _is_junk_artist(artist_dir.name):
                    logger.debug(f"  Pass 2: skipping junk artist dir '{artist_dir.name}'")
                    continue
                self._merge_artist_album_variants(artist_dir, report)

    def _merge_artist_album_variants(self, artist_dir: Path, report: MergeReport) -> None:
        """Find and merge album-title variants inside one artist directory."""
        album_dirs = [d for d in sorted(artist_dir.iterdir()) if d.is_dir()]
        if not album_dirs:
            return

        groups: Dict[str, List[Path]] = {}
        for album_dir in album_dirs:
            if self._is_empty_or_cover_only_album_dir(album_dir):
                self._remove_empty_album_dir(album_dir, report)
                continue
            if _is_junk_album(album_dir.name):
                logger.debug(
                    f"  Pass 2: skipping junk album dir "
                    f"'{artist_dir.name}/{album_dir.name}'"
                )
                continue
            key = _album_folder_key(album_dir.name)
            if not key:
                continue
            groups.setdefault(key, []).append(album_dir)
        # Exact keys only: a similarity threshold on album names joins
        # "The Blueprint" and "The Blueprint 3".
        merged_groups = list(groups.values())

        owned = self._lidarr_ownership()[1]
        for group in merged_groups:
            if len(group) < 2:
                continue
            held = [d for d in group if any(os.path.normpath(str(p)) in owned for p in d.rglob("*"))]
            if len(held) > 1:
                # Lidarr keeps these as albums of its own; its reconcile decides, not a name match.
                logger.info(f"  Album folders {[d.name for d in group]} under '{artist_dir.name}' "
                            f"are Lidarr's: left to Lidarr")
                continue
            canonical_dir = held[0] if held else self._pick_canonical_album_dir(group)
            others        = [d for d in group if d != canonical_dir]
            logger.info(
                f"  Album duplicate under '{artist_dir.name}': "
                f"{[d.name for d in group]} → '{canonical_dir.name}'"
            )
            for src_dir in others:
                self._merge_dir_into(src_dir, canonical_dir, report,
                                     action_type="merge_album_dir",
                                     reason=f"Album variant of '{canonical_dir.name}'", owned=owned)

    def _is_empty_or_cover_only_album_dir(self, album_dir: Path) -> bool:
        """Return True if the album directory has no audio and only cover/art files."""
        if not album_dir.is_dir():
            return False

        allowed_names = {
            'cover.jpg', 'cover.png', 'artist.jpg', 'artist.png',
            'folder.jpg', 'artwork.jpg',
        }

        for child in album_dir.iterdir():
            if child.is_dir():
                return False
            if child.is_file():
                if child.suffix.lower() in AUDIO_EXTENSIONS:
                    return False
                if child.name.lower() not in allowed_names:
                    return False
        return True

    def _remove_empty_album_dir(self, album_dir: Path, report: MergeReport) -> None:
        """Delete an album directory that contains no audio files and only cover art."""
        if self.dry_run:
            logger.info(f"  [DRY RUN] Would remove empty album directory: {album_dir}")
            report.record(MergeAction(
                action="remove_empty_album_dir",
                source=str(album_dir),
                target="",
                reason="empty or cover-only album dir",
                success=True,
            ))
            return

        try:
            shutil.rmtree(album_dir)
            logger.info(f"  ✓ Removed empty album directory: {album_dir}")
            report.record(MergeAction(
                action="remove_empty_album_dir",
                source=str(album_dir),
                target="",
                reason="empty or cover-only album dir",
                success=True,
            ))
        except Exception as exc:
            logger.warning(f"  ⚠ Failed to remove empty album directory {album_dir}: {exc}")
            report.record(MergeAction(
                action="remove_empty_album_dir",
                source=str(album_dir),
                target="",
                reason="failed to remove empty or cover-only album dir",
                success=False,
                error=str(exc),
            ))

    def _pick_canonical_album_dir(self, dirs: List[Path]) -> Path:
        """Canonical album = most audio files; tie-break: shortest normalised name.

        Uses _normalize_album_name for the tie-breaker so
        that 'Album - Original Motion Picture Score' normalises to the same
        length as 'Album' and the alphabetically-first simple name wins.
        """
        def _score(d: Path) -> Tuple[int, int]:
            count        = sum(1 for f in d.rglob("*") if f.suffix.lower() in AUDIO_EXTENSIONS)
            stripped_len = len(_normalize_album_name(d.name))
            return (-count, -stripped_len)

        return min(dirs, key=_score)

    # ------------------------------------------------------------------
    # ──────────────────────────────────────────────────────────────────────────
    # Pass 4: Plex-level split-album merge
    # ──────────────────────────────────────────────────────────────────────────

    def _pass_plex_merge_split_albums(self, report: MergeReport) -> None:
        """Merge Plex albums that represent the same release but are split.

        Plex sometimes creates multiple album objects for the same album (e.g.
        one per disc, one per file-tag variant).  This pass:
        1. Fetches all albums from the music section.
        2. Groups them by artist+album title similarity.
        3. For groups with >1 Plex album object, calls ``Album.mergeInto()``
           on all non-canonical members to fold them into the canonical album.
        4. Triggers a partial library refresh for the affected artist.

        Requires an active Plex connection.  Skips gracefully if Plex is None.
        """
        if not self.plex:
            logger.debug("Pass 4: Plex not connected — skipping Plex album merge")
            return

        logger.info("Pass 4: Plex split-album merge")

        try:
            section = self.plex.library.section(self.plex_music_section)
        except Exception as exc:
            logger.warning(f"  Could not open Plex music section: {exc}")
            return

        # Fetch all albums (may take a few seconds on large libraries)
        try:
            all_albums = section.albums()
            logger.info(f"  Fetched {len(all_albums)} Plex album objects")
        except Exception as exc:
            logger.warning(f"  Could not fetch Plex albums: {exc}")
            return

        # Group by the Lidarr album each item is a copy of; items Lidarr
        # doesn't know are grouped by normalised (artist, title).
        lidarr_ready = False
        if self.lidarr:
            try:
                self.lidarr.load()
                lidarr_ready = True
            except Exception as exc:
                logger.warning(f"  Lidarr unavailable for Plex grouping: {exc}")
        groups: Dict[tuple, list] = {}
        lidarr_album_for: Dict[int, object] = {}   # Plex ratingKey -> LidarrAlbum
        for album in all_albums:
            artist_name = getattr(album, "parentTitle", "") or ""
            album_title = getattr(album, "title", "") or ""
            # Never merge catch-all or placeholder albums/artists in Plex
            if _is_junk_artist(artist_name) or _is_junk_album(album_title):
                logger.debug(
                    f"  Pass 4: skipping junk Plex album "
                    f"'{artist_name} / {album_title}'"
                )
                continue
            if not self._artist_selected(artist_name):
                continue
            la = self.lidarr.match_album(artist_name, [album_title]) if lidarr_ready else None
            if la is not None and _album_folder_key(la.title) != _album_folder_key(album_title):
                la = None          # "Album (Classics Tokyo Sessions)" is not Lidarr's "Album"
            if la is not None:
                key = ("lidarr", la.id)
                lidarr_album_for[album.ratingKey] = la
            else:
                key = (
                    _normalise_for_compare(_canonical_artist_name(artist_name)),
                    _normalise_for_compare(_strip_noise(album_title)),
                )
            groups.setdefault(key, []).append(album)

        merged_plex_groups = self._fuzzy_merge_plex_groups(list(groups.values()))

        merge_count = 0
        for group in merged_plex_groups:
            # The same Plex album can land in a group twice; merging it into
            # itself is an empty merge Plex rejects (400).
            group = list({a.ratingKey: a for a in group}.values())
            if len(group) < 2:
                continue
            la = next((lidarr_album_for[a.ratingKey] for a in group if a.ratingKey in lidarr_album_for), None)
            canonical_album = self._pick_canonical_plex_album(group, la)
            others          = [a for a in group if a.ratingKey != canonical_album.ratingKey]
            logger.info(
                f"  Plex split-album: '{canonical_album.parentTitle} — "
                f"{canonical_album.title}' has {len(others)} extra object(s)"
                + (f" (Lidarr: '{la.title}')" if la else "")
            )
            for dup_album in others:
                action = MergeAction(
                    action  = "merge_plex_album",
                    source  = f"{dup_album.parentTitle} / {dup_album.title} (key={dup_album.ratingKey})",
                    target  = f"{canonical_album.parentTitle} / {canonical_album.title} (key={canonical_album.ratingKey})",
                    reason  = ("Same Lidarr album" if la else "Plex split-album detected by title/artist similarity"),
                )
                logger.info(
                    f"    Merging Plex album '{dup_album.title}' "
                    f"(key {dup_album.ratingKey}) into '{canonical_album.title}' "
                    f"(key {canonical_album.ratingKey})"
                )
                report.record(action)
            if self.dry_run:
                continue
            try:
                # merge() folds the given items INTO the item it's called on,
                # so the canonical album (and its title/metadata) survives.
                canonical_album.merge([str(a.ratingKey) for a in others])
                merge_count += len(others)
            except Exception as exc:
                logger.warning(f"    Plex merge failed: {exc}")
                for action in report.actions[-len(others):]:
                    action.success, action.error = False, str(exc)
                report.errors += len(others)
                report.plex_album_merges -= len(others)
                continue
            if la and canonical_album.title != la.title:
                try:
                    canonical_album.editTitle(la.title)
                    logger.info(f"    Renamed Plex album to Lidarr's title '{la.title}'")
                except Exception as exc:
                    logger.debug(f"    Could not rename Plex album: {exc}")

        logger.info(f"  Plex album merge: {merge_count} album(s) merged")

        # Trigger a partial scan so Plex picks up the changes
        if merge_count and not self.dry_run:
            try:
                section.update()
                logger.info("  Plex library scan triggered after album merges")
            except Exception as exc:
                logger.warning(f"  Could not trigger Plex scan: {exc}")

    def _fuzzy_merge_plex_groups(self, groups: List[list]) -> List[list]:
        """Merge Plex album groups whose (artist, title) keys are very similar."""
        # Each group has albums with the same normalised key already.
        # Here we look for groups whose keys are close but not identical.
        if not groups:
            return []

        # Represent each group by the canonical key of its first member
        def _key(g):
            a = getattr(g[0], "parentTitle", "") or ""
            t = getattr(g[0], "title", "") or ""
            return (
                _normalise_for_compare(_canonical_artist_name(a)),
                _normalise_for_compare(_strip_noise(t)),
            )

        used   = [False] * len(groups)
        result = []
        for i in range(len(groups)):
            if used[i]:
                continue
            cluster = list(groups[i])
            used[i] = True
            ka = _key(groups[i])
            for j in range(i + 1, len(groups)):
                if used[j]:
                    continue
                kb = _key(groups[j])
                # Same artist AND near-identical title. Plex merges are hard
                # to undo, so this is strict and never joins different
                # numbers ("Vol. 1" / "Vol. 2", "D12 World" / "D12 Forever").
                if _similarity(ka[0], kb[0]) >= ARTIST_SIMILARITY_THRESHOLD and \
                        _album_folder_key(getattr(groups[i][0], "title", "")) == \
                        _album_folder_key(getattr(groups[j][0], "title", "")):
                    cluster.extend(groups[j])
                    used[j] = True
            result.append(cluster)

        return result

    def _pick_canonical_plex_album(self, albums: list, lidarr_album=None):
        """Canonical Plex album: the one in Lidarr's folder, else most tracks, else earliest added."""
        lidarr_dir = ""
        if lidarr_album is not None and self.lidarr:
            try:
                lidarr_dir = os.path.normpath(
                    self.lidarr.album_dir(lidarr_album, allowed_roots=self._all_album_roots()) or "")
                lidarr_dir = "" if lidarr_dir == "." else lidarr_dir
            except Exception:
                lidarr_dir = ""

        def _in_lidarr_dir(a) -> bool:
            if not lidarr_dir:
                return False
            try:
                # Plex reports host paths (/srv/media/music/...) while we see
                # container paths (/music/...): compare by suffix.
                return any(os.path.normpath(os.path.dirname(part.file)).endswith(lidarr_dir)
                           for t in a.tracks() for m in t.media for part in m.parts)
            except Exception:
                return False

        def _score(a):
            track_count = getattr(a, "leafCount", 0) or 0
            added_at    = getattr(a, "addedAt", None)
            ts = added_at.timestamp() if hasattr(added_at, "timestamp") else float("inf")
            return (0 if _in_lidarr_dir(a) else 1, -track_count, ts)
        return min(albums, key=_score)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _all_album_roots(self) -> List[str]:
        """Return all album root directories to scan.

        Includes ``self.album_dir`` plus ``/music/users/*/albums`` siblings.
        """
        roots = []
        base  = Path(self.album_dir)

        # Primary album dir
        if base.is_dir():
            roots.append(str(base))

        # Multi-user layout: walk up to /music/users and enumerate per-user dirs
        try:
            parts = base.parts
            if "users" in parts:
                idx       = parts.index("users")
                users_dir = Path(*parts[: idx + 1])
                if users_dir.is_dir():
                    for entry in sorted(users_dir.iterdir()):
                        candidate = entry / "albums"
                        if candidate.is_dir():
                            s = str(candidate)
                            if s not in roots:
                                roots.append(s)
            else:
                # Guess siblings: /music/albums → /music/users/*/albums
                music_dir = base.parent
                users_dir = music_dir / "users"
                if users_dir.is_dir():
                    for entry in sorted(users_dir.iterdir()):
                        candidate = entry / "albums"
                        if candidate.is_dir():
                            s = str(candidate)
                            if s not in roots:
                                roots.append(s)
        except Exception as exc:
            logger.debug(f"_all_album_roots: could not enumerate user dirs: {exc}")

        return roots


    def _merge_dir_into(
        self,
        src_dir:     Path,
        dst_dir:     Path,
        report:      MergeReport,
        action_type: str,
        reason:      str,
        owned:       Optional[Set[str]] = None,
    ) -> Set[str]:
        """Move every song (and cover art) from src_dir into dst_dir, keeping
        the folder structure below it; returns the files Lidarr owns that were
        left for Lidarr to move (see _hand_strays_to_lidarr).

        * A song moves with its lyrics files and its probation/soon-to-go state.
        * A file Lidarr owns is never moved or removed by MusicAI.
        * A song of the same name already there is compared by sound: the same
          recording keeps the copy Lidarr owns, else the better one, and the
          other goes to the quarantine folder (never deleted outright); a
          different recording is left where it is.
        * Cover art: dst's copy stays.
        Empty folders left in src_dir are removed.
        """
        import song_dedup
        owned = owned if owned is not None else self._lidarr_ownership()[1]
        strays: Set[str] = set()
        if not src_dir.is_dir():
            return strays
        stamp = time.strftime("%Y%m%d-%H%M%S")

        for src_path in sorted(src_dir.rglob("*")):
            if not src_path.is_file() or src_path.is_symlink():
                continue
            rel      = src_path.relative_to(src_dir)
            dst_path = dst_dir / rel
            is_audio = src_path.suffix.lower() in AUDIO_EXTENSIONS
            is_cover = src_path.name.lower() in {"cover.jpg", "cover.png", "folder.jpg", "artwork.jpg"}
            if not is_audio and not is_cover:
                continue          # lyrics move with their song (library_state.moved)
            action = MergeAction(action=action_type, source=str(src_path), target=str(dst_path), reason=reason)

            if is_audio and os.path.normpath(str(src_path)) in owned:
                strays.add(str(src_path))
                continue
            try:
                if dst_path.exists():
                    if is_cover:
                        if not self.dry_run:
                            src_path.unlink()
                        continue
                    if not song_dedup.same_audio(str(src_path), str(dst_path)):
                        action.success, action.error = False, "a different song of that name is already there"
                        logger.info(f"  Not merged, a different song has that name: {src_path} vs {dst_path}")
                        report.record(action)
                        continue
                    dst_owned = os.path.normpath(str(dst_path)) in owned
                    keep_src = not dst_owned and _file_quality_rank(str(src_path)) < _file_quality_rank(str(dst_path))
                    worse = dst_path if keep_src else src_path
                    if self.dry_run:
                        logger.info(f"  [DRY RUN] would quarantine duplicate {worse} (same recording)")
                    else:
                        library_state.unmark(str(worse))
                        song_dedup.quarantine(str(worse), self.quarantine_dir, stamp)
                        if keep_src:
                            shutil.move(str(src_path), str(dst_path))
                            library_state.moved(str(src_path), str(dst_path))
                elif self.dry_run:
                    logger.info(f"  [DRY RUN] would merge {src_path} → {dst_path}")
                else:
                    dst_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src_path), str(dst_path))
                    if is_audio:
                        library_state.moved(str(src_path), str(dst_path))
                report.record(action)
            except Exception as exc:
                action.success = False
                action.error   = str(exc)
                report.record(action)
                logger.warning(f"    Could not process {src_path}: {exc}")

        if not self.dry_run:
            self._rmdir_if_empty(src_dir)
        return strays

    def _rmdir_if_empty(self, d: Path) -> None:
        """Recursively remove empty directories."""
        try:
            for child in sorted(d.iterdir(), reverse=True):
                if child.is_dir():
                    self._rmdir_if_empty(child)
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                logger.debug(f"    Removed empty directory: {d}")
        except Exception as exc:
            logger.debug(f"    Could not remove dir {d}: {exc}")

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _ensure_db(self) -> None:
        """Create the merges table if it doesn't exist."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS merges (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          REAL    NOT NULL,
                    action      TEXT    NOT NULL,
                    source      TEXT    NOT NULL,
                    target      TEXT    NOT NULL,
                    reason      TEXT,
                    success     INTEGER NOT NULL DEFAULT 1,
                    error       TEXT    DEFAULT ''
                )
            """)
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.debug(f"Could not ensure merges table: {exc}")

    def _persist_report(self, report: MergeReport) -> None:
        """Write all MergeAction entries from a report to the database."""
        if not report.actions:
            return
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            now    = time.time()
            cursor.executemany(
                """
                INSERT INTO merges (ts, action, source, target, reason, success, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (now, a.action, a.source, a.target, a.reason, int(a.success), a.error)
                    for a in report.actions
                ],
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.debug(f"Could not persist merge report: {exc}")


# ---------------------------------------------------------------------------
# Module-level helpers (used by app.py directly or by the class)
# ---------------------------------------------------------------------------





def build_merger_from_env(plex=None) -> "AlbumArtistMerger":
    """Convenience factory that reads config from environment variables.

    Reads the same env vars as app.py so no extra configuration is needed.

    >>> merger = build_merger_from_env(plex=my_plex_server)
    >>> report = merger.run_full_merge()
    """
    lidarr = None
    try:
        from lidarr_catalog import LidarrCatalog
        lidarr = LidarrCatalog.from_env()
    except Exception as exc:
        logger.warning(f"Lidarr catalog unavailable: {exc}")
    return AlbumArtistMerger(
        plex               = plex,
        plex_music_section = os.environ.get("PLEX_MUSIC_SECTION", "Music").split(":")[0].split(",")[0],
        album_dir          = os.environ.get("ALBUM_DIR", "/music/albums"),
        db_path            = os.environ.get("DB_PATH", "/app/data/music_ai.db"),
        dry_run            = os.environ.get("MERGER_DRY_RUN", "").lower() in ("1", "true", "yes"),
        lidarr             = lidarr,
        quarantine_dir     = os.environ.get("MERGER_QUARANTINE_DIR", "/app/data/merge_quarantine"),
    )


# ---------------------------------------------------------------------------
# CLI entry-point (python -m album_artist_merger)
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Merge duplicate artists and albums in a MusicAI library."
    )
    parser.add_argument("--album-dir",  default=os.environ.get("ALBUM_DIR", "/music/albums"))
    parser.add_argument("--db-path",    default=os.environ.get("DB_PATH",   "/app/data/music_ai.db"))
    parser.add_argument("--plex-url",   default=os.environ.get("PLEX_URL",  "http://localhost:32400"))
    parser.add_argument("--plex-token", default=os.environ.get("PLEX_TOKEN", ""))
    parser.add_argument("--section",    default=os.environ.get("PLEX_MUSIC_SECTION", "Music"))
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--artist", action="append", default=[],
                        help="Limit the lidarr/plex passes to this artist (repeatable)")
    parser.add_argument(
        "--pass", dest="pass_name",
        choices=["all", "artists", "albums", "lidarr", "songs", "plex"],
        default="all",
        help="Which merge pass to run (default: all)",
    )
    args = parser.parse_args()
    os.environ["ALBUM_DIR"] = args.album_dir
    os.environ["DB_PATH"] = args.db_path
    os.environ["PLEX_MUSIC_SECTION"] = args.section
    if args.dry_run:
        os.environ["MERGER_DRY_RUN"] = "1"

    plex_conn = None
    if args.plex_token:
        try:
            from plexapi.server import PlexServer
            plex_conn = PlexServer(args.plex_url, args.plex_token)
            logging.getLogger(__name__).info(f"Connected to Plex at {args.plex_url}")
        except Exception as e:
            logging.getLogger(__name__).warning(f"Could not connect to Plex: {e}")

    merger = build_merger_from_env(plex=plex_conn)
    if args.artist:
        from lidarr_catalog import compact
        merger.artist_filter = {compact(a) for a in args.artist}

    pass_map = {
        "all":     merger.run_full_merge,
        "artists": merger.normalise_artist_dirs,
        "albums":  merger.merge_duplicate_album_dirs,
        "lidarr":  merger.lidarr_reconcile,
        "songs":   merger.dedup_songs,
        "plex":    merger.plex_merge_split_albums,
    }
    report = pass_map[args.pass_name]()
    print(report.summary())
