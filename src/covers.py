"""Covers: telling that a requested video is one, and finding the official song.

A video is a cover when its title or channel says so ("Epic Orchestral Cover",
a channel named "… Covers"), or when it names a work ("(Example OST)", 'From
"Example"') on whose soundtrack another artist has the same song, and its
performer has released a recording of their own. A fan's upload of the official
recording is not a cover: the uploader has no release of it. When the work's
soundtrack has the song at the video's length, the upload is that recording
("Example OST 3 - Track 18 - Nocturne" by a fan's channel): it is filed as the
soundtrack's, without a question.

The official song is looked for on Deezer: a track of the same title by the
artist the video credits ("(Coldplay cover)", "Composer: …"), or on the named
work's soundtrack. Without either, the most popular track of that title is only
a likely original. Remixes, karaoke and tracks that call themselves covers are
never the official song.
"""
import logging
import re
from typing import Callable, Dict, List, Optional, Tuple

from album_match import _get, _norm
from song_dedup import variant_labels

logger = logging.getLogger("musicai.covers")

# The context fields a request carries its video's own words in, for this module only.
TITLE_FIELD, UPLOADER_FIELD, NOTES_FIELD = "cover_title", "cover_uploader", "cover_notes"
OFFICIALS = 2               # official songs offered, at most
COVERS = 4                  # covers offered below them, at most
SAME_LENGTH = 3             # seconds: a release this close to the linked video is the same recording
LIKELY = 3                  # a likely original is this many times as popular as any other artist's

_BRACKETS = "()[]「」『』【】〈〉＜＞<>"
_BRACKETED = re.compile(r"[(\[「『【〈＜<]([^()\[\]「」『』【】〈〉＜＞<>]*)[)\]」』】〉＞>]")
_SAYS_COVER = re.compile(r"\bcover(?:s|ed)?\b(?!\s+(?:art|image|reveal|version\s+art))", re.I)
_NOT_THE_SONG = re.compile(r"\b(?:album|front|book|magazine)\s+cover\b", re.I)
# What a cover calls itself after the song's name.
_COVER_TAIL = re.compile(
    r"[\s\-–—|:]*\b(?:(?:epic|orchestral|symphonic|rock|metal|punk|jazz|lofi|piano|guitar|violin|cello|drum|bass|flute|"
    r"acoustic|vocal|female|male|fingerstyle|full[ -]band|cinematic|english|an|a|the|official)\s+)*covers?\b.*$", re.I)
_STYLE = re.compile(r"\b(?:epic|orchestral|symphonic|rock|metal|punk|jazz|lo-?fi|piano|guitar|violin|cello|drums?|bass|"
                    r"flute|acoustic|vocal|female|male|fingerstyle|full[ -]band|cinematic|english|live|studio|official|"
                    r"my|a|the|an|best|new|\d+)\b", re.I)
_VERSION = re.compile(r"\b(?:version|remaster(?:ed)?|remake|rework|ver\.?|arrangement|edit|extended|theme|hd|hq|4k)\b", re.I)
_SOUNDTRACK = re.compile(r"\b(?:ost|o\.s\.t\.?|soundtrack|score)\b", re.I)
_FROM_WORK = re.compile(r"^\s*from\s+(?:the\s+(?:movie|film|anime|game|series)\s+)?[\"'“”‘’]*(.+?)[\"'“”‘’]*\s*$", re.I)
_GENERIC = {"the", "of", "a", "an", "and", "from", "tv", "animation", "anime", "movie", "film", "game", "series",
            "original", "motion", "picture", "soundtrack", "soundtracks", "ost", "score", "music", "theme", "themes",
            "episode", "season", "cour", "part", "vol", "volume", "collection", "best"}
# A result that names itself a cover, or an arrangement made in someone else's name.
_COVER_RESULT = re.compile(r"\bcovers?\b|\btributes?\b|\bkaraoke\b|\bmade famous\b|\bin the style of\b|\boriginally performed\b|"
                           r"\bpiano (?:version|collection|renditions?|dreams?)\b|\blullab(?:y|ies)\b|\b8.?bit\b", re.I)
