"""
🎒 Attendance & Absences in the chat: the question after your last lecture ("did you go today?"),
the screen with every course's count and its limit, and the warnings (attendance.py has the rules).

Every screen ends with the way back (the screen before it, then 🏠 Main Menu). In Slack the courses are
lines — a section each, its words whole — with a drop-down to set the number or a button to toggle;
anywhere else they are numbered, with ordinary buttons.

Buttons: att:ask:<date> · att:all:<date> · att:none:<date> · att:pick:<date> · att:t:<date>:<n> ·
att:save:<date> · att:home · att:today · att:set · att:to:<n>:<count> · att:adj:<n>:<up|down>
"""

import logging
import time
from datetime import date, datetime

from uninotifier import attendance, failsafe, media, notifier, slack
from uninotifier.bot import chat

log = logging.getLogger("uninotifier")

TICK = 60                                   # seconds between looks at whether the question is due
MAIN = {"text": "🏠 Main Menu", "callback_data": "menu"}
ATTENDANCE = {"text": "🔙 Attendance", "callback_data": "att:home"}
KIND_TAG = {"blended": " · blended", "once": " · once a week", "normal": ""}


def _in_slack() -> bool:
    return slack.mode() == "slack"


def _day_name(day: date) -> str:
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def _clock(hhmm: str) -> str:
    hour, minute = map(int, hhmm.split(":"))
    return notifier.clock12(hour, minute)


def _course_lines(day: date) -> list[str]:
    return [f"• {lec['course']} · {_clock(lec['start'])}–{_clock(lec['end'])}" + (" · online" if lec.get("mode") == "Online" else "")
            for lec in attendance.classes_on(day)]


def _course_text(i: int, course: str, kind: str, info: dict) -> str:
    return (f"{attendance.MARK[info['state']]} *{i + 1}. {course}*{KIND_TAG[kind]}\n"
            f"{attendance.bar(info['count'], info['allowed'])}  {info['count']} of {info['allowed']} · "
            f"{attendance.state_text(info)}")


# ------------------------------------------------------------------ the question

def view_ask(day: date) -> tuple[str, list]:
    """The question fits the day: a day on campus asks whether you went to university, a day of online
    lectures whether you attended; one course is attended or missed (no "some"); several can be missed in part."""
    stamp = day.isoformat()
    courses = attendance.courses_on(day)
    campus = any(lec.get("mode") in attendance.CAMPUS_MODES for lec in attendance.classes_on(day))
    if len(courses) == 1:
        question = "Did you go to your lecture?" if campus else "Did you attend your online lecture?"
        rows = [[{"text": "✅ Attended", "callback_data": f"att:all:{stamp}"},
                 {"text": "❌ Absent", "callback_data": f"att:none:{stamp}"}]]
    else:
        question = "Did you go to university today?" if campus else "Did you attend your online lectures today?"
        rows = [[{"text": "✅ Attended all", "callback_data": f"att:all:{stamp}"},
                 {"text": "🎯 Missed some", "callback_data": f"att:pick:{stamp}"},
                 {"text": "❌ Didn't go" if campus else "❌ Missed all", "callback_data": f"att:none:{stamp}"}]]
    lines = [f"🎒 *Attendance* — {_day_name(day)}", media.DIVIDER, *_course_lines(day), "", question]
    return "\n".join(lines), [*rows, [ATTENDANCE, MAIN]]


def view_recorded(day: date) -> tuple[str, list]:
    """Opened again after you answered: what was recorded (and when), and the way to change it."""
    data = attendance.load()
    stamp = day.isoformat()
    absent = [c for c in attendance.courses_on(day) if stamp in data["absences"].get(c, [])]
    said = (f"absent from {', '.join(absent)}" if absent else "present in everything")
    at = data["answered_at"].get(stamp)
    when = f" at {notifier.clock12(*datetime.fromtimestamp(at).timetuple()[3:5])}" if at else ""
    lines = [f"🎒 *Attendance* — {_day_name(day)}", media.DIVIDER, *_course_lines(day), "",
             f"✅ *Already recorded{when}:* {said}", "", "Do you want to change it?"]
    return "\n".join(lines), [[{"text": "✏️ Change it", "callback_data": f"att:ask:{stamp}"}], [ATTENDANCE, MAIN]]


