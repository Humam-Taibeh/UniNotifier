"""Reading Moodle: pages, the HTTP layer, sign-in and its second factor, the watchdogs."""

from support import (COURSE_PAGE, FAKE_MICROSOFT, _legacy_markdown_ok, monitor_failure_kind, _package,
                     _REAL_SYNC_PLAYWRIGHT, _resilience_checks)

import json
import logging
import tempfile
import time
from datetime import datetime
from pathlib import Path

from uninotifier import archive
from uninotifier import media
from uninotifier import moodle
from uninotifier import notifier


def test_richtext_and_course_page():
    from uninotifier import richtext
    page = moodle.parse_course_page(COURSE_PAGE)
    assert page["manual"] == {"19793": False, "20770": True, "21747": False}
    assert set(page["bodies"]) == {"19793", "21747"}
    assert list(page["summaries"]) == ["11"]   # empty / "." summaries are ignored
    desc = richtext.to_text(page["bodies"]["21747"])
    assert desc.startswith("*Overview*") and "*Grades*" in desc, desc
    assert "1. 30% activities\n2. 30% midterm\n3. 40% final" in desc, desc
    label = page["bodies"]["19793"]
    assert richtext.first_line(label) == "Arabic Communication"
    assert richtext.image_paths(label) == ["/pluginfile.php/1/mod_label/intro/banner.jpg"]
    assert "Lecture time :" in richtext.to_text(label)
    summary = richtext.to_text(page["summaries"]["11"])
    assert "[Teams](https://teams.microsoft.com/l/meetup-join/abc)" in summary, summary
    assert richtext.links(page["summaries"]["11"]) == ["https://teams.microsoft.com/l/meetup-join/abc"]
    # fingerprints: markup / cache-buster churn is not an edit, a text change is
    assert richtext.digest(label) == richtext.digest(label.replace("?time=5", "?time=9").replace("<p>", "<p class='x'>"))
    assert richtext.digest(label) != richtext.digest(label.replace("Office hours :", "Office hours : Sun 10-12"))
    assert not richtext.is_meaningful("<p>.</p>") and richtext.is_meaningful(label)
    assert richtext.to_text("<p>a_b *c*</p>") == "a\\_b \\*c\\*"


def test_display_names_and_completion_policy():
    item = {"name": ".", "type": "page", "section": "مقدمة المادة", "cmid": "1"}
    assert moodle._display_name(item, "") == "مقدمة المادة"
    assert moodle._display_name({**item, "type": "label", "name": "x ..."}, "<p>Office hours</p>") == "Office hours"
    assert moodle._display_name({**item, "name": "Week 1"}, "") == "Week 1"
    assert media.completion_rows({"type": "assign", "manual": True, "cmid": "5"}) == []
    assert media.completion_rows({"type": "forum", "manual": False, "cmid": "5"}) == []
    assert media.completion_rows({"type": "forum", "manual": True, "cmid": "5"}) == [
        [{"text": "✅ Mark as Done", "callback_data": "done:5:1"}]]
    assert media.completion_rows({"type": "page", "manual": True, "done": True, "cmid": "5"}) == [
        [{"text": "↩️ Undo Done", "callback_data": "done:5:0"}]]
    assert "quiz" in media.NEVER_DONE_TYPES and "assign" not in media.AUTO_DONE_TYPES
    msg = media.build_item_message({"name": "n", "type": "page", "course": "c", "url": "u",
                                    "manual": True, "done": True}, None)
    assert "Progress:* ✅ Marked as done" in msg


def test_forum_messages_and_state():
    forum = {"name": "Announcements", "course": "20261-Big Data-AllSec-3-", "type": "forum"}
    post = {"id": "9", "subject": "Midterm moved", "author": "Dr. Hani", "time": 2e9,
            "is_reply": False, "attachments": [("a.pdf", "u")], "html": "<p>Sunday</p>"}
    msg = media.build_forum_post_message(forum, post)
    assert "New Announcement" in msg and "Midterm moved" in msg and "Attachments:* 1" in msg
    assert "New Forum Reply" in media.build_forum_post_message(forum, {**post, "is_reply": True})
    assert "New Forum Topic" in media.build_forum_post_message({**forum, "name": "Q&A"}, post)
    monitor = _package("uninotifier.monitor")
    p = lambda i: {"id": str(i)}
    saved = {"forums": {"5-1": ["1", "2"]}, "content": {"5-1": {}, "5-2": {}}}
    fresh = monitor.new_forum_posts(saved, {"5-1": [p(1), p(2), p(3)], "5-2": [p(4)],
                                            "5-3": [p(5)], "5-4": None}, notify_new=True)
    # known forum -> only the unseen post; known-but-never-read forum -> baseline;
    # brand-new forum -> everything; unreadable -> nothing
    assert fresh == {"5-1": [p(3)], "5-3": [p(5)]}, fresh