_CREDITS = (
    re.compile(r"\b(?:originally|original(?:\s+song|\s+artist)?)\s*(?:(?:performed|written|composed|sung)\s+)?(?:by|:)\s*([^\n/|•►(]{2,60})", re.I),
    re.compile(r"\bcover\s+of\s+.{1,60}?\s+by\s+([^\n/|•►(]{2,60})", re.I),
    re.compile(r"\bcomposers?\s*:\s*([^\n/|•►(]{2,60})", re.I),
)


def _core(title: str) -> str:
    """A title without brackets, credits, case and punctuation."""
    text = re.sub(r"\b(?:feat\.?|ft\.?|featuring)\s.*$", "", _BRACKETED.sub(" ", title or ""), flags=re.I)
    return _norm(text).replace(" ", "")


def _words(text: str) -> set:
    return {w for w in _norm(text).split() if w not in _GENERIC and not w.isdigit()}


def says_cover(text: str) -> bool:
    return bool(_SAYS_COVER.search(_NOT_THE_SONG.sub("", text or "")))


def _same_artist(left: str, right: str) -> bool:
    a, b = _norm(left).split(), _norm(right).split()
    return bool(a and b) and (sorted(a) == sorted(b) or "".join(a) == "".join(b))


_DASH = re.compile(r"\s*[-–—]\s+|\s+[-–—]\s*")
_NUMBERING = re.compile(r"^\s*(?:(?:track|tr\.?|no\.?|#)\s*)?\d+\s*$", re.I)


def _before_soundtrack(text: str) -> str:
    """"Example Show Episode 4" of "Example Show Episode 4 OST": the work."""
    work = _SOUNDTRACK.split(text)[0]
    return re.sub(r"\b(?:original|episode\s*\d+|ep\.?\s*\d+|season\s*\d+|cour\s*\d+)\b", " ", work, flags=re.I).strip(" -–—:")


def _work(title: str) -> str:
    """The film, game or series a title says its song comes from."""
    for inside in _BRACKETED.findall(title or ""):
        named = _FROM_WORK.match(inside)
        if named:
            return named.group(1).strip(" \"'“”‘’")
        if _SOUNDTRACK.search(inside):
            return _before_soundtrack(inside)
    for inside in re.findall(r"[「『【]([^」』】]+)[」』】]", title or ""):
        if not says_cover(inside):
            return inside.strip()
    # "Example Show OST 3 - Track 18 - Nocturne"
    for part in _DASH.split(_BRACKETED.sub(" ", title or "")):
        if _SOUNDTRACK.search(part) and not says_cover(part) and _words(_before_soundtrack(part)):
            return _before_soundtrack(part)
    return ""


def _halves(video_title: str) -> List[str]:
    """The parts of an "artist - song" (or "song - artist") video title."""
    plain = re.split(r"\||\bon (?:spotify|apple|itunes|youtube)\b", _BRACKETED.sub(" ", video_title or ""), flags=re.I)[0]
    return [p.strip(" -–—:") for p in re.split(r"\s+[-–—]\s+", _COVER_TAIL.sub("", plain)) if p.strip(" -–—:")][:2]


def song_names(title: str, video_title: str = "") -> List[str]:
    """What the covered song may be called: the request's title without what
    makes it a cover, then a name in the video title's brackets ("＜Nocturne Remaster＞"),
    then the parts of a dashed title that aren't a number or a soundtrack
    ("Example OST 3 - Track 18 - Nocturne")."""
    names = [_COVER_TAIL.sub("", _BRACKETED.sub(" ", title or ""))]
    parts = list(reversed(_halves(video_title)))
    for inside in re.findall(r"[＜〈<]([^＞〉>]+)[＞〉>]", f"{title} {video_title}"):
        parts.append(_STYLE.sub(" ", _VERSION.sub(" ", inside)))
    for text in (title, video_title):
        dashed = _DASH.split(_COVER_TAIL.sub("", _BRACKETED.sub(" ", text or "")))
        parts += dashed if len(dashed) > 1 else []
    names += [p for p in parts if not _SOUNDTRACK.search(p)]
    found = []
    for name in (re.sub(r"\s+", " ", n).strip(" -–—:") for n in names):
        name = re.sub(r"^(?:track|tr\.?|no\.?|#)\s*\d+\s*[-–—.:]\s*", "", name, flags=re.I)
        if _NUMBERING.match(name):
            continue
        if len(_core(name)) > 2 and _core(name) not in [_core(f) for f in found]:
            found.append(name)
    return found[:4]


