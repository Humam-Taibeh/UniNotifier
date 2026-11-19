"""
The midterm marks watch in the bot (exams_watch.py has the rules, exams.py reads the site): every INTERVAL_SECONDS the
exams site is read with the portal login, and each mark that appears or changes is sent to you as a message of its own.
"""

import logging
import threading
import time

from uninotifier import exams, exams_watch, failsafe, notifier, portal
from uninotifier.bot import chat

log = logging.getLogger("uninotifier")

TICK = 60                                   # seconds between looks at whether a look is due


def _send(text: str, url: str | None = None) -> bool:
    rows = [[{"text": "🌐 Open Exams Site", "url": url}]] if url else None
    return bool(notifier.send_message(chat.BOT_TOKEN, chat.CHAT_ID, text, rows=rows))


def check_once() -> int:
    """One unattended look. Returns how many marks you were told about (the first look says the watch is on)."""
    creds = portal.credentials()
    if creds is None:
        return 0
    state = exams_watch.load()
    try:
        courses = exams.read(*creds, known=state["courses"])
    except exams.ExamsError as e:
        log.info("Exams watch: not read (%s)", notifier.redact(str(e)))
        exams_watch.skipped()
        return 0
    first = not state.get("started")
    news, new_state = exams_watch.compare(state, courses)
    sent, failed = 0, False
    if first:
        failed = not _send(exams_watch.intro(courses), exams.OVERVIEW_URL)
    for item in news:
        if _send(exams_watch.message(item), item["url"]):
            sent += 1
            log.info("Exams watch: told about %s — %s", item["course"], item["exam"])
        else:
            failed = True
    if failed:                                # something did not go out: nothing is remembered, the next look tells again
        exams_watch.skipped()
        return sent
    exams_watch.save(new_state)
    return sent


def loop() -> None:
    while True:
        try:
            if portal.credentials() is not None and exams_watch.due():
                check_once()
        except Exception as e:
            log.exception("The exams watch failed")
            failsafe.bug("bot:exams-watch", "The midterm marks watch is failing", e,
                         "You may not be told when a midterm mark appears until it is fixed.")
        time.sleep(TICK)


def start() -> None:
    threading.Thread(target=loop, name="exams", daemon=True).start()
