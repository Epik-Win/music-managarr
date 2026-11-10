"""Translate file paths between Plex's view (host) and this container's view.

Plex reports library files under `HOST_MUSIC_BASE_PATH/music/...`
(/srv/media/music/albums/... when HOST_MUSIC_BASE_PATH=/srv/media). That
directory is bind-mounted into this container at `MUSIC_BASE_DIR` (/music),
so the same file is /music/albums/... here. Unset, both sides use /music.
"""
import os


def host_music_root() -> str:
    return os.environ.get('HOST_MUSIC_BASE_PATH', '').rstrip('/') + '/music'


def container_music_root() -> str:
    return os.environ.get('MUSIC_BASE_DIR', '/music').rstrip('/') or '/music'


def host_to_container(path: str) -> str:
    """Map a Plex-reported host path to the container path; other paths pass through."""
    if not path:
        return path
    container_root = container_music_root()
    if path.startswith(container_root + '/') or path == container_root:
        return path
    host_root = host_music_root()
    if path.startswith(host_root + '/') or path == host_root:
        return container_root + path[len(host_root):]
    return path


def container_to_host(path: str) -> str:
    """Inverse of host_to_container, for paths handed to Plex (e.g. playlists)."""
    if not path:
        return path
    container_root = container_music_root()
    if path.startswith(container_root + '/') or path == container_root:
        return host_music_root() + path[len(container_root):]
    return path
