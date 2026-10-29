"""
The bot itself: routing every command, button and typed answer to its flow,
the 20-second clock (lectures, reminders, the 11:59 PM recap), the Finals
Watch thread, and the long-polling loop (main).
"""

import json
import logging
import os
import sys
import threading
import time

from uninotifier import access
from uninotifier import checkin
from uninotifier import paths
from uninotifier import daily
from uninotifier import envcheck
from uninotifier import failsafe
from uninotifier import fileio
from uninotifier import guide
from uninotifier import healthcheck
from uninotifier import heartbeat
from uninotifier import lectures
from uninotifier import media
from uninotifier import mfa
from uninotifier import moodle
from uninotifier import notifier
from uninotifier import probe
from uninotifier import cambridge          # temporary (this semester): remove with its thread below
from uninotifier import reminders
from uninotifier import schedule
from uninotifier import tasks
from uninotifier import telegram
from uninotifier.bot import chat, checks, finals_flow, gpa_flow, guest_flow, portal_flow, reminder_flow, views

log = logging.getLogger("uninotifier")


def _poll_timeout() -> int:
    try:
        return min(50, max(5, int(os.getenv("BOT_POLL_TIMEOUT") or 20)))
    except ValueError:
        return 20


# Seconds Telegram holds a getUpdates call open. Short on purpose: a connection that silently
# dies mid-wait then costs at most POLL_TIMEOUT + POLL_SLACK seconds instead of 45 (measured
# with data/telegram_stats.json; override with BOT_POLL_TIMEOUT in .env).
POLL_TIMEOUT = _poll_timeout()
POLL_SLACK = 10          # extra read time before the call is given up on
OUTAGE_AFTER = 4         # failed polls in a row before it counts as an outage (one blip is routine)
MAX_POLL_WAIT = 120      # the longest "retry after" the loop waits out (well inside heartbeat.STALE_SECONDS)


