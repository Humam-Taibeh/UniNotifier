"""
Personal reminders: notes the user schedules from the chat, delivered as a
push notification at the exact minute.

Where they come from:
  ➕ New Custom Reminder   (Upcoming Tasks & Reminders, /remind) — a typed note
  📌 Add to Reminders      under an announcement, forum post or course page;
                           the monitor files what the button refers to in
                           reminder_sources.json, since a button carries only
                           a short key ("p<post id>" / "i<cmid>")

When: 🏫 Next Campus Day (an hour before the first in-person class of the next
campus day — Sunday to Wednesday, the university's regular days), ⏰ Before
Next Lecture (30 min before the course's next in-person lecture on those days;
online sessions, activity slots and weekend labs don't count), or 🗓️ a day and
time picked with buttons.

A reminder pinned from a message remembers that message's Telegram id: it is
delivered as a reply to it (the announcement is quoted right in the
notification), and 🔗 View Original Post in Upcoming Tasks jumps back to it.

Delivery: the bot's 20-second clock sends what is due; the scheduled monitor
does the same as a fallback when the bot is down. A delivered reminder is an
exclusively-created marker file (as in lectures.py), so it goes out once even
with both running. reminders.json is written only by the bot.
"""

import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

from uninotifier import daily
from uninotifier import failsafe
from uninotifier import fileio
from uninotifier import notifier
from uninotifier import schedule
from uninotifier import userdata
from uninotifier.notifier import escape_md
from uninotifier import paths

log = logging.getLogger("uninotifier")

STATE_FILE = paths.DATA / "reminders.json"
SOURCES_FILE = paths.DATA / "reminder_sources.json"
CLAIM_DIR = paths.DATA / "reminders_sent"
KEEP_SOURCES = 300
KEEP_DELIVERED_SECONDS = 7 * 86400
CAMPUS_LEAD = timedelta(hours=1)          # before the first in-person class of the day
LECTURE_LEAD = timedelta(minutes=30)      # before the next class
CAMPUS_DAYS = ("sun", "mon", "tue", "wed")   # the regular university days; Thu–Sat never count
CAMPUS_MODE = "In-Person"                    # online sessions and activity slots are not on campus
HOURS = ("08:00", "09:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00")
SNOOZE = timedelta(hours=1)
NOTE_LIMIT = 300

PIN_BUTTON = "📌 Add to Reminders"
TIME_RE = re.compile(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|ص|م)?\s*", re.I)


# ------------------------------------------------------------------- store

def load() -> dict:
    state = userdata.load(STATE_FILE, "your reminders")
    state.setdefault("next_id", 1)
    state.setdefault("items", [])
    return state


def save(state: dict) -> None:
    userdata.save(STATE_FILE, state)


def add(note: str, due: float, course: str | None = None, url: str | None = None,
        message_id: int | None = None) -> dict:
    """message_id: the Telegram message the 📌 was tapped on (None for a typed note)."""
    state = load()
    item = {"id": state["next_id"], "note": note.strip()[:NOTE_LIMIT], "due": due,
            "course": course, "url": url, "message_id": message_id, "created": time.time()}
    state["next_id"] += 1
    state["items"].append(item)
    prune(state)
    save(state)
    return item


def get(reminder_id: int) -> dict | None:
    return next((r for r in load()["items"] if r["id"] == reminder_id), None)


def remove(reminder_id: int) -> dict | None:
    state = load()
    found = next((r for r in state["items"] if r["id"] == reminder_id), None)
    if found:
        state["items"].remove(found)
        save(state)
    return found


def snooze(reminder_id: int, now: float | None = None) -> dict | None:
    state = load()
    found = next((r for r in state["items"] if r["id"] == reminder_id), None)
    if found:
        found["due"] = (now or time.time()) + SNOOZE.total_seconds()
        save(state)
    return found


def prune(state: dict, now: float | None = None) -> None:
    """Forget reminders delivered more than a week ago."""
    cutoff = (now or time.time()) - KEEP_DELIVERED_SECONDS
    state["items"] = [r for r in state["items"] if not (delivered(r) and r["due"] < cutoff)]


def pending(now: float | None = None) -> list[dict]:
    """Reminders still to come (or due and not yet sent), soonest first."""
    return sorted((r for r in load()["items"] if not delivered(r)), key=lambda r: r["due"])


# ----------------------------------------------------------------- sources

def remember_source(key: str, note: str, course: str | None, url: str | None) -> None:
    """What a 📌 button refers to (written by the monitor when it sends one)."""
    try:
        sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sources = {}
    sources[key] = {"note": note.strip()[:NOTE_LIMIT], "course": course, "url": url, "at": time.time()}
    if len(sources) > KEEP_SOURCES:
        for old in sorted(sources, key=lambda k: sources[k]["at"])[:len(sources) - KEEP_SOURCES]:
            del sources[old]
    fileio.write_json_atomic(SOURCES_FILE, sources)


def source(key: str) -> dict | None:
    try:
        return json.loads(SOURCES_FILE.read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError):
        return None


def pin_row(key: str) -> list[dict]:
    return [{"text": PIN_BUTTON, "callback_data": f"rem:s:{key}"}]


# ------------------------------------------------------------------ timing

