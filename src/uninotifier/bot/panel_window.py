"""
🎛️ Control Panel (Slack): everything the bot does, in one window over the chat.

  main window     five sections, each with a line of what is in it now      ("panel" — buttons)
  section window  that section's actions as a list, and Go                  ("panel_go" — a list and Go)

Go does one of two things. An action that is a form (a reminder, Grade Needed, Settings) turns the window into that
form, and Save closes everything. An action whose answer is something to read (a timetable, a transcript, your tasks)
closes everything and the answer comes down in the chat as it always did — a window cannot hold a picture or a file,
and the chat keeps what you asked for. Notifications are not touched.

The chat's own menus stay (and are all Telegram has). slackbot.py routes the presses; this module only builds windows.
"""

import json
import time

from uninotifier import gpa, reminders, tasks
from uninotifier.bot import chat, gpa_window, reminder_window, settings_flow, winkit

MAIN = "panel"                 # the main window's callback_id (it has no Save: it is only buttons)
GO = "panel_go"                # a section window's callback_id (its Go is a submit)
OPEN = "panel:open:"           # the value of a section's button
PICK = "pick"                  # the list's block id

# (key, emoji, title, [(value, label, what it does, "chat" | "window")])
# "chat": the answer is a message in the chat · "window": the action is a form
SECTIONS = (
    ("courses", "📚", "Courses & Schedule", (
        ("courselist", "📋 Course List & Details", "Every course with its teacher, section and hours", "chat"),
        ("schedule", "📅 Weekly Timetable", "Your week as a picture, with the PDF", "chat"),
        ("att:home", "🎒 Attendance", "Absences per course and what each one allows", "chat"))),
    ("tasks", "⏳", "Tasks & Reminders", (
        ("tasks", "⏳ Upcoming Tasks", "Assignments, quizzes and your reminders", "chat"),
        ("rem:new", "➕ New Reminder", "A note with a day and a time you pick", "window"),
        ("rem:list", "🗑️ Manage Reminders", "Open, edit or remove a reminder", "chat"),
        ("check", "🔄 Check Now", "Look for anything new on Moodle right now", "chat"))),
    ("gpa", "📊", "GPA & Grades", (
        ("gpa:need", "🎯 Grade Needed", "The mark each course needs for your target", "window"),
        ("gpa:cg", "🎓 CGPA & Hours", "Enter your record by hand", "window"),
        ("mid:status", "📝 Midterm Watch", "Your midterm marks, and the watch's switch", "chat"),
        ("finals:status", "🏁 Finals Watch", "Your grades as they are released", "chat"))),
    ("record", "📜", "Record & Balance", (
        ("portal:transcript", "📜 Transcript", "Your CGPA, passed hours and every term's courses", "chat"),
        ("portal:balance", "💰 Balance", "Your tuition balance", "chat"),
        ("reg:fresh", "🗓️ Registration Plan", "What to register next term, and what is not open yet", "chat"),
        ("guest:start", "👤 Guest Look-up", "A friend's record, with their login (never saved)", "chat"))),
    ("settings", "⚙️", "Settings", (
        ("settings", "⚙️ Automatic Actions", "Auto Check-In and Auto Mark as Done", "window"),
        ("set:update", "🆕 Check for Updates", "Is a new version of UniNotifier ready?", "chat"))),
)

TITLE_LIMIT = 24
TEXT_LIMIT = 75


def _section(key: str):
    return next((s for s in SECTIONS if s[0] == key), None)


def _action(value: str):
    for _, _, _, actions in SECTIONS:
        for action in actions:
            if action[0] == value:
                return action
    return None


def _state(on: bool) -> str:
    return "On" if on else "Off"


def summary(key: str) -> str:
    """One line of what a section holds right now, read from the files the bot already keeps."""
    if key == "courses":
        count = len(chat.load_state().get("courses", {}))
        return f"{count} course{'' if count == 1 else 's'} this term"
    if key == "tasks":
        open_tasks = tasks.open_tasks(chat.load_state(), time.time())
        mine = reminders.pending()
        return f"{len(open_tasks)} task{'' if len(open_tasks) == 1 else 's'} ahead · {len(mine)} reminder{'' if len(mine) == 1 else 's'}"
    if key == "gpa":
        cgpa = gpa.load().get("cgpa")
        return f"CGPA {cgpa:.2f}%" if cgpa is not None else "CGPA not synced yet — read your transcript once"
    if key == "record":
        return "Transcript · Balance · Registration · Guest"
    on = {name: settings_flow.is_on(name) for name, _, _ in settings_flow.SWITCHES}
    return " · ".join(f"{label.split(' ', 1)[1]} {_state(on[name])}" for name, label, _ in settings_flow.SWITCHES)


def main_window() -> dict:
    from uninotifier.bot import views
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            "*Everything UniNotifier does, in one place.*\nPick a section. What you ask for arrives in the chat."}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(
            part for part in (views.semester(), chat.monitor_status()) if part)}]},
    ]
    for key, emoji, title, _ in SECTIONS:
        blocks += [{"type": "divider"},
                   {"type": "section", "text": {"type": "mrkdwn", "text": f"*{emoji} {title}*\n{summary(key)}"},
                    "accessory": {"type": "button", "action_id": f"panel_{key}", "value": OPEN + key,
                                  "text": winkit.plain("Open")}}]
    return {"type": "modal", "callback_id": MAIN, "title": winkit.plain("🎛️ Control Panel"),
            "close": winkit.plain("Close"), "blocks": blocks}


def section_window(key: str) -> dict | None:
    found = _section(key)
    if found is None:
        return None
    _, emoji, title, actions = found
    options = [winkit.choice(f"*{label}*"[:TEXT_LIMIT], value, words[:TEXT_LIMIT]) for value, label, words, _ in actions]
    blocks = [{"type": "input", "block_id": PICK, "label": winkit.plain("What would you like to do?"),
               "element": {"type": "radio_buttons", "action_id": PICK, "options": options}},
              {"type": "context", "elements": [{"type": "mrkdwn", "text":
                  "A form opens here. Anything else is sent to the chat."}]}]
    window = winkit.modal(GO, f"{emoji} {title}", "Go", blocks)
    window["close"] = winkit.plain("Back")
    return window


def opened_from_panel(window: dict | None) -> dict | None:
    """A form opened from the panel: Save closes the whole panel with it (slackbot reads this mark)."""
    if window is not None:
        found = winkit.meta(window)
        window["private_metadata"] = json.dumps({**found, "panel": True}, ensure_ascii=False)
    return window


def form_for(value: str) -> dict | None:
    """The form window a "window" action opens."""
    if value == "settings":
        return settings_flow.modal(panel=True)
    for form in (reminder_window, gpa_window):
        window = form.opening([value], {}, None)
        if window:
            return opened_from_panel(window)
    return None


def go(payload: dict) -> tuple[dict, str | None]:
    """A section window's Go: (Slack's answer, the chat action to run once Slack has it — None for a form).
    Nothing chosen is said under the list."""
    view = payload.get("view") or {}
    value = winkit.values(view).get(PICK, "")
    action = _action(value)
    if action is None:
        return winkit.problem(PICK, "Choose one of the options first."), None
    if action[3] == "window":
        window = form_for(value)
        if window:
            return {"response_action": "update", "view": window}, None
        return winkit.problem(PICK, "That form is not available right now — try again in a moment."), None
    return {"response_action": "clear"}, value
