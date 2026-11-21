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
from uninotifier import lecturelinks
from uninotifier import media
from uninotifier import notifier
from uninotifier import paths
from uninotifier import reminders
from uninotifier import schedule
from uninotifier import slack
from uninotifier import slackbot
from uninotifier import tasks
from uninotifier import timetable
from uninotifier.bot import chat, settings_flow
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")


SCHEDULE_PDF = paths.SCHEDULE_PDF


SCHEDULE_IMAGE = paths.SCHEDULE_IMAGE


def semester() -> str:
    """The term's name ("First Semester 2026/2027", "Summer Semester 2026/2027"), worked out when a screen is
    drawn from the term code Moodle puts in front of each of your courses (20261-… is the first semester of
    2026/2027), so it follows the student and the time by itself; the schedule's own "semester" only when
    Moodle's courses do not say. Importing this module touches no file."""
    from uninotifier import autoschedule
    names = [c.get("name", "") for c in chat.load_state().get("courses", {}).values()]
    return autoschedule.semester_name(names) or schedule.load_schedule().get("semester", "")


def view_menu() -> tuple[str, list]:
    text = " • ".join(part for part in ("🎓 *UniNotifier System*", semester(), chat.monitor_status()) if part)
    panel = [[{"text": "🎛️ Control Panel", "callback_data": "panel"}]] if slack.mode() == "slack" else []
    return text, panel + [
        [{"text": "📚 My Courses & Schedule", "callback_data": "courses"},
         {"text": "⏳ Upcoming Tasks & Reminders", "callback_data": "tasks"}],
        [{"text": "📊 GPA & Grade Hub", "callback_data": "academic"},
         {"text": "📜 Academic Record & Balance", "callback_data": "record"}],
        [{"text": "⚙️ Settings", "callback_data": "settings"}],
    ]


def view_guide_link() -> tuple[str, list]:
    """/help: the guide is the project's README and reference, not screens in the chat."""
    return ("📖 *Guide* — every feature, the setup steps and the settings are explained in the project's "
            "`README.md` and `docs/REFERENCE.md` (on its GitHub page, and in the folder next to `bot.py`).",
            [chat.BACK_ROW])


def base_line() -> str:
    record = gpa.load()
    if record.get("cgpa") is None:
        return "🎓 *CGPA base:* not synced yet — read 📜 Transcript once"
    return f"🎓 *Cumulative GPA:* {record['cgpa']:.2f}%"


def view_academic() -> tuple[str, list]:
    """📊 GPA & Grade Hub: what each course needs, and the grades as they are released."""
    lines = ["📊 *GPA & Grade Hub*", media.DIVIDER, base_line(), "",
             "🎯 *Grade Needed* — the mark each course needs for your target", "",
             "📝 *Midterm Watch* — your midterm marks, as they appear", "",
             "🏁 *Finals Watch* — your grades, as they are released"]
    return "\n".join(lines), [
        [{"text": "🎯 Grade Needed", "callback_data": "gpa:need"}],
        [{"text": "📝 Midterm Watch", "callback_data": "mid:status"},
         {"text": "🏁 Finals Watch", "callback_data": "finals:status"}],
        chat.BACK_ROW,
    ]


def view_record() -> tuple[str, list]:
    """📜 Academic Record & Balance: the transcript, the tuition balance and the registration plan."""
    lines = ["📜 *Academic Record & Balance*", media.DIVIDER,
             "📜 *Transcript* — your cumulative GPA, passed hours, status and every term's courses",
             "💰 *Balance* — your tuition balance",
             "🗓️ *Registration Plan* — what to register next term: what is open to you, what is not yet and why"]
    return "\n".join(lines), [
        [{"text": "📜 Transcript", "callback_data": "portal:transcript"},
         {"text": "💰 Balance", "callback_data": "portal:balance"}],
        [{"text": "🗓️ Registration Plan", "callback_data": "reg:fresh"},
         {"text": "👤 Guest Look-up", "callback_data": "guest:start"}],
        chat.BACK_ROW,
    ]


REMINDERS_SHOWN = 10
SCREEN_BUDGET = 3600                 # Telegram stops a message at 4096 characters; room is kept for the footer
LRM, FSI, PDI = "\u200e", "\u2068", "\u2069"


def reminder_lines(number: int, reminder: dict) -> list[str]:
    """One reminder in the list, all of it: its title (the note's first line), the rest of the note
    under it (a pinned post's note is the whole post, up to reminders.NOTE_LIMIT characters), then the
    course and the time, each on a line of its own. Every note line is direction-isolated, so an
    Arabic note next to an English course name or a time never reorders the line."""
    written = [line.strip() for line in (reminder.get("note") or "").splitlines() if line.strip()] or ["Reminder"]
    title = written[0]
    ind = "" if slack.mode() == "slack" else "      "      # Slack wraps a long line back to the margin: nothing is indented there
    bold = "*" if slack.mode() == "slack" else ""         # in Slack the title is bold and what is under it small, inside it
    lines = [f"{LRM}{number}. 📝 {bold}{FSI}{escape_md(title)}{PDI}{bold}"]
    lines += [slack.small(f"{LRM}{ind}{FSI}{escape_md(line)}{PDI}") for line in written[1:]]
    if reminder.get("course") and reminder["course"] not in title:
        lines.append(slack.small(f"{LRM}{ind}📘 {escape_md(reminder['course'])}"))
    lines.append(slack.small(f"{LRM}{ind}🕐 {reminders.when_text(reminder['due'])} · ⏱️ {chat.remaining_text(reminder['due'])}"))
    return lines


VIEW_POST_BUTTONS = 6


