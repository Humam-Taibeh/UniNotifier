"""
Reading the portal: the browser (started only when needed), the plain-HTTP
scan of every course, submissions, teachers, forums, edits and lecture
links — retried after a network failure.
"""

from concurrent.futures import ThreadPoolExecutor
import logging
import time
import zlib

from playwright.sync_api import sync_playwright

from uninotifier import archive
from uninotifier import failsafe
from uninotifier import lecturelinks
from uninotifier import maintenance
from uninotifier import media
from uninotifier import moodle
from uninotifier import notifier
from uninotifier import watchdog
from uninotifier.httpctx import HttpContext
from uninotifier.monitor import alerts, config, items, notify, state
from uninotifier.zuj_auth import InvalidCredentialsError, LoginError, MFARequiredError, back_on_portal, install_virtual_authenticator, is_authenticated, login_via_sso

log = logging.getLogger("uninotifier")


def new_forum_posts(saved: dict, forum_posts: dict, notify_new: bool) -> dict:
    """forum item id -> posts not seen before. A forum read for the first
    time is baselined silently, unless the forum itself is brand new (then
    everything in it is news)."""
    fresh = {}
    for item_id, posts in forum_posts.items():
        if posts is None:
            continue
        seen = saved["forums"].get(item_id)
        if seen is None:
            if notify_new and item_id not in saved["content"] and posts:
                fresh[item_id] = posts
            continue
        unseen = [p for p in posts if p["id"] not in set(seen)]
        if unseen:
            fresh[item_id] = unseen
    return fresh


