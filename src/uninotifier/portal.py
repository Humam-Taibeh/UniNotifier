"""
On-demand access to the ZUJ registration portal (https://portal.zuj.edu.jo):
official grades, the transcript (passed hours) and the financial balance.

Only ever run by a command from the user (/grades, /transcript, /balance) —
nothing here is scheduled or polled, and monitor.py does not import it.

Credentials come from .env, never from the chat:
    PORTAL_USERNAME=...
    PORTAL_PASSWORD=...
(.env is re-read on every request, so editing it needs no restart.)

The portal asks for an emailed/texted verification code after the password
("التحقق من الأمان"). fetch() relays it through the `ask_code` callback — the
bot asks in Telegram and waits for the reply.

The pages behind the login were written without access to them, so the
navigation is defensive: the section is found by its menu text (Arabic or
English), every table on the page is read, and a screenshot is returned
beside the parsed text — the screenshot is the source of truth. What was on
screen is also kept in portal_debug/ (git-ignored: it holds personal data) so
the parsing can be tightened against the real pages.

The owner's session is kept (data/portal_session.json) and reused for REUSE_SECONDS after its last
read: the browser opens the student pages with its cookies and skips the sign-in. Measured
2026-10-20: a session survives 20 idle minutes, not 40; the portal never asked for a code in 26
sign-ins; its only cookies (JSESSIONID, a load-balancer route) end with the browser, so there is
no "trusted device" to keep. An ended session answers with the sign-in form (HTTP 200, same
address): then it signs in as usual. A guest's sign-in never touches the owner's session.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from dotenv import dotenv_values

from uninotifier import failsafe
from uninotifier import fileio
from uninotifier import gradebook
from uninotifier import maintenance
from uninotifier import transcript
from uninotifier.notifier import escape_md
from uninotifier import paths

log = logging.getLogger("uninotifier")

ENV_FILE = paths.ENV_FILE
DEBUG_DIR = paths.DATA / "portal_debug"
SITE = "https://portal.zuj.edu.jo"
LOGIN_URL = f"{SITE}/faces/ui/login.xhtml"
ENV_USERNAME, ENV_PASSWORD = "PORTAL_USERNAME", "PORTAL_PASSWORD"
SITE_NAME = "registration portal"

LOGIN_TIMEOUT = 120          # seconds for the whole sign-in, verification included
CODE_SETTLE = 8              # seconds to wait for a submitted code before judging it
MAX_CODE_TRIES = 3
VIEWPORT = {"width": 1400, "height": 1000}
SCALE = 2
MAX_SHOT_HEIGHT = 8000       # CSS px; Chromium can't render taller than ~16k device px
MAX_TABLE_ROWS = 80          # rows sent as text; the screenshot has the rest
SESSION_FILE = paths.DATA / "portal_session.json"
REUSE_SECONDS = 15 * 60      # a session idle longer than this is not tried (it survives 20 min idle, not 40)
HOME_PATH = "/faces/ui/pages/student/index.xhtml"

# Each section's page address (read from the portal's own menu), and — as a
# fallback should an address ever change — the menu text to look for.
SECTIONS = {
    "grades": {
        "title": "Grades",
        "path": "/faces/ui/pages/student/courseResult/index.xhtml",          # "نتائج المواد"
        "match": r"نتائج|علامات|العلامات|Grades?|Results?|Marks",
        "skip": r"كشف|Transcript",
    },
    "transcript": {
        "title": "Transcript",
        "path": "/faces/ui/pages/student/transcript/index.xhtml",            # "السجل الاكاديمي"
        "match": r"كشف\s*(?:العلامات|الدرجات)|السجل\s*الأكاديمي|Transcript|Academic\s*Record",
        "skip": r"$^",
    },
    "balance": {
        "title": "Balance",
        "path": "/faces/ui/pages/student/financialRecord/index.xhtml",       # "السجل المالي"
        "match": r"الحساب\s*المالي|الوضع\s*المالي|الرصيد|الرسوم|المالية|Financial|Balance|Account\s*Statement|Fees",
        "skip": r"$^",
    },
}

_NUM = r"(-?\d[\d,]*(?:\.\d+)?)"
_GAP = r"[^\d-]{0,25}"   # label, colon, spaces — up to the number (a minus sign is part of it)
FIGURES = {
    "passed_hours": [
        rf"(?:الساعات|ساعات)\s*(?:المنجزة|المجتازة|الناجحة|المقطوعة|المكتسبة){_GAP}{_NUM}",
        rf"(?:passed|completed|earned)\s*(?:credit\s*)?hours{_GAP}{_NUM}"],
    "cgpa": [
        rf"المعدل\s*التراكمي{_GAP}{_NUM}",
        rf"(?:cumulative|overall)\s*(?:gpa|average){_GAP}{_NUM}"],
    "balance": [
        rf"(?:الرصيد|المبلغ\s*المستحق|الذمم){_GAP}{_NUM}",
        rf"(?:balance|amount\s*due){_GAP}{_NUM}"],
}


STATUS_NAMES = {"منتظم": "Regular / Good Standing", "إنذار": "Academic Warning", "انذار": "Academic Warning",
                "مراقب": "Academic Probation", "مفصول": "Dismissed", "متخرج": "Graduated", "مؤجل": "Deferred"}
STATUS_RE = re.compile(r"الحالة\s*:?\s*([^\s\d:]+)")


def english_status(raw: str) -> str:
    """The academic status the transcript shows (منتظم …) in English."""
    return next((name for word, name in STATUS_NAMES.items() if word in raw), raw)


class PortalError(Exception):
    """Something the user should read (the message is shown in chat)."""


@dataclass
class Result:
    section: str
    found: bool = False                      # the section's page was reached
    png: bytes | None = None                 # screenshot of that page (or of where we got stuck)
    tables: list[list[list[str]]] = field(default_factory=list)
    figures: dict[str, float] = field(default_factory=dict)
    menu: list[str] = field(default_factory=list)   # menu texts seen, when the section wasn't found
    lines: list[str] = field(default_factory=list)  # the page's content as Markdown, in English
    data: dict = field(default_factory=dict)        # structured content: "semesters" / "results" / "term"


# ------------------------------------------------------------- credentials

def credentials() -> tuple[str, str] | None:
    env = {**dotenv_values(ENV_FILE), **{k: v for k, v in os.environ.items() if k.startswith("PORTAL_")}}
    user, password = env.get(ENV_USERNAME), env.get(ENV_PASSWORD)
    return (user, password) if user and password else None


# ---------------------------------------------------------- pure parsing

def clean_tables(raw: list[list[list[str]]]) -> list[list[list[str]]]:
    """Keep real data tables: at least two rows and two columns of content,
    no row entirely empty."""
    out = []
    for table in raw:
        rows = [[re.sub(r"\s+", " ", c or "").strip() for c in row] for row in table]
        rows = [r for r in rows if any(r)]
        if len(rows) >= 2 and max(len([c for c in r if c]) for r in rows) >= 2:
            out.append(rows)
    return out


def find_figures(text: str) -> dict[str, float]:
    """Best-effort numbers from the page text: passed hours, CGPA, balance."""
    found = {}
    for key, patterns in FIGURES.items():
        for pattern in patterns:
            match = re.search(pattern, text or "", re.I)
            if match:
                try:
                    found[key] = float(match.group(1).replace(",", ""))
                except ValueError:
                    continue
                break
    return found


def table_lines(tables: list[list[list[str]]], limit: int = MAX_TABLE_ROWS,
                translate: bool = False) -> list[str]:
    """Plain 'cell | cell | cell' lines (Markdown-escaped); the first row of
    each table is its header, translated to English with translate=True. A
    blank line separates tables; output stops after `limit` rows."""
    lines, used = [], 0
    for table in tables:
        for n, row in enumerate(table):
            if used >= limit:
                lines.append(f"… {sum(len(t) for t in tables) - used} more rows — see the screenshot")
                return lines
            cells = [HEADER_NAMES.get(c, c) if translate and n == 0 else c for c in row if c]
            text = escape_md(" | ".join(cells))
            lines.append(f"*{text}*" if n == 0 else text)
            used += 1
        lines.append("")
    return lines


# ------------------------------------------------- page-specific readers
# Written against the real pages (dumps taken 2026-10-11). The transcript is one
# collapsible block per semester; the financial record is one table plus a
# "total balance" bar; the results page is a table with an "empty" message.

TERM_NAMES = {"الأول": "First semester", "الثاني": "Second semester", "الصيفي": "Summer semester"}
HEADER_NAMES = {"رمز المقرر": "Code", "اسم المقرر": "Course", "النشاط": "Activity", "الحالة": "Status",
                "العلامة": "Mark", "ناجح؟": "Passed", "ناجح": "Passed", "عدد الساعات المعتمدة": "Hours",
                "الفصل الدراسي": "Semester", "الرصيد": "Balance", "التخصص": "Major"}
NO_RESULTS = re.compile(r"لا\s*يوجد\s*(?:نتائج|بيانات)")

_TRANSCRIPT_JS = """() => [...document.querySelectorAll('fieldset.ui-fieldset-toggleable')].map(f => {
    const table = f.querySelector('table');
    const clean = s => s.replace(/\\s+/g, ' ').trim();
    const cell = td => {
        const box = td.querySelector('.ui-chkbox-box');
        if (box) return box.classList.contains('ui-state-active') ? '✓' : '✗';
        const c = td.cloneNode(true);
        c.querySelectorAll('.ui-column-title').forEach(e => e.remove());
        return clean(c.innerText);
    };
    return {legend: clean(f.querySelector('legend').innerText), text: f.innerText,
            heads: table ? [...table.querySelectorAll('thead th')].map(th => clean(th.innerText)) : [],
            rows: table ? [...table.querySelectorAll('tbody tr')].map(tr => [...tr.cells].map(cell)) : []};
})"""
_RESULTS_JS = """() => ({
    term: ([...document.querySelectorAll('.ui-selectonemenu-label')]
        .map(e => e.innerText.trim()).find(t => /\\d{4}\\/\\d{4}/.test(t))) || '',
    text: document.body.innerText})"""


_GRID_JS = """() => {
    const clean = s => s.replace(/\\s+/g, ' ').trim();
    const cell = td => {
        const box = td.querySelector('.ui-chkbox-box');
        if (box) return box.classList.contains('ui-state-active') ? '✓' : '✗';
        const c = td.cloneNode(true);
        c.querySelectorAll('.ui-column-title').forEach(e => e.remove());
        return clean(c.innerText);
    };
    return [...document.querySelectorAll('table')].filter(t => t.offsetWidth || t.offsetHeight).map(t => ({
        heads: [...t.querySelectorAll('thead th')].map(th => clean(th.innerText)),
        rows: [...t.querySelectorAll('tbody tr')].map(tr => [...tr.cells].map(cell))}));
}"""


def english_term(legend: str) -> str:
    """'الفصل الثاني 2025/2026 20252' -> 'Second semester 2025/2026'."""
    year = re.search(r"\d{4}/\d{4}", legend or "")
    name = next((v for k, v in TERM_NAMES.items() if k in (legend or "")), "Semester")
    return f"{name} {year.group(0)}" if year else name


def _number(text: str) -> float | None:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text or "")
    return float(match.group(0).replace(",", "")) if match else None


def parse_transcript(raw: list[dict]) -> list[dict]:
    """The semester blocks, newest first as the portal lists them:
    {"term", "average", "cumulative", "courses": [{"code", "name", "hours",
    "mark", "passed" (True / False / None while in progress), "repeated"}]}."""
    semesters = []
    for block in raw:
        heads = block.get("heads", [])
        col = {key: next((i for i, h in enumerate(heads) if word in h), None)
               for key, word in (("code", "رمز"), ("name", "اسم"), ("hours", "الساعات"),
                                 ("mark", "العلامة"), ("passed", "ناجح"), ("repeated", "معادة"))}
        courses = []
        for row in block.get("rows", []):
            if len(row) < len(heads) or col["code"] is None:
                continue                                   # the "no data" row
            passed = row[col["passed"]] if col["passed"] is not None else ""
            courses.append({
                "code": row[col["code"]], "name": row[col["name"]] if col["name"] is not None else "",
                "hours": _number(row[col["hours"]]) or 0 if col["hours"] is not None else 0,
                "mark": _number(row[col["mark"]]) if col["mark"] is not None else None,
                "passed": True if passed == "✓" else False if passed == "✗" else None,
                "repeated": col["repeated"] is not None and row[col["repeated"]] == "✓"})
        graded = {c["code"] for c in courses if c["passed"] is not None}
        courses = [c for c in courses if c["passed"] is not None or c["code"] not in graded]  # stale duplicate rows
        text = block.get("text", "")
        average =re.search(r"المعدل\s*الفصلي\s*(\d+(?:\.\d+)?)", text)
        cumulative = re.search(r"المعدل\s*التراكمي\s*(\d+(?:\.\d+)?)", text)
        semesters.append({"term": english_term(block.get("legend", "")),
                          "average": float(average.group(1)) if average else None,
                          "cumulative": float(cumulative.group(1)) if cumulative else None,
                          "courses": courses})
    return semesters


def passed_hours(semesters: list[dict]) -> float:
    """Credit hours of every course passed, each course counted once."""
    hours = {}
    for sem in semesters:
        for c in sem["courses"]:
            if c["passed"]:
                hours[c["code"]] = c["hours"]
    return sum(hours.values())


def transcript_lines(semesters: list[dict]) -> list[str]:
    """The semester blocks (see transcript.py)."""
    return transcript.blocks(semesters)


def parse_balance(text: str, tables: list[list[list[str]]]) -> dict:
    """{"total": float|None (positive = owed / debit, negative = credit),
    "rows": [(term, hours, balance)]} from the financial record page."""
    total = None
    match = re.search(r"الرصيد\s*الكلي\s*(-?\d[\d,]*(?:\.\d+)?)\s*(مدين|دائن)?", text or "")
    if match:
        total = float(match.group(1).replace(",", ""))
        if match.group(2) == "دائن":
            total = -total
    rows = []
    for table in tables:
        head = table[0]
        find = lambda word: next((i for i, h in enumerate(head) if word in h), None)
        term_col, hours_col, balance_col = find("الفصل"), find("الساعات"), find("الرصيد")
        if term_col is None or balance_col is None:
            continue
        for row in table[1:]:
            if len(row) > max(term_col, balance_col):
                rows.append((english_term(row[term_col]), _number(row[hours_col]) if hours_col is not None
                             and len(row) > hours_col else None, _number(row[balance_col]) or 0.0))
    return {"total": total, "rows": rows}


def parse_major(tables: list[list[list[str]]]) -> str | None:
    """The student's major as the financial-record page lists it (a column of its semester table) —
    the newest semester's. Only the major is taken from that page; nothing else of it is used."""
    for table in tables:
        head = table[0]
        col = next((i for i, h in enumerate(head) if "التخصص" in h), None)
        if col is None:
            continue
        for row in table[1:]:
            if len(row) > col and re.search(r"[A-Za-z\u0600-\u06FF]", row[col] or ""):
                return row[col].strip()
    return None


