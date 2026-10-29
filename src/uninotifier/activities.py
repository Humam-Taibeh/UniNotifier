"""
Two Moodle activities ZUJ uses for online teaching, read from their pages:

  teamscheduler  a Microsoft Teams meeting. The page shows the meeting's
                 details (type, repeat days, period, time, organizer) and the
                 join link in a read-only field (#teamscheduler-meetingurl).
  attendance     mod_attendance. The page lists every session (date, time,
                 description, status, points). While a session is open for
                 self-marking, its Status cell holds a "Submit attendance"
                 link (attendance.php?sessid=…&sesskey=…) — the only way in.

`check_in()` follows that link and fills in the form behind it the way the
student would: the "Present" choice, then Save changes. It never decides
whether that worked — checkin.py reads the attendance page again for that.
"""

import html as htmlmod
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urljoin

import lxml.etree
import lxml.html

log = logging.getLogger("uninotifier")

TEAMS_JOIN_RE = re.compile(r"https://teams\.microsoft\.com/l/meetup-join/[^\s\"'<>]+")
SUBMIT_LINK_RE = re.compile(r"/mod/attendance/attendance\.php\?[^\"'<>\s]*sessid=\d+")
PRESENT_RE = re.compile(r"^\s*(present|حاضر)\s*(\(.*\))?\s*$", re.I)    # "Present (Available for 10 minutes)"
PASSWORD_FIELDS = {"studentpassword", "qrpass"}     # a session password, or a QR code's
ONGOING_GRACE = timedelta(minutes=90)     # a session that started this long ago still counts as "next"

MEETING_ICONS = {"meeting type": "🗂️", "repeat": "🔁", "repeat on": "📆", "recurrence period": "📅",
                 "meeting time": "🕖", "organizer": "👤", "date": "📅", "start": "🕖", "duration": "⏱️"}


def _doc(page_html: str):
    try:
        return lxml.html.fromstring(page_html or "<html></html>")
    except (ValueError, lxml.etree.ParserError):
        return lxml.html.fromstring("<html></html>")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.text_content()).strip()


def _main(doc):
    found = doc.xpath('//*[@role="main"]')
    return found[0] if found else doc


# ----------------------------------------------------------------- Teams

def parse_meeting(page_html: str) -> dict:
    """{"url": join link or None, "details": [(label, value), …]}."""
    doc = _doc(page_html)
    main = _main(doc)
    url = next(iter(doc.xpath('//input[@id="teamscheduler-meetingurl"]/@value')), None)
    if not url:
        match = TEAMS_JOIN_RE.search(htmlmod.unescape(page_html or ""))
        url = match.group(0) if match else None
    details = []
    for row in main.xpath('.//table[contains(@class, "generaltable")]//tr'):
        cells = [_text(c) for c in row.xpath("./td|./th")]
        if len(cells) == 2 and all(cells):
            details.append((cells[0], cells[1]))
    return {"url": url.strip() if url else None, "details": details}


def meeting_icon(label: str) -> str:
    return MEETING_ICONS.get(label.strip().lower(), "•")


# ------------------------------------------------------------ attendance

def parse_sessions(page_html: str, base_url: str = "https://elearning.zuj.edu.jo") -> list[dict]:
    """Every session row: {"date", "time", "start", "end", "description", "status",
    "points", "remarks", "submit_url"}. start and end are datetimes when they parse."""
    sessions = []
    for row in _doc(page_html).xpath('//table[contains(@class, "generaltable")]/tbody/tr'):
        date_cell = row.xpath('./td[contains(@class, "datecol")]')
        if not date_cell:
            continue
        parts = [_text(n) for n in date_cell[0].xpath(".//nobr")] or [_text(date_cell[0])]
        day, time_range = parts[0], (parts[1] if len(parts) > 1 else "")

        def cell(cls):
            found = row.xpath(f'./td[contains(@class, "{cls}")]')
            return _text(found[0]) if found else ""
        links = [urljoin(base_url, h) for h in row.xpath('./td[contains(@class, "statuscol")]//a/@href')
                 if SUBMIT_LINK_RE.search(h)]
        sessions.append({"date": day, "time": time_range, "start": _start(day, time_range),
                         "end": _end(day, time_range), "description": cell("desccol"),
                         "status": cell("statuscol"), "points": cell("pointscol"),
                         "remarks": cell("remarkscol"), "submit_url": links[0] if links else None})
    return sessions


def _at(day: str, clock: str) -> datetime | None:
    clock = clock.strip().upper().replace(" ", "")
    for fmt in ("%a %d %b %Y %I:%M%p", "%a %d %b %Y %I%p"):
        try:
            return datetime.strptime(f"{day} {clock}", fmt)
        except ValueError:
            continue
    return None


