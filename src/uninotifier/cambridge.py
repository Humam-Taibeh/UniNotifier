"""
Cambridge One — the English course's assignments (temporary: for this semester only).

A stand-alone module: nothing else in UniNotifier depends on it. To remove it when the
semester ends, delete this file, its lines in bot/app.py and tests/, the CAMBRIDGE_* keys in
.env and data/cambridge*. It also switches itself off after CAMBRIDGE_UNTIL (YYYY-MM-DD).

  .env   CAMBRIDGE_ASSIGNMENTS_URL   the class's assignments page (it carries your class ids)
         CAMBRIDGE_UNTIL             the last day it runs
         CAMBRIDGE_EMAIL / _PASSWORD optional: the bot signs in again by itself when the session ends
  data/  cambridge_state.json        the browser session (cookies + local storage)
         cambridge.json              the assignments already seen, reminders already sent
         cambridge_feed.json         the latest feed as Cambridge sent it (for checking the parser)
         cambridge_recon/            what --login recorded: the page's JSON answers + a screenshot

How it looks (measured 2026-10-20): the assignments page is built from ONE JSON answer,
GET /class/apigateway/<org>/class/<class>/learner/assignments, which plain HTTP gets in ~0.3 s
with the saved session's cookies and the header cambridgeone-app-version: v2 (403 without it).
So every CHECK_SECONDS the bot thread sends that one request — no browser. Chromium starts
only for the 2x screenshot that goes with an alert, and to renew the session when Cambridge
refuses it.

A session does NOT last: Cambridge ends it about 72 hours after the sign-in (seen 2026-10-20 →
10-23 and 10-25 → 10-28, both ≈ 72 h to the minute), and Gigya's login token, which could have
renewed it silently, is good for 6 hours only. So when the feed answers 401/403 the hidden
browser signs in with CAMBRIDGE_EMAIL / CAMBRIDGE_PASSWORD (Gigya's plain form on /login, no
captcha seen). Without them, or when Cambridge refuses them or asks for a check only a person
can pass, the owner is told once and waits for --login. Refused credentials are not tried again
for a day (a lockout is worse than waiting).

  new assignment        🚨 alert: course, class, title, opens, due date and its activities, the
                        page as a 2x PNG document, 🔗 Open in Cambridge One. Not marked as seen
                        until it reached Telegram (so a failed send is tried again).
  due date moved        an ordinary alert.
  gone                  an ordinary alert, after it is missing from two answers in a row.
  24 h before the due   🚨 reminder — while any activity is not done (done = submittedOnce and
                        status "completed"). Once per due date.
The first run only learns what is there.

An assignment as Cambridge sends it (seen 2026-10-21, tests/fixtures/cambridge_assignment.json):
assigned_path_id (its id), title ("Assignment 1, <class title>"), startdate / duedate (ms), and
items[] — the activities: name, __analytics.status ("not_started", …), __analytics.submittedOnce.
Unknown shapes still parse (usual field names, else a fingerprint that ignores the progress
fields "__…", so starting an activity never makes an assignment look new).

  python -m uninotifier.cambridge --login   a visible browser: sign in yourself, the session
                                            is saved (and the page's answers recorded)
  python -m uninotifier.cambridge --check   one check now, printed (sends what is due)
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

from uninotifier import failsafe, fileio, notifier, paths
from uninotifier.media import DIVIDER
from uninotifier.notifier import escape_md, format_timestamp

load_dotenv(paths.ENV_FILE)
log = logging.getLogger("uninotifier")

ENV_URL = "CAMBRIDGE_ASSIGNMENTS_URL"
ENV_UNTIL = "CAMBRIDGE_UNTIL"
ENV_EMAIL = "CAMBRIDGE_EMAIL"
ENV_PASSWORD = "CAMBRIDGE_PASSWORD"
STATE_FILE = paths.DATA / "cambridge_state.json"
SEEN_FILE = paths.DATA / "cambridge.json"
FEED_FILE = paths.DATA / "cambridge_feed.json"
RECON_DIR = paths.DATA / "cambridge_recon"

COURSE = "English Communication Skills"
BUTTON_TEXT = "🔗 Open in Cambridge One ↗️"
CHECK_SECONDS = 30 * 60            # between two checks
RETRY_SECONDS = 5 * 60             # after a network error
FAILURES_BEFORE_ALERT = 12         # failed reads in a row (5 min apart) before the owner is told: an hour
RELOGIN_RETRY = 6 * 3600          # a session that could not be renewed is tried again after this (or after --login)
CREDENTIALS_RETRY = 24 * 3600     # ... but after Cambridge refused the e-mail / password: once a day, never in a loop
REMIND_BEFORE = 24 * 3600
GONE_AFTER = 2                     # answers in a row without an assignment before it counts as gone
HTTP_TIMEOUT = (6, 20)
BROWSER_TIMEOUT = 45_000           # ms: the page and its feed, in the hidden browser
FIRST_LOOK_TIMEOUT = 15_000        # ms: the feed is already known to be refused, so a sign-in page shows up fast
FORM_TIMEOUT = 15_000              # ms: the sign-in form to appear and take the typed values
SIGN_IN_TIMEOUT = 45_000           # ms: from pressing Enter to leaving the sign-in page
APP_VERSION = "v2"                 # the cambridgeone-app-version header the feed requires
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")

VIEWPORT = {"width": 1280, "height": 900}
SCALE = 2                          # screenshots at 2x, like every other page the bot sends
LOGIN_MINUTES = 15                 # --login gives up after this long
MAX_BODY_BYTES = 2 * 1024 * 1024   # a recorded answer larger than this is listed, not saved
STATE_SAVE_SECONDS = 10            # while signed in, the session is saved this often
FEED_PATH = "/learner/assignments"   # the JSON the assignments page is built from (cookies only, no bearer)
FEED_PLAIN_HEADERS = ("user-id", "user-role", "cambridgeone-app-version")   # not secrets: recorded with values

# where an assignment's fields may be (Cambridge's own names first): first match wins
ID_KEYS = ("assigned_path_id", "uuid", "assignment_id", "assignmentId", "assignment_uuid", "id", "_id", "guid", "key")
TITLE_KEYS = ("title", "name", "assignment_name", "assignmentName", "assignment_title", "label")
DUE_KEYS = ("due_date", "dueDate", "duedate", "due", "due_on", "dueOn", "end_date", "endDate", "enddate",
            "deadline", "close_date", "closeDate")
START_KEYS = ("start_date", "startDate", "startdate", "assigned_date", "assignedDate", "open_date", "openDate")
STATUS_KEYS = ("submitted", "is_submitted", "isSubmitted", "submission_status", "submissionStatus",
               "status", "state", "completion_status", "completionStatus", "completed", "is_completed")
NESTED_KEYS = ("assignment", "details", "meta", "activity", "data", "attributes")
DONE_WORDS = {"submitted", "completed", "complete", "done", "graded", "finished", "turned_in", "turnedin"}
OPEN_WORDS = {"not_started", "notstarted", "in_progress", "inprogress", "pending", "assigned", "open",
              "started", "incomplete", "not_submitted", "notsubmitted", "todo", "new", "overdue"}
ITEM_DONE_STATUS = "completed"     # an activity is done when submittedOnce and this status (the owner's rule)


class Expired(Exception):
    """Cambridge refused the session (401/403)."""


class NeedsLogin(Exception):
    """The session could not be renewed without the owner (--login)."""


class CredentialsRejected(NeedsLogin):
    """Cambridge One answered the saved e-mail and password with an error."""


# ── settings ─────────────────────────────────────────────────────────────────────────────────


def assignments_url() -> str | None:
    return (os.getenv(ENV_URL) or "").strip() or None


def until() -> date | None:
    raw = (os.getenv(ENV_UNTIL) or "").strip()
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def credentials() -> tuple[str, str] | None:
    """(e-mail or user name, password) from .env; None unless both are set."""
    email, password = (os.getenv(ENV_EMAIL) or "").strip(), os.getenv(ENV_PASSWORD) or ""
    return (email, password) if email and password else None


def active(today: date | None = None) -> bool:
    """On while the page is set and CAMBRIDGE_UNTIL (required: it is a temporary watch) is not past."""
    end = until()
    return bool(assignments_url()) and end is not None and (today or date.today()) <= end


def feed_url(page_url: str) -> str:
    """…/class/learner/<org>/class/<class>/assignments → …/class/apigateway/<org>/class/<class>/learner/assignments"""
    parts = urlsplit(page_url)
    found = re.fullmatch(r"/class/learner/(.+)/assignments/?", parts.path)
    if not found:
        raise ValueError(f"{ENV_URL} is not a Cambridge One assignments page")
    return f"{parts.scheme}://{parts.netloc}/class/apigateway/{found.group(1)}{FEED_PATH}"


# ── reading the feed (plain HTTP) ────────────────────────────────────────────────────────────


def session_from_state() -> requests.Session:
    """A requests session carrying the saved browser session's cookies. NeedsLogin without one."""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise NeedsLogin("no saved Cambridge One session") from e
    http = requests.Session()
    for c in state.get("cookies", []):
        http.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
    http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
                         "cambridgeone-app-version": APP_VERSION})
    return http


