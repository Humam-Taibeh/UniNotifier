"""
Automatic attendance check-in for the online lectures, with proof either way.

Which   every attendance activity (mod_attendance) of an online course — while
        "auto_check_in" is true in schedule.json (off by default). Moodle itself says
        which courses are online: ZUJ ends a course's name with its teaching mode
        ("…-ش12-3-ك": ك online, و or F in person, nothing blended), and an online
        course has a Teams meeting. A course with an `Online` slot in schedule.json
        counts too.
When    each of its sessions, from EARLY before the start to LATE after the end. The
        sessions come from the attendance page itself (read again every SESSIONS_EVERY),
        so a moved or an extra session is followed too.
How     during a session the page is read every POLL_SECONDS. As soon as your instructor
        opens check-in (a "Submit attendance" link in your row), the form behind it is
        filled in as you would: Present, then Save changes (activities.check_in). Then
        the page is read again: only Moodle showing you as taken counts.

What you get, once per session:
  ✅ Checked In             Moodle shows you Present — a screenshot of your row.
  ⚠️ Recorded as <status>   taken, but not as Present (Late, Absent, …) — a screenshot.
  🚨 Check In by Hand       it could not be done for you — a password or QR code, no
                            "Present" choice, Moodle refused or did not record it, or
                            could not be read for TROUBLE_AFTER: why, the page it saw,
                            and a button to the attendance page. It goes on watching, so
                            ✅ follows once you have checked in yourself.
  ℹ️ No Check-in Opened     a timetabled online lecture ended without check-in ever
                            opening (sent silently).

A saved session that has expired starts a check, which signs in again. The bot runs this
on its own thread; a monitor run is the fallback while the bot is down; claim files
(daily.py) make each message go out once and keep two processes from submitting at the
same moment. The check-in page of every attempt is kept, its session key removed, in
data/checkin_pages/ for a later look. Never raises from tick().
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta

import requests

from uninotifier import activities, archive, daily, failsafe, fileio, media, notifier, paths, probe, schedule
from uninotifier.httpctx import HttpContext
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")

SESSION_FILE = paths.DATA / "auth_state.json"
STATE_FILE = paths.DATA / "saved_items.json"
CACHE_FILE = paths.DATA / "checkin.json"        # the watched activities and their sessions
PAGES_DIR = paths.DATA / "checkin_pages"

EARLY = timedelta(minutes=10)         # watching starts this long before a session
LATE = timedelta(minutes=30)          # ... and ends this long after it
WRAP_UP = timedelta(hours=2)          # after that, the last look at the session (its outcome)
LENGTH = timedelta(minutes=90)        # a session shown without an end time
POLL_SECONDS = 60                     # between two reads of the page during a session
SESSIONS_EVERY = 6 * 3600             # the sessions are read again this often
RETRY_SECONDS = 15 * 60               # ... or this soon after a failed read
TROUBLE_AFTER = 10 * 60               # Moodle unreadable this long during a session: check in by hand
SIGN_IN_EVERY = 15 * 60               # at most one sign-in started per this long
SLOT_MATCH = timedelta(minutes=30)    # a session this close to an Online slot is that lecture
KEEP_PAGES = 30
TICK = 20                             # seconds between the bot thread's looks
VIEWPORT, SCALE = {"width": 1100, "height": 900}, 2

MANUAL_BUTTON = "✍️ Check In by Hand"
OPEN_BUTTON = "📝 Open Attendance"
LECTURE_NOTE = ("🤖 Auto check-in is on: I'll check you in as soon as your instructor opens it, "
                "and send you the proof.")
SESSKEY_RE = re.compile(r'sesskey(?:=|"\s*:\s*"|"\s+value=")([A-Za-z0-9]{6,})')

# Keep only your session's row (highlighted) in the table, hide what floats over the page,
# and return the box of the main region to clip the screenshot to.
_SHOT_JS = r"""(want) => {
    const norm = s => (s || '').replace(/\s+/g, ' ').trim();
    const table = document.querySelector('table.generaltable');
    if (want && table) {
        for (const row of table.querySelectorAll('tbody tr')) {
            const cell = row.querySelector('td.datecol');
            if (cell && norm(cell.innerText) === want) {
                row.querySelectorAll('td').forEach(td => { td.style.background = '#fff3b0'; td.style.fontWeight = '600'; });
            } else {
                row.style.display = 'none';
            }
        }
    }
    const main = document.querySelector('[role="main"]') || document.body;
    document.querySelectorAll('body *').forEach(e => {
        const pos = getComputedStyle(e).position;
        if ((pos === 'fixed' || pos === 'sticky') && !e.contains(main)) e.style.visibility = 'hidden';
    });
    const r = main.getBoundingClientRect();
    return {x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height};
}"""


class Trouble(Exception):
    """The attendance page could not be read (the message says why, for you)."""


class Watch:
    """What one process remembers between ticks."""

    def __init__(self):
        self.last_read: dict[str, float] = {}       # session key -> when its page was last read
        self.trouble: dict[str, float] = {}         # session key -> since when it cannot be read
        self.last_refresh = float("-inf")           # the last attempt to read the sessions
        self.last_sign_in = float("-inf")


# ----------------------------------------------------------------- what is watched

ONLINE_MARK = "-ك"         # the end of an online course's Moodle name (ك = إلكتروني)
TEAMS_TYPES = {"teamscheduler"}


def online_courses(sched: dict, content: dict) -> set[str]:
    """The online courses, by their English name (see the module's docstring)."""
    found = {lec.get("course") for lectures in (sched.get("days") or {}).values()
             for lec in lectures if lec.get("mode") == "Online"}
    for item in content.values():
        full = (item.get("course") or "").strip()
        if full.endswith(ONLINE_MARK) or item.get("type") in TEAMS_TYPES:
            found.add(media.clean_course_name(full))
    return found


def enabled(sched: dict) -> bool:
    return sched.get("auto_check_in") is True


def covers(course: str, sched: dict | None = None, content: dict | None = None) -> bool:
    """Whether this course's attendance is checked in automatically."""
    sched = schedule.load_schedule() if sched is None else sched
    if not enabled(sched):
        return False
    return course in online_courses(sched, _load_content() if content is None else content)


def watched(sched: dict, content: dict) -> list[dict]:
    """The attendance activities of the online courses: {"cmid", "course", "name", "url"}."""
    courses = online_courses(sched, content)
    return [{"cmid": str(it.get("cmid") or key), "course": media.clean_course_name(it.get("course", "")),
             "name": it.get("name", ""), "url": it["url"]}
            for key, it in sorted(content.items())
            if it.get("type") in media.ATTENDANCE_TYPES and it.get("url")
            and media.clean_course_name(it.get("course", "")) in courses]


def page_url(activity: dict) -> str:
    """The attendance page with every session listed (Moodle remembers the last view)."""
    return activity["url"] + ("&" if "?" in activity["url"] else "?") + "view=5"


def session_key(activity: dict, start: datetime) -> str:
    return f"checkin-{activity['cmid']}-{start:%Y%m%d-%H%M}"


def _http() -> HttpContext:
    http = HttpContext.from_state_file(SESSION_FILE)
    if http is None:
        raise Trouble("there is no saved Moodle session yet")
    return http


def read_sessions(http, activity: dict) -> list[dict]:
    try:
        resp = http.get(page_url(activity))
    except requests.RequestException:
        raise Trouble("Moodle could not be reached") from None
    if "/login/" in resp.url:
        raise Trouble("my saved Moodle session has expired")
    if not resp.ok:
        raise Trouble(f"Moodle answered HTTP {resp.status}")
    return activities.parse_sessions(resp.text())


def find_row(sessions: list[dict], start: datetime) -> dict:
    found = next((s for s in sessions if s["start"] == start), None)
    if found is None:
        raise Trouble("this session is no longer on the attendance page")
    return found


# ------------------------------------------------------------------ the cache

def _load_cache() -> dict:
    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_content() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("content") or {}
    except (OSError, ValueError):
        return {}


def refresh(watch: Watch, sched: dict, now: float, force: bool = False) -> dict:
    """The cache, read again when it is old or the online courses changed. An
    activity whose page cannot be read keeps the sessions it had."""
    cache = _load_cache()
    content = _load_content()
    courses = sorted(online_courses(sched, content))
    stale = (cache.get("courses") != courses or now - cache.get("read", 0) >= SESSIONS_EVERY)
    if not (force or stale) or now - watch.last_refresh < RETRY_SECONDS:
        return cache
    watch.last_refresh = now
    old = {a["cmid"]: a for a in cache.get("activities", [])}
    fresh, failed = [], 0
    http = None
    for activity in watched(sched, content):
        try:
            http = http or _http()
            sessions = [{"start": s["start"].isoformat(), "end": s["end"].isoformat() if s["end"] else None}
                        for s in read_sessions(http, activity) if s["start"]]
        except Trouble as e:
            log.info("Check-in: the sessions of %s could not be read (%s)", activity["name"], e)
            sessions, failed = old.get(activity["cmid"], {}).get("sessions", []), failed + 1
        fresh.append({**activity, "sessions": sessions})
    cache = {"read": now if not failed else cache.get("read", 0), "courses": courses, "activities": fresh}
    fileio.write_json_atomic(CACHE_FILE, cache)
    return cache


# ------------------------------------------------------------------ the messages

def _session_line(row: dict) -> str:
    return f"🗓️ *Session:* {escape_md(row['date'])} · {escape_md(row['time'])}"


def _course_line(activity: dict) -> str:
    return f"📘 *Course:* {escape_md(activity['course'])}"


def screenshot(url: str, want: str | None = None) -> bytes | None:
    """The page in a headless browser with the saved session, clipped to its main
    region; `want` keeps only that session's row ("Wed 4 Nov 2026 7PM - 8:30PM"),
    highlighted. None when it could not be taken (the message goes without it)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
                    viewport=VIEWPORT, device_scale_factor=SCALE)
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                if "/login/" in page.url:
                    return None
                box = page.evaluate(_SHOT_JS, want)
                if box["width"] < 10 or box["height"] < 10:
                    return page.screenshot(type="png")
                return page.screenshot(type="png", full_page=True, clip=box)
            finally:
                browser.close()
    except Exception as e:                       # a screenshot is a bonus: never the reason a message is lost
        log.warning("Check-in: no screenshot (%s)", notifier.redact(str(e)).splitlines()[0] if str(e) else type(e).__name__)
        return None


def _send(token: str, chat_id: str, text: str, rows: list, png: bytes | None, activity: dict,
          silent: bool = False) -> bool:
    if png and len(text) <= notifier.CAPTION_LIMIT and notifier.send_png_file(
            token, chat_id, png, f"{archive.safe_name(activity['course'], 'attendance')} - attendance.png",
            text, rows=rows):
        return True
    return notifier.send_message(token, chat_id, text, rows=rows, silent=silent)


def recorded_message(activity: dict, row: dict, by_me: bool) -> str:
    present = bool(activities.PRESENT_RE.match(row["status"]))
    status = row["status"] + (f" · {row['remarks']}" if row.get("remarks") else "")
    lines = ["✅ *Checked In — Attendance Recorded*" if present
             else f"⚠️ *Attendance Recorded as {escape_md(row['status'])}*", "",
             _course_line(activity), _session_line(row), f"📝 *Status:* {escape_md(status)}", ""]
    if not present:
        lines.append("It is not *Present* — talk to your instructor if that is wrong.")
    elif by_me:
        at = datetime.now()
        lines.append(f"🤖 I checked you in at {notifier.clock12(at.hour, at.minute)}.")
    else:
        lines.append("Moodle shows you as checked in.")
    return "\n".join(lines)


def failed_message(activity: dict, row: dict | None, reason: str, png: bool) -> str:
    lines = ["🚨 *Check In by Hand — Auto Check-in Failed*", "", _course_line(activity)]
    if row:
        lines.append(_session_line(row))
    lines += [f"❌ *Why:* {escape_md(reason[:1].upper() + reason[1:])}.", "",
              f"⚠️ *Do it now:* tap *{MANUAL_BUTTON}* below and choose *Present* before check-in "
              "closes. I'll keep watching and confirm once Moodle shows you checked in."]
    if png:
        lines.append("📸 The page I saw is attached.")
    return "\n".join(lines)


def no_check_in_message(activity: dict, row: dict) -> str:
    return "\n".join(["ℹ️ *No Check-in Opened*", "", _course_line(activity), _session_line(row), "",
                      "Check-in never opened during this lecture, so there was nothing to submit. "
                      "If your instructor opens it later, check in yourself."])


def report_recorded(token: str, chat_id: str, activity: dict, row: dict, key: str, by_me: bool = False) -> bool:
    if not daily.claim(f"{key}-done"):
        return False
    text = recorded_message(activity, row, by_me)
    png = screenshot(page_url(activity), f"{row['date']} {row['time']}")
    if png:
        text += "\n📸 Your row on Moodle's attendance page is attached."
    if _send(token, chat_id, text, [[{"text": OPEN_BUTTON, "url": activity["url"]}]], png, activity):
        log.info("Check-in: %s is recorded (%s) — sent", key, row["status"])
        return True
    daily.release(f"{key}-done")              # the next look sends it
    return False


def report_failure(token: str, chat_id: str, activity: dict, row: dict | None, key: str, reason: str,
                   shot_url: str | None = None) -> bool:
    if not daily.claim(f"{key}-alerted"):
        return False
    want = f"{row['date']} {row['time']}" if row and not shot_url else None
    png = screenshot(shot_url or page_url(activity), want) if row else None
    text = failed_message(activity, row, reason, bool(png))
    if _send(token, chat_id, text, [[{"text": MANUAL_BUTTON, "url": activity["url"]}]], png, activity):
        log.warning("Check-in: %s failed (%s) — told you to check in by hand", key, reason)
        return True
    daily.release(f"{key}-alerted")
    return False


# ------------------------------------------------------------------ one session

def _keep_page(key: str, page: str | None) -> None:
    if not page:
        return
    for value in set(SESSKEY_RE.findall(page)):
        page = page.replace(value, "SESSKEY")
    PAGES_DIR.mkdir(exist_ok=True)
    (PAGES_DIR / f"{key}-{int(time.time())}.html").write_text(page, encoding="utf-8")
    for old in sorted(PAGES_DIR.glob("*.html"))[:-KEEP_PAGES]:
        old.unlink(missing_ok=True)


def _sign_in(watch: Watch, now: float) -> None:
    """An expired session: start a check, which signs in again (and saves the session)."""
    if now - watch.last_sign_in < SIGN_IN_EVERY or probe.MONITOR_LOCK.exists():
        return
    watch.last_sign_in = now
    probe.launch_check()
    log.info("Check-in: the saved session has expired — started a check to sign in again")


def _trouble(token: str, chat_id: str, watch: Watch, activity: dict, key: str, row: dict | None,
             reason: str, now: float) -> None:
    since = watch.trouble.setdefault(key, now)
    log.info("Check-in: %s — %s", key, reason)
    if "expired" in reason:
        _sign_in(watch, now)
    if now - since >= TROUBLE_AFTER:
        report_failure(token, chat_id, activity, row, key, reason)


def attempt(token: str, chat_id: str, watch: Watch, http, activity: dict, row: dict, key: str,
            now: float) -> None:
    """Check-in is open: submit it once, then read the page to see what Moodle recorded."""
    if not daily.claim(f"{key}-try"):
        return                                  # the other process is on it right now
    try:
        outcome = activities.check_in(http, row, dry_run=False)
    except requests.RequestException:
        outcome = {"ok": False, "reason": "Moodle could not be reached", "retry": True, "page": None}
    finally:
        daily.release(f"{key}-try")
    _keep_page(key, outcome["page"])
    log.info("Check-in: %s — %s", key, outcome["reason"])
    try:
        after = find_row(read_sessions(http, activity), row["start"])
    except Trouble:
        after = None
    if after and activities.recorded(after):
        watch.trouble.pop(key, None)
        report_recorded(token, chat_id, activity, after, key, by_me=True)
        return
    if outcome["retry"]:
        _trouble(token, chat_id, watch, activity, key, row, outcome["reason"], now)
        return
    daily.claim(f"{key}-gave-up")             # never sent again: the next looks only watch for your own check-in
    reason = (outcome["reason"] if not outcome["ok"]
              else "I sent the form, but Moodle still does not show you as checked in")
    form_problem = not outcome["ok"] and outcome["page"] is not None
    report_failure(token, chat_id, activity, after or row, key, reason,
                   shot_url=row["submit_url"] if form_problem else None)


def during(token: str, chat_id: str, watch: Watch, activity: dict, start: datetime, key: str,
           now: float) -> None:
    """One look while the session runs (every POLL_SECONDS)."""
    if now - watch.last_read.get(key, float("-inf")) < POLL_SECONDS:
        return
    watch.last_read[key] = now
    try:
        http = _http()
        row = find_row(read_sessions(http, activity), start)
    except Trouble as e:
        _trouble(token, chat_id, watch, activity, key, None, str(e), now)
        return
    watch.trouble.pop(key, None)
    if activities.recorded(row):
        report_recorded(token, chat_id, activity, row, key)
    elif row["submit_url"]:
        daily.claim(f"{key}-opened")
        if not daily.claimed(f"{key}-gave-up"):
            attempt(token, chat_id, watch, http, activity, row, key, now)


def wrap_up(token: str, chat_id: str, watch: Watch, activity: dict, start: datetime, key: str,
            sched: dict, now: float) -> None:
    """The session is over: what it ended as, once."""
    if now - watch.last_read.get(key, float("-inf")) < POLL_SECONDS:
        return
    watch.last_read[key] = now
    try:
        row = find_row(read_sessions(_http(), activity), start)
    except Trouble:
        return                                  # tried again on the next look, until WRAP_UP ends
    if activities.recorded(row):
        report_recorded(token, chat_id, activity, row, key)
        return
    if daily.claimed(f"{key}-opened"):
        if not daily.claimed(f"{key}-alerted"):
            report_failure(token, chat_id, activity, row, key, "check-in closed before I could finish it")
        daily.claim(f"{key}-done")
    elif timetabled(activity, start, sched) and daily.claim(f"{key}-done"):
        if not _send(token, chat_id, no_check_in_message(activity, row),
                     [[{"text": OPEN_BUTTON, "url": activity["url"]}]], None, activity, silent=True):
            daily.release(f"{key}-done")
    else:
        daily.claim(f"{key}-done")              # an activity day: nothing to say


def timetabled(activity: dict, start: datetime, sched: dict) -> bool:
    """The session is one of the course's Online lectures in schedule.json — or any
    session, for an online course the timetable does not list at all."""
    days = sched.get("days") or {}
    if not any(lec.get("course") == activity["course"] for lectures in days.values() for lec in lectures):
        return True
    day = start.strftime("%a").lower()
    for lec in days.get(day, []):
        if lec.get("mode") == "Online" and lec.get("course") == activity["course"]:
            at = datetime.combine(start.date(), datetime.strptime(lec["start"], "%H:%M").time())
            if abs(at - start) <= SLOT_MATCH:
                return True
    return False


# ------------------------------------------------------------------ the clock

def tick(token: str, chat_id: str, watch: Watch, now: datetime | None = None) -> None:
    now = now or datetime.now()
    sched = schedule.load_schedule()
    if not enabled(sched):
        return
    cache = refresh(watch, sched, now.timestamp())
    for activity in cache.get("activities", []):
        for s in activity.get("sessions", []):
            try:
                start = datetime.fromisoformat(s["start"])
                end = datetime.fromisoformat(s["end"]) if s.get("end") else start + LENGTH
            except (KeyError, TypeError, ValueError):
                continue
            if not start - EARLY <= now <= end + LATE + WRAP_UP:
                continue
            key = session_key(activity, start)
            if daily.claimed(f"{key}-done"):
                continue
            if now <= end + LATE:
                during(token, chat_id, watch, activity, start, key, now.timestamp())
            else:
                wrap_up(token, chat_id, watch, activity, start, key, sched, now.timestamp())


def loop(token: str, chat_id: str) -> None:
    """The bot's check-in thread."""
    watch = Watch()
    while True:
        try:
            tick(token, chat_id, watch)
        except Exception as e:
            log.exception("Automatic check-in failed")
            failsafe.bug("bot:checkin", "Automatic attendance check-in is failing", e,
                         f"Check in yourself until it is fixed: the {media.CHECK_IN_BUTTON} button in the "
                         "lecture alert.")
        time.sleep(TICK)