def view_today() -> tuple[str, list]:
    """Record today: what was already answered (and a way to change it); before the day's last lecture ends, when the question comes;
    after it, the question itself."""
    day = date.today()
    if not attendance.courses_on(day):
        return "🎒 *Attendance*\nYou have no lectures today, so there is nothing to record.", [[ATTENDANCE, MAIN]]
    if day.isoformat() in attendance.load()["answered"]:
        return view_recorded(day)
    over = attendance.day_over_at(day)
    if over and datetime.now() < over:
        lines = [f"🎒 *Attendance* — {_day_name(day)}", media.DIVIDER, *_course_lines(day), "",
                 f"⏳ Your last lecture today ends at {_clock(over.strftime('%H:%M'))}, so I'll ask you then.",
                 "Record it now anyway?"]
        return "\n".join(lines), [[{"text": "✅ Record now", "callback_data": f"att:ask:{day.isoformat()}"}], [ATTENDANCE, MAIN]]
    return view_ask(day)


def view_pick(day: date) -> tuple[str, list]:
    stamp = day.isoformat()
    courses = attendance.courses_on(day)
    chosen = set(attendance.load()["pick"].get(stamp, []))
    times = {lec["course"]: f"{_clock(lec['start'])}–{_clock(lec['end'])}" for lec in reversed(attendance.classes_on(day))}
    lines = [f"🎯 *Which did you miss?* — {_day_name(day)}", media.DIVIDER,
             "Tap a course to switch it between present and absent, then save."]
    rows = []
    for i, course in enumerate(courses):
        absent = course in chosen
        button = {"text": "❌ Absent" if absent else "✅ Present", "callback_data": f"att:t:{stamp}:{i}"}
        if _in_slack():
            button["line"] = {"text": f"*{i + 1}. {course}*\n{times.get(course, '')}", "style": "danger" if absent else None}
        else:
            button["text"] = ("❌ " if absent else "✅ ") + course
        rows.append([button])
    rows.append([{"text": "✅ Save", "callback_data": f"att:save:{stamp}"}])
    rows.append([{"text": "🔙 Back", "callback_data": f"att:ask:{stamp}"}, MAIN])
    return "\n".join(lines), rows


def view_result(day: date) -> tuple[str, list]:
    kind = attendance.kinds()
    data = attendance.load()
    absent = [c for c in attendance.courses_on(day) if day.isoformat() in data["absences"].get(c, [])]
    head = "✅ *Saved* — present in everything" if not absent else f"✅ *Saved* — absent from {', '.join(absent)}"
    lines = [f"🎒 {head}", media.DIVIDER]
    lines += [w for c in absent if (w := attendance.warning(c, kind[c], data))] or ["No warnings."]
    return "\n".join(lines), [[{"text": "✏️ Change today's answer", "callback_data": f"att:ask:{day.isoformat()}"}],
                              [ATTENDANCE, MAIN]]


# ------------------------------------------------------------------ the screens

def view_home() -> tuple[str, list]:
    kind = attendance.kinds()
    data = attendance.load()
    if not kind:
        return ("🎒 *Attendance & Absences*\n" + media.DIVIDER + "\nNo timetable yet, so there is nothing to count. "
                "It appears once your timetable is read.", [chat.COURSES_ROW])
    lines = ["🎒 *Attendance & Absences*", media.DIVIDER]
    for i, course in enumerate(attendance.ordered()):
        lines += [_course_text(i, course, kind[course], attendance.status(course, kind[course], data)), ""]
    return "\n".join(lines).rstrip(), [[{"text": "➕ Record today", "callback_data": "att:today"},
                               {"text": "📝 Earlier absences", "callback_data": "att:set"},
                               {"text": "⚙️ Limits", "callback_data": "att:lim"}], chat.COURSES_ROW]