def _start(day: str, time_range: str) -> datetime | None:
    """'Wed 14 Oct 2026' + '7PM - 8:30PM' -> 2026-10-14 19:00."""
    found = _at(day, time_range.split("-")[0])
    if found:
        return found
    try:
        return datetime.strptime(day, "%a %d %b %Y")
    except ValueError:
        return None


def _end(day: str, time_range: str) -> datetime | None:
    """'Wed 14 Oct 2026' + '7PM - 8:30PM' -> 2026-10-14 20:30 (None without an end time)."""
    parts = time_range.split("-")
    return _at(day, parts[1]) if len(parts) > 1 else None


def recorded(session: dict) -> bool:
    """Attendance has been taken for this session (its status is anything but "?")."""
    return session["status"].strip() not in ("", "?") and not session["submit_url"]


def next_session(sessions: list[dict], now: datetime | None = None) -> dict | None:
    """The session in progress or the next one to come."""
    now = now or datetime.now()
    upcoming = [s for s in sessions if s["start"] and s["start"] + ONGOING_GRACE >= now]
    return min(upcoming, key=lambda s: s["start"]) if upcoming else None


def open_session(sessions: list[dict]) -> dict | None:
    """The session that can be self-marked right now (it has a submit link)."""
    return next((s for s in sessions if s["submit_url"]), None)


# --------------------------------------------------------- self check-in

@dataclass
class CheckInForm:
    action: str
    fields: dict[str, str] = field(default_factory=dict)        # hidden inputs and other values
    choices: dict[str, tuple[str, str]] = field(default_factory=dict)   # label -> (radio name, value)
    password: bool = False                                       # the session asks for a password


def read_form(page_html: str, page_url: str) -> CheckInForm | None:
    """The self-marking form on a check-in page (mod_attendance's studentattendance
    form: sessid and sesskey hidden, one "status" radio per choice, Save changes and
    Cancel): where it posts, its fields, and the status choices by their label. A
    session set to give the status itself has no choices — only Save changes."""
    doc = _doc(page_html)
    forms = (doc.xpath('//form[.//input[@name="sessid"]]')
             or [f for f in doc.xpath("//form") if f.xpath('.//input[@type="radio"]')])
    if not forms:
        return None
    form = forms[0]
    result = CheckInForm(action=urljoin(page_url, form.get("action") or page_url))
    for inp in form.xpath(".//input"):
        name, kind = inp.get("name"), (inp.get("type") or "text").lower()
        if not name:
            continue
        if kind == "password" or name in PASSWORD_FIELDS:
            result.password = True
        elif kind == "radio":
            ids = inp.get("id")
            label = doc.xpath(f'//label[@for="{ids}"]') if ids else []
            text = _text(label[0]) if label else _text(inp.getparent())
            result.choices[text or inp.get("value", "")] = (name, inp.get("value", ""))
        elif kind == "submit" and name == "cancel":
            continue                       # sending it would cancel the form
        elif kind in ("hidden", "text", "submit") and inp.get("value") is not None:
            result.fields.setdefault(name, inp.get("value"))
    return result


def check_in(http, session: dict, dry_run: bool = True) -> dict:
    """Self-mark one session as Present, as the student would: open its submit
    link, choose "Present" (حاضر) and Save changes.

    Returns {"ok", "reason", "plan", "retry", "page"}: ok means Moodle took the
    form (not yet that it recorded you — read the attendance page for that);
    retry, that trying again later may help (Moodle did not answer); plan, the
    POST sent (or that would be, with dry_run); page, the check-in page as seen.
    Never submits a session that asks for a password or a QR code, nor one
    without a "Present" choice. A session that gives the status itself has no
    choices: its form is sent as it is, the way Save changes would."""
    page = None

    def result(ok: bool, reason: str, plan=None, retry: bool = False) -> dict:
        return {"ok": ok, "reason": reason, "plan": plan, "retry": retry, "page": page}

    if not session.get("submit_url"):
        return result(False, "check-in is not open for this session", retry=True)
    resp = http.get(session["submit_url"])
    if not resp.ok:
        return result(False, f"Moodle answered HTTP {resp.status}", retry=True)
    page = resp.text()
    form = read_form(page, session["submit_url"])
    if form is None:
        return result(False, "the check-in page has no form")
    if form.password:
        return result(False, "this session asks for a password or a QR code")
    data = dict(form.fields)
    if form.choices:
        choice = next((v for label, v in form.choices.items() if PRESENT_RE.match(label)), None)
        if choice is None:
            return result(False, "the form has no 'Present' choice (only: " + ", ".join(form.choices) + ")")
        data[choice[0]] = choice[1]
    plan = {"url": form.action, "data": data}
    if dry_run:
        return result(False, "dry run", plan)
    sent = http.post(plan["url"], data=plan["data"],
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    if not sent.ok:
        return result(False, f"Moodle refused the form (HTTP {sent.status})", plan, retry=True)
    return result(True, "submitted", plan)
