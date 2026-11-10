"""Lidarr's view of the library, used as the authority for album identity.

When the same album exists more than once (different folders, spellings or
editions), Lidarr's setup decides the winner: its album title, its folder and
track naming format, the release it has selected, and its quality profile.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests

logger = logging.getLogger(__name__)

# Lidarr's quality groups, lowest to highest — the order its profiles use.
LIDARR_QUALITY_ORDER = [
    "Unknown",
    "Trash Quality Lossy",
    "Poor Quality Lossy",
    "Low Quality Lossy",
    "Mid Quality Lossy",
    "High Quality Lossy",
    "Lossless",
]
_LOSSLESS_EXTS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv", ".alac"}

ARTIST_MATCH_THRESHOLD = 0.92
ALBUM_MATCH_THRESHOLD = 0.92
# When the files carry the album's MusicBrainz ID, edition noise in the title
# ("(Bonus Track Version)") is tolerated a little more.
MBID_ALBUM_MATCH_THRESHOLD = 0.80
# Lidarr's existing folder is only trusted if its name resembles the album.
EXISTING_FOLDER_SIMILARITY = 0.6
# Two candidate albums scoring within this of each other is ambiguous.
AMBIGUITY_MARGIN = 0.02

_BRACKETED_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_APOSTROPHES_RE = re.compile(r"[’'`´‘]")


def _ascii_fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def normalize_words(s: str) -> str:
    """Lowercased words with brackets, apostrophes and punctuation removed.

    "Devil’s Night (2001)" -> "devils night", "D-12 World" -> "d 12 world".
    Letters of every script are kept ("Детство" -> "детство"): reducing a
    name to ASCII would make every Cyrillic, Arabic or Japanese name the same
    empty string.
    """
    s = _ascii_fold(s).lower().replace("&", " and ")
    s = _BRACKETED_RE.sub(" ", s)
    s = _APOSTROPHES_RE.sub("", s)
    s = re.sub(r"[\W_]+", " ", s).strip()
    s = re.sub(r"^the\s+", "", s)
    return s


def _title_as_written(s: str) -> str:
    """Words of a title including bracketed ones: "Red" ≠ "Red (Taylor's
    Version)", which normalize_words makes equal."""
    s = _APOSTROPHES_RE.sub("", _ascii_fold(s).lower().replace("&", " and "))
    return re.sub(r"[\W_]+", " ", s).strip()


def compact(s: str) -> str:
    """normalize_words without spaces, so "D-12" and "D12" compare equal."""
    return normalize_words(s).replace(" ", "")


def _numbers(s: str) -> List[str]:
    return sorted(str(int(n)) for n in re.findall(r"\d+", normalize_words(s)))