class OutageTracker:
    """Telegram unreachable? A single failed poll is routine (and counted in telegram.py's
    statistics); this logs only a real outage — once when it starts, once when it ends —
    and returns how long to pause before the next try (backing off, never hammering)."""

    def __init__(self):
        self.failures = 0
        self.since = 0.0
        self.reported = False

    def failed(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        if not self.failures:
            self.since = now
        self.failures += 1
        if self.failures == OUTAGE_AFTER and not self.reported:
            self.reported = True
            log.warning("Telegram is unreachable: %d polls in a row failed", self.failures)
        return telegram.backoff_seconds(self.failures)

    def ok(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.reported:
            log.info("Telegram is reachable again (%d failed polls, %.0f s)", self.failures, now - self.since)
        self.failures, self.since, self.reported = 0, 0.0, False


COMMANDS = [
    ("start", "🏠 Main Menu"),
    ("help", "📖 System Guide"),
    ("courses", "📚 My Courses & Schedule"),
    ("tasks", "⏳ Upcoming Tasks & Reminders"),
    ("remind", "➕ New Custom Reminder"),
    ("schedule", "📅 Weekly Timetable"),
    ("need", "🎯 Grade Needed"),
    ("gpa", "📊 GPA & Grade Hub"),
    ("record", "📜 Academic Record & Balance"),
    ("guest", "👤 Guest Look-up"),
    ("grades", "🔄 Check Grades Now"),
    ("transcript", "📜 Transcript & Passed Hours"),
    ("balance", "💰 Tuition Balance"),
    ("finals", "🏁 Finals Watch"),
]


def handle_command(text: str, message_id: int | None = None) -> None:
    if guest_flow._guest.get("step") in ("user", "password"):
        if time.time() - guest_flow._guest.get("at", 0) > guest_flow.GUEST_TIMEOUT or text.strip().startswith("/"):
            if guest_flow.handle_expired_prompt(text, message_id):      # expired, or any command: the prompt is dropped
                return
        else:
            guest_flow.handle_guest_text(text, message_id)
            return
    elif guest_flow._guest.get("step") == "target":          # a typed target: the session itself stays
        if time.time() - guest_flow._guest.get("at", 0) > guest_flow.GUEST_TIMEOUT or text.strip().startswith("/"):
            guest_flow._guest["step"] = "done"
        else:
            guest_flow.handle_guest_text(text, message_id)
            return
    if checks.handle_mfa_code(text):
        return
    if portal_flow.handle_portal_code(text):
        if guest_flow._guest.get("step") == "running" and message_id:
            guest_flow._guest["messages"].append(message_id)          # the guest's code goes on 🚪 Sign Out too
        return
    command = text.split()[0].split("@")[0].lower() if text.strip() else ""
    if reminder_flow._reminder_wait and not command.startswith("/"):
        reminder_flow.handle_reminder_text(text)
        return
    if gpa_flow._pending_prompt and not command.startswith("/"):
        gpa_flow.handle_gpa_answer(text)
        return
    gpa_flow._pending_prompt = reminder_flow._reminder_wait = None      # any command drops an open question
    if command == "/remind":
        reminder_flow.start_reminder()
    elif command == "/guest":
        guest_flow.start_guest()
    elif command == "/need":
        gpa_flow.handle_need(text.strip()[len(text.split()[0]):])
    elif command == "/finals":
        finals_flow.handle_finals(text.strip()[len(text.split()[0]):])
    elif command == "/grades":
        finals_flow.handle_finals("check")                  # this term's grades live in Finals Watch
    elif command in views.PORTAL_COMMANDS:
        if not portal_flow.start_portal(views.PORTAL_COMMANDS[command]):
            chat.send_view("⏳ *A portal request is already running.*", [chat.BACK_ROW])
    elif command == "/check":
        if not checks.start_check(None):
            chat.send_view("⏳ *A check is already running.*", [chat.BACK_ROW])
    elif command == "/schedule":
        views.start_schedule_files()
    elif command == "/help":
        chat.send_view(*guide.view_hub(chat.monitor_status()))
    elif command in views.COMMAND_VIEWS:
        chat.send_view(*views.VIEWS[views.COMMAND_VIEWS[command]]())
    elif command.startswith("/"):
        chat.send_view("❓ Unknown command. Use the menu 👇", views.view_menu()[1])


def set_item_done(cmid: str, done: bool) -> None:
    """Mirror a toggle into saved_items.json so /items shows it at once (the
    next monitor run would read the same state back from the portal). The read and
    the write happen under the file's write lock, which the monitor's saves take too:
    the bot never writes back a copy older than the monitor's latest save (that would
    make the next check announce again what it had just announced)."""
    try:
        with fileio.write_lock(chat.DATA_FILE):
            state = chat.load_state()
            if not state:
                return        # unreadable right now: never write back an empty state
            for item in state.get("content", {}).values():
                if item.get("cmid") == cmid:
                    item["done"] = done
            fileio.write_json_atomic(chat.DATA_FILE, state)
    except OSError:
        log.exception("Could not record completion of cm %s", cmid)


DONE_WAIT = 2.5      # seconds a "Mark as done" press waits for the portal before answering "working"


def _set_done_row(cb: dict, cmid: str, new_row: list) -> None:
    """Replace this item's done/undo button on the message that carried it."""
    message = cb.get("message") or {}
    rows = (message.get("reply_markup") or {}).get("inline_keyboard", [])
    swapped = [new_row if any(b.get("callback_data", "").startswith(f"done:{cmid}:") for b in row) else row
               for row in rows]
    if message.get("message_id"):
        chat.api("editMessageReplyMarkup", chat_id=chat.CHAT_ID, message_id=message["message_id"],
                 reply_markup={"inline_keyboard": swapped})


def handle_done_toggle(cb: dict, action: str) -> None:
    """'done:<cmid>:<1|0>' - set the portal's completion, then flip the button on the message
    that carried it. The portal can be slow, and a press must be answered quickly: the work runs
    in a thread; if it has not finished after DONE_WAIT seconds the press is answered with
    "updating..." and the outcome shows on the button itself when it comes."""
    _, cmid, value = action.split(":")
    done = value == "1"

    def work() -> None:
        ok = expired = False
        try:
            ok = moodle.http_set_completion(cmid, done)
        except moodle.SessionExpiredError:
            expired = True
        except Exception:
            log.exception("Completion toggle failed for cm %s", cmid)
        try:
            if ok:
                set_item_done(cmid, done)
                chat.answer(cb["id"], "✅ Marked as done on the portal" if done else "↩️ Marked as not done")
                _set_done_row(cb, cmid, media.completion_row(cmid, done))
                return
            told = chat.answer(
                cb["id"], "⚠️ The portal session has expired. It renews on the next check." if expired
                else "❌ The portal didn't accept the change. Try again later.", alert=expired)
            if not told:                               # the person only saw "updating...": say it on the button
                _set_done_row(cb, cmid, media.completion_failed_row(cmid, done))
        except Exception:
            log.exception("Could not show the outcome of the completion toggle for cm %s", cmid)

    worker = threading.Thread(target=work, name="done-toggle", daemon=True)
    worker.start()
    worker.join(DONE_WAIT)
    if worker.is_alive():
        chat.answer(cb["id"], "⏳ Updating the portal…")


def handle_callback(cb: dict) -> None:
    """One button press. It is answered at once (toast), a double tap is ignored, and
    whatever route ran, the press is answered before this returns."""
    action = cb.get("data", "")
    message_id = (cb.get("message") or {}).get("message_id")
    if chat.is_double_tap(message_id, action):
        chat.answer(cb["id"])
        return
    route_callback(cb, action, message_id)
    chat.answer(cb["id"])                  # a no-op when the route already answered; a raise skips it (update_failed answers)


def route_callback(cb: dict, action: str, message_id: int | None) -> None:
    if action.startswith("done:"):
        handle_done_toggle(cb, action)
        return
    if action.startswith("gpa:"):
        chat.answer(cb["id"])
        gpa_flow.handle_gpa_callback(action, message_id)
        return
    if action.startswith("rem:"):
        reminder_flow.handle_reminder_callback(cb, action, message_id)
        return
    if action.startswith("guest:"):
        guest_flow.handle_guest_callback(cb, action)
        return
    if action.startswith("finals:"):
        chat.answer(cb["id"])
        finals_flow.handle_finals(action.split(":")[1].replace("status", ""), message_id)
        return
    if action == "portal:grades":                            # an old "This Term's Grades" button
        chat.answer(cb["id"])
        finals_flow.handle_finals("check", message_id)
        return
    if action.startswith("portal:"):
        started = portal_flow.start_portal(action.split(":")[1], None)
        chat.answer(cb["id"], "🏛️ Contacting the portal…" if started else "⏳ A portal request is already running")
        return
    if action == mfa.SMS_CALLBACK:
        ok = mfa.answer_request(choice="sms")
        chat.answer(cb["id"], "📱 Requesting an SMS code…" if ok else "⌛ That sign-in request has already ended")
        return
    if action == "check":
        started = checks.start_check(message_id)
        chat.answer(cb["id"], "🔄 Checking…" if started else "⏳ A check is already running")
        return
    chat.answer(cb["id"])
    if (action == guide.HUB or action.startswith("help:")) and message_id:
        chat.edit_view(message_id, *guide.view(action.partition(":")[2], chat.monitor_status()))
        return
    if action == "schedule":
        views.start_schedule_files()
        return
    if action in views.VIEWS and message_id:
        chat.edit_view(message_id, *views.VIEWS[action]())


def handle_update(update: dict) -> None:
    """The one gate: only the owner's own messages and button presses get past access.is_owner;
    everything else is dropped without a reply (access.deny)."""
    if not access.is_owner(update):
        access.deny(update)
        return
    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return
    msg = update.get("message") or {}
    if msg.get("text"):
        handle_command(msg["text"], msg.get("message_id"))


def update_failed(update: dict, exc: BaseException) -> None:
    """A handler raised. The bug is reported to you (held back after the first
    time, per bug — failsafe.py) and the person who pressed the button is always
    answered, so a broken button never just does nothing. Never raises."""
    try:
        cb = update.get("callback_query")
        what = "button press" if cb else "message"
        failsafe.report(f"bot:update:{failsafe.signature(exc)}", f"The bot failed to handle a {what}",
                        detail="It stays up; the other buttons and commands still work.", exc=exc, wait=False)
        origin = str((((cb or {}).get("message") or update.get("message") or {}).get("chat") or {}).get("id", ""))
        if origin != chat.CHAT_ID:
            return
        if cb:
            chat.answer(cb["id"], "⚠️ Something went wrong — it has been logged and reported.")
        else:
            chat.api("sendMessage", chat_id=chat.CHAT_ID, parse_mode="Markdown",
                     text="⚠️ *Something went wrong* with that request — it has been logged and reported.")
    except Exception:
        log.exception("Could not report a failed update")


def dispatch(update: dict) -> None:
    """handle_update, with whatever goes wrong contained: the bot keeps polling and
    you are told (update_failed)."""
    try:
        handle_update(update)
    except Exception as e:
        # never log the update itself: it may hold what you typed (a guest's password)
        log.exception("Update %s failed", update.get("update_id"))
        update_failed(update, e)


LECTURE_TICK = 20   # seconds between looks at the clock


# Threads whose messages are notifications, not replies: the first one of a
# day gets the date separator above it (daily.py).
AUTOMATIC_THREADS = ("lectures", "finals")


def before_send() -> None:
    if threading.current_thread().name in AUTOMATIC_THREADS:
        daily.ensure_separator(chat.BOT_TOKEN, chat.CHAT_ID)


def lecture_loop() -> None:
    """Lecture messages on the minute: an hour before (text) and at the start
    (Join Meeting, Course Page, link screenshot). The scheduled monitor sends
    the same messages as a fallback when this bot is down; lectures.py makes
    sure each goes out once. Personal reminders the same way (reminders.py).
    At noon, the open-tasks digest (tasks.py). At 11:59 PM, the quiet-day
    recap — left to the monitor while a check is running (it may still find
    something)."""
    while True:
        for name, job in (("lectures", lambda: lectures.send_due(chat.BOT_TOKEN, chat.CHAT_ID)),
                          ("reminders", lambda: reminders.send_due(chat.BOT_TOKEN, chat.CHAT_ID)),
                          ("tasks", lambda: tasks.send_digest(chat.BOT_TOKEN, chat.CHAT_ID, chat.load_state())),
                          ("recap", send_recap_if_due)):
            try:
                job()
            except Exception as e:
                log.exception("The %s ticker failed", name)
                failsafe.report(f"bot:ticker:{name}:{failsafe.signature(e)}", f"The bot's {name} clock is failing",
                                detail="Alerts of this kind may be late or missing until it is fixed.", exc=e, wait=False)
        heartbeat.beat("ticker")
        time.sleep(LECTURE_TICK)


def send_recap_if_due() -> None:
    if daily.recap_time() and not checks.MONITOR_LOCK.exists() and not checks._check_lock.locked():
        daily.send_recap(chat.BOT_TOKEN, chat.CHAT_ID, chat.load_state().get("days", {}))


PULSE_TICK = 60          # seconds between the pulse thread's looks
PING_EVERY = 300         # seconds between pings of the bot's healthchecks.io check


def pulse_loop() -> None:
    """Pings the bot's healthchecks.io check, but only while BOTH loops (listening,
    clock) have turned recently — so a hung bot stops pinging and you are alerted by
    healthchecks.io itself, even when the whole PC is off and nothing local can speak."""
    last_ping = 0.0
    while True:
        try:
            if heartbeat.fresh_in_memory() and time.time() - last_ping >= PING_EVERY:
                healthcheck.ping_bot()
                last_ping = time.time()
        except Exception:
            log.exception("Pulse thread failed")
        time.sleep(PULSE_TICK)


def register_commands(attempts: int = 5) -> bool:
    """Tell Telegram the command list (the "/" menu). Telegram keeps it between runs, so this is
    housekeeping, not a precondition: it runs beside the polling loop, with a few tries, instead of
    delaying the start of listening when the first connection of a new process is slow."""
    for attempt in range(1, attempts + 1):
        done = (chat.api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS]) is not None
                and chat.api("setChatMenuButton", menu_button={"type": "commands"}) is not None)
        if done:
            return True
        time.sleep(5 * attempt)
    log.warning("Could not register the command list with Telegram (it keeps the one it has)")
    return False


