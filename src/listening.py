"""The listening tracker: how much of each song every listener hears.

Plex keeps a play count, a skip count and a history of finished plays, but
not how much of a song someone listened to before moving on. This process
watches Plex's sessions (every LISTEN_POLL_SECONDS, through the server
owner's connection, which sees every listener) and writes one row per play
to `DATA_DIR/listening.db`:

- who listened (their Plex account), on which player and device, and when
  (the hour and weekday are kept for later use);
- how far into the song they got, how long they actually listened (seeking
  ahead does not count), and the song's length;
- how it ended: **finished** (to the last tenth or the last 15 seconds),
  **skipped** (left in its first 30 seconds or first 30 %), or **partial**.

A song played again straight after it ended is a second play. A play still
going when the tracker stops is not recorded: it would look like a song left
halfway. The taste profile (taste.py) reads these rows for every user.

    python3 listening.py            # runs until stopped
"""
import logging
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger('musicai.listening')


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


POLL_SECONDS = max(2.0, _env_float('LISTEN_POLL_SECONDS', 5))
FINISHED_SHARE, FINISHED_REST = 0.9, 15.0      # heard to the last tenth or the last 15 s
SKIP_SECONDS, SKIP_SHARE = 30.0, 0.3           # left within 30 s or the first 30 %
DATA_DIR = os.environ.get('DATA_DIR', '/app/data')

