"""User identity for the whole system.

The Wizarr mapping file (`WIZARR_MAPPING_FILE`) is the only source of users.
Its entries pair a Discord handle with a Plex username, and the first entry
is the admin. The file is `{discord_handle: plex_username}`, and
`DISCORD_TO_PLEX_MAPPING_ADMIN` (`discord:plex`, or either name alone) picks
which entry is the admin when the file's own order shouldn't be trusted.

Every user shares the single music library at `MUSIC_BASE_DIR`.
"""
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('musicai.app')

DEFAULT_MAPPING_FILE = '/data/wizarr/plex_invite_targets.json'


def mapping_file_path() -> str:
    return os.environ.get('WIZARR_MAPPING_FILE', DEFAULT_MAPPING_FILE)


def music_base_dir() -> str:
    return os.environ.get('MUSIC_BASE_DIR', '/music').rstrip('/') or '/music'


def parse_admin_override(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (discord_handle, plex_username) from DISCORD_TO_PLEX_MAPPING_ADMIN.

    Accepts `discord:plex`, a single-entry JSON object {discord: plex}, or a
    bare name (returned as (name, name) so it can be matched against either
    side of the mapping).
    """
    value = (value or '').strip()
    if not value:
        return None, None

    if value.startswith('{'):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict) and len(parsed) == 1:
                discord_user, plex_user = next(iter(parsed.items()))
                discord_user, plex_user = str(discord_user).strip(), str(plex_user).strip()
                if plex_user and discord_user:
                    return discord_user, plex_user
        except Exception:
            pass

    if ':' in value:
        a, b = [p.strip() for p in value.split(':', 1)]
        if a and b:
            return a, b

    return value, value


def load_wizarr_mapping(path: Optional[str] = None, admin_override: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Return an ordered {discord_handle: plex_username} dict, admin first.

    The file is read as {discord: plex}. Returns {} if it is missing or empty,
    and None if an admin override was given but matches no entry (a
    configuration error the caller should surface rather than silently pick a
    different admin).
    """
    path = path or mapping_file_path()
    if admin_override is None:
        admin_override = os.environ.get('DISCORD_TO_PLEX_MAPPING_ADMIN', '')

    try:
        with open(path, 'r') as fh:
            parsed = json.load(fh)
    except FileNotFoundError:
        logger.debug(f"Wizarr mapping file not found at {path}")
        return {}
    except Exception as exc:
        logger.warning(f"Failed to read Wizarr mapping file {path}: {exc}")
        return {}

    if not isinstance(parsed, dict):
        logger.debug(f"Wizarr mapping file {path} does not contain a JSON object")
        return {}

    # Discord handles are case-insensitive; keep them lowercase so lookups
    # by handle never depend on how the file was typed.
    mapping = {str(k).strip().lower(): str(v).strip() for k, v in parsed.items() if str(k).strip() and str(v).strip()}
    if not mapping:
        return {}

    admin_discord, admin_plex = parse_admin_override(admin_override)
    if not admin_discord and not admin_plex:
        return mapping

    admin_key = None
    for discord_user, plex_user in mapping.items():
        if discord_user == (admin_discord or '').lower() or plex_user.lower() == (admin_plex or '').lower():
            admin_key = discord_user
            break
    if admin_key is None:
        logger.error(f"DISCORD_TO_PLEX_MAPPING_ADMIN='{admin_override}' not found in Wizarr mapping file")
        return None

    ordered = {admin_key: mapping[admin_key]}
    ordered.update({d: p for d, p in mapping.items() if d != admin_key})
    return ordered


def canonical_plex_user(name: Optional[str], mapping: Dict[str, str]) -> str:
    """Resolve a Discord handle or Plex username (any case) to the Plex username."""
    raw = (name or '').strip()
    if not raw:
        return ''
    for discord_key, plex_value in mapping.items():
        if raw.lower() == discord_key.lower() or raw.lower() == plex_value.lower():
            return plex_value
    return raw


def plex_user_index(plex_user: str, mapping: Dict[str, str]) -> int:
    """Position of a user in the mapping (admin is 0); 0 if unknown."""
    users = list(mapping.values())
    canonical = canonical_plex_user(plex_user, mapping)
    return users.index(canonical) if canonical in users else 0


def plex_usernames(mapping: Dict[str, str]) -> List[str]:
    return list(mapping.values())