def test_edit_detection():
    monitor = _package("uninotifier.monitor")
    saved = {"content": {"a": {"hash": "1"}, "b": {"hash": "2"}, "c": {}}}
    content = {"a": {"hash": "1"}, "b": {"hash": "3"}, "c": {"hash": "4"}, "d": {"hash": "5"}}
    assert monitor.edited_items(saved, content) == ["b"]
    assert monitor.public({"name": "x", "_body": "<p>", "hash": "1"}) == {"name": "x", "hash": "1"}


def test_mfa_flow():
    from uninotifier import mfa
    from uninotifier.zuj_auth import MFARequiredError
    sync_playwright = _REAL_SYNC_PLAYWRIGHT            # a local page, on purpose: the guard is for accidents

    sent = []
    with tempfile.TemporaryDirectory() as tmp:
        mfa.MFA_REQUEST, mfa.MFA_RESPONSE = Path(tmp) / "req.json", Path(tmp) / "resp.json"
        notifier_upload, notifier_call, notifier_send = notifier.upload, notifier.call_api, notifier.send_message
        notifier.upload = lambda token, method, data, *a: sent.append(("photo", data["caption"], data["reply_markup"])) or {"message_id": len(sent)}
        notifier.call_api = lambda token, method, data=None, timeout=20: sent.append((method, data)) or {"message_id": len(sent)}
        notifier.send_message = lambda token, chat, text, url=None, rows=None: sent.append(("text", text, rows)) or True
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_content(FAKE_MICROSOFT)

                # 1. number matching -> the number goes to Telegram, once
                page.evaluate("show('number')")
                handler = mfa.TelegramMFA("t", "c")
                handler.step(page)
                handler.step(page)
                photos = [s for s in sent if s[0] == "photo"]
                assert len(photos) == 1, sent
                assert "4️⃣7️⃣" in photos[0][1] and "(*47*)" in photos[0][1], photos
                assert mfa.SMS_CALLBACK in photos[0][2]
                assert page.is_checked("#idChkBx_SAOTCAS_TD"), "'don't ask again' should be ticked"
                assert mfa.read_request()["expects"] == "choice"

                # 2. user taps "Text me a code instead" -> another way -> SMS -> code screen
                assert mfa.answer_request(choice="sms")
                handler.step(page)                       # clicks "I can't use my Authenticator"
                assert mfa.detect(page) == "proofs"
                handler.step(page)                       # chooses OneWaySMS
                assert mfa.detect(page) == "code"
                handler.step(page)                       # asks for the code on Telegram
                assert any("Verification Code Needed" in s[1] and "XXXXXXX12" in s[1]
                           for s in sent if s[0] == "text"), sent
                assert mfa.read_request()["expects"] == "code"

                # 3. a wrong code is reported, the right one gets through
                mfa.answer_request(code="000000")
                handler.step(page)
                handler.step(page)
                assert any("rejected that code" in s[1] for s in sent if s[0] == "text"), sent
                mfa.answer_request(code="123456")
                handler.step(page)
                assert page.locator("#finished").count() == 1
                handler.succeeded()
                assert "Signed in" in sent[-1][1] and not mfa.MFA_REQUEST.exists()

                # 4. timeout: one fresh request, then give up (already notified)
                handler = mfa.TelegramMFA("t", "c")
                page.evaluate("show('timeout')")
                handler.step(page)
                assert mfa.detect(page) == "number" and "fresh request" in sent[-1][1]
                page.evaluate("show('timeout')")
                try:
                    handler.step(page)
                    raise AssertionError("second timeout should give up")
                except MFARequiredError as e:
                    assert e.notified and "No response" in sent[-1][1]

                # 5. denied, and 6. nobody answers before the deadline
                for screen, expected in (("denied", "denied"), ("number", "expired")):
                    handler = mfa.TelegramMFA("t", "c", wait_seconds=0 if screen == "number" else 300)
                    page.evaluate(f"show('{screen}')")
                    if screen == "number":
                        handler.started, handler.deadline = time.time(), time.monotonic() - 1
                    try:
                        handler.step(page)
                        raise AssertionError(f"{screen} should end the attempt")
                    except MFARequiredError as e:
                        assert e.notified and expected in sent[-1][1].lower(), sent[-1]
                        buttons = [b["callback_data"] for row in sent[-1][2] for b in row]
                        assert buttons == ["check"], sent[-1]   # "🔄 Try again now"
                browser.close()
        finally:
            notifier.upload, notifier.call_api, notifier.send_message = notifier_upload, notifier_call, notifier_send
    assert mfa.keycaps("05") == "0️⃣5️⃣"


