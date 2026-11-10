"""Identify a song by what is sung in it, for when every fingerprint service
comes up empty: the local whisper transcribes a few clips, candidate songs
are found by searching the clearest lines (Genius's API when a token is set,
and the local SearXNG), and each candidate's real lyrics (LRCLIB) are
compared with the transcript. Transcription uses any OpenAI-compatible
endpoint (WHISPER_URL) and model (WHISPER_MODEL). A small Whisper can return
nothing for singing over loud music (a dance track) while transcribing rap
fine; use a large model (large-v3-turbo) for songs. Only a song whose lyrics
match counts.

Lyrics name the song, not the recording: a cover of "Summertime" sings the
same words as Billie Holiday's. A match whose recording is the file's length
counts as identifying it (two votes); any other match is only a suggestion
(one vote), which Fix Metadata never applies on its own.

An obscure song may have no lyrics anywhere MusicAI can read (LRCLIB;
Genius's API has no lyrics and its pages refuse automated reads). Then
Genius's own answer is an unconfirmed suggestion, shown to the admin in
Manual Fix and never applied: the top Genius result for one of the sung
lines whose title is itself sung at least twice (Maddhouse's "Rain", whose
title is the chorus), and the only such song.
"""
import json
import logging
import os
import random
import re
import socket
import struct
import subprocess
import tempfile
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("LYRICS_ID_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
GENIUS_TOKEN = os.environ.get("GENIUS_ACCESS_TOKEN", "").strip()
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1").strip() or "whisper-1"
MIN_WORDS = 12                 # fewer sung words than this: an instrumental, or noise
MATCH_SCORE = 0.25             # share of the transcript's words in runs also found in the lyrics
MATCH_LEAD = 0.10              # ahead of the next-best song by at least this
# SearXNG's engines suspend it on bursts of searches, so it is used sparingly:
SEARX_PER_SONG = 3
SEARX_PER_DAY = int(os.environ.get("LYRICS_ID_SEARCHES_PER_DAY", "30"))
SEARX_INTERVAL = 15            # seconds between two searches
SEARX_PAUSE = 3600             # after SearXNG reports its engines suspended
_BUDGET_FILE = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "lyrics_id_searches.json")
SAME_RECORDING_SECONDS = 6     # a match this close in length is the file's own recording
GENIUS_LINES = 8               # Genius has no daily limit: more lines, more chances at the chorus
_TIMEOUT = 20

# What whisper writes over music with nobody singing.
_HALLUCINATIONS = re.compile(
    r"thank you(?: so much)? for watching|thanks for watching|subtitles by|subscribe|♪|\bmusic\b|"
    r"amara\.org|transcribed by|copyright", re.I)


# ── local services (this container's VPN breaks normal service-name DNS) ─────

def _docker_dns(name: str) -> Optional[str]:
    """Docker's embedded DNS answers when asked directly at 127.0.0.11, even
    though the VPN has replaced resolv.conf."""
    try:
        tid = random.randint(0, 65535)
        query = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
        query += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        sock.sendto(query, ("127.0.0.11", 53))
        data, _ = sock.recvfrom(512)
        if struct.unpack(">H", data[6:8])[0] < 1:
            return None
        return socket.inet_ntoa(data[-4:])
    except Exception:
        return None


def _service_url(env_var: str, container: str, port: int, fallback: str) -> str:
    if os.environ.get(env_var):
        return os.environ[env_var].rstrip("/")
    ip = _docker_dns(container)
    return f"http://{ip}:{port}" if ip else fallback


def whisper_url() -> str:
    return _service_url("WHISPER_URL", "litellm", 4000, "")


def searxng_url() -> Optional[str]:
    url = _service_url("SEARXNG_URL", "searxng", 8080, "")
    return url or None


# ── transcription ────────────────────────────────────────────────────────────

def _duration(path: str) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout
        return float(out.strip())
    except Exception:
        return 0.0


