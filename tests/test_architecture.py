"""The whole suite, offline: no portal, no Telegram, nothing of yours read or written.

Run:  .venv\\Scripts\\python tests\\test_architecture.py

The tests live beside this file, one file per topic (TOPICS); support.py holds what they share. Every
function named test_* in a topic file is run, in the file's order — there is no list to forget a
test in.
"""

import importlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import GUARD_HITS, _REMINDER_TMP, _package, heartbeat      # noqa: E402 - sets the throw-away home up first

TOPICS = ("test_cambridge",
          "test_security_and_privacy",
          "test_project",
          "test_reliability",
          "test_telegram_transport",
          "test_bot_chat",
          "test_lectures_and_schedule",
          "test_tasks_and_reminders",
          "test_grades_and_gpa",
          "test_portal",
          "test_archive",
          "test_moodle_reading")


def collect() -> list:
    """Every test of every topic file, in order."""
    found = []
    for topic in TOPICS:
        module = importlib.import_module(topic)
        found += [fn for name, fn in vars(module).items()
                  if name.startswith("test_") and callable(fn) and fn.__module__ == topic]
    return found


if __name__ == "__main__":
    _HEARTBEAT = heartbeat.FILE
    _CHAT_ORIGINALS = {n: getattr(_package("uninotifier.bot"), n) for n in ("send_view", "edit_view", "api")}
    for fn in collect():
        _chat = _package("uninotifier.bot")
        for _name, _original in _CHAT_ORIGINALS.items():      # a test that forgets to restore a stub cannot leak it into the next
            setattr(_chat, _name, _original)
        _chat._answered.clear()           # the tests reuse made-up button ids; Telegram's are unique
        _chat._last_press.clear()
        _chat.UI_FILE = _REMINDER_TMP / "ui_state.json"       # which message is the menu: never the real file
        _chat.reset_menu()
        _chat.MONITOR = _REMINDER_TMP / "never-run-the-real-monitor.py"     # /check in a test must not start a real check
        shutil.rmtree(_chat.CACHE_DIR, ignore_errors=True)    # a portal answer saved by one test is not the next one's
        importlib.import_module("uninotifier.portal").drop_session()   # ... nor its portal session
        _chat.ANSWER_ASYNC = False          # answers are sent in line, so a test can read them at once (test_buttons_are_answered_first covers the worker)
        _HEARTBEAT.unlink(missing_ok=True)   # a bot pulse left by one test would look like a dead bot to the next one's check
        _chat.DEBOUNCE_SECONDS = 0          # tests press the same button back to back; test_buttons_are_answered_first checks the debounce
        fn()
        if GUARD_HITS:                     # caught even when the code under test swallowed the refusal
            raise SystemExit(f"FAIL {fn.__name__} tried to reach the outside world: {GUARD_HITS}")
        print("ok", fn.__name__)
