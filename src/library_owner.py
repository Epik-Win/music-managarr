"""Songs and folders in the library belong to the library's owner.

MusicAI runs as root (the VPN needs it), so every file it downloads and
every folder it creates or moves into would be root's — and Lidarr, which
runs as the library's owner, can then not rename, move or re-tag those
songs ("Access to the path is denied"). `adopt()` hands them back. The
owner is PUID/PGID when set, else whoever owns the library folder itself.

    python3 library_owner.py [/music/albums]
"""
import logging
import os
import sys
from typing import Optional, Tuple

logger = logging.getLogger("musicai.library_owner")


def owner(root: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(root)
    except OSError:
        return None
    try:
        return int(os.environ.get("PUID") or st.st_uid), int(os.environ.get("PGID") or st.st_gid)
    except ValueError:
        logger.warning("PUID/PGID must be numbers; leaving ownership alone")
        return None


def adopt(root: Optional[str] = None) -> int:
    """Give every file and folder under `root` (default ALBUM_DIR) that isn't
    the owner's to the owner. Returns how many changed; does nothing unless
    running as root, or when the owner would be root."""
    root = root or os.environ.get("ALBUM_DIR", "/music/albums")
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return 0
    ids = owner(root)
    if ids is None or ids == (0, 0):
        return 0
    uid, gid = ids
    changed = 0
    for folder, _dirs, files in os.walk(root):
        for path in [folder] + [os.path.join(folder, name) for name in files]:
            try:
                st = os.lstat(path)
                if (st.st_uid, st.st_gid) != (uid, gid):
                    os.chown(path, uid, gid, follow_symlinks=False)
                    changed += 1
            except OSError as exc:
                logger.debug(f"Could not hand {path} to {uid}:{gid}: {exc}")
    if changed:
        logger.info(f"Handed {changed} file(s)/folder(s) in {root} to the library owner ({uid}:{gid})")
    return changed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(adopt(sys.argv[1] if len(sys.argv) > 1 else None))
