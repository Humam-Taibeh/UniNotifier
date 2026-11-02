"""
Claim files: one marker file per message or attempt that must happen once, however many
processes (the bot's clock, the scheduled monitor) look at the same minute.

A claim is an exclusively-created file in data/daily_sent/ (as lectures.py, tasks.py and
checkin.py use them): `claim` is True for exactly one caller per key, `release` gives it back
when the message could not be delivered, and `prune` forgets markers older than a week.
"""

import os
import time
from pathlib import Path

from uninotifier import paths

CLAIM_DIR = paths.DATA / "daily_sent"
KEEP_DAYS = 7

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _marker(key: str) -> Path:
    return CLAIM_DIR / f"{key}.sent"


def claim(key: str) -> bool:
    """True for exactly one caller per key."""
    CLAIM_DIR.mkdir(exist_ok=True)
    try:
        os.close(os.open(_marker(key), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def release(key: str) -> None:
    _marker(key).unlink(missing_ok=True)


def claimed(key: str) -> bool:
    return _marker(key).exists()


def prune() -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in CLAIM_DIR.glob("*.sent") if CLAIM_DIR.is_dir() else []:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass
