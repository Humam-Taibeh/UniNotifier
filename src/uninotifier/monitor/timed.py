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

            for flag, window_seconds in config.REMINDER_WINDOWS:
                key = f"reminded_{flag}"
                if d.get(key) or remaining > window_seconds:
                    continue
                sent = alerts.send_telegram(media.build_reminder_message(d, flag), d.get("url"))
                if sent:  # an undelivered reminder is retried on the next run
                    d[key] = True
                    if on_sent:
                        on_sent()
        except Exception:
            log.exception("Skipping a malformed deadline entry: %r", d)


def send_schedule_messages(saved: dict) -> None:
    """Lecture messages (an hour before: text only; at the start: Join Meeting,
    Course Page and the link's screenshot). The bot sends them on the minute;
    this is the fallback for when it isn't running — lectures.py makes sure each
    goes out once. Personal reminders too (the bot's fallback). Needs no portal access."""
    lectures.send_due(config.BOT_TOKEN, config.CHAT_ID)
    reminders.send_due(config.BOT_TOKEN, config.CHAT_ID)
