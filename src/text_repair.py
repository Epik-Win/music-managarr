"""Undo mojibake: UTF-8 text that was decoded as Latin-1/Windows-1252.

"ما أجمل الحياة" read that way becomes "Ù\x85Ø§ Ø£Ø¬Ù\x85Ù\x84 …" and
"Montréal" becomes "MontrÃ©al". The usual cause is an HTTP reply labelled
`text/plain` without a charset (ACRCloud does this): `requests` then assumes
ISO-8859-1. The damage is reversible as long as no byte was dropped.
"""
import re

# A UTF-8 lead byte read as Latin-1 (Ã Â Ø Ù Ð Ñ …) followed by a
# continuation byte read as Latin-1 or Windows-1252.
_MOJIBAKE_RE = re.compile(
    r"[Â-ô][\u0080-¿ŒœŠšŸŽžƒˆ˜"
    r"–—‘-„†-•…‰‹›€™]"
)


def _as_raw_bytes(text: str) -> bytes:
    """The bytes the text was decoded from: Latin-1 for U+0000–U+00FF,
    Windows-1252 for its extra characters (€, ‘, …)."""
    out = bytearray()
    for ch in text:
        code = ord(ch)
        if code < 256:
            out.append(code)
        else:
            out += ch.encode("cp1252")   # raises if it can't be a misread byte
    return bytes(out)


def fix_mojibake(text):
    """Return the repaired text, or the input unchanged if it isn't mojibake."""
    if not isinstance(text, str) or not _MOJIBAKE_RE.search(text):
        return text
    current = text
    for _ in range(2):                    # text garbled twice needs two passes
        if not _MOJIBAKE_RE.search(current):
            break
        try:
            current = _as_raw_bytes(current).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
    return current


def looks_garbled(text) -> bool:
    return fix_mojibake(text) != text


def repair_fields(meta, fields=("artist", "title", "album")):
    """Copy of a metadata dict with mojibake repaired in the given fields."""
    if not meta:
        return meta
    fixed = dict(meta)
    for field in fields:
        if isinstance(fixed.get(field), str):
            fixed[field] = fix_mojibake(fixed[field])
    return fixed


_FEAT_TAIL = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+(.+)$", re.IGNORECASE)
_FEAT_BRACKET = re.compile(r"[\(\[](?:feat\.?|ft\.?|featuring)\s+([^\)\]]+)[\)\]]", re.IGNORECASE)


def dedupe_feat(title):
    """Drop a trailing feat. credit the title already carries in brackets:
    ACRCloud can return "Fall (feat. Christina Sapalo) feat. Christina
    Sapalo"."""
    if not title:
        return title
    tail = _FEAT_TAIL.search(title)
    if tail:
        head = title[:tail.start()]
        bracketed = {m.group(1).strip().casefold() for m in _FEAT_BRACKET.finditer(head)}
        if tail.group(1).strip().casefold() in bracketed:
            return head.rstrip()
    return title
