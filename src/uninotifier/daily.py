"""
The chat's day structure: a date line above the first message of each day,
and a short recap at 11:59 PM when a whole day passed with nothing new.

Day separator — `ensure_separator` runs before every automatic message
(notifier.before_send). The first one of a calendar day claims the day and
sends "━━━━━━━━ 📅 Tuesday, 13 Oct 2026 ━━━━━━━━" silently, so the phone
buzzes once, for the message itself. Replies to commands get no separator.

Midnight recap — each monitor run writes down, in saved_items.json "days",
that the day was checked and whether it found anything new (a course, an
item, a forum post, an updated file, an edited page). At 11:59 PM a day that
was checked and stayed quiet gets one "nothing new" message — from the bot's
clock, or from a monitor run that falls in that minute. If the PC was off or
asleep then, the first check after midnight sends it for the previous day
instead — unless that check finds new items itself: they may have been posted
the evening before, and "nothing new yesterday" would then be false. A day
without one successful check gets no recap; nothing can be said about it.

Claims are exclusively-created marker files, as in lectures.py, so the bot
and the monitor never both send the same separator or recap.
"""

import logging
import os
import time
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path

from uninotifier import notifier, paths

log = logging.getLogger("uninotifier")

CLAIM_DIR = paths.DATA / "daily_sent"
KEEP_DAYS = 7
RECAP_AT = clock(23, 59)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
QUIET = "No new files, assignments, or announcements posted {when} across your courses."


def day_name(day: date, year: bool = True) -> str:
    """'Tuesday, 13 Oct 2026' (English whatever the system locale)."""
    text = f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"
    return f"{text} {day.year}" if year else text


def separator_text(day: date) -> str:
    return f"━━━━━━━━ 📅 {day_name(day)} ━━━━━━━━"


def recap_text(day: date, today: date) -> str:
    if day == today:
        return "✨ *Daily Recap:* " + QUIET.format(when="today")
    return f"✨ *Daily Recap ({day_name(day, year=False)}):* " + QUIET.format(when="yesterday")


# ------------------------------------------------------------------ claims

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


# --------------------------------------------------------------- separator

def ensure_separator(token: str, chat_id: str, now: datetime | None = None) -> None:
    """Send today's date line unless it is already in the chat. The claim is
    taken before sending, so the separator's own message doesn't recurse."""
    day = (now or datetime.now()).date()
    key = f"separator-{day}"
    if not claim(key):
        return
    if not notifier.send_message(token, chat_id, separator_text(day), silent=True):
        release(key)        # the next message tries again
    prune()


# ------------------------------------------------------------------- recap

def note_check(days: dict, found_new: bool, now: datetime | None = None) -> None:
    """Record a successful check in saved["days"] (the last KEEP_DAYS days)."""
    today = str((now or datetime.now()).date())
    for key, add in (("checked", True), ("active", found_new)):
        seen = set(days.get(key, []))
        if add:
            seen.add(today)
        days[key] = sorted(seen)[-KEEP_DAYS:]


def quiet(days: dict, day: date) -> bool:
    """Checked at least once, and nothing new was found."""
    return str(day) in days.get("checked", []) and str(day) not in days.get("active", [])


def recap_time(now: datetime | None = None) -> bool:
    return (now or datetime.now()).time() >= RECAP_AT


def _send_recap(token: str, chat_id: str, day: date, today: date) -> bool:
    key = f"recap-{day}"
    if not claim(key):
        return False
    if notifier.send_message(token, chat_id, recap_text(day, today)):
        log.info("Sent the daily recap for %s", day)
        return True
    release(key)
    return False


def send_recap(token: str, chat_id: str, days: dict, now: datetime | None = None) -> bool:
    """11:59 PM: today's recap, if today was checked and stayed quiet."""
    now = now or datetime.now()
    if not recap_time(now) or not quiet(days, now.date()):
        return False
    return _send_recap(token, chat_id, now.date(), now.date())


def catch_up(token: str, chat_id: str, days: dict, found_new: bool,
             now: datetime | None = None) -> bool:
    """The first check after a missed 11:59 PM sends yesterday's recap. A
    check that finds new items closes yesterday without one (see above)."""
    today = (now or datetime.now()).date()
    yesterday = today - timedelta(days=1)
    if not quiet(days, yesterday):
        return False
    if found_new:
        claim(f"recap-{yesterday}")
        return False
    return _send_recap(token, chat_id, yesterday, today)