def fetch(http: requests.Session, page_url: str) -> dict:
    """The feed. Expired on 401/403; requests errors / ValueError (not JSON) for anything else."""
    r = http.get(feed_url(page_url), params={"timestamp": int(time.time() * 1000)},
                 headers={"Referer": page_url}, timeout=HTTP_TIMEOUT)
    if r.status_code in (401, 403):
        raise Expired(f"HTTP {r.status_code}")
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or not isinstance(data.get("assignmentsData"), dict):
        raise ValueError("the Cambridge One feed has an unexpected shape")
    return data


# ── understanding it ─────────────────────────────────────────────────────────────────────────


def _find(entity: dict, keys: tuple[str, ...]):
    """The first of `keys` with a value, in the entry itself, then one level down."""
    for where in [entity, *[entity[k] for k in NESTED_KEYS if isinstance(entity.get(k), dict)]]:
        for key in keys:
            value = where.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def to_epoch(value) -> float | None:
    """Milliseconds, seconds or an ISO date → seconds since the epoch."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        return number / 1000 if number > 1e11 else number if number > 1e8 else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if isinstance(value, dict):                     # {"date": …} / {"$date": …}
        return to_epoch(_find(value, ("date", "$date", "value", "timestamp")))
    return None


def submitted_state(value) -> bool | None:
    """True / False when the feed says so; None when it doesn't (or says something unknown)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return submitted_state(_find(value, ("status", "state", "submitted", "value")))
    if isinstance(value, str):
        word = re.sub(r"[\s-]+", "_", value.strip().lower())
        if word in DONE_WORDS:
            return True
        if word in OPEN_WORDS:
            return False
    return None


