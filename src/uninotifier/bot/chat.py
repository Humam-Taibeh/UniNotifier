"""
Talking to Telegram, and what every screen shares: the bot's credentials,
sending and editing a screen, the common buttons, and reading the tracker
state and the last check's summary (for the 🟢 status line).
"""

import functools
import json
import logging
import os
import queue
import threading
import time

from dotenv import load_dotenv

from uninotifier import fileio
from uninotifier import notifier
from uninotifier import paths
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")


load_dotenv(paths.ENV_FILE)


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID") or "")


DATA_FILE = paths.DATA / "saved_items.json"


LAST_RUN_FILE = paths.DATA / "last_run.json"


MONITOR_STALE_SECONDS = 45 * 60   # three missed 15-minute checks: the PC is off or asleep


# ---------------------------------------------------------------- helpers

def load_state() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_last_run() -> dict:
    try:
        return json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def remaining_text(ts: float) -> str:
    seconds = int(ts - time.time())
    if seconds <= 0:
        return "now"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def age_text(ts: float) -> str:
    minutes = max(0, int(time.time() - ts)) // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


def link(text: str, url: str | None) -> str:
    return f"[{escape_md(text)}]({url})" if url else f"*{escape_md(text)}*"


def monitor_status() -> str:
    """One honest line about the scheduled monitor, from its last run."""
    run = load_last_run()
    finished = run.get("finished")
    if not finished:
        return "⚪ *Monitor not started yet*"
    if run.get("maintenance"):
        return "🛠️ *Portal under maintenance* — checking every 15 min"
    if run.get("error") or not run.get("ok"):
        return f"🟠 *Last check failed* ({age_text(finished)} ago) — retrying automatically"
    if time.time() - finished > MONITOR_STALE_SECONDS:
        return f"🟡 *Monitor paused* — last check {age_text(finished)} ago"
    return "🟢 *Live Monitor Active*"


# ------------------------------------------------------------------ views
# Each view returns (text, keyboard rows). Rows are lists of inline buttons.


BACK_ROW = [{"text": "🔙 Main Menu", "callback_data": "menu"}]


COURSES_ROW = [{"text": "🔙 My Courses & Schedule", "callback_data": "courses"},
               {"text": "🏠 Main Menu", "callback_data": "menu"}]


def _short(text: str, limit: int = 28) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# ---------------------------------------------------------------- sending

def api(method: str, **data):
    return notifier.call_api(BOT_TOKEN, method, data)


# ------------------------------------------------- button presses and chat actions

ANSWER_KEEP_SECONDS = 120
DEBOUNCE_SECONDS = 1.0           # the same button on the same message, pressed again within this, is a double tap
ACTION_REFRESH_SECONDS = 4.0     # Telegram shows "typing…" for 5 s, so it is renewed every 4

_Thread = threading.Thread          # the real class, kept at import (a test may swap threading.Thread for an inline stub)
_press_lock = threading.Lock()
_answered: dict[str, float] = {}
_last_press: dict[tuple, float] = {}


def answer(callback_id: str, text: str | None = None, alert: bool = False) -> bool:
    """Answer a button press — the small toast at the top of the chat (alert=True: a dialog).
    A press can be answered once; a second call is ignored (and costs nothing), so every
    route can answer without coordinating, and the handlers' own texts do not double up.
    True when THIS call is the one that answered."""
    now = time.monotonic()
    with _press_lock:
        for old in [i for i, t in _answered.items() if now - t > ANSWER_KEEP_SECONDS]:
            del _answered[old]
        if callback_id in _answered:
            return False
        _answered[callback_id] = now
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    if alert:
        data["show_alert"] = True
    if ANSWER_ASYNC:
        start_answer_workers()
        _answer_queue.put(data)                  # sent beside whatever the caller does next (the edit)
    else:
        api("answerCallbackQuery", **data)
    return True


# The answer to a press and the edit it causes are two Telegram calls of ~0.3 s each. Sent one after
# the other they add up; the answer therefore goes out from a standing worker thread while the
# caller carries on with the edit. Two standing workers rather than a thread per answer: no thread
# start-up per press, and their first call warms the connection pool every thread shares (telegram.py).
ANSWER_ASYNC = True
ANSWER_WORKERS = 2
_answer_queue: queue.Queue = queue.Queue()
_answer_started = False


def _answer_worker() -> None:
    api("getMe")                                 # open this worker's connection before the first press
    while True:
        data = _answer_queue.get()
        try:
            api("answerCallbackQuery", **data)
        except Exception:
            log.exception("Could not answer a button press")
        finally:
            _answer_queue.task_done()


def start_answer_workers() -> None:
    global _answer_started
    with _press_lock:
        if _answer_started:
            return
        _answer_started = True
    for n in range(ANSWER_WORKERS):
        _Thread(target=_answer_worker, name=f"answer-{n}", daemon=True).start()


def flush_answers(timeout: float = 5.0) -> bool:
    """Wait until every queued answer has been sent (tests, a clean shutdown). True when none is left."""
    end = time.monotonic() + timeout
    while _answer_queue.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.005)
    return not _answer_queue.unfinished_tasks


def is_double_tap(message_id, data: str) -> bool:
    """The same button of the same message pressed again within DEBOUNCE_SECONDS."""
    now = time.monotonic()
    key = (message_id, data)
    with _press_lock:
        for old in [k for k, t in _last_press.items() if now - t > 10 * DEBOUNCE_SECONDS]:
            del _last_press[old]
        again = key in _last_press and now - _last_press[key] < DEBOUNCE_SECONDS
        _last_press[key] = now
    return again


