"""
Everything that talks to the Moodle portal and returns plain data:
course list, course content tree, activity indexes, calendar deadlines.

Content discovery uses three complementary sources, merged by item id:
  1. core_courseformat_get_state — the complete section/activity tree of a
     course (every week, every module). This is the primary source;
     core_course_get_contents itself is not exposed on this instance's AJAX
     service, and get_state returns the same tree.
  2. The per-type activity indexes (/mod/assign/index.php, /mod/quiz/index.php)
     — a confirmation pass so no assignment or quiz can slip through.
  3. The rendered course page (DOM) — fallback if get_state fails.

The server-rendered course page (fetched over HTTP, no browser) adds what the
tree lacks: the HTML of labels ("Text and media area"), activity descriptions
and section summaries, plus which activities use manual completion.
Forum topics and replies come from each forum's discussion list and
mod_forum_get_discussion_posts.
"""

import json
import logging
import re

import requests
from lxml import html as lxml_html
from playwright.sync_api import TimeoutError as PWTimeoutError

from uninotifier import richtext
from uninotifier.zuj_auth import BASE_URL
from uninotifier import paths

log = logging.getLogger("uninotifier")

AJAX_URL = f"{BASE_URL}/lib/ajax/service.php"
SESSION_FILE = paths.DATA / "auth_state.json"


class SessionExpiredError(Exception):
    """The saved portal session no longer logs in."""

# Only these (modname, eventtype) pairs are tracked for due-date reminders:
# assignment due dates and quiz opening times.
DEADLINE_EVENT_TYPES = {
    "assign": "due",
    "quiz": "open",
}

CONTENT_ITEM_SELECTOR = 'li[data-for="cmitem"]'
INDEXED_ACTIVITY_TYPES = ("assign", "quiz")


def extract_cmid(url: str) -> str | None:
    if not url:
        return None
    match = re.search(r"[?&]id=(\d+)", url)
    return match.group(1) if match else None


