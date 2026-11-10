"""Asking a requester which song they meant.

The local AI chooses among search results on its own, even when it is not
strongly sure. Only when its confidence is very low (below
REQUEST_CHOICE_BELOW, it is guessing) does the downloader ask the person who
made the request: their private Discord thread gets a drop-down list of the
results plus "None of them match", and a Select button that sends the
choice back. "None of them match" moves on to the next download source,
which asks again only if the AI is guessing again. The downloader runs in
the bot process (live requests) or in a cycle process (queued requests), so
a question is a file in DATA_DIR/request_choices/ that the bot posts and
answers:

    new ──bot posts──▶ asked ──list + Select──▶ answered   (the downloader reads it)
                         └──no answer in time──▶ expired    (the bot deletes it)

A settled question leaves nothing in the thread: the bot deletes it, and
what was decided is noted on the request's final message instead
(`Requester.notes`).

Whoever downloads for a requester wraps the download in `asking(...)`; the
downloader calls `ask(...)` only inside one, so cycle picks are never asked.
"""
import contextlib
import contextvars
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Union

logger = logging.getLogger("musicai.request_choice")

NEXT = "next"
POLL_SECONDS = 2.0


@dataclass
class Requester:
    discord_user: str                       # Discord username (or id)
    # Set while a question waits, so a download timeout can leave that time out.
    waiting: threading.Event = field(default_factory=threading.Event)
    # What each question settled, for the request's final message.
    notes: List[str] = field(default_factory=list)


_current: contextvars.ContextVar[Optional[Requester]] = contextvars.ContextVar("requester", default=None)


def ask_below() -> float:
    """The AI's confidence (0–1) under which the requester is asked."""
    try:
        return float(os.environ.get("REQUEST_CHOICE_BELOW", "0.1"))
    except ValueError:
        return 0.1


def timeout_seconds() -> int:
    try:
        return int(os.environ.get("REQUEST_CHOICE_TIMEOUT_SECONDS", "900"))
    except ValueError:
        return 900


def _dir() -> str:
    return os.path.join(os.environ.get("DATA_DIR", "/app/data"), "request_choices")


def _path(question_id: str) -> str:
    return os.path.join(_dir(), f"{question_id}.json")


def _write(question: Dict) -> None:
    os.makedirs(_dir(), exist_ok=True)
    tmp = _path(question["id"]) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(question, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(question["id"]))


def load(question_id: str) -> Optional[Dict]:
    try:
        with open(_path(question_id)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# -- the downloader's side ----------------------------------------------------

@contextlib.contextmanager
def asking(discord_user: Optional[str]) -> Iterator[Optional[Requester]]:
    """Downloads inside this block may ask `discord_user` to choose."""
    requester = Requester(str(discord_user)) if discord_user else None
    token = _current.set(requester)
    try:
        yield requester
    finally:
        _current.reset(token)


def current() -> Optional[Requester]:
    return _current.get()


def can_ask() -> bool:
    """A requester to ask, a bot to ask them, and time to wait for them."""
    bot_on = bool(os.environ.get("DISCORD_BOT_TOKEN", "").strip()) and \
        os.environ.get("DISCORD_ENABLED", "true").strip().lower() not in ("0", "false", "no")
    return current() is not None and bot_on and timeout_seconds() > 0


def ask(request: str, source: str, options: List[Dict[str, str]], next_step: Optional[str],
        suggestion: Optional[int] = None) -> Union[int, str, None]:
    """Ask the current requester which option ({"label", "detail"}) is
    `request` and wait.

    Returns the chosen option's index, NEXT ("None of these"), or None when
    nobody answered in time (or the requester can't be reached), in which
    case the caller keeps its own best guess (`suggestion`).
    """
    requester = current()
    if requester is None or not options:
        return None
    question = {
        "id": uuid.uuid4().hex[:12], "status": "new", "discord_user": requester.discord_user,
        "request": request, "source": source, "options": options[:24], "next_step": next_step,
        "suggestion": suggestion, "created": time.time(), "timeout": timeout_seconds(),
    }
    _write(question)
    logger.info(f"❓ Asking {requester.discord_user} which {source} result is '{request}' "
                f"({len(options)} option(s), waiting up to {question['timeout'] // 60} min)")
    deadline = time.time() + question["timeout"]
    requester.waiting.set()
    try:
        while time.time() < deadline:
            time.sleep(POLL_SECONDS)
            now = load(question["id"])
            if now is None:
                return None
            if now["status"] == "answered":
                os.remove(_path(question["id"]))
                answer = now.get("answer")
                logger.info(f"❓ {now.get('answered_by') or requester.discord_user} answered: "
                            f"{'none of these' if answer == NEXT else f'#{answer + 1}'}")
                if answer == NEXT:
                    requester.notes.append(f"{source}: none of its results matched")
                    return NEXT
                requester.notes.append(f"{source}: you picked {_label(options[int(answer)])}")
                return int(answer)
            if now["status"] == "unreachable":
                os.remove(_path(question["id"]))
                return None
        now = load(question["id"]) or question
        now["status"] = "expired"
        _write(now)
        logger.info(f"❓ No answer from {requester.discord_user} for '{request}'")
        requester.notes.append(f"{source}: no answer in time, my best guess was taken")
        return None
    finally:
        requester.waiting.clear()


def _label(option) -> str:
    return option.get("label", "") if isinstance(option, dict) else str(option)


# -- the bot's side -----------------------------------------------------------

def pending() -> List[Dict]:
    """Questions the bot has to post, close or give up on, oldest first."""
    found = []
    try:
        names = os.listdir(_dir())
    except OSError:
        return []
    for name in names:
        if name.endswith(".json"):
            question = load(name[:-5])
            if not question:
                continue
            # Asked by a process that is gone (restarted): nobody reads the
            # answer any more, so it is closed like an expired one.
            overdue = time.time() > question.get("created", 0) + question.get("timeout", 0) + 60
            if question.get("status") in ("asked", "new") and overdue:
                question["status"] = "expired"
                _write(question)
            if question.get("status") in ("new", "expired"):
                found.append(question)
    return sorted(found, key=lambda q: q.get("created", 0))


def posted(question_id: str, thread_id: int, message_id: int) -> None:
    question = load(question_id)
    if question and question["status"] == "new":
        question.update(status="asked", thread_id=thread_id, message_id=message_id)
        _write(question)


def unreachable(question_id: str) -> None:
    question = load(question_id)
    if question and question["status"] == "new":
        question["status"] = "unreachable"
        _write(question)


def _valid(question: Dict, choice: Union[int, str]) -> bool:
    return choice == NEXT or (isinstance(choice, int) and 0 <= choice < len(question["options"]))


def select(question_id: str, choice: Union[int, str], by: str) -> Optional[Dict]:
    """Remember what was picked in the list (sent by the Select button)."""
    question = load(question_id)
    if not question or question["status"] != "asked" or not _valid(question, choice):
        return None
    question.update(selected=choice, selected_by=by)
    _write(question)
    return question


def answer(question_id: str, choice: Union[int, str], by: str) -> Optional[Dict]:
    """Send the choice back to the downloader; the question, or None when it
    is no longer open."""
    question = load(question_id)
    if not question or question["status"] != "asked" or not _valid(question, choice):
        return None
    question.update(status="answered", answer=choice, answered_by=by)
    _write(question)
    return question


def closed(question_id: str) -> None:
    """The bot has removed an expired question from the thread."""
    try:
        os.remove(_path(question_id))
    except OSError:
        pass
