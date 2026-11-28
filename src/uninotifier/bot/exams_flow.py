"""
Midterm Watch in the bot (exams_watch.py has the rules, exams.py reads the site): next to Finals Watch in the
📊 GPA & Grade Hub.

  * every INTERVAL_SECONDS (while it is on) the exams site is read with the portal login, and each mark that appears
    or changes is sent as a message of its own — with the picture of the grade page, the mark, the mark out of 100,
    and the most the course can still reach;
  * 📊 All Courses (under every such message, and in the hub) is the summary: every course, its mark or "waiting";
  * ✍️ Add / Change a Mark is for a mark a doctor gave you somewhere else (a file, a post): it is used until you
    remove it, and counts like the site's;
  * every mark in use is handed to the grade calculator, which then knows the most each course can reach.
"""

import logging
import threading
import time

from uninotifier import exams, exams_watch, failsafe, gpa, media, notifier, portal, schedule, slack
from uninotifier.bot import chat, portal_flow
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")

TICK = 60                                   # seconds between looks at whether a look is due
HUB = {"text": "🔙 GPA & Grade Hub", "callback_data": "academic"}
MAIN = {"text": "🏠 Main Menu", "callback_data": "menu"}
STATUS = "mid:status"
_wait: dict | None = None                   # {"course": name}: the next plain message is that course's mark


def g(value: float) -> str:
    return f"{value:g}"


def courses_in_order(state: dict) -> list[str]:
    """Every course the exams site or the grade calculator knows, in the order of the timetable."""
    names = set(state["courses"]) | set(gpa.load()["courses"]) | set(state.get("manual") or {})
    return schedule.sort_courses(names)


def best_possible(course: str, mark: dict) -> float:
    """The most the course can end on: the calculator's own figure when it knows the course, else 100 minus what the
    mark already lost."""
    known = gpa.load()["courses"].get(course)
    if known is not None:
        return gpa.ceiling(known)
    return max(0.0, 100.0 - max(mark["out_of"] - mark["got"], 0.0))


def course_lines(number: int, course: str, state: dict) -> list[str]:
    mark = exams_watch.effective(state, course)
    if not mark:
        return [f"⏳ *{number}. {escape_md(course)}*", slack.small("waiting for the mark")]
    bits = [f"{g(mark['got'])} / {g(mark['out_of'])}", f"{round(exams_watch.hundred(mark['got'], mark['out_of']), 1):g} / 100"]
    best = best_possible(course, mark)
    if best < 100:
        bits.append(f"best possible {g(best)}")
    if mark["source"] == "you":
        bits.append("added by you")
    elif mark["source"] == "moodle":
        bits.append("found on Moodle")
    return [f"✅ *{number}. {escape_md(course)}*", slack.small(" · ".join(bits))]


def view_status() -> tuple[str, list]:
    """The Midterm Watch screen: the switch, every course with its mark (or waiting), and what can be done."""
    state = exams_watch.load()
    on = exams_watch.enabled(state)
    if portal.credentials() is None:
        return (f"📝 *Midterm Watch*\n{media.DIVIDER}\n" + portal_flow.portal_setup_text(),
                [[HUB, MAIN]])
    lines = ["📝 *Midterm Watch*", media.DIVIDER,
             "🟢 *On* — I look at the exams site every 2 minutes and tell you the moment a mark appears." if on else
             "⏸️ *Off* — I am not looking. Turn it on when your midterm exams begin.", ""]
    names = courses_in_order(state)
    if not names:
        lines.append("No courses yet — tap 🔄 Check Now.")
    for number, course in enumerate(names, 1):
        lines += [*course_lines(number, course, state), ""]
    rows = [[{"text": "🔄 Check Now", "callback_data": "mid:check"},
             {"text": "⏸️ Turn Off" if on else "▶️ Turn On", "callback_data": "mid:off" if on else "mid:on"}],
            [{"text": "✍️ Add / Change a Mark", "callback_data": "mid:set"}],
            [HUB, MAIN]]
    return "\n".join(lines).rstrip(), rows


MARK_STEPS = tuple(n / 2 for n in range(0, 61))             # 0, 0.5 ... 30: the marks of a midterm out of 30


