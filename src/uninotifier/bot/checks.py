"""
A manual check (/check runs the monitor and reports what it found) and the
Microsoft verification code typed in reply.
"""

import logging
import os
import re
import subprocess
import sys
import threading

from uninotifier import failsafe
from uninotifier import media
from uninotifier import mfa
from uninotifier import paths
from uninotifier.bot import chat
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")


MONITOR = paths.MONITOR_SCRIPT


MONITOR_LOCK = paths.DATA / "monitor.lock"


EXIT_BUSY = 3            # monitor.py's "another run holds the lock"


CHECK_TIMEOUT = 15 * 60


_check_lock = threading.Lock()


def summary_text(code: int) -> str:
    run = chat.load_last_run()
    if code == EXIT_BUSY:
        return "⏳ *Another check is already running* — try again in a few minutes."
    if code == 0 and run.get("maintenance"):
        return ("🛠️ *The e-learning portal is under maintenance* — the screenshot is above; "
                "checks carry on automatically.")
    if code != 0 or not run.get("ok"):
        return "❌ *Check Failed* — see `data/logs/monitor.log` for the reason."
    lines = [
        "✅ *Check Complete*", "",
        f'📚 *Courses:* {run.get("courses", 0)}   📦 *Items:* {run.get("items", 0)}',
        f'🆕 *New:* {len(run.get("new_items", [])) + run.get("new_courses", 0)}   '
        f'🔄 *Updated files:* {run.get("updated_files", 0)}',
        f'📢 *New posts:* {run.get("new_posts", 0)}   ✏️ *Edited:* {run.get("edited", 0)}',
        f'⏳ *Pending tasks:* {run.get("pending", 0)}',
    ]
    if run.get("seconds") is not None:
        lines.append(f'⏱️ *Took:* {run["seconds"]:g} s')
    for it in run.get("new_items", [])[:8]:
        emoji = media.CONTENT_TYPE_LABELS.get(it["type"], ("📎", ""))[0]
        lines.append(f'   {emoji} {escape_md(it["name"])} — {escape_md(it["course"])}')
    if len(run.get("new_items", [])) > 8:
        lines.append(f'   … +{len(run["new_items"]) - 8}')
    if not run.get("new_items") and not run.get("new_courses"):
        lines.append("\nNothing new 👌")
    return "\n".join(lines)


# ------------------------------------------------------------------ check

@chat.with_action("typing")
def run_check(message_id: int | None) -> None:
    """Runs in a worker thread: the polling loop stays responsive meanwhile."""
    if not _check_lock.acquire(blocking=False):
        return
    try:
        wait_text, rows = "🔄 *Checking the portal…*\nThis may take a minute or more.", []
        if message_id is None:
            sent = chat.send_view(wait_text, rows)
            message_id = sent["message_id"] if isinstance(sent, dict) else None
        else:
            chat.edit_view(message_id, wait_text, rows)

        log.info("Manual check started")
        try:
            code = subprocess.run(
                [sys.executable, str(MONITOR)], cwd=paths.ROOT, capture_output=True,
                timeout=CHECK_TIMEOUT, stdin=subprocess.DEVNULL,
                # the user is right here: a Microsoft verification may be relayed now
                env={**os.environ, "UNINOTIFIER_MANUAL": "1"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode
        except subprocess.TimeoutExpired:
            code = 1
        log.info("Manual check finished (exit %s)", code)

        result = (summary_text(code), [[{"text": "⏳ Tasks & Reminders", "callback_data": "tasks"}], chat.BACK_ROW])
        if message_id:
            chat.edit_view(message_id, *result)
        else:
            chat.send_view(*result)
    except Exception as e:
        log.exception("Manual check failed")
        failsafe.bug("bot:check", "/check failed", e)
        try:                                       # never leave "Checking the portal…" on screen
            failed = ("❌ *Check Failed* — something went wrong; it has been reported.", [chat.BACK_ROW])
            chat.edit_view(message_id, *failed) if message_id else chat.send_view(*failed)
        except Exception:
            log.exception("Could not show that the check failed")
    finally:
        _check_lock.release()


def start_check(message_id: int | None) -> bool:
    if _check_lock.locked():
        return False
    threading.Thread(target=run_check, args=(message_id,), daemon=True).start()
    return True


# -------------------------------------------------------------- dispatching

CODE_RE = re.compile(r"\d{4,8}")


def handle_mfa_code(text: str) -> bool:
    """A reply with just a verification code, while the monitor waits for
    one, is handed over to it. Returns True when the text was consumed."""
    digits = re.sub(r"[\s-]", "", text)
    req = mfa.read_request()
    if not (req and req.get("expects") == "code" and CODE_RE.fullmatch(digits)):
        return False
    mfa.answer_request(code=digits)
    chat.send_view("🔑 *Code received* — entering it on the Microsoft sign-in page…", [])
    return True
