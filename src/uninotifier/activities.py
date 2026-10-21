"""
Two Moodle activities ZUJ uses for online teaching, read from their pages:

  teamscheduler  a Microsoft Teams meeting. The page shows the meeting's
                 details (type, repeat days, period, time, organizer) and the
                 join link in a read-only field (#teamscheduler-meetingurl).
  attendance     mod_attendance. The page lists every session (date, time,
                 description, status, points). While a session is open for
                 self-marking, its Status cell holds a "Submit attendance"
                 link (attendance.php?sessid=…&sesskey=…) — the only way in.

Self check-in is prepared but not switched on: no session has been open yet,
so the form behind that link has never been seen. `check_in()` already finds
the open session, reads whatever form it leads to and picks the "Present"
choice, but stops before submitting until SUBMIT_VERIFIED is set — see its
docstring for the one step left.
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
PRESENT_RE = re.compile(r"^\s*(present|حاضر)\s*$", re.I)
ONGOING_GRACE = timedelta(minutes=90)     # a session that started this long ago still counts as "next"
SUBMIT_VERIFIED = False                    # see check_in()

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
    """Every session row: {"date", "time", "start", "description", "status",
    "points", "submit_url"}. start is a datetime when the date parses."""
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
                         "description": cell("desccol"), "status": cell("statuscol"),
                         "points": cell("pointscol"), "submit_url": links[0] if links else None})
    return sessions


def _start(day: str, time_range: str) -> datetime | None:
    """'Wed 14 Oct 2026' + '7PM - 8:30PM' -> 2026-10-14 19:00."""
    first = time_range.split("-")[0].strip().upper().replace(" ", "")
    for fmt in ("%a %d %b %Y %I:%M%p", "%a %d %b %Y %I%p"):
        try:
            return datetime.strptime(f"{day} {first}", fmt)
        except ValueError:
            continue
    try:
        return datetime.strptime(day, "%a %d %b %Y")
    except ValueError:
        return None


def next_session(sessions: list[dict], now: datetime | None = None) -> dict | None:
    """The session in progress or the next one to come."""
    now = now or datetime.now()
    upcoming = [s for s in sessions if s["start"] and s["start"] + ONGOING_GRACE >= now]
    return min(upcoming, key=lambda s: s["start"]) if upcoming else None


def open_session(sessions: list[dict]) -> dict | None:
    """The session that can be self-marked right now (it has a submit link)."""
    return next((s for s in sessions if s["submit_url"]), None)


# ---------------------------------------------------- self check-in (stub)

@dataclass
class CheckInForm:
    action: str
    fields: dict[str, str] = field(default_factory=dict)        # hidden inputs and other values
    choices: dict[str, tuple[str, str]] = field(default_factory=dict)   # label -> (radio name, value)
    password: bool = False                                       # the session asks for a password


def read_form(page_html: str, page_url: str) -> CheckInForm | None:
    """The self-marking form on a check-in page: where it posts, its hidden
    fields, and the status choices by their label."""
    doc = _doc(page_html)
    forms = [f for f in doc.xpath("//form") if f.xpath('.//input[@type="radio"]')]
    if not forms:
        return None
    form = forms[0]
    result = CheckInForm(action=urljoin(page_url, form.get("action") or page_url))
    for inp in form.xpath(".//input"):
        name, kind = inp.get("name"), (inp.get("type") or "text").lower()
        if not name:
            continue
        if kind == "radio":
            ids = inp.get("id")
            label = doc.xpath(f'//label[@for="{ids}"]') if ids else []
            text = _text(label[0]) if label else _text(inp.getparent())
            result.choices[text or inp.get("value", "")] = (name, inp.get("value", ""))
        elif kind == "password":
            result.password = True
        elif kind in ("hidden", "text", "submit") and inp.get("value") is not None:
            result.fields.setdefault(name, inp.get("value"))
    return result


def check_in(http, sessions: list[dict], dry_run: bool = True) -> dict:
    """Self-mark attendance for the open session as Present.

    Done so far: find the open session's submit link, open it, read the form
    and choose "Present" (حاضر). Returns {"ok", "reason", "plan"}; the plan is
    the POST that would be sent.

    Left to do, once a real check-in page is available (open one during a
    session and save its HTML): confirm that read_form() finds the form and
    that posting the plan marks attendance, then set SUBMIT_VERIFIED = True.
    Until then nothing is ever submitted, dry_run or not. Sessions protected
    by a password are never submitted automatically."""
    session = open_session(sessions)
    if session is None:
        return {"ok": False, "reason": "no session is open for check-in", "plan": None}
    resp = http.get(session["submit_url"])
    form = read_form(resp.text(), session["submit_url"]) if resp.ok else None
    if form is None:
        return {"ok": False, "reason": "the check-in page has no form", "plan": None}
    choice = next((v for label, v in form.choices.items() if PRESENT_RE.match(label)), None)
    if choice is None:
        return {"ok": False, "reason": "no 'Present' choice on the form", "plan": None}
    plan = {"url": form.action, "data": {**form.fields, choice[0]: choice[1]}}
    if form.password:
        return {"ok": False, "reason": "this session needs a password", "plan": plan}
    if dry_run or not SUBMIT_VERIFIED:
        return {"ok": False, "reason": "dry run" if dry_run else "submitting is not verified yet", "plan": plan}
    sent = http.post(plan["url"], data=plan["data"],
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    return {"ok": sent.ok, "reason": "submitted" if sent.ok else f"HTTP {sent.status}", "plan": plan}
