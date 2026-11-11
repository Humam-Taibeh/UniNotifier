"""
🆕 New versions in the chat (updates.py): the daily look, /update, and ⬇️ Update Now.
"""

import logging
import os
import threading
import time

from uninotifier import failsafe
from uninotifier import selfheal
from uninotifier import updates
from uninotifier.bot import chat

log = logging.getLogger("uninotifier")

_busy = threading.Lock()
RESTART_WAIT = 2                    # seconds for the last message and the button's answer to leave


def _send(text: str, rows: list) -> bool:
    return chat.send_view(text, rows + [chat.BACK_ROW]) is not None


def loop() -> None:
    """Once a day (the first time a little after the bot starts): is there a new version?"""
    time.sleep(updates.FIRST_CHECK_AFTER)
    while True:
        try:
            if updates.due():
                updates.check(_send)
        except Exception as e:
            log.exception("The update check failed")
            failsafe.bug("bot:update-check", "The check for a new version failed", e,
                         "Nothing else is affected; `git pull` still updates by hand.")
        time.sleep(3600)


def view_status() -> tuple[str, list]:
    """/update: look now."""
    found = updates.look()
    if found is None:
        text = ("🆕 *Updates*\n\nI couldn't check: this copy was downloaded as a ZIP (see *Updating* in the README), "
                "or GitHub can't be reached right now.") if not updates.is_clone() else (
                "🆕 *Updates*\n\nGitHub can't be reached right now — try again later.")
        return text, [chat.BACK_ROW]
    if not found["behind"]:
        return "✅ *You are on the newest version.*", [chat.BACK_ROW]
    return updates.message(found), updates.ROWS + [chat.BACK_ROW]


def restart() -> None:
    """End this process; its scheduled task starts the bot again on the new code a few seconds later
    (selfheal.start_after_exit). If that could not be arranged, the bot stays up on the old code in
    memory and says so — never a bot that is simply gone."""
    if not selfheal.start_after_exit():
        chat.send_view("⚠️ The new version is installed, but I couldn't restart myself — restart the bot "
                       "(or the PC) to start using it.", [chat.BACK_ROW])
        return
    time.sleep(RESTART_WAIT)
    chat.flush_answers()
    log.info("Update: restarting the bot")
    os._exit(0)


def run_update(message_id: int | None) -> None:
    try:
        done = updates.apply()
    except updates.UpdateError as e:
        chat.send_view(f"⚠️ *Not updated* — {e}.", [chat.BACK_ROW])
        return
    except Exception as e:
        log.exception("The update failed")
        failsafe.bug("bot:update", "Update Now failed", e, "Update by hand with `git pull`, then restart the bot.")
        chat.send_view("⚠️ *Not updated* — something went wrong; it has been logged.", [chat.BACK_ROW])
        return
    finally:
        _busy.release()
    if done.startswith("Already"):
        chat.send_view(f"✅ {done}", [chat.BACK_ROW])
        return
    chat.send_view(f"✅ *{done}* The bot restarts now — it answers again in a few seconds.", [])
    restart()


def handle_callback(cb: dict, action: str, message_id: int | None) -> None:
    """update:now · update:later"""
    if action == "update:later":
        chat.answer(cb["id"], "OK — /update whenever you want it")
        if message_id:
            chat.edit_view(message_id, "⏰ *Update postponed* — send /update whenever you want it.", [chat.BACK_ROW])
        return
    if not _busy.acquire(blocking=False):
        chat.answer(cb["id"], "⏳ Already updating…")
        return
    chat.answer(cb["id"], "⬇️ Updating…")
    if message_id:
        chat.edit_view(message_id, "⬇️ *Updating…* this takes about a minute.", [])
    threading.Thread(target=run_update, args=(message_id,), name="update", daemon=True).start()
