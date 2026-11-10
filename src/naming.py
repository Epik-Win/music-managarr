"""Artist/title string parsing shared by the analyzer and the CLI."""
import re
from typing import List, Tuple


def parse_artist_field(artist_field: str) -> Tuple[str, str]:
    """Return (primary_artist, featured_string).
    
    Examples:
      'Artist feat. Someone' -> ('Artist', 'Someone')
      'Artist (feat. Someone & Other)' -> ('Artist', 'Someone & Other')
      'Artist1 & Artist2' -> ('Artist1', 'Artist2')
      'Artist1_Artist2' -> ('Artist1', 'Artist2')  # Underscore separator
    
    Returns:
        Tuple of (primary_artist, featured_artists_string)
    """
    if not artist_field:
        return ("Unknown", "")
    
    s = artist_field.strip()

    # Look for parenthetical featuring: (feat. ...)
    # Whole words only: "Thrift Shop" is not "Thri feat. Shop", "(Left Behind)" is no credit.
    m = re.search(r"\(([^)]*\b(?:feat(?:uring)?|ft)\b\.?[^)]*)\)", s, flags=re.I)
    feat = ""
    if m:
        feat = m.group(1)
        # remove parentheses content
        s = re.sub(r"\([^)]*\)", "", s).strip()
    else:
        # Look for explicit feat tokens
        m2 = re.search(r"(?i)\b(?:feat(?:uring)?|ft)\b\.?:?\s*(.+)$", s)
        if m2:
            feat = m2.group(1)
            s = re.sub(r"(?i)\b(?:feat(?:uring)?|ft)\b\.?:?\s*.+$", "", s).strip()

    # If still no feat, split on common separators and treat extras as features
    if not feat:
        # Include underscore as a separator (for cases like "Kendrick Lamar_SZA")
        for sep in ["_", " & ", ";", ",", " / ", " x ", " + ", " and "]:
            if sep in s:
                parts = [p.strip() for p in s.split(sep) if p.strip()]
                if len(parts) > 1:
                    primary = parts[0]
                    feat = ", ".join(parts[1:])
                    s = primary
                    break

    # Clean feature prefix if it still has leading 'feat'
    feat = re.sub(r"(?i)^(?:feat(?:uring)?|ft)\.?\s*", "", feat).strip()
    primary = s.strip() or "Unknown"
    return (primary, feat)


# ── names that are written: artists, albums, titles, folders ────────────────

_OPEN, _CLOSE = "([{", ")]}"


def split_outside_brackets(text: str, separator: "re.Pattern") -> List[str]:
    """Split at `separator` only where no bracket is open: "RISE (ft. The
    Glitch Mob, Mako, and The Word Alive)" has no comma to split at."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text or ""):
        ch = text[i]
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE and depth:
            depth -= 1
        elif depth == 0:
            m = separator.match(text, i)
            if m and m.end() > i:
                parts.append(text[start:i])
                start = i = m.end()
                continue
        i += 1
    parts.append((text or "")[start:])
    return [p.strip() for p in parts if p.strip()]


def has_unbalanced_brackets(text: str) -> bool:
    depth = 0
    for ch in text or "":
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
            if depth < 0:
                return True
    return depth != 0


def balanced(text: str) -> str:
    """A name fit to write: an unclosed bracket and what follows it are cut
    ("RISE (ft. The Glitch Mob" → "RISE"), a stray closing bracket dropped,
    and a separator left dangling at the end trimmed."""
    if not has_unbalanced_brackets(text):
        return (text or "").strip()
    out, depth, cut = [], 0, None
    for ch in text:
        if ch in _OPEN:
            if depth == 0:
                cut = len(out)          # where an unclosed bracket would be cut
            depth += 1
        elif ch in _CLOSE:
            if depth == 0:
                continue                # a closing bracket nothing opened
            depth -= 1
        out.append(ch)
    s = "".join(out[:cut] if depth and cut is not None else out)
    return re.sub(r"[\s\-–—|,;:/]+$", "", s).strip() or (text or "").strip()


_CHANNEL_WORDS = re.compile(
    r"\s*[-–—]\s*topic$"                                   # YouTube's "Adele - Topic"
    r"|\s*vevo$"                                            # "AdeleVEVO"
    r"|[\s\-–—|]*(?:the\s+)?official(?:\s+(?:youtube|music|artist))*(?:\s+channel)?$"
    r"|\s+youtube\s+channel$",
    re.IGNORECASE)


def artist_from_channel(channel: str) -> str:
    """The artist a YouTube channel is named after: "Joe Hisaishi Official",
    "いきものがかり Official YouTube Channel", "AdeleVEVO", "Adele - Topic".
    A name that is nothing but those words stays as it is."""
    return _CHANNEL_WORDS.sub('', (channel or '').strip()).strip() or (channel or '').strip()


_QUOTED_SONG = re.compile(r'^(?P<before>.*?)[『「](?P<song>[^』」]+)[』」](?P<after>.*)$')
_VIDEO_WORDS = re.compile(r'^\W*(?:official\s+)?(?:music\s+video|lyric\s+video|video|audio|mv|pv)?\W*$', re.IGNORECASE)


def song_in_quotes(title: str, artist: str) -> str:
    """The song in a Japanese-style video title, which sets it in quotation
    marks: "いきものがかり 『ブルーバード』Music Video" is ブルーバード. Only when
    the rest is the artist's name and words about the video; anything else
    ("『Song』Live") may be part of the title, which then stays as it is."""
    match = _QUOTED_SONG.match(title or '')
    if not match:
        return title
    before = match['before'].strip(' -–—|:')
    if before and before.casefold() != (artist or '').strip().casefold():
        return title
    return match['song'].strip() if _VIDEO_WORDS.match(match['after']) else title
