"""
Your weekly timetable as the registration portal writes it, read by the bot by itself.

The portal's home page lists every section you take with its meetings in ZUJ's codes:
"[ 19:00_20:30 ] ن (ن) ر (ك)" is the time, then each day's letter with that day's mode
(schedule.parse_codes). The bot keeps that table in data/class_table.json, so it knows
which days are online lectures — for any student, without a schedule.json.

When   only while "auto_check_in" is true in schedule.json — switching it on is what lets
       the bot sign in to the portal for you — and PORTAL_USERNAME / PORTAL_PASSWORD are in
       .env. ONCE: as soon as there is no table (a new student, or the first look of a new
       semester, when the old table is STALE_AFTER old), not again until you run /check — the
       way to tell the bot that your courses changed. After a failed look it waits RETRY_AFTER.
       It never starts while a command of yours is using the portal.
Tells  what it found, on the first look and whenever the timetable changed: the online
       lectures, and how many days are in person or activities — so you can see it understood.
How    the owner's own portal session (portal.fetch_many, the page's rows-per-page set to 15 so
       one look reads every row). If the portal asks for a verification code nobody can
       answer — it never did, in 26 measured sign-ins — you are told once (failsafe) and the
       check-in falls back to the codes in Moodle and schedule.json.
Use    mode_at(start): the mode of the class that day and time, whatever its course — a
       lecture is never two courses at once, so the day and the time are enough.
Days   the university days only (reminders.CAMPUS_DAYS: Sunday to Wednesday): a student's
       courses are split over them — Sun + Tue, Mon + Wed, or all four, each course on two of
       them with its own mode on each (a blended course: in person one day, an activity the
       other, in either order). Thursday to Saturday (a Saturday lab) never count.
Reads  every "(x)" in a row's time must be understood: a row with a code the bot does not
       know is not silently dropped — it is listed in the message and in data/class_table.json.
"""

import json
import logging
import time
from datetime import datetime, timedelta

from uninotifier import failsafe, fileio, notifier, paths, schedule
from uninotifier.reminders import CAMPUS_DAYS

log = logging.getLogger("uninotifier")

CACHE_FILE = paths.DATA / "class_table.json"
RETRY_AFTER = 6 * 3600              # a failed look is tried again this long after
STALE_AFTER = 120 * 86400           # a table this old is a past semester's: not trusted, read again
MATCH = timedelta(minutes=30)       # a session this close to a class's start is that class
TICK = 600                          # seconds between the bot thread's looks


def slots_from_rows(rows: list[dict]) -> list[dict]:
    """The portal's rows (portal.timetable_rows) as one slot per day:
    {"day", "start", "end", "mode", "code", "course_ar", "section", "activity", "room"}."""
    slots = []
    for row in rows:
        for slot in schedule.parse_codes(row.get("time", "")):
            if slot["day"] not in CAMPUS_DAYS:
                continue
            slots.append({**slot, "code": row.get("code", ""), "course_ar": row.get("name", ""),
                          "section": row.get("section", ""), "activity": row.get("activity", ""),
                          "room": row.get("room", "")})
    return slots


def unreadable(rows: list[dict]) -> list[dict]:
    """The rows with a "(x)" the bot does not understand — an unknown day letter or mode letter,
    or no codes at all: [{"code", "time"}]. Never left out silently."""
    return [{"code": row.get("code", ""), "time": row.get("time", "")} for row in rows
            if not row.get("time", "").count("(")
            or len(schedule.parse_codes(row["time"])) != row["time"].count("(")]


def load() -> dict:
    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def known(now: float | None = None) -> bool:
    """A table is on file and recent enough to trust."""
    cache = load()
    now = time.time() if now is None else now
    return bool(cache.get("slots")) and now - cache.get("read", 0) < STALE_AFTER


def mode_at(start: datetime, now: float | None = None) -> str | None:
    """The mode ("Online", "In-Person", "Blended", "Activity") of the class that starts
    near `start` on its weekday, or None when the table has none (or no table)."""
    if not known(now):
        return None
    day = schedule.DAY_KEYS[start.weekday()]
    best = None
    for slot in load()["slots"]:
        if slot.get("day") != day or day not in CAMPUS_DAYS:      # a table kept by an older version may hold a Saturday
            continue
        try:
            at = datetime.combine(start.date(), datetime.strptime(slot["start"], "%H:%M").time())
        except (KeyError, ValueError):
            continue
        gap = abs(at - start)
        if gap <= MATCH and (best is None or gap < best[0]):
            best = (gap, slot.get("mode"))
    return best[1] if best else None


# ------------------------------------------------------------------ reading

def wanted() -> bool:
    return schedule.load_schedule().get("auto_check_in") is True