def view_set() -> tuple[str, list]:
    """A mark you know yourself, per course. In Slack every course is a line with a drop-down (its marks out of 30, none to
    remove one, or type another figure); elsewhere a button per course and the mark typed."""
    state = exams_watch.load()
    lines = ["✍️ *Add / Change a Mark*", media.DIVIDER,
             "For a mark a doctor gave you outside the exams site. It is used until you remove it."]
    rows = []
    for i, course in enumerate(courses_in_order(state)):
        mine = (state.get("manual") or {}).get(course)
        now = f"{g(mine['got'])} / {g(mine['out_of'])} (yours)" if mine else (
            "no mark yet" if not exams_watch.effective(state, course) else
            f"{g(exams_watch.effective(state, course)['got'])} / {g(exams_watch.effective(state, course)['out_of'])} (the site's)")
        if slack.mode() == "slack":
            options = [["Remove my mark", f"mid:s:{i}:x"]] + [[f"{g(v)} / 30", f"mid:s:{i}:{g(v)}"] for v in MARK_STEPS] + \
                      [["Type another figure…", f"mid:s:{i}:t"]]
            rows.append([{"text": "Mark", "callback_data": f"mid:s:{i}:t", "line": {
                "text": f"*{i + 1}. {escape_md(course)}*\n{now}", "options": options,
                "initial": f"mid:s:{i}:{g(mine['got'])}" if mine and mine["out_of"] == 30 and mine["got"] in MARK_STEPS else f"mid:s:{i}:t"}}])
        else:
            rows.append([{"text": f"{i + 1}. {chat._short(course, 28)} — {now}", "callback_data": f"mid:s:{i}:t"}])
    if not rows:
        lines.append("\nNo courses yet.")
    return "\n".join(lines), [*rows, [{"text": "🔙 Midterm Watch", "callback_data": STATUS}, MAIN]]


def ask_text(course: str) -> tuple[str, list]:
    return (f"✍️ *Mark for {escape_md(course)}*\n{media.DIVIDER}\nSend the mark: *24* (out of 30) or *24/25* (out of 25).",
            [chat.cancel_row(STATUS)])


def drop_pending() -> None:
    """Back or Main Menu on a screen waiting for a mark: the question is dropped."""
    global _wait
    _wait = None


def handle_text(text: str) -> bool:
    """A typed mark for the course being asked about. True when the message was the answer (or its error)."""
    global _wait
    if not _wait:
        return False
    try:
        got, out_of = exams_watch.parse_mark(text)
    except ValueError as e:
        chat.send_view(f"⚠️ {escape_md(str(e))}", [chat.cancel_row(STATUS)])
        return True
    course, _wait = _wait["course"], None
    exams_watch.set_manual(course, got, out_of)
    exams_watch.apply_to_gpa()
    chat.send_view(*view_status())
    return True


def _show(message_id: int | None, text: str, rows: list) -> None:
    chat.edit_view(message_id, text, rows) if message_id else chat.send_view(text, rows)


def handle_callback(cb: dict, action: str, message_id: int | None) -> None:
    """mid:status · mid:check · mid:on · mid:off · mid:set · mid:s:<course number>:<mark | x | t>"""
    global _wait
    chat.answer(cb["id"])
    if chat.is_file_message(cb.get("message")):             # pressed under a picture: the screen opens below it
        message_id = None
    parts = action.split(":")[1:]
    kind = parts[0] if parts else "status"
    if kind != "s":
        _wait = None
    if kind == "check":
        if portal.credentials() is None:
            _show(message_id, *view_status())
            return
        _show(message_id, f"📝 *Midterm Watch*\n{media.DIVIDER}\n⏳ Reading the exams site… usually a few seconds",
              [chat.cancel_row(STATUS)])
        threading.Thread(target=_manual_check, args=(message_id,), daemon=True).start()
        return
    if kind in ("on", "off"):
        exams_watch.set_enabled(kind == "on")
    elif kind == "set":
        _show(message_id, *view_set())
        return
    elif kind == "s" and len(parts) == 3:
        state = exams_watch.load()
        names = courses_in_order(state)
        if not parts[1].isdigit() or int(parts[1]) >= len(names):
            _show(message_id, *view_set())
            return
        course = names[int(parts[1])]
        if parts[2] == "t":
            _wait = {"course": course}
            _show(message_id, *ask_text(course))
            return
        if parts[2] == "x":
            exams_watch.clear_manual(course)
        else:
            try:
                exams_watch.set_manual(course, float(parts[2]), exams_watch.DEFAULT_OUT_OF)
            except ValueError:
                pass
        exams_watch.apply_to_gpa()
        _show(message_id, *view_set())
        return
    _show(message_id, *view_status())


