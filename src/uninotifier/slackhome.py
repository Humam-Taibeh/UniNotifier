"""
The app's Home page on Slack as a dashboard, not a list of four buttons: what is on today, what is
due soon, your next reminders — then the buttons. The main menu (bot/views.view_menu) is drawn this
way on Home; every other screen keeps its text-and-buttons look (slack.home_view).

Everything shown is read from the files the bot already keeps (schedule, tasks, reminders, the
monitor's last run): drawing the page touches no network but Slack's own. Never raises into the
caller: slackbot.show_home falls back to the plain menu if anything here fails.
"""

from datetime import datetime

from uninotifier import media, notifier, reminders, schedule, slack, tasks

MENU_MARK = "🎓 *UniNotifier System*"       # the first words of views.view_menu's text
TASK_CARDS = 5                               # tasks shown as cards (Slack allows up to 10 in a carousel)
REMINDERS_SHOWN = 3
CARD_BODY = 200                              # Slack's limit for a card's body

LRM, FSI, PDI = "‎", "⁨", "⁩"       # an Arabic name never turns a line round: the emoji stays first

EXTRA_BUTTONS = [{"text": "🔄 Check Now", "callback_data": "check"},
                 {"text": "➕ New Reminder", "callback_data": "rem:new"}]


def is_menu(text: str) -> bool:
    return (text or "").startswith(MENU_MARK)


def _text(value) -> str:
    """Words from a file (a course, a note) made safe for Slack's markup."""
    return slack._escape(str(value or "")).replace("*", "").replace("_", " ").strip()


def _clock(hhmm: str) -> str:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return notifier.clock12(hour, minute)


def _span(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    days, rest = divmod(seconds, 86400)
    hours, minutes = rest // 3600, rest % 3600 // 60
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


# ------------------------------------------------------------------ today

def lecture_lines(now: datetime, sch: dict | None = None) -> list[str]:
    """Today's classes in time order: what is over (struck through), what is on now, what comes next."""
    sch = schedule.load_schedule() if sch is None else sch
    todays = (sch.get("days") or {}).get(schedule.DAY_KEYS[now.weekday()]) or []
    lines = []
    for lec in sorted(todays, key=lambda l: l.get("start", "")):
        try:
            start = datetime.combine(now.date(), datetime.strptime(lec["start"], "%H:%M").time())
            end = datetime.combine(now.date(), datetime.strptime(lec["end"], "%H:%M").time())
        except (KeyError, ValueError):
            continue
        mode = lec.get("mode") or ""
        place = f" · {_text(lec['place'])}" if lec.get("place") else ""
        label = f"{schedule.MODE_EMOJI.get(mode, '📘')} {_text(mode)}{place}".strip()
        course = f"{FSI}{_text(lec.get('course'))}{PDI}"
        when = f"{_clock(lec['start'])} – {_clock(lec['end'])}"
        if mode == "Activity":                       # an activity / assignment slot of a blended course: no lecture
            tail = "✔️" if now >= end else "_activity — no lecture_"
            lines.append(f"{LRM}🧩 {when}   *{course}*   {tail}")
        elif now >= end:
            lines.append(f"{LRM}~{when}   {course}~   ✔️")
        elif now >= start:
            lines.append(f"{LRM}▶️ {when}   *{course}*   {label}   *now*")
        else:
            lines.append(f"{LRM}🕘 {when}   *{course}*   {label}   _in {_span((start - now).total_seconds())}_")
    return lines


# ------------------------------------------------------------------ due soon

def task_card(task: dict, now: float, number: int = 0) -> dict:
    """One open task as Slack's card: its name, its course, when it is due, a button that opens it."""
    course = task["course"] if task["kind"] == "cambridge" else media.clean_course_name(task["course"])
    if task.get("late"):
        when = f"⚠️ *Overdue* — was due {notifier.format_timestamp(task['due'])}"
    elif task["kind"] == "quiz" and task.get("opens") and task["opens"] > now:
        when = f"*🗓️ Opens:* {notifier.format_timestamp(task['opens'])}\n_in {_span(task['opens'] - now)}_"
    elif task["due"]:
        field = "Closes" if task["kind"] == "quiz" else "Due"
        when = f"*🕐 {field}:* {notifier.format_timestamp(task['due'])}\n_{_span(task['due'] - now)} left_"
    else:
        when = "🕐 No due date set"
    card = {"type": "card",
            "title": {"type": "plain_text", "text": f"{LRM}{tasks.KIND[task['kind']]} {FSI}{task['name']}{PDI}"[:150], "emoji": False},
            "subtitle": {"type": "mrkdwn", "text": f"📘 {_text(course)}"[:150]},
            "body": {"type": "mrkdwn", "text": when[:CARD_BODY]}}
    if task.get("url"):
        card["actions"] = [{"type": "button", "text": {"type": "plain_text", "text": "🔗 Open", "emoji": True},
                            "url": task["url"], "action_id": f"open{number}"}]
    return card


def _heading(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}}


# ------------------------------------------------------------------ the page

def blocks(markup, now: datetime | None = None, state: dict | None = None) -> list[dict]:
    """The dashboard's blocks (without the footer): header, today, due soon, reminders, the buttons."""
    from uninotifier.bot import chat, views            # the bot's own readers; imported late (they import slackbot)
    now = now or datetime.now()
    stamp = now.timestamp()
    state = chat.load_state() if state is None else state
    courses = len(state.get("courses", {}))
    out: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": slack.HOME_TITLE, "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"{_text(views.semester())} · {slack.mrkdwn(chat.monitor_status())} · {courses} course{'' if courses == 1 else 's'}"}]},
        {"type": "divider"},
        _heading("📅 Today"),
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lecture_lines(now)) or "🎉 No lectures today"}},
    ]
    open_tasks = tasks.open_tasks(state, stamp)
    out.append(_heading("⏳ Due soon"))
    if open_tasks:
        out.append({"type": "carousel", "elements": [task_card(t, stamp, n) for n, t in enumerate(open_tasks[:TASK_CARDS])]})
        if len(open_tasks) > TASK_CARDS:
            out.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                        f"… and {len(open_tasks) - TASK_CARDS} more in ⏳ Upcoming Tasks & Reminders"}]})
    else:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": "🎉 No assignments or quizzes ahead"}})
    mine = reminders.pending()
    if mine:
        out.append(_heading("🔔 Your reminders"))
        lines = []
        for reminder in mine[:REMINDERS_SHOWN]:
            title = next((l.strip() for l in (reminder.get("note") or "").splitlines() if l.strip()), "Reminder")
            lines.append(f"{LRM}📝 {FSI}{_text(title)[:90]}{PDI}\n{LRM}      🕐 {reminders.when_text(reminder['due'])} · ⏱️ {_span(reminder['due'] - stamp)}")
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        if len(mine) > REMINDERS_SHOWN:
            out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"… and {len(mine) - REMINDERS_SHOWN} more"}]})
    out.append({"type": "divider"})
    out += slack.buttons(markup)
    out += slack.buttons({"inline_keyboard": [EXTRA_BUTTONS]}, prefix="x")
    return out


def view(markup, now: datetime | None = None) -> dict:
    """The Home page view: the dashboard and its footer."""
    page = blocks(markup, now)
    return {"type": "home", "blocks": page[:slack.HOME_BLOCKS - 1] + [slack.footer()]}