def credited(title: str, description: str = "") -> List[str]:
    """The artists a video names as the song's own."""
    found = []
    for inside in _BRACKETED.findall(title or ""):
        named = re.match(r"^(.{2,50}?)\s+cover$", inside.strip(), re.I)
        if named and _STYLE.sub("", named.group(1)).strip():
            name = named.group(1).strip()
            while _STYLE.fullmatch(name.split()[-1]) and len(name.split()) > 1:
                name = name.rsplit(None, 1)[0]
            found.append(name)
    for pattern in _CREDITS:
        for text in (title, description):
            found += [name.strip(" .,-–—\"'") for name in pattern.findall(text or "")]
    if says_cover(title):
        found += _halves(title) if len(_halves(title)) == 2 else []
    kept = []
    for name in found:
        if name and not any(_same_artist(name, k) for k in kept):
            kept.append(name)
    return kept[:4]


def clues(video_title: str, uploader: str = "", description: str = "") -> Dict:
    """What a video's own words say about it being a cover; {} when nothing does."""
    says, work = says_cover(video_title) or says_cover(uploader), _work(video_title)
    if not says and not work:
        return {}
    return {"says": says, "work": work, "credits": credited(video_title, description)}


# ── the request's context ────────────────────────────────────────────────────

def context_fields(video_title: str, uploader: str, description: str = "") -> List[str]:
    """Context fields for a video that may be a cover; none otherwise."""
    if not clues(video_title, uploader, description):
        return []
    tidy = lambda text, length: re.sub(r"\s+", " ", (text or "")).replace(";", ",").strip()[:length]
    fields = ((TITLE_FIELD, tidy(video_title, 200)), (UPLOADER_FIELD, tidy(uploader, 80)),
              (NOTES_FIELD, " / ".join(f"Composer: {c}" for c in credited(video_title, description))))
    return [f"{name}={value}" for name, value in fields if value]


def take_fields(request_context: Optional[str]) -> Tuple[Optional[str], Dict[str, str]]:
    """(the context without this module's fields, those fields)."""
    if not request_context:
        return request_context, {}
    own, rest = {}, []
    for part in request_context.split("; "):
        name, sep, value = part.partition("=")
        if sep and name.strip() in (TITLE_FIELD, UPLOADER_FIELD, NOTES_FIELD):
            own[name.strip()] = value.strip()
        else:
            rest.append(part)
    return "; ".join(rest), own


# ── the official song ────────────────────────────────────────────────────────

def is_cover(candidate: Dict) -> bool:
    """A search result that calls itself a cover."""
    text = " ".join(str(candidate.get(k) or "") for k in ("artist", "title", "album"))
    return bool(_COVER_RESULT.search(text))


def _track(item: Dict) -> Dict:
    return {"id": str(item.get("id") or ""), "artist": (item.get("artist") or {}).get("name") or "",
            "title": item.get("title") or "", "album": (item.get("album") or {}).get("title") or "",
            "duration": int(item.get("duration") or 0), "rank": int(item.get("rank") or 0)}


def _search(query: str, get: Callable) -> List[Dict]:
    try:
        return [_track(i) for i in ((get("search", q=query, limit=25) or {}).get("data") or [])]
    except Exception as exc:
        logger.debug(f"Deezer search for {query!r} failed: {exc}")
        return []


def _on_soundtrack(track: Dict, work: str) -> bool:
    return bool(_words(work) & _words(track["album"])) and bool(_SOUNDTRACK.search(track["album"]))


