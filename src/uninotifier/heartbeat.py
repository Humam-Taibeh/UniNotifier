"""
The bot's pulse. The always-on bot has two loops that must keep turning —
"poll" (listening to Telegram) and "ticker" (lecture and reminder clock) — and
each stamps data/bot_heartbeat.json as it goes round. Two things read it:

  - the scheduled monitor, which tells you when the bot has gone quiet
    (monitor/alerts.py: check_bot), and
  - the bot's own pulse thread, which pings the bot's healthchecks.io check
    only while both loops are fresh (so a hung bot stops pinging and
    healthchecks.io alerts you through its own channels).

Writing is throttled (every few seconds at most) and never raises.
"""

import json
import logging
import os
import threading
import time

from uninotifier import fileio, paths

log = logging.getLogger("uninotifier")

FILE = paths.DATA / "bot_heartbeat.json"
PARTS = ("poll", "ticker")
WRITE_EVERY = 10                  # seconds between disk writes
STALE_SECONDS = 10 * 60           # a loop silent for this long counts as stopped

_lock = threading.Lock()
_state: dict = {"started": time.time()}
_last_write = 0.0


def beat(part: str, now: float | None = None) -> None:
    """One turn of loop `part` ("poll" or "ticker")."""
    global _last_write
    now = time.time() if now is None else now
    with _lock:
        _state[part] = now
        _state["pid"] = os.getpid()
        if now - _last_write < WRITE_EVERY:
            return
        _last_write = now
        try:
            fileio.write_json_atomic(FILE, _state)
        except OSError:
            log.warning("Could not write %s", FILE.name)


def read() -> dict:
    """The last pulse on disk, {} when there is none (the bot never ran here)."""
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def stale_parts(beat_data: dict, now: float | None = None, limit: float = STALE_SECONDS) -> list[str]:
    now = time.time() if now is None else now
    return [p for p in PARTS if now - float(beat_data.get(p) or 0) > limit]


def fresh_in_memory(limit: float = STALE_SECONDS) -> bool:
    """Both loops of THIS process turned recently (the pulse thread's test)."""
    with _lock:
        return not stale_parts(_state, limit=limit)


def uptime_seconds() -> float:
    """Seconds since Windows booted (a huge number elsewhere): right after a
    start-up the bot has not had its chance to beat yet."""
    if os.name != "nt":
        return float("inf")
    try:
        import ctypes
        return ctypes.windll.kernel32.GetTickCount64() / 1000
    except Exception:
        return float("inf")