def test_mfa_policy_and_bot_relay():
    bot = _package("uninotifier.bot")
    from uninotifier import mfa
    monitor = _package("uninotifier.monitor")
    saved = {"mfa": {}}
    monitor.MANUAL_RUN = False
    assert monitor.mfa_allowed(saved, datetime(2026, 10, 12, 14, 0))
    assert not monitor.mfa_allowed(saved, datetime(2026, 10, 12, 3, 0)), "quiet hours"
    saved["mfa"]["last_attempt"] = datetime(2026, 10, 12, 13, 30).timestamp()
    assert not monitor.mfa_allowed(saved, datetime(2026, 10, 12, 14, 0)), "hourly retry"
    assert monitor.mfa_allowed(saved, datetime(2026, 10, 12, 14, 31))
    monitor.MANUAL_RUN = True
    assert monitor.mfa_allowed(saved, datetime(2026, 10, 12, 3, 0)), "/check always may"
    monitor.MANUAL_RUN = False

    replies = []
    bot.send_view = lambda text, rows: replies.append(text)
    with tempfile.TemporaryDirectory() as tmp:
        mfa.MFA_REQUEST, mfa.MFA_RESPONSE = Path(tmp) / "req.json", Path(tmp) / "resp.json"
        assert not bot.handle_mfa_code("123456"), "no pending request: not a code"
        mfa._write_json(mfa.MFA_REQUEST, {"id": "r1", "created": time.time(), "expects": "code"})
        assert not bot.handle_mfa_code("hello")
        assert bot.handle_mfa_code("123 456")
        assert json.loads(mfa.MFA_RESPONSE.read_text()) == {"id": "r1", "code": "123456"}
        assert "Code received" in replies[-1]
        mfa._write_json(mfa.MFA_REQUEST, {"id": "r2", "created": time.time() - 3600, "expects": "code"})
        assert not bot.handle_mfa_code("654321"), "a stale request is ignored"