class working:
    """`with chat.working("upload_document"):` — the chat shows "sending a document…" (or
    "typing…", "sending a photo…") for as long as the block runs, instead of a message saying
    so. Telegram drops the status after 5 s, so a helper thread renews it. Never raises."""

    def __init__(self, action: str = "typing"):
        self.action = action
        self._stop = threading.Event()

    def _send(self) -> None:
        try:
            api("sendChatAction", chat_id=CHAT_ID, action=self.action)
        except Exception:
            pass

    def _renew(self) -> None:
        while not self._stop.wait(ACTION_REFRESH_SECONDS):
            self._send()

    def __enter__(self):
        self._send()
        _Thread(target=self._renew, name="chat-action", daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        return False


def with_action(action: str = "typing"):
    """Decorator form of `working` for a function that runs in a worker thread."""
    def decorate(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with working(action):
                return fn(*args, **kwargs)
        return inner
    return decorate


# ------------------------------------------------------------ the one menu
#
# A screen with buttons is THE menu: there is only ever one, and it is the last thing in the
# chat. Sending a new screen removes the previous one; pressing a button of an old screen
# (one that is no longer the menu) puts the answer at the bottom and removes the old screen;
# files and images the person asked for are followed by the menu again (reanchor()). Buttons
# on alerts and results (Join Meeting, Mark as Done, SMS code, ...) are not menus and are
# never touched. Automatic alerts that land below the menu leave it where it is: it moves to
# the bottom the next time the person uses the bot. Which message is the menu is kept in
# data/ui_state.json, so it survives a restart.

UI_FILE = paths.DATA / "ui_state.json"
_menu_lock = threading.Lock()
_menu: dict = {"message_id": None, "text": None, "rows": None}
_menu_loaded = False


def _load_menu() -> None:
    global _menu_loaded
    if _menu_loaded:
        return
    _menu_loaded = True
    try:
        data = json.loads(UI_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("message_id"), int):
            _menu.update(message_id=data["message_id"], text=data.get("text"), rows=data.get("rows"))
    except (OSError, ValueError):
        pass


def _save_menu() -> None:
    try:
        fileio.write_json_atomic(UI_FILE, _menu)
    except OSError:
        log.warning("Could not write %s", UI_FILE.name)


def reset_menu() -> None:
    """Forget which message is the menu (tests; a chat that was cleared)."""
    global _menu_loaded
    with _menu_lock:
        _menu.update(message_id=None, text=None, rows=None)
        _menu_loaded = True


def current_menu_id() -> int | None:
    with _menu_lock:
        _load_menu()
        return _menu["message_id"]


def retire(message_id: int | None) -> None:
    """Take an old screen out of the chat: deleted, or - when Telegram will not delete it (too
    old, already gone) - at least without its buttons."""
    if not message_id:
        return
    if api("deleteMessage", chat_id=CHAT_ID, message_id=message_id) is None:
        api("editMessageReplyMarkup", chat_id=CHAT_ID, message_id=message_id, reply_markup={"inline_keyboard": []})


def _post_menu(text: str, rows: list, silent: bool = False) -> dict | None:
    extra = {"disable_notification": True} if silent else {}
    sent = api("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="Markdown",
               disable_web_page_preview=True, reply_markup={"inline_keyboard": rows}, **extra)
    if isinstance(sent, dict) and sent.get("message_id"):
        with _menu_lock:
            _load_menu()
            old = _menu["message_id"]
            _menu.update(message_id=sent["message_id"], text=text, rows=rows)
            _save_menu()
        if old and old != sent["message_id"]:
            retire(old)
    return sent


def send_view(text: str, rows: list) -> dict | None:
    """A new screen at the bottom of the chat. With buttons it replaces the menu; without
    (a status line, a question being asked) it is a plain message and the menu stays."""
    if not rows:
        return api("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="Markdown",
                   disable_web_page_preview=True, reply_markup={"inline_keyboard": []})
    return _post_menu(text, rows)


def edit_view(message_id: int, text: str, rows: list) -> None:
    """Change a screen in place. A screen with buttons that is not the menu - an old one a
    button was pressed on, a status line that has finished - is not edited where it sits: the
    new screen goes to the bottom and the old one is removed."""
    with _menu_lock:
        _load_menu()
        active = _menu["message_id"]
    if rows and active not in (None, message_id):
        if _post_menu(text, rows):
            retire(message_id)
        return
    if api("editMessageText", chat_id=CHAT_ID, message_id=message_id, text=text, parse_mode="Markdown",
           disable_web_page_preview=True, reply_markup={"inline_keyboard": rows}) is None:
        return
    if message_id == active or (rows and active is None):
        with _menu_lock:
            _menu.update(message_id=message_id, text=text, rows=rows)
            _save_menu()


def reanchor() -> None:
    """Something was posted below the menu (the timetable files, a screenshot): send the menu
    again at the bottom, silently, and remove the old one. Nothing to do without a menu."""
    with _menu_lock:
        _load_menu()
        text, rows = _menu["text"], _menu["rows"]
        has_menu = bool(_menu["message_id"] and text and rows)
    if has_menu:
        _post_menu(text, rows, silent=True)
