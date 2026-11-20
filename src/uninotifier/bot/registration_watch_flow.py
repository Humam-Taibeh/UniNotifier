"""
The registration watch in the bot (registration_watch.py has the rules): every 2 hours the portal's registration page is read;
the moment it stops saying registration is closed you are told, once, with the screenshot.
"""

import logging
import time

from uninotifier import notifier
from uninotifier.bot import portal_flow

log = logging.getLogger("uninotifier")


def watch_once() -> bool:
    """One unattended look at the registration page. True when you were told it is open."""
    from uninotifier import maintenance, portal, registration_watch
    try:
        result = portal.fetch_many(["registration"], lambda attempt, rejected: None)["registration"]
    except (maintenance.PortalUnavailable, portal.PortalError) as e:
        log.info("Registration watch: the portal was not read (%s)", notifier.redact(str(e)))
        registration_watch.skipped()
        return False
    if not result.found or "open" not in result.data:
        registration_watch.skipped()
        return False
    if not registration_watch.observe(result.data["open"]):
        return False
    portal_flow.send_registration_answer(result, alert=True)
    return True


def watch_loop() -> None:
    from uninotifier import failsafe, portal, registration_watch
    while True:
        try:
            if portal.credentials() is not None and registration_watch.due() and portal_flow._portal_lock.acquire(blocking=False):
                try:
                    watch_once()
                finally:
                    portal_flow._portal_lock.release()
        except Exception as e:
            log.exception("The registration watch failed")
            failsafe.bug("bot:registration-watch", "The registration watch is failing", e,
                         "You may not be told when registration opens until it is fixed.")
        time.sleep(60)
