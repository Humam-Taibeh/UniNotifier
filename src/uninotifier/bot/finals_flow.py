"""
Finals Watch in the bot: the status screen, the automatic checks while it is
on, and /finals on | off | check.
"""

import logging
import threading
import time

from uninotifier import failsafe
from uninotifier import finals
from uninotifier import maintenance
from uninotifier import notifier
from uninotifier import portal
from uninotifier.bot import chat, portal_flow
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")


# ------------------------------------------------------------ finals season

FINALS_TICK = 60                      # seconds between looks at whether a check is due


def finals_status_view() -> tuple[str, list]:
    """The Finals Watch screen: this term's grades, one action (check now) and the switch."""
    state = finals.load()
    on = finals.active(state=state)
    rows = [[{"text": "🔄 Check Grades Now", "callback_data": "finals:check"}],
            [{"text": "⏸️ Turn Off Background Checks" if on else "▶️ Turn On Background Checks",
              "callback_data": "finals:off" if on else "finals:on"}],
            [{"text": "🔙 GPA & Grade Hub", "callback_data": "academic"},
             {"text": "🏠 Main Menu", "callback_data": "menu"}]]
    return finals.status_text(state), rows


def send_finals(messages: list[dict]) -> None:
    for m in messages:
        notifier.send_message(chat.BOT_TOKEN, chat.CHAT_ID, m["text"], rows=m["rows"] or None)


def auto_ask_code(attempt: int, rejected: bool) -> str | None:
    """An unattended check never waits for an answer; it says, now and then,
    that the portal wants a code and leaves it to /finals check."""
    state = finals.load()
    if time.time() - state.get("code_notice", 0) > finals.FAILURE_NOTICE_SECONDS:
        state["code_notice"] = time.time()
        finals.save(state)
        chat.send_view("🔐 *Finals Watch needs a verification code*\n\nThe portal asked for one. "
                  "Send /finals check and reply with the code when asked.", [chat.BACK_ROW])
    return None


def _show(message_id: int | None, text: str, rows: list) -> None:
    """Put a screen in the message the person pressed a button on, or at the bottom."""
    chat.edit_view(message_id, text, rows) if message_id else chat.send_view(text, rows)


@chat.with_action("typing")
def run_finals_check(manual: bool, message_id: int | None = None) -> None:
    """One reading of the portal for the finals watcher. The caller holds
    _portal_lock; it is released here. A manual check ends on the Finals Watch screen
    (this term's grades), in the message the button was pressed on."""
    try:
        state = finals.load()
        ask = portal_flow.ask_portal_code if manual else auto_ask_code
        unavailable = None
        try:
            messages, state = finals.check(lambda sections: portal.fetch_many(sections, ask), state)
        except maintenance.PortalUnavailable as e:
            problem, unavailable = e.detail, e
        except portal.PortalError as e:
            problem = notifier.redact(str(e))
        except Exception:
            log.exception("Finals check failed")
            problem = "the portal couldn't be read — see data/logs/bot.log"
        else:
            problem = None
        if problem:
            state["last_check"] = time.time()               # try again after the interval
            if manual or time.time() - state["failure_notice"] > finals.FAILURE_NOTICE_SECONDS:
                state["failure_notice"] = time.time()
                if unavailable:
                    portal_flow.send_unavailable(unavailable, "Finals Watch tries again at its next check.",
                                                 rows=[] if manual else None)
                    if manual:
                        _show(message_id, *finals_status_view())
                else:
                    _show(message_id, f"⚠️ *Finals Watch couldn't read the portal*\n\n{escape_md(problem)}",
                          [[{"text": "🔄 Try Again", "callback_data": "finals:check"}], chat.BACK_ROW])
            finals.save(state)
            return
        portal_flow.lock_grades(state.get("seen", {}))
        if state.get("base") and state["base"][0] is not None:
            portal_flow.sync_base(*state["base"])
        if state.get("retakes") is not None:
            portal_flow.sync_retakes(state["retakes"])
        send_finals(messages)
        if manual:
            screen = finals_status_view()
            if messages:
                chat.send_view(*screen)           # announcements landed below the screen: it returns to the bottom
            else:
                _show(message_id, *screen)        # nothing new: the screen updates where it is
    except Exception as e:
        log.exception("Finals check crashed")
        failsafe.bug("bot:finals", "A Finals Watch check crashed", e)
    finally:
        portal_flow._portal_lock.release()


def finals_loop() -> None:
    """While finals mode is on, look at the portal every few minutes — the one
    automatic use of the portal (see finals.py)."""
    while True:
        try:
            if finals.due() and portal_flow._portal_lock.acquire(blocking=False):
                run_finals_check(manual=False)
        except Exception as e:
            log.exception("Finals ticker failed")
            failsafe.bug("bot:finals-clock", "The Finals Watch clock is failing", e,
                         "Automatic grade checks may not run until it is fixed.")
        time.sleep(FINALS_TICK)


def handle_finals(arg: str, message_id: int | None = None) -> None:
    """/finals [on|off|check]"""
    arg = arg.strip().lower()
    if arg == "check":
        if portal.credentials() is None:
            chat.send_view(portal_flow.portal_setup_text(), [chat.BACK_ROW])
        elif portal_flow._portal_lock.acquire(blocking=False):
            if message_id:                                   # the button's own message shows the progress: no extra message
                chat.edit_view(message_id, "🏁 *Finals Watch*\n\n⏳ Reading your grades from the portal…", [])
            threading.Thread(target=run_finals_check, args=(True, message_id), daemon=True).start()
        else:
            chat.send_view("⏳ *A portal request is already running.*", [chat.BACK_ROW])
        return
    if arg in ("on", "off"):
        state = finals.load()
        state["enabled"] = arg == "on"
        state["last_check"] = 0                              # the first check comes right away
        finals.save(state)
        if arg == "on" and portal.credentials() is None:
            chat.send_view(portal_flow.portal_setup_text(), [chat.BACK_ROW])
            return
    text, rows = finals_status_view()
    chat.edit_view(message_id, text, rows) if message_id else chat.send_view(text, rows)