def probe_due(item_id: str, now: float, manual: bool = False) -> bool:
    """Is it this archived file's turn to be asked whether the lecturer changed it? Each file is
    due once per FILE_PROBE_SECONDS, on a run slot fixed by the file's id: the files are spread
    evenly over the runs, nothing is stored, and a missed run only delays a file to its next turn.
    A manual check (/check) asks about every file."""
    if manual:
        return True
    runs = max(1, round(config.FILE_PROBE_SECONDS / config.SCAN_SLOT_SECONDS))
    return (int(now // config.SCAN_SLOT_SECONDS) + zlib.crc32(item_id.encode("utf-8"))) % runs == 0


# ------------------------------------------------------------ main flow

class Browser:
    """Playwright, started only when something really needs it: signing in,
    a screenshot, the weekly teacher refresh or the occasional deep check.
    A routine check that finds nothing new never starts it."""

    def __init__(self):
        self._pw = self._browser = self._context = self._page = None

    @property
    def started(self) -> bool:
        return self._page is not None

    @property
    def page(self):
        if self._page is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                storage_state=str(state.SESSION_FILE) if state.SESSION_FILE.exists() else None,
                viewport=config.VIEWPORT, device_scale_factor=config.SCALE)
            self._page = self._context.new_page()
            install_virtual_authenticator(self._context, self._page)
        return self._page

    def sign_in(self, mfa_handler=None) -> None:
        """Raises MFARequiredError / InvalidCredentialsError as they are and any
        other failure as maintenance.PortalUnavailable, with a screenshot. A
        network error stays one (it is retried) unless the page shows
        maintenance. (Moodle's own maintenance page sits at /my/ and passes
        for signed in; run_scan catches it when the session is still refused.
        The dashboard itself is never scanned for the words: a course post
        about lab maintenance must not stop a healthy check.)"""
        page = self.page
        try:
            if not is_authenticated(page):
                login_via_sso(page, config.USERNAME, config.PASSWORD, mfa_handler=mfa_handler)
        except (MFARequiredError, InvalidCredentialsError):
            raise
        except Exception as e:
            # a page that didn't load is the network; a form that never appeared is not
            network = alerts.failure_kind(e) == "network" and "waiting for locator" not in str(e)
            if network and not maintenance.detect(maintenance.page_text(page)):
                raise
            raise maintenance.unavailable(page, config.SITE_NAME, e, signed_in=back_on_portal(page)) from e
        self.save_state()

    def cookies(self) -> list[dict]:
        self.page
        return self._context.cookies()

    def save_state(self) -> None:
        if self.started:
            self._context.storage_state(path=str(state.SESSION_FILE))

    def close(self) -> None:
        for closer in (lambda: self._browser.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass  # never started, or already gone


def pmap(fn, items: list) -> list[tuple]:
    """fn over items on a few threads: [(item, result, error)] in order. Used
    for the plain-HTTP reads only — the browser is never shared across threads."""
    def call(item):
        try:
            return item, fn(item), None
        except Exception as e:
            return item, None, e
    with ThreadPoolExecutor(config.HTTP_WORKERS) as pool:
        return list(pool.map(call, items))


def check_submissions(http, content: dict, deadlines: dict, saved: dict) -> dict:
    """Read the submission status of every upcoming (or brand-new) assignment.
    A submitted assignment is flagged on its deadline, which stops its 24h / 1h
    reminders; a pending one keeps them. An unreadable page keeps what was
    known before."""
    now = time.time()
    wanted = []
    for item_id, item in content.items():
        if item["type"] != "assign":
            continue
        key = f'assign-{moodle.extract_cmid(item["url"])}'
        upcoming = key in deadlines and deadlines[key]["timestamp"] > now
        if upcoming or item_id not in saved["content"]:
            wanted.append((key, item))
    submissions = {}
    for (key, item), status, error in pmap(lambda w: moodle.fetch_submission_status(http, w[1]["url"]), wanted):
        if isinstance(error, moodle.SessionExpiredError):
            raise error
        if error or status is None:
            continue
        submissions[key] = status
        if key in deadlines:
            deadlines[key]["submitted"] = status
        log.info("  assignment %s: %s", item["name"], "submitted" if status else "pending")
    return submissions


def refresh_teachers(browser: Browser, courses: dict, saved: dict) -> None:
    """Teacher names change rarely: re-read them at most once a week (the only
    routine use of the browser). saved['teachers'] =
    {'fetched': ts, 'by_course': {course_id: [...]}}."""
    teachers = saved["teachers"]
    if time.time() - teachers.get("fetched", 0) < config.TEACHERS_REFRESH_SECONDS and teachers.get("by_course"):
        return
    by_course = dict(teachers.get("by_course", {}))
    for course_id in courses:
        found = moodle.fetch_teachers(browser.page, course_id)
        if found is not None:
            by_course[course_id] = found
    saved["teachers"] = {"fetched": time.time(), "by_course": by_course}


def edited_items(saved: dict, content: dict) -> list[str]:
    """Items whose text (label, page, description, section summary) changed
    since the last run. Items saved before fingerprints existed are
    baselined silently."""
    return [iid for iid, item in content.items()
            if item.get("hash") and saved["content"].get(iid, {}).get("hash")
            and item["hash"] != saved["content"][iid]["hash"]]


def run_scan(saved: dict, notify_new: bool, mfa_handler=None) -> dict:
    """Read the portal. Almost everything is plain HTTP with the saved session
    cookies (a few seconds); the browser starts only for signing in, screenshots,
    the weekly teacher refresh and the deep check every DEEP_CHECK_SECONDS.
    Returns a dict:
      courses, content, deadlines
      artifacts    item id -> processing result for every item that needed a
                   download or a notification this run
      updates      item id -> the freshly downloaded modified file version
      edits        item id -> screenshots of an item whose text was edited
      posts        forum item id -> new forum posts (attachments downloaded)
      forum_posts  forum item id -> every post id read this run (None = unreadable)
      submissions  'assign-<cmid>' -> True/False for assignments read this run
      link_changes meeting links first seen this run (their screenshots are saved)
      seconds, browser_used"""
    started = time.monotonic()
    browser = Browser()
    try:
        http = HttpContext.from_state_file(state.SESSION_FILE)
        sesskey = http.sesskey() if http else None
        if not sesskey:
            log.info("The saved session is not valid — signing in")
            browser.sign_in(mfa_handler)
            http = HttpContext.from_cookie_list(browser.cookies())
            sesskey = http.sesskey()
            if not sesskey:
                raise maintenance.unavailable(browser.page, config.SITE_NAME,
                                              "signed in, but the portal did not accept the session")

        courses = moodle.fetch_courses(http, sesskey)
        deadlines = moodle.update_deadlines(
            saved["deadlines"], moodle.fetch_deadline_events(http, sesskey))
        for course in courses.values():
            archive.ensure_course_tree(course["name"])

        if not courses and saved["courses"]:
            raise watchdog.LayoutChanged("the list of your enrolled courses came back empty")
        content, failed_courses = {}, []
        scans = pmap(lambda c: moodle.scan_course(http, sesskey, c[0], c[1]), list(courses.items()))
        for (course_id, course), found, error in scans:
            if isinstance(error, moodle.SessionExpiredError):
                raise error
            if error is not None:
                log.error("Course %s: %r — reading its page in the browser instead", course["name"], error)
                try:
                    found = moodle.scan_course_in_browser(browser.page, course_id, course)
                except Exception:
                    failed_courses.append(course_id)
                    log.exception("Failed scanning course %s — skipping it this run", course["name"])
                    continue
            content.update(found)
        # A course that reads far shorter than before is treated as unreadable this run
        # (watchdog.py): the page layout may have changed, and its known items must not vanish.
        gone, anomalies = watchdog.check_courses(courses, content, saved["content"])
        protect, accepted = watchdog.review(saved["layout"], gone, anomalies, courses, time.time())
        for cid in protect:
            log.warning("Course %s read far shorter than before — keeping its known items", courses[cid]["name"])
            failed_courses.append(cid)
            content = {iid: it for iid, it in content.items() if watchdog.course_id(iid) != cid}
        # A course that failed to scan keeps its previously known items, so
        # they aren't mistaken for new ones next time.
        for item_id, item in saved["content"].items():
            if item_id.split("-", 1)[0] in failed_courses:
                content.setdefault(item_id, item)

        if time.time() - saved["deep"].get("at", 0) > config.DEEP_CHECK_SECONDS:
            log.info("Deep check: confirming assignments and quizzes through the activity indexes")
            for course_id, course in courses.items():
                if course_id not in failed_courses:
                    moodle.deep_check(browser.page, course_id, course["name"], content)
            saved["deep"] = {"at": time.time()}

        submissions = check_submissions(http, content, deadlines, saved)
        try:
            refresh_teachers(browser, courses, saved)
        except Exception as e:
            log.exception("Teacher refresh failed")
            failsafe.bug("monitor:teachers", "Reading the instructors' names failed", e,
                         "It is tried again on every check (with the browser) until it works.")

        artifacts = {}
        for item_id, item in content.items():
            is_new = item_id not in saved["content"]
            needs_file = item["type"] in media.FILE_TYPES and item_id not in saved["files"]
            wants_shot = is_new and notify_new and not media.silent_container(item)
            if not (needs_file or wants_shot):
                continue
            log.info("  processing %s: %s", item["type"], item["name"])
            try:
                artifacts[item_id] = art = items.process_item(http, sesskey, item)
            except Exception as e:
                log.exception("Failed processing %s", item["name"])
                failsafe.bug("monitor:item", "A new item could not be processed", e,
                             f"“{item['name']}” is announced without its file or details.")
                artifacts[item_id] = art = {"kind": "unknown"}
            if wants_shot:
                # tick first, so the screenshot already shows "Done"
                items.auto_complete(http, sesskey, item, art)
                if items.needs_screenshot(art):
                    art["shots"], art["truncated"] = items.capture_screens(
                        browser.page, *moodle.screenshot_target(item))

        # A Teams meeting saved before its join link was read (or whose page
        # failed to load): read it again, quietly, so the lecture reminders get it.
        for item_id, item in content.items():
            if (item["type"] in media.MEETING_TYPES and item_id not in artifacts and item_id in saved["content"]
                    and not saved["content"][item_id].get("target")):
                artifacts[item_id] = items.process_item(http, sesskey, item)

        edits = {}
        for item_id in edited_items(saved, content):
            item = content[item_id]
            log.info("  content edited: %s", item["name"])
            shots, truncated = items.capture_screens(browser.page, *moodle.screenshot_target(item))
            edits[item_id] = {"shots": shots, "truncated": truncated}

        forum_posts = {}
        forums = [(iid, it) for iid, it in content.items() if it["type"] == "forum"]
        for (item_id, item), posts_read, error in pmap(
                lambda f: moodle.fetch_forum_posts(http, sesskey, f[1]["url"]), forums):
            if isinstance(error, moodle.SessionExpiredError):
                raise error
            if error is not None:
                log.error("Could not read forum %s: %r", item["name"], error)
            forum_posts[item_id] = None if error else posts_read
        anomalies += watchdog.check_forums(forum_posts)
        posts = new_forum_posts(saved, forum_posts, notify_new)
        for item_id, fresh in posts.items():
            log.info("  %d new post(s) in %s", len(fresh), content[item_id]["name"])
            for post in fresh:
                notify.download_attachments(http, post)

        updates = {}
        archived = [(iid, it) for iid, it in content.items()
                    if it["type"] == "resource" and saved["files"].get(iid) and iid not in artifacts]
        now = time.time()
        probes = [(iid, it) for iid, it in archived if probe_due(iid, now, manual=config.MANUAL_RUN)]
        log.info("File update check: %d of %d archived file(s) are due", len(probes), len(archived))
        shared = archive.shared_paths(saved["files"])   # one file, several items: never retired
        for (item_id, item), art, error in pmap(
                lambda f: items.check_file_update(http, f[1], saved["files"][f[0]],
                                                  archive.is_shared(saved["files"][f[0]], shared)), probes):
            if error is not None:
                log.error("Update check failed for %s: %r", item["name"], error)
            elif art:
                updates[item_id] = art

        link_changes = detect_lecture_links(browser, saved, content, artifacts, forum_posts)

        # Keep the cookie file current: the Telegram bot's "Mark as Done"
        # buttons reuse it without a browser.
        if browser.started:
            browser.save_state()
        else:
            http.save_cookies(state.SESSION_FILE)
        seconds = round(time.monotonic() - started, 1)
        log.info("Portal read in %.1f s (%s)", seconds, "browser used" if browser.started else "no browser")
        return {"courses": courses, "content": content, "deadlines": deadlines,
                "artifacts": artifacts, "updates": updates, "edits": edits,
                "posts": posts, "forum_posts": forum_posts, "submissions": submissions,
                "link_changes": link_changes, "seconds": seconds, "browser_used": browser.started,
                "anomalies": anomalies, "accepted": accepted}
    finally:
        browser.close()


def scan_with_retries(saved: dict, notify_new: bool, mfa_handler=None) -> dict:
    """run_scan, run again after a network failure (SCAN_RETRY_SECONDS) — a
    dropped connection or a slow handshake fails one attempt, not the check.
    Sign-in problems and other errors are not retried: repeating them changes
    nothing (and could repeat a Microsoft verification prompt)."""
    for attempt in range(1, len(config.SCAN_RETRY_SECONDS) + 2):
        try:
            result = run_scan(saved, notify_new, mfa_handler)
            state.SUMMARY["attempts"] = attempt
            return result
        except (LoginError, maintenance.PortalUnavailable):
            raise
        except Exception as e:
            if alerts.failure_kind(e) != "network" or attempt > len(config.SCAN_RETRY_SECONDS):
                raise
            wait = config.SCAN_RETRY_SECONDS[attempt - 1]
            log.warning("Check attempt %d/%d hit a network error (%s) — retrying in %d s",
                        attempt, len(config.SCAN_RETRY_SECONDS) + 1, notifier.redact(f"{type(e).__name__}: {e}")[:300],
                        wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def detect_lecture_links(browser: Browser, saved: dict, content: dict, artifacts: dict,
                         forum_posts: dict) -> list[dict]:
    """Record meeting links that appeared on the portal and keep a screenshot
    of the post or section that published each one. Never raises."""
    try:
        # URL activities keep their resolved destination, so a meeting link
        # behind one is recognised on every run, not only the first.
        for iid, item in content.items():
            target = artifacts.get(iid, {}).get("target") or saved["content"].get(iid, {}).get("target")
            if target:
                item["target"] = target
        changes = lecturelinks.update_from_portal(content, forum_posts)
    except Exception as e:
        log.exception("Lecture link detection failed")
        failsafe.bug("monitor:links", "Looking for online-lecture links failed", e,
                     "A new Teams/Zoom link may not reach the lecture alerts.")
        return []
    for change in changes:
        try:
            shots, _ = items.capture_screens(browser.page, *change["shot"])
            if shots:
                lecturelinks.save_shot(change["course"], shots[0])
        except Exception:
            log.exception("Could not keep the screenshot of the link source")
    return changes