def main() -> None:
    notifier.setup_logging(paths.LOGS / "bot.log")
    failsafe.install_excepthooks("bot")
    envcheck.require("bot")
    if access.problems(chat.CHAT_ID):
        sys.exit("; ".join(access.problems(chat.CHAT_ID)))
    if access.from_chat_id:
        log.warning("ADMIN_USER_ID is not set in .env - using TELEGRAM_CHAT_ID as the owner's id")
    schedule.load_schedule()          # a broken schedule.json is reported now, not at the first lecture
    threading.Thread(target=register_commands, name="register", daemon=True).start()
    chat.start_answer_workers()
    notifier.before_send = before_send
    heartbeat.beat("poll")
    threading.Thread(target=lecture_loop, name="lectures", daemon=True).start()
    threading.Thread(target=finals_flow.finals_loop, name="finals", daemon=True).start()
    threading.Thread(target=pulse_loop, name="pulse", daemon=True).start()
    threading.Thread(target=probe.loop, name="probe", daemon=True).start()     # the 2-minute look at Moodle
    threading.Thread(target=checkin.loop, args=(chat.BOT_TOKEN, chat.CHAT_ID), name="checkin",
                     daemon=True).start()                                       # attendance check-in
    if cambridge.active():              # Cambridge One assignments, until CAMBRIDGE_UNTIL
        threading.Thread(target=cambridge.loop, name="cambridge", daemon=True).start()
    log.info("Bot polling started")
    offset = load_offset()
    outage = OutageTracker()
    while True:
        offset = poll_once(offset, outage)


