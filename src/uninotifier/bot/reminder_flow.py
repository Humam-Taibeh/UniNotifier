"""
Personal reminders in the chat: 📌 Add to Reminders, ➕ New Custom Reminder,
the day / hour pickers, Manage Reminders, Snooze, Done and View Original Post.
"""

from datetime import datetime
import logging

from uninotifier import daily
from uninotifier import notifier
from uninotifier import reminders
from uninotifier.bot import chat
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")


# ------------------------------------------------------------- reminders

_reminder_draft: dict = {}           # the reminder being scheduled: note, course, url, day


_reminder_wait: str | None = None    # "note" | "time": the next plain message answers it


REMINDER_CANCEL_ROW = [{"text": "✖️ Cancel", "callback_data": "rem:cancel"}]


def view_when(draft: dict) -> tuple[str, list]:
    """📌 → when should it come back?"""
    lines = ["📌 *Add to Reminders*", "", f"📝 {escape_md(draft['note'])}"]
    if draft.get("course"):
        lines.append(f"📘 *Course:* {escape_md(draft['course'])}")
    lines += ["", "When should I remind you?"]
    rows = []
    campus = reminders.next_campus_day()
    if campus:
        rows.append([{"text": f"🏫 Next Campus Day · {reminders.when_text(campus.timestamp())}",
                      "callback_data": "rem:q:campus"}])
    lecture = reminders.before_next_lecture(draft.get("course"))
    if lecture:
        rows.append([{"text": f"⏰ Before Next Lecture · {reminders.when_text(lecture[0].timestamp())}",
                      "callback_data": "rem:q:lecture"}])
        if draft.get("course") and lecture[1] != draft["course"]:
            lines.insert(-1, f"ℹ️ {escape_md(draft['course'])} has no in-person lecture — "
                             f"⏰ uses your next one ({escape_md(lecture[1])}).")
    rows += [[{"text": "🗓️ Custom Date & Time", "callback_data": "rem:q:custom"}], REMINDER_CANCEL_ROW]
    return "\n".join(lines), rows


def view_days() -> tuple[str, list]:
    buttons = [{"text": label, "callback_data": f"rem:day:{day:%Y%m%d}"} for label, day in reminders.day_choices()]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([{"text": "✏️ Type a Date", "callback_data": "rem:typedate"}])
    return (f"🗓️ *Pick a day*\n\n📝 {escape_md(_reminder_draft.get('note', ''))}",
            rows + [REMINDER_CANCEL_ROW])


def view_hours(day: str) -> tuple[str, list]:
    picked = datetime.strptime(day, "%Y%m%d").date()
    buttons = [{"text": notifier.hhmm12(h), "callback_data": f"rem:hr:{day}:{h.replace(':', '')}"}
               for h in reminders.hour_choices(picked)]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows += [[{"text": "✏️ Type a Time", "callback_data": f"rem:type:{day}"}],
             [{"text": "🔙 Another Day", "callback_data": "rem:q:custom"}], REMINDER_CANCEL_ROW]
    return f"🕐 *Pick a time — {reminders.short_day(picked)} {daily.MONTHS[picked.month - 1]}*", rows


def schedule_draft(due: datetime) -> tuple[str, list]:
    """Save the drafted reminder for `due`; the confirmation screen. A reminder being changed (the draft
    carries its id) gets the new time and goes back to its own screen."""
    if _reminder_draft.get("edit"):
        item_id = _reminder_draft["edit"]
        _reminder_draft.clear()
        if reminders.update(item_id, due=due.timestamp()):
            return view_reminder(item_id, "✅ *Time changed*")
        return "That reminder is already gone.", [[{"text": "🔙 Tasks & Reminders", "callback_data": "tasks"}]]
    item = reminders.add(_reminder_draft["note"], due.timestamp(), _reminder_draft.get("course"),
                         _reminder_draft.get("url"), _reminder_draft.get("message_id"))
    _reminder_draft.clear()
    text = "\n".join(["✅ *Reminder set*", "", f"📝 {escape_md(item['note'])}",
                      f"🕐 {reminders.when_text(item['due'])} · in {chat.remaining_text(item['due'])}",
                      "", "I'll send it here at that time."])
    return text, [[{"text": "⏳ Tasks & Reminders", "callback_data": "tasks"}], chat.BACK_ROW]