def _manual_check(message_id: int | None) -> None:
    try:
        told = check_once(manual=True)
        if told:
            chat.send_view(*view_status())              # the marks landed below: the summary returns to the bottom
        else:
            _show(message_id, *view_status())
    except Exception as e:
        log.exception("The midterm check failed")
        failsafe.bug("bot:exams-check", "A midterm check crashed", e)
        _show(message_id, "⚠️ *Midterm Watch couldn't read the exams site*\n\nTry again in a moment.",
              [[{"text": "🔄 Try Again", "callback_data": "mid:check"}], [HUB, MAIN]])


# ------------------------------------------------------------------ the watch itself

def _notify(item: dict, state: dict, creds: tuple[str, str]) -> bool:
    """One mark: its message with the picture of the grade page (when it can be taken) and its buttons."""
    mark = exams_watch.effective(state, item["course"])
    shown = {**item, "best": best_possible(item["course"], mark) if mark else None}
    if mark and mark["source"] == "you":
        shown["yours"] = f"{g(mark['got'])} / {g(mark['out_of'])}"
    text = exams_watch.message(shown)
    rows = [[{"text": "📊 All Courses", "callback_data": STATUS}],
            [{"text": "🌐 Open Exams Site", "url": item["course_url"]}]]
    pictures = exams.screenshots(*creds, item["course_url"])
    if pictures and notifier.send_png_file(chat.BOT_TOKEN, chat.CHAT_ID, pictures[0],
                                           f"Exams - {media.clean_course_name(item['course'])}.png", text, rows=rows):
        return True
    return bool(notifier.send_message(chat.BOT_TOKEN, chat.CHAT_ID, text, rows=rows))


def check_once(manual: bool = False) -> int:
    """One look. Returns how many marks you were told about (the first look says the watch is on)."""
    creds = portal.credentials()
    if creds is None:
        return 0
    state = exams_watch.load()
    try:
        known = {name: found for name, found in state["courses"].items()       # a graded course kept before marks were stored is read again
                 if not (exams.number(found.get("overview", "")) is not None and "mark" not in found)}
        courses = exams.read(*creds, known=known)
    except exams.ExamsError as e:
        log.info("Midterm watch: not read (%s)", notifier.redact(str(e)))
        exams_watch.skipped()
        if manual:
            chat.send_view(f"⚠️ *Midterm Watch couldn't read the exams site*\n\n{escape_md(notifier.redact(str(e)))}",
                           [[{"text": "🔄 Try Again", "callback_data": "mid:check"}], [HUB, MAIN]])
        return 0
    first = not state.get("started")
    news, new_state = exams_watch.compare(state, courses)
    exams_watch.apply_to_gpa(new_state)                       # the marks are known to the calculator before you are told
    sent, failed = 0, False
    if first:
        failed = not notifier.send_message(chat.BOT_TOKEN, chat.CHAT_ID, exams_watch.intro(courses),
                                           rows=[[{"text": "📊 All Courses", "callback_data": STATUS}],
                                                 [{"text": "🌐 Open Exams Site", "url": exams.OVERVIEW_URL}]])
    for item in news:
        if _notify(item, new_state, creds):
            sent += 1
            log.info("Midterm watch: told about %s — %s", item["course"], item["exam"])
        else:
            failed = True
    if failed:                                # something did not go out: nothing is remembered, the next look tells again
        exams_watch.skipped()
        return sent
    exams_watch.save(new_state)
    return sent + (1 if first else 0)


def loop() -> None:
    while True:
        try:
            if portal.credentials() is not None and exams_watch.due():
                check_once()
        except Exception as e:
            log.exception("The midterm watch failed")
            failsafe.bug("bot:exams-watch", "The Midterm Watch is failing", e,
                         "You may not be told when a midterm mark appears until it is fixed.")
        time.sleep(TICK)


def start() -> None:
    threading.Thread(target=loop, name="exams", daemon=True).start()