def transcribe(path: str, clip_seconds: int = 45) -> str:
    """What is sung in the file: whisper on up to three clips (after the
    intro, the middle, before the end)."""
    length = _duration(path)
    if length <= 0:
        return ""
    starts = sorted({min(30, max(0, length / 6)), max(0, length / 2 - clip_seconds / 2),
                     max(0, length - clip_seconds - 20)}) if length > 3 * clip_seconds else [0]
    base = whisper_url()
    if not base:
        logger.info("[lyrics_id] no Whisper service (set WHISPER_URL); skipping lyrics identification")
        return ""
    url = base + "/v1/audio/transcriptions"
    api_key = os.environ.get("WHISPER_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    texts = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, start in enumerate(starts):
            clip = os.path.join(tmp, f"clip{i}.mp3")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.0f}", "-t", str(clip_seconds), "-i", path,
                            "-ac", "1", "-ar", "16000", clip], check=False, timeout=120)
            if not os.path.exists(clip):
                continue
            try:
                with open(clip, "rb") as fh:
                    resp = requests.post(
                        url,
                        headers=headers,
                        files={"file": ("clip.mp3", fh, "audio/mpeg")},
                        data={"model": WHISPER_MODEL},
                        timeout=600,
                    )
                if not resp.ok:
                    # Not "no vocals": a wrong WHISPER_API_KEY or URL must not pass for an instrumental.
                    logger.warning(f"[lyrics_id] whisper answered {resp.status_code}: {resp.text[:200]}")
                    return ""
                texts.append((resp.json() or {}).get("text", "").strip())
            except Exception as exc:
                logger.info(f"[lyrics_id] whisper unavailable ({exc}); skipping lyrics identification")
                return ""
    return " ".join(t for t in texts if t)


def _words(text: str) -> List[str]:
    return re.findall(r"[^\W\d_]{2,}", (text or "").casefold())


def has_vocals(transcript: str) -> bool:
    """Real words, not whisper's filler over an instrumental."""
    cleaned = _HALLUCINATIONS.sub(" ", transcript or "")
    words = _words(cleaned)
    return len(words) >= MIN_WORDS and len(set(words)) >= MIN_WORDS // 2


def search_lines(transcript: str, count: int = 2) -> List[str]:
    """The clearest lines to search for. Whisper often leaves out punctuation
    but capitalises each sung line ("You're holding on Can't pretend that
    nothing's changed Living in the shadows…"), so lines are split there too;
    the longest are kept, cut to seven words — a short exact phrase is
    likelier to be heard right. Shouted all-caps runs are whisper's noise."""
    text = _HALLUCINATIONS.sub(" ", transcript or "")
    text = re.sub(r"(?<=[a-z',])\s+(?=[A-Z][a-z'])", "\n", text)
    parts = [p.strip() for p in re.split(r"[.!?\n]+", text) if p.strip()]
    parts = [p for p in parts if sum(c.isupper() for c in p) < 0.5 * sum(c.isalpha() for c in p)]
    parts.sort(key=lambda p: -len(_words(p)))
    lines, seen = [], set()
    for p in parts:
        # Repeated lines ("Now I, I, I, never meant to" / "Now I, I, I Never
        # meant to") are searched once.
        key = tuple(_words(p)[:4])
        if len(_words(p)) >= 5 and key not in seen:
            seen.add(key)
            lines.append(" ".join(p.split()[:7]))
        if len(lines) >= count:
            break
    return lines


# ── candidates ───────────────────────────────────────────────────────────────

# Genius's song id and art for each (artist, title) its search returned.
_GENIUS_SONGS: Dict[Tuple[str, str], Dict[str, object]] = {}


def _genius_candidates(line: str) -> List[Tuple[str, str]]:
    if not GENIUS_TOKEN:
        return []
    try:
        resp = requests.get("https://api.genius.com/search", params={"q": line},
                            headers={"Authorization": f"Bearer {GENIUS_TOKEN}"}, timeout=_TIMEOUT)
        hits = ((resp.json() or {}).get("response") or {}).get("hits") or []
    except Exception as exc:
        logger.debug(f"[lyrics_id] Genius search failed: {exc}")
        return []
    out = []
    for hit in hits[:5]:
        r = hit.get("result") or {}
        artist = (r.get("primary_artist") or {}).get("name", "")
        title = r.get("title", "")
        if artist and title:
            out.append((artist, title))
            _GENIUS_SONGS[(artist, title)] = {"id": r.get("id"), "art": r.get("song_art_image_url")}
    return out


def _genius_details(artist: str, title: str) -> Tuple[str, Optional[str]]:
    """(album, cover art URL) of a song Genius returned: its album, else the
    title (a single); its song art unless that is Genius's placeholder."""
    known = _GENIUS_SONGS.get((artist, title)) or {}
    album, art = "", known.get("art")
    if GENIUS_TOKEN and known.get("id"):
        try:
            song = requests.get(f"https://api.genius.com/songs/{known['id']}", timeout=_TIMEOUT,
                                headers={"Authorization": f"Bearer {GENIUS_TOKEN}"}).json()["response"]["song"]
            album = (song.get("album") or {}).get("name") or ""
            art = song.get("song_art_image_url") or art
        except Exception as exc:
            logger.debug(f"[lyrics_id] Genius song details failed: {exc}")
    if art and "default" in str(art).rsplit("/", 1)[-1]:
        art = None
    return album or title, art


