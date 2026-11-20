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
FOUND_FILE = paths.DATA / "midterm_found.json"      # marks the monitor found on Moodle (written only by the monitor)
MIDTERM_WORDS = ("midterm", "mid-term", "mid term", "first exam", "1st exam", "ميد", "منتصف", "نصفي", "الامتحان الاول", "الامتحان الأول", "امتحان اول", "امتحان أول")
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


def enabled(state: dict | None = None) -> bool:
    """The watch is on unless you turned it off (the midterm period is over, or has not begun)."""
    return (load() if state is None else state).get("enabled", True) is not False


def set_enabled(on: bool) -> None:
    state = load()
    state["enabled"] = bool(on)
    state["checked"] = 0                                     # switched on: the first look comes right away
    save(state)


def due(now: float | None = None, state: dict | None = None) -> bool:
    state = load() if state is None else state
    return enabled(state) and (time.time() if now is None else now) - float(state.get("checked") or 0) >= INTERVAL_SECONDS


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


DEFAULT_OUT_OF = 30.0                      # a midterm is out of 30 at the university: what a typed "24" is out of


def hundred(got: float, out_of: float) -> float:
    """The mark as a figure out of 100: 24 of 30 -> 80."""
    return got / out_of * 100 if out_of else 0.0


def parse_mark(text: str) -> tuple[float, float]:
    """'24', '24/30', '24 of 30', '24 من 30' -> (24.0, 30.0). A bare number is out of DEFAULT_OUT_OF.
    Raises ValueError with what to send instead."""
    import re
    numbers = re.findall(r"\d+(?:\.\d+)?", (text or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    if len(numbers) not in (1, 2):
        raise ValueError("Send the mark, e.g. 24 (out of 30) or 24/25 (out of 25).")
    got = float(numbers[0])
    out_of = float(numbers[1]) if len(numbers) == 2 else DEFAULT_OUT_OF
    if out_of <= 0 or out_of > 100:
        raise ValueError("A midterm is out of 1 to 100 — e.g. 24/30.")
    if got > out_of:
        raise ValueError(f"{got:g} is more than {out_of:g} — send it as mark/out of, e.g. 24/30.")
    return got, out_of


def set_manual(course: str, got: float, out_of: float, now: float | None = None) -> None:
    """A mark you know yourself (a doctor posted it somewhere else): it is the one used until you remove it."""
    state = load()
    state.setdefault("manual", {})[course] = {"got": got, "out_of": out_of, "at": time.time() if now is None else now}
    save(state)


def clear_manual(course: str) -> bool:
    state = load()
    found = (state.get("manual") or {}).pop(course, None)
    if found:
        save(state)
    return bool(found)


def _found() -> dict:
    try:
        found = json.loads(FOUND_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        found = {}
    return found if isinstance(found, dict) else {}


def midterm_mark_from_moodle(course: str, titles: list[str], summary: str) -> dict | None:
    """A mark found in a file or post on Moodle (grades.extract_grade's summary, e.g. '24/30'): when what it was found in
    is named like a midterm, it is kept as that course's midterm mark. Returns {"got", "out_of"} or None."""
    import re
    named = " ".join(titles).lower()
    if not any(word in named for word in MIDTERM_WORDS):
        return None
    found = re.search(r"(\d+(?:\.\d+)?)\s*(?:/|من|of)\s*(\d+(?:\.\d+)?)", (summary or "").translate(
        str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    if not found:
        return None
    got, out_of = float(found.group(1)), float(found.group(2))
    if not 0 < out_of <= 100 or got > out_of:
        return None
    marks = _found()
    marks[course] = {"got": got, "out_of": out_of, "item": " / ".join(t for t in titles if t)[:120], "at": time.time()}
    fileio.write_json_atomic(FOUND_FILE, marks)
    return {"got": got, "out_of": out_of}


def effective(state: dict, course: str) -> dict | None:
    """The mark in use for a course: {"got", "out_of", "source": "you" | "site" | "moodle", "exam"} — what you entered
    wins, then the exams site's, then a mark found in a file or post on Moodle — or None while there is none."""
    mine = (state.get("manual") or {}).get(course)
    if mine:
        return {"got": mine["got"], "out_of": mine["out_of"], "source": "you", "exam": None}
    site = (state["courses"].get(course) or {}).get("mark")
    if site:
        return {**site, "source": "site"}
    moodle = _found().get(course)
    return {"got": moodle["got"], "out_of": moodle["out_of"], "source": "moodle", "exam": moodle.get("item")} if moodle else None


def apply_to_gpa(state: dict | None = None) -> list[str]:
    """Hand every course's mark in use to the grade calculator (gpa.json: "midterm"), so what a course can
    still reach is known from now on. A course with no mark loses an old one. Returns the courses changed."""
    from uninotifier import gpa
    state = load() if state is None else state
    record = gpa.load()
    changed = []
    for name, course in record["courses"].items():
        mark = effective(state, name)
        wanted = {"got": mark["got"], "out_of": mark["out_of"]} if mark else None
        if course.get("midterm") != wanted:
            if wanted:
                course["midterm"] = wanted
            else:
                course.pop("midterm", None)
            changed.append(name)
    if changed:
        gpa.save(record)
    return changed


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
                         "got": exams.number(row["grade"]), "out_of": row["max"],
                         "url": row["url"] or course["url"], "course_url": course["url"]})
        first_mark = next((r for r in rows if exams.number(r["grade"]) is not None and r.get("max")), None)
        kept[course["course"]] = {"overview": course["grade"], "items": items,
                                  **({"mark": {"got": exams.number(first_mark["grade"]), "out_of": first_mark["max"],
                                               "exam": first_mark["name"]}} if first_mark else {})}
    new_state = {**state, "courses": kept, "started": state.get("started") or time.time(), "checked": time.time()}
    return news, new_state


def message(item: dict) -> str:
    changed = item["was"] is not None
    lines = ["✏️ *Midterm Mark Changed*" if changed else "🎉 *Midterm Mark Posted*", "",
             media.course_line(item["course"]),
             f"📝 *Exam:* {escape_md(item['exam'])}",
             f"🏆 *Mark:* *{escape_md(item['mark'])}*"]
    if changed:
        was = exams.number(item["was"])
        lines.append(f"↩️ *It was:* {escape_md(f'{was:g}' if was is not None else item['was'])}")
    if item.get("got") is not None and item.get("out_of"):
        lines.append(f"💯 *Out of 100:* *{round(hundred(item['got'], item['out_of']), 1):g} / 100*")
    if item.get("best") is not None:
        lines.append(f"🔝 *Best possible in the course:* *{item['best']:g}*")
    if item.get("yours"):
        lines.append(f"✍️ _You entered {item['yours']} yourself — that one stays in use until you remove it._")
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