def ajax_call(context, sesskey: str, methodname: str, args: dict) -> dict:
    payload = [{"index": 0, "methodname": methodname, "args": args}]
    resp = context.request.post(
        f"{AJAX_URL}?sesskey={sesskey}&info={methodname}",
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    return resp.json()[0]


def ajax_batch(context, sesskey: str, calls: list[tuple[str, dict]]) -> list[dict]:
    """Several AJAX calls in one round trip. Moodle stops at the first failing
    call, so the result may be shorter than `calls`."""
    if not calls:
        return []
    payload = [{"index": n, "methodname": m, "args": a} for n, (m, a) in enumerate(calls)]
    resp = context.request.post(
        f"{AJAX_URL}?sesskey={sesskey}&info={calls[0][0]}",
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    return resp.json()


def fetch_html(context, url: str) -> str | None:
    """A portal page's server-rendered HTML over plain HTTP (no rendering)."""
    resp = context.request.get(url, timeout=30000)
    if "/login/" in resp.url:
        raise SessionExpiredError(f"redirected to login while fetching {url}")
    return resp.text() if resp.ok else None


def get_sesskey(page) -> str:
    sesskey = page.evaluate("() => window.M && M.cfg && M.cfg.sesskey")
    if sesskey:
        return sesskey
    match = re.search(r'"sesskey":"([a-zA-Z0-9]+)"', page.content())
    if match:
        return match.group(1)
    raise RuntimeError("Could not extract sesskey from the dashboard page")


def fetch_courses(context, sesskey: str) -> dict:
    result = ajax_call(
        context, sesskey, "core_course_get_enrolled_courses_by_timeline_classification",
        {"classification": "all", "limit": 0, "offset": 0, "sort": "fullname"},
    )
    if result.get("error"):
        raise RuntimeError(f"Moodle AJAX error (course list): {result.get('exception')}")

    courses = {}
    for c in result["data"]["courses"]:
        courses[str(c["id"])] = {
            "name": c["fullname"],
            "url": c.get("viewurl") or f"{BASE_URL}/course/view.php?id={c['id']}",
        }
    log.info("Found %d enrolled course(s)", len(courses))
    return courses


def fetch_course_content(page, course_id: str, course_url: str, course_name: str) -> dict:
    """Scan one course page for every piece of content (any module type),
    not just assignments. Content is rendered server-side regardless of
    whether its section is collapsed, so no "expand all" click is needed —
    but names are read via a JS-side text extraction that strips Moodle's
    hidden accessibility suffix (e.g. "Assignment"), which is more robust
    than Playwright's innerText() (which would return "" for anything
    inside a collapsed/display:none section)."""
    content = {}
    try:
        page.goto(course_url, wait_until="networkidle", timeout=30000)
    except PWTimeoutError:
        log.warning("Timed out loading course page: %s", course_name)
        return content

    items = page.locator(CONTENT_ITEM_SELECTOR)
    seen_hrefs = set()
    count = items.count()
    for i in range(count):
        item = items.nth(i)
        try:
            class_attr = item.get_attribute("class") or ""
            match = re.search(r"modtype_(\w+)", class_attr)
            if not match:
                continue
            modname = match.group(1)

            link = item.locator('a[href*="/mod/"][href*="view.php"]').first
            if link.count() == 0:
                continue  # e.g. labels: inline content with no detail page
            href = link.get_attribute("href")
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            href = href.split("&", 1)[0]

            name = item.evaluate(
                """(el) => {
                    const nameEl = el.querySelector('.instancename');
                    if (!nameEl) return '';
                    const clone = nameEl.cloneNode(true);
                    clone.querySelectorAll('.accesshide').forEach(e => e.remove());
                    return clone.textContent.trim();
                }"""
            )
            name = re.sub(r"\s+", " ", name or "").strip() or "(بدون اسم)"

            cmid = extract_cmid(href) or str(item.get_attribute("data-id") or i)
            content[f"{course_id}-{cmid}"] = {
                "name": name,
                "type": modname,
                "course": course_name,
                "url": href,
                "cmid": cmid,
            }
        except Exception:
            log.exception("Skipping a malformed content item in %s", course_name)

    log.info("  - %s: %d content item(s)", course_name, len(content))
    return content


def fetch_deadline_events(context, sesskey: str, limit: int = 50) -> list:
    # Moodle's core_calendar_get_action_events_by_timesort caps limitnum at 50.
    try:
        result = ajax_call(
            context, sesskey, "core_calendar_get_action_events_by_timesort",
            {"timesortfrom": 0, "limitnum": limit},
        )
    except Exception:
        log.exception("Could not reach the calendar deadlines endpoint")
        return []

    if result.get("error"):
        log.warning("Calendar deadlines not available: %s", result.get("exception"))
        return []
    return result.get("data", {}).get("events", [])


def update_deadlines(saved_deadlines: dict, events: list) -> dict:
    """Merge freshly-fetched due dates into the existing deadline state,
    keeping each item's reminder flags so a reminder already sent is never
    repeated. Any event with an unexpected shape is skipped, not fatal."""
    deadlines = {k: dict(v) for k, v in saved_deadlines.items()}
    for ev in events:
        try:
            modname = ev.get("modulename")
            if DEADLINE_EVENT_TYPES.get(modname) != ev.get("eventtype"):
                continue
            timestamp = ev.get("timesort")
            if not timestamp:
                continue

            action = ev.get("action") or {}
            url = action.get("url") or ev.get("url") or ""
            cmid = extract_cmid(url)
            if not cmid:
                continue

            key = f"{modname}-{cmid}"
            course = ev.get("course") or {}
            existing = deadlines.get(key, {})
            deadlines[key] = {
                "name": ev.get("name") or existing.get("name") or "(بدون اسم)",
                "course": course.get("fullname") or existing.get("course") or "",
                "url": url or existing.get("url", ""),
                "type": modname,
                "timestamp": timestamp,
                "reminded_24h": existing.get("reminded_24h", False),
                "reminded_1h": existing.get("reminded_1h", False),
                "submitted": existing.get("submitted", False),
            }
        except Exception:
            log.exception("Skipping a malformed calendar event: %r", ev)
    return deadlines


def course_url(course_id: str) -> str:
    return f"{BASE_URL}/course/view.php?id={course_id}"


def screenshot_target(item: dict) -> tuple[str, str]:
    """(page url, CSS selector) of the part of the portal that shows an item:
    its card on the course page for labels, descriptions and URL activities
    (whose own page would redirect away), the main region of its own page
    otherwise."""
    course_page = course_url(item.get("course_id") or "")
    if item["type"] == "section":
        return course_page, f'li[data-id="{item["sectionid"]}"] [data-for="sectioninfo"]'
    if item["type"] in ("label", "url") or (item["type"] == "page" and not item.get("_own_body")):
        return course_page, f'#module-{item["cmid"]}'
    return item["url"], "#region-main"


def fetch_course_tree(context, sesskey: str, course_id: str, course_name: str) -> tuple[dict, dict]:
    """The full activity tree of one course (all sections/weeks) via
    core_courseformat_get_state. Returns (content, sections); labels have no
    detail page, so their link points at their anchor on the course page."""
    result = ajax_call(context, sesskey, "core_courseformat_get_state", {"courseid": int(course_id)})
    if result.get("error"):
        raise RuntimeError(f"course state unavailable: {result.get('exception')}")
    state = json.loads(result["data"])
    sections = {
        str(s["id"]): {"title": re.sub(r"\s+", " ", s.get("title", "")).strip(),
                       "url": s.get("sectionurl") or course_url(course_id)}
        for s in state.get("section", []) if s.get("visible", True)
    }

    content = {}
    for cm in state.get("cm", []):
        if not cm.get("uservisible", True):
            continue
        cmid = str(cm["id"])
        url = cm.get("url") or f"{course_url(course_id)}#module-{cmid}"
        content[f"{course_id}-{cmid}"] = {
            "name": re.sub(r"\s+", " ", cm.get("name", "")).strip(),
            "type": cm.get("module", ""),
            "course": course_name,
            "url": url.split("&", 1)[0],
            "section": sections.get(str(cm.get("sectionid")), {}).get("title", ""),
            "course_id": course_id,
            "cmid": cmid,
            "done": bool(cm.get("isoverallcomplete")) or cm.get("completionstate") in (1, 2),
        }
    log.info("  - %s: %d item(s) in the course tree", course_name, len(content))
    return content, sections


def _inner_html(el) -> str:
    return "".join(lxml_html.tostring(child, encoding="unicode") for child in el) or (el.text or "")


def parse_course_page(page_html: str) -> dict:
    """What only the rendered course page shows:
      bodies    cmid -> HTML of the label / activity description
      summaries sectionid -> HTML of the section summary
      manual    cmid -> done? for every activity with a manual completion toggle"""
    root = lxml_html.fromstring(page_html)
    bodies, summaries, manual = {}, {}, {}
    for li in root.xpath('//li[@data-for="cmitem"]'):
        boxes = li.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " activity-altcontent ")]')
        if boxes:
            bodies[str(li.get("data-id"))] = _inner_html(boxes[0])
    for sec in root.xpath('//li[@data-for="section"]'):
        info = sec.xpath('.//div[@data-for="sectioninfo"]')
        if not info:
            continue
        for availability in info[0].xpath('.//div[contains(@class, "section_availability")]'):
            availability.getparent().remove(availability)
        summary = _inner_html(info[0])
        if richtext.is_meaningful(summary):
            summaries[str(sec.get("data-id"))] = summary
    for btn in root.xpath('//button[@data-action="toggle-manual-completion"]'):
        manual[str(btn.get("data-cmid"))] = btn.get("data-toggletype") == "manual:undo"
    return {"bodies": bodies, "summaries": summaries, "manual": manual}


