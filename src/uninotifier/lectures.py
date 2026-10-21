"""
Sends the lecture messages that schedule.due_messages says are due.

Each online lecture produces two messages:
  ⏰ one hour before — plain text, no buttons;
  🎥 at the start    — the actionable alert: 🎥 Join Meeting, 🌐 Open Course Page
                       and a screenshot of the post that published the link
                       (or a note that no link has been posted yet).

Two processes can send these — the always-running bot, which checks every
few seconds so a message lands on the minute, and the scheduled monitor as a
fallback when the bot is down. Whoever claims a message first sends it: a claim
is an exclusively-created marker file, which is atomic, so the other one skips
it. A message that fails to send releases its claim and is retried on the next
tick. Needs no portal access.
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from uninotifier import archive
from uninotifier import failsafe
from uninotifier import lecturelinks
from uninotifier import media
from uninotifier import notifier, paths
from uninotifier import schedule

log = logging.getLogger("uninotifier")

CLAIM_DIR = paths.DATA / "lecture_sent"
STATE_FILE = paths.DATA / "saved_items.json"     # the attendance activities, from the monitor
CLAIM_KEEP_SECONDS = 3 * 86400
NO_LINK_NOTE = ("🔗 No meeting link has been posted yet — I'll send it as soon as it appears, "
                "and the next reminder will carry it.")


def _marker(key: str) -> Path:
    return CLAIM_DIR / (re.sub(r"[^\w.-]+", "_", key) + ".sent")


def claim(key: str) -> bool:
    """True for exactly one caller per key."""
    CLAIM_DIR.mkdir(exist_ok=True)
    try:
        os.close(os.open(_marker(key), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def release(key: str) -> None:
    _marker(key).unlink(missing_ok=True)


def prune() -> None:
    cutoff = time.time() - CLAIM_KEEP_SECONDS
    for path in CLAIM_DIR.glob("*.sent") if CLAIM_DIR.is_dir() else []:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def attendance_rows(course: str) -> list[list[dict]]:
    """✍️ Check In for every attendance activity the course has on Moodle —
    whichever online course it is; several activities each get a button."""
    try:
        content = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("content", {})
    except (OSError, ValueError):
        return []
    found = [it for it in content.values() if it.get("type") in media.ATTENDANCE_TYPES
             and media.clean_course_name(it.get("course", "")) == course]
    many = len(found) > 1
    return [[{"text": media.CHECK_IN_BUTTON + (f" · {it['name']}" if many else ""), "url": it["url"]}]
            for it in found]


def send_with_link(token: str, chat_id: str, text: str, course: str, hint: bool = True,
                   check_in: bool = False) -> bool:
    """A lecture message with its meeting buttons (🎥 Join Meeting, 🌐 Open
    Course Page) and, when the link came from the portal, the screenshot of the
    post or section that published it; check_in adds the course's ✍️ Check In.
    Text and attendance only when there is no link; text only if the image
    can't be sent."""
    link = lecturelinks.entry(course)
    attendance = attendance_rows(course) if check_in else []
    if not link.get("url"):
        return notifier.send_message(token, chat_id, f"{text}\n\n{NO_LINK_NOTE}", rows=attendance or None)
    rows = lecturelinks.join_rows(link) + attendance
    if hint:
        text += "\n\nTap *Join Meeting* below."
    png = lecturelinks.shot_bytes(course)
    if png:
        text += "\n📸 The post that published the link is attached."
        if len(text) <= notifier.CAPTION_LIMIT and notifier.send_png_file(
                token, chat_id, png, f"{archive.safe_name(course, 'lecture')} - link source.png",
                text, rows=rows):
            return True
    return notifier.send_message(token, chat_id, text, rows=rows)


def send_due(token: str, chat_id: str, now: datetime | None = None) -> int:
    """Send every lecture message that is due and not yet sent; returns how
    many went out."""
    sent = 0
    for key, kind, text, course in schedule.due_messages(schedule.load_schedule(), now or datetime.now()):
        if not claim(key):
            continue
        try:
            ok = (send_with_link(token, chat_id, text, course, check_in=True) if kind == "start"
                  else notifier.send_message(token, chat_id, text))
        except Exception as e:
            log.exception("Lecture message %s failed", key)
            failsafe.bug("lectures", "A lecture alert could not be sent", e)
            ok = False
        if ok:
            sent += 1
            log.info("Sent lecture message: %s", key)
        else:
            release(key)       # try again on the next tick
    prune()
    return sent