def balance_lines(info: dict) -> list[str]:
    lines = []
    total = info["total"]
    if total is not None:
        side = "owed to the university (debit)" if total > 0 else "in your favour (credit)" if total < 0 else "settled"
        lines.append(f"💰 *Total balance:* {abs(total):,.2f} JD — {side}".replace(".00", ""))
    due = [(t, h, b) for t, h, b in info["rows"] if b]
    for term, hours, amount in due:
        lines.append(f"• {term}" + (f" · {hours:g}h" if hours else "") + f" · {amount:,.2f} JD".replace(".00", ""))
    if info["rows"] and not due:
        lines.append("• Every semester shows a zero balance.")
    elif due and len(due) < len(info["rows"]):
        lines.append("• All other semesters: 0 JD")
    return lines


def parse_results(grids: list[dict]) -> list[dict]:
    """The rows of the course-results table — [{"code", "name", "activity",
    "status", "mark" (float|None), "passed" (True/False/None)}] — found by its
    column titles, so the order of the columns doesn't matter."""
    for grid in grids:
        heads = grid.get("heads", [])
        col = {key: next((i for i, h in enumerate(heads) if word in h), None)
               for key, word in (("code", "رمز"), ("name", "اسم"), ("activity", "النشاط"),
                                 ("status", "الحالة"), ("mark", "العلامة"), ("passed", "ناجح"))}
        if col["code"] is None or col["mark"] is None:
            continue
        rows = []
        for row in grid.get("rows", []):
            if len(row) < len(heads) or not re.search(r"\d", row[col["code"]]):
                continue                                      # the "no results" row
            passed = row[col["passed"]] if col["passed"] is not None else ""
            rows.append({
                "code": row[col["code"]], "name": row[col["name"]] if col["name"] is not None else "",
                "activity": row[col["activity"]] if col["activity"] is not None else "",
                "status": row[col["status"]] if col["status"] is not None else "",
                "mark": _number(row[col["mark"]]),
                "passed": True if passed == "\u2713" else False if passed == "\u2717" else None})
        return rows
    return []