def fetch_page_body(context, url: str) -> str:
    """The main text of a Page module (its own content, not its description)."""
    page_html = fetch_html(context, url)
    if not page_html:
        return ""
    boxes = lxml_html.fromstring(page_html).xpath(
        '//div[@role="main"]//div[contains(concat(" ", normalize-space(@class), " "), " generalbox ")]')
    if not boxes:
        return ""
    for modified in boxes[0].xpath('.//div[contains(@class, "modified")]'):
        modified.getparent().remove(modified)
    return _inner_html(boxes[0])


def _display_name(item: dict, body: str) -> str:
    """Moodle allows titles like "." — fall back to something a person can
    recognise: the label's first line, or the section's title."""
    name = item["name"]
    if len(re.findall(r"\w", name)) >= 2 and item["type"] != "label":
        return name
    if item["type"] == "label":
        return richtext.first_line(body) or name or "Text & Media"
    return item.get("section") or name or "Untitled"


def enrich_with_page(context, course_id: str, course_name: str, content: dict, sections: dict) -> None:
    """Attach bodies, readable names, fingerprints and manual-completion flags
    to the tree items, and add one item per section that has a summary.
    The body HTML is kept under "_body" (not persisted); its fingerprint is
    persisted as "hash" so an edit can be detected on a later run."""
    page_html = fetch_html(context, course_url(course_id))
    if not page_html:
        # Without the page, bodies and fingerprints would look "removed";
        # failing makes the caller keep last run's items for this course.
        raise RuntimeError(f"course page {course_id} unavailable")
    page = parse_course_page(page_html)
    for item_id, item in list(content.items()):
        cmid = item.get("cmid")
        body = page["bodies"].get(cmid, "")
        if item["type"] == "page":
            try:
                own = fetch_page_body(context, item["url"])
            except SessionExpiredError:
                raise
            except Exception:  # one unreadable page must not hide the rest
                log.exception("Could not read page %s", item["url"])
                own = ""
            if richtext.is_meaningful(own):
                body = f"{body}<hr>{own}" if richtext.is_meaningful(body) else own
                item["_own_body"] = True
        if item["type"] == "label" and not richtext.is_meaningful(body):
            del content[item_id]  # an empty spacer label: nothing to report
            continue
        if cmid in page["manual"]:
            item["manual"] = True
            item["done"] = page["manual"][cmid]
        item["name"] = _display_name(item, body)
        if richtext.is_meaningful(body):
            item["_body"] = body
            item["hash"] = richtext.digest(body)
    for sid, summary in page["summaries"].items():
        sec = sections.get(sid)
        if not sec:
            continue
        content[f"{course_id}-section-{sid}"] = {
            "name": sec["title"] or "Section",
            "type": "section",
            "course": course_name,
            "url": sec["url"],
            "section": sec["title"],
            "course_id": course_id,
            "sectionid": sid,
            "_body": summary,
            "hash": richtext.digest(summary),
        }