def view_manage_reminders() -> tuple[str, list]:
    mine = reminders.pending()
    lines = ["✏️ *Manage Reminders*", "", "Tap one to read it, change it or delete it." if mine else "No reminders left."]
    rows = [[{"text": f"{chat._short(_first_line(r), 30)} · {reminders.when_text(r['due'])}",
              "callback_data": f"rem:open:{r['id']}"}] for r in mine]
    return "\n".join(lines), rows + [[{"text": "🔙 Tasks & Reminders", "callback_data": "tasks"}]]


def _first_line(reminder: dict) -> str:
    return next((x.strip() for x in (reminder.get("note") or "").splitlines() if x.strip()), "Reminder")


def view_reminder(reminder_id: int, header: str = "📝 *Reminder*") -> tuple[str, list]:
    """One reminder in full, with what can be done to it: change its text, its day, its time, or delete it."""
    item = reminders.get(reminder_id)
    if not item:
        return "That reminder is already gone.", [[{"text": "🔙 Manage Reminders", "callback_data": "rem:list"}]]
    lines = [header, "", escape_md(item["note"])]
    if item.get("course"):
        lines.append(f"\n📘 *Course:* {escape_md(item['course'])}")
    lines.append(f"\n🕐 {reminders.when_text(item['due'])} · in {chat.remaining_text(item['due'])}")
    rows = [[{"text": "✏️ Edit Text", "callback_data": f"rem:et:{reminder_id}"}],
            [{"text": "🗓️ Change Date", "callback_data": f"rem:ed:{reminder_id}"},
             {"text": "🕐 Change Time", "callback_data": f"rem:eh:{reminder_id}"}]]
    if item.get("message_id"):
        rows.append([{"text": "🔗 View Original Post", "callback_data": f"rem:view:{reminder_id}"}])
    rows += [[{"text": "🗑️ Delete", "callback_data": f"rem:del:{reminder_id}"}],
             [{"text": "🔙 Manage Reminders", "callback_data": "rem:list"}]]
    return "\n".join(lines), rows


def view_confirm_delete(reminder_id: int) -> tuple[str, list]:
    item = reminders.get(reminder_id)
    if not item:
        return view_reminder(reminder_id)
    return (f"🗑️ *Delete this reminder?*\n\n{escape_md(item['note'])}\n\n🕐 {reminders.when_text(item['due'])}",
            [[{"text": "🗑️ Yes, Delete", "callback_data": f"rem:yes:{reminder_id}"},
              {"text": "🔙 Keep It", "callback_data": f"rem:open:{reminder_id}"}]])


def _edit_draft(reminder_id: int) -> dict | None:
    """Starts changing a reminder: the pickers work on a draft carrying its note, so they are the new
    reminder's own pickers, and the draft's `edit` sends the result back to this reminder."""
    item = reminders.get(reminder_id)
    if not item:
        return None
    _reminder_draft.clear()
    _reminder_draft.update(note=item["note"], course=item.get("course"), edit=reminder_id)
    return item


def start_reminder(source: dict | None = None, message_id: int | None = None) -> None:
    """➕ (no source): ask for the note; 📌 (source): straight to the time.
    message_id: the chat message the 📌 sits under — the reminder links back to it."""
    global _reminder_wait
    _reminder_draft.clear()
    if source is None:
        _reminder_wait = "note"
        chat.send_view("➕ *New Custom Reminder*\n\nSend the note — a short line, e.g. "
                  "“Buy the textbook at the Faculty of Arts, floor 1”.", [REMINDER_CANCEL_ROW])
        return
    _reminder_wait = None
    _reminder_draft.update(note=source["note"], course=source.get("course"), url=source.get("url"),
                           message_id=message_id)
    chat.send_view(*view_when(_reminder_draft))


