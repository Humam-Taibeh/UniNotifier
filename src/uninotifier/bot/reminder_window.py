"""
⏰ Reminders as a Slack window: the note, a date picker, a time picker and quick picks — in one place, no typing of
dates and no chain of day and hour screens.

  ➕ New Custom Reminder · 📌 Add to Reminders · ✏️ Edit Text / 🗓️ Change Date / 🕐 Change Time of a reminder
  all open the same window (an edit has the reminder's own text, day and time in it); Save sets it.

Quick picks above the date and time fill them in for you (next campus day, before the next lecture, in an hour,
tomorrow morning) and keep what you typed. A time that has already passed is said under the time, nothing saved.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from uninotifier import reminders
from uninotifier.bot import winkit

log = logging.getLogger("uninotifier")

CALLBACK = "reminder"
OPENING = ("rem:new", "rem:et", "rem:ed", "rem:eh")      # + "rem:s:<key>" (a 📌 under a notification)
QUICK = "quick"


def default_time(now: datetime | None = None) -> datetime:
    """The next whole hour — or tomorrow at 8 when that would be late at night or very early."""
    now = now or datetime.now()
    soon = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if soon.hour >= 23 or soon.hour < 6:
        soon = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    return soon


def quick_picks(course: str | None, now: datetime | None = None) -> list[tuple[str, str, datetime]]:
    """(button, id, time) for the quick row: only the picks that exist (a timetable may have no campus day)."""
    now = now or datetime.now()
    picks = []
    campus = reminders.next_campus_day(now)
    if campus:
        picks.append((f"🏫 Next campus day · {reminders.when_text(campus.timestamp())}", "campus", campus))
    lecture = reminders.before_next_lecture(course, now)
    if lecture:
        picks.append((f"⏰ Before next lecture · {reminders.when_text(lecture[0].timestamp())}", "lecture", lecture[0]))
    picks.append(("🕐 In 1 hour", "hour", now.replace(second=0, microsecond=0) + timedelta(hours=1)))
    morning = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    picks.append(("🌅 Tomorrow 8:00 AM", "morning", morning))
    return picks


def build(note: str = "", due: datetime | None = None, meta: dict | None = None) -> dict:
    meta = dict(meta or {})
    due = due or default_time()
    editing = bool(meta.get("edit"))
    heading = ("*Change this reminder.* Its text, day and time are all here — *Save* applies them." if editing
               else "*Remind me* — write it, pick the day and the time, and tap *Set Reminder*. "
                    "I'll send it here at that minute.")
    if meta.get("course"):
        heading += f"\n📘 {meta['course']}"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": heading}},
              winkit.text_field("note", "Reminder", note, hint="e.g. Buy the textbook, Faculty of Arts, floor 1",
                                multiline=True, limit=reminders.NOTE_LIMIT),
              {"type": "actions", "block_id": QUICK, "elements": [
                  {"type": "button", "action_id": f"q_{pick}", "value": f"rem:q:{pick}", "text": winkit.plain(label[:75])}
                  for label, pick, _ in quick_picks(meta.get("course"))[:5]]},
              winkit.date_field("day", "Day", f"{due:%Y-%m-%d}"),
              winkit.time_field("time", "Time", f"{due:%H:%M}")]
    title = "✏️ Edit Reminder" if editing else ("📌 Add to Reminders" if meta.get("key") else "⏰ New Reminder")
    return winkit.modal(CALLBACK, title, "Save" if editing else "Set Reminder", blocks, meta)


def opening(values: list[str], payload: dict, message_number: int | None) -> dict | None:
    """The window for a press that asks for it, else None (an ordinary press)."""
    value = next((v for v in values if v.startswith("rem:") and (v in OPENING or v.startswith(
        ("rem:s:", "rem:et:", "rem:ed:", "rem:eh:")))), "")
    if not value:
        return None
    parts = value.split(":")
    meta = {"message_id": message_number}
    if parts[1] == "new":
        return build(meta=meta)
    if parts[1] == "s":
        key = value.split(":", 2)[2] if len(parts) > 2 else ""
        found = reminders.source(key) if key else None
        if not found:
            return None                                   # too old: the ordinary press says so
        meta.update(key=key, course=found.get("course"), url=found.get("url"))
        return build(found["note"], meta=meta)
    item = reminders.get(int(parts[2])) if len(parts) > 2 and parts[2].isdigit() else None
    if not item:
        return None
    meta.update(edit=item["id"], course=item.get("course"))
    return build(item["note"], datetime.fromtimestamp(item["due"]), meta)


def modal_action(payload: dict) -> dict | None:
    """A quick pick pressed inside the window: the same window again with the day and time filled in — and what
    was typed in the note kept."""
    view = payload.get("view") or {}
    value = next((v for v in (a.get("value") or "" for a in payload.get("actions") or []) if v.startswith("rem:q:")), "")
    if view.get("callback_id") != CALLBACK or not value:
        return None
    meta = winkit.meta(view)
    chosen = next((when for _, pick, when in quick_picks(meta.get("course")) if pick == value.split(":")[2]), None)
    if chosen is None:
        return None
    return build(winkit.values(view).get("note", ""), chosen, meta)


def due_from(values: dict) -> datetime | None:
    try:
        return datetime.strptime(f"{values.get('day')} {values.get('time')}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def submit(payload: dict) -> tuple[dict, Callable[[], str | None] | None]:
    """Slack's acknowledgement for Save (what to answer) and the work to do after it (or None)."""
    view = payload.get("view") or {}
    values, meta = winkit.values(view), winkit.meta(view)
    note = (values.get("note") or "").strip()
    due = due_from(values)
    if not note:
        return winkit.problem("note", "Write what to remind you about."), None
    if due is None:
        return winkit.problem("time", "Pick the day and the time."), None
    if due <= datetime.now():
        return winkit.problem("time", "That time has already passed — pick a later one."), None
    return {}, lambda: finish(note, due, meta)


def finish(note: str, due: datetime, meta: dict) -> str:
    """Save the reminder (after Slack was answered: reading a post's page can take a moment). The line to tell you."""
    from uninotifier.bot import chat, reminder_flow
    when = reminders.when_text(due.timestamp())
    if meta.get("edit"):
        item = reminders.update(meta["edit"], note=note, due=due.timestamp())
        if not item:
            return "That reminder is already gone."
        if meta.get("message_id"):
            chat.edit_view(meta["message_id"], *reminder_flow.view_reminder(meta["edit"], "✅ *Reminder changed*"))
        return f"✅ *Reminder changed* — {when}"
    found = reminders.source(meta["key"]) if meta.get("key") else None
    if found and note == (found.get("note") or "").strip():     # unchanged: it keeps the post's own wording too
        note = reminder_flow.with_words(note, found.get("url"))
    reminders.add(note, due.timestamp(), meta.get("course"), meta.get("url"), meta.get("message_id"), meta.get("key"))
    return f"✅ *Reminder set* — {when}"