def _without_progress(value):
    """The entry minus its progress fields ("__analytics" …), which change as activities are done."""
    if isinstance(value, dict):
        return {k: _without_progress(v) for k, v in value.items() if not str(k).startswith("__")}
    if isinstance(value, list):
        return [_without_progress(v) for v in value]
    return value


def fingerprint(entity) -> str:
    stable = _without_progress(entity)
    return "fp-" + hashlib.sha1(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def activities(entity: dict) -> list[dict]:
    """The assignment's activities: {name, done}. Done = submittedOnce and status "completed"."""
    found = []
    for item in entity.get("items") or []:
        if not isinstance(item, dict):
            continue
        progress = item.get("__analytics") if isinstance(item.get("__analytics"), dict) else {}
        found.append({"name": str(item.get("name") or "Untitled activity").strip(),
                      "done": progress.get("submittedOnce") is True and progress.get("status") == ITEM_DONE_STATUS})
    return found


def class_title(feed: dict) -> str:
    details = feed.get("classDetails") or {}
    return str(details.get("title") or "").strip()


def short_title(title: str, klass: str) -> str:
    """"Assignment 1, Ross. B2. sec. 27 . 3:30-5:00" → "Assignment 1" (the class is on its own line)."""
    if klass and title.endswith(klass):
        trimmed = title[:-len(klass)].rstrip(" ,-–—·|")
        return trimmed or title
    return title


def parse(feed: dict) -> list[dict]:
    """The assignments in the feed: {id, title, full_title, due, start, submitted, activities, raw}."""
    klass = class_title(feed)
    found = []
    for entity in (feed.get("assignmentsData") or {}).get("entities") or []:
        if not isinstance(entity, dict):
            continue
        ident = _find(entity, ID_KEYS)
        title = _find(entity, TITLE_KEYS)
        title = str(title).strip() if isinstance(title, (str, int)) else "Untitled assignment"
        acts = activities(entity)
        found.append({
            "id": str(ident) if isinstance(ident, (str, int)) else fingerprint(entity),
            "title": short_title(title, klass),
            "full_title": title,
            "due": to_epoch(_find(entity, DUE_KEYS)),
            "start": to_epoch(_find(entity, START_KEYS)),
            # with activities: submitted only when every one is done; without: whatever the entry says
            "submitted": all(a["done"] for a in acts) if acts else submitted_state(_find(entity, STATUS_KEYS)),
            "activities": acts,
            "raw": entity,
        })
    return found


# ── messages ─────────────────────────────────────────────────────────────────────────────────


def _lines(a: dict, klass: str) -> list[str]:
    lines = [f"📘 *Course:* {escape_md(COURSE)}"]
    if klass:
        lines.append(f"🏫 *Class:* {escape_md(klass)}")
    lines.append(f"📌 *Title:* {escape_md(a['title'])}")
    if a["start"]:
        lines.append(f"🗓️ *Opens:* {format_timestamp(a['start'])}")
    lines.append(f"🕐 *Due Date:* {format_timestamp(a['due'])}" if a["due"] else "🕐 *Due Date:* not given")
    return lines


def _activity_lines(a: dict) -> list[str]:
    acts = a.get("activities") or []
    if not acts:
        return []
    return ["", f"📋 *Activities ({len(acts)}):*", *[f"• {escape_md(act['name'])}" for act in acts]]


def _progress(a: dict) -> str:
    acts = a.get("activities") or []
    done = sum(1 for act in acts if act["done"])
    return f"{len(acts) - done} of {len(acts)} activities not done yet" if acts else "not submitted yet"


def _stamp(now: float) -> str:
    return f"🕒 *Detected:* {notifier.audit_timestamp(now)}"


def new_message(a: dict, klass: str, now: float) -> str:
    return "\n".join(["🚨 *New Assignment — Cambridge One*", *_lines(a, klass), *_activity_lines(a),
                      "", _stamp(now)])


def moved_message(a: dict, old_due: float | None, klass: str, now: float) -> str:
    before = format_timestamp(old_due) if old_due else "not given"
    return "\n".join(["🗓️ *Due Date Changed — Cambridge One*", *_lines(a, klass),
                      f"↩️ *Was:* {before}", "", _stamp(now)])


def gone_message(seen: dict, klass: str, now: float) -> str:
    lines = ["🗑️ *Assignment Removed — Cambridge One*", f"📘 *Course:* {escape_md(COURSE)}"]
    if klass:
        lines.append(f"🏫 *Class:* {escape_md(klass)}")
    lines += [f"📌 *Title:* {escape_md(seen.get('title') or 'Untitled assignment')}", "",
              "It is no longer in your assignments list.", _stamp(now)]
    return "\n".join(lines)


def reminder_message(a: dict, klass: str, now: float) -> str:
    hours = max(1, round((a["due"] - now) / 3600))
    return "\n".join(["🚨 *Reminder — Cambridge One*", *_lines(a, klass), "",
                      f"⏳ Due in {hours} hour{'s' if hours != 1 else ''} — {_progress(a)}.", "", _stamp(now)])


def _rows(page_url: str) -> list[list[dict]]:
    return [[{"text": BUTTON_TEXT, "url": page_url}]]


def _credentials() -> tuple[str, str]:
    return os.getenv("TELEGRAM_BOT_TOKEN") or "", str(os.getenv("TELEGRAM_CHAT_ID") or "")


def send(header: str, body: str, page_url: str, shot: bytes | None = None) -> bool:
    """Like the monitor's notifications: the screenshot as an uncompressed PNG document captioned
    with the whole message when it fits, otherwise followed by the full text. True when the text
    reached Telegram."""
    token, chat_id = _credentials()
    full = f"{header}\n{DIVIDER}\n{body}" if body else header
    rows = _rows(page_url)
    if shot:
        if len(full) <= notifier.CAPTION_LIMIT:
            if notifier.send_png_file(token, chat_id, shot, "Cambridge One - Assignments.png", full, None, rows):
                return True
        else:
            notifier.send_png_file(token, chat_id, shot, "Cambridge One - Assignments.png")   # best effort
    return notifier.send_long_message(token, chat_id, full, None, rows)


# ── the hidden browser: renewing the session, the screenshot ─────────────────────────────────

_DISMISS_JS = """() => {
  // Cambridge's announcement bar ("Due to a high volume of requests…") and the cookie bar:
  // their close buttons, then whatever bar is left at the very top
  for (const b of document.querySelectorAll('button, [role=button], a')) {
    const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.title || '') + ' ' + (b.innerText || '')).trim().toLowerCase();
    const top = b.getBoundingClientRect().top;
    if (top < 320 && /^(close|dismiss|×|✕|x)$|close (announcement|banner|notification)/.test(label)) b.click();
  }
  for (const e of document.querySelectorAll('[class*=cookie i], [id*=cookie i], [class*=announcement i], [id*=onetrust i]'))
    if (e.getBoundingClientRect().top < 320 && e.offsetHeight < 200) e.remove();
}"""


def _first_line(error: Exception) -> str:
    return notifier.redact(str(error)).splitlines()[0]


def _where(page) -> str:
    """Where the browser stands, without the query (class ids, tokens)."""
    parts = urlsplit(page.url)
    return notifier.redact(f"{parts.netloc}{parts.path}" if parts.netloc else page.url.split("?")[0])


def _on_web(page) -> bool:
    """False for the browser's own error page (chrome-error://…): the address never answered."""
    return urlsplit(page.url).scheme in ("http", "https")


def _reach_feed(page, page_url: str, timeout: int) -> bool:
    """Opens the assignments page; True when its feed answers 200, i.e. the session is valid."""
    from playwright.sync_api import Error

    try:
        with page.expect_response(lambda r: FEED_PATH in r.url and r.status == 200, timeout=timeout):
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout)
    except Error:
        return False
    return True


