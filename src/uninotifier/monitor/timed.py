"""
Messages that depend only on the clock, not on what the portal shows:
deadline reminders (24 h / 1 h) and the lecture messages (fallback for the bot).
"""

import logging
import time

from uninotifier import lectures
from uninotifier import media
from uninotifier import reminders
from uninotifier.monitor import alerts, config

log = logging.getLogger("uninotifier")


# --------------------------------------------------- reminders (time-based)

def send_deadline_reminders(deadlines: dict, on_sent=None) -> None:
    """24h / 1h reminders. `on_sent()` runs right after each one is delivered —
    the caller saves the flag there, so a reminder is never repeated."""
    now = time.time()
    for d in deadlines.values():
        try:
            remaining = d["timestamp"] - now
            if remaining <= 0:
                continue

            if d.get("submitted"):
                continue  # handed in: no more nagging

            due = [(flag, window_seconds) for flag, window_seconds in config.REMINDER_WINDOWS
                   if remaining <= window_seconds and not d.get(f"reminded_{flag}")]
            if not due:
                continue
            # One message per check, the nearest window's: a task found half an hour before it is
            # due gets the final reminder, not that and a "24 hours" one in the same minute. The
            # wider windows it has already passed are settled with it.
            flag = min(due, key=lambda entry: entry[1])[0]
            sent = alerts.send_telegram(media.build_reminder_message(d, flag), d.get("url"))
            if sent:  # an undelivered reminder is retried on the next run
                for settled, _ in due:
                    d[f"reminded_{settled}"] = True
                log.info("Sent the %s reminder of %s", flag, d.get("name"))
                if on_sent:
                    on_sent()
        except Exception:
            log.exception("Skipping a malformed deadline entry: %r", d)


def send_assignment_notices(deadlines: dict, on_sent=None) -> None:
    """An assignment that closed, or was removed from the course, while it was not handed in
    (scan.check_overdue left a "notice" on it): told once. A removed one is then forgotten."""
    for key, d in list(deadlines.items()):
        kind = d.get("notice")
        if kind not in ("closed", "removed"):
            continue
        try:
            sent = alerts.send_telegram(media.build_assignment_notice(d, kind),
                                        d.get("url") if kind == "closed" else None)
        except Exception:
            log.exception("Could not tell that %s was %s", d.get("name"), kind)
            continue
        if sent:                                    # undelivered: tried again on the next run
            if kind == "removed":
                del deadlines[key]
            else:
                d["notice"] = "told"
            log.info("Told that %s was %s", d.get("name"), kind)
            if on_sent:
                on_sent()


def send_schedule_messages(saved: dict) -> None:
    """Lecture messages (an hour before: text only; at the start: Join Meeting,
    Course Page and the link's screenshot). The bot sends them on the minute;
    this is the fallback for when it isn't running — lectures.py makes sure each
    goes out once. Personal reminders too (the bot's fallback). Needs no portal access."""
    lectures.send_due(config.BOT_TOKEN, config.CHAT_ID)
    reminders.send_due(config.BOT_TOKEN, config.CHAT_ID)
