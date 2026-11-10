#!/usr/bin/env python3
"""
Singleton Downloader Manager with File-Based Mutex Locking

This module provides a shared Downloader instance that can be safely used by
multiple processes (app.py and discord_bot.py) with coordinated access via
file-based locks.

Since app.py and discord_bot.py run as separate processes (not threads),
we use file-based locks instead of threading locks.
"""

import os
import logging
import time
import hashlib
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Global singleton instance
_downloader_instance = None
_downloader_lock = None

# ---- ARL Stale State (file-based cross-process coordination) ----------------
# app.py writes the flag when deemix reports auth failure.
# discord_bot.py polls _ARL_ALERT_FILE to send the admin notification.
# app.py waits until the ARL hash changes, then clears the flag and resumes.
_DATA_DIR       = os.getenv('DATA_DIR', '/app/data')
_ARL_STALE_FLAG = os.path.join(_DATA_DIR, 'arl_stale.flag')
_ARL_ALERT_FILE    = os.path.join(_DATA_DIR, 'arl_alert.json')   # consumed by discord_bot.py
_ARL_REFRESH_NONCE = os.path.join(_DATA_DIR, 'arl_refresh.nonce') # written by arl_server


def _get_current_arl_hash() -> str:
    """Hash the ARL from env var + deemix ~/.config/deemix/.arl file.

    Both sources are checked so admin refresh via either mechanism is detected:
      - Updating DEEZER_ARL env var (requires container restart)
      - Logging in to Deezer so deemix writes a fresh .arl file
    """
    arl_env = os.environ.get('DEEZER_ARL', '').strip()
    arl_file_content = ''
    try:
        arl_file = Path.home() / '.config' / 'deemix' / '.arl'
        if arl_file.exists():
            arl_file_content = arl_file.read_text().strip()
    except Exception:
        pass
    combined = f"{arl_env}|{arl_file_content}"
    return hashlib.md5(combined.encode()).hexdigest()


def get_arl_health_snapshot(project_env_file: Optional[str] = None) -> dict:
    """Capture the current ARL health state for logging and debugging.

    Includes the runtime env value, the deemix .arl file value, the project .env
    value, whether each matches, and the stale-state flag.
    """
    env_arl = os.environ.get('DEEZER_ARL', '').strip()
    file_arl = ''
    try:
        arl_file = Path.home() / '.config' / 'deemix' / '.arl'
        if arl_file.exists():
            file_arl = arl_file.read_text().strip()
    except Exception:
        pass

    env_file = project_env_file or os.environ.get('PROJECT_ENV_FILE')
    if not env_file:
        env_file = str(Path(__file__).resolve().parents[1] / '.env')
    project_arl = ''
    try:
        project_path = Path(env_file)
        if project_path.exists():
            for line in project_path.read_text().splitlines():
                if line.strip().startswith('DEEZER_ARL='):
                    project_arl = line.split('=', 1)[1].strip()
                    break
    except Exception:
        pass

    snapshot = {
        'stale': is_arl_stale(),
        'env_set': bool(env_arl),
        'file_set': bool(file_arl),
        'project_env_set': bool(project_arl),
        'env_matches_file': env_arl and file_arl and env_arl == file_arl,
        'env_matches_project': env_arl and project_arl and env_arl == project_arl,
        'file_matches_project': file_arl and project_arl and file_arl == project_arl,
        'env_hash': _get_current_arl_hash(),
        'runtime_arl_present': bool(env_arl),
        'deemix_arl_present': bool(file_arl),
        'project_env_path': env_file,
    }
    return snapshot


def log_arl_health(label: str = 'ARL health') -> None:
    """Emit a concise, structured log message describing current ARL sync health."""
    snapshot = get_arl_health_snapshot()
    logger.info(
        "%s: stale=%s env_set=%s file_set=%s project_env_set=%s "
        "env_matches_file=%s env_matches_project=%s file_matches_project=%s hash=%s",
        label,
        snapshot['stale'],
        snapshot['env_set'],
        snapshot['file_set'],
        snapshot['project_env_set'],
        snapshot['env_matches_file'],
        snapshot['env_matches_project'],
        snapshot['file_matches_project'],
        snapshot['env_hash'],
    )