def view_tasks() -> tuple[str, list]:
    """Every open task — assignments, quizzes, Cambridge One (tasks.py) — then
    the user's own reminders."""
    now = time.time()
    upcoming = tasks.open_tasks(chat.load_state(), now)

    in_slack = slack.mode() == "slack"
    lines = ["⏳ *Upcoming Tasks & Reminders*", media.DIVIDER, slack.heading("📚 *Course tasks*", "📚 Course tasks")]
    if not upcoming:                          # in Slack: small, inside "Course tasks" — like the details under any item
        lines.append(slack.small("none ahead 🎉") if in_slack else "🎉 No assignments or quizzes ahead.")
    for n, task in enumerate(upcoming, 1):
        lines += tasks.task_lines(n, task, now)
    rows = tasks.open_buttons(upcoming)              # (the controls: one group, under one line)

    mine = reminders.pending()
    lines += ["", media.DIVIDER, slack.heading("🔔 *Your reminders*", "🔔 Your reminders")]       # the course's own tasks, then yours: a line between them
    if not mine:
        lines.append("None yet — tap ➕ below, or 📌 Add to Reminders under any announcement.")
    shown = []
    for r in mine[:REMINDERS_SHOWN]:
        block = ["", *reminder_lines(len(shown) + 1, r)]
        if len("\n".join(lines + block)) > SCREEN_BUDGET:     # whole reminders only: never one cut in half
            break
        lines += block
        shown.append(r)
    if len(mine) > len(shown):
        lines.append(f"… and {len(mine) - len(shown)} more — 🗑️ Manage Reminders lists them all")
    # a reminder pinned from a post: one tap back to that message in the chat. The button carries the
    # number the list gives the reminder (so it is renumbered with the list), and the reminder's own id
    # (so a screen that is a little old still opens the right one).
    pinned = [(n, r) for n, r in enumerate(shown, 1) if r.get("message_id")]
    for n, r in pinned[:VIEW_POST_BUTTONS]:
        if slack.mode() == "slack":          # Slack cannot quote a message, and the reminder shows the whole post: no button
            continue
        rows.append([{"text": f"🔗 View Original Post ({n})", "callback_data": f"rem:view:{r['id']}"}])

    rows.append([{"text": "➕ New Custom Reminder", "callback_data": "rem:new"}]
                + ([{"text": "🗑️ Manage Reminders", "callback_data": "rem:list"}] if mine else []))
    rows.append(chat.BACK_ROW)
    return "\n".join(lines), rows


def view_courses() -> tuple[str, list]:
    """My Courses & Schedule: a menu, not a wall of text."""
    text = "\n".join(["📚 *My Courses & Schedule*", media.DIVIDER, "What would you like to see?"])
    return text, [[{"text": "📋 Course List & Details", "callback_data": "courselist"}],
                  [{"text": "📅 Weekly Timetable", "callback_data": "schedule"},
                   {"text": "🎒 Attendance", "callback_data": "att:home"}],
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

    hours = sum(c["hours"] for c in gpa.load()["courses"].values()) or 3 * len(courses)
    lines = ["📚 *My Courses & Schedule*", f"Total: {len(courses)} course{'' if len(courses) == 1 else 's'} · {gpa.fmt(hours)} credit hours"]
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
         "academic": view_academic, "record": view_record, "settings": settings_flow.view_settings}


PORTAL_COMMANDS = {"/transcript": "transcript", "/balance": "balance"}      # /grades is Finals Watch's check


COMMAND_VIEWS = {"/start": "menu", "/tasks": "tasks", "/courses": "courses", "/gpa": "academic",
                 "/record": "record", "/settings": "settings"}


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
    """The timetable as one album: a PDF to keep and an image to glance at. Your own files in
    assets/ when you keep any; otherwise the timetable the bot draws from the schedule in force
    (timetable_image.py) — drawn on the first press and again whenever the schedule changed."""
    started = time.monotonic()
    items = [(path, caption) for path, caption in (
        (SCHEDULE_PDF, "📎 High-Resolution PDF"),
        (SCHEDULE_IMAGE, "🖼️ Quick PNG Preview")) if path.is_file()]
    if not items:
        from uninotifier import autoschedule, timetable_image
        current = schedule.load_schedule()
        drawn = timetable_image.ensure(current, autoschedule.hours_for(current))
        if drawn:
            items = [(drawn[1], "📎 High-Resolution PDF"), (drawn[0], "🖼️ Quick PNG Preview")]
    if items and slack.mode() == "slack":
        items = [(path, "") for path, _ in items]       # in Slack the files speak for themselves: no caption text
    if len(items) == 2 and slack.active() and slackbot.interactive():
        from uninotifier import slackfiles              # uploaded once, then shown from Slack's own copy
        how = slackfiles.send_timetable(items[0][0], items[1][0])
        if how:
            log.info("Timetable sent in %.1f s (%s)", time.monotonic() - started, how)
            chat.reanchor()
            return
    if items:
        if len(items) == 1 or not notifier.send_document_group(chat.BOT_TOKEN, chat.CHAT_ID, items):
            for path, caption in items:  # album failed: fall back to single sends
                notifier.send_document(chat.BOT_TOKEN, chat.CHAT_ID, path, caption)
        log.info("Timetable sent in %.1f s", time.monotonic() - started)
        chat.reanchor()                  # the files landed below the menu: it goes back to the bottom
    else:
        chat.send_view("⚠️ No timetable yet. With `PORTAL_USERNAME` and `PORTAL_PASSWORD` in `.env` I read it from "
                       "the portal and draw it for you (send /check to read it now). Or put your own in the "
                       f"project's `assets` folder as `{SCHEDULE_PDF.name}` and/or `{SCHEDULE_IMAGE.name}`.",
                       [chat.BACK_ROW])
