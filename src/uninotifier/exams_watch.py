"""
Midterm marks watch: the bot reads the exams site (exams.py) every so often and tells you when a mark of any course
appears or changes — the exam, the mark out of its maximum, the percentage.

The first look only remembers what is there (and tells you once that the watch is on, with how many courses already
have a mark); after that a message goes out for each exam whose mark is new, and for one that changed (was → now).
A course the site lists for the first time later is news only if it already holds a mark. A look that fails (the
site is down, the sign-in refused) changes nothing and is tried again next time.

State: data/exams_watch.json — {"started": when, "checked": when, "courses": {course: {"overview": grade,
"items": {exam: grade}}}}.
"""

import json
import time

from uninotifier import exams, fileio, media, paths
from uninotifier.notifier import escape_md

STATE_FILE = paths.DATA / "exams_watch.json"
INTERVAL_SECONDS = 15 * 60


def load() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state = state if isinstance(state, dict) else {}
    if not isinstance(state.get("courses"), dict):
        state["courses"] = {}
    return state


def save(state: dict) -> None:
    fileio.write_json_atomic(STATE_FILE, state)


def due(now: float | None = None, state: dict | None = None) -> bool:
    state = load() if state is None else state
    return (time.time() if now is None else now) - float(state.get("checked") or 0) >= INTERVAL_SECONDS


def skipped(now: float | None = None) -> None:
    """A look that could not be made: remembered as tried, so it is not repeated every minute."""
    state = load()
    state["checked"] = time.time() if now is None else now
    save(state)


def _mark(grade: str, top: float | None, percent: str = "") -> str:
    """'24 / 30 (80%)'."""
    value = exams.number(grade)
    if value is None:
        return grade
    text = f"{value:g}" + (f" / {top:g}" if top else "")
    pct = exams.number(percent)
    return text + (f" ({pct:g}%)" if pct is not None and top else "")


def compare(state: dict, courses: list[dict]) -> tuple[list[dict], dict]:
    """(the news, the new state). News: {"course", "exam", "was" | None, "now", "mark", "url"} — one per exam whose
    mark is new or changed (a course with no exam rows: its course total). The first look is no news."""
    first = not state.get("started")
    news, kept = [], {}
    for course in courses:
        before = state["courses"].get(course["course"]) or {}
        old_items = dict(before.get("items") or {})
        report = course.get("report")
        if report is None:                                   # not read again: unchanged since the last look
            kept[course["course"]] = before or {"overview": course["grade"], "items": {}}
            continue
        items = {}
        rows = report["items"] or ([{"name": course["course"], "grade": course["grade"], "max": (report["total"] or {}).get("max"),
                                     "percent": (report["total"] or {}).get("percent", ""), "url": course["url"]}]
                                   if course["grade"] != exams.NO_GRADE else [])
        for row in rows:
            items[row["name"]] = row["grade"]
            if exams.number(row["grade"]) is None:
                continue
            was = old_items.get(row["name"])
            if was == row["grade"] or first:
                continue
            news.append({"course": course["course"], "exam": row["name"],
                         "was": was if exams.number(was or "") is not None else None, "now": row["grade"],
                         "mark": _mark(row["grade"], row["max"], row["percent"]),
                         "url": row["url"] or course["url"], "course_url": course["url"]})
        kept[course["course"]] = {"overview": course["grade"], "items": items}
    new_state = {**state, "courses": kept, "started": state.get("started") or time.time(), "checked": time.time()}
    return news, new_state


def message(item: dict, detected_at: float | None = None) -> str:
    changed = item["was"] is not None
    lines = ["✏️ *Midterm Mark Changed*" if changed else "🎉 *Midterm Mark Posted*", "",
             media.course_line(item["course"]),
             f"📝 *Exam:* {escape_md(item['exam'])}",
             f"🏆 *Mark:* *{escape_md(item['mark'])}*"]
    if changed:
        was = exams.number(item["was"])
        lines.append(f"↩️ *It was:* {escape_md(f'{was:g}' if was is not None else item['was'])}")
    lines.append(media._audit(detected_at))
    return "\n".join(lines)


def intro(courses: list[dict]) -> str:
    """Said once, when the watch first looks: what is already out and what is awaited."""
    graded = [c for c in courses if exams.number(c["grade"]) is not None]
    waiting = [c for c in courses if exams.number(c["grade"]) is None]
    lines = ["👀 *Midterm Marks Watch is on*", "",
             "I look at the exams site every 15 minutes and tell you the moment a mark appears."]
    if graded:
        lines += ["", f"✅ *Already out ({len(graded)}):*"] + [f"• {escape_md(c['course'])} — {escape_md(_mark(c['grade'], ((c.get('report') or {}).get('total') or {}).get('max')))}" for c in graded]
    if waiting:
        lines += ["", f"⏳ *Waiting for ({len(waiting)}):*"] + [f"• {escape_md(c['course'])}" for c in waiting]
    return "\n".join(lines)