def due(now: float) -> bool:
    """No table (or a past semester's) and not tried lately."""
    return not known(now) and now - load().get("tried", 0) >= RETRY_AFTER


def _no_code(attempt: int, rejected: bool) -> None:
    return None                       # nobody can type a code for a background look


def read() -> tuple[list[dict], list[dict]]:
    """(the slots, the rows not understood) on the portal's home page now. Raises
    portal.PortalError (something to read) or maintenance.PortalUnavailable (try later)."""
    from uninotifier import portal
    rows = portal.fetch_many(["home"], _no_code)["home"].data.get("timetable") or []
    return slots_from_rows(rows), unreadable(rows)


def refresh(now: float | None = None) -> bool:
    """Read the portal and keep the table. True when a table was saved."""
    from uninotifier import maintenance, portal
    now = time.time() if now is None else now
    cache = load()
    cache["tried"] = now
    try:
        slots, unclear = read()
    except maintenance.PortalUnavailable as e:
        log.info("Timetable: the portal is not available (%s) — trying again later", e)
    except portal.PortalError as e:
        log.warning("Timetable: %s", e)
        failsafe.report("classtable:portal", "The bot could not read your timetable from the portal",
                        detail=f"{e} Automatic check-in goes on with the codes in Moodle and schedule.json; "
                               f"send /check to try again, or the bot tries by itself in "
                               f"{RETRY_AFTER // 3600} hours.", wait=False)
    else:
        if slots:
            cache.update(read=now, slots=slots, unreadable=unclear)
            log.info("Timetable: read %d class day(s) from the portal%s", len(slots),
                     f", {len(unclear)} row(s) not understood" if unclear else "")
            fileio.write_json_atomic(CACHE_FILE, cache)
            return True
        log.warning("Timetable: the portal's home page showed no timetable")
    fileio.write_json_atomic(CACHE_FILE, cache)
    return False


def summary(slots: list[dict], first: bool, unclear: list[dict] | None = None) -> str:
    """What the bot understood, for the chat."""
    online = sorted((s for s in slots if s["mode"] == "Online"),
                    key=lambda s: (schedule.DAY_KEYS.index(s["day"]), s["start"]))
    lines = ["🗓️ *Timetable read from the portal*" if first else "🗓️ *Your timetable on the portal changed*", ""]
    if online:
        lines.append("💻 *Online lectures* (I check you in to these):")
        lines += [f"• {schedule.DAY_ABBR[s['day']]} · {notifier.hhmm12(s['start'])} – {notifier.hhmm12(s['end'])}"
                  for s in online]
    else:
        lines.append("💻 No online lectures — there is nothing for me to check in to.")
    campus = sum(s["mode"] in ("In-Person", "Blended") for s in slots)
    activity = sum(s["mode"] == "Activity" for s in slots)
    lines += ["", f"🏫 {campus} in-person day(s) · 🧩 {activity} activity day(s) — I leave those alone."]
    if unclear:
        lines += ["", f"⚠️ I could not read {len(unclear)} row(s) of the timetable (course "
                      f"{', '.join(r['code'] or '?' for r in unclear)}): for those days the check-in "
                      "uses the codes in Moodle and your schedule.json."]
    lines.append("Courses changed? Send /check and I read it again.")
    return "\n".join(lines)


def update(token: str, chat_id: str, lock=None, now: float | None = None, force: bool = False) -> bool:
    """Read the portal's timetable when it is due — or whenever `force` (a manual /check) — and
    tell what it found when it is new or changed. True when it was read."""
    from uninotifier import portal
    now = time.time() if now is None else now
    if not wanted() or portal.credentials() is None or not (force or due(now)):
        return False
    if lock is not None and not lock.acquire(blocking=False):
        return False                  # a command of yours is using the portal: the next look
    try:
        before = load()
        if not refresh(now):
            return False
        after = load()
        if (after["slots"], after.get("unreadable")) != (before.get("slots"), before.get("unreadable")):
            notifier.send_message(token, chat_id, summary(after["slots"], first=not before.get("slots"),
                                                          unclear=after.get("unreadable")))
        return True
    finally:
        if lock is not None:
            lock.release()


def loop(token: str, chat_id: str, lock=None) -> None:
    """The bot's timetable thread."""
    while True:
        try:
            update(token, chat_id, lock)
        except Exception as e:
            log.exception("Reading the timetable failed")
            failsafe.bug("bot:classtable", "Reading your timetable from the portal is failing", e,
                         "Automatic check-in goes on with the codes in Moodle and schedule.json.")
        time.sleep(TICK)


if __name__ == "__main__":         # python -m uninotifier.classtable — read it now
    print("read" if refresh() else "not read (see the log)")
