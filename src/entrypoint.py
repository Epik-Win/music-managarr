#!/usr/bin/env python3
import os, sys, subprocess, signal, logging, threading, time
import importlib
import json
import hashlib
from pathlib import Path

import cycle_schedule
import users

# Read log level from LOG_LEVEL env var (default: INFO).
# Set LOG_LEVEL=DEBUG in docker-compose.yml (or shell) to enable debug output.
_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

if _log_level == logging.DEBUG:
    logger.debug("🐛 Debug logging enabled (LOG_LEVEL=DEBUG)")


class MultiUserManager:
    def __init__(self):
        self.num_users = None
        self.user_paths = []
        self.user_names = []
        self.plex_usernames = []  # Plex usernames (for token lookup)
        self.discord_usernames = []  # Discord usernames (optional; maps to plex/user order)
        self.plex_sections = []
        self._wizarr_hash = None
        self.app_script = "/app/app.py"
        self.discord_bot_script = "/app/discord_bot.py"
        self.arl_server_script = "/app/arl_server.py"
        self.discord_bot_process = None
        self.arl_server_process = None
        self.one_star_process = None
        self.fix_metadata_process = None
        # The first entry in the Wizarr mapping file is always treated as the
        # ADMIN / MAIN user. Populated by validate_wizarr_mapping_file().
        self.admin_plex_username = None
        self.admin_discord_username = None

    def _load_wizarr_mapping_file(self):
        """Ordered {discord: plex} mapping from the Wizarr file, admin first; {} if unusable."""
        return users.load_wizarr_mapping() or {}

    def _compute_user_paths_for_usernames(self, user_names: list) -> list:
        """Every user shares the single library at MUSIC_BASE_DIR."""
        return [users.music_base_dir()] * len(user_names)

    def startup_cycles_enabled(self):
        """Return True when the manager should start the full app cycle immediately."""
        value = os.environ.get("CYCLE_ON_STARTUP", "true").strip().lower()
        return value in ("true", "1", "yes", "y", "on")
    

    def validate_wizarr_mapping_file(self):
        """Ensure the Wizarr mapping file exists and contains AT LEAST ONE user.

        The file maps Discord username -> Plex username. Its first entry is
        always treated as the ADMIN / MAIN user, and must be present before
        any user processes are spawned. This check runs before anything else
        in validate_configuration() since the mapping file is the
        authoritative source of which users this deployment manages.
        """
        wizarr_mapping_file = os.environ.get('WIZARR_MAPPING_FILE', '/data/wizarr/plex_invite_targets.json')
        logger.info(f"🔍 Validating Wizarr mapping file: {wizarr_mapping_file}")

        if not os.path.exists(wizarr_mapping_file):
            logger.error(f"❌ Wizarr mapping file not found: {wizarr_mapping_file}")
            logger.error("   At least one user (the admin) must be present in this file before starting.")
            return False

        try:
            with open(wizarr_mapping_file, 'r') as fh:
                parsed = json.load(fh)
        except Exception as e:
            logger.error(f"❌ Could not parse Wizarr mapping file {wizarr_mapping_file}: {e}")
            return False

        if not isinstance(parsed, dict) or len(parsed) == 0:
            logger.error(f"❌ Wizarr mapping file {wizarr_mapping_file} must contain AT LEAST ONE user (the admin)")
            return False

        mapping = self._load_wizarr_mapping_file()
        if not mapping:
            logger.error(f"❌ Wizarr mapping file {wizarr_mapping_file} did not produce a usable Discord->Plex mapping")
            return False

        self.admin_discord_username = next(iter(mapping.keys()))
        self.admin_plex_username = mapping[self.admin_discord_username]

        logger.info(f"✓ Wizarr mapping file valid ({len(parsed)} user(s))")
        logger.info(f"✓ Admin/main user: {self.admin_plex_username} (Discord: {self.admin_discord_username})")
        return True

    def _run_subprocess_and_stream_output(self, args, env, user_name, stage):
        """Run a subprocess and stream its output into the parent logger."""
        logger.info(f"  ▶ Starting subprocess for {stage} (user={user_name}): {' '.join(args)}")
        process = subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        if process.stdout is not None:
            for line in process.stdout:
                raw_line = line.rstrip()
                if "[USER:" in raw_line:
                    logger.info(raw_line)
                else:
                    logger.info(f"[{user_name}] {raw_line}")

        return_code = process.wait()
        logger.info(f"  ✓ Subprocess complete for {user_name} (stage={stage}, rc={return_code})")
        return return_code
    
    def validate_configuration(self):
        logger.info("🔍 Validating multi-user configuration...")

        # The Wizarr mapping file is the only source of users: one process per
        # entry, the first entry is the admin.
        if not self.validate_wizarr_mapping_file():
            return False

        if not os.environ.get("PLEX_TOKEN", "").strip():
            logger.error("❌ PLEX_TOKEN is required (the admin account's Plex token)")
            logger.error("   It is used to authenticate and retrieve each Home user's token")
            return False
        logger.info("✓ PLEX_TOKEN configured")

        mapping = self._load_wizarr_mapping_file()
        self.discord_usernames = list(mapping.keys())
        self.plex_usernames = list(mapping.values())
        self.user_names = list(self.plex_usernames)
        self.num_users = len(self.user_names)
        self.user_paths = self._compute_user_paths_for_usernames(self.user_names)

        # One shared Plex music library for every user.
        section = os.environ.get("PLEX_MUSIC_SECTIONS", "Music").strip().split(":")[0].strip() or "Music"
        self.plex_sections = [section] * self.num_users

        wizarr_mapping_file = os.environ.get('WIZARR_MAPPING_FILE', '/data/wizarr/plex_invite_targets.json')
        try:
            self._wizarr_mtime = os.path.getmtime(wizarr_mapping_file)
        except OSError:
            self._wizarr_mtime = None

        logger.info(f"✓ Users from Wizarr mapping: {':'.join(self.user_names)} (admin={self.admin_plex_username})")
        logger.info(f"✓ Plex music section: {section}")

        logger.info("📁 Validating user paths...")
        for i, path in enumerate(self.user_paths):
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
                logger.info(f"  ✓ User {i} ({self.user_names[i]}): {path}")
            except Exception as e:
                logger.error(f"  ❌ Cannot access {path}: {e}")
                return False

        logger.info("✅ Configuration validation successful")
        return True

    # --- Dynamic Wizarr mapping helpers ---------------------------------

    def build_worker_plan(self):
        if not self.user_names:
            mapping = self._load_wizarr_mapping_file()
            if mapping:
                self.discord_usernames = list(mapping.keys())
                self.plex_usernames = list(mapping.values())
                self.user_names = list(self.plex_usernames)
                self.user_paths = self._compute_user_paths_for_usernames(self.user_names)
                self.num_users = len(self.user_names)
                self.admin_discord_username = self.discord_usernames[0]
                self.admin_plex_username = self.plex_usernames[0]
                if len(self.plex_sections) < self.num_users:
                    fallback_section = self.plex_sections[0] if self.plex_sections else 'Music'
                    while len(self.plex_sections) < self.num_users:
                        self.plex_sections.append(fallback_section)
                elif len(self.plex_sections) > self.num_users:
                    self.plex_sections = self.plex_sections[: self.num_users]

        if self.num_users is None:
            self.num_users = len(self.user_names)

        plan = []
        for i in range(self.num_users):
            plex_username = self.plex_usernames[i] if i < len(self.plex_usernames) else self.user_names[i]
            discord_username = self.discord_usernames[i] if i < len(self.discord_usernames) else plex_username
            user_path = self.user_paths[i] if i < len(self.user_paths) else self._compute_user_paths_for_usernames([plex_username])[0]
            plex_section = self.plex_sections[i] if i < len(self.plex_sections) else (self.plex_sections[0] if self.plex_sections else 'Music')
            plan.append({
                'index': i,
                'user_name': self.user_names[i],
                'plex_username': plex_username,
                'discord_username': discord_username,
                'user_path': user_path,
                'plex_section': plex_section,
                'is_admin': plex_username == self.admin_plex_username,
            })
        return plan

    def refresh_users_from_mapping(self):
        """Reload the Wizarr mapping and update the worker plan if it has changed."""
        mapping = self._load_wizarr_mapping_file()
        if not mapping:
            return False

        new_discord_usernames = list(mapping.keys())
        new_plex_usernames = list(mapping.values())
        new_admin_discord = new_discord_usernames[0]
        new_admin_plex = new_plex_usernames[0]

        if (
            new_plex_usernames == self.plex_usernames
            and new_discord_usernames == self.discord_usernames
            and new_admin_plex == self.admin_plex_username
            and new_admin_discord == self.admin_discord_username
        ):
            return False

        self.admin_plex_username = new_admin_plex
        self.admin_discord_username = new_admin_discord
        self.plex_usernames = new_plex_usernames
        self.discord_usernames = new_discord_usernames
        self.user_names = list(self.plex_usernames)
        self.user_paths = self._compute_user_paths_for_usernames(self.user_names)
        self.num_users = len(self.user_names)

        if len(self.plex_sections) < self.num_users:
            fallback_section = self.plex_sections[0] if self.plex_sections else 'Music'
            while len(self.plex_sections) < self.num_users:
                self.plex_sections.append(fallback_section)
        elif len(self.plex_sections) > self.num_users:
            self.plex_sections = self.plex_sections[: self.num_users]

        logger.info(f"✓ Refreshed worker plan from Wizarr mapping: {self.plex_usernames}")
        return True

    def run_user_cycle(self, user_index):
        user_name = self.user_names[user_index]
        plex_username = self.plex_usernames[user_index]
        logger.info(f"▶ Running one app.py cycle for user '{user_name}' (Plex: {plex_username})")
        env = self.get_user_environment(user_index)
        env['APP_RUN_ONCE'] = 'true'

        try:
            return self._run_subprocess_and_stream_output(
                [sys.executable, self.app_script, 'cycle-once'],
                env=env,
                user_name=user_name,
                stage='cycle-once',
            )
        except Exception as e:
            logger.error(f"❌ Failed to run app.py for user '{user_name}': {e}")
            return 1

    
    def run_library_review(self, when):
        """Run library_review.py (see its docstring) with the admin's environment."""
        admin = next((i for i, name in enumerate(self.plex_usernames) if name == self.admin_plex_username), 0)
        try:
            return self._run_subprocess_and_stream_output(
                [sys.executable, '/app/library_review.py', '--when', when],
                env=self.get_user_environment(admin),
                user_name='library-review',
                stage=f'library-review-{when}',
            )
        except Exception as e:
            logger.error(f"❌ Library review ({when}) failed: {e}")
            return 1

    def get_user_environment(self, user_index):
        env = os.environ.copy()
        user_path = self.user_paths[user_index]
        user_name = self.user_names[user_index]
        try:
            plex_username = self.plex_usernames[user_index]
        except Exception:
            plex_username = self.user_names[user_index]
            logger.warning(f"⚠️ Plex username missing for index {user_index}; falling back to app username '{plex_username}'")
        try:
            plex_section = self.plex_sections[user_index]
        except Exception:
            plex_section = self.plex_sections[0] if self.plex_sections else 'Music'
        
        # One library: every song lives in albums/; probation and soon-to-go
        # are symlinks under DATA_DIR/library (library_state).
        env["MUSIC_DIR"] = os.path.join(user_path, "albums")
        env["ALBUM_DIR"] = os.path.join(user_path, "albums")
        env["TEMP_DOWNLOAD_DIR"] = os.path.join(user_path, "albums")
        env["EXPORTIFY_DIR"] = os.path.join(user_path, "exportify")
        env["PLEX_MUSIC_SECTION"] = plex_section
        env["PLEX_USERNAME"] = plex_username  # For token lookup
        env["DB_PATH"] = f"/app/data/{user_name}/music_ai.db"
        env["DATA_DIR"] = "/app/data"  # For Discord requests database
        env["APP_USER_INDEX"] = str(user_index)
        env["PROCESS_ROLE"] = "app_user"

        # APP_USER_NAME is the internal/Plex identity used for databases,
        # locks, cycle files, and per-user processing. Discord is an alias
        # used only for request attribution and presentation.
        env["APP_USER_NAME"] = plex_username
        env["APP_IS_ADMIN_USER"] = "true" if plex_username and plex_username == self.admin_plex_username else "false"

        # Propagate log level so child processes (app.py, downloader.py) use the same level
        env["LOG_LEVEL"] = os.environ.get("LOG_LEVEL", "INFO")
        return env
    
    def create_user_directories(self, user_index, base_path):
        user_name = self.user_names[user_index]
        try:
            for dir_name in ["albums", "exportify"]:
                Path(os.path.join(base_path, dir_name)).mkdir(parents=True, exist_ok=True)
            Path(f"/app/data/{user_name}").mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            logger.error(f"Failed: {e}")
            return False

    
    def setup_deemix_arl(self):
        """Configure deemix with ARL token from environment and other settings."""
        arl = os.environ.get('DEEZER_ARL', '').strip()
        if not arl:
            logger.warning("⚠️  DEEZER_ARL not set. Deemix will require interactive ARL input.")
            return
        
        config_dir = Path.home() / '.config' / 'deemix'
        config_file = config_dir / 'config.json'
        
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            
            # Read existing config
            if config_file.exists():
                with open(config_file, 'r') as f:
                    config = json.load(f)
            else:
                config = {}
            
            # Add ARL token
            config['arl'] = arl
            
            # Set deemix defaults for reliable downloading
            # These settings help with account license limitations
            if 'maxBitrate' not in config:
                config['maxBitrate'] = '0'  # Best available (will fallback gracefully)
            if 'fallbackBitrate' not in config:
                config['fallbackBitrate'] = True  # Enable bitrate fallback
            if 'downloadLocation' not in config:
                config['downloadLocation'] = '/downloads'  # Default, will be overridden in code
            
            # Write back config
            with open(config_file, 'w') as f:
                json.dump(config, f, indent=2)
            
            logger.info("✅ Deemix ARL token and settings configured successfully")
        except Exception as e:
            logger.error(f"❌ Failed to configure deemix ARL: {e}")
    
    def discover_and_cache_plex_tokens(self):
        """
        Authenticate as Plex admin, get tokens for CONFIGURED Plex Home users only.
        Processes the users from the Wizarr mapping; the first is the admin.
        """
        import sqlite3
        import plex_auth
        from plexapi.server import PlexServer

        plex_url = os.environ.get("PLEX_URL", "http://localhost:32400")
        plex_server_name = os.environ.get("PLEX_SERVER_NAME", "").strip()
        # First PLEX_USERNAME is the admin user
        admin_username = self.plex_usernames[0] if self.plex_usernames else ""
        
        logger.info("🔐 Authenticating with Plex using admin account...")
        
        try:
            account = plex_auth.admin_account()
            logger.info(f"  ✓ Authenticated to Plex.tv as: {account.username}")
            
            # Get the Plex server resource
            logger.info(f"🔍 Connecting to Plex server at {plex_url}")
            
            # List available servers to help debug
            try:
                resources = account.resources()
                server_names = [r.name for r in resources if r.provides == 'server' and r.name == plex_server_name]
                if not server_names:
                    logger.warning(f"  ⚠️ No servers found in account resources")
                else:
                    logger.debug(f"  Available servers: {server_names}")
            except:
                pass
            
            try:
                # Connect using the auth token
                auth_token = account.authenticationToken
                plex = PlexServer(plex_url, auth_token)
                logger.info(f"  ✓ Connected to server: {plex.friendlyName}")
            except Exception as e:
                logger.error(f"  ❌ Could not connect to server: {e}")
                logger.info(f"   Tried URL: {plex_url}")
                logger.info(f"   Make sure PLEX_URL is correct and accessible from container")
                return
            
            # Get list of Plex Home users and their tokens.
            # Prefer the users from the configured Discord->Plex mapping, but fall back to
            # discovering them from the Plex account if the mapping is empty.
            configured_users = list(self.plex_usernames or [])
            if not configured_users:
                try:
                    discovered_users = [user.username for user in account.users() or [] if getattr(user, 'username', '').strip()]
                    if discovered_users:
                        configured_users = discovered_users
                        logger.info(f"👥 Discovered Plex home users from Plex API: {', '.join(configured_users)}")
                    else:
                        configured_users = [admin_username] if admin_username else []
                except Exception as exc:
                    logger.warning(f"Could not discover Plex home users from Plex API: {exc}")
                    configured_users = [admin_username] if admin_username else []

            logger.info(f"👥 Retrieving tokens for Plex users: {', '.join(configured_users)}")
            token_map = {}
            
            # Cache the admin token separately - this is the master token that works for server access
            admin_token = account.authenticationToken
            token_map['_admin'] = admin_token
            if admin_username:
                token_map[admin_username] = admin_token
            logger.info(f"✓ Cached admin server token as '_admin'")
            
            plex_to_discord = {
                p.lower(): d
                for p, d in zip(self.plex_usernames, self.discord_usernames)
                if p and d
            }

            def resolve_plex_user(plex_username):
                if not plex_username:
                    return None

                lower_username = plex_username.strip().lower()

                try:
                    return account.user(plex_username)
                except Exception:
                    pass

                mapped_discord = plex_to_discord.get(lower_username)
                if mapped_discord:
                    try:
                        return account.user(mapped_discord)
                    except Exception:
                        pass

                import difflib
                best_candidate = None
                best_score = 0.0
                for user_obj in account.users() or []:
                    if user_obj is None:
                        continue
                    candidates = [
                        getattr(user_obj, 'username', '') or '',
                        getattr(user_obj, 'title', '') or '',
                        getattr(user_obj, 'email', '') or '',
                    ]
                    for candidate in candidates:
                        if not candidate:
                            continue
                        candidate_lower = candidate.strip().lower()
                        if lower_username == candidate_lower:
                            return user_obj
                        if mapped_discord and mapped_discord.strip().lower() == candidate_lower:
                            return user_obj
                        if lower_username in candidate_lower or candidate_lower in lower_username:
                            return user_obj
                        score = difflib.SequenceMatcher(None, lower_username, candidate_lower).ratio()
                        if score > best_score:
                            best_score = score
                            best_candidate = user_obj

                if best_candidate and best_score >= 0.45:
                    logger.warning(
                        f"  ⚠️ Fallback matched Plex username '{plex_username}' to user '{getattr(best_candidate, 'username', None) or getattr(best_candidate, 'title', None)}'"
                    )
                    return best_candidate
                return None

            try:
                # Get tokens for the configured Plex users (or the users discovered from Plex API)
                for plex_username in configured_users:
                    try:
                        # Skip if this is the admin user itself
                        if plex_username == admin_username:
                            logger.debug(f"  Admin user '{admin_username}' (already have admin token)")
                            continue
                        
                        # Resolve the home user and switch on the Plex server to retrieve their token.
                        user_obj = resolve_plex_user(plex_username)
                        if user_obj is None:
                            logger.error(f"  ❌ Could not resolve Plex user '{plex_username}' from Plex Home users")
                            return False
                        switched_plex = plex.switchUser(user_obj)
                        hu_title = getattr(user_obj, 'title', plex_username)
                        hu_token = getattr(switched_plex, '_token', None)
                        if hu_token:
                            token_map[plex_username] = hu_token
                            if hu_title and hu_title != plex_username:
                                token_map[hu_title] = hu_token
                            hu_username = getattr(user_obj, 'username', None)
                            hu_email = getattr(user_obj, 'email', None)
                            if hu_username and hu_username != plex_username:
                                token_map[hu_username] = hu_token
                            if hu_email and hu_email != plex_username:
                                token_map[hu_email] = hu_token
                            logger.info(f"  ✓ Retrieved token for Plex user: {plex_username}")
                        else:
                            logger.error(f"  ❌ No token found for Plex user: {plex_username}")
                            return False
                    except Exception as e:
                        logger.error(f"  ❌ Error switching to Plex user '{plex_username}': {e}")
                        return False
                
                # For single-user setups with admin, just use admin token (no switchHomeUser needed)
                if self.num_users == 1 and admin_username in self.plex_usernames and len(token_map) == 1:
                    logger.info(f"  ✓ Single-user setup: using admin token directly")
                elif not token_map or (len(token_map) == 1 and '_admin' in token_map):
                    logger.error(f"❌ No configured user tokens retrieved via switchHomeUser()")
                    logger.info(f"   Must have tokens for all configured users to access their libraries")
                    return False
                    
            except Exception as e:
                logger.error(f"  ❌ Error retrieving Plex home users: {e}")
                import traceback
                logger.debug(f"Traceback: {traceback.format_exc()}")
                return False
            
            logger.info(f"✓ Retrieved {len(token_map)-1} per-user token(s) + admin token = {len(token_map)} total tokens")
            
            # Cache tokens in each app user's database
            logger.info(f"💾 Caching tokens in per-user databases...")
            for app_user_index, app_user_name in enumerate(self.user_names):
                db_path = f"/app/data/{app_user_name}/music_ai.db"
                
                # Ensure database directory exists
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                
                try:
                    logger.debug(f"  Opening database: {db_path}")
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS plex_tokens (
                            token TEXT PRIMARY KEY,
                            username TEXT,
                            obtained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    
                    # Cache all retrieved tokens (so any app user can access any user's libraries if needed).
                    # Only the current token is kept per user: Plex revokes old ones, and a
                    # cycle that picked one up would get nothing but 401s.
                    for plex_username, token in token_map.items():
                        try:
                            cursor.execute('DELETE FROM plex_tokens WHERE username = ? AND token != ?',
                                           (plex_username, token))
                            cursor.execute('INSERT OR REPLACE INTO plex_tokens (token, username, obtained_at) VALUES (?, ?, CURRENT_TIMESTAMP)', 
                                         (token, plex_username))
                            logger.debug(f"  ✓ Cached token for Plex user '{plex_username}' in app user '{app_user_name}' database")
                        except Exception as e:
                            logger.debug(f"  Error caching token: {e}")
                    
                    conn.commit()
                    conn.close()
                    logger.info(f"  ✓ Updated database for app user '{app_user_name}': {db_path}")
                except Exception as e:
                    logger.error(f"  ❌ Error updating database for user '{app_user_name}': {e}")
            
            logger.info(f"✅ Token retrieval and caching complete")
            return True
        
        except Exception as e:
            logger.error(f"❌ Error during Plex authentication: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
    
    def initialize_user_plex_libraries(self):
        """
        Initialize Plex library scanning for each configured user.
        
        This ensures that:
        1. Plex library metadata is available in the database before app.py starts
        2. Track scores are populated for preference-based filtering
        3. Fresh cleanup-albums runs will see accurate Plex data
        4. Works regardless of CYCLE_ON_STARTUP setting
        """
        if not self.startup_cycles_enabled():
            logger.info("CYCLE_ON_STARTUP=false; skipping startup Plex library initialization cycles")
            return

        logger.info("=" * 70)
        logger.info("📊 Initializing Plex libraries for all users...")
        logger.info("=" * 70)
        
        # Don't import `app` at function entry. Importing it here would cause
        # module-level initialization in `app.py` to run with the current
        # (parent) process environment (e.g. APP_USER_NAME unset -> 'default').
        # We'll import or reload `app` inside the per-user loop after setting
        # the environment for that user so module-level constants pick up the
        # correct `APP_USER_NAME`.
        app_module = None

        success_count = 0
        # Run per-user `app.py cycle-once` subprocesses instead of importing
        # `app.py` into the parent process. This ensures no module-level
        # initialization in `app.py` runs under the parent (which would pick
        # up default environment values like APP_USER_NAME='default').
        for user_index in range(self.num_users):
            user_name = self.user_names[user_index]
            plex_section = self.plex_sections[user_index] if user_index < len(self.plex_sections) else (self.plex_sections[0] if self.plex_sections else 'Music')
            logger.info(f"📁 Initializing Plex library for user '{user_name}' (section: {plex_section}) via subprocess...")
            try:
                env = self.get_user_environment(user_index)
                env['APP_RUN_ONCE'] = 'true'
                # Run app.py cycle-once in a separate process so it performs
                # analyzer initialization and DB population with the correct
                # per-user environment.
                return_code = self._run_subprocess_and_stream_output(
                    [sys.executable, self.app_script, 'cycle-once'],
                    env=env,
                    user_name=user_name,
                    stage='initialize-plex-library',
                )
                if return_code == 0:
                    logger.info(f"  ✓ Subprocess initialization succeeded for '{user_name}' (rc=0)")
                    success_count += 1
                else:
                    logger.warning(f"  ⚠️ Subprocess initialization failed for '{user_name}' (rc={return_code})")
            except Exception as e:
                logger.warning(f"  ⚠️  Failed to initialize Plex for user '{user_name}': {e}")
        
        if success_count > 0:
            logger.info(f"✅ Plex initialization complete for {success_count}/{self.num_users} user(s)\n")
            return True
        else:
            logger.warning(f"⚠️  Plex initialization skipped for all users\n")
            return False

    def reset_cleanup_databases(self):
        """
        Reset cleanup status databases to ensure full re-processing.
        
        This ensures that:
        1. When /app/data is deleted, cleanup-albums runs on ALL songs
        2. Old cleanup status doesn't prevent re-processing
        3. Each user starts fresh without stale records
        
        Call this during startup if cleanup tracking should be refreshed.
        """
        logger.info("=" * 70)
        logger.info("🔄 Checking cleanup databases...")
        logger.info("=" * 70)
        
        for user_index in range(self.num_users):
            user_name = self.user_names[user_index]
            db_path = f"/app/data/{user_name}/music_ai.db"
            
            try:
                import sqlite3
                
                # Ensure database exists
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                
                # Create/reset cleanup_status table
                cursor.execute('''
                    DROP TABLE IF EXISTS cleanup_status
                ''')
                
                cursor.execute('''
                    CREATE TABLE cleanup_status (
                        track_path TEXT PRIMARY KEY,
                        metadata_done BOOLEAN DEFAULT 0,
                        video_checked BOOLEAN DEFAULT 0,
                        symlink_done BOOLEAN DEFAULT 0,
                        deemix_clean BOOLEAN DEFAULT 0,
                        symlink_targets TEXT DEFAULT '[]',
                        last_run TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                conn.commit()
                conn.close()
                
                logger.info(f"  ✓ Reset cleanup tracking database for user '{user_name}'")
                
            except Exception as e:
                logger.warning(f"  ⚠️  Could not reset cleanup database for '{user_name}': {e}")
    

    def spawn_discord_bot_process(self):
        """Spawn the Discord bot as a separate background process."""
        if not os.environ.get('DISCORD_BOT_TOKEN'):
            logger.warning("⚠️  DISCORD_BOT_TOKEN not set in environment. Skipping Discord bot initialization.")
            return None
        
        if not os.path.exists(self.discord_bot_script):
            logger.warning(f"⚠️  Discord bot script not found at {self.discord_bot_script}. Skipping Discord bot initialization.")
            return None
        
        logger.info("🤖 Starting Discord Bot process...")
        
        bot_env = os.environ.copy()
        bot_env['MUSIC_BASE_DIR'] = users.music_base_dir()
        # Keep the bot's identity distinct from any real user's so nothing in
        # app.py-derived code paths treats it as an analyzer for a user.
        bot_env['APP_USER_NAME'] = 'discord-bot'
        bot_env['APP_USER_INDEX'] = '-1'
        bot_env['PROCESS_ROLE'] = 'discord_bot'

        # Ensure bot has access to shared config but remove user-specific paths
        for key in ["MUSIC_DIR", "ALBUM_DIR", "TEMP_DOWNLOAD_DIR", "EXPORTIFY_DIR", "PLEX_MUSIC_SECTION", "DB_PATH"]:
            bot_env.pop(key, None)
        
        try:
            proc = subprocess.Popen([sys.executable, self.discord_bot_script], env=bot_env, preexec_fn=os.setsid)
            logger.info(f"✓ Discord Bot spawned with PID: {proc.pid}")
            return proc
        except Exception as e:
            logger.error(f"❌ Failed to spawn Discord bot: {e}")
            return None

    def spawn_arl_server_process(self):
        """Spawn the ARL refresh web server as a separate background process.

        Listens on PORT (required); a reverse proxy can publish it at
        ARL_PUBLIC_URL.

        The server lets an admin log in to Deezer (or paste an ARL manually),
        writes the new token to ~/.config/deemix/.arl, and
        downloader_manager.wait_for_arl_refresh() detects the change within
        60 s — releasing every paused download process automatically.

        The server starts unconditionally (it's a lightweight Flask process
        with no external deps beyond flask + requests).  If PORT is not set,
        we skip and log a warning rather than crashing.
        """
        port = os.environ.get("PORT", "").strip()
        if not port:
            logger.warning(
                "⚠️  PORT env var not set — skipping ARL server. "
                "Set PORT to enable the ARL refresh page."
            )
            return None

        if not os.path.exists(self.arl_server_script):
            logger.warning(
                f"⚠️  ARL server script not found at {self.arl_server_script} — skipping."
            )
            return None

        logger.info(f"🌐 Starting ARL refresh server on port {port}...")

        # Inherit the full environment so DATA_DIR, DEEZER_ARL, etc. are available.
        # ARL_SERVER_SECRET may also be set to protect the submit endpoints.
        srv_env = os.environ.copy()
        srv_env["APP_USER_NAME"] = "arl-server"
        srv_env["APP_USER_INDEX"] = "-1"
        srv_env["PROCESS_ROLE"] = "arl_server"

        try:
            proc = subprocess.Popen(
                [sys.executable, self.arl_server_script],
                env=srv_env,
                preexec_fn=os.setsid,
            )
            logger.info(
                f"✓ ARL server spawned (PID {proc.pid}) — "
                f"accessible at http://0.0.0.0:{port}"
                + (f" / {os.environ['ARL_PUBLIC_URL']}" if os.environ.get('ARL_PUBLIC_URL') else "")
            )
            return proc
        except Exception as e:
            logger.error(f"❌ Failed to spawn ARL server: {e}")
            return None
    
    def spawn_one_star_watcher(self):
        """Background process that deletes tracks the admin rates one star.
        Always started: the admin can switch it on and off from Discord
        (/one_star_delete), and it checks that setting before every pass."""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'one_star_watcher.py')
        env = os.environ.copy()
        env.update({"APP_USER_NAME": "one-star-watcher", "APP_USER_INDEX": "-1", "PROCESS_ROLE": "one_star_watcher"})
        try:
            proc = subprocess.Popen([sys.executable, script], env=env, preexec_fn=os.setsid)
            logger.info(f"⭐ One-star watcher spawned (PID {proc.pid})")
            return proc
        except Exception as exc:
            logger.error(f"❌ Failed to spawn one-star watcher: {exc}")
            return None

    def spawn_fix_metadata_daemon(self):
        """Background process for daily metadata repair, independent of
        SCAN_INTERVAL: each user's Fix Metadata playlist, a library scan for
        missing metadata (Lidarr, then audio fingerprinting), and the admin's
        Manual Fix playlist and its admin notice. See fix_metadata_daemon.py."""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fix_metadata_daemon.py')
        if not os.path.exists(script):
            logger.warning(f"⚠️  fix_metadata_daemon.py not found at {script}. Skipping.")
            return None
        env = os.environ.copy()
        env.update({"APP_USER_NAME": "fix-metadata-daemon", "APP_USER_INDEX": "-1", "PROCESS_ROLE": "fix_metadata_daemon"})
        try:
            proc = subprocess.Popen([sys.executable, script], env=env, preexec_fn=os.setsid)
            logger.info(f"🧭 Fix Metadata daemon spawned (PID {proc.pid})")
            return proc
        except Exception as exc:
            logger.error(f"❌ Failed to spawn Fix Metadata daemon: {exc}")
            return None

    def _initial_schedule(self, scan_interval):
        """(due_at, users already done) for the pass to run first after start."""
        schedule = cycle_schedule.load()
        if cycle_schedule.pass_in_progress(schedule):
            done = cycle_schedule.users_done(schedule)
            logger.info(f"🔁 Resuming the pass started {schedule.get('started')} (already done: {done or 'nobody'})")
            return cycle_schedule.now(), done
        due_at = cycle_schedule.next_due(schedule)
        if due_at is not None:
            logger.info(f"📅 Next full cycle is due at {due_at.isoformat()}")
            return due_at, []
        if self.startup_cycles_enabled():
            logger.info("CYCLE_ON_STARTUP=true and no schedule on disk; starting the first cycle now")
            return cycle_schedule.now(), []
        due_at = cycle_schedule.next_due(cycle_schedule.defer_first_pass(scan_interval))
        logger.info(f"CYCLE_ON_STARTUP=false and no schedule on disk; first cycle due at {due_at.isoformat()}")
        return due_at, []

    def _supervise_companions(self):
        """Start a companion service again when it exits (the bot, for one,
        can lose its first login while the VPN is still coming up). One that
        keeps dying is retried less and less often, up to hourly."""
        spawners = {
            'discord_bot_process': ('Discord bot', self.spawn_discord_bot_process),
            'arl_server_process': ('ARL server', self.spawn_arl_server_process),
            'one_star_process': ('one-star watcher', self.spawn_one_star_watcher),
            'fix_metadata_process': ('Fix Metadata daemon', self.spawn_fix_metadata_daemon),
        }
        backoff = {attr: 60 for attr in spawners}
        next_try = {attr: 0.0 for attr in spawners}
        while True:
            time.sleep(60)
            for attr, (label, spawn) in spawners.items():
                proc = getattr(self, attr, None)
                if proc is None or proc.poll() is None or time.time() < next_try[attr]:
                    continue
                logger.warning(f"⚠️ {label} exited (code {proc.returncode}); starting it again")
                try:
                    setattr(self, attr, spawn() or proc)
                except Exception as e:
                    logger.error(f"❌ Could not restart the {label}: {e}")
                next_try[attr] = time.time() + backoff[attr]
                backoff[attr] = min(backoff[attr] * 2, 3600)

    def _wait_until(self, due_at):
        """Sleep in short slices so shutdown signals and mapping changes are
        noticed promptly during a multi-day wait."""
        while True:
            remaining = (due_at - cycle_schedule.now()).total_seconds()
            if remaining <= 0:
                return
            if int(remaining) % 3600 < 60 or remaining < 60:
                logger.info(f"⏳ Next cycle in {remaining / 3600:.1f}h")
            time.sleep(min(60, remaining))

    def run(self):
        logger.info("=" * 70)
        logger.info("🎵 MUSIC AI MULTI-USER MANAGER - STARTING")
        logger.info("=" * 70)
        
        if not self.validate_configuration():
            logger.error("❌ Configuration validation failed. Exiting.")
            return 1
        
        # Initialize singleton Downloader with VPN check
        logger.info("=" * 70)
        logger.info("🔧 Initializing Singleton Downloader...")
        logger.info("=" * 70)
        
        # Import downloader_manager
        try:
            from downloader_manager import initialize_downloader_singleton
        except ImportError as e:
            logger.error(f"❌ Could not import downloader_manager: {e}")
            return 1
        
        # Check if VPN is required
        require_vpn = os.environ.get('VPN_REQUIRED', 'true').lower() == 'true'
        logger.info(f"VPN requirement: {'REQUIRED' if require_vpn else 'optional'}")
        
        # Initialize downloader
        if not initialize_downloader_singleton(require_vpn=require_vpn):
            logger.error("❌ Downloader initialization failed. Exiting.")
            if require_vpn:
                logger.error("   VPN setup failed and VPN_REQUIRED=true")
                logger.error("   Fix VPN issues or set VPN_REQUIRED=false to continue")
            return 1
        
        logger.info("✅ Downloader singleton initialized successfully")

        # Run API / service configuration check so any missing or invalid keys
        # are surfaced immediately at startup rather than silently failing later.
        logger.info("=" * 70)
        logger.info("🔑 Checking API and service configuration...")
        logger.info("=" * 70)
        try:
            from downloader import Downloader, _can_import
            Downloader.check_api_configuration()
        except Exception as _api_chk_err:
            logger.warning(f"API configuration check failed (non-fatal): {_api_chk_err}")

        # Also run audio-ID health check if audio_id module is available
        try:
            from audio_id import audio_id_health_check
            audio_id_health_check()
        except ImportError:
            logger.debug("audio_id module not available — skipping audio-ID health check")
        except Exception as _aid_err:
            logger.warning(f"Audio-ID health check failed (non-fatal): {_aid_err}")
        
        # Discover and cache per-user Plex tokens from server logs
        logger.info("=" * 70)
        logger.info("🔐 Discovering per-user Plex tokens...")
        logger.info("=" * 70)
        if not self.discover_and_cache_plex_tokens():
            logger.warning("⚠️ Failed to discover Plex tokens; continuing startup without per-user tokens.")
            # Continue startup even if per-user Plex tokens couldn't be discovered.
            # Functionality may be limited (no per-user library access), but avoid
            # exiting the container to prevent restart loops.
        
        # Setup deemix ARL token
        logger.info("=" * 70)
        logger.info("🎵 Setting up deemix configuration...")
        logger.info("=" * 70)
        self.setup_deemix_arl()
        # Remove stale downloader lock file if present so new cycles can start
        lock_path = "/app/data/downloader.lock"
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
                logger.info(f"✓ Removed stale downloader lock: {lock_path}")
        except Exception as e:
            logger.warning(f"⚠ Failed to remove downloader lock {lock_path}: {e}")

        # Reset cleanup databases to ensure fresh cleanup-albums processing
        # This allows cleanup-albums to process ALL songs when run initially
        self.reset_cleanup_databases()

        # Spawn companion background services early so they can initialize while
        # Plex library initialization runs.
        logger.info("=" * 70)
        logger.info("🤖 Initializing companion background services...")
        logger.info("=" * 70)
        self.discord_bot_process = self.spawn_discord_bot_process()
        self.arl_server_process = self.spawn_arl_server_process()
        self.one_star_process = self.spawn_one_star_watcher()
        self.fix_metadata_process = self.spawn_fix_metadata_daemon()
        threading.Thread(target=self._supervise_companions, daemon=True).start()

        # Initialize Plex libraries for all users
        # This populates track scores in the database before app.py lifecycle starts
        # Ensures accurate data is available regardless of CYCLE_ON_STARTUP setting
        self.initialize_user_plex_libraries()

        logger.info(f"🚀 Starting sequential app.py cycles for {self.num_users} user(s)...")

        for i in range(self.num_users):
            if not self.create_user_directories(i, self.user_paths[i]):
                logger.error(f"❌ Failed to create directories for user '{self.user_names[i]}'. Exiting.")
                return 1

        logger.info("✅ Initial user directories are ready")
        logger.info("🔄 Entering parent sequential worker loop")

        scan_interval = int(os.environ.get('SCAN_INTERVAL', '604800'))
        logger.info(f"⏱️ Using SCAN_INTERVAL={scan_interval} seconds between full user cycles")

        def signal_handler(signum, frame):
            logger.warning(f"⚠️ Received signal {signum}. Initiating graceful shutdown...")
            sys.exit(0)

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        # The schedule is persisted so a container restart neither restarts
        # the countdown nor starts a fresh pass: an interrupted pass resumes
        # with the users that have not finished, and between passes the
        # manager just keeps waiting for the stored due time.
        due_at, resume_users = self._initial_schedule(scan_interval)

        while True:
            try:
                self._wait_until(due_at)

                if self.refresh_users_from_mapping():
                    logger.info("🔁 Wizarr mapping changed; refreshed worker plan")
                    for i in range(self.num_users):
                        self.create_user_directories(i, self.user_paths[i])

                plan = self.build_worker_plan()
                logger.info(f"🔁 Worker plan order: {[e['user_name'] for e in plan]} (admin={self.admin_plex_username})")

                if resume_users:
                    done = set(resume_users)
                    resume_users = []
                else:
                    cycle_schedule.begin_pass()
                    done = set()
                    # Make room before the downloads if the disk is already low.
                    self.run_library_review('start')

                restart_plan = False
                for entry in plan:
                    if entry['user_name'] in done:
                        logger.info(f"⏭️ '{entry['user_name']}' already finished this pass; skipping")
                        continue
                    return_code = self.run_user_cycle(entry['index'])
                    if return_code != 0:
                        logger.warning(f"⚠️ app.py cycle for '{entry['user_name']}' exited with code {return_code}")
                    else:
                        cycle_schedule.mark_user_done(entry['user_name'])

                    if entry['is_admin'] and self.refresh_users_from_mapping():
                        logger.info("🔁 Admin cycle detected a mapping change; restarting sequential loop")
                        restart_plan = True
                        break

                if restart_plan:
                    resume_users = cycle_schedule.users_done(cycle_schedule.load())
                    due_at = cycle_schedule.now()
                    continue

                # Every user's scores are fresh now: graduate, review probation, free space.
                self.run_library_review('end')
                due_at = cycle_schedule.next_due(cycle_schedule.finish_pass(scan_interval))
                logger.info(f"✅ Full pass complete; next cycle due at {due_at.isoformat()}")
            except KeyboardInterrupt:
                logger.info("⌨️ Keyboard interrupt received")
                return 0
            except Exception as e:
                logger.error(f"Unexpected error in parent loop: {e}", exc_info=True)
                time.sleep(10)
        
    
    def cleanup(self):
        logger.info("🧹 Cleaning up processes...")

        # Clean up Discord bot process
        if self.discord_bot_process and self.discord_bot_process.poll() is None:
            try:
                logger.info(f"  🤖 Terminating Discord Bot process (PID: {self.discord_bot_process.pid})...")
                os.killpg(os.getpgid(self.discord_bot_process.pid), signal.SIGTERM)
                self.discord_bot_process.wait(timeout=5)
                logger.info(f"  ✓ Discord Bot process terminated")
            except subprocess.TimeoutExpired:
                logger.warning(f"  ⚠️ Discord Bot process did not terminate, killing...")
                try:
                    os.killpg(os.getpgid(self.discord_bot_process.pid), signal.SIGKILL)
                except:
                    pass
            except Exception as e:
                logger.error(f"  ❌ Error terminating Discord Bot process: {e}")

        # Clean up ARL server process
        if self.arl_server_process and self.arl_server_process.poll() is None:
            try:
                logger.info(f"  🌐 Terminating ARL server process (PID: {self.arl_server_process.pid})...")
                os.killpg(os.getpgid(self.arl_server_process.pid), signal.SIGTERM)
                self.arl_server_process.wait(timeout=5)
                logger.info("  ✓ ARL server process terminated")
            except subprocess.TimeoutExpired:
                logger.warning("  ⚠️ ARL server did not terminate, killing...")
                try:
                    os.killpg(os.getpgid(self.arl_server_process.pid), signal.SIGKILL)
                except:
                    pass
            except Exception as e:
                logger.error(f"  ❌ Error terminating ARL server process: {e}")

        if self.one_star_process and self.one_star_process.poll() is None:
            try:
                logger.info(f"  ⭐ Terminating one-star watcher process (PID: {self.one_star_process.pid})...")
                os.killpg(os.getpgid(self.one_star_process.pid), signal.SIGTERM)
                self.one_star_process.wait(timeout=5)
                logger.info("  ✓ one-star watcher process terminated")
            except subprocess.TimeoutExpired:
                logger.warning("  ⚠️ one-star watcher did not terminate, killing...")
                try:
                    os.killpg(os.getpgid(self.one_star_process.pid), signal.SIGKILL)
                except:
                    pass
            except Exception as e:
                logger.error(f"  ❌ Error terminating one-star watcher process: {e}")

        if self.fix_metadata_process and self.fix_metadata_process.poll() is None:
            try:
                logger.info(f"  🧭 Terminating Fix Metadata daemon (PID: {self.fix_metadata_process.pid})...")
                os.killpg(os.getpgid(self.fix_metadata_process.pid), signal.SIGTERM)
                self.fix_metadata_process.wait(timeout=5)
                logger.info("  ✓ Fix Metadata daemon terminated")
            except subprocess.TimeoutExpired:
                logger.warning("  ⚠️ Fix Metadata daemon did not terminate, killing...")
                try:
                    os.killpg(os.getpgid(self.fix_metadata_process.pid), signal.SIGKILL)
                except:
                    pass
            except Exception as e:
                logger.error(f"  ❌ Error terminating Fix Metadata daemon: {e}")

        logger.info("✅ Cleanup complete")

if __name__ == "__main__":
    sys.exit(MultiUserManager().run())
