"""Plex authentication for the admin account and its Home users.

Only an admin *token* (`PLEX_TOKEN`) is stored — never the account password.
A token can be revoked from Plex's account settings without changing the
password, grants API access only, and keeps working for accounts with 2FA
enabled, where a non-interactive password login cannot.

Non-admin users must only ever act through their own Home-user token, so
their listening history is attributed to them and not to the admin. Those
tokens are minted here from the admin account and cached by the callers.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger('musicai.app')


def admin_token() -> str:
    token = os.environ.get('PLEX_TOKEN', '').strip()
    if not token:
        raise RuntimeError("PLEX_TOKEN is not set; it is required for Plex access")
    return token


def admin_account():
    from plexapi.myplex import MyPlexAccount
    return MyPlexAccount(token=admin_token())


def home_user_token(plex_url: str, username: str) -> Optional[str]:
    """Token for a Plex Home user, minted through the admin account."""
    from plexapi.server import PlexServer
    account = admin_account()
    user_obj = None
    try:
        user_obj = account.user(username)
    except Exception as exc:
        logger.debug(f"account.user() failed for '{username}': {exc}")
        for candidate in account.users() or []:
            if (getattr(candidate, 'username', '') or '').lower() == username.lower():
                user_obj = candidate
                break
    if not user_obj:
        logger.error(f"Plex Home user '{username}' not found on the admin account")
        return None
    switched = PlexServer(plex_url, admin_token()).switchUser(user_obj)
    return getattr(switched, '_token', None)