def test_http_layer():
    import http.server
    import threading

    from uninotifier import httpctx
    from uninotifier.httpctx import HttpContext

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status=200, body=b"", headers=None):
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            if self.path == "/my/":
                self._reply(body=b'<script>M.cfg = {"sesskey":"abc123XY"}</script>')
            elif self.path == "/redir":
                self._reply(302, headers={"Location": "/file.bin"})
            elif self.path == "/file.bin":
                self._reply(body=b"DATA", headers={"Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"})
            elif self.path == "/cookie":
                self._reply(body=self.headers.get("Cookie", "").encode())
            elif self.path == "/missing":
                self._reply(404, b"nope")
            else:
                self._reply(404)

        do_HEAD = do_GET

        def do_POST(self):
            size = int(self.headers.get("Content-Length", 0))
            payload = self.rfile.read(size)
            self._reply(body=json.dumps({"got": payload.decode(), "ctype": self.headers.get("Content-Type")}).encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        http_ctx = HttpContext.from_cookie_list([{"name": "MoodleSession", "value": "s1", "domain": "127.0.0.1", "path": "/"}])
        assert http_ctx.request is http_ctx
        assert http_ctx.get(f"{base}/cookie").text() == "MoodleSession=s1", "saved cookies are sent"

        resp = http_ctx.get(f"{base}/redir")                      # followed, like Playwright's default
        assert resp.ok and resp.body() == b"DATA" and resp.url.endswith("/file.bin")
        resp = http_ctx.get(f"{base}/redir", max_redirects=0)     # not followed: the Location is readable
        assert not resp.ok and resp.status == 302 and resp.headers.get("location") == "/file.bin"
        head = http_ctx.head(f"{base}/redir")
        assert head.ok and head.headers.get("content-length") == "4" and head.headers.get("last-modified")
        assert not http_ctx.get(f"{base}/missing").ok

        out = http_ctx.post(f"{base}/ajax", data='[{"a":1}]', headers={"Content-Type": "application/json"}).json()
        assert out == {"got": '[{"a":1}]', "ctype": "application/json"}

        old_base, httpctx.BASE_URL = httpctx.BASE_URL, base
        try:
            assert http_ctx.sesskey() == "abc123XY"
            assert HttpContext.from_cookie_list([]).sesskey() == "abc123XY"   # the fake portal needs no login
        finally:
            httpctx.BASE_URL = old_base

        # a failing request raises like Playwright's: the monitor's network-error rule recognises it
        # (after httpctx's own retries — sped up and kept out of monitor.log here)
        server.shutdown()
        server.server_close()
        backoff, httpctx.BACKOFF_SECONDS = httpctx.BACKOFF_SECONDS, 0
        logging.disable(logging.WARNING)
        try:
            http_ctx.get(f"{base}/my/", timeout=2000)
        except Exception as e:
            assert monitor_failure_kind(e) == "network", repr(e)
        else:
            raise AssertionError("expected a connection error")
        finally:
            httpctx.BACKOFF_SECONDS = backoff
            logging.disable(logging.INFO)
    finally:
        server.shutdown()

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "auth_state.json"
        state.write_text(json.dumps({"cookies": [
            {"name": "MoodleSession", "value": "old", "domain": "elearning.zuj.edu.jo", "path": "/"},
            {"name": "other", "value": "keep", "domain": "login.microsoftonline.com", "path": "/"}],
            "origins": [{"origin": "x"}]}), encoding="utf-8")
        ctx = HttpContext.from_state_file(state)
        assert HttpContext.from_state_file(Path(tmp) / "none.json") is None
        ctx._session().cookies.set("MoodleSession", "new", domain="elearning.zuj.edu.jo", path="/")
        ctx.save_cookies(state)
        saved = json.loads(state.read_text(encoding="utf-8"))
        assert saved["cookies"][0]["value"] == "new" and saved["cookies"][1]["value"] == "keep"
        assert saved["origins"] == [{"origin": "x"}], "the rest of the saved browser state is untouched"


def test_fast_scan_helpers():
    monitor = _package("uninotifier.monitor")

    done = monitor.pmap(lambda x: 10 // x, [5, 0, 2, 1])
    assert [(i, r) for i, r, e in done] == [(5, 2), (0, None), (2, 5), (1, 10)], "order is kept"
    assert isinstance(done[1][2], ZeroDivisionError) and done[0][2] is None, "an error is returned, not raised"

    class Fake:
        def __init__(self, url, ok=True):
            self.url, self.ok = url, ok

        def text(self):
            return PAGE

    class FakeContext:
        class request:
            @staticmethod
            def get(url, timeout=0):
                return Fake(url)

    PAGE = '<div class="submissionstatustable"><table><tr><td>Submission status</td><td class="submissionstatussubmitted">Submitted for grading</td></tr></table></div>'
    assert moodle.fetch_submission_status(FakeContext, "https://x/mod/assign/view.php?id=1") is True
    PAGE = '<div class="submissionsummarytable"><table><tr><td>Submission status</td><td>No attempt</td></tr></table></div>'
    assert moodle.fetch_submission_status(FakeContext, "https://x/mod/assign/view.php?id=1") is False
    PAGE = "<p>no status block here</p>"
    assert moodle.fetch_submission_status(FakeContext, "https://x/mod/assign/view.php?id=1") is None
    PAGE = "<p>login</p>"
    FakeContext.request.get = staticmethod(lambda url, timeout=0: Fake("https://x/login/index.php"))
    try:
        moodle.fetch_submission_status(FakeContext, "https://x/mod/assign/view.php?id=1")
    except moodle.SessionExpiredError:
        pass
    else:
        raise AssertionError("an expired session must surface")

    # check_submissions: only upcoming or brand-new assignments are read; results flag the deadline
    now = time.time()
    content = {"1-10": {"type": "assign", "url": "https://x/mod/assign/view.php?id=10", "name": "HW1"},
               "1-11": {"type": "assign", "url": "https://x/mod/assign/view.php?id=11", "name": "HW2 (old)"},
               "1-12": {"type": "assign", "url": "https://x/mod/assign/view.php?id=12", "name": "HW3 (new)"},
               "1-13": {"type": "page", "url": "https://x/mod/page/view.php?id=13", "name": "P"}}
    deadlines = {"assign-10": {"timestamp": now + 3600}, "assign-11": {"timestamp": now - 3600}}
    saved = {"content": {"1-10": {}, "1-11": {}}}
    read = []
    orig = moodle.fetch_submission_status
    moodle.fetch_submission_status = lambda ctx, url: read.append(url) or (None if url.endswith("12") else True)
    try:
        result = monitor.check_submissions(object(), content, deadlines, saved)
    finally:
        moodle.fetch_submission_status = orig
    assert sorted(read) == ["https://x/mod/assign/view.php?id=10", "https://x/mod/assign/view.php?id=12"]
    assert result == {"assign-10": True} and deadlines["assign-10"]["submitted"] is True
    assert "submitted" not in deadlines["assign-11"], "a past deadline is not re-read"

    # the browser is started lazily: building the object costs nothing
    browser = monitor.Browser()
    assert not browser.started
    browser.close()                                    # closing a never-started browser is harmless
    assert monitor.HTTP_WORKERS >= 2 and monitor.DEEP_CHECK_SECONDS >= 3600
    assert "deep" in monitor.STATE_KEYS


def test_resource_page_file_link():
    """A resource's intermediate page lists the theme's favicon through
    pluginfile.php before the real file; the file is the mod_resource one."""
    page = ('<link rel="shortcut icon" href="https://x.jo/pluginfile.php/2/theme_mb2nl/favicon/0/favicon.ico">'
            '<a href="https://x.jo/pluginfile.php/204704/mod_resource/content/4/The%20Bisection%20Method.pdf?forcedownload=1&amp;a=b">file</a>')
    assert archive._file_link(page) == "https://x.jo/pluginfile.php/204704/mod_resource/content/4/The%20Bisection%20Method.pdf?forcedownload=1&a=b"
    # no mod_resource link: still never the theme's icon
    other = ('<a href="https://x.jo/pluginfile.php/2/theme_mb2nl/favicon/0/favicon.ico">i</a>'
             '<a href="https://x.jo/pluginfile.php/9/mod_folder/content/0/a.pdf">f</a>')
    assert archive._file_link(other) == "https://x.jo/pluginfile.php/9/mod_folder/content/0/a.pdf"
    assert archive._file_link('<a href="https://x.jo/pluginfile.php/2/theme_x/favicon/0/favicon.ico">i</a>') is None
    assert archive._file_link("<p>nothing</p>") is None


def test_network_resilience():
    """A dropped connection is retried (per request, then per check); a stalled git
    push can't outlive its time limit; a run killed holding the lock doesn't block
    the next ones; a retried check still attaches the files it downloaded."""
    import subprocess
    import sys

    import requests

    from uninotifier import gitsync
    from uninotifier import httpctx
    monitor = _package("uninotifier.monitor")

    logging.disable(logging.WARNING)      # the simulated failures stay out of monitor.log
    try:
        _resilience_checks(subprocess, sys, requests, gitsync, httpctx, monitor)
    finally:
        logging.disable(logging.INFO)


def test_sign_in_needs_no_form_when_microsoft_still_remembers_you():
    """Moodle's own session can lapse while Microsoft's has not: the OIDC redirect then comes straight
    back to the dashboard and no e-mail form exists. That is signed in (nothing is filled — it used to
    wait 30 s for the field and fail every run); a real Microsoft page still gets the e-mail filled."""
    from uninotifier import zuj_auth

    class Page:
        def __init__(self, lands_on):
            self.url, self.lands_on, self.filled = "about:blank", lands_on, []

        def goto(self, url, **kwargs):
            self.url = self.lands_on

        def fill(self, selector, text):
            self.filled.append((selector, text))
            raise zuj_auth.LoginError("stop here: the form was reached")

        click = wait_for_load_state = wait_for_timeout = fill

    class Handler:
        done = 0

        def succeeded(self):
            self.done += 1

    for landed in ("https://elearning.zuj.edu.jo/my/", "https://elearning.zuj.edu.jo/"):
        page, handler = Page(landed), Handler()
        zuj_auth.login_via_sso(page, "me@std-zuj.edu.jo", "pw", mfa_handler=handler)
        assert page.filled == [] and handler.done == 1, (landed, page.filled, handler.done)
    for form in ("https://login.microsoftonline.com/common/oauth2/authorize",
                 "https://elearning.zuj.edu.jo/auth/oidc/?source=loginpage",
                 "https://elearning.zuj.edu.jo/login/index.php"):
        page = Page(form)
        try:
            zuj_auth.login_via_sso(page, "me@std-zuj.edu.jo", "pw")
            raise AssertionError(f"{form} needs the form")
        except zuj_auth.LoginError:
            pass
        assert page.filled == [('input[type="email"]', "me@std-zuj.edu.jo")], (form, page.filled)


def test_layout_watchdog():
    """A page that suddenly reads short (the site changed) keeps its known items and is
    reported once; ✅ when it reads normally again; a shortfall that lasts 3 days is
    accepted as the new normal; an empty course list fails the run without touching state."""
    monitor = _package("uninotifier.monitor")
    from uninotifier import watchdog

    course = "20261-Numerical Analysis(1)-AllSec-3-"

    def items(n, start=1, text=False):
        return {f"8802-{i}": {"name": f"Item {i}", "type": "resource", "course": course, "cmid": str(i),
                              "url": f"https://x/mod/resource/view.php?id={i}", **({"hash": "h"} if text else {})}
                for i in range(start, start + n)}
    courses = {"8802": {"name": course, "url": "https://x/c"}}

    gone, findings = watchdog.check_courses(courses, items(2), items(46))
    assert gone == ["8802"] and findings == ["Numerical Analysis (1): 2 item(s) read, 46 known"], findings
    assert watchdog.check_courses(courses, items(30), items(46)) == ([], []), "a normal change"
    assert watchdog.check_courses(courses, items(1), items(3)) == ([], []), "too small to judge"
    assert watchdog.check_courses(courses, items(5), items(5, text=True))[1] == [
        "Numerical Analysis (1): the course page's text could not be read"]
    assert watchdog.check_forums({"a": None, "b": None}) == ["none of the 2 forums could be read"]
    assert watchdog.check_forums({"a": None, "b": []}) == []
    assert _legacy_markdown_ok(watchdog.alert_text(findings))

    with tempfile.TemporaryDirectory() as tmp:
        keep = {k: getattr(monitor, k) for k in ("DATA_FILE", "USERNAME", "PASSWORD", "BOT_TOKEN", "CHAT_ID",
                                                 "run_scan", "notify_new_item", "send_schedule_messages",
                                                 "mfa_allowed", "notify_new_course", "send_telegram")}
        monitor.DATA_FILE = Path(tmp) / "saved_items.json"
        monitor.USERNAME = monitor.PASSWORD = monitor.BOT_TOKEN = monitor.CHAT_ID = "x"
        monitor.send_schedule_messages = lambda saved: None
        monitor.mfa_allowed = lambda saved: False
        monitor.notify_new_course = lambda c: True
        announced, said = [], []
        monitor.notify_new_item = lambda it, deadline, art, submitted=None: announced.append(it["name"]) or True
        monitor.send_telegram = lambda text, url=None: said.append(text) or True
        reading = {"courses": dict(courses), "content": items(2)}

        def fake_scan(saved, notify_new, mfa_handler=None):
            # the same steps run_scan takes, on a fixed reading
            content, failed = dict(reading["content"]), []
            gone, anomalies = watchdog.check_courses(reading["courses"], content, saved["content"])
            protect, accepted = watchdog.review(saved["layout"], gone, anomalies, reading["courses"], clock["now"])
            for cid in protect:
                failed.append(cid)
                content = {i: it for i, it in content.items() if watchdog.course_id(i) != cid}
            for iid, it in saved["content"].items():
                if watchdog.course_id(iid) in failed:
                    content.setdefault(iid, it)
            return {"courses": reading["courses"], "content": content, "deadlines": {}, "artifacts": {},
                    "updates": {}, "edits": {}, "posts": {}, "forum_posts": {}, "submissions": {},
                    "link_changes": [], "seconds": 1.0, "browser_used": False,
                    "anomalies": anomalies, "accepted": accepted}
        monitor.run_scan = fake_scan
        clock = {"now": time.time()}
        keep_time = monitor.time.time
        monitor.time.time = lambda: clock["now"]
        try:
            monitor.save_data({**monitor.load_saved_data(), "courses": dict(courses), "content": items(46)})

            def run():
                try:
                    monitor.run()
                except SystemExit as e:
                    return e.code
                return 0
            assert run() == 0
            saved = json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))
            assert len(saved["content"]) == 46 and announced == [], "known items kept, nothing announced"
            assert said[-1].startswith("🧩 *Moodle looks different*") and "2 item(s) read, 46 known" in said[-1]
            n = len(said)
            clock["now"] += 3600
            run()
            assert len(said) == n, "not repeated within 6 hours"
            clock["now"] += watchdog.REPEAT_SECONDS
            run()
            assert len(said) == n + 1, "repeated after 6 hours"

            reading["content"] = items(46)                     # the page reads normally again
            clock["now"] += 900
            run()
            assert said[-1] == watchdog.RECOVERED_TEXT and announced == []
            run()
            assert said[-1] == watchdog.RECOVERED_TEXT and said.count(watchdog.RECOVERED_TEXT) == 1

            reading["content"] = items(10)                     # the instructor really removed 36 items
            for _ in range(13):                                 # 3 days of checks, 6 h apart
                clock["now"] += 6 * 3600
                run()
            assert any(t.startswith("ℹ️ *Accepted as the new normal:* Numerical Analysis (1)") for t in said)
            assert len(json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))["content"]) == 10

            before = monitor.DATA_FILE.read_text(encoding="utf-8")
            reading["courses"] = {}

            def empty_scan(saved, notify_new, mfa_handler=None):
                if not reading["courses"] and saved["courses"]:
                    raise watchdog.LayoutChanged("the list of your enrolled courses came back empty")
            monitor.run_scan = empty_scan
            assert run() == 1
            after = json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))
            assert after["content"] == json.loads(before)["content"], "state untouched"
            assert after["failures"]["kind"] == "layout" and after["failures"]["count"] == 1, "alert after 2 runs"
        finally:
            for k, v in keep.items():
                setattr(monitor, k, v)
            monitor.time.time = keep_time