def _class_starts(sch: dict, now: datetime, days: int = 14):
    """(start datetime, lecture) for every in-person class on a campus day in
    the coming `days`."""
    for offset in range(days):
        day = (now + timedelta(days=offset)).date()
        key = schedule.DAY_KEYS[day.weekday()]
        if key not in CAMPUS_DAYS:
            continue
        for lec in sch.get("days", {}).get(key, []):
            if lec["mode"] == CAMPUS_MODE:
                h, m = map(int, lec["start"].split(":"))
                yield datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m), lec


def next_campus_day(now: datetime | None = None, sch: dict | None = None) -> datetime | None:
    """An hour before the first in-person class of the next campus day."""
    now = now or datetime.now()
    sch = sch or schedule.load_schedule()
    firsts: dict[date, datetime] = {}
    for start, _ in _class_starts(sch, now):
        firsts[start.date()] = min(firsts.get(start.date(), start), start)
    times = sorted(t - CAMPUS_LEAD for t in firsts.values() if t - CAMPUS_LEAD > now)
    return times[0] if times else None


def before_next_lecture(course: str | None, now: datetime | None = None,
                        sch: dict | None = None) -> tuple[datetime, str] | None:
    """(time, course) 30 min before the course's next in-person lecture — the
    next one of any course when it has none (an online course) or none is given."""
    now = now or datetime.now()
    sch = sch or schedule.load_schedule()
    slots = sorted((start - LECTURE_LEAD, lec["course"]) for start, lec in _class_starts(sch, now)
                   if start - LECTURE_LEAD > now)
    own = [s for s in slots if course and s[1] == course]
    return (own or slots or [None])[0]


def parse_time(text: str) -> tuple[int, int]:
    """'18:45', '6:45 pm', '6pm', '9' -> (hour, minute). Raises ValueError."""
    match = TIME_RE.fullmatch(text or "")
    if not match:
        raise ValueError("Send a time like 18:45 or 6:45 PM.")
    hour, minute, half = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower()
    if half in ("pm", "م") and hour < 12:
        hour += 12
    elif half in ("am", "ص") and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("That isn't a time of day — e.g. 18:45 or 6:45 PM.")
    return hour, minute


def when_text(ts: float) -> str:
    """'Wed 14 Oct · 6:00 PM'."""
    dt = datetime.fromtimestamp(ts)
    return f"{short_day(dt.date())} {daily.MONTHS[dt.month - 1]} · {notifier.clock12(dt.hour, dt.minute)}"


def short_day(day: date) -> str:
    """'Wed 14' (English whatever the system locale)."""
    return f"{daily.WEEKDAYS[day.weekday()][:3]} {day.day}"


def day_choices(now: datetime | None = None) -> list[tuple[str, date]]:
    """(button label, day) for the day picker: Today (while hours remain),
    Tomorrow, Next Sunday, then the rest of the coming week."""
    now = now or datetime.now()
    today = now.date()
    picks: list[tuple[str, date]] = []
    if now.hour < 22:
        picks.append(("Today", today))
    picks.append(("Tomorrow", today + timedelta(days=1)))
    sunday = today + timedelta(days=(6 - today.weekday()) % 7 or 7)
    picks.append(("Next Sunday", sunday))
    seen = {d for _, d in picks}
    for offset in range(2, 9):
        day = today + timedelta(days=offset)
        if day not in seen:
            picks.append((short_day(day), day))
    return picks


def hour_choices(day: date, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    return [h for h in HOURS
            if datetime.combine(day, datetime.min.time()).replace(hour=int(h[:2])) > now]


# ---------------------------------------------------------------- delivery

def _key(item: dict) -> str:
    return f"{item['id']}-{int(item['due'])}"        # a snoozed reminder is a new delivery


def delivered(item: dict) -> bool:
    return (CLAIM_DIR / f"{_key(item)}.sent").exists()


def _claim(item: dict) -> bool:
    CLAIM_DIR.mkdir(exist_ok=True)
    try:
        os.close(os.open(CLAIM_DIR / f"{_key(item)}.sent", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def _release(item: dict) -> None:
    (CLAIM_DIR / f"{_key(item)}.sent").unlink(missing_ok=True)


def message(item: dict) -> tuple[str, list]:
    lines = ["⏰ *Reminder*", "", f"📝 {escape_md(item['note'])}"]
    if item.get("course"):
        lines.append(f"📘 *Course:* {escape_md(item['course'])}")
    lines.append(f"🕐 *Set for:* {when_text(item['due'])}")
    rows = [[{"text": "🌐 Open on Moodle", "url": item["url"]}]] if item.get("url") else []
    rows.append([{"text": "⏰ Snooze 1 h", "callback_data": f"rem:z:{item['id']}"},
                 {"text": "✅ Done", "callback_data": f"rem:done:{item['id']}"}])
    return "\n".join(lines), rows


def send_due(token: str, chat_id: str, now: float | None = None) -> int:
    """Send every reminder whose time has come; returns how many went out."""
    now = now or time.time()
    sent = 0
    for item in load()["items"]:
        if item["due"] > now or not _claim(item):
            continue
        text, rows = message(item)
        try:
            ok = notifier.send_message(token, chat_id, text, rows=rows, reply_to=item.get("message_id"))
        except Exception as e:
            log.exception("Reminder %s failed", item["id"])
            failsafe.bug("reminders", "A reminder could not be sent", e, "It is tried again on the next tick.")
            ok = False
        if ok:
            sent += 1
            log.info("Sent reminder %s", item["id"])
        else:
            _release(item)                              # try again on the next tick
    return sent