def view_limits() -> tuple[str, list]:
    """The absences allowed in each course: the rule's own number (auto), or the one you set — the next absence deprives."""
    data = attendance.load()
    kind = attendance.kinds()
    lines = ["⚙️ *Absences allowed*", media.DIVIDER,
             "Auto follows the rule; or set a course's own limit — the next absence deprives you."]
    rows = []
    for i, course in enumerate(attendance.ordered()):
        auto = attendance.ALLOWED[kind[course]]
        mine = data["limits"].get(course)
        now = int(mine or auto)
        if _in_slack():
            options = [[f"Auto ({auto})", f"att:lim:{i}:auto"]] + [[f"{n} allowed", f"att:lim:{i}:{n}"] for n in attendance.LIMIT_CHOICES]
            rows.append([{"text": "Limit", "callback_data": f"att:lim:{i}:{mine or 'auto'}", "line": {
                "text": f"*{i + 1}. {course}*\n{now} allowed" + (" · set by you" if mine else " · auto"),
                "options": options, "initial": f"att:lim:{i}:{mine or 'auto'}"}}])
        else:
            lines.append(f"*{i + 1}.* {course} — {now} allowed" + (" (set by you)" if mine else " (auto)"))
            rows.append([{"text": f"{i + 1}: " + ("✅ " if (mine or 0) == n else "") + str(n), "callback_data": f"att:lim:{i}:{n}"}
                         for n in attendance.LIMIT_CHOICES] + [{"text": f"{i + 1}: Auto", "callback_data": f"att:lim:{i}:auto"}])
    return "\n".join(lines), [*rows, [ATTENDANCE, MAIN]]


def view_set() -> tuple[str, list]:
    """Absences from before the bot counted, the courses in the order of the week. In Slack every course
    is a line with a drop-down: pick the number you have now. Elsewhere a numbered list with a ➕ row and a
    ➖ row."""
    data = attendance.load()
    kind = attendance.kinds()
    names = attendance.ordered()
    lines = ["📝 *Your earlier absences*", media.DIVIDER, "Set each course to the number of absences you have now."]
    rows = []
    for i, course in enumerate(names):
        info = attendance.status(course, kind[course], data)
        if _in_slack():
            top = max(info["allowed"] + 2, info["count"])
            options = [[f"{k} of {info['allowed']}" if k <= info["allowed"] else f"🚫 {k} — deprived", f"att:to:{i}:{k}"]
                       for k in range(top + 1)]
            rows.append([{"text": "Set", "callback_data": f"att:to:{i}:{info['count']}",
                          "line": {"text": _course_text(i, course, kind[course], info), "options": options,
                                   "initial": f"att:to:{i}:{info['count']}"}}])
        else:
            lines.append(f"*{i + 1}.* {course} — {info['count']} of {info['allowed']}")
    if not _in_slack():
        rows = [[{"text": f"➕ {i}", "callback_data": f"att:adj:{i - 1}:up"} for i in range(1, len(names) + 1)],
                [{"text": f"➖ {i}", "callback_data": f"att:adj:{i - 1}:down"} for i in range(1, len(names) + 1)]]
    return "\n".join(lines), [*rows, [ATTENDANCE, MAIN]]


# ------------------------------------------------------------------ buttons

def _show(message_id: int | None, view: tuple[str, list]) -> None:
    chat.edit_view(message_id, *view) if message_id else chat.send_view(*view)


def handle_callback(cb: dict, action: str, message_id: int | None) -> None:
    chat.answer(cb["id"])
    parts = action.split(":")
    what = parts[1] if len(parts) > 1 else "home"
    if chat.is_file_message(cb.get("message")):
        message_id = None
    names = attendance.ordered()
    index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
    if what == "home":
        _show(message_id, view_home())
    elif what == "set":
        _show(message_id, view_set())
    elif what == "lim":
        if 0 <= index < len(names) and len(parts) > 3 and (parts[3] == "auto" or parts[3].isdigit()):
            attendance.set_limit(names[index], None if parts[3] == "auto" else int(parts[3]))
        _show(message_id, view_limits())
    elif what == "to" and 0 <= index < len(names) and len(parts) > 3 and parts[3].isdigit():
        attendance.set_count(names[index], int(parts[3]))
        _show(message_id, view_set())
    elif what == "adj" and 0 <= index < len(names) and len(parts) > 3 and parts[3] in ("up", "down"):
        attendance.adjust(names[index], 1 if parts[3] == "up" else -1)
        _show(message_id, view_set())
    elif what == "today":
        _show(message_id, view_today())
    elif what in ("ask", "pick", "t", "all", "none", "save"):
        try:
            day = date.fromisoformat(parts[2])
        except (IndexError, ValueError):
            _show(message_id, view_home())
            return
        _handle_day(message_id, what, day, parts)
    else:
        _show(message_id, view_home())


def _handle_day(message_id: int | None, what: str, day: date, parts: list[str]) -> None:
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
    log.info("Attendance: asked about %s (time %s)", day, attendance.load()["asked"][day.isoformat()]["times"])
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
