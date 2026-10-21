"""
Class-schedule reminders, driven by schedule.json:
  - a text-only reminder N minutes before each online lecture
  - an actionable alert when it starts
  - a morning summary on class days (sent once, before the first lecture)

Pure logic: `due_messages` returns what is due for a given moment; lectures.py
sends it (adding the join link to the start-of-lecture alert) and makes sure
nothing repeats.
"""

import copy
import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta
from functools import lru_cache

from uninotifier import failsafe, fileio, paths
from uninotifier.archive import course_folder_name
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")

SCHEDULE_FILE = paths.SCHEDULE_FILE
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # datetime.weekday() order
DAY_NAMES = {"sun": "Sunday", "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
             "thu": "Thursday", "fri": "Friday", "sat": "Saturday"}
MODE_EMOJI = {"In-Person": "🏫", "Blended": "🔀", "Online": "💻", "Activity": "🧩"}


LAST_GOOD_FILE = paths.DATA / "schedule.last_good.json"     # the newest copy that passed validate()

KNOWN_KEYS = {"semester", "lecture_reminder_minutes", "morning_summary_hour", "remind_modes", "link_modes",
              "portal_aliases", "course_codes", "teachers", "days", "finals", "degree_hours"}
_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _text_map(value, allow_list: bool = False) -> bool:
    ok = lambda v: isinstance(v, str) or (allow_list and isinstance(v, list) and all(isinstance(i, str) for i in v))
    return isinstance(value, dict) and all(isinstance(k, str) and ok(v) for k, v in value.items())


def validate(data) -> list[str]:
    """Everything wrong with a parsed schedule.json, as plain sentences (an empty
    list = fine). Strict on purpose: a misspelt setting or a lecture without an end
    time would otherwise be ignored — or raise — and take the reminders with it."""
    if not isinstance(data, dict):
        return ["the file must hold one JSON object ({ ... })"]
    errors = [f'unknown setting "{key}" (misspelt?)' for key in data
              if not key.startswith("_") and key not in KNOWN_KEYS]

    days = data.get("days")
    if not isinstance(days, dict):
        errors.append('"days" is missing or is not an object')
        days = {}
    for day, lectures in days.items():
        if day not in DAY_KEYS:
            errors.append(f'"days": unknown day "{day}" (use {", ".join(DAY_KEYS)})')
            continue
        if not isinstance(lectures, list):
            errors.append(f'"{day}" must be a list of lectures')
            continue
        for n, lec in enumerate(lectures, 1):
            where = f"{day} #{n}"
            if not isinstance(lec, dict):
                errors.append(f"{where}: must be an object")
                continue
            for field in ("start", "end", "course", "course_ar", "section", "mode"):
                if not isinstance(lec.get(field), str) or not lec[field].strip():
                    errors.append(f'{where}: "{field}" is missing')
            times = [lec.get(f) for f in ("start", "end")]
            for field, value in zip(("start", "end"), times):
                if isinstance(value, str) and value.strip() and not _TIME.match(value):
                    errors.append(f'{where}: "{field}" must look like 09:30 (24-hour), not "{value}"')
            if all(isinstance(t, str) and _TIME.match(t) for t in times) and times[0] >= times[1]:
                errors.append(f"{where}: it ends ({times[1]}) before it starts ({times[0]})")
            if isinstance(lec.get("mode"), str) and lec["mode"] not in MODE_EMOJI:
                errors.append(f'{where}: "mode" must be one of {", ".join(MODE_EMOJI)}, not "{lec["mode"]}"')
            for field in ("label", "place"):
                if field in lec and not isinstance(lec[field], str):
                    errors.append(f'{where}: "{field}" must be text')

    minutes = data.get("lecture_reminder_minutes", 60)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 720:
        errors.append('"lecture_reminder_minutes" must be a whole number from 1 to 720')
    hour = data.get("morning_summary_hour")
    if hour is not None and (isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23):
        errors.append('"morning_summary_hour" must be null or a whole number from 0 to 23')
    for key in ("remind_modes", "link_modes"):
        if key in data and not (isinstance(data[key], list) and all(m in MODE_EMOJI for m in data[key])):
            errors.append(f'"{key}" must be a list of: {", ".join(MODE_EMOJI)}')
    degree = data.get("degree_hours")
    if "degree_hours" in data and (isinstance(degree, bool) or not isinstance(degree, (int, float)) or not 30 <= degree <= 400):
        errors.append('"degree_hours" must be the number of credit hours of the whole degree plan (30 to 400)')
    if "semester" in data and not isinstance(data["semester"], str):
        errors.append('"semester" must be text')
    for key in ("portal_aliases", "course_codes"):
        if key in data and not _text_map(data[key]):
            errors.append(f'"{key}" must map text to text')
    if "teachers" in data and not _text_map(data["teachers"], allow_list=True):
        errors.append('"teachers" must map a course to a name (or a list of names)')

    if "finals" in data:
        finals = data["finals"]
        if not isinstance(finals, dict):
            errors.append('"finals" must be an object: {"from": "2027-01-04", "to": "2027-01-31", ...}')
        else:
            try:
                start, end = date.fromisoformat(finals["from"]), date.fromisoformat(finals["to"])
                if end < start:
                    errors.append('"finals": "to" is before "from"')
            except (KeyError, TypeError, ValueError):
                errors.append('"finals" needs "from" and "to" as YYYY-MM-DD dates')
            interval = finals.get("interval_minutes", 30)
            if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 1440:
                errors.append('"finals": "interval_minutes" must be a whole number from 1 to 1440')
    return errors