def results_lines(raw: dict, tables: list[list[list[str]]], rows: list[dict] | None = None) -> list[str]:
    term = english_term(raw.get("term", "")) if raw.get("term") else "this semester"
    if rows:
        lines = [f"📅 *{term}*"]
        for r in rows:
            mark = f"{r['mark']:g}" if r["mark"] is not None else "—"
            verdict = "" if r["mark"] is None else (" ✅" if r["passed"] is not False and r["mark"] >= 50 else " ❌")
            lines.append(f"• {escape_md(r['code'])} {escape_md(r['name'])}"
                         + (f" ({escape_md(r['activity'])})" if r["activity"] else "") + f" · {mark}{verdict}")
        return lines
    if NO_RESULTS.search(raw.get("text", "")) and not any(len(t) > 1 and not NO_RESULTS.search(" ".join(t[1])) for t in tables):
        return [f"ℹ️ No results are published yet for {term}.", "They appear here as the instructors release them."]
    return [f"📅 *{term}*"] + table_lines(tables, translate=True)


# --------------------------------------------------------------- browser

_TABLES_JS = """() => {
    const shown = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
    return [...document.querySelectorAll('table')]
        .filter(t => shown(t) && !t.querySelector('table'))
        .map(t => [...t.rows].map(r => [...r.cells].map(c => c.innerText)));
}"""
_MENU_JS = """() => [...new Set([...document.querySelectorAll('a, .ui-menuitem-text, [role=menuitem]')]
    .filter(e => e.offsetWidth || e.offsetHeight)
    .map(e => e.innerText.replace(/\\s+/g, ' ').trim())
    .filter(t => t && t.length <= 60))]"""