def is_arl_stale() -> bool:
    """Return True when the ARL stale flag file exists (cross-process check)."""
    return os.path.exists(_ARL_STALE_FLAG)


def set_arl_stale(stale: bool) -> None:
    """Write or remove the ARL stale flag file.

    Called by downloader.py when login_via_arl() fails (stale=True),
    and by app.py when a fresh ARL is detected and downloads should resume
    (stale=False).
    """
    if stale:
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_ARL_STALE_FLAG, 'w') as _f:
                _f.write(f"{os.getpid()}\n{time.time()}\n")
            logger.warning(
                "ARL stale flag SET -- all deemix downloads paused "
                "until admin refreshes the Deezer ARL token"
            )
        except Exception as _exc:
            logger.warning(f"Could not write ARL stale flag: {_exc}")
    else:
        try:
            os.remove(_ARL_STALE_FLAG)
            logger.info("ARL stale flag CLEARED -- deemix downloads will resume")
        except FileNotFoundError:
            pass
        except Exception as _exc:
            logger.warning(f"Could not clear ARL stale flag: {_exc}")


def write_arl_alert() -> None:
    """Write a JSON alert file that discord_bot.py polls every 60 s.

    discord_bot.py reads this file, sends the admin channel message, then
    deletes the file so the alert is only sent once per stale event.
    """
    import json as _json
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        payload = {
            'type': 'arl_stale',
            'timestamp': time.time(),
            'pid': os.getpid(),
            'message': (
                "**Deezer ARL is stale** -- all downloads are paused.\n"
                "Please log in to Deezer to refresh your session:\n"
                "https://www.deezer.com/login\n\n"
                "Once logged in, deemix will pick up the new ARL automatically "
                "and downloads will resume within 60 seconds."
            ),
        }
        with open(_ARL_ALERT_FILE, 'w') as _f:
            _json.dump(payload, _f)
        logger.info(f"ARL alert file written -> {_ARL_ALERT_FILE}")
    except Exception as _exc:
        logger.warning(f"Could not write ARL alert file: {_exc}")


def wait_for_arl_refresh(poll_interval: int = 60, timeout: int = 86400) -> bool:
    """Block the calling thread until the ARL changes or the timeout elapses.

    Polls every poll_interval seconds, comparing a hash of the current ARL
    (env var + deemix config file) against the hash captured at stale detection.
    When the hash changes the admin has refreshed the token; the stale flag is
    cleared and this function returns True so the caller can retry downloads.

    Returns True if ARL was refreshed, False if timeout elapsed.
    """
    log_arl_health('ARL refresh wait start')
    stale_hash = _get_current_arl_hash()
    stale_start = time.time()
    elapsed = 0
    # Clear any leftover nonce from a previous refresh cycle
    try:
        if os.path.exists(_ARL_REFRESH_NONCE):
            os.remove(_ARL_REFRESH_NONCE)
    except Exception:
        pass
    logger.warning(
        f"Waiting for admin to refresh the Deezer ARL "
        f"(checking every {poll_interval}s, giving up after {timeout // 3600}h)..."
    )
    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval
        # Condition 1: ARL content changed
        if _get_current_arl_hash() != stale_hash:
            log_arl_health(f'ARL refreshed after {elapsed}s')
            logger.info(f"ARL change detected after {elapsed}s -- resuming downloads")
            set_arl_stale(False)
            return True
        # Condition 2: stale flag deleted externally
        if not os.path.exists(_ARL_STALE_FLAG):
            logger.info(f"ARL stale flag cleared externally after {elapsed}s -- resuming")
            return True
        # Condition 3: nonce file written by arl_server after we started waiting.
        # Fires even when the same ARL string is re-submitted (hash unchanged).
        try:
            if os.path.exists(_ARL_REFRESH_NONCE):
                nonce_mtime = os.path.getmtime(_ARL_REFRESH_NONCE)
                if nonce_mtime >= stale_start:
                    logger.info(
                        f"ARL refresh nonce detected after {elapsed}s "
                        "-- resuming downloads"
                    )
                    set_arl_stale(False)
                    try:
                        os.remove(_ARL_REFRESH_NONCE)
                    except Exception:
                        pass
                    return True
        except Exception:
            pass
        logger.debug(f"  ARL still stale ({elapsed}s / {timeout}s elapsed)")

    logger.warning(
        f"ARL refresh timeout after {timeout}s -- "
        "clearing stale flag and proceeding without deemix"
    )
    set_arl_stale(False)
    return False