_read = {"path": None, "text": None, "data": None}   # the last file content seen and the schedule it gave
_good: dict | None = None                            # the newest valid schedule seen by this process
_broken = False                                      # the file on disk is currently reported as broken


def _last_good_from_disk() -> dict | None:
    try:
        data = json.loads(LAST_GOOD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if not validate(data) else None


def load_schedule() -> dict:
    """The schedule in force. A file that is missing, is not valid JSON or fails
    validate() does NOT switch the reminders off: the last good copy stays in force
    and you are told on Telegram what to fix (once per version of the broken file —
    and again when it is valid). Only with no earlier copy at all is the schedule
    empty. Reading is by content, so an edit takes effect on the next call."""
    global _good, _broken
    path = str(SCHEDULE_FILE)
    try:
        text = SCHEDULE_FILE.read_text(encoding="utf-8")
    except OSError as e:
        if _read["path"] == path and _read["text"] is None and _read["data"] is not None:
            return copy.deepcopy(_read["data"])           # still missing: already reported
        text, problems = None, [f"there is no {SCHEDULE_FILE.name} yet — copy {paths.SCHEDULE_EXAMPLE.name} to "
                                f"{SCHEDULE_FILE.name} and put your own timetable in it"
                                if isinstance(e, FileNotFoundError) else f"the file cannot be read ({type(e).__name__})"]
    else:
        if _read["path"] == path and _read["text"] == text:
            return copy.deepcopy(_read["data"])
        try:
            data = json.loads(text)
            problems = validate(data)
        except ValueError as e:
            problems = [f"not valid JSON — {e}"]

    if not problems:
        _good = data
        _read.update(path=path, text=text, data=data)
        _arabic_to_english.cache_clear()
        try:
            fileio.write_json_atomic(LAST_GOOD_FILE, data)
        except OSError:
            log.warning("Could not keep a copy of the good schedule.json")
        if _broken:
            _broken = False
            failsafe.resolve("schedule", "✅ *schedule.json is valid again* — lecture reminders use it now.", wait=False)
        return copy.deepcopy(data)

    fallback = _good or _last_good_from_disk()
    result = fallback or {"days": {}}
    _read.update(path=path, text=text, data=result)
    _arabic_to_english.cache_clear()
    _broken = True
    fingerprint = hashlib.sha1((text or "<missing>").encode("utf-8")).hexdigest()[:10]
    log.error("schedule.json is invalid (%s) — %s", "; ".join(problems[:3]),
              "using the last good copy" if fallback else "no earlier copy: reminders are off")
    failsafe.report(
        "schedule", "schedule.json needs fixing",
        detail=("Lecture reminders keep using the last good copy until you fix it:" if fallback else
                "There is no earlier copy, so lecture reminders are OFF until you fix it:")
               + "\n" + "\n".join(f"• {p}" for p in problems[:8]) + (f"\n• … and {len(problems) - 8} more" if len(problems) > 8 else ""),
        fingerprint=fingerprint, wait=False)
    return copy.deepcopy(result)


def _at(day: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def _line(lec: dict) -> str:
    """One lecture in the morning summary: the hour with the course and section, then the kind and the room
    as branches (the layout lives in timetable.py)."""
    from uninotifier import timetable
    hour, *rest = timetable.lecture_lines(lec)
    return "\n".join([f'{hour} · *{escape_md(lec["course"])}* · Section {lec["section"]}', *rest])


def _when(lec: dict) -> str:
    """When, and what kind of session, and where — the same layout as the course list."""
    from uninotifier import timetable
    return "\n".join(timetable.lecture_lines(lec))


def _in(minutes: int) -> str:
    """'1 hour', '45 min' — a reminder a few minutes late still reads naturally."""
    return "1 hour" if minutes >= 55 else f"{max(1, minutes)} min"


def due_messages(schedule: dict, now: datetime) -> list[tuple[str, str, str, str | None]]:
    """What is due right now, as (dedupe key, kind, text, course):

      pre      a text-only heads-up `lecture_reminder_minutes` before the lecture
      start    the actionable alert from the lecture's start until it ends — the
               caller adds the Join / Course Page buttons and the link screenshot
      summary  a morning overview of the day's lectures (only if enabled)

    Only modes in `remind_modes` (online sessions) are announced. Each key
    appears once per day and lecture, so a message already sent is never
    repeated. Pure logic: nothing is sent here."""
    day_key = DAY_KEYS[now.weekday()]
    lectures = [l for l in schedule.get("days", {}).get(day_key, [])
                if l["mode"] in schedule.get("remind_modes", ["Online"])]
    if not lectures:
        return []

    today = now.strftime("%Y-%m-%d")
    out = []

    hour = schedule.get("morning_summary_hour")
    if hour is not None and _at(now, f"{hour:02d}:00") <= now < _at(now, lectures[0]["start"]):
        text = f"🌅 *Today's Schedule — {DAY_NAMES[day_key]}*\n\n" + "\n".join(_line(l) for l in lectures)
        out.append((f"{today}-summary", "summary", text, None))

    lead = timedelta(minutes=schedule.get("lecture_reminder_minutes", 60))
    for lec in lectures:
        start, end = _at(now, lec["start"]), _at(now, lec["end"])
        course = escape_md(lec["course"])
        if start - lead <= now < start:
            minutes = round((start - now).total_seconds() / 60)
            out.append((f'{today}-pre-{lec["start"]}-{lec["course"]}', "pre",
                        f'⏰ *Reminder:* {course} starts in {_in(minutes)}\n{_when(lec)}', lec["course"]))
        elif start <= now < end:
            late = int((now - start).total_seconds() // 60)
            head = (f"🎥 *Lecture starting now:* {course}" if late < 5
                    else f"🎥 *Lecture in progress:* {course} (started {late} min ago)")
            out.append((f'{today}-start-{lec["start"]}-{lec["course"]}', "start",
                        f"{head}\n{_when(lec)}", lec["course"]))
    return out


@lru_cache(maxsize=1)
def _arabic_to_english() -> dict[str, str]:
    lectures = (l for day in load_schedule().get("days", {}).values() for l in day)
    return {l["course_ar"]: l["course"] for l in lectures}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def canonical_course(name: str) -> str:
    """Map a portal course name to the English name used in schedule.json:
    Arabic names via their Arabic title, English ones via spacing-insensitive
    match plus the `portal_aliases` table. Unknown names pass through."""
    if name in _arabic_to_english():
        return _arabic_to_english()[name]
    known = {_norm(en): en for en in _arabic_to_english().values()}
    alias = load_schedule().get("portal_aliases", {}).get(name, name)
    return known.get(_norm(alias), name)


def teachers_for(course_en: str, section: str | None, by_course: dict, courses: dict) -> list[str]:
    """Instructor names for a course. A `teachers` entry in schedule.json wins;
    otherwise the portal's teachers, narrowed to the user's section when the
    portal tags teachers with groups like '0101221_2'."""
    manual = load_schedule().get("teachers", {}).get(course_en)
    if manual:
        return [manual] if isinstance(manual, str) else list(manual)
    for cid, course in courses.items():
        if canonical_course(course_folder_name(course["name"])) != course_en:
            continue
        listed = by_course.get(cid, [])
        mine = [t for t in listed if section and any(
            g.strip().rsplit("_", 1)[-1].lstrip("0") == section.lstrip("0")
            for g in t.get("groups", "").split(","))]
        return [t["name"].title() if t["name"].islower() else t["name"] for t in (mine or listed)]
    return []


def section_of(course_en: str) -> str | None:
    """The user's section number for a course (schedule.json is per-section)."""
    for day in load_schedule().get("days", {}).values():
        for lec in day:
            if lec["course"] == course_en:
                return lec["section"]
    return None


DAY_ABBR = {"sun": "Sun", "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat"}


def lectures_for(course_en: str) -> list[tuple[str, dict]]:
    """Every weekly slot of a course as (day_key, lecture), in week order."""
    days = load_schedule().get("days", {})
    return [(day, lec) for day in DAY_KEYS[6:] + DAY_KEYS[:6]
            for lec in days.get(day, []) if lec["course"] == course_en]