def _visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector)
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


KNOWN_ERRORS = {
    "اسم المستخدم / كلمة المرور غير صحيحة": "Incorrect username or password.",
}


def _error_text(page) -> str:
    """The error the login form is showing (a PrimeFaces growl), in English
    when the message is a known one."""
    try:
        texts = page.locator(".ui-growl-error .ui-growl-message, .ui-messages-error, "
                             ".ui-message-error").all_inner_texts()
    except Exception:
        return ""
    text = re.sub(r"\s+", " ", " ".join(texts)).strip()
    return KNOWN_ERRORS.get(text, text)


def _signed_in(page) -> bool:
    """The sign-in form is gone (a page mid-navigation counts as not yet)."""
    try:
        return page.locator('#lognForm input[type="password"]').count() == 0
    except Exception:
        return False


def login(page, username: str, password: str, ask_code) -> None:
    """Sign in. ask_code(attempt, rejected) -> str | None supplies the
    verification code when the portal asks for one (None = nobody answered)."""
    page.goto(LOGIN_URL, wait_until="networkidle", timeout=45000)
    if maintenance.detect(maintenance.page_text(page)):
        raise maintenance.unavailable(page, SITE_NAME, "the sign-in page shows a maintenance notice")
    form = page.locator("#lognForm")
    form.locator('input[type="text"]').first.fill(username)
    form.locator('input[type="password"]').first.fill(password)
    form.locator('button[type="submit"]').first.click()

    dialog = page.locator("#lognForm .ui-dialog")
    deadline = time.monotonic() + LOGIN_TIMEOUT
    submitted_at, tries = 0.0, 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass                                    # ajax still running: look again
        if _signed_in(page):
            return
        if dialog.count() and dialog.first.is_visible():
            if time.monotonic() - submitted_at < CODE_SETTLE:
                continue                            # a submitted code is being checked
            if tries >= MAX_CODE_TRIES:
                raise PortalError("The portal rejected the verification code three times.")
            code = ask_code(tries, tries > 0)
            if not code:
                raise PortalError("No verification code arrived in time — try the command again.")
            dialog.first.locator('input[type="password"], input[type="text"]').first.fill(code)
            dialog.first.locator("button").last.click()
            submitted_at, tries = time.monotonic(), tries + 1
            deadline = max(deadline, time.monotonic() + 30)
            continue
        message = _error_text(page)
        if message:
            if maintenance.detect(message):
                raise maintenance.unavailable(page, SITE_NAME, message)
            raise PortalError(f"The portal refused the sign-in: {message}")
    raise maintenance.unavailable(page, SITE_NAME, "the sign-in didn't finish within two minutes")