def handle_reminder_text(text: str) -> None:
    """A typed note or time for the reminder being drafted."""
    global _reminder_wait
    if _reminder_wait == "note":
        _reminder_wait = None
        start_reminder({"note": text})
        return
    if _reminder_wait == "edit":
        item_id = _reminder_draft.get("edit")
        if not text.strip():
            chat.send_view("⚠️ Send the new text.", [REMINDER_CANCEL_ROW])
            return
        _reminder_wait = None
        _reminder_draft.clear()
        chat.send_view(*view_reminder(item_id, "✅ *Text changed*") if reminders.update(item_id, note=text)
                       else ("That reminder is already gone.", [chat.BACK_ROW]))
        return
    if _reminder_wait == "date":
        try:
            day = reminders.parse_date(text)
        except ValueError as e:
            chat.send_view(f"⚠️ {escape_md(str(e))}", [REMINDER_CANCEL_ROW])
            return
        _reminder_wait = None
        _reminder_draft["day"] = f"{day:%Y%m%d}"
        chat.send_view(*view_hours(_reminder_draft["day"]))
        return
    try:
        hour, minute = reminders.parse_time(text)
        day = datetime.strptime(_reminder_draft["day"], "%Y%m%d")
    except (ValueError, KeyError) as e:
        chat.send_view(f"⚠️ {escape_md(str(e) if isinstance(e, ValueError) else 'Pick the day again.')}",
                  [REMINDER_CANCEL_ROW])
        return
    due = day.replace(hour=hour, minute=minute)
    if due <= datetime.now():
        chat.send_view("⚠️ That time has already passed — send a later one.", [REMINDER_CANCEL_ROW])
        return
    _reminder_wait = None
    chat.send_view(*schedule_draft(due))


def show_original(reminder_id: int, answer) -> None:
    """🔗 View Original Post: a reply to the pinned message — tapping its quote
    jumps straight to the announcement and its screenshot in the chat."""
    item = reminders.get(reminder_id)
    if not item or not item.get("message_id"):
        answer("That reminder has no original post.")
        return
    text = f"📌 *Original post* ⬆️ — tap the quote to jump to it.\n📝 {escape_md(item['note'])}"
    way_back = [[{"text": "⏳ Tasks & Reminders", "callback_data": "tasks"}], chat.BACK_ROW]
    sent = chat.send_reply_view(text, way_back, item["message_id"])       # the bottom of the chat, with its way back
    if sent:
        answer()
        return
    # the message was deleted from the chat: the post itself is still on Moodle
    answer("⚠️ That message is no longer in the chat.")
    rows = [[{"text": "🌐 Open on Moodle", "url": item["url"]}]] if item.get("url") else []
    chat.send_view(f"⚠️ The original message was deleted from the chat.\n📝 {escape_md(item['note'])}",
              rows + [[{"text": "⏳ Tasks & Reminders", "callback_data": "tasks"}], chat.BACK_ROW])


def _strip_actions(cb: dict) -> None:
    """Take Snooze / Done off a delivered reminder, keeping its link button."""
    message = cb.get("message") or {}
    rows = [row for row in (message.get("reply_markup") or {}).get("inline_keyboard", [])
            if not any(b.get("callback_data", "").startswith("rem:") for b in row)]
    if message.get("message_id"):
        chat.api("editMessageReplyMarkup", chat_id=chat.CHAT_ID, message_id=message["message_id"],
            reply_markup={"inline_keyboard": rows})


