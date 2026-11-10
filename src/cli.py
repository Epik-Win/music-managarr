#!/usr/bin/env python3
"""
MusicAI CLI - maintenance commands:

- download-exportify: download the songs of Spotify Exportify CSVs
- cleanup-albums: tidy the library (album_artist_merger.py, every pass)
- clear-db: reset a user's database tables
"""

import os
import sys
import logging
import csv
import sqlite3
from typing import Dict, List, Optional, Any
import json
import re

from downloader import Downloader

# Try to import Plex library
try:
    from plexapi.server import PlexServer
    PLEX_AVAILABLE = True
except ImportError:
    PLEX_AVAILABLE = False
    PlexServer = None

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Environment configuration
APP_USER_NAME = os.environ.get("APP_USER_NAME", "default")
DATA_DIR = os.environ.get('DATA_DIR', './musicai/data')
MUSIC_BASE_DIR = os.environ.get('MUSIC_BASE_DIR', '/music')
ALBUM_DIR = os.environ.get('ALBUM_DIR', os.path.join(MUSIC_BASE_DIR, 'albums'))
TEMP_DOWNLOAD_DIR = os.environ.get('TEMP_DOWNLOAD_DIR', '/tmp/music_downloads')

# Plex configuration
PLEX_URL = os.environ.get('PLEX_URL', 'http://localhost:32400')
PLEX_TOKEN = os.environ.get('PLEX_TOKEN', '')
PLEX_MUSIC_SECTION = os.environ.get('PLEX_MUSIC_SECTION', 'Music')


# Supported audio extensions
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wav'}

# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_database_path(user: str = None) -> str:
    """Get the database path for a specific user.

    Reads ``DATA_DIR`` from the environment each time so callers (and unit
    tests) can modify the value via ``os.environ`` without having to mutate the
    module-level constant directly.
    """
    # prefer the environment variable to allow runtime overrides
    data_dir = os.environ.get('DATA_DIR', DATA_DIR)
    if user is None:
        user = APP_USER_NAME
    user_data_dir = os.path.join(data_dir, user)
    os.makedirs(user_data_dir, exist_ok=True)
    return os.path.join(user_data_dir, 'music_ai.db')


# -----------------------------------------------------------------------------
# Database helpers for cleanup-albums tracking
# -----------------------------------------------------------------------------

def ensure_cleanup_table(db_path: str) -> None:
    """Ensure the cleanup_status table exists in the given database.

    Columns:
      metadata_done   — metadata tags have been verified/corrected
      video_checked   — music-video audio check has been run
      deemix_clean    — song was downloaded by deemix; tags/metadata are
                        already high-quality from Deezer, skip re-processing
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cleanup_status (
                track_path TEXT PRIMARY KEY,
                metadata_done BOOLEAN DEFAULT 0,
                video_checked BOOLEAN DEFAULT 0,
                deemix_clean BOOLEAN DEFAULT 0,
                last_run TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Add columns that older database files lack
        existing = {row[1] for row in cursor.execute("PRAGMA table_info(cleanup_status)")}
        for col, defval in [('deemix_clean', '0')]:
            if col not in existing:
                col_type = 'BOOLEAN'
                cursor.execute(f"ALTER TABLE cleanup_status ADD COLUMN {col} {col_type} DEFAULT {defval}")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Could not ensure cleanup_status table: {e}")
    finally:
        try:
            conn.close()
        except:
            pass




def update_cleanup_record(
    db_path: str,
    track_path: str,
    metadata_done: Optional[bool] = None,
    video_checked: Optional[bool] = None,
    symlink_done: Optional[bool] = None,
    deemix_clean: Optional[bool] = None,
    symlink_targets: Optional[list] = None,
) -> None:
    """Create or update a cleanup_status record for the given track.

    Only fields that are not ``None`` will be modified. ``None`` leaves the
    existing value untouched (or treated as False on insert).
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # make sure table exists before touching it
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cleanup_status (
                track_path TEXT PRIMARY KEY,
                metadata_done BOOLEAN DEFAULT 0,
                video_checked BOOLEAN DEFAULT 0,
                symlink_done BOOLEAN DEFAULT 0,
                last_run TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # see if record exists
        cursor.execute("SELECT track_path FROM cleanup_status WHERE track_path = ?", (track_path,))
        exists = cursor.fetchone()

        if exists:
            updates = []
            params: List[Any] = []
            if metadata_done is not None:
                updates.append("metadata_done = ?")
                params.append(1 if metadata_done else 0)
            if video_checked is not None:
                updates.append("video_checked = ?")
                params.append(1 if video_checked else 0)
            if symlink_done is not None:
                updates.append("symlink_done = ?")
                params.append(1 if symlink_done else 0)
            if deemix_clean is not None:
                updates.append("deemix_clean = ?")
                params.append(1 if deemix_clean else 0)
            if symlink_targets is not None:
                updates.append("symlink_targets = ?")
                params.append(json.dumps(symlink_targets))
            if updates:
                updates.append("last_run = CURRENT_TIMESTAMP")
                cursor.execute(
                    f"UPDATE cleanup_status SET {', '.join(updates)} WHERE track_path = ?",
                    (*params, track_path),
                )
        else:
            # Insert default False for None values
            cursor.execute(
                "INSERT INTO cleanup_status "
                "(track_path, metadata_done, video_checked, symlink_done, deemix_clean, symlink_targets) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    track_path,
                    1 if metadata_done else 0,
                    1 if video_checked else 0,
                    1 if symlink_done else 0,
                    1 if deemix_clean else 0,
                    json.dumps(symlink_targets or []),
                ),
            )
        conn.commit()
    except Exception as e:
        logger.debug(f"Error updating cleanup record: {e}")
    finally:
        try:
            conn.close()
        except:
            pass


def sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename or directory name."""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, '_')
    name = name.strip('. ')
    if len(name) > 200:
        name = name[:200]
    return name








def format_song_title_with_features(title: str, artist: str = None) -> str:
    """
    Format song title to have clean structure with featured artists.
    
    Examples:
        "Song (feat. Artist1, Artist2)" -> "Song (feat. Artist1, Artist2)"
        "Song featuring Artist1 & Artist2" -> "Song (feat. Artist1, Artist2)"
        "Song ft Artist1 and Artist2" -> "Song (feat. Artist1, Artist2)"
        "Song with Artist1" -> "Song (feat. Artist1)"
    
    Args:
        title: The song title to format
        artist: Optional primary artist name to exclude from features
    
    Returns:
        Formatted song title with standardized feature notation
    """
    # Extract the base title and any featured artists
    base_title = title
    featured_artists = []
    
    # Pattern to match various featuring formats
    # Matches: (feat. X), [feat. X], feat. X, featuring X, ft. X, ft X, with X
    feat_patterns = [
        r'[\(\[]?\s*\b(feat\.?|featuring|ft\.?)\s+([^\)\]]+?)[\)\]]?$',  # End of string with optional brackets (whole word: not the "ft" in "Thrift Shop")
        r'\s+(?:feat\.?|featuring|ft\.?|with)\s+(.+?)(?:\s*[\(\[]|$)',  # Middle or end without brackets
    ]
    
    for pattern in feat_patterns:
        match = re.search(pattern, title, flags=re.IGNORECASE)
        if match:
            # Get the featured artists text (last group in match)
            feat_text = match.group(match.lastindex)
            
            # Remove this from the base title
            base_title = title[:match.start()].strip()
            
            # Parse the featured artists
            # Split by common separators: comma, &, and, /
            feat_text = re.sub(r'\s+and\s+', ', ', feat_text, flags=re.IGNORECASE)
            feat_text = re.sub(r'\s*&\s*', ', ', feat_text)
            feat_text = re.sub(r'\s*/\s*', ', ', feat_text)
            
            # Split by comma and clean up
            artists_list = [a.strip() for a in feat_text.split(',') if a.strip()]
            
            # Filter out the primary artist if provided
            if artist:
                primary_normalized = artist.lower().strip()
                artists_list = [a for a in artists_list if a.lower().strip() != primary_normalized]
            
            featured_artists.extend(artists_list)
            break
    
    # Remove any remaining video/audio suffixes from base title
    base_title = re.sub(r'\s*[\[\(].*?(Video|Audio|Official|Lyrics|Remix|Cover).*?[\]\)]', '', base_title, flags=re.IGNORECASE).strip()
    
    # Rebuild the title with standardized format
    if featured_artists:
        # Remove duplicates while preserving order
        seen = set()
        unique_artists = []
        for a in featured_artists:
            a_lower = a.lower()
            if a_lower not in seen:
                seen.add(a_lower)
                unique_artists.append(a)
        
        # Format as: "Song Title (feat. Artist1, Artist2)"
        feat_string = ', '.join(unique_artists)
        formatted_title = f"{base_title} (feat. {feat_string})"
    else:
        formatted_title = base_title
    
    return sanitize_filename(formatted_title)




















# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: download-exportify
# ═══════════════════════════════════════════════════════════════════════════════

def download_from_exportify_csv(csv_file: str, downloader: Downloader, target_dir: str = None) -> Dict[str, Any]:
    """Download tracks from a Spotify Exportify CSV file."""
    if not os.path.exists(csv_file):
        logger.error(f"CSV file not found: {csv_file}")
        return {'success': False, 'error': 'File not found'}
    
    if target_dir is None:
        target_dir = ALBUM_DIR
    
    stats = {
        'total': 0,
        'successful': 0,
        'failed': 0,
        'skipped': 0,
        'errors': []
    }
    
    logger.info("=" * 60)
    logger.info(f"📥 Downloading from Exportify CSV: {csv_file}")
    logger.info(f"🎯 Target directory: {target_dir}")
    logger.info("=" * 60)
    
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                stats['total'] += 1
                
                try:
                    track_name = row.get('Track Name', '').strip()
                    artist_name = row.get('Artist Name(s)', '').strip()
                    album_name = row.get('Album Name', '').strip()
                    
                    if not track_name or not artist_name:
                        logger.warning(f"Skipping row {stats['total']}: missing track or artist name")
                        stats['skipped'] += 1
                        continue
                    
                    if not album_name:
                        album_name = track_name
                    
                    logger.info(f"\n[{stats['total']}] Downloading: {artist_name} - {track_name}")
                    
                    result = downloader.download_track(
                        artist=artist_name,
                        title=track_name,
                        album=album_name,
                        target_dir=target_dir
                    )
                    
                    if result.get('success'):
                        stats['successful'] += 1
                        logger.info(f"  ✓ Downloaded: {result.get('file_path')}")
                    else:
                        stats['failed'] += 1
                        error = result.get('error', 'Unknown error')
                        logger.warning(f"  ✗ Failed: {error}")
                        stats['errors'].append({
                            'track': f"{artist_name} - {track_name}",
                            'error': error
                        })
                
                except Exception as e:
                    stats['failed'] += 1
                    logger.error(f"  ✗ Error processing row {stats['total']}: {e}")
                    stats['errors'].append({
                        'track': f"Row {stats['total']}",
                        'error': str(e)
                    })
        
        logger.info("\n" + "=" * 60)
        logger.info("Download Summary:")
        logger.info(f"  Total tracks: {stats['total']}")
        logger.info(f"  Successful: {stats['successful']}")
        logger.info(f"  Failed: {stats['failed']}")
        logger.info(f"  Skipped: {stats['skipped']}")
        logger.info("=" * 60)
        
        if stats['errors']:
            logger.info("\nFailed Downloads:")
            for error in stats['errors'][:10]:
                logger.info(f"  ✗ {error['track']}: {error['error']}")
            if len(stats['errors']) > 10:
                logger.info(f"  ... and {len(stats['errors']) - 10} more errors")
        
        stats['success'] = True
        return stats
    
    except Exception as e:
        logger.error(f"Error reading CSV file: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


def cmd_download_exportify(args: List[str]) -> int:
    """Command handler for download-exportify."""
    csv_file = None
    target_dir = ALBUM_DIR
    
    for i, arg in enumerate(args):
        if arg.endswith('.csv'):
            csv_file = arg
        elif i > 0 and not args[i-1].endswith('.csv'):
            target_dir = arg
    
    if csv_file is None:
        current_dir = os.getcwd()
        csv_files = [f for f in os.listdir(current_dir) if f.endswith('.csv')]
        if csv_files:
            csv_file = csv_files[0]
            logger.info(f"Auto-detected CSV file: {csv_file}")
        else:
            logger.error("No CSV file specified and none found in current directory")
            return 1
    
    try:
        downloader = Downloader(analyzer=None)
    except Exception as e:
        logger.error(f"Error initializing downloader: {e}")
        return 1
    
    stats = download_from_exportify_csv(csv_file, downloader, target_dir)
    
    return 0 if stats.get('success') else 1


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: cleanup-albums
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_cleanup_albums(args: List[str]) -> int:
    """
    Tidy the library: every pass of album_artist_merger.py (what the Discord
    /clean_library command runs).

    Usage: python cli.py cleanup-albums [--dry-run]

        --dry-run   Only log what would change.
    """
    from album_artist_merger import build_merger_from_env
    if '--dry-run' in args:
        os.environ['MERGER_DRY_RUN'] = '1'
    plex = None
    if PLEX_AVAILABLE and PLEX_TOKEN:
        try:
            plex = PlexServer(PLEX_URL, PLEX_TOKEN)
        except Exception as e:
            logger.warning(f"Could not connect to Plex ({e}); Plex's split albums are not joined this time")
    report = build_merger_from_env(plex=plex).run_full_merge()
    print(report.summary())
    return 0 if not report.errors else 1


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: clear-db
# ═══════════════════════════════════════════════════════════════════════════════

def infer_user_from_path(path: str) -> Optional[str]:
    """Infer the user name from a music library path.

    Given a path such as ``/music/users/alice/albums/...`` this will return
    ``'alice'``.  If the path doesn't follow the expected ``/music/users/<user>``
    pattern, ``None`` is returned.
    """
    if not path:
        return None
    normalized = os.path.normpath(path)
    parts = normalized.split(os.sep)
    for i, part in enumerate(parts):
        if part == 'users' and i + 1 < len(parts):
            return parts[i + 1]
    return None




def clear_database(
    force: bool = False,
    song_path: Optional[str] = None,
    db_path: Optional[str] = None,
    drop_db: bool = False,
) -> int:
    """Clear database tables or reset a specific song entry.

    If ``song_path`` is provided, only the corresponding row in
    ``cleanup_status`` will be deleted. Otherwise the standard tables
    (track_scores, recommendations, temp_downloads, cleanup_status)
    are cleared completely.

    ``db_path`` may be supplied for testing; if omitted the current
    user's database is used.
    """
    if db_path is None:
        # if a song_path is provided and it appears to live under
        # /music/users/<user>/..., select that user's database rather than the
        # current APP_USER_NAME.  this mirrors how the rest of the system
        # segregates per-user libraries.
        if song_path:
            inferred = infer_user_from_path(song_path)
            if inferred:
                db_path = get_database_path(user=inferred)
            else:
                db_path = get_database_path()
        else:
            db_path = get_database_path()
    
    # if the database file doesn't exist we normally don't want this to be
    # a hard error.  an empty library means there's nothing to clear and
    # callers shouldn't have to create a dummy file just to satisfy the
    # command.  we treat all cases as success, logging informational messages
    # when appropriate.  the only behaviour that differs from a normal
    # ``drop_db`` request is that we simply report and return rather than
    # delete.
    if not os.path.exists(db_path):
        if drop_db:
            logger.info(f"🗑️  Database file not present, nothing to drop: {db_path}")
            return 0
        if song_path:
            logger.info(
                f"ℹ️  Database not found ({db_path}); nothing to remove for song/directory {song_path}"
            )
            return 0
        logger.info(f"ℹ️  Database not found ({db_path}); nothing to clear")
        return 0
    # if drop_db requested, delete file entirely
    if drop_db:
        logger.info(f"🗑️  Dropping database file: {db_path}")
        try:
            os.remove(db_path)
            logger.info("✅ Database file removed")
            return 0
        except Exception as e:
            logger.error(f"Error deleting database file: {e}")
            return 1
    
    logger.info("=" * 60)
    logger.info(f"🗑️  Database Clear")
    logger.info(f"📁 Database: {db_path}")
    logger.info("=" * 60)
    
    # If we're only clearing a single song or directory, confirm if not forced
    if song_path:
        ensure_cleanup_table(db_path)
        # determine target file list
        paths_to_delete = []
        if os.path.isdir(song_path):
            # walk recursively and collect audio files
            for root, dirs, files in os.walk(song_path):
                for f in files:
                    if any(f.endswith(ext) for ext in AUDIO_EXTENSIONS):
                        paths_to_delete.append(os.path.join(root, f))
        else:
            paths_to_delete.append(song_path)

        if not force:
            if len(paths_to_delete) == 1:
                print(f"\nThis will remove cleanup tracking for song:\n  {paths_to_delete[0]}\n")
            else:
                print(f"\nThis will remove cleanup tracking for {len(paths_to_delete)} songs under directory:\n  {song_path}\n")
            print("Are you sure you want to continue? (yes/no): ", end='')
            response = input().strip().lower()
            if response not in ('yes', 'y'):
                logger.info("Operation cancelled by user")
                return 0
    else:
        # Confirm action for full database wipe
        if not force:
            print("\nThis will delete the following tables:")
            print("  - track_scores")
            print("  - recommendations")
            print("  - temp_downloads")
            print("  - cleanup_status")
            print("\nAre you sure you want to continue? (yes/no): ", end='')
            response = input().strip().lower()
            if response not in ('yes', 'y'):
                logger.info("Operation cancelled by user")
                return 0
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor = conn.cursor()
        # if clearing a specific song or directory, delete each path collected earlier
        if song_path:
            deleted_total = 0
            for sp in paths_to_delete:
                cursor.execute('''
                    DELETE FROM cleanup_status WHERE track_path = ?
                ''', (sp,))
                deleted_total += cursor.rowcount
            conn.commit()
            conn.close()
            if deleted_total:
                logger.info(f"✅ Removed cleanup record(s) for {deleted_total} song(s)")
            else:
                logger.info(f"ℹ️  No cleanup records found for provided path(s)")
            return 0

        # otherwise perform full wipe
        # Get table names
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        
        # Clear tables
        tables_to_clear = ['track_scores', 'recommendations', 'temp_downloads', 'cleanup_status']
        cleared = 0
        
        for table in tables_to_clear:
            if table in tables:
                try:
                    cursor.execute(f"DELETE FROM {table}")
                    rows_deleted = cursor.rowcount
                    logger.info(f"  ✓ Cleared {table}: {rows_deleted} rows deleted")
                    cleared += 1
                except Exception as e:
                    logger.error(f"  ✗ Failed to clear {table}: {e}")
        
        conn.commit()
        conn.close()
        
        logger.info("=" * 60)
        logger.info(f"✅ Database cleared: {cleared}/{len(tables_to_clear)} tables")
        logger.info("=" * 60)
        
        return 0
    
    except Exception as e:
        logger.error(f"Error clearing database: {e}", exc_info=True)
        return 1


def cmd_clear_db(args: List[str]) -> int:
    """Command handler for clear-db."""
    force = '--force' in args
    song_path = None
    drop_db = '--drop' in args
    if '--song' in args:
        idx = args.index('--song')
        if idx + 1 < len(args):
            song_path = args[idx + 1]
    return clear_database(force, song_path, drop_db=drop_db)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def print_usage():
    """Print CLI usage information."""
    print("MusicAI CLI - Command Line Interface")
    print("\nUsage: python cli.py [command] [options]")
    print("\nCommands:")
    print("  download-exportify [csv_file] [target_dir]")
    print("    Download tracks from Spotify Exportify CSV")
    print("    - csv_file: Path to exportify CSV (optional, auto-detected)")
    print("    - target_dir: Target directory (optional, defaults to ALBUM_DIR)")
    print()
    print("  cleanup-albums [--dry-run]")
    print("    Tidy the library: one folder per artist and album (Lidarr's names win), loose")
    print("    files filed, albums matched to Lidarr's, duplicate songs quarantined, Plex's")
    print("    split albums joined. --dry-run only logs what would change.")
    print()
    print("  clear-db [--force] [--song <path>] [--drop]")
    print("    Clear database tables (track_scores, recommendations, temp_downloads)")
    print("    - --force: Skip confirmation prompt")
    print("    - --song <path>: remove tracking for a specific file or all songs under a")
    print("      directory (recursive)")
    print("    - --drop: delete the database file entirely")
    print()
    print("Examples:")
    print("  python cli.py download-exportify playlist.csv")
    print("  python cli.py cleanup-albums --dry-run")
    print("  python cli.py clear-db --force")
    print("  python cli.py clear-db --song /music/users/john/albums/Artist/Album/song.mp3")  # reset single track record
    print("  python cli.py clear-db --song /music/users/john/albums/Artist/Album/" )  # reset all songs under directory
    print("  python cli.py clear-db --drop --force")  # delete entire database file

    print("  # shorthand: you can omit the command and use clear-db flags directly")
    print("  python cli.py --song /music/users/john/albums/Artist/Album/")


def main():
    """Main CLI entry point."""
    if len(sys.argv) < 2:
        print_usage()
        return 1
    
    command = sys.argv[1]
    args = sys.argv[2:]
    
    # Allow bare flags to default to the clear-db command for convenience.
    # This mirrors the behaviour of some CLI tools where dropping the command
    # and using clear-db-specific options is a shorthand, e.g.
    #   python cli.py --song /path
    # is equivalent to
    #   python cli.py clear-db --song /path
    if command.startswith('--') and any(command in opt for opt in ('--song', '--force', '--drop')):
        return cmd_clear_db([command] + args)

    # Route to command handlers
    if command == 'download-exportify':
        return cmd_download_exportify(args)
    
    elif command == 'cleanup-albums':
        return cmd_cleanup_albums(args)

    elif command == 'clear-db':
        return cmd_clear_db(args)
    
    else:
        logger.error(f"Unknown command: {command}")
        print_usage()
        return 1


if __name__ == '__main__':
    import metadata_proxy
    metadata_proxy.install()          # lookups outside the VPN, downloads in it
    sys.exit(main())