def _open_section(page, spec: dict) -> bool:
    """Click through the menu to the section: the shortest visible entry that
    matches, then again for a sub-entry, up to three levels."""
    match, skip = re.compile(spec["match"], re.I), re.compile(spec["skip"], re.I)
    clicked: set[str] = set()
    for _ in range(3):
        entries = page.locator("a:visible, .ui-menuitem-link:visible, [role=menuitem]:visible").filter(has_text=match)
        texts = [re.sub(r"\s+", " ", t).strip() for t in entries.all_inner_texts()]
        options = [(len(t), n) for n, t in enumerate(texts) if t and t not in clicked and not skip.search(t)]
        if not options:
            break
        _, pick = min(options)
        clicked.add(texts[pick])
        log.info("Portal: opening “%s”", texts[pick])
        entries.nth(pick).click(timeout=10000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        if clean_tables(page.evaluate(_TABLES_JS)):
            return True
    return bool(clicked)


def _goto_section(page, spec: dict) -> bool:
    """Open the section by its address; if that doesn't land on a real page
    (the address changed), fall back to clicking through the menu."""
    try:
        page.goto(SITE + spec["path"], wait_until="networkidle", timeout=45000)
    except Exception:
        log.exception("Portal: could not open %s", spec["path"])
    if "login.xhtml" not in page.url and spec["path"].rsplit("/", 2)[-2] in page.url:
        return True
    log.warning("Portal: %s did not open (landed on %s) — trying the menu", spec["path"], page.url)
    return _open_section(page, spec)


def _screenshot(page) -> bytes | None:
    try:
        height = page.evaluate("document.documentElement.scrollHeight")
        width = page.evaluate("document.documentElement.scrollWidth")
        return page.screenshot(type="png", full_page=True,
                               clip={"x": 0, "y": 0, "width": max(width, VIEWPORT["width"]),
                                     "height": min(height, MAX_SHOT_HEIGHT)})
    except Exception:
        log.exception("Portal screenshot failed")
        return None


def _keep_debug(page, section: str, png: bytes | None) -> None:
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / f"{section}.html").write_text(page.content(), encoding="utf-8")
        if png:
            (DEBUG_DIR / f"{section}.png").write_bytes(png)
    except Exception:
        log.exception("Could not keep the portal debug dump")


