"""
🎒 Attendance & Absences in the chat: the question after your last lecture ("did you go today?"),
the screen with every course's count and its limit, and the warnings (attendance.py has the rules).

Buttons: att:ask:<date> · att:all:<date> · att:none:<date> · att:pick:<date> · att:t:<date>:<n> ·
att:save:<date> · att:home · att:today · att:undo:<n>
"""

import logging
import time
from datetime import date, datetime

from uninotifier import attendance, failsafe, media, notifier
from uninotifier.bot import chat

log = logging.getLogger("uninotifier")

TICK = 60                                   # seconds between looks at whether the question is due
HOME_ROW = [{"text": "🎒 Attendance", "callback_data": "att:home"}, {"text": "🏠 Main Menu", "callback_data": "menu"}]


def _day_name(day: date) -> str:
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def _clock(hhmm: str) -> str:
    hour, minute = map(int, hhmm.split(":"))
    return notifier.clock12(hour, minute)


def _course_lines(day: date) -> list[str]:
    return [f"• {lec['course']} · {_clock(lec['start'])}–{_clock(lec['end'])}" for lec in attendance.classes_on(day)]


# ------------------------------------------------------------------ the question

def view_ask(day: date) -> tuple[str, list]:
    stamp = day.isoformat()
    lines = [f"🎒 *Attendance* — {_day_name(day)}", media.DIVIDER, *_course_lines(day), "",
             "Did you go to university today?"]
    return "\n".join(lines), [
        [{"text": "✅ I attended everything", "callback_data": f"att:all:{stamp}"}],
        [{"text": "🎯 I missed some", "callback_data": f"att:pick:{stamp}"}],
        [{"text": "❌ I didn't go today", "callback_data": f"att:none:{stamp}"}]]


def view_pick(day: date) -> tuple[str, list]:
    stamp = day.isoformat()
    courses = attendance.courses_on(day)
    chosen = set(attendance.load()["pick"].get(stamp, []))
    lines = [f"🎯 *Which did you miss?* — {_day_name(day)}", media.DIVIDER,
             "Tap the courses you were absent from, then 💾 Save."]
    rows = [[{"text": ("❌ " if course in chosen else "✅ ") + course[:40], "callback_data": f"att:t:{stamp}:{i}"}]
            for i, course in enumerate(courses)]
    rows.append([{"text": "💾 Save", "callback_data": f"att:save:{stamp}"},
                 {"text": "🔙 Back", "callback_data": f"att:ask:{stamp}"}])
    return "\n".join(lines), rows


def view_result(day: date) -> tuple[str, list]:
    kind = attendance.kinds()
    data = attendance.load()
    absent = [c for c in attendance.courses_on(day) if day.isoformat() in data["absences"].get(c, [])]
    head = "✅ *Saved* — present in everything" if not absent else f"✅ *Saved* — absent from {', '.join(absent)}"
    lines = [f"🎒 {head}", media.DIVIDER]
    lines += [w for c in absent if (w := attendance.warning(c, kind[c], data))] or ["No warnings."]
    return "\n".join(lines), [[{"text": "✏️ Change today's answer", "callback_data": f"att:ask:{day.isoformat()}"}],
                              HOME_ROW]


# ------------------------------------------------------------------ the screen

def view_home() -> tuple[str, list]:
    kind = attendance.kinds()
    data = attendance.load()
    if not kind:
        return ("🎒 *Attendance & Absences*\n" + media.DIVIDER + "\nNo timetable yet, so there is nothing to count. "
                "It appears once your timetable is read.", [chat.BACK_ROW])
    names = sorted(kind)
    lines = ["🎒 *Attendance & Absences*", media.DIVIDER]
    rows = []
    for i, course in enumerate(names):
        info = attendance.status(course, kind[course], data)
        mark = {"ok": "🟢", "last": "🟠", "limit": "🔴", "deprived": "🚫"}[info["state"]]
        tag = " · blended: in-person days only" if kind[course] == "blended" else ""
        lines.append(f"{mark} *{course}* — {info['count']} of {info['allowed']}{tag}")
        if info["count"]:
            rows.append([{"text": f"↩️ Undo the last absence: {course[:28]}", "callback_data": f"att:undo:{i}"}])
    lines += ["", "Normal course: 5 allowed, the 6th deprives. Blended: 2 allowed, the 3rd deprives."]
    return "\n".join(lines), [[{"text": "➕ Record today", "callback_data": "att:today"}], *rows, chat.BACK_ROW]


# ------------------------------------------------------------------ buttons

def _show(message_id: int | None, view: tuple[str, list]) -> None:
    chat.edit_view(message_id, *view) if message_id else chat.send_view(*view)


def handle_callback(cb: dict, action: str, message_id: int | None) -> None:
    chat.answer(cb["id"])
    parts = action.split(":")
    what = parts[1] if len(parts) > 1 else "home"
    if chat.is_file_message(cb.get("message")):
        message_id = None
    if what == "home":
        _show(message_id, view_home())
        return
    if what == "today":
        day = date.today()
        _show(message_id, view_ask(day) if attendance.courses_on(day)
              else ("🎒 *Attendance*\nNo counted lectures today.", [HOME_ROW]))
        return
    if what == "undo":
        names = sorted(attendance.kinds())
        index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        if 0 <= index < len(names):
            attendance.remove_one(names[index])
        _show(message_id, view_home())
        return
    try:
        day = date.fromisoformat(parts[2])
    except (IndexError, ValueError):
        _show(message_id, view_home())
        return
    stamp = day.isoformat()
    courses = attendance.courses_on(day)
    if what == "ask":
        _show(message_id, view_ask(day))
    elif what == "pick":
        _show(message_id, view_pick(day))
    elif what == "t" and len(parts) > 3 and parts[3].isdigit() and int(parts[3]) < len(courses):
        data = attendance.load()
        chosen = data["pick"].setdefault(stamp, [])
        course = courses[int(parts[3])]
        chosen.remove(course) if course in chosen else chosen.append(course)
        attendance.save(data)
        _show(message_id, view_pick(day))
    elif what in ("all", "none", "save"):
        absent = (courses if what == "none" else [] if what == "all"
                  else [c for c in courses if c in attendance.load()["pick"].get(stamp, [])])
        attendance.record_day(day, absent)
        _show(message_id, view_result(day))


def start() -> None:
    """/attendance"""
    chat.send_view(*view_home())


# ------------------------------------------------------------------ the clock

def tick(now: datetime | None = None) -> bool:
    day = attendance.due(now)
    if day is None:
        return False
    chat.send_view(*view_ask(day))
    attendance.mark_asked(day, now)
    return True


def loop() -> None:
    """The question comes an hour after the last counted lecture of the day, once more three hours later
    if unanswered; nothing is ever recorded without an answer."""
    while True:
        try:
            tick()
        except Exception as e:
            log.exception("The attendance clock failed")
            failsafe.bug("bot:attendance", "The attendance question could not be sent", e)
        time.sleep(TICK)