# Which update comes next, kept on disk. Telegram forgets an update only when the next getUpdates
# asks past it, so a bot killed while handling one (stuck on a dead connection, then restarted by
# the monitor) used to get that same update again after the restart — and could get stuck on it
# again. The offset is saved BEFORE each update is handled: a restart moves past it. (The price:
# the one update being handled when the process died is not repeated.)
OFFSET_FILE = paths.DATA / "telegram_offset.json"


def _bot_id() -> str:
    return (chat.BOT_TOKEN or "").split(":", 1)[0]


def load_offset() -> int | None:
    """The saved offset — only if it belongs to this bot (another token has its own numbering)."""
    try:
        data = json.loads(OFFSET_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("bot") != _bot_id() or not isinstance(data.get("offset"), int):
        return None
    return data["offset"]


def save_offset(offset: int) -> None:
    try:
        fileio.write_json_atomic(OFFSET_FILE, {"bot": _bot_id(), "offset": offset, "at": time.time()})
    except OSError:
        log.warning("Could not save the update offset")


def poll_once(offset: int | None, outage: OutageTracker) -> int | None:
    """One turn of the polling loop: fetch what is new, hand each update to dispatch() — its
    offset saved first — and return the offset to ask for next."""
    params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    refusal: dict = {}
    updates = notifier.call_api(chat.BOT_TOKEN, "getUpdates", params, timeout=POLL_TIMEOUT + POLL_SLACK,
                                outcome=refusal)
    heartbeat.beat("poll")                     # a turn of the loop, whether or not Telegram answered
    if updates is None:
        wait = outage.failed()                 # network down, or another poller is active
        if refusal.get("retry_after"):         # Telegram asked to slow down: do, within MAX_POLL_WAIT
            wait = max(wait, min(refusal["retry_after"], MAX_POLL_WAIT))
            log.warning("Telegram asked the bot to slow down — next poll in %.0f s", wait)
        time.sleep(wait)
        return offset
    if not isinstance(updates, list):          # call_api gives True for an "ok" without a result
        log.warning("getUpdates answered %s instead of a list of updates — ignored", type(updates).__name__)
        time.sleep(outage.failed())
        return offset
    outage.ok()
    for update in updates:
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            log.warning("Skipped a malformed update from Telegram")
            continue
        offset = update["update_id"] + 1
        save_offset(offset)                    # before handling it: a restart never replays it
        dispatch(update)
    return offset