def _form_error(form) -> str:
    """The error text the sign-in form shows (Gigya marks it .gigya-error-msg-active); "" when none."""
    from playwright.sync_api import Error

    try:
        shown = [t.strip() for t in form.locator(".gigya-error-msg-active").all_inner_texts() if t.strip()]
    except Error:
        return ""
    return notifier.redact(" / ".join(shown))[:120]


def _sign_in(page, email: str, password: str) -> None:
    """Types the saved e-mail and password into Cambridge One's sign-in form and presses Enter.
    Returns once the browser has left the sign-in page. CredentialsRejected when the form answers
    with an error; NeedsLogin for anything else (no form, or a check only a person can pass)."""
    from playwright.sync_api import Error

    form = page.locator("#gigya-login-form")
    try:
        form.wait_for(state="visible", timeout=FORM_TIMEOUT)
        form.locator("input[name=username]").fill(email, timeout=FORM_TIMEOUT)
        box = form.locator("input[name=password]")
        box.fill(password, timeout=FORM_TIMEOUT)
        box.press("Enter", timeout=FORM_TIMEOUT)
    except Error as e:
        raise NeedsLogin(f"the sign-in form could not be filled in (the browser is on {_where(page)}): "
                         f"{_first_line(e)}") from e
    try:
        page.wait_for_url(lambda url: not urlsplit(url).path.startswith("/login"),
                          timeout=SIGN_IN_TIMEOUT, wait_until="commit")
    except Error:
        shown = _form_error(form)
        if shown:
            raise CredentialsRejected(f"Cambridge One refused the saved e-mail and password (it says: {shown})") from None
        raise NeedsLogin("the sign-in did not finish - Cambridge One may be asking for a check "
                         "only you can pass") from None


