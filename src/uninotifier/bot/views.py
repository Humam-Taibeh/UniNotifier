"""
The menus: the main dashboard, My Courses & Schedule, Upcoming Tasks &
Reminders, GPA & Grade Hub, Academic Record & Balance — and the weekly
timetable files.
"""

import logging
import threading
import time

from uninotifier import failsafe
from uninotifier import gpa
from uninotifier import guide
from uninotifier import lecturelinks
from uninotifier import media
from uninotifier import notifier
from uninotifier import paths
from uninotifier import reminders
from uninotifier import schedule
from uninotifier import timetable
from uninotifier.bot import chat
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")


SCHEDULE_PDF = paths.SCHEDULE_PDF


SCHEDULE_IMAGE = paths.SCHEDULE_IMAGE


def semester() -> str:
    """The term's name from schedule.json ("First Semester 2026/2027"), read when a screen is drawn:
    importing this module touches no file, and a new term shows without restarting the bot."""
    return schedule.load_schedule().get("semester", "")


def view_menu() -> tuple[str, list]:
    courses = len(chat.load_state().get("courses", {}))
    text = (f"🎓 *UniNotifier System* • {semester()}\n"
            f"{chat.monitor_status()} • {courses} Course{'' if courses == 1 else 's'} Tracked")
    return text, [
        [{"text": "📚 My Courses & Schedule", "callback_data": "courses"},
         {"text": "⏳ Upcoming Tasks & Reminders", "callback_data": "tasks"}],
        [{"text": "📊 GPA & Grade Hub", "callback_data": "academic"},
         {"text": "📜 Academic Record & Balance", "callback_data": "record"}],
        [{"text": "📖 System Guide", "callback_data": "help"}],
    ]


def base_line() -> str:
    record = gpa.load()
    if record.get("cgpa") is None:
        return "🎓 *CGPA base:* not synced yet — read 📜 Transcript once"
    return f"🎓 *Cumulative GPA:* {record['cgpa']:.2f}%"


def view_academic() -> tuple[str, list]:
    """📊 GPA & Grade Hub: what each course needs, and the grades as they are released."""
    lines = ["📊 *GPA & Grade Hub*", guide.DIVIDER, base_line(), "",
             "🎯 *Grade Needed* — what each course needs for a semester or cumulative target; "
             "hours, retakes and marks are adjusted there",
             "🏁 *Finals Watch* — check this term's grades now, or have each one announced the moment it is released"]
    return "\n".join(lines), [
        [{"text": "🎯 Grade Needed", "callback_data": "gpa:need"},
         {"text": "🏁 Finals Watch", "callback_data": "finals:status"}],
        chat.BACK_ROW,
    ]


def view_record() -> tuple[str, list]:
    """📜 Academic Record & Balance: the transcript and the tuition balance."""
    lines = ["📜 *Academic Record & Balance*", guide.DIVIDER,
             "📜 *Transcript* — your cumulative GPA, passed hours, status and every term's courses",
             "💰 *Balance* — your tuition balance"]
    return "\n".join(lines), [
        [{"text": "📜 Transcript", "callback_data": "portal:transcript"},
         {"text": "💰 Balance", "callback_data": "portal:balance"}],
        [{"text": "👤 Guest Look-up", "callback_data": "guest:start"}],
        chat.BACK_ROW,
    ]


OPEN_TASK_BUTTONS = 8


REMINDERS_SHOWN = 10


VIEW_POST_BUTTONS = 6


def view_tasks() -> tuple[str, list]:
    """Course deadlines from the portal, then the user's own reminders."""
    now = time.time()
    upcoming = sorted(
        (d for d in chat.load_state().get("deadlines", {}).values()
         if d.get("timestamp", 0) > now and not d.get("submitted")),
        key=lambda d: d["timestamp"])

    lines = ["⏳ *Upcoming Tasks & Reminders*", guide.DIVIDER, "📚 *Course deadlines*"]
    rows = []
    if not upcoming:
        lines.append("🎉 No assignments or quizzes ahead.")
    for n, d in enumerate(upcoming, 1):
        is_quiz = d.get("type") == "quiz"
        emoji = "📊" if is_quiz else "📝"
        field = "Opens" if is_quiz else "Due"
        lines += [
            f'{n}. {emoji} *{escape_md(d.get("name", "?"))}*',
            f'    {media.course_line(d.get("course", ""))}',
            f'    🕐 *{field}:* {notifier.format_timestamp(d["timestamp"])} · ⏱️ {chat.remaining_text(d["timestamp"])}',
        ]
        if d.get("url") and len(rows) < OPEN_TASK_BUTTONS:
            rows.append([{"text": f"🔗 Open Task {n} · {chat._short(d.get('name', ''))}", "url": d["url"]}])

    mine = reminders.pending()
    lines += ["", "🔔 *Your reminders*"]
    if not mine:
        lines.append("None yet — tap ➕ below, or 📌 Add to Reminders under any announcement.")
    for r in mine[:REMINDERS_SHOWN]:
        course = f" · {escape_md(r['course'])}" if r.get("course") else ""
        lines.append(f"• 📝 {escape_md(chat._short(r['note'], 80))}{course}")
        lines.append(f"    🕐 {reminders.when_text(r['due'])} · ⏱️ {chat.remaining_text(r['due'])}")
    if len(mine) > REMINDERS_SHOWN:
        lines.append(f"… and {len(mine) - REMINDERS_SHOWN} more")
    # a reminder pinned from a post: one tap back to that message in the chat
    for r in [r for r in mine[:REMINDERS_SHOWN] if r.get("message_id")][:VIEW_POST_BUTTONS]:
        rows.append([{"text": f"🔗 View Original Post · {chat._short(r['note'], 24)}",
                      "callback_data": f"rem:view:{r['id']}"}])
    last = chat.load_last_run().get("finished")
    if last:
        lines += ["", f"🕒 *Last Checked:* {notifier.audit_timestamp(last)}"]

    rows.append([{"text": "➕ New Custom Reminder", "callback_data": "rem:new"}]
                + ([{"text": "🗑️ Manage Reminders", "callback_data": "rem:list"}] if mine else []))
    rows.append(chat.BACK_ROW)
    return "\n".join(lines), rows


