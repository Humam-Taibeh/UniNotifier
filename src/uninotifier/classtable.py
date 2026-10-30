"""
Your weekly timetable as the registration portal writes it, read by the bot by itself.

The portal's home page lists every section you take with its meetings in ZUJ's codes:
"[ 19:00_20:30 ] ن (ن) ر (ك)" is the time, then each day's letter with that day's mode
(schedule.parse_codes). The bot keeps that table in data/class_table.json, so it knows
which days are online lectures — for any student, without a schedule.json.

When   only while "auto_check_in" is true in schedule.json — switching it on is what lets
       the bot sign in to the portal for you — and PORTAL_USERNAME / PORTAL_PASSWORD are in
       .env. Right away when there is no table yet (a new student, a new semester's first
       look), then every READ_EVERY; after a failure it waits RETRY_AFTER before it tries
       again. It never starts while a command of yours is using the portal.
How    the owner's own portal session (portal.fetch_many, the page's rows-per-page set to 15 so
       one look reads every row). If the portal asks for a verification code nobody can
       answer — it never did, in 26 measured sign-ins — you are told once (failsafe) and the
       check-in falls back to the codes in Moodle and schedule.json.
Use    mode_at(start): the mode of the class that day and time, whatever its course — a
       lecture is never two courses at once, so the day and the time are enough.
"""

import json
import logging
import time
from datetime import datetime, timedelta

from uninotifier import failsafe, fileio, paths, schedule

log = logging.getLogger("uninotifier")

CACHE_FILE = paths.DATA / "class_table.json"
READ_EVERY = 7 * 86400              # the table is read again this often
RETRY_AFTER = 6 * 3600              # ... or this long after a failed look
STALE_AFTER = 120 * 86400           # a table this old is not trusted any more (a semester)
MATCH = timedelta(minutes=30)       # a session this close to a class's start is that class
TICK = 600                          # seconds between the bot thread's looks


def slots_from_rows(rows: list[dict]) -> list[dict]:
    """The portal's rows (portal.timetable_rows) as one slot per day:
    {"day", "start", "end", "mode", "code", "course_ar", "section", "activity", "room"}."""
    slots = []
    for row in rows:
        for slot in schedule.parse_codes(row.get("time", "")):
            slots.append({**slot, "code": row.get("code", ""), "course_ar": row.get("name", ""),
                          "section": row.get("section", ""), "activity": row.get("activity", ""),
                          "room": row.get("room", "")})
    return slots


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
        if slot.get("day") != day:
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
    cache = load()
    return now - cache.get("read", 0) >= READ_EVERY and now - cache.get("tried", 0) >= RETRY_AFTER


def _no_code(attempt: int, rejected: bool) -> None:
    return None                       # nobody can type a code for a background look


def read() -> list[dict]:
    """The slots on the portal's home page now. Raises portal.PortalError (something to read)
    or maintenance.PortalUnavailable (try later)."""
    from uninotifier import portal
    result = portal.fetch_many(["home"], _no_code)["home"]
    return slots_from_rows(result.data.get("timetable") or [])


def refresh(now: float | None = None) -> bool:
    """Read the portal and keep the table. True when a table was saved."""
    from uninotifier import maintenance, portal
    now = time.time() if now is None else now
    cache = load()
    cache["tried"] = now
    try:
        slots = read()
    except maintenance.PortalUnavailable as e:
        log.info("Timetable: the portal is not available (%s) — trying again later", e)
    except portal.PortalError as e:
        log.warning("Timetable: %s", e)
        failsafe.report("classtable:portal", "The bot could not read your timetable from the portal",
                        detail=f"{e} Automatic check-in goes on with the codes in Moodle and schedule.json; "
                               f"the bot tries again in {RETRY_AFTER // 3600} hours.", wait=False)
    else:
        if slots:
            cache.update(read=now, slots=slots)
            log.info("Timetable: read %d class day(s) from the portal", len(slots))
            fileio.write_json_atomic(CACHE_FILE, cache)
            return True
        log.warning("Timetable: the portal's home page showed no timetable")
    fileio.write_json_atomic(CACHE_FILE, cache)
    return False


def tick(lock=None, now: float | None = None) -> bool:
    """One look; True when it read the portal successfully."""
    from uninotifier import portal
    now = time.time() if now is None else now
    if not wanted() or not due(now) or portal.credentials() is None:
        return False
    if lock is not None and not lock.acquire(blocking=False):
        return False                  # a command of yours is using the portal: the next look
    try:
        return refresh(now)
    finally:
        if lock is not None:
            lock.release()


def loop(lock=None) -> None:
    """The bot's timetable thread."""
    while True:
        try:
            tick(lock)
        except Exception as e:
            log.exception("Reading the timetable failed")
            failsafe.bug("bot:classtable", "Reading your timetable from the portal is failing", e,
                         "Automatic check-in goes on with the codes in Moodle and schedule.json.")
        time.sleep(TICK)


if __name__ == "__main__":         # python -m uninotifier.classtable — read it now
    print("read" if refresh() else "not read (see the log)")