def visit(shot: bool) -> tuple[bool, bytes | None]:
    """Opens the assignments page headless with the saved session. (did the feed answer 200 —
    the session is valid, and saved again, renewed; the page as a 2x PNG when `shot`).
    A session Cambridge has ended is replaced by signing in with CAMBRIDGE_EMAIL / _PASSWORD when
    they are set (NeedsLogin / CredentialsRejected when that does not work), else (False, None).
    requests.ConnectionError when Cambridge One could not be reached at all."""
    from playwright.sync_api import Error, sync_playwright

    page_url = assignments_url()
    login = credentials()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE,
                                          storage_state=str(STATE_FILE) if STATE_FILE.exists() else None)
            page = context.new_page()
            if not _reach_feed(page, page_url, FIRST_LOOK_TIMEOUT if login else BROWSER_TIMEOUT):
                if not _on_web(page):
                    raise requests.ConnectionError(f"Cambridge One did not answer ({_where(page)})")
                if not login:
                    log.info("Cambridge One: sent to the sign-in page (%s), and %s / %s are not in .env",
                             _where(page), ENV_EMAIL, ENV_PASSWORD)
                    return False, None                # only --login helps now
                log.info("Cambridge One: the session has ended — signing in with the saved e-mail and password")
                _sign_in(page, *login)
                if not _reach_feed(page, page_url, BROWSER_TIMEOUT):
                    raise NeedsLogin(f"signed in, but the assignments page did not load (the browser is on "
                                     f"{_where(page)})")
                log.info("Cambridge One: signed in again")
            fileio.write_json_atomic(STATE_FILE, context.storage_state())
            if not shot:
                return True, None
            png = None
            try:
                page.wait_for_timeout(2500)           # the list renders after its feed
                accept = page.get_by_role("button", name=re.compile("accept cookies", re.I))
                if accept.count():
                    accept.first.click(timeout=3000)
                page.evaluate(_DISMISS_JS)
                page.wait_for_timeout(800)
                png = page.screenshot(full_page=True)
                fileio.write_json_atomic(STATE_FILE, context.storage_state())   # the dismissals are remembered
            except Error as e:
                log.warning("Cambridge One: no screenshot (%s)", notifier.redact(str(e)).splitlines()[0])
            return True, png
        finally:
            browser.close()


def screenshot() -> bytes | None:
    """The page for an alert; None when it could not be taken (the alert goes without it)."""
    from playwright.sync_api import Error

    try:
        return visit(shot=True)[1]
    except (Error, requests.RequestException, NeedsLogin) as e:
        log.warning("Cambridge One: no screenshot (%s)", _first_line(e))
        return None


# ── what has been seen ───────────────────────────────────────────────────────────────────────


def load_seen() -> dict | None:
    """None when there is none yet (the first run is a baseline). An unreadable file is set aside,
    reported, and the run starts a new baseline."""
    try:
        text = SEEN_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("seen"), dict):
            data.setdefault("reminded", {})
            return data
        raise ValueError("not a Cambridge One state")
    except ValueError as e:
        SEEN_FILE.replace(SEEN_FILE.with_name(f"cambridge.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json"))
        failsafe.report("cambridge:state", "The Cambridge One record was unreadable", exc=e,
                        detail="It was set aside and a new one started: an assignment posted just now may "
                               "not have been announced — have a look at Cambridge One.")
        return None


def _record(a: dict, now: float) -> dict:
    return {"title": a["title"], "due": a["due"], "submitted": a["submitted"], "first_seen": now, "missing": 0}


def _keep_feed(feed: dict, found: list[dict]) -> None:
    """The latest feed, kept when it changes (to check the parser against the real format)."""
    data = {k: v for k, v in (feed.get("assignmentsData") or {}).items() if k != "server"}   # its request id changes every time
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1)
    try:
        if FEED_FILE.exists() and FEED_FILE.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    fileio.write_text_atomic(FEED_FILE, text)
    if found and any(a["id"].startswith("fp-") or a["due"] is None for a in found):
        log.info("Cambridge One: an assignment field was not recognised — see %s", FEED_FILE.name)


