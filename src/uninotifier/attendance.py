"""
Absences, counted for you. After your last lecture of a day the bot asks whether you went; what you
answer is kept here, per course, and you are warned before a course deprives you.

The rules (ZUJ, as given by the owner):
  - a normal course: 5 absences are allowed, the 6th is a deprivation;
  - a blended course (it has an In-Person day AND an Online day in the timetable): only the In-Person
    day counts — the Online day is never asked about — 2 absences are allowed, the 3rd deprives;
  - an Activity slot is never counted.

The timetable comes from schedule.load_schedule() (your schedule.json or the portal's), so this works
for any student. Nothing here sends anything: the bot's attendance_flow asks and shows. The file is
data/attendance.json: {"absences": {course: [dates]}, "answered": {date: "all"|"none"|"some"},
"asked": {date: {"times": n, "at": ts}}, "pick": {date: [courses]}}.
"""

import json
from datetime import date, datetime, timedelta

from uninotifier import fileio, paths, schedule

FILE = paths.DATA / "attendance.json"
ALLOWED = {"normal": 5, "blended": 2}            # absences allowed; one more deprives
COUNTED_MODES = ("In-Person", "Online")
ASK_AFTER_MINUTES = 60                           # asked this long after the day's last lecture ends
ASK_AGAIN_HOURS = 3                              # asked once more if there was no answer
ASK_TIMES = 2


def load() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in ("absences", "answered", "asked", "pick"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def save(data: dict) -> None:
    fileio.write_json_atomic(FILE, data)


def _slots(sched: dict | None = None) -> list[tuple[str, dict]]:
    sched = sched if sched is not None else schedule.load_schedule()
    return [(day, lec) for day, lectures in sched.get("days", {}).items() for lec in lectures]


def kinds(sched: dict | None = None) -> dict[str, str]:
    """Every course with a counted slot -> "normal" or "blended": a course that meets In-Person one day
    and not in person (Online or an Activity day) another is blended — only its In-Person day counts."""
    modes: dict[str, set] = {}
    for _, lec in _slots(sched):
        modes.setdefault(lec["course"], set()).add(lec.get("mode"))
    return {course: "blended" if "In-Person" in found and len(found) > 1 else "normal"
            for course, found in modes.items() if found & set(COUNTED_MODES)}


def classes_on(day: date, sched: dict | None = None) -> list[dict]:
    """The lectures of that date that count towards absences, in time order (a blended course's
    Online day and any Activity are left out)."""
    sched = sched if sched is not None else schedule.load_schedule()
    kind = kinds(sched)
    key = schedule.DAY_KEYS[day.weekday()]
    return sorted((lec for lec in sched.get("days", {}).get(key, [])
                   if lec.get("mode") in COUNTED_MODES and lec["course"] in kind
                   and not (kind[lec["course"]] == "blended" and lec["mode"] != "In-Person")),
                  key=lambda lec: lec["start"])


def courses_on(day: date, sched: dict | None = None) -> list[str]:
    """The courses that count that day, once each, in time order."""
    seen: list[str] = []
    for lec in classes_on(day, sched):
        if lec["course"] not in seen:
            seen.append(lec["course"])
    return seen


def day_over_at(day: date, sched: dict | None = None) -> datetime | None:
    """When the bot asks about that day: an hour after its last counted lecture ends."""
    lectures = classes_on(day, sched)
    if not lectures:
        return None
    hour, minute = map(int, max(lec["end"] for lec in lectures).split(":"))
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute) \
        + timedelta(minutes=ASK_AFTER_MINUTES)


def count(course: str, data: dict | None = None) -> int:
    return len((data or load())["absences"].get(course, []))


def status(course: str, kind: str, data: dict | None = None) -> dict:
    """{"count", "allowed", "left", "state"}: state is "ok", "last" (one absence left), "limit" (none
    left — the next one deprives) or "deprived"."""
    used, allowed = count(course, data), ALLOWED[kind]
    left = allowed - used
    state = "deprived" if left < 0 else "limit" if left == 0 else "last" if left == 1 else "ok"
    return {"count": used, "allowed": allowed, "left": max(left, 0), "state": state}


def record_day(day: date, absent: list[str], sched: dict | None = None) -> list[str]:
    """Settle one date: those courses are absent, the other counted courses of that day are attended.
    Idempotent — answering the same day again replaces the earlier answer. Returns the courses whose
    absence was newly added."""
    stamp = day.isoformat()
    data = load()
    today_courses = courses_on(day, sched)
    added = []
    for course in today_courses:
        days = [d for d in data["absences"].get(course, []) if d != stamp]
        if course in absent:
            days.append(stamp)
            if stamp not in data["absences"].get(course, []):
                added.append(course)
        if days:
            data["absences"][course] = sorted(days)
        else:
            data["absences"].pop(course, None)
    data["answered"][stamp] = ("none" if absent and len(absent) >= len(today_courses)
                               else "some" if absent else "all")
    data["pick"].pop(stamp, None)
    save(data)
    return added


def remove_one(course: str) -> bool:
    """Take back the latest absence of a course (a mistaken answer)."""
    data = load()
    days = data["absences"].get(course) or []
    if not days:
        return False
    gone = days.pop()
    if days:
        data["absences"][course] = days
    else:
        data["absences"].pop(course, None)
    data["answered"].pop(gone, None)
    save(data)
    return True


def due(now: datetime | None = None, sched: dict | None = None) -> date | None:
    """Today's date when the question about today is to be asked right now, else None."""
    now = now or datetime.now()
    day = now.date()
    stamp = day.isoformat()
    data = load()
    if stamp in data["answered"]:
        return None
    over = day_over_at(day, sched)
    if over is None or now < over:
        return None
    asked = data["asked"].get(stamp) or {}
    if asked.get("times", 0) >= ASK_TIMES:
        return None
    if asked and now.timestamp() - asked.get("at", 0) < ASK_AGAIN_HOURS * 3600:
        return None
    return day


def mark_asked(day: date, now: datetime | None = None) -> None:
    data = load()
    asked = data["asked"].setdefault(day.isoformat(), {"times": 0, "at": 0})
    asked["times"] += 1
    asked["at"] = (now or datetime.now()).timestamp()
    save(data)


def warning(course: str, kind: str, data: dict | None = None) -> str | None:
    info = status(course, kind, data)
    if info["state"] == "deprived":
        return f"🚫 *{course}* — you are over the limit ({info['count']} of {info['allowed']}): deprivation"
    if info["state"] == "limit":
        return f"🚨 *{course}* — {info['count']} of {info['allowed']}: no absences left, the next one deprives you"
    if info["state"] == "last":
        return f"⚠️ *{course}* — {info['count']} of {info['allowed']}: ONE absence left before deprivation"
    return None