FINISHED, PARTIAL, SKIPPED = 'finished', 'partial', 'skipped'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS listens (
    id INTEGER PRIMARY KEY,
    account INTEGER,           -- Plex account id of the listener
    user TEXT,                 -- their Plex name
    track_key TEXT,            -- Plex ratingKey
    artist TEXT, title TEXT, album TEXT,
    player TEXT, device TEXT,
    started REAL, ended REAL,  -- epoch seconds
    duration REAL,             -- song length, seconds
    reached REAL,              -- furthest point reached, seconds
    listened REAL,             -- seconds actually played
    fraction REAL,             -- share of the song heard, 0 … 1
    outcome TEXT,              -- finished / partial / skipped
    hour INTEGER, weekday INTEGER
);
CREATE INDEX IF NOT EXISTS listens_by_song ON listens (account, track_key);
'''


def db_path() -> str:
    return os.path.join(DATA_DIR, 'listening.db')


@dataclass
class Play:
    """One play, while it lasts."""
    account: int
    user: str
    key: str
    artist: str
    title: str
    album: str
    player: str
    device: str
    duration: float
    started: float
    updated: float
    offset: float
    reached: float
    listened: float = 0.0
    state: str = 'playing'

    def heard(self) -> float:
        return max(self.reached, self.listened)

    def outcome(self) -> str:
        d, heard = self.duration, self.heard()
        if d > 0 and heard >= min(d * FINISHED_SHARE, d - FINISHED_REST):
            return FINISHED
        if heard < (min(SKIP_SECONDS, d * SKIP_SHARE) if d > 0 else SKIP_SECONDS):
            return SKIPPED
        return PARTIAL

    def fraction(self) -> float:
        return round(min(1.0, self.heard() / self.duration), 3) if self.duration > 0 else 0.0


class Tracker:
    """Follows every listener's sessions from one look to the next. update()
    takes what Plex shows now and returns the plays that have ended."""

    def __init__(self):
        self.open: Dict[Tuple[int, str], Play] = {}

    def update(self, sessions: Sequence[dict], now: float) -> List[Play]:
        ended: List[Play] = []
        seen = set()
        for s in sessions:
            slot = (s['account'], s['player'])
            seen.add(slot)
            play = self.open.get(slot)
            again = (play is not None and play.key == s['key'] and play.outcome() == FINISHED
                     and s['offset'] < play.offset - 10)
            if play is not None and (play.key != s['key'] or again):
                ended.append(play)
                play = None
            if play is None:
                self.open[slot] = Play(
                    account=s['account'], user=s['user'], key=s['key'], artist=s['artist'], title=s['title'],
                    album=s['album'], player=s['player'], device=s['device'], duration=s['duration'],
                    started=now, updated=now, offset=s['offset'], reached=s['offset'], state=s['state'])
                continue
            step = s['offset'] - play.offset
            # Played on, not sought ahead: what moved fits in the time between
            # looks (a paused song doesn't move).
            if 0 < step <= (now - play.updated) * 1.5 + 2:
                play.listened += step
            play.offset, play.updated, play.state = s['offset'], now, s['state']
            play.reached = max(play.reached, s['offset'])
            play.duration = s['duration'] or play.duration
        for slot in [slot for slot in self.open if slot not in seen]:
            ended.append(self.open.pop(slot))
        return ended


# -- Plex -----------------------------------------------------------------------

def _account_of(item) -> Tuple[int, str]:
    """(account id, name) of a session's listener: plexapi keeps the session's
    <User id title> as _userId / _username (the server owner is 1)."""
    names = getattr(item, 'usernames', None) or []
    name = str(getattr(item, '_username', None) or (names[0] if names else '') or '')
    try:
        account = int(getattr(item, '_userId', None) or 0)
    except (TypeError, ValueError):
        account = 0
    return account, name


def sessions(server, section_key: Optional[str]) -> List[dict]:
    """The songs being played now, one dict per listener and player."""
    out = []
    for item in server.sessions():
        if getattr(item, 'type', '') != 'track':
            continue
        if section_key and str(getattr(item, 'librarySectionID', '') or '') not in ('', str(section_key)):
            continue
        account, user = _account_of(item)
        player = getattr(item, 'player', None)
        out.append({
            'account': account, 'user': user, 'key': str(item.ratingKey),
            'artist': getattr(item, 'originalTitle', None) or getattr(item, 'grandparentTitle', None) or '',
            'title': item.title or '', 'album': getattr(item, 'parentTitle', None) or '',
            'player': str(getattr(player, 'machineIdentifier', '') or getattr(player, 'title', '') or ''),
            'device': ' / '.join(v for v in (getattr(player, 'product', None), getattr(player, 'device', None),
                                             getattr(player, 'title', None)) if v),
            'duration': (getattr(item, 'duration', 0) or 0) / 1000.0,
            'offset': (getattr(item, 'viewOffset', 0) or 0) / 1000.0,
            'state': str(getattr(player, 'state', '') or 'playing'),
        })
    return out


# -- storing and reading ----------------------------------------------------------

def connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path(), timeout=30)
    conn.executescript(SCHEMA)
    return conn


def save(plays: Iterable[Play], path: Optional[str] = None) -> int:
    rows = []
    for p in plays:
        when = datetime.fromtimestamp(p.started)
        rows.append((p.account, p.user, p.key, p.artist, p.title, p.album, p.player, p.device, p.started,
                     p.updated, p.duration, p.reached, round(p.listened, 1), p.fraction(), p.outcome(),
                     when.hour, when.weekday()))
    if not rows:
        return 0
    conn = connect(path)
    with conn:
        conn.executemany('INSERT INTO listens (account, user, track_key, artist, title, album, player, device, '
                         'started, ended, duration, reached, listened, fraction, outcome, hour, weekday) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', rows)
    conn.close()
    return len(rows)


@dataclass
class Heard:
    """What one listener's tracked plays of one song came to."""
    finished: int = 0
    partial: int = 0
    skipped: int = 0
    fractions: List[float] = field(default_factory=list)
    last: float = 0.0
    artist: str = ''
    title: str = ''

    @property
    def plays(self) -> int:
        return self.finished + self.partial + self.skipped

    @property
    def share(self) -> float:
        """The share of the song they usually hear, 0 … 1."""
        return sum(self.fractions) / len(self.fractions) if self.fractions else 0.0


def heard_by(accounts: Iterable[int], path: Optional[str] = None, since: float = 0.0,
             names: Iterable[str] = ()) -> Dict[str, Heard]:
    """{Plex key: Heard} of the tracked plays since `since` of these accounts
    (or, for plays whose account Plex didn't give, these Plex names)."""
    accounts = sorted({int(a) for a in accounts})
    names = sorted({n.casefold() for n in names if n})
    if not (accounts or names) or not os.path.exists(path or db_path()):
        return {}
    out: Dict[str, Heard] = {}
    conn = connect(path)
    marks = ','.join('?' * len(accounts)) or 'NULL'
    name_marks = ','.join('?' * len(names)) or 'NULL'
    for key, artist, title, outcome, fraction, started in conn.execute(
            f'SELECT track_key, artist, title, outcome, fraction, started FROM listens '
            f'WHERE (account IN ({marks}) OR (account = 0 AND lower(user) IN ({name_marks}))) AND started >= ?',
            (*accounts, *names, since)):
        h = out.setdefault(str(key), Heard(artist=artist or '', title=title or ''))
        if outcome == FINISHED:
            h.finished += 1
        elif outcome == SKIPPED:
            h.skipped += 1
        else:
            h.partial += 1
        h.fractions.append(float(fraction or 0.0))
        h.last = max(h.last, float(started or 0.0))
    conn.close()
    return out


# -- the process ------------------------------------------------------------------

def _music_section_key(server) -> Optional[str]:
    name = (os.environ.get('PLEX_MUSIC_SECTION') or os.environ.get('PLEX_MUSIC_SECTIONS') or 'Music')
    name = name.split(',')[0].split(':')[0].strip()
    for section in server.library.sections():
        if section.type == 'artist' and (section.title == name or section.title.lower() == 'music'):
            return str(section.key)
    return None


def main() -> int:
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s - [%(levelname)s] - [listening] - %(message)s')
    import plex_auth
    from plexapi.server import PlexServer
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    tracker, server, section_key = Tracker(), None, None
    logger.info(f"🎧 Listening tracker: looking at Plex's sessions every {POLL_SECONDS:g} s → {db_path()}")
    while not stopping:
        try:
            if server is None:
                server = PlexServer(os.environ['PLEX_URL'], plex_auth.admin_token(), timeout=30)
                section_key = _music_section_key(server)
            ended = tracker.update(sessions(server, section_key), time.time())
            for play in ended:
                logger.info(f"🎧 {play.user or play.account}: {play.artist} – {play.title} "
                            f"{play.outcome()} ({play.fraction():.0%}, {play.device})")
            save(ended)
        except Exception as exc:
            # Plex unreachable: plays in progress stay open until it answers again.
            logger.debug(f"Plex sessions not read: {exc}")
            server = None
        time.sleep(POLL_SECONDS)
    return 0


if __name__ == '__main__':
    sys.exit(main())