def adopt_legacy(seen: dict, reminded: dict, found: list[dict]) -> None:
    """An assignment first recorded under a fingerprint (before its real id was known) keeps its
    record under the real id — the same assignment, announced once."""
    for a in found:
        if a["id"] in seen:
            continue
        legacy = next((k for k, s in seen.items() if k.startswith("fp-") and k != a["id"]
                       and s.get("due") == a["due"] and s.get("title") in (a["title"], a["full_title"])), None)
        if legacy:
            seen[a["id"]] = seen.pop(legacy)
            if legacy in reminded:
                reminded[a["id"]] = reminded.pop(legacy)
            log.info("Cambridge One: %s is now recorded under its id", a["title"])


def apply(feed: dict, now: float, page_url: str, shoot=None) -> dict:
    """Compares one feed with what was seen and sends what is due. Returns counts (for --check)."""
    found = parse(feed)
    klass = class_title(feed)
    _keep_feed(feed, found)
    state = load_seen()
    counts = {"assignments": len(found), "new": 0, "moved": 0, "gone": 0, "reminded": 0, "baseline": state is None}
    if state is None:
        state = {"seen": {a["id"]: _record(a, now) for a in found}, "reminded": {}}
        fileio.write_json_atomic(SEEN_FILE, state)
        log.info("Cambridge One: watching %d assignment(s)", len(found))
        return counts
    seen, reminded = state["seen"], state["reminded"]
    adopt_legacy(seen, reminded, found)
    current = {a["id"]: a for a in found}
    fresh = [a for a in found if a["id"] not in seen]
    fresh_ids = {a["id"] for a in fresh}
    shot = (shoot or screenshot)() if fresh else None     # one screenshot shows the whole list
    for a in fresh:
        if send(new_message(a, klass, now), "", page_url, shot):
            seen[a["id"]] = _record(a, now)
            counts["new"] += 1
            if a["due"] and a["submitted"] is False and 0 < a["due"] - now <= REMIND_BEFORE:
                reminded[a["id"]] = a["due"]           # the alert itself says when it is due
    for ident, a in current.items():
        old = seen.get(ident)
        if old is None or ident in fresh_ids:
            continue
        old["missing"] = 0
        if a["due"] != old.get("due") and a["due"] and old.get("due"):
            if not send(moved_message(a, old["due"], klass, now), "", page_url):
                continue
            counts["moved"] += 1
        old.update(title=a["title"], due=a["due"], submitted=a["submitted"])
    for ident in [i for i in seen if i not in current]:
        seen[ident]["missing"] = seen[ident].get("missing", 0) + 1
        if seen[ident]["missing"] >= GONE_AFTER and send(gone_message(seen[ident], klass, now), "", page_url):
            del seen[ident]
            reminded.pop(ident, None)
            counts["gone"] += 1
    for ident, a in current.items():
        if (ident in seen and a["due"] and a["submitted"] is False and 0 < a["due"] - now <= REMIND_BEFORE
                and reminded.get(ident) != a["due"]):
            if send(reminder_message(a, klass, now), "", page_url):
                reminded[ident] = a["due"]
                counts["reminded"] += 1
    fileio.write_json_atomic(SEEN_FILE, state)
    return counts


# ── the bot's thread ─────────────────────────────────────────────────────────────────────────


class Watcher:
    def __init__(self):
        self.http: requests.Session | None = None
        self.next_check = 0.0
        self.blocked_at: float | None = None       # the session could not be renewed: waiting for --login
        self.blocked_mtime: float | None = None
        self.retry_gap = RELOGIN_RETRY             # how long the block above lasts without a new session file
        self.failures = 0                         # failed reads in a row (network, maintenance, odd answer)
        self.ended = False


def _state_mtime() -> float | None:
    try:
        return STATE_FILE.stat().st_mtime
    except OSError:
        return None


def read_feed(w: Watcher, page_url: str, renew=None) -> dict:
    """The feed, renewing the session in the hidden browser once if Cambridge refuses it."""
    if w.http is None:
        w.http = session_from_state()
    try:
        return fetch(w.http, page_url)
    except Expired:
        log.info("Cambridge One: the session was refused — renewing it in a hidden browser")
        ok, _ = (renew or visit)(False)
        w.http = None
        if not ok:
            raise NeedsLogin("the saved session could not be renewed")
        w.http = session_from_state()
        try:
            return fetch(w.http, page_url)
        except Expired as e:
            raise NeedsLogin("renewed, but still refused") from e


