"""
Open tasks: every Moodle assignment and quiz — and every Cambridge One assignment —
that still needs you, for the ⏳ Upcoming Tasks screen and the daily digest.

Open means
  • an assignment that is not submitted, with its due date ahead — or with no due
    date at all and not marked done;
  • a quiz not attempted yet whose closing time is ahead (or that has none);
  • a Cambridge One assignment with activities left and its due date ahead.
A due date that has passed is no longer a task: its reminders have had their turn.

The digest — every day at DIGEST_AT (noon), one message with the open tasks, the
closest first, so a long assignment is never forgotten while there is still time
to work on it. Whatever is due within LAST_DAY comes first, in a loud 🚨 block.
A quiz that has not opened yet is left out — there is nothing to do yet; it is in
the screen, and its own 24 h / 1 h reminders come before it opens. No open task,
no message. If the PC was off at noon, the first tick later that day sends it; a
missed day is never sent the next day. The bot sends it on its clock and a monitor
run is the fallback; a daily.claim makes sure it goes out once.
"""

import json
import logging
import time
from datetime import datetime, time as clock

from uninotifier import cambridge, daily, media, moodle, notifier
from uninotifier.notifier import escape_md, format_timestamp

log = logging.getLogger("uninotifier")

DIGEST_AT = clock(12, 0)
LAST_DAY = 24 * 3600
OPEN_BUTTONS = 8          # 🔗 Open Task buttons, closest first


def _task(kind: str, name: str, course: str, url: str | None, due: float | None,
          opens: float | None = None, progress: str | None = None) -> dict:
    return {"kind": kind, "name": name, "course": course, "url": url, "due": due, "opens": opens,
            "progress": progress}


def _moodle(state: dict, now: float) -> list[dict]:
    deadlines = state.get("deadlines") or {}
    undated = state.get("undated") or {}
    found = []
    for d in deadlines.values():
        if d.get("submitted"):
            continue
        name, course, url = d.get("name", "?"), d.get("course", ""), d.get("url")
        if d.get("type") == "assign":
            if d.get("timestamp", 0) > now:
                found.append(_task("assign", name, course, url, d["timestamp"]))
        elif d.get("type") == "quiz":
            opens, closes = moodle.quiz_times(d)
            if closes and closes > now:
                found.append(_task("quiz", name, course, url, closes, opens))
            elif not closes and opens and opens > now:      # only the opening time is known
                found.append(_task("quiz", name, course, url, None, opens))
    for item in (state.get("content") or {}).values():
        kind = item.get("type")
        if kind not in ("assign", "quiz"):
            continue
        key = f'{kind}-{moodle.extract_cmid(item.get("url", ""))}'
        if key in deadlines or item.get("done") or undated.get(key):
            continue
        found.append(_task(kind, item.get("name", "?"), item.get("course", ""), item.get("url"), None))
    return found


def _read_json(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cambridge(now: float) -> list[dict]:
    """Read as they are: cambridge.py owns these files (and repairs them), this only looks."""
    if not cambridge.active():
        return []
    seen = _read_json(cambridge.SEEN_FILE).get("seen") or {}
    feed = {"assignmentsData": _read_json(cambridge.FEED_FILE)}       # the feed as last read
    progress = {a["id"]: cambridge._progress(a) for a in cambridge.parse(feed)}
    url = cambridge.assignments_url()
    found = []
    for ident, a in seen.items():
        due = a.get("due")
        if a.get("submitted") is not True and due and due > now:
            found.append(_task("cambridge", a.get("title", "Assignment"), cambridge.COURSE, url, due,
                               progress=progress.get(ident, "not submitted yet")))
    return found


def open_tasks(state: dict, now: float | None = None) -> list[dict]:
    """Every open task, the closest date first; those without a date last."""
    now = time.time() if now is None else now
    found = _moodle(state, now) + _cambridge(now)
    return sorted(found, key=lambda t: (t["due"] or t["opens"] or float("inf"), t["name"]))


def is_open_now(task: dict, now: float) -> bool:
    """A quiz before its opening time can't be done yet."""
    return not (task["kind"] == "quiz" and task["opens"] and task["opens"] > now)


# ── text ─────────────────────────────────────────────────────────────────────────────────────

KIND = {"assign": "📝", "quiz": "📋", "cambridge": "🌐"}


def _span(ts: float, now: float) -> str:
    """'3d 4h', '5h 20m', '45m'."""
    days, rest = divmod(max(int(ts - now), 0), 86400)
    hours, minutes = rest // 3600, rest % 3600 // 60
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def task_lines(n: int, task: dict, now: float) -> list[str]:
    course = task["course"] if task["kind"] == "cambridge" else media.clean_course_name(task["course"])
    lines = [f"{n}. {KIND[task['kind']]} *{escape_md(task['name'])}*", f"    📘 *Course:* {escape_md(course)}"]
    if task["kind"] == "quiz" and task["opens"] and task["opens"] > now:
        lines.append(f"    🗓️ *Opens:* {format_timestamp(task['opens'])} · ⏱️ in {_span(task['opens'], now)}")
    if task["due"]:
        field = "Closes" if task["kind"] == "quiz" else "Due"
        lines.append(f"    🕐 *{field}:* {format_timestamp(task['due'])} · ⏱️ {_span(task['due'], now)} left")
    elif not (task["kind"] == "quiz" and task["opens"]):
        lines.append("    🕐 No due date set")
    if task["progress"]:
        lines.append(f"    📊 {escape_md(task['progress'])}")
    return lines


def open_buttons(tasks: list[dict]) -> list[list[dict]]:
    rows = []
    for n, task in enumerate(tasks, 1):
        if task["url"] and len(rows) < OPEN_BUTTONS:
            short = task["name"] if len(task["name"]) <= 28 else task["name"][:27].rstrip() + "…"
            rows.append([{"text": f"🔗 Open Task {n} · {short}", "url": task["url"]}])
    return rows


def digest(tasks: list[dict], now: float) -> tuple[str, list] | None:
    """The noon message, or None when nothing can be worked on today."""
    doable = [t for t in tasks if is_open_now(t, now)]
    if not doable:
        return None
    urgent = [t for t in doable if t["due"] and t["due"] - now <= LAST_DAY]
    rest = [t for t in doable if t not in urgent]
    ordered = urgent + rest
    lines = ["📋 *Your Open Tasks*", f"{len(doable)} still waiting for you — a little every day keeps them easy."]
    n = 0
    if urgent:
        lines += ["", "🚨🚨 *LAST DAY — DO NOT MISS THESE* 🚨🚨"]
        for task in urgent:
            n += 1
            lines += task_lines(n, task, now)
        lines.append("⚠️ *Submit before the time above — after it, Moodle may not accept it.*")
    if rest:
        lines += ["", "⏳ *Still open*"]
        for task in rest:
            n += 1
            lines += task_lines(n, task, now)
    return "\n".join(lines), open_buttons(ordered)


def digest_time(now: datetime | None = None) -> bool:
    return (now or datetime.now()).time() >= DIGEST_AT


def send_digest(token: str, chat_id: str, state: dict, now: datetime | None = None) -> bool:
    """Today's digest, once, from noon on (see the module's docstring)."""
    now = now or datetime.now()
    if not digest_time(now):
        return False
    message = digest(open_tasks(state, now.timestamp()), now.timestamp())
    if message is None:
        return False
    key = f"tasks-{now.date()}"
    if not daily.claim(key):
        return False
    text, rows = message
    if notifier.send_message(token, chat_id, text, rows=rows):
        log.info("Sent the open-tasks digest")
        return True
    daily.release(key)          # the next tick tries again
    return False