class DownloaderLock:
    """File-based mutex lock for coordinating access across processes."""
    
    def __init__(self, lock_file: str = None):
        """
        Initialize lock with a file path.
        
        Args:
            lock_file: Path to lock file. If None, uses DATA_DIR/downloader.lock
        """
        if lock_file is None:
            data_dir = os.getenv('DATA_DIR', './musicai/data')
            os.makedirs(data_dir, exist_ok=True)
            lock_file = os.path.join(data_dir, 'downloader.lock')
        
        self.lock_file = lock_file
        self.acquired = False
    
    # ── Stale-lock helpers ───────────────────────────────────────────────────

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        """Return True if the given PID is still running on this host."""
        try:
            os.kill(pid, 0)   # signal 0 = existence check, no actual signal
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True       # process exists, we just can't signal it
        except Exception:
            return False

    # Maximum seconds a process may hold the lock before it is considered
    # frozen (alive but stuck — e.g. deemix hanging on a rate-limited download
    # with no internal timeout).  Defaults to 5 hours.
    # Override via env var DOWNLOADER_LOCK_MAX_AGE_SECONDS.
    MAX_LOCK_AGE: int = int(
        os.environ.get("DOWNLOADER_LOCK_MAX_AGE_SECONDS", str(5 * 3600))
    )

    @property
    def _status_file(self) -> str:
        """Path of the JSON status file written alongside the lock file."""
        return self.lock_file + ".status"

    def write_status(self, track: str, tool: str, stage: str,
                     batch_info: str = "") -> None:
        """Write current download progress to the status file.

        Called by downloader.py at each stage so that waiting processes can
        report *why* the lock is held, not just for how long.

        Args:
            track:      "Artist - Title" string currently being attempted.
            tool:       Active downloader tool name (e.g. "deemix").
            stage:      Short stage label, e.g. "authenticating", "searching",
                        "downloading", "timed_out", "post_processing".
            batch_info: Optional "N/Total" string for the current batch position.
        """
        try:
            import json as _json
            payload = {
                "pid":        os.getpid(),
                "track":      track,
                "tool":       tool,
                "stage":      stage,
                "batch_info": batch_info,
                "updated_at": time.time(),
            }
            _tmp = self._status_file + ".tmp"
            with open(_tmp, "w") as fh:
                _json.dump(payload, fh)
            os.replace(_tmp, self._status_file)
        except Exception:
            pass  # status file is best-effort, never block downloads

    def read_status(self) -> Optional[dict]:
        """Read and return the current status dict, or None if unavailable."""
        try:
            import json as _json
            with open(self._status_file, "r") as fh:
                return _json.load(fh)
        except Exception:
            return None

    def clear_status(self) -> None:
        """Remove the status file (called on lock release)."""
        try:
            if os.path.exists(self._status_file):
                os.remove(self._status_file)
        except Exception:
            pass

    def _read_lock_pid(self) -> Optional[int]:
        """Read the PID stored in the lock file; return None if unreadable."""
        pid, _ = self._read_lock_info()
        return pid

    def _read_lock_info(self):
        """Return (pid: int|None, written_at: float|None) from the lock file.

        The lock file format written by acquire() is two lines:
            <PID>
            <unix_timestamp_float>
        A single-line lock file (PID only) is read too.
        """
        try:
            with open(self.lock_file, 'r') as fh:
                lines = fh.read().strip().splitlines()
            pid        = int(lines[0].strip())        if lines          else None
            written_at = float(lines[1].strip())      if len(lines) > 1 else None
            return pid, written_at
        except Exception:
            return None, None

    def _clear_stale_lock(self) -> bool:
        """Remove the lock file if it is stale.

        Staleness is determined by three criteria (checked in order):
          1. Unreadable / corrupt lock file.
          2. The holding PID is no longer alive (crashed / OOM-killed / SIGKILL'd).
          3. The lock is older than MAX_LOCK_AGE seconds even if the PID is still
             alive — the process is frozen (e.g. stuck in a hanging download).

        Returns True if the lock was cleared so the caller can retry immediately.
        """
        holder, written_at = self._read_lock_info()

        # ── 1. Unreadable ─────────────────────────────────────────────────────
        if holder is None:
            try:
                os.remove(self.lock_file)
                logger.warning("🔧 Removed unreadable/corrupt lock file")
                return True
            except Exception:
                return False

        # ── 2. Dead PID ───────────────────────────────────────────────────────
        if not self._pid_is_alive(holder):
            try:
                os.remove(self.lock_file)
                logger.warning(
                    f"🔧 Removed stale lock (PID {holder} is no longer running — "
                    "process crashed or was killed without releasing the lock)"
                )
                return True
            except Exception:
                return False

        # ── 3. Alive but frozen (held too long) ───────────────────────────────
        if written_at is not None:
            age_s = time.time() - written_at
            if age_s > self.MAX_LOCK_AGE:
                try:
                    os.remove(self.lock_file)
                    logger.warning(
                        f"🔧 Removed stale lock — PID {holder} has held it for "
                        f"{age_s/3600:.1f}h which exceeds MAX_LOCK_AGE "
                        f"({self.MAX_LOCK_AGE/3600:.1f}h). "
                        "Process is alive but appears frozen (stuck download?)."
                    )
                    return True
                except Exception:
                    return False

        return False   # Lock is legitimately held

    # ── acquire ──────────────────────────────────────────────────────────────

    def acquire(self, timeout: int = 3600, poll_interval: float = 1.0) -> bool:
        """Acquire the lock with timeout and automatic stale-lock recovery.

        If the lock file exists but the PID inside is no longer running
        (crashed, OOM-killed, SIGKILL'd) the file is removed and the acquire
        proceeds immediately.  This prevents indefinite blocking after a hard
        process death.

        Args:
            timeout:       Max seconds to wait (default 1 h for long cycles).
            poll_interval: Poll interval in seconds.

        Returns:
            True if acquired, False if timed out.
        """
        start_time = time.time()
        lock_pid   = os.getpid()

        while True:
            elapsed = time.time() - start_time

            try:
                fd = os.open(
                    self.lock_file,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
                # Two-line format: PID\nUNIX_TIMESTAMP — enables age-based staleness detection
                os.write(fd, f"{lock_pid}\n{time.time()}\n".encode())
                os.close(fd)
                self.acquired = True
                logger.info(f"🔒 Downloader lock acquired by PID {lock_pid}")
                return True

            except FileExistsError:
                # Check whether the lock is stale before waiting
                if self._clear_stale_lock():
                    continue   # retry the atomic create immediately

                if elapsed >= timeout:
                    holder, written_at = self._read_lock_info()
                    age_h = (time.time() - written_at) / 3600 if written_at else 0
                    logger.error(
                        f"⏱️ Downloader lock timeout after {timeout:.0f}s "
                        f"(held by PID {holder or '?'} for {age_h:.1f}h — "
                        f"still alive but not releasing lock)"
                    )
                    return False

                # Log every 60 s to keep logs readable
                if elapsed > 0 and int(elapsed) % 60 == 0:
                    holder, written_at = self._read_lock_info()
                    age_min = (time.time() - written_at) / 60 if written_at else 0
                    status  = self.read_status()
                    if status:
                        _stale_s = time.time() - status.get("updated_at", time.time())
                        _track   = status.get("track", "?")
                        _tool    = status.get("tool",  "?")
                        _stage   = status.get("stage", "?")
                        _batch   = status.get("batch_info", "")
                        _batch_s = f" [{_batch}]" if _batch else ""
                        _stale_s_str = f", status {_stale_s:.0f}s old" if _stale_s > 5 else ""
                        logger.info(
                            f"⏳ Waiting for downloader lock "
                            f"({elapsed:.0f}s elapsed, "
                            f"held by PID {holder or '?'}, "
                            f"lock age {age_min:.0f}min / max {self.MAX_LOCK_AGE/60:.0f}min) "
                            f"— currently: [{_tool}] {_stage}{_batch_s}: {_track}{_stale_s_str}"
                        )
                    else:
                        logger.info(
                            f"⏳ Waiting for downloader lock "
                            f"({elapsed:.0f}s elapsed, "
                            f"held by PID {holder or '?'}, "
                            f"lock age {age_min:.0f}min / max {self.MAX_LOCK_AGE/60:.0f}min) "
                            f"— no status available (holder may be idle or between tracks)"
                        )

                time.sleep(poll_interval)

            except Exception as exc:
                logger.error(f"❌ Error acquiring lock: {exc}")
                return False
    
    async def async_acquire(self, timeout: int = 30, poll_interval: float = 1.0) -> bool:
        """Async-safe acquire: uses asyncio.sleep instead of time.sleep.

        For use inside discord_bot's async event handlers so the event loop
        (and the Discord heartbeat) are never blocked while waiting.

        Args:
            timeout:       Max seconds to wait.
            poll_interval: Seconds between retries (default 1 s).

        Returns:
            True if acquired, False if timed out.
        """
        import asyncio
        start_time = time.time()
        lock_pid   = os.getpid()

        while True:
            elapsed = time.time() - start_time
            try:
                fd = os.open(
                    self.lock_file,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
                os.write(fd, f"{lock_pid}\n{time.time()}\n".encode())
                os.close(fd)
                self.acquired = True
                logger.info(f"🔒 Downloader lock acquired by PID {lock_pid} (async)")
                return True
            except FileExistsError:
                if self._clear_stale_lock():
                    continue
                if elapsed >= timeout:
                    holder, written_at = self._read_lock_info()
                    age_h = (time.time() - written_at) / 3600 if written_at else 0
                    logger.error(
                        f"⏱️ Downloader lock timeout after {timeout:.0f}s "
                        f"(held by PID {holder or '?'} for {age_h:.1f}h)"
                    )
                    return False
                await asyncio.sleep(poll_interval)
            except Exception as exc:
                logger.error(f"❌ Error in async_acquire: {exc}")
                return False

    def release(self) -> bool:
        """Release the lock.

        Only removes the file if it was written by this process (PID check).
        Prevents accidental release of another process's lock after a restart.
        """
        try:
            if not os.path.exists(self.lock_file):
                self.acquired = False
                return True   # already gone — nothing to do

            holder = self._read_lock_pid()
            my_pid = os.getpid()
            if holder is not None and holder != my_pid:
                logger.warning(
                    f"⚠️ Skipping lock release — file is held by PID {holder}, "
                    f"we are PID {my_pid}"
                )
                self.acquired = False
                return False

            os.remove(self.lock_file)
            self.acquired = False
            self.clear_status()
            logger.info(f"🔓 Downloader lock released by PID {my_pid}")
            return True
        except Exception as exc:
            logger.error(f"❌ Error releasing lock: {exc}")
            return False
    
    def __enter__(self):
        """Context manager entry."""
        self.acquire()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.release()
        return False


def initialize_downloader_singleton(require_vpn: bool = False) -> bool:
    """Initialize the Downloader singleton for the current process.

    Called once from entrypoint.py (the parent/manager process) before any
    user subprocesses are spawned.  Responsibilities:

    1. VPN: nothing here; the VPN is brought up lazily on the first
       download, so a manager without downloads never touches it.

    2. Create the in-process singleton Downloader instance.

    Args:
        require_vpn: When True, return False (failure) if VPN could not be
                     set up.  When False (default), VPN is optional — the
                     function still returns True so downloads can proceed
                     over the standard connection.

    Returns:
        True if initialisation succeeded (or VPN is optional and failed),
        False only when require_vpn=True and VPN setup failed.
    """
    global _downloader_instance

    logger.info("🔧 Setting up Downloader singleton (VPN + proxy)...")

    # ── Check VPN credentials ─────────────────────────────────────────────────
    mullvad_account = os.environ.get("MULLVAD_ACCOUNT_NUMBER", "").strip()
    if mullvad_account:
        logger.info(f"✓ Mullvad VPN credentials present (account: {mullvad_account[:5]}...)")
    elif require_vpn:
        logger.error("❌ VPN_REQUIRED=true but MULLVAD_ACCOUNT_NUMBER is not set")
        logger.error("   Set MULLVAD_ACCOUNT_NUMBER, or VPN_REQUIRED=false to continue without VPN")
        return False
    else:
        logger.info("ℹ️  No VPN credentials configured — downloads will use direct connection")

    # ── Step 2: Create in-process Downloader singleton ────────────────────────
    if _downloader_instance is None:
        try:
            from downloader import Downloader
            # No socks5_proxy: the Downloader brings the VPN up on first use
            _downloader_instance = Downloader(analyzer=None, socks5_proxy=None)
            logger.info("✓ Downloader singleton created in parent process")
        except Exception as exc:
            logger.error(f"❌ Failed to create Downloader singleton: {exc}")
            return False

    return True


def get_downloader(analyzer=None):
    """
    Get or create the singleton Downloader instance.
    
    Args:
        analyzer: MusicAIAnalyzer instance. Optional - if None, waits briefly then creates with None analyzer.
    
    Returns:
        Downloader instance
    
    Notes:
        - app.py initializes with an analyzer (preferred)
        - discord_bot.py can call with analyzer=None (will create with None if not yet initialized)
        - Both processes will have independent Downloader instances (globals don't cross processes)
    """
    global _downloader_instance
    process_role = os.getenv('PROCESS_ROLE', os.getenv('APP_USER_NAME', 'unknown'))
    
    if _downloader_instance is None:
        if analyzer is None:
            # Discord bot case: wait briefly for app.py to initialize its instance,
            # then create our own if needed (each process has its own globals anyway)
            logger.debug(f"[{process_role}] Downloader not yet initialized in this process, waiting briefly for app.py...")
            for attempt in range(2):
                time.sleep(0.5)
                if _downloader_instance is not None:
                    logger.debug(f"Downloader initialized by app.py after {attempt+0.5}s")
                    return _downloader_instance
            
            # After brief wait, create downloader without analyzer (will still work for discord immediate downloads)
            logger.debug("Creating Downloader without analyzer for discord_bot process...")
            from downloader import Downloader
            _downloader_instance = Downloader(None, socks5_proxy=None)
            logger.info("✓ Downloader instance created for discord_bot")
        else:
            # app.py case: initialize with analyzer. No proxy: the VPN
            # tunnels the whole container.
            from downloader import Downloader
            _downloader_instance = Downloader(analyzer, socks5_proxy=None)
            logger.info("✓ Downloader singleton initialized by app.py")
    
    return _downloader_instance


def write_lock_status(track: str, tool: str, stage: str,
                      batch_info: str = "") -> None:
    """Module-level convenience wrapper for DownloaderLock.write_status().

    Called from downloader.py without needing a lock instance reference.
    Reads the lock file path from the same env var used by DownloaderLock so
    it always writes to the right status file regardless of how the lock was
    constructed.
    """
    try:
        data_dir  = os.getenv("DATA_DIR", "./musicai/data")
        lock_file = os.path.join(data_dir, "downloader.lock")
        status_file = lock_file + ".status"
        import json as _json
        payload = {
            "pid":        os.getpid(),
            "track":      track,
            "tool":       tool,
            "stage":      stage,
            "batch_info": batch_info,
            "updated_at": time.time(),
        }
        _tmp = status_file + ".tmp"
        with open(_tmp, "w") as fh:
            _json.dump(payload, fh)
        os.replace(_tmp, status_file)
    except Exception:
        pass  # best-effort, never block caller


def get_downloader_lock(lock_file: str = None) -> DownloaderLock:
    """
    Get the singleton DownloaderLock instance.
    
    Args:
        lock_file: Path to lock file (optional)
    
    Returns:
        DownloaderLock instance
    """
    global _downloader_lock
    
    if _downloader_lock is None:
        _downloader_lock = DownloaderLock(lock_file)
        logger.info(f"✓ Downloader lock initialized: {_downloader_lock.lock_file}")
    
    return _downloader_lock