def login_help(reason: str) -> str:
    """What the owner is told when the session could not be renewed."""
    by_hand = ("On the PC, run: python -m uninotifier.cambridge --login — sign in in the window that opens, "
               "then close it.")
    if credentials():
        return (f"The automatic sign-in did not work: {reason}. New English assignments are not being watched. "
                f"If the e-mail or password in .env changed, fix CAMBRIDGE_EMAIL / CAMBRIDGE_PASSWORD and restart "
                f"the bot. Either way you can sign in yourself: {by_hand}")
    return (f"New English assignments are not being watched. {by_hand} To stop doing this every few days, "
            f"put CAMBRIDGE_EMAIL and CAMBRIDGE_PASSWORD in .env and restart the bot.")


def tick(w: Watcher, now: float | None = None, today: date | None = None) -> dict | None:
    """Called often; checks only when due. The counts of a check that ran, else None."""
    now = time.time() if now is None else now
    if not active(today):
        if not w.ended:
            w.ended = True
            log.info("Cambridge One: not watching (%s not set, or past %s)", ENV_URL, ENV_UNTIL)
        return None
    if now < w.next_check:
        return None
    w.next_check = now + CHECK_SECONDS
    if w.blocked_at is not None:
        if _state_mtime() == w.blocked_mtime and now - w.blocked_at < w.retry_gap:
            return None                            # nothing new to try until --login (or the retry gap)
        w.blocked_at = w.blocked_mtime = None
    page_url = assignments_url()
    try:
        feed_url(page_url)
    except ValueError as e:
        w.ended = True
        failsafe.report("cambridge:url", f"{ENV_URL} in .env is not a Cambridge One assignments page", exc=e,
                        detail="Copy the address of your class's My assignments page into it, then restart the bot.")
        return None
    try:
        feed = read_feed(w, page_url)
    except NeedsLogin as e:
        w.blocked_at, w.blocked_mtime, w.http = now, _state_mtime(), None
        w.retry_gap = CREDENTIALS_RETRY if isinstance(e, CredentialsRejected) else RELOGIN_RETRY
        reason = notifier.redact(str(e))
        log.warning("Cambridge One: %s — run python -m uninotifier.cambridge --login", reason)
        failsafe.report("cambridge:login", "Cambridge One needs you to sign in again",
                        detail=login_help(reason))
        return None
    except (requests.RequestException, ValueError) as e:     # no network, or not the JSON (maintenance)
        log.info("Cambridge One: %s — trying again in %d min", type(e).__name__, RETRY_SECONDS // 60)
        w.http, w.next_check = None, now + RETRY_SECONDS
        w.failures += 1
        if w.failures == FAILURES_BEFORE_ALERT:    # quiet while it is a blip; told once it lasts
            failsafe.report("cambridge:read", "Cambridge One has not answered for an hour", exc=e,
                            detail="English assignments are not being watched meanwhile; it keeps trying.")
        return None
    w.failures = 0
    failsafe.resolve("cambridge:login", "✅ Cambridge One is signed in again — English assignments are watched.",
                     wait=False)
    failsafe.resolve("cambridge:read", "✅ Cambridge One answers again — English assignments are watched.", wait=False)
    return apply(feed, now, page_url)


def loop() -> None:
    """The bot's Cambridge One thread. Ends by itself after CAMBRIDGE_UNTIL."""
    w = Watcher()
    while not w.ended:
        try:
            tick(w)
        except Exception as e:
            log.exception("Cambridge One check failed")
            failsafe.bug("bot:cambridge", "The Cambridge One assignment check is failing", e,
                         "English assignments are not being watched until it is fixed.")
        time.sleep(60)


# ── --login: the visible browser ─────────────────────────────────────────────────────────────


def _signed_in(url: str, target: str) -> bool:
    """On the class's pages (not the sign-in screens)."""
    return urlsplit(url).path.startswith(urlsplit(target).path.rsplit("/", 1)[0])


class _Recorder:
    """Keeps every JSON answer the page receives, with how it was asked (header names only)."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.index: list[dict] = []
        self.count = 0
        self.feed_seen = False                     # the assignments feed has answered 200
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*"):
            old.unlink()

    def __call__(self, response) -> None:
        from playwright.sync_api import Error

        request = response.request
        kind = (response.headers.get("content-type") or "").lower()
        if request.resource_type not in ("xhr", "fetch") and "json" not in kind:
            return
        self.count += 1                            # numbered now: answers are handled interleaved
        entry = {"n": self.count, "method": request.method, "status": response.status,
                 "url": notifier.redact(response.url), "type": kind.split(";")[0],
                 "resource": request.resource_type}
        try:
            headers = request.all_headers()
        except Error:
            headers = {}
        entry["request_headers"] = sorted(h for h in headers if h != "cookie")
        auth = headers.get("authorization", "")
        entry["authorization"] = auth.split(" ", 1)[0] if auth else None   # the scheme, never the value
        cookies = dict(c.strip().split("=", 1) for c in headers.get("cookie", "").split(";") if "=" in c)
        entry["cookies"] = sorted(cookies)
        if FEED_PATH in response.url:              # what a plain-HTTP check would have to send
            entry["feed_headers"] = {h: headers[h] for h in FEED_PLAIN_HEADERS if h in headers}
            entry["csrf_header_is_cookie"] = headers.get("csrf-token") == cookies.get("csrf-token")
            self.feed_seen = self.feed_seen or response.status == 200
        if request.method != "GET" and request.post_data:
            entry["post_bytes"] = len(request.post_data)
        try:
            body = response.body()
        except Error:
            body = b""
        entry["bytes"] = len(body)
        if body and "json" in kind and len(body) <= MAX_BODY_BYTES:
            name = f"{entry['n']:03d}.json"
            (self.folder / name).write_bytes(body)
            entry["file"] = name
            if request.post_data and len(request.post_data) <= MAX_BODY_BYTES:
                (self.folder / f"{entry['n']:03d}.request.txt").write_text(request.post_data, encoding="utf-8")
        self.index.append(entry)

    def save(self) -> None:
        (self.folder / "index.json").write_text(json.dumps(self.index, indent=1, ensure_ascii=False),
                                                encoding="utf-8")


def _save_state(context) -> list[str]:
    """Saves the session; returns the local-storage key names (for the summary)."""
    state = context.storage_state()
    fileio.write_json_atomic(STATE_FILE, state)
    return sorted({item["name"] for origin in state.get("origins", []) for item in origin.get("localStorage", [])})


def login() -> int:
    """The visible-browser sign-in (and recording). Returns the exit code."""
    from playwright.sync_api import Error, sync_playwright

    target = assignments_url()
    if not target:
        print(f"Add {ENV_URL} to your .env file first.")
        return 1
    recorder = _Recorder(RECON_DIR)
    storage_keys: list[str] = []
    saved_at, shot_taken, signed_in = 0.0, False, False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE,
                                      storage_state=str(STATE_FILE) if STATE_FILE.exists() else None)
        context.on("response", recorder)               # every tab, if an assignment opens in a new one
        page = context.new_page()
        page.goto(target, wait_until="domcontentloaded")
        print("Sign in in the browser window. Once \"Screenshot taken\" is printed here, close the\n"
              "window (open an assignment first, if any is listed). It gives up after %d minutes." % LOGIN_MINUTES)
        deadline = time.time() + LOGIN_MINUTES * 60
        try:
            while time.time() < deadline and not page.is_closed():
                page.wait_for_timeout(1000)
                if not _signed_in(page.url, target) or not recorder.feed_seen:
                    continue                           # the class's address shows for a moment before the sign-in screen: the feed answering is the proof
                if time.time() - saved_at >= STATE_SAVE_SECONDS:      # first: closing the window may come any time
                    storage_keys = _save_state(context)
                    saved_at = time.time()
                if not signed_in:
                    signed_in = True
                    print("Signed in — the session is saved (and kept fresh until you close the window).")
                if not shot_taken and recorder.feed_seen:
                    page.wait_for_timeout(3000)        # the list renders after its feed (analytics never let the network go idle)
                    page.screenshot(path=str(RECON_DIR / "assignments.png"), full_page=True)
                    shot_taken = True
                    print("Screenshot taken — you can close the window.")
        except Error as e:
            if not page.is_closed():                   # closing the window is the normal way out; anything else is shown
                print(f"Stopped early: {notifier.redact(str(e)).splitlines()[0]}")
        finally:
            recorder.save()
            try:
                browser.close()
            except Error:
                pass

    print(f"\nRecorded {len(recorder.index)} answer(s) in {RECON_DIR}")
    for e in recorder.index:
        path = urlsplit(e["url"]).path
        print(f"  {e['n']:3d} {e['method']:4} {e['status']} {e['bytes']:>8} B  {urlsplit(e['url']).netloc}{path[:90]}"
              f"{'  auth=' + e['authorization'] if e['authorization'] else ''}")
    if not signed_in:
        print("Never reached the class's pages — the session was NOT saved.")
        return 1
    print(f"Session saved to {STATE_FILE.name}; local-storage keys: {', '.join(storage_keys) or '(none)'}")
    return 0


def check_now() -> int:
    """--check: one check, printed."""
    notifier.setup_logging(paths.LOGS / "bot.log")
    if not active():
        print(f"Not watching: set {ENV_URL} and {ENV_UNTIL} (YYYY-MM-DD, not past) in .env")
        return 1
    w = Watcher()
    started = time.time()
    counts = tick(w)
    failsafe.wait_idle()
    print(f"{time.time() - started:.2f} s: {counts}" if counts is not None else "No check (see data/logs/bot.log)")
    return 0 if counts is not None else 1


def main(argv: list[str]) -> int:
    if "--login" in argv:
        return login()
    if "--check" in argv:
        return check_now()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