def name_similarity(a: str, b: str) -> float:
    ca, cb = compact(a), compact(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    return SequenceMatcher(None, ca, cb).ratio()


def titles_match(a: str, b: str, threshold: float = ALBUM_MATCH_THRESHOLD) -> Tuple[bool, float]:
    """Fuzzy title match that refuses to join different numbers ("Vol. 1"/"Vol. 2")."""
    if _numbers(a) != _numbers(b):
        return False, 0.0
    score = name_similarity(a, b)
    return score >= threshold, score


def local_quality(path: str) -> str:
    """Map a local file onto the Lidarr quality group it would import as."""
    ext = Path(path).suffix.lower()
    try:
        from mutagen import File as MuFile  # type: ignore
        mf = MuFile(path)
        info = getattr(mf, "info", None)
    except Exception:
        info = None
    codec = str(getattr(info, "codec", "") or "").lower()
    if ext in _LOSSLESS_EXTS or codec == "alac":
        return "Lossless"
    kbps = (getattr(info, "bitrate", 0) or 0) / 1000
    if kbps >= 300:
        return "High Quality Lossy"
    if kbps >= 192:
        return "Mid Quality Lossy"
    if kbps >= 128:
        return "Low Quality Lossy"
    if kbps > 0:
        return "Poor Quality Lossy"
    return "Unknown"


def lidarr_instances() -> List[Tuple[str, str]]:
    """The configured Lidarr instances, [(url, api_key)], in user order.

    LIDARR_URLS and LIDARR_API_KEYS are parallel lists (comma- or
    semicolon-separated); see lidarr_for_user for which one a user follows
    their top artists in. LIDARR_URL / LIDARR_API_KEY are accepted for a
    single instance. The first entry is the library's: it owns /music/albums
    and is the one the reconcile, Fix Metadata and the library review consult."""
    def split(*names):
        raw = next((os.environ.get(n, "") for n in names if os.environ.get(n, "").strip()), "")
        return [v.strip() for v in raw.replace(";", ",").split(",") if v.strip()]
    urls, keys = split("LIDARR_URLS", "LIDARR_URL"), split("LIDARR_API_KEYS", "LIDARR_API_KEY")
    return [(u.rstrip("/"), k) for u, k in zip(urls, keys)]


def lidarr_for_user(user_index: int, instances: Optional[List[Tuple[str, str]]] = None) -> Optional[Tuple[str, str]]:
    """The Lidarr instance (url, api_key) user N follows their top artists
    in: entry N, or the first instance for a user without an entry of their
    own — so one Lidarr follows every user's top artists. None without Lidarr."""
    instances = lidarr_instances() if instances is None else instances
    if not instances:
        return None
    return instances[user_index] if 0 <= user_index < len(instances) else instances[0]


@dataclass
class LidarrAlbum:
    id: int
    artist_id: int
    artist_name: str
    artist_path: str            # local path
    title: str
    year: Optional[int]
    release_group_id: str
    release_ids: Set[str] = field(default_factory=set)
    selected_release_id: str = ""
    track_file_dir: Optional[str] = None   # local folder holding Lidarr's files
    monitored: bool = False                # album and artist both monitored


class LidarrCatalog:
    """Read-mostly snapshot of a Lidarr instance's artists, albums and settings."""

    def __init__(self, base_url: str, api_key: str, path_map: Optional[Tuple[str, str]] = None,
                 timeout: int = 30):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.path_map = path_map
        self.timeout = timeout
        self.loaded = False
        self.albums: List[LidarrAlbum] = []
        self._by_mbid: Dict[str, LidarrAlbum] = {}
        self._by_artist: Dict[str, List[LidarrAlbum]] = {}   # compact artist name -> albums
        self._fuzzy_artist_cache: Dict[str, List[LidarrAlbum]] = {}
        self._artists_with_files: List[int] = []
        self._track_file_paths: Optional[Set[str]] = None
        self._track_file_ids: Dict[str, Tuple[int, int]] = {}   # local path -> (artist id, file id)
        self._artists: Dict[int, Tuple[str, str]] = {}   # id -> (name, local path)
        self._monitored_artists: Set[int] = set()
        self._kept_paths: Optional[Set[str]] = None
        self._tracks: Dict[int, List[dict]] = {}          # album id -> Lidarr's tracks
        self.track_format = "{Album Title} ({Release Year})/{Artist Name} - {Album Title} - {track:00} - {Track Title}"
        self.artist_folder_format = "{Artist Name}"
        self.colon_replacement = " -"
        self.root_folders: List[str] = []   # local paths

    # -- plumbing ---------------------------------------------------------

    @classmethod
    def from_env(cls) -> Optional["LidarrCatalog"]:
        instances = lidarr_instances()
        if not instances:
            return None
        url, key = instances[0]
        path_map = None
        raw_map = os.environ.get("LIDARR_PATH_MAP", "").strip()   # "lidarr_prefix=local_prefix"
        if "=" in raw_map:
            lidarr_prefix, local_prefix = raw_map.split("=", 1)
            path_map = (lidarr_prefix.rstrip("/"), local_prefix.rstrip("/"))
        return cls(url, key, path_map=path_map)

    def _get(self, path: str, **params):
        resp = requests.get(f"{self.base_url}/api/v1/{path}", headers={"X-Api-Key": self.api_key},
                            params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def to_local(self, lidarr_path: str) -> str:
        if self.path_map and lidarr_path.startswith(self.path_map[0]):
            return self.path_map[1] + lidarr_path[len(self.path_map[0]):]
        return lidarr_path

    def to_lidarr(self, local_path: str) -> str:
        if self.path_map and local_path.startswith(self.path_map[1]):
            return self.path_map[0] + local_path[len(self.path_map[1]):]
        return local_path

    # -- loading ----------------------------------------------------------

    def load(self) -> "LidarrCatalog":
        if self.loaded:
            return self
        naming = self._get("config/naming")
        self.track_format = naming.get("multiDiscTrackFormat") or naming.get("standardTrackFormat") or self.track_format
        self.artist_folder_format = naming.get("artistFolderFormat") or self.artist_folder_format
        # Lidarr's colonReplacementFormat: 0 delete, 1 dash, 2 space-dash, 3 space-dash-space, 4 smart
        self.colon_replacement = {0: "", 1: "-", 2: " -", 3: " - "}.get(naming.get("colonReplacementFormat"), " -")
        self.root_folders = [self.to_local(r["path"]).rstrip("/") for r in self._get("rootfolder") if r.get("path")]

        for a in self._get("artist"):
            self._artists[a["id"]] = (a.get("artistName", ""), self.to_local(a.get("path", "")))
            if a.get("monitored"):
                self._monitored_artists.add(a["id"])
            if (a.get("statistics") or {}).get("trackFileCount"):
                self._artists_with_files.append(a["id"])

        for al in self._get("album"):
            name, path = self._artists.get(al.get("artistId"), ("", ""))
            releases = al.get("releases") or []
            selected = next((r for r in releases if r.get("monitored")), None)
            date = (al.get("releaseDate") or "")[:4]
            album = LidarrAlbum(
                id=al["id"],
                artist_id=al.get("artistId", 0),
                artist_name=name,
                artist_path=path,
                title=al.get("title", ""),
                year=int(date) if date.isdigit() else None,
                release_group_id=al.get("foreignAlbumId", ""),
                release_ids={r.get("foreignReleaseId") for r in releases if r.get("foreignReleaseId")},
                selected_release_id=(selected or {}).get("foreignReleaseId", ""),
                monitored=bool(al.get("monitored")) and al.get("artistId") in self._monitored_artists,
            )
            self.albums.append(album)
            self._by_artist.setdefault(compact(name), []).append(album)
            for mbid in {album.release_group_id, *album.release_ids}:
                if mbid:
                    self._by_mbid[mbid] = album

        self.loaded = True
        logger.info(f"Lidarr catalog: {len(self._artists)} artists, {len(self.albums)} albums")
        return self

    def track_file_paths(self) -> Set[str]:
        """Local paths of every file Lidarr manages (normalised)."""
        if self._track_file_paths is None:
            paths: Set[str] = set()
            for artist_id in self._artists_with_files:
                try:
                    for f in self._get("trackfile", artistId=artist_id):
                        if f.get("path"):
                            path = os.path.normpath(self.to_local(f["path"]))
                            paths.add(path)
                            self._track_file_ids[path] = (artist_id, f["id"])
                except Exception as exc:
                    logger.debug(f"Lidarr trackfile listing failed for artist {artist_id}: {exc}")
            self._track_file_paths = paths
            logger.info(f"Lidarr manages {len(paths)} track file(s)")
        return self._track_file_paths

    def artist_folders(self) -> Dict[str, int]:
        """Each Lidarr artist's local folder → its artist id."""
        return {os.path.normpath(path): artist_id for artist_id, (_name, path) in self._artists.items() if path}

    def move_files_home(self, local_paths: Iterable[str]) -> int:
        """Ask Lidarr to move files it owns into their artist's folder, named
        its way (Lidarr's RenameFiles); MusicAI never moves Lidarr's files
        itself. Returns how many files were handed over."""
        self.track_file_paths()
        by_artist: Dict[int, List[int]] = {}
        for path in {os.path.normpath(p) for p in local_paths}:
            if path in self._track_file_ids:
                artist_id, file_id = self._track_file_ids[path]
                by_artist.setdefault(artist_id, []).append(file_id)
        for artist_id, file_ids in by_artist.items():
            try:
                requests.post(f"{self.base_url}/api/v1/command", headers={"X-Api-Key": self.api_key},
                              json={"name": "RenameFiles", "artistId": artist_id, "files": sorted(file_ids)},
                              timeout=self.timeout)
            except Exception as exc:
                logger.warning(f"Could not ask Lidarr to move artist {artist_id}'s files: {exc}")
        moved = sum(len(v) for v in by_artist.values())
        if moved:
            logger.info(f"Lidarr asked to move {moved} of its file(s) into their artist folders")
        return moved

    def kept_track_file_paths(self) -> Set[str]:
        """Local paths of Lidarr's files in albums it monitors: deleted, Lidarr
        would download them again."""
        if self._kept_paths is None:
            monitored = {a.id for a in self.albums if a.monitored}
            paths: Set[str] = set()
            for artist_id in self._artists_with_files:
                if artist_id not in self._monitored_artists:
                    continue
                try:
                    for f in self._get("trackfile", artistId=artist_id):
                        if f.get("path") and f.get("albumId") in monitored:
                            paths.add(os.path.normpath(self.to_local(f["path"])))
                except Exception as exc:
                    logger.debug(f"Lidarr trackfile listing failed for artist {artist_id}: {exc}")
            self._kept_paths = paths
        return self._kept_paths

    def track_file_dir(self, album: LidarrAlbum) -> Optional[str]:
        """Folder where Lidarr keeps this album's files (fetched lazily)."""
        if album.track_file_dir is None:
            try:
                files = self._get("trackfile", albumId=album.id)
            except Exception as exc:
                logger.debug(f"Lidarr trackfile lookup failed for album {album.id}: {exc}")
                files = []
            dirs = sorted({os.path.dirname(self.to_local(f["path"])) for f in files if f.get("path")})
            album.track_file_dir = dirs[0] if dirs else ""
        return album.track_file_dir or None

    def find_track(self, album: LidarrAlbum, title: str) -> Optional[dict]:
        """Lidarr's track of this title on the album (its selected release):
        {'title', 'track', 'disc', 'has_file'}, or None when the album has no
        such track."""
        if album.id not in self._tracks:
            try:
                self._tracks[album.id] = self._get("track", albumId=album.id)
            except Exception as exc:
                logger.debug(f"Lidarr track lookup failed for album {album.id}: {exc}")
                self._tracks[album.id] = []
        # Brackets are ignored by titles_match, so a version must agree on
        # its own ("Everything Has Changed" is not track 27, "… (commentary)")
        # and the title as written wins a tie.
        from artwork import SPECIAL_RELEASE
        from song_dedup import variant_labels
        scored = []
        for t in self._tracks[album.id]:
            other = t.get("title", "")
            ok, score = titles_match(title, other, ALBUM_MATCH_THRESHOLD)
            if not ok or variant_labels(other) != variant_labels(title) \
                    or bool(SPECIAL_RELEASE.search(other)) != bool(SPECIAL_RELEASE.search(title)):
                continue
            scored.append((_title_as_written(other) == _title_as_written(title), score, t))
        if not scored:
            return None
        t = max(scored, key=lambda s: (s[0], s[1]))[2]
        return {"title": t.get("title", ""), "track": t.get("absoluteTrackNumber") or None,
                "disc": t.get("mediumNumber") or None, "has_file": bool(t.get("hasFile"))}

    # -- matching ---------------------------------------------------------

    def match_album(self, artist: str, album_titles: Iterable[str], mbids: Iterable[str] = (),
                    year: Optional[int] = None) -> Optional[LidarrAlbum]:
        """Find the Lidarr album a local album is a copy of.

        The artist must match and the best title must clear the threshold
        without a close runner-up. A MusicBrainz ID shared with the album
        lowers the title threshold but never replaces these checks: tags
        written by metadata lookups are wrong often enough (a George Benson
        folder tagged with "Let It Be") that an ID alone isn't trusted.
        """
        mbid_hits = {self._by_mbid[m].id for m in mbids if m and m in self._by_mbid}

        titles = [t for t in album_titles if t]
        if not artist or not titles:
            return None
        key = compact(artist)
        candidates = self._by_artist.get(key)
        if candidates is None:
            if key not in self._fuzzy_artist_cache:
                self._fuzzy_artist_cache[key] = [
                    a for name, albums in self._by_artist.items()
                    if name and SequenceMatcher(None, key, name).ratio() >= ARTIST_MATCH_THRESHOLD
                    for a in albums
                ]
            candidates = self._fuzzy_artist_cache[key]
        scored: List[Tuple[float, LidarrAlbum]] = []
        for cand in candidates:
            threshold = MBID_ALBUM_MATCH_THRESHOLD if cand.id in mbid_hits else ALBUM_MATCH_THRESHOLD
            best = 0.0
            for t in titles:
                ok, score = titles_match(t, cand.title, threshold)
                if ok:
                    best = max(best, score + (0.05 if cand.id in mbid_hits else 0.0))
            if best:
                if year and cand.year and abs(year - cand.year) <= 1:
                    best += 0.01
                scored.append((best, cand))
        if not scored:
            return None
        scored.sort(key=lambda s: -s[0])
        # A title that is the album's exactly beats one that only matches with
        # its brackets dropped: "Red" is Lidarr's "Red", not "Red (Taylor's
        # Version)" (both score 1.0).
        exact = {c.id: c for score, c in scored if score >= scored[0][0] - AMBIGUITY_MARGIN
                 and any(_title_as_written(t) == _title_as_written(c.title) for t in titles)}
        if len(exact) == 1:
            return next(iter(exact.values()))
        if len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUITY_MARGIN and scored[0][1].id != scored[1][1].id:
            logger.info(f"Lidarr match ambiguous for '{artist} - {titles[0]}': "
                        f"'{scored[0][1].title}' vs '{scored[1][1].title}' — skipping")
            return None
        return scored[0][1]

    def folder_agrees(self, folder_name: str, artist: str, album: LidarrAlbum, has_mbid: bool) -> bool:
        """Does a local folder's name also say it is this album?

        Tags written by metadata lookups can name the wrong album ("Walkerworld
        2.0/" tagged "Walkerworld"); the folder name is independent evidence.
        "Bon Jovi 2020" is accepted for "2020" by dropping the artist prefix.
        """
        threshold = MBID_ALBUM_MATCH_THRESHOLD if has_mbid else ALBUM_MATCH_THRESHOLD
        candidates = [folder_name]
        for name in (artist, album.artist_name):
            words = normalize_words(name)
            folder_words = normalize_words(folder_name)
            if words and folder_words.startswith(words + " "):
                candidates.append(folder_words[len(words) + 1:])
        return any(titles_match(c, album.title, threshold)[0] for c in candidates)

    # -- Lidarr's preferences ---------------------------------------------

    def _clean_component(self, s: str) -> str:
        s = s.replace(":", self.colon_replacement)
        s = re.sub(r'[\\/*?"<>|]', "", s)
        return re.sub(r"\s+", " ", s).strip().rstrip(".")

    @staticmethod
    def _token_value(s: str) -> str:
        # A "/" inside a name must not become a folder; Lidarr writes AC/DC as AC+DC.
        return (s or "").replace("/", "+")

    def render_track_path(self, album: LidarrAlbum, track_title: str, track_no: Optional[int],
                          disc_no: Optional[int], ext: str) -> str:
        """Relative path (under the artist folder) Lidarr's naming format gives this track."""
        values = {
            "Artist Name": self._token_value(album.artist_name),
            "Album Title": self._token_value(album.title),
            "Release Year": str(album.year or ""),
            "Track Title": self._token_value(track_title),
        }

        def _sub(m: re.Match) -> str:
            token, _, fmt = m.group(1).partition(":")
            if token in ("track", "medium"):
                n = track_no if token == "track" else disc_no
                if n is None:
                    return ""
                return str(n).zfill(len(fmt)) if fmt else str(n)
            return values.get(token, "")

        rendered = re.sub(r"\{([^{}]+)\}", _sub, self.track_format)
        parts = [self._clean_component(p) for p in rendered.split("/")]
        parts = [re.sub(r"\(\s*\)", "", p).strip() for p in parts]
        parts[-1] = re.sub(r"\s*-\s*-\s*", " - ", parts[-1]).strip(" -")
        return "/".join(p for p in parts if p) + ext

    def _under(self, path: str, roots: Iterable[str]) -> bool:
        path = os.path.normpath(path)
        return any(path == os.path.normpath(r) or path.startswith(os.path.normpath(r) + os.sep) for r in roots)

    def album_dir(self, album: LidarrAlbum, allowed_roots: Optional[Iterable[str]] = None) -> Optional[str]:
        """Where Lidarr's setup puts this album.

        Lidarr's own folder for the album when it has one inside an allowed
        root; otherwise its root folder + artist folder format + album folder
        format. Paths outside the allowed roots are never used (an artist
        imported from some other folder keeps pointing there). Returns None
        if no allowed location exists.
        """
        roots = [r.rstrip("/") for r in (allowed_roots if allowed_roots is not None else self.root_folders or ["/"])]
        existing = self.track_file_dir(album)
        # Lidarr sometimes files an album's tracks under another album's folder
        # (its "Self Portrait" lives in "Another Self Portrait … Vol. 10"); such
        # a folder is a Lidarr mistake, not a preference.
        if existing and self._under(existing, roots) and \
                name_similarity(os.path.basename(existing), album.title) >= EXISTING_FOLDER_SIMILARITY:
            return existing

        folder = os.path.dirname(self.render_track_path(album, "x", 1, 1, ""))
        if album.artist_path and self._under(album.artist_path, roots):
            artist_dir = album.artist_path
        else:
            lidarr_roots = [r for r in self.root_folders if self._under(r, roots)] or \
                           [r for r in roots if r != "/"]
            if not lidarr_roots:
                return None
            artist_folder = re.sub(r"\{Artist Name\}", self._token_value(album.artist_name), self.artist_folder_format)
            artist_dir = os.path.join(lidarr_roots[0], self._clean_component(artist_folder))
        return os.path.join(artist_dir, folder) if folder else artist_dir

    def rescan(self, local_folders: Iterable[str]) -> None:
        folders = sorted({self.to_lidarr(f) for f in local_folders if f})
        if not folders:
            return
        try:
            requests.post(f"{self.base_url}/api/v1/command", headers={"X-Api-Key": self.api_key},
                          json={"name": "RescanFolders", "folders": folders}, timeout=self.timeout)
            logger.info(f"Lidarr rescan requested for {len(folders)} folder(s)")
        except Exception as exc:
            logger.warning(f"Could not request Lidarr rescan: {exc}")