def view_courses() -> tuple[str, list]:
    """My Courses & Schedule: a menu, not a wall of text."""
    state = chat.load_state()
    count = len(state.get("courses", {}))
    hours = sum(c["hours"] for c in gpa.load()["courses"].values()) or 3 * count
    text = "\n".join(["📚 *My Courses & Schedule*", guide.DIVIDER,
                      f"{count} course{'' if count == 1 else 's'} · {gpa.fmt(hours)} credit hours · {semester()}",
                      "", "What would you like to see?"])
    return text, [[{"text": "📋 Course List & Details", "callback_data": "courselist"}],
                  [{"text": "📅 Weekly Timetable", "callback_data": "schedule"}],
                  chat.BACK_ROW]


def view_course_list() -> tuple[str, list]:
    """The courses as a timetable: grouped by the days they meet (Sun · Tue, Mon · Wed), each group in the
    order of its lectures, a course that meets at the same time on both days said once (timetable.py)."""
    state = chat.load_state()
    courses = state.get("courses", {})
    by_course = state.get("teachers", {}).get("by_course", {})
    entries = []
    for course in courses.values():
        name = media.clean_course_name(course["name"])
        entries.append((name, course["url"], schedule.lectures_for(name)))
    entries.sort(key=lambda e: (timetable.order_key(e[2]), e[0]))

    lines = [f"📚 *My Courses & Schedule* ({len(courses)})"]
    group, number = None, 0
    for name, url, slots in entries:
        days = timetable.pattern(slots)
        if group != days or number == 0:
            group = days
            lines += ["", f"📅 *{timetable.group_title(days)}*" if slots else "📅 *No timetable entry*"]
        number += 1
        section = schedule.section_of(name)
        teachers = schedule.teachers_for(name, section, by_course, courses)
        lines += ["", f"{number}. 📘 {chat.link(name, url)}"]
        if teachers:
            lines.append(f"{timetable.IND}👨‍🏫 {escape_md(' / '.join(teachers))}")
        if section:
            lines.append(f"{timetable.IND}🔢 Section {section}")
        if slots:
            lines += [f"{timetable.IND}{line}" for line in timetable.describe(slots)]      # the hours, right under the section
        if name in lecturelinks.online_courses():
            link = lecturelinks.get(name)
            lines.append(f"{timetable.IND}🎥 [Join Meeting]({link})" if link else f"{timetable.IND}🎥 Lecture link: not posted yet")
    if not entries:
        lines += ["", "No courses saved yet — run a check first."]
    return "\n".join(lines), [chat.COURSES_ROW]


VIEWS = {"menu": view_menu, "tasks": view_tasks, "courses": view_courses, "courselist": view_course_list,
         "academic": view_academic, "record": view_record}


PORTAL_COMMANDS = {"/transcript": "transcript", "/balance": "balance"}      # /grades is Finals Watch's check


COMMAND_VIEWS = {"/start": "menu", "/tasks": "tasks", "/courses": "courses", "/gpa": "academic",
                 "/record": "record"}


_schedule_lock = threading.Lock()


def start_schedule_files() -> bool:
    """/schedule and its button: the files go out from a worker thread — an upload can take minutes
    on a bad connection, and the polling loop must keep answering every other button meanwhile.
    One at a time: a second tap while they are on their way does nothing (False)."""
    if not _schedule_lock.acquire(blocking=False):
        return False

    def work() -> None:
        try:
            send_schedule_files()
        except Exception as e:
            log.exception("Sending the timetable files failed")
            failsafe.bug("bot:schedule", "The timetable files could not be sent", e)
        finally:
            _schedule_lock.release()

    threading.Thread(target=work, name="schedule", daemon=True).start()
    return True


@chat.with_action("upload_document")
def send_schedule_files() -> None:
    """The timetable as one album: a PDF to keep and an image to glance at."""
    items = [(path, caption) for path, caption in (
        (SCHEDULE_PDF, "📎 High-Resolution PDF"),
        (SCHEDULE_IMAGE, "🖼️ Quick PNG Preview")) if path.is_file()]
    if items:
        if len(items) == 1 or not notifier.send_document_group(chat.BOT_TOKEN, chat.CHAT_ID, items):
            for path, caption in items:  # album failed: fall back to single sends
                notifier.send_document(chat.BOT_TOKEN, chat.CHAT_ID, path, caption)
        chat.reanchor()                  # the files landed below the menu: it goes back to the bottom
    else:
        chat.send_view("⚠️ No timetable files yet. Put your timetable in the project's `assets` folder as "
                       f"`{SCHEDULE_PDF.name}` and/or `{SCHEDULE_IMAGE.name}`.", [chat.BACK_ROW])