# Lyrics pages: "Lil Baby – Freestyle Lyrics | Genius Lyrics", "Artist - Title
# Lyrics | AZLyrics.com", "Title Lyrics - Artist", "Title lyrics by Artist".
_DASHED = re.compile(r"^(?P<a>[^|]+?)\s+[–—-]\s+(?P<b>[^|]+?)(?:\s*[|(]|$)")
_BY = re.compile(r"^(?P<b>.+?)\s+lyrics\s+by\s+(?P<a>[^|,]+)", re.I)


def _pairs_from_title(title: str) -> List[Tuple[str, str]]:
    """(artist, title) readings of a result title, both ways round when the
    order isn't certain: the lyrics check throws out the wrong one."""
    text = re.sub(r"\s*\b(?:lyrics?|letra|paroles|songtext)\b\s*", " ", title or "", flags=re.I).strip()
    m = _BY.match(title or "")
    if m:
        return [(m.group("a").strip(), m.group("b").strip())]
    m = _DASHED.match(text)
    if not m:
        return []
    a, b = m.group("a").strip(), m.group("b").strip()
    return [(a, b), (b, a)]


def _budget() -> dict:
    try:
        with open(_BUDGET_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _spend_search() -> bool:
    """One SearXNG search, if today's allowance and the pause allow it."""
    import time
    budget = _budget()
    today = time.strftime("%Y-%m-%d")
    if budget.get("day") != today:
        budget = {"day": today, "used": 0, "last": 0, "paused_until": budget.get("paused_until", 0)}
    now = time.time()
    if budget.get("paused_until", 0) > now or budget["used"] >= SEARX_PER_DAY:
        return False
    wait = budget.get("last", 0) + SEARX_INTERVAL - now
    if wait > 0:
        time.sleep(wait)
    budget["used"] += 1
    budget["last"] = time.time()
    try:
        with open(_BUDGET_FILE, "w") as fh:
            json.dump(budget, fh)
    except Exception:
        pass
    return True


def _pause_searches() -> None:
    import time
    budget = _budget()
    budget["paused_until"] = time.time() + SEARX_PAUSE
    try:
        with open(_BUDGET_FILE, "w") as fh:
            json.dump(budget, fh)
    except Exception:
        pass


def _searxng_candidates(line: str, queries: List[str]) -> List[Tuple[str, str]]:
    """Lyrics pages a web search finds for the line (exact phrase). Each
    search spends from the shared allowance; a suspended SearXNG pauses it."""
    base = searxng_url()
    if not base or len(queries) >= SEARX_PER_SONG or not _spend_search():
        return []
    query = f'"{line}" lyrics'
    queries.append(query)
    try:
        data = requests.get(f"{base}/search", params={"q": query, "format": "json"}, timeout=_TIMEOUT).json() or {}
    except Exception as exc:
        logger.debug(f"[lyrics_id] SearXNG search failed: {exc}")
        return []
    results = data.get("results") or []
    if not results and any("suspend" in str(e).lower() for e in data.get("unresponsive_engines") or []):
        logger.info("[lyrics_id] SearXNG's engines are suspended; web searches paused for an hour")
        _pause_searches()
    out = []
    for r in results[:8]:
        out += _pairs_from_title((r.get("title") or "").strip())
    return out


def genius_search(transcript: str) -> List[Tuple[str, List[Tuple[str, str]]]]:
    """Each searched line with Genius's results for it, best first."""
    if not GENIUS_TOKEN:
        return []
    return [(line, _genius_candidates(line)) for line in search_lines(transcript, GENIUS_LINES)]


def candidates(transcript: str, genius: Optional[List[Tuple[str, List[Tuple[str, str]]]]] = None
               ) -> List[Tuple[str, str]]:
    """Songs to check: Genius first (when a token is set); the web search
    only when Genius found nothing, within its allowance."""
    seen, out, queries = set(), [], []
    genius = genius_search(transcript) if genius is None else genius
    found = [c for _line, hits in genius for c in hits]
    if not found:
        found = [c for line in search_lines(transcript) for c in _searxng_candidates(line, queries)]
    for artist, title in found:
        key = (artist.casefold(), re.sub(r"\s*[\(\[].*?[\)\]]", "", title).casefold())
        if key not in seen:
            seen.add(key)
            out.append((artist, title))
    return out[:24]


def _title_sung(transcript: str, title: str, times: int = 2) -> bool:
    """The song's title (brackets dropped) is sung at least `times` times."""
    words = _words(re.sub(r"\s*[\(\[].*?[\)\]]", "", title))
    if not words or sum(len(w) for w in words) < 3:
        return False
    sung = _words(transcript)
    n = len(words)
    return sum(1 for i in range(len(sung) - n + 1) if sung[i:i + n] == words) >= times


def unconfirmed_suggestion(transcript: str, genius: List[Tuple[str, List[Tuple[str, str]]]]
                           ) -> Optional[Tuple[str, str]]:
    """Genius's answer when no lyrics could confirm one: the top result for a
    sung line whose title is sung at least twice, when only one song is."""
    picks = {}
    for _line, hits in genius:
        if hits and _title_sung(transcript, hits[0][1]):
            artist, title = hits[0]
            picks.setdefault((artist.casefold(), title.casefold()), (artist, title))
    return next(iter(picks.values())) if len(picks) == 1 else None


# ── verification ─────────────────────────────────────────────────────────────

def _lrclib(artist: str, title: str) -> List[dict]:
    """LRCLIB's records of the song, with lyrics; also under the title
    without a version in brackets ("When We Were Beautiful (Live from The
    Circle Tour)" is listed as "When We Were Beautiful")."""
    plain = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title).strip()
    for name in dict.fromkeys(t for t in (title, plain) if t):
        try:
            resp = requests.get("https://lrclib.net/api/search", params={"artist_name": artist, "track_name": name},
                                headers={"User-Agent": "MusicAI (self-hosted music library)"}, timeout=_TIMEOUT)
            found = [r for r in (resp.json() or []) if r.get("plainLyrics")] if resp.ok else []
        except Exception as exc:
            logger.debug(f"[lyrics_id] LRCLIB lookup failed for {artist} – {name}: {exc}")
            found = []
        if found:
            return found
    return []


