"""Persisted schedule for the full multi-user download cycle.

The timer is kept on disk rather than in memory so a container restart
(deploy, network change, crash) neither restarts the countdown nor, with
CYCLE_ON_STARTUP=true, starts a whole new pass. It lives in
`DATA_DIR/cycle_schedule.json`:

    {
      "next_due":  "<iso>",   # when the next full pass may start
      "started":   "<iso>",   # start of the pass in progress (or last pass)
      "completed": "<iso>",   # end of the last full pass; null while running
      "users_done": [...]     # users finished in the pass in progress
    }

A restart mid-pass resumes with the users that have not finished; a restart
between passes just keeps waiting for `next_due`.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger('musicai.app')


def schedule_path() -> str:
    return os.path.join(os.environ.get('DATA_DIR', '/app/data'), 'cycle_schedule.json')


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load() -> Dict:
    try:
        with open(schedule_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning(f"Could not read cycle schedule {schedule_path()}: {exc}")
        return {}


def save(data: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(schedule_path()), exist_ok=True)
        tmp = schedule_path() + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, schedule_path())
    except Exception as exc:
        logger.warning(f"Could not write cycle schedule {schedule_path()}: {exc}")


def now() -> datetime:
    return datetime.now(timezone.utc)


def next_due(data: Optional[Dict] = None) -> Optional[datetime]:
    return _parse((data if data is not None else load()).get('next_due'))


def pass_in_progress(data: Dict) -> bool:
    """True when a pass started and never recorded its completion."""
    started, completed = _parse(data.get('started')), _parse(data.get('completed'))
    return started is not None and (completed is None or completed < started)


def users_done(data: Dict) -> List[str]:
    done = data.get('users_done')
    return [str(u) for u in done] if isinstance(done, list) else []


def begin_pass() -> Dict:
    data = load()
    data.update({'started': now().isoformat(), 'completed': None, 'users_done': []})
    save(data)
    return data


def mark_user_done(user: str) -> Dict:
    data = load()
    done = users_done(data)
    if user not in done:
        done.append(user)
    data['users_done'] = done
    save(data)
    return data


def finish_pass(scan_interval_seconds: int) -> Dict:
    data = load()
    finished = now()
    data.update({
        'completed': finished.isoformat(),
        'next_due': (finished + timedelta(seconds=scan_interval_seconds)).isoformat(),
        'users_done': [],
    })
    save(data)
    return data


def defer_first_pass(scan_interval_seconds: int) -> Dict:
    """First start with CYCLE_ON_STARTUP=false: fix the first due time so
    later restarts do not push it further out."""
    data = load()
    data['next_due'] = (now() + timedelta(seconds=scan_interval_seconds)).isoformat()
    save(data)
    return data