def _settle(page) -> None:
    """Wait until the page after sign-in has really loaded: the form going
    away only means the browser started navigating."""
    for _ in range(3):
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_selector("body", state="attached", timeout=10000)
            page.evaluate("document.body.innerText.length")
            return
        except Exception:
            page.wait_for_timeout(1500)


def _read(page, script: str, default):
    """page.evaluate that survives a navigation happening underneath it."""
    for _ in range(3):
        try:
            return page.evaluate(script)
        except Exception:
            _settle(page)
    return default


def _interpret(page, result: Result, text: str) -> None:
    """Turn the section's page into figures and English lines. A page that
    doesn't read as expected is left to the generic table dump."""
    try:
        if result.section == "transcript":
            semesters = parse_transcript(_read(page, _TRANSCRIPT_JS, []))
            if any(s["courses"] for s in semesters):
                result.figures = {"passed_hours": passed_hours(semesters)}
                latest = next((s["cumulative"] for s in semesters if s["cumulative"] is not None), None)
                if latest is not None:
                    result.figures["cgpa"] = latest
                # the record computed directly: every attempt counts (failures at 35),
                # a retaken course only once — this is what the portal's own figure is built from
                record_cgpa, record_hours = gradebook.cumulative(gradebook.attempts(semesters))
                if record_cgpa is not None:
                    result.figures.update(record_cgpa=round(record_cgpa, 2), record_hours=record_hours)
                # the base for this semester's targets: the record without the current term
                # (its grades are locked into the planner separately — never counted twice)
                base_cgpa, base_hours = gradebook.cumulative(gradebook.attempts(semesters[1:]))
                if base_cgpa is not None:
                    result.figures.update(base_cgpa=base_cgpa, base_hours=base_hours)
                # this term's retakes: {code: the old attempt as recorded}, for Grade Needed
                report = gradebook.season_report(semesters, {})
                result.data["retakes"] = {c["code"]: c["replaces"] for c in report["courses"]
                                          if c["replaces"] is not None}
                status = STATUS_RE.search(text or "")
                if status:
                    result.data["status"] = status.group(1)
                result.data.update(semesters=semesters)
                result.lines = transcript_lines(semesters)
        elif result.section == "balance":
            info = parse_balance(text, result.tables)
            if info["total"] is not None:
                result.figures = {"balance": info["total"]}
                result.lines = balance_lines(info)
        elif result.section == "grades":
            raw = _read(page, _RESULTS_JS, {})
            rows = parse_results(_read(page, _GRID_JS, []))
            result.data = {"results": rows, "term": english_term(raw.get("term", "")) if raw.get("term") else ""}
            result.lines = results_lines(raw, result.tables, rows)
    except Exception as e:
        log.exception("Portal: could not interpret the %s page", result.section)
        failsafe.bug(f"portal:parse:{result.section}", f"The portal's {result.section} page could not be read", e,
                     "Its answers show the plain table and the screenshot until the reader is fixed.")


