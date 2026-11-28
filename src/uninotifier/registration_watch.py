"""
Registration watch: the portal's e-registration page (التسجيل ← التسجيل الإلكتروني) says "غير مسموح التسجيل بهذه الفترة"
(registration is not allowed in this period) while registration is closed. The bot looks at it every INTERVAL_SECONDS
and tells you ONCE, with the screenshot, the moment that sentence is gone — so you can register the first minute.

State: data/registration_watch.json — {"open": the last state seen (True/False), "checked": when}.
A first look that finds it closed says nothing; one that finds it open tells you (you were not told before).
"""

import json
import time

from uninotifier import fileio, paths

STATE_FILE = paths.DATA / "registration_watch.json"
INTERVAL_SECONDS = 15 * 60


def load() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def due(now: float | None = None) -> bool:
    return (time.time() if now is None else now) - float(load().get("checked") or 0) >= INTERVAL_SECONDS


def observe(is_open: bool, now: float | None = None) -> bool:
    """The state as just read. True when you are to be told: it is open now and was not seen open before."""
    state = load()
    tell = bool(is_open) and state.get("open") is not True
    state.update(open=bool(is_open), checked=time.time() if now is None else now)
    fileio.write_json_atomic(STATE_FILE, state)
    return tell


def skipped(now: float | None = None) -> None:
    """A look that could not read the page: the next one is in INTERVAL_SECONDS, not at once."""
    state = load()
    state["checked"] = time.time() if now is None else now
    fileio.write_json_atomic(STATE_FILE, state)