def handle_reminder_callback(cb: dict, action: str, message_id: int | None) -> None:
    global _reminder_wait
    parts = action.split(":")[1:]
    kind = parts[0] if parts else ""

    def answer(text: str | None = None) -> None:
        chat.answer(cb["id"], text)

    def show(view: tuple[str, list]) -> None:
        chat.edit_view(message_id, *view) if message_id else chat.send_view(*view)

    if kind == "new":
        answer()
        start_reminder()
    elif kind == "s":
        found = reminders.source(parts[1]) if len(parts) > 1 else None
        if not found:
            answer("⌛ That post is too old to pin — add it with ➕ New Custom Reminder.")
            return
        answer()
        start_reminder(found, (cb.get("message") or {}).get("message_id"))
    elif kind == "open":
        answer()
        _reminder_draft.clear()
        _reminder_wait = None
        show(view_reminder(int(parts[1])))
    elif kind in ("et", "ed", "eh"):
        item = _edit_draft(int(parts[1]))
        if not item:
            answer("Already gone")
            show(view_manage_reminders())
            return
        answer()
        if kind == "ed":
            show(view_days())
        elif kind == "eh":
            _reminder_draft["day"] = f"{datetime.fromtimestamp(item['due']):%Y%m%d}"
            show(view_hours(_reminder_draft["day"]))
        else:
            _reminder_wait = "edit"
            chat.send_view(f"✏️ *Edit Text*\n\nNow:\n{escape_md(item['note'])}\n\nSend the new text.", [REMINDER_CANCEL_ROW])
    elif kind in ("q", "day", "hr", "type", "typedate") and not _reminder_draft.get("note"):
        answer("⌛ That reminder draft expired — start again.")
    elif kind == "q":
        answer()
        if parts[1] == "custom":
            show(view_days())
            return
        when = (reminders.next_campus_day() if parts[1] == "campus"
                else (reminders.before_next_lecture(_reminder_draft.get("course")) or (None,))[0])
        if when is None:
            show(view_days())
            return
        show(schedule_draft(when))
    elif kind == "day":
        answer()
        _reminder_draft["day"] = parts[1]
        show(view_hours(parts[1]))
    elif kind == "hr":
        answer()
        due = datetime.strptime(parts[1] + parts[2], "%Y%m%d%H%M")
        show(schedule_draft(due) if due > datetime.now() else view_hours(parts[1]))
    elif kind == "type":
        answer()
        _reminder_draft["day"], _reminder_wait = parts[1], "time"
        chat.send_view("✏️ *Type a time* — e.g. `18:45` or `6:45 PM`", [REMINDER_CANCEL_ROW])
    elif kind == "typedate":
        answer()
        _reminder_wait = "date"
        chat.send_view("✏️ *Type a date* — e.g. `26/11`, `26/11/2026` or `26 Nov`", [REMINDER_CANCEL_ROW])
    elif kind == "cancel":
        answer("✖️ Cancelled")
        editing = _reminder_draft.get("edit")
        _reminder_draft.clear()
        _reminder_wait = None
        show(view_reminder(editing) if editing else ("✖️ *Reminder cancelled.*", [chat.BACK_ROW]))
    elif kind == "list":
        answer()
        show(view_manage_reminders())
    elif kind == "view":
        show_original(int(parts[1]), answer)
    elif kind == "del":                                   # asks first: a tap never deletes
        answer()
        show(view_confirm_delete(int(parts[1])))
    elif kind == "yes":
        removed = reminders.remove(int(parts[1]))
        answer("🗑️ Deleted" if removed else "Already gone")
        show(view_manage_reminders())
    elif kind == "z":
        item = reminders.snooze(int(parts[1]))
        answer(f"⏰ Snoozed until {reminders.when_text(item['due'])}" if item else "Already gone")
        _strip_actions(cb)
    elif kind == "done":
        reminders.remove(int(parts[1]))
        answer("✅ Done")
        _strip_actions(cb)
    else:
        answer()