def lyrics_score(transcript: str, lyrics: str) -> float:
    """Share of the transcript's words that fall in runs of two or more words
    also sung, in order, in the lyrics. Tolerant of whisper's mishearings,
    strict about the song: the right song scores about 0.5-0.65, a wrong
    one close to 0."""
    heard, known = _words(transcript), _words(lyrics)
    if len(heard) < 3 or len(known) < 3:
        return 0.0
    matcher = SequenceMatcher(None, heard, known, autojunk=False)
    return sum(b.size for b in matcher.get_matching_blocks() if b.size >= 2) / len(heard)


def identify(path: str, transcript: Optional[str] = None) -> Optional[Dict[str, object]]:
    """The song whose lyrics are sung in the file, or None."""
    if not ENABLED:
        return None
    transcript = transcribe(path) if transcript is None else transcript
    if not has_vocals(transcript):
        logger.info("[lyrics_id] no sung words detected; not identifiable by lyrics")
        return None
    length = _duration(path)
    genius = genius_search(transcript)
    scored = []
    for artist, title in candidates(transcript, genius):
        for record in _lrclib(artist, title)[:3]:
            score = lyrics_score(transcript, record.get("plainLyrics", ""))
            gap = abs((record.get("duration") or 0) - length) if length else 999
            scored.append((score, -gap, record))
    if not scored:
        logger.info("[lyrics_id] no candidate's lyrics could be checked")
        return _unconfirmed(transcript, genius)
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    best_score, neg_gap, best = scored[0]
    runner = next((s for s in scored[1:] if s[2].get("trackName", "").casefold() != best.get("trackName", "").casefold()), None)
    if best_score < MATCH_SCORE or (runner and runner[0] > best_score - MATCH_LEAD):
        logger.info(f"[lyrics_id] no clear lyrics match (best {best.get('artistName')} – {best.get('trackName')}: "
                    f"{best_score:.2f})")
        return _unconfirmed(transcript, genius)
    same_recording = -neg_gap <= SAME_RECORDING_SECONDS
    logger.info(f"[lyrics_id] lyrics match: {best.get('artistName')} – {best.get('trackName')} "
                f"(score {best_score:.2f}, {'same length' if same_recording else 'other length'})")
    return {
        "artist": best.get("artistName", ""),
        "title": best.get("trackName", ""),
        "album": best.get("albumName", "") or "",
        "duration": best.get("duration"),
        "service": "Lyrics",
        "votes": 2 if same_recording else 1,
        "lyrics_score": round(best_score, 3),
    }


def _unconfirmed(transcript: str, genius) -> Optional[Dict[str, object]]:
    suggestion = unconfirmed_suggestion(transcript, genius)
    if suggestion is None:
        return None
    artist, title = suggestion
    album, art = _genius_details(artist, title)
    logger.info(f"[lyrics_id] Genius suggests {artist} – {title} [{album}]; no lyrics to confirm it")
    return {"artist": artist, "title": title, "album": album, "duration": None, "cover_art_url": art,
            "service": "Lyrics (Genius)", "votes": 1, "unconfirmed": True}
