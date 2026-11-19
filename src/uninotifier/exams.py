"""
The exams site (exams.zuj.edu.jo) — a Moodle of its own where the midterm exams are held and their marks appear.
Same sign-in as the registration portal (the university number and its password, PORTAL_USERNAME / PORTAL_PASSWORD),
no verification code. It is read over plain HTTP: sign in, the grades overview (one line per course), and — only
for a course whose grade changed — that course's grade report (each exam with its mark and range).

Nothing here sends anything: exams_watch.py decides what is news, bot/exams_flow.py tells you.
"""

import logging
import re

import requests
from lxml import html as lxml_html

from uninotifier import media

log = logging.getLogger("uninotifier")

BASE = "https://exams.zuj.edu.jo"
LOGIN_URL = f"{BASE}/login/index.php"
OVERVIEW_URL = f"{BASE}/grade/report/overview/index.php"
TIMEOUT = 30
NO_GRADE = "-"


class ExamsError(Exception):
    """The exams site could not be read (down, refused the sign-in, changed shape)."""


def _text(element) -> str:
    return " ".join(element.text_content().split()) if element is not None else ""


def clean_course(raw: str) -> str:
    """'Exams-20261-Communication Skills in Arabic-Sec12-3-E' -> 'Communication Skills in Arabic'."""
    name = re.sub(r"^\s*exams?[-\s]+", "", raw or "", flags=re.I)
    return media.clean_course_name(name) or name.strip()


def parse_overview(page_html: str) -> list[dict]:
    """The grades overview: [{"course", "raw", "grade" ('-' when none yet), "url"}], in the site's order."""
    tree = lxml_html.fromstring(page_html or "<html></html>")
    found = []
    for row in tree.xpath("//table//tr[td]"):
        cells = row.xpath("./td|./th")
        link = row.xpath(".//a/@href")
        if len(cells) < 2 or not link or not _text(cells[0]):
            continue
        found.append({"course": clean_course(_text(cells[0])), "raw": _text(cells[0]),
                      "grade": _text(cells[1]) or NO_GRADE, "url": link[0]})
    return found


def number(text: str) -> float | None:
    """'24.00' -> 24.0; '-' / '' / words -> None."""
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*%?\s*", text or "")
    return float(match.group(1)) if match else None


def parse_course(page_html: str) -> dict:
    """One course's grade report: {"items": [{"name", "grade", "max", "percent", "url"}], "total": {...} | None}.
    An item's grade is '-' until it is marked."""
    tree = lxml_html.fromstring(page_html or "<html></html>")
    exam_rows, total = [], None
    for row in tree.xpath("//table[contains(@class,'user-grade')]//tr"):
        title = row.xpath(".//*[contains(@class,'rowtitle')]")
        grade_cell = row.xpath("./td[contains(@class,'column-grade')]")
        if not grade_cell:
            continue
        top = grade_cell[0].xpath("./div/div[1]")
        grade = _text(top[0]) if top else _text(grade_cell[0])
        span = _text((row.xpath("./td[contains(@class,'column-range')]") or [None])[0])
        top_mark = re.split(r"[–-]", span)[-1].strip() if span else ""
        entry = {"grade": grade or NO_GRADE, "max": number(top_mark),
                 "percent": _text((row.xpath("./td[contains(@class,'column-percentage')]") or [None])[0])}
        if "lastrow" in (row.get("class") or ""):             # the course total closes the table: not an exam
            total = entry
        elif title:
            link = title[0].xpath(".//a/@href")
            exam_rows.append({"name": _text(title[0]), "url": link[0] if link else "", **entry})
    return {"items": exam_rows, "total": total}


def sign_in(session: requests.Session, username: str, password: str) -> None:
    """The site's own login form; raises ExamsError when it is not reachable or refuses the sign-in."""
    try:
        page = session.get(LOGIN_URL, timeout=TIMEOUT)
        tokens = lxml_html.fromstring(page.text).xpath("//input[@name='logintoken']/@value")
        reply = session.post(LOGIN_URL, timeout=TIMEOUT, data={
            "username": username, "password": password, "logintoken": tokens[0] if tokens else ""})
    except requests.RequestException as e:
        raise ExamsError(f"the exams site could not be reached ({type(e).__name__})") from e
    if "logout.php" not in reply.text:
        raise ExamsError("the exams site did not accept the sign-in")


def screenshots(username: str, password: str, url: str) -> list[bytes]:
    """The page as a picture (the grade table of a course): signed in in a headless browser, the main region of the page.
    Best effort — [] when it could not be taken; a notification then goes without it."""
    try:
        from playwright.sync_api import sync_playwright
        from uninotifier.monitor import config, items
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_context(viewport=config.VIEWPORT, device_scale_factor=config.SCALE).new_page()
                page.goto(LOGIN_URL, wait_until="networkidle", timeout=45000)
                page.locator("input[name=username]:visible").first.fill(username)
                page.locator("input[name=password]:visible").first.fill(password)
                page.locator("#loginbtn:visible, form:visible [type=submit]:visible").first.click()
                page.wait_for_load_state("networkidle", timeout=45000)
                if "/login/" in page.url:
                    return []
                shots, _ = items.capture_screens(page, url, "#region-main")
                return shots[:3]
            finally:
                browser.close()
    except Exception as e:                    # a picture is a bonus: never the reason a mark is not announced
        log.warning("Exams: no picture (%s)", type(e).__name__)
        return []


def read(username: str, password: str, known: dict | None = None) -> list[dict]:
    """Every course with its overview grade; the grade report (items) only of a course whose grade is not what
    `known` ({course: {"overview": ...}}) says — the rest are carried as the caller already has them.
    Returns [{"course", "raw", "grade", "url", "report": {...} | None}]."""
    known = known or {}
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    sign_in(session, username, password)
    try:
        courses = parse_overview(session.get(OVERVIEW_URL, timeout=TIMEOUT).text)
    except requests.RequestException as e:
        raise ExamsError(f"the grades could not be read ({type(e).__name__})") from e
    if not courses:
        raise ExamsError("the grades overview had no courses")
    for course in courses:
        course["report"] = None
        if (known.get(course["course"]) or {}).get("overview") == course["grade"]:
            continue
        try:
            course["report"] = parse_course(session.get(course["url"], timeout=TIMEOUT).text)
        except requests.RequestException as e:
            log.info("Exams: %s could not be read (%s)", course["course"], type(e).__name__)
            course["grade"] = (known.get(course["course"]) or {}).get("overview", NO_GRADE)   # looked at again next time
    return courses
