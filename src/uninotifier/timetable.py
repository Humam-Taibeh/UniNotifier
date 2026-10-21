"""
The course list as a timetable: pure text, built from schedule.lectures_for().

  - courses are grouped by their pattern of campus days (Sun · Tue together, Mon · Wed together) and,
    inside a group, listed in the order of their lectures, earliest first;
  - under a course: the lecture time, and branching from it what each day is — said ONCE when the days
    are alike ("└ 🏫 In-Person" with its 📍 room as a branch of its own below), one branch per day when they
    differ ("├ Sun — 🏫 In-Person / │ └ 📍 room / └ Tue — 🧩 Activity");
  - words are short: In-Person, Online, Activity (a lab stays "Lab"); a room is "📍 Room <number>" and
    nothing else (no building or faculty name, no block prefix), and only for a session held in a room;
  - any day outside the campus days (a Saturday lab, should there be one) is listed as its own time.

This is THE layout of a lecture everywhere in the bot: the course list, the lecture alerts and the morning
summary all build their lines here.
"""

import re

from uninotifier.notifier import escape_md, hhmm12
from uninotifier.schedule import DAY_ABBR, MODE_EMOJI

WEEK = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
CAMPUS_DAYS = ("sun", "mon", "tue", "wed", "thu")
BRANCH, LAST = "├", "└"
REMOTE_MODES = ("Activity", "Online")      # self-study and remote: no room
ROOM_WORD = re.compile(r"room\s*([A-Za-z0-9\-]+)", re.I)
NB = "\u00a0"                      # indentation that survives: Telegram may trim ordinary leading spaces


def pad(n: int) -> str:
    return NB * n


IND = pad(4)                       # what a course's lines are indented by in the list


def _split(slots: list[tuple[str, dict]]) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    main = [s for s in slots if s[0] in CAMPUS_DAYS]
    return main, [s for s in slots if s[0] not in CAMPUS_DAYS]


def pattern(slots: list[tuple[str, dict]]) -> tuple[str, ...]:
    """The campus days a course meets on, in week order: ("sun", "tue")."""
    main, _ = _split(slots)
    return tuple(day for day in CAMPUS_DAYS if any(d == day for d, _ in main))


def order_key(slots: list[tuple[str, dict]]) -> tuple:
    """Sorts groups by their first day and courses within a group by the start of their first lecture;
    a course with no campus lecture goes after those that have one, one without a timetable last."""
    days = pattern(slots)
    if days:
        main, _ = _split(slots)
        return (0, [WEEK.index(d) for d in days], min(lec["start"] for _, lec in main))
    return (1 if slots else 2, [], min((lec["start"] for _, lec in slots), default=""))


def group_title(days: tuple[str, ...]) -> str:
    return " · ".join(DAY_ABBR[d] for d in days) if days else "Other days"


def time_range(lec: dict) -> str:
    """'9:30–11:00 AM' (the AM/PM said once when both ends share it), '11:00 AM–12:30 PM' otherwise."""
    start, end = hhmm12(lec["start"]), hhmm12(lec["end"])
    return f"{start[:-3]}–{end}" if start[-2:] == end[-2:] else f"{start}–{end}"


def _kind(lec: dict) -> str:
    word = "Lab" if "lab" in (lec.get("label") or "").lower() else lec["mode"]
    return f"{MODE_EMOJI.get(lec['mode'], '')} {escape_md(word)}".strip()


def room(place: str | None) -> str:
    """A place as 'Room <number>' — nothing else, so a location line is always short and never wraps on a
    phone: 'Building 5, Room 9147' -> 'Room 9147', 'Engineering College, Room 19123' -> 'Room 19123',
    'Room 60-9104' -> 'Room 9104' (a numeric block prefix goes; a lettered room like 'A-12' is kept as it is).
    A place that names no room is left as written."""
    if not place or not place.strip():
        return ""
    found = ROOM_WORD.search(place)
    if not found:
        return place.strip()
    token = found.group(1)
    block = re.fullmatch(r"\d{1,3}-(\d+)", token)
    return f"Room {block.group(1) if block else token}"


def _place(lec: dict) -> str:
    """The room line of a lecture — never for a session that is not in a room (Activity, Online)."""
    if lec["mode"] in REMOTE_MODES or not room(lec.get("place")):
        return ""
    return f"📍 {escape_md(room(lec.get('place')))}"


def _entry(days: list[str], lecs: list[dict]) -> list[str]:
    """One time with its branches: the days that meet at this time and what each of them is. The kind of
    session and the room are on separate lines — side by side they wrap on a phone — and the room hangs
    from the kind with a branch of its own, so it can never look detached from it."""
    head = f"🕘 {' · '.join(DAY_ABBR[d] for d in days)} · {time_range(lecs[0])}"
    kinds, places = [_kind(l) for l in lecs], [_place(l) for l in lecs]
    if len(set(zip(kinds, places))) == 1:                                           # alike: said once
        return [head, f"{pad(3)}{LAST} {kinds[0]}"] + ([f"{pad(5)}{LAST} {places[0]}"] if places[0] else [])
    lines = [head]
    for i, (d, kind, place) in enumerate(zip(days, kinds, places)):
        last = i == len(days) - 1
        lines.append(f"{pad(3)}{LAST if last else BRANCH} {DAY_ABBR[d]} — {kind}")
        if place:
            lines.append(f"{pad(3)}{NB if last else '│'}{pad(2)}{LAST} {place}")
    return lines


def lecture_lines(lec: dict) -> list[str]:
    """The same template for ONE lecture of today (an alert, the morning summary): the hour, then the kind of
    session, then its room hanging from it."""
    place = _place(lec)
    return [f"🕘 {time_range(lec)}", f"{pad(3)}{LAST} {_kind(lec)}"] + ([f"{pad(5)}{LAST} {place}"] if place else [])


def describe(slots: list[tuple[str, dict]]) -> list[str]:
    """The lines of one course's timetable (without the course name, instructor or section)."""
    main, extra = _split(slots)
    per_day: dict[str, list[dict]] = {}
    for day, lec in main:
        per_day.setdefault(day, []).append(lec)
    days = [d for d in CAMPUS_DAYS if d in per_day]
    lines: list[str] = []
    one_a_day = bool(days) and all(len(per_day[d]) == 1 for d in days)
    if one_a_day and len({(per_day[d][0]["start"], per_day[d][0]["end"]) for d in days}) == 1:
        lines += _entry(days, [per_day[d][0] for d in days])                       # the same hour on every day
    else:                                                                          # different hours: one entry each
        for day, lec in main:
            lines += _entry([day], [lec])
    for day, lec in extra:
        lines += _entry([day], [lec])
    return lines