def fetch_activity_index(page, course_id: str, course_name: str) -> dict:
    """Confirmation pass over /mod/<type>/index.php for assignments and
    quizzes. Moodle 5 redirects these to the course activity overview, which
    lists the activities as links."""
    found = {}
    for modname in INDEXED_ACTIVITY_TYPES:
        try:
            page.goto(f"{BASE_URL}/mod/{modname}/index.php?id={course_id}",
                      wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(800)
            links = page.locator(f'a[href*="/mod/{modname}/view.php"]')
            for i in range(links.count()):
                href = links.nth(i).get_attribute("href") or ""
                cmid = extract_cmid(href)
                if not cmid:
                    continue
                name = re.sub(r"\s+", " ", links.nth(i).inner_text() or "").strip()
                found.setdefault(f"{course_id}-{cmid}", {
                    "name": name or "Untitled",
                    "type": modname,
                    "course": course_name,
                    "url": href.split("&", 1)[0],
                    "section": "",
                    "cmid": cmid,
                })
        except Exception:
            log.exception("Activity index check failed (%s, %s)", modname, course_name)
    return found


def scan_course(context, sesskey: str, course_id: str, course: dict) -> dict:
    """One course over plain HTTP: its activity tree plus the text only the
    rendered course page carries. Raises when the tree can't be read — the
    caller then falls back to scan_course_in_browser."""
    name = course["name"]
    content, sections = fetch_course_tree(context, sesskey, course_id, name)
    enrich_with_page(context, course_id, name, content, sections)
    return content


def scan_course_in_browser(page, course_id: str, course: dict) -> dict:
    """Fallback when the tree is unavailable: read the course page's DOM."""
    return fetch_course_content(page, course_id, course["url"], course["name"])


def deep_check(page, course_id: str, course_name: str, content: dict) -> int:
    """Confirmation pass through the browser-rendered activity indexes; adds
    any assignment/quiz the tree missed to `content` and returns how many."""
    added = 0
    for item_id, item in fetch_activity_index(page, course_id, course_name).items():
        if item_id not in content:
            log.info("  + index check found an item the tree missed: %s", item["name"])
            content[item_id] = item
            added += 1
    return added


# ------------------------------------------------- submission status

SUBMITTED_RE = re.compile(r"submitted\s+for\s+grading|تم\s+التسليم\s+للتقييم", re.I)
STATUS_TABLE_SELECTOR = ".submissionstatustable, .submissionsummarytable"


def parse_submission_status(table_text: str, has_submitted_cell: bool = False) -> bool:
    """True when the submission-status block says the work was handed in.
    Only that block is inspected, so "Submitted" elsewhere on the page
    (a lecturer's feedback, a due-date note) can't trigger it."""
    return has_submitted_cell or bool(SUBMITTED_RE.search(table_text or ""))


def _with_class(tree, *classes: str) -> list:
    """Elements carrying any of the CSS classes."""
    return tree.xpath("//*[" + " or ".join(
        f"contains(concat(' ', normalize-space(@class), ' '), ' {c} ')" for c in classes) + "]")


def fetch_submission_status(context, assign_url: str) -> bool | None:
    """True = submitted for grading, False = still pending, None = the page
    could not be read (the caller keeps the previous knowledge). The status
    block is part of the server-rendered page, so no browser is needed."""
    try:
        html = fetch_html(context, assign_url)
        if not html:
            return None
        tree = lxml_html.fromstring(html)
        blocks = _with_class(tree, "submissionstatustable", "submissionsummarytable")
        if not blocks:
            return None
        text = " ".join(b.text_content() for b in blocks)
        return parse_submission_status(text, bool(_with_class(tree, "submissionstatussubmitted")))
    except SessionExpiredError:
        raise
    except Exception:
        log.exception("Submission status check failed for %s", assign_url)
        return None


# --------------------------------------------------------------- forums

DISCUSSION_RE = re.compile(r"discuss\.php\?d=(\d+)")
POSTS_PER_BATCH = 25


def _post(raw: dict, forum_url: str) -> dict:
    return {
        "id": str(raw["id"]),
        "discussion": str(raw.get("discussionid", "")),
        "subject": re.sub(r"\s+", " ", raw.get("subject") or "").strip(),
        "author": ((raw.get("author") or {}).get("fullname") or "").strip(),
        "time": raw.get("timecreated") or 0,
        "html": raw.get("message") or "",
        "is_reply": bool(raw.get("hasparent") or raw.get("parentid")),
        "url": ((raw.get("urls") or {}).get("view") or forum_url),
        "attachments": [(a.get("filename") or "attachment", a.get("url") or a.get("fileurl"))
                        for a in raw.get("attachments") or [] if a.get("url") or a.get("fileurl")],
    }


def fetch_forum_posts(context, sesskey: str, forum_url: str) -> list[dict] | None:
    """Every post (topic openers and replies) of one forum, oldest first.
    None when the forum couldn't be read at all; a discussion that fails on
    its own is skipped and simply retried on the next run."""
    page_html = fetch_html(context, forum_url)
    if page_html is None:
        return None
    ids = list(dict.fromkeys(DISCUSSION_RE.findall(page_html)))
    posts, pending = [], [int(d) for d in ids]
    while pending:
        batch, pending = pending[:POSTS_PER_BATCH], pending[POSTS_PER_BATCH:]
        calls = [("mod_forum_get_discussion_posts",
                  {"discussionid": d, "sortby": "created", "sortdirection": "ASC"}) for d in batch]
        results = ajax_batch(context, sesskey, calls)
        if not results:
            log.warning("Forum %s: empty response for its discussions", forum_url)
            break
        for n, result in enumerate(results):
            if result.get("error"):
                log.warning("Forum discussion %s unreadable: %s", batch[n], result.get("exception"))
                continue
            posts += [_post(p, forum_url) for p in (result.get("data") or {}).get("posts", [])]
        # Moodle stops a batch after its first failing call (which is the
        # last result): requeue the calls that never ran.
        pending = batch[len(results):] + pending
    return sorted(posts, key=lambda p: (p["time"], int(p["id"])))


# ----------------------------------------------------------- completion

COMPLETION_METHOD = "core_completion_update_activity_completion_status_manually"


def set_completion(context, sesskey: str, cmid: str, done: bool) -> bool:
    """Tick or untick an activity's manual "Mark as done" toggle."""
    try:
        result = ajax_call(context, sesskey, COMPLETION_METHOD, {"cmid": int(cmid), "completed": done})
    except Exception:
        log.exception("Completion update failed for cm %s", cmid)
        return False
    ok = not result.get("error") and (result.get("data") or {}).get("status") is True
    if not ok:
        log.warning("Completion update refused for cm %s: %s", cmid, result.get("exception"))
    return ok


def http_session() -> requests.Session:
    """A plain-HTTP session carrying the saved browser cookies — for quick
    portal calls from the Telegram bot, which has no browser."""
    session = requests.Session()
    state = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    for c in state.get("cookies", []):
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    return session


def http_set_completion(cmid: str, done: bool) -> bool:
    """set_completion without a browser. Raises SessionExpiredError when the
    saved session no longer logs in."""
    session = http_session()
    resp = session.get(f"{BASE_URL}/my/", timeout=30)
    match = re.search(r'"sesskey":"([a-zA-Z0-9]+)"', resp.text)
    if "/login/" in resp.url or not match:
        raise SessionExpiredError("the saved portal session has expired")
    payload = [{"index": 0, "methodname": COMPLETION_METHOD,
                "args": {"cmid": int(cmid), "completed": done}}]
    result = session.post(f"{AJAX_URL}?sesskey={match.group(1)}&info={COMPLETION_METHOD}",
                          json=payload, timeout=30).json()[0]
    return not result.get("error") and (result.get("data") or {}).get("status") is True


# ------------------------------------------------------------ teachers

_TEACHERS_JS = """() => [...document.querySelectorAll('table#participants tbody tr')]
    .map(tr => [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim()))
    .filter(c => c.length >= 4 && c[1] && /teacher/i.test(c[2]))
    .map(c => ({name: c[1], role: c[2], groups: c[3]}))"""


def fetch_teachers(page, course_id: str) -> list | None:
    """Teachers listed on a course's participants page, as
    [{'name', 'role', 'groups'}]. Courses that don't list their teachers give
    []; None means the page could not be read."""
    try:
        page.goto(f"{BASE_URL}/user/index.php?id={course_id}&perpage=5000",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("table#participants", timeout=30000)
        return page.evaluate(_TEACHERS_JS)
    except Exception:
        log.exception("Could not read the teachers of course %s", course_id)
        return None
