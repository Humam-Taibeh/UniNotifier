"""
Absences, counted for you. After your last lecture of a day the bot asks whether you went; what you
answer is kept here, per course, and you are warned before a course deprives you.

The rules (ZUJ, as given by the owner):
  - a normal course: 5 absences are allowed, the 6th is a deprivation;
  - a blended course — the portal marks its campus day Blended (م), or it meets on campus one day and not
    on campus (Online / Activity) another, or it is a FREE elective (the study plan's "حرة" category,
    found by its course number): only the campus day counts — the other day is never asked about —
    2 absences are allowed, the 3rd deprives;
  - a course attended once a week (one counted day, e.g. an online course with an Activity day): the
    same as a blended one — 2 allowed, the 3rd deprives;
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
ALLOWED = {"normal": 5, "blended": 2, "once": 2}   # absences allowed; one more deprives
CAMPUS_MODES = ("In-Person", "Blended")           # "Blended" is the portal's own mark (م) for a blended course's campus day
COUNTED_MODES = CAMPUS_MODES + ("Online",)
ASK_AFTER_MINUTES = 60                           # asked this long after the day's last lecture ends
ASK_AGAIN_AT = (22, 0)                           # asked once more at 10 PM if there was no answer (never sooner than
ASK_AGAIN_GAP_MINUTES = 60                       # an hour after the first time: a late lecture's question is not repeated at once)
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
    """Every course with a counted slot -> "normal" or "blended". Blended is: a slot the portal marks
    Blended (م); or a course that meets on campus one day and not on campus (Online, an Activity day)
    another; or a free elective (a course of the study plan's "حرة" category) — only its campus days count."""
    sched = sched if sched is not None else schedule.load_schedule()
    modes: dict[str, set] = {}
    for _, lec in _slots(sched):
        modes.setdefault(lec["course"], set()).add(lec.get("mode"))
    free = free_courses(sched)
    days: dict[str, set] = {}
    for day, lec in _slots(sched):
        if lec.get("mode") in COUNTED_MODES:
            days.setdefault(lec["course"], set()).add(day)
    return {course: ("blended" if ("Blended" in found or "In-Person" in found and len(found) > 1 or course in free)
                     else "once" if len(days.get(course, ())) == 1 else "normal")
            for course, found in modes.items() if found & set(COUNTED_MODES)}


def free_codes() -> set[str]:
    """The course numbers of the study plan's free-elective category ("حرة" / "Free"), from the last
    transcript the bot read. Empty when no plan has been read or it has no such category."""
    try:
        from uninotifier.bot import portal_flow
        cached = portal_flow.load_cached("transcript")
        categories = ((cached[0].data or {}).get("plan") or {}).get("categories", []) if cached else []
    except Exception:
        return set()
    return {str(c["code"]) for cat in categories
            if "حرة" in str(cat.get("name", "")) or "free" in str(cat.get("name", "")).lower()
            for c in cat.get("courses", [])}


def free_courses(sched: dict) -> set[str]:
    """The timetable's course names that are free electives (the schedule's course_codes: number -> name)."""
    codes = free_codes()
    return {name for code, name in (sched.get("course_codes") or {}).items() if str(code) in codes}


def classes_on(day: date, sched: dict | None = None) -> list[dict]:
    """The lectures of that date that count towards absences, in time order (a blended course's
    Online day and any Activity are left out)."""
    sched = sched if sched is not None else schedule.load_schedule()
    kind = kinds(sched)
    key = schedule.DAY_KEYS[day.weekday()]
    campus = {lec["course"] for _, lec in _slots(sched) if lec.get("mode") in CAMPUS_MODES}
    return sorted((lec for lec in sched.get("days", {}).get(key, [])
                   if lec.get("mode") in COUNTED_MODES and lec["course"] in kind
                   and not (kind[lec["course"]] == "blended" and lec["mode"] not in CAMPUS_MODES
                            and lec["course"] in campus)),
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
    if asked:
        again = datetime.combine(day, datetime.min.time()).replace(hour=ASK_AGAIN_AT[0], minute=ASK_AGAIN_AT[1])
        if now < again or now.timestamp() - asked.get("at", 0) < ASK_AGAIN_GAP_MINUTES * 60:
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
    if info["state"] == "ok":
        return None
    return f"{MARK[info['state']]} *{course}* — {info['count']} of {info['allowed']} · {state_text(info)}"


MARK = {"ok": "🟢", "last": "🟠", "limit": "🔴", "deprived": "🚫"}


def state_text(info: dict) -> str:
    """The state in words."""
    if info["state"] == "deprived":
        return "*Deprived* — over the limit"
    if info["state"] == "limit":
        return "no absences left — the next one means *deprivation*"
    if info["state"] == "last":
        return "ONE absence left before *deprivation*"
    return f"{info['left']} left"


def set_count(course: str, number: int) -> int:
    """Make the course's absences `number` (earlier, undated ones are added or removed first)."""
    number = max(int(number), 0)
    while count(course) < number:
        adjust(course, 1)
    while count(course) > number:
        adjust(course, -1)
    return count(course)


EARLIER = "earlier-"            # an absence from before the bot counted: kept as a count, with no date


def adjust(course: str, delta: int) -> int:
    """One absence more (an earlier one, no date) or one fewer (an earlier one first, else the latest).
    Never below 0. Returns the new count."""
    data = load()
    days = list(data["absences"].get(course, []))
    if delta > 0:
        number = 1
        while f"{EARLIER}{number}" in days:
            number += 1
        days.append(f"{EARLIER}{number}")
    elif days:
        earlier = [d for d in days if d.startswith(EARLIER)]
        gone = earlier[-1] if earlier else days[-1]
        days.remove(gone)
        data["answered"].pop(gone, None)
    if days:
        data["absences"][course] = sorted(days)
    else:
        data["absences"].pop(course, None)
    save(data)
    return len(days)


def ordered(sched: dict | None = None) -> list[str]:
    """The counted courses in the order of the week: the one whose first lecture comes first (Sunday
    morning first) at the top — never alphabetical."""
    sched = sched if sched is not None else schedule.load_schedule()
    counted = kinds(sched)
    return [course for course in schedule.course_order(sched) if course in counted]


def bar(used: int, allowed: int) -> str:
    """▰▰▱▱▱ — the absences used of the ones allowed (a course over its limit shows the extra ones filled)."""
    return "▰" * used + "▱" * max(allowed - used, 0)