def test_recorded_real_pages():
    """Your real Moodle, replayed: the pages recorded by tools.record_pages are read
    again by today's code and must give exactly what they gave when recorded — every
    course, item, name, text fingerprint, forum post, deadline, Teams link and
    attendance session. Skipped when nothing has been recorded on this PC."""
    from uninotifier.tools import record_pages

    if not record_pages.MOODLE_FILE.exists():
        print("  (skipped: no recorded pages — run python -m uninotifier.tools.record_pages)")
        return
    recording = json.loads(record_pages.MOODLE_FILE.read_text(encoding="utf-8"))
    replay = record_pages.Replay(recording["calls"])
    got = record_pages.read_everything(replay, record_pages.PLACEHOLDER)
    expected = recording["expected"]
    assert got["courses"] == expected["courses"]
    for cid, items in expected["content"].items():
        assert got["content"][cid] == items, f"course {expected['courses'][cid]} reads differently"
    for key in ("deadlines", "forums", "meetings", "sessions"):
        assert got[key] == expected[key], key
    assert sum(len(c) for c in got["content"].values()) > 0


def test_the_moodle_probe():
    """The 2-minute look: one batched request (updates since the last look + the calendar). The
    first look is the baseline; a new change starts ONE check; our own Mark as done (completion)
    is no change; a moved deadline is; a refused key or session is read afresh, an expired session
    waits for the monitor; nothing starts while a check runs, nor more than once per MIN_TRIGGER_GAP."""
    import requests
    from uninotifier import probe

    def updates(*pairs):
        return {"data": {"instances": [{"contextlevel": "module", "id": cm, "updates": [{"name": area, "timeupdated": t}]}
                                       for cm, area, t in pairs], "warnings": []}}

    courses = probe.course_ids()
    assert len(courses) == 6, courses                         # from the fixture state
    quiet = [updates() for _ in courses]
    calendar = {"data": {"events": [{"id": 1, "timesort": 100, "name": "HW1"}]}}

    # reading one answer
    found, fp, refused = probe.signals([updates((5, "configuration", 9), (5, "completion", 9))] + quiet[1:] + [calendar], courses)
    assert found == {f"{courses[0]}:5:configuration:9"} and fp and not refused
    assert probe.signals(quiet[:3], courses)[2], "a short answer (Moodle stopped at an error) is refused"
    assert probe.signals(quiet + [{"error": True, "exception": {"errorcode": "invalidsesskey"}}], courses)[2]
    # one error object instead of the list (seen 2026-11-05: it crashed the look): refused, not a crash
    assert probe.signals({"error": "servicerequireslogin", "errorcode": "servicerequireslogin"}, courses) == (set(), None, True)
    assert probe.signals(["<html>", "502"], courses) == (set(), None, True)

    class FakeHttp:
        def __init__(self, key="k"):
            self.key = key

        def sesskey(self):
            return self.key

    answers, launched = [], []
    keep = (probe.HttpContext.from_state_file, moodle.ajax_batch, probe.launch_check, probe.MONITOR_LOCK)
    probe.HttpContext.from_state_file = staticmethod(lambda path: FakeHttp())
    moodle.ajax_batch = lambda http, key, calls: answers.pop(0)
    probe.launch_check = lambda: launched.append(1) or type("P", (), {"poll": lambda self: 0})()
    with tempfile.TemporaryDirectory() as tmp:
        probe.MONITOR_LOCK = Path(tmp) / "monitor.lock"
        try:
            p, t = probe.Probe(), 1_000_000.0
            answers[:] = [quiet + [calendar]]
            assert not probe.look(p, t) and p.since == t, "the first look is the baseline"
            answers[:] = [[updates((7, "contentfiles", 50))] + quiet[1:] + [calendar]]
            assert probe.look(p, t + 120), "a new file"
            answers[:] = [[updates((7, "contentfiles", 50))] + quiet[1:] + [calendar]]
            assert not probe.look(p, t + 240), "the same change (the overlap asks back) counts once"
            answers[:] = [[updates((7, "completion", 300))] + quiet[1:] + [calendar]]
            assert not probe.look(p, t + 360), "our own Mark as done is not a change"
            answers[:] = [quiet + [{"data": {"events": [{"id": 1, "timesort": 200, "name": "HW1"}]}}]]
            assert probe.look(p, t + 480), "a deadline moved"
            answers[:] = [quiet + [{"error": True}]]
            assert not probe.look(p, t + 600) and p.sesskey is None and p.http is None, "refused: read afresh"
            probe.HttpContext.from_state_file = staticmethod(lambda path: FakeHttp(None))
            assert not probe.look(p, t + 720) and p.wait_until == t + 720 + probe.SESSION_RETRY, \
                "an expired session waits for the monitor, never signs in"

            # tick: only when due, never while a check runs, one check per MIN_TRIGGER_GAP
            probe.HttpContext.from_state_file = staticmethod(lambda path: FakeHttp())
            q, t = probe.Probe(), 2_000_000.0
            answers[:] = [quiet + [calendar]]
            assert not probe.tick(q, t)                                   # baseline
            assert not probe.tick(q, t + 30) and not answers, "not due yet: no request at all"
            probe.MONITOR_LOCK.write_text("1")
            assert not probe.tick(q, t + probe.PROBE_SECONDS), "a check is running: no look"
            probe.MONITOR_LOCK.unlink()
            for n, at in enumerate((2, 3, 4), 1):
                answers[:] = [[updates((9, "configuration", n))] + quiet[1:] + [calendar]]
                probe.tick(q, t + at * probe.PROBE_SECONDS)
            assert launched == [1], "three changes 2 min apart: one check (MIN_TRIGGER_GAP)"
            answers[:] = [[updates((9, "configuration", 99))] + quiet[1:] + [calendar]]
            later = t + 2 * probe.PROBE_SECONDS + probe.MIN_TRIGGER_GAP + probe.PROBE_SECONDS   # due, and past the gap
            assert probe.tick(q, later) and launched == [1, 1]

            # no network: logged, nothing raised, the next look reads afresh
            def offline(*a):
                raise requests.ConnectionError("down")
            moodle.ajax_batch = offline
            assert not probe.tick(q, t + 100 * probe.PROBE_SECONDS) and q.http is None
        finally:
            probe.HttpContext.from_state_file, moodle.ajax_batch, probe.launch_check, probe.MONITOR_LOCK = keep
