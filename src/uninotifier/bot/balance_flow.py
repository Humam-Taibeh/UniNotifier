"""
The tuition balance watch in the bot (balance_watch.py has the rules): every INTERVAL_SECONDS the portal's financial
record is read, and when the balance changed you get a message with the screenshot, the old and the new amount.
"""

import logging
import threading
import time

from uninotifier import balance_watch, failsafe, maintenance, notifier, portal
from uninotifier.bot import chat, portal_flow

log = logging.getLogger("uninotifier")

TICK = 60                                   # seconds between looks at whether a look is due


def check_once() -> bool:
    """One unattended look at the balance. True when it was changed and you were told."""
    try:
        result = portal.fetch_many(["balance"], lambda attempt, rejected: None)["balance"]     # no code can be asked for
    except (maintenance.PortalUnavailable, portal.PortalError) as e:
        log.info("Balance watch: the portal was not read (%s)", notifier.redact(str(e)))
        balance_watch.skipped()
        return False
    total = result.figures.get("balance") if result.found else None
    if total is None:
        balance_watch.skipped()
        return False
    before = balance_watch.observe(total)
    if not balance_watch.changed(before, total):
        return False
    text = balance_watch.message(before, total)
    rows = [[{"text": "💰 Balance", "callback_data": "portal:balance"}], chat.BACK_ROW]
    if not (result.png and notifier.send_png_file(chat.BOT_TOKEN, chat.CHAT_ID, result.png,
                                                  "ZUJ Portal - Tuition Balance.png", text, rows=rows)):
        notifier.send_message(chat.BOT_TOKEN, chat.CHAT_ID, text, rows=rows)
    return True


def loop() -> None:
    while True:
        try:
            if portal.credentials() is not None and balance_watch.due() and portal_flow._portal_lock.acquire(blocking=False):
                try:
                    check_once()
                finally:
                    portal_flow._portal_lock.release()
        except Exception as e:
            log.exception("The balance watch failed")
            failsafe.bug("bot:balance-watch", "The balance watch is failing", e,
                         "You may not be told when your balance changes until it is fixed.")
        time.sleep(TICK)


def start() -> None:
    threading.Thread(target=loop, name="balance", daemon=True).start()