def saved_session(now: float | None = None) -> dict | None:
    """The owner's last session (a browser storage state), if it was used within REUSE_SECONDS."""
    try:
        saved = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict) or (time.time() if now is None else now) - saved.get("at", 0) >= REUSE_SECONDS:
        return None
    return saved.get("state") if isinstance(saved.get("state"), dict) else None


def keep_session(context) -> None:
    """After a read: the session, and when it was last used."""
    try:
        fileio.write_json_atomic(SESSION_FILE, {"at": time.time(), "state": context.storage_state()})
    except Exception as e:                     # a session that is not kept only costs the next read a sign-in
        log.warning("Portal: could not keep the session (%s)", e)


def drop_session() -> None:
    SESSION_FILE.unlink(missing_ok=True)


def resume(page) -> bool:
    """The saved session's cookies are in the browser: open the student home page. True when it is
    still signed in; an ended session gets the sign-in form back."""
    try:
        page.goto(SITE + HOME_PATH, wait_until="networkidle", timeout=30000)
    except Exception as e:                     # not reachable now: the sign-in will say why
        log.info("Portal: the saved session could not be opened (%s)", type(e).__name__)
        return False
    return "/pages/student/" in page.url and _signed_in(page) and page.locator("#lognForm").count() == 0


def fetch(section: str, ask_code) -> Result:
    """Sign in, open one section and read it."""
    return fetch_many([section], ask_code)[section]


def fetch_many(sections: list[str], ask_code, creds: tuple[str, str] | None = None,
               keep_debug: bool = True) -> dict[str, Result]:
    """Sign in once and read several sections. Raises PortalError for anything
    the user can act on (missing credentials, refused sign-in, no code).
    creds: someone else's (username, password) instead of .env's — the guest look-up;
    keep_debug=False leaves portal_debug/ (the owner's pages) untouched."""
    from playwright.sync_api import sync_playwright

    own = creds is None                     # the owner's request: their session may be reused and is kept
    creds = creds or credentials()
    dump = _keep_debug if keep_debug else (lambda *args: None)
    if not creds:
        raise PortalError(f"Add {ENV_USERNAME} and {ENV_PASSWORD} to your .env file first.")
    results: dict[str, Result] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page, section, signed_in = None, "", False
        try:
            state = saved_session() if own else None
            context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE, storage_state=state)
            page = context.new_page()
            resumed = bool(state) and resume(page)
            if state and not resumed:
                log.info("Portal: the saved session has ended — signing in")
            if not resumed:
                try:
                    login(page, *creds, ask_code)
                except (PortalError, maintenance.PortalUnavailable):
                    raise
                except Exception as e:          # the form never appeared, the page didn't load, ...
                    raise maintenance.unavailable(page, SITE_NAME, e) from e
            signed_in = True
            _settle(page)
            log.info("Portal: %s (%s), reading %s", "session reused" if resumed else "signed in",
                     page.url.split("?")[0], ", ".join(sections))
            dump(page, f"{sections[0]}-landing", None)
            for section in sections:
                spec = SECTIONS[section]
                found = _goto_section(page, spec)
                _settle(page)
                result = Result(section=section, found=found)
                result.tables = clean_tables(_read(page, _TABLES_JS, [])) if found else []
                text = _read(page, "document.body.innerText", "")
                result.figures = find_figures(text)
                if found:
                    _interpret(page, result, text)
                result.png = _screenshot(page)
                if not found:
                    result.menu = _read(page, _MENU_JS, [])[:40]
                dump(page, section, result.png)
                results[section] = result
            if own:
                keep_session(context)
            return results
        except Exception:
            if own:
                drop_session()                  # whatever went wrong, the next read starts clean
            if page is not None and signed_in:
                dump(page, f"{section or sections[0]}-error", _screenshot(page))   # whatever was on screen
            raise
        finally:
            browser.close()