def find(title: str, performer: str, video_title: str, uploader: str = "", description: str = "",
         duration: int = 0, get: Callable = _get) -> Optional[Dict]:
    """{'official': tracks, 'covers': tracks, 'sure': bool} for a cover whose
    official song Deezer has; None when the video isn't a cover or there is none.

    `official` holds the official song first. `sure` tells whether the video's
    own words lead to it, rather than its popularity alone. A fan's upload of
    the soundtrack recording itself also has 'reupload': True (and no covers)."""
    seen = clues(video_title, uploader, description)
    if not seen:
        return None
    names, work = song_names(title, video_title), seen["work"]
    cores = {_core(n) for n in names}
    performers = [p for p in (performer, uploader, re.sub(r"\s*[-–—]\s*topic$", "", uploader or "", flags=re.I)) if p]
    mine = lambda t: any(_same_artist(t["artist"], p) for p in performers)

    found: Dict[str, Dict] = {}
    heads = [" ".join(sorted(_words(re.split(r"[:–—-]", work)[0]), key=_norm(work).find))] if work else []
    for name in names:
        for extra in seen["credits"] + [h for h in heads if h] + [""]:
            for track in _search(f"{name} {extra}".strip(), get):
                if track["id"] and _core(track["title"]) in cores:
                    found.setdefault(track["id"], track)
    tracks = list(found.values())

    of_work = lambda t: bool(work) and bool(_words(work) & (_words(t["title"]) | _words(t["album"])))
    originals = [t for t in tracks if not mine(t) and not is_cover(t) and not variant_labels(t["title"])]
    credits = [t for t in originals if any(_same_artist(t["artist"], c) for c in seen["credits"])]
    scored = [t for t in originals if work and _on_soundtrack(t, work)]
    evident = credits + [t for t in scored if t not in credits]
    if not seen["says"]:
        own = [t for t in tracks if mine(t)]
        same = [t for t in scored if duration and abs(t["duration"] - duration) <= SAME_LENGTH]
        if same and not own:
            # A fan's upload of the soundtrack's own recording: that recording.
            same.sort(key=lambda t: (abs(t["duration"] - duration), -t["rank"]))
            return {"official": same[:1], "covers": [], "sure": True, "reupload": True}
        # Only another artist's soundtrack recording makes the performer's own
        # one a cover, and not when the performer is on that soundtrack too.
        if not scored or not own or any(_on_soundtrack(t, work) for t in own):
            return None
    evident.sort(key=lambda t: (t in credits and t in scored, t["rank"]), reverse=True)
    official, sure = evident, bool(evident)
    # A title that says what it is from ("… (From …)") is seldom the original.
    plain = [t for t in originals if not any(_FROM_WORK.match(i) for i in _BRACKETED.findall(t["title"]))]
    if not official and plain:
        best = max(plain, key=lambda t: t["rank"])
        others = max((t["rank"] for t in tracks if not _same_artist(t["artist"], best["artist"])), default=0)
        official = [best] if best["rank"] >= LIKELY * max(others, 1) else []
    if not official:
        return None
    official = [t for i, t in enumerate(official)
                if not any(_same_artist(t["artist"], o["artist"]) and abs(t["duration"] - o["duration"]) <= SAME_LENGTH
                           for o in official[:i])][:OFFICIALS]

    by_official = lambda t: any(_same_artist(t["artist"], o["artist"]) for o in evident + official)
    covers = [t for t in tracks if not by_official(t) and (mine(t) or is_cover(t) or of_work(t))]
    covers.sort(key=lambda t: (mine(t), t["rank"]), reverse=True)
    kept: List[Dict] = []
    for track in covers:
        if not any(_same_artist(track["artist"], k["artist"]) for k in kept):
            kept.append(track)
    for track in kept:
        track["linked"] = bool(mine(track) and duration and abs(track["duration"] - duration) <= SAME_LENGTH)
    return {"official": official, "covers": kept[:COVERS], "sure": sure}
