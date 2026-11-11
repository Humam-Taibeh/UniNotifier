"""Cambridge One: its assignments and its own sign-in."""

from support import (_fresh_failsafe, OUTBOX)

import json
import os
import tempfile
from datetime import date
from datetime import datetime
from pathlib import Path

import playwright.sync_api as _playwright_api
from uninotifier import failsafe
from uninotifier import notifier


def test_cambridge_one_assignments():
    """Cambridge One (temporary): the feed's address comes from the page's; the watch needs
    CAMBRIDGE_UNTIL and stops after it. The parser finds an assignment's fields under the usual
    names (or one level down), dates as ms / s / ISO, and falls back to a fingerprint id. The
    first feed is a baseline; a new assignment is a 🚨 alert with the screenshot and 🔗 Open in
    Cambridge One, seen only once it reached Telegram; a moved due date and a removal (missing
    twice) are told; the 24 h reminder goes once per due date and only for "not submitted". A
    refused session is renewed once in the browser, else the owner is told once to --login and
    nothing is retried until the session file changes; network blips stay quiet for an hour.
    Its session cookies and any JWT are masked in logs."""
    import requests
    from uninotifier import cambridge

    page = "https://www.cambridgeone.org/class/learner/org_test/class/c-1/assignments"
    assert cambridge.feed_url(page) == "https://www.cambridgeone.org/class/apigateway/org_test/class/c-1/learner/assignments"
    for bad in ("https://www.cambridgeone.org/dashboard/learner/dashboard", "https://example.invalid/x"):
        try:
            cambridge.feed_url(bad)
            raise AssertionError(bad)
        except ValueError:
            pass

    recorded = json.loads((Path(__file__).resolve().parent / "fixtures" / "cambridge_feed.json").read_text(encoding="utf-8"))
    assert cambridge.parse(recorded) == [] and cambridge.class_title(recorded).startswith("Teacher. B2")

    t = 1_800_000_000.0
    ms = lambda seconds: int(seconds * 1000)          # noqa: E731
    a1 = {"uuid": "a1", "title": "Unit 1 Writing_task", "due_date": ms(t + 3 * 86400), "status": "not_started"}
    a2 = {"assignment": {"name": "Unit 2 Reading"}, "endDate": "2027-01-10T21:00:00Z", "submitted": True}
    a3 = {"id": 7, "name": "Unit 3 Quiz", "state": "something new"}
    p1, p2, p3 = cambridge.parse({"assignmentsData": {"entities": [a1, a2, a3, "junk"]}})
    assert (p1["id"], p1["due"], p1["submitted"]) == ("a1", t + 3 * 86400, False)
    assert p2["title"] == "Unit 2 Reading" and p2["id"].startswith("fp-") and p2["submitted"] is True
    assert p2["due"] == datetime.fromisoformat("2027-01-10T21:00:00+00:00").timestamp()
    assert (p3["id"], p3["due"], p3["submitted"]) == ("7", None, None)
    assert cambridge.to_epoch(1_800_000_000) == 1_800_000_000 and cambridge.to_epoch("soon") is None
    assert "fields" not in p1 and p1["activities"] == [], "no raw field dump any more"

    # the real assignment (2026-10-21): its id, a short title, the activities, and the submission rule
    real = json.loads((Path(__file__).resolve().parent / "fixtures" / "cambridge_assignment.json").read_text(encoding="utf-8"))
    entity = real["assignmentsData"]["entities"][0]
    [a] = cambridge.parse(real)
    assert a["id"] == entity["assigned_path_id"] and a["title"] == "Assignment 1"
    assert a["full_title"] == "Assignment 1, Ross. B2. sec. 27 . 3:30-5:00"
    assert (a["start"], a["due"]) == (entity["startdate"] / 1000, entity["duedate"] / 1000)
    assert len(a["activities"]) == 15 and a["activities"][0]["name"] == "Vocabulary: Character adjectives 1"
    assert a["submitted"] is False, "not started: not submitted"
    text = cambridge.new_message(a, cambridge.class_title(real), 1_792_540_000)
    lines = text.splitlines()
    assert lines[:6] == ["🚨 *New Assignment — Cambridge One*", "📘 *Course:* English Communication Skills",
                         "🏫 *Class:* Ross. B2. sec. 27 . 3:30-5:00", "📌 *Title:* Assignment 1",
                         f"🗓️ *Opens:* {notifier.format_timestamp(a['start'])}",
                         f"🕐 *Due Date:* {notifier.format_timestamp(a['due'])}"], lines[:6]
    assert lines[6:8] == ["", "📋 *Activities (15):*"] and lines[8] == "• Vocabulary: Character adjectives 1"
    assert lines[22] == "• Writing: An article" and lines[-1].startswith("🕒 *Detected:*")
    for raw in ("rule_id", "sub-type", "tags", "assigned_path_id", "__analytics", "•  "):
        assert raw not in text, raw
    assert len(text) <= notifier.CAPTION_LIMIT, "fits the screenshot's caption: one message"

    def progressed(*done_flags):
        e = json.loads(json.dumps(entity))
        for item, (submitted, status) in zip(e["items"], done_flags):
            item["__analytics"].update(submittedOnce=submitted, status=status)
        return cambridge.parse({**real, "assignmentsData": {"entities": [e]}})[0]

    all_done = [(True, "completed")] * 15
    assert progressed(*all_done)["submitted"] is True, "every activity handed in"
    assert progressed(*all_done[:14], (True, "in_complete"))["submitted"] is True,         "handed in, then opened again (or waiting for the teacher's mark): Cambridge lists it as Completed"
    assert progressed(*all_done[:14], (False, "completed"))["submitted"] is False, "one never handed in"
    played = json.loads(json.dumps(entity))
    for item in played["items"]:
        item["__analytics"].update(submittedOnce=False, completedOnce=True)
    assert cambridge.parse({**real, "assignmentsData": {"entities": [played]}})[0]["submitted"] is True,         "an activity with nothing to submit counts once it is completed"
    half = progressed(*all_done[:5])
    assert "5 of 15 activities done — 10 left" in cambridge.reminder_message(half, "", half["due"] - 3600)
    # the fallback id ignores progress: starting an activity never makes it a new assignment
    no_id = {k: v for k, v in entity.items() if k != "assigned_path_id"}
    started = json.loads(json.dumps(no_id))
    started["items"][0]["__analytics"].update(status="in_progress", launchCount=3)
    assert cambridge.fingerprint(no_id) == cambridge.fingerprint(started)
    # recorded under a fingerprint before its id was known: kept under the id, announced once
    seen = {"fp-0123456789abcdef": {"title": a["full_title"], "due": a["due"], "missing": 0}}
    reminded = {"fp-0123456789abcdef": a["due"]}
    cambridge.adopt_legacy(seen, reminded, [a])
    assert list(seen) == [a["id"]] and list(reminded) == [a["id"]]

    def feed(*entities):
        return {"classDetails": {"title": "Teacher. B2"}, "assignmentsData": {"entities": list(entities)}}

    sent: list[tuple[str, str, object]] = []
    online = [True]

    def png_file(token, chat, png, name, caption="", url=None, rows=None):
        if online[0]:
            sent.append(("png", caption, rows))
        return online[0]

    def long_message(token, chat, text, url=None, rows=None):
        if online[0]:
            sent.append(("text", text, rows))
        return online[0]

    keep_files = (cambridge.SEEN_FILE, cambridge.FEED_FILE, cambridge.STATE_FILE)
    keep_calls = (notifier.send_png_file, notifier.send_long_message, cambridge.fetch, cambridge.visit)
    keep_env = {k: os.environ.get(k) for k in (cambridge.ENV_URL, cambridge.ENV_UNTIL)}
    shots = []
    with tempfile.TemporaryDirectory() as tmp:
        cambridge.SEEN_FILE, cambridge.FEED_FILE = Path(tmp) / "cambridge.json", Path(tmp) / "cambridge_feed.json"
        cambridge.STATE_FILE = Path(tmp) / "cambridge_state.json"
        notifier.send_png_file, notifier.send_long_message = png_file, long_message
        shoot = lambda: shots.append(1) or b"png"     # noqa: E731
        _fresh_failsafe()
        try:
            # baseline, then a new assignment due in 10 h (the alert says so: no reminder on top)
            assert cambridge.apply(feed(a1), t, page, shoot)["baseline"] and not sent and not shots
            a4 = {"uuid": "a4", "title": "Unit 4", "due_date": ms(t + 10 * 3600), "status": "in_progress"}
            counts = cambridge.apply(feed(a1, a4), t + 60, page, shoot)
            assert counts["new"] == 1 and counts["reminded"] == 0 and shots == [1], counts
            kind, caption, rows = sent.pop()
            assert kind == "png" and caption.startswith("🚨 *New Assignment — Cambridge One*") and "Unit 4" in caption
            assert "📘 *Course:* English Communication Skills" in caption and "uuid" not in caption
            assert rows == [[{"text": cambridge.BUTTON_TEXT, "url": page}]] and not sent

            # Telegram unreachable: not seen, so the next check sends it
            a5 = {"uuid": "a5", "title": "Unit 5", "due_date": ms(t + 5 * 86400)}
            online[0] = False
            assert cambridge.apply(feed(a1, a4, a5), t + 120, page, shoot)["new"] == 0
            online[0] = True
            assert cambridge.apply(feed(a1, a4, a5), t + 180, page, shoot)["new"] == 1 and len(sent) == 1
            sent.clear()
            assert cambridge.apply(feed(a1, a4, a5), t + 240, page, shoot) == {
                "assignments": 3, "new": 0, "moved": 0, "gone": 0, "reminded": 0, "baseline": False}

            # a1's due date moves to 20 h away: told, and (not submitted) the reminder follows — once
            a1_moved = {**a1, "due_date": ms(t + 20 * 3600)}
            counts = cambridge.apply(feed(a1_moved, a4, a5), t + 300, page, shoot)
            assert counts["moved"] == 1 and counts["reminded"] == 1, counts
            assert "Due Date Changed" in sent[0][1] and "↩️ *Was:*" in sent[0][1]
            assert sent[1][1].startswith("🚨 *Reminder — Cambridge One*") and "not submitted yet" in sent[1][1]
            assert sent[1][2] == [[{"text": cambridge.BUTTON_TEXT, "url": page}]]
            sent.clear()
            assert cambridge.apply(feed(a1_moved, a4, a5), t + 360, page, shoot)["reminded"] == 0 and not sent

            # submitted, or an unknown status: no reminder
            a6 = {"uuid": "a6", "title": "Unit 6", "due_date": ms(t + 30 * 3600), "status": "weird"}
            a7 = {"uuid": "a7", "title": "Unit 7", "due_date": ms(t + 30 * 3600), "status": "submitted"}
            cambridge.apply(feed(a1_moved, a4, a5, a6, a7), t + 400, page, shoot)
            sent.clear()
            assert cambridge.apply(feed(a1_moved, a4, a5, a6, a7), t + 8 * 3600, page, shoot)["reminded"] == 0

            # gone only once it is missing twice in a row (one odd answer is not a removal)
            assert cambridge.apply(feed(a1_moved, a5, a6, a7), t + 9 * 3600, page, shoot)["gone"] == 0
            assert cambridge.apply(feed(a1_moved, a5, a6, a7), t + 10 * 3600, page, shoot)["gone"] == 1
            assert "Assignment Removed" in sent[-1][1] and "Unit 4" in sent[-1][1]
            assert json.loads(cambridge.FEED_FILE.read_text(encoding="utf-8"))["entities"][0]["uuid"] == "a1"

            # an unreadable record: set aside, told, a new baseline
            cambridge.SEEN_FILE.write_text("{broken", encoding="utf-8")
            sent.clear()
            assert cambridge.apply(feed(a1_moved), t + 11 * 3600, page, shoot)["baseline"] and not sent
            assert list(Path(tmp).glob("cambridge.corrupt-*.json"))
            assert any("Cambridge One record was unreadable" in m for m in OUTBOX), OUTBOX

            # the thread's tick: settings, renewal, waiting for --login, quiet network blips
            os.environ[cambridge.ENV_URL] = page
            os.environ.pop(cambridge.ENV_UNTIL, None)
            assert not cambridge.active(), "no end date: not watching"
            os.environ[cambridge.ENV_UNTIL] = "2027-01-31"
            assert cambridge.active(date(2027, 1, 31)) and not cambridge.active(date(2027, 2, 1))
            w = cambridge.Watcher()
            assert cambridge.tick(w, t, date(2027, 2, 1)) is None and w.ended, "past CAMBRIDGE_UNTIL the thread ends"

            cambridge.STATE_FILE.write_text(json.dumps({"cookies": [
                {"name": "c1_sid", "value": "s1", "domain": ".cambridgeone.org", "path": "/"}]}), encoding="utf-8")
            answers, renewals = [], []

            def fake_fetch(http, url):
                assert http.headers["cambridgeone-app-version"] == "v2" and http.cookies.get("c1_sid") == "s1"
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer

            cambridge.fetch = fake_fetch
            cambridge.visit = lambda shot: renewals.append(shot) or (renewal_ok[0], None)
            renewal_ok = [True]
            today = date(2026, 11, 1)
            _fresh_failsafe()
            w = cambridge.Watcher()
            answers[:] = [cambridge.Expired("HTTP 401"), feed(a1_moved)]
            assert cambridge.tick(w, t, today) is not None and renewals == [False], "renewed once, then read"
            assert cambridge.tick(w, t + 60, today) is None and not answers, "not due yet: no request"

            renewal_ok[0] = False
            answers[:] = [cambridge.Expired("HTTP 403")]
            assert cambridge.tick(w, t + cambridge.CHECK_SECONDS, today) is None and w.blocked_at is not None
            failsafe.wait_idle()
            assert sum("sign in again" in m for m in OUTBOX) == 1, OUTBOX
            assert cambridge.tick(w, t + 2 * cambridge.CHECK_SECONDS, today) is None and len(renewals) == 2, \
                "waiting for --login: nothing is tried"
            cambridge.STATE_FILE.write_text(cambridge.STATE_FILE.read_text(encoding="utf-8") + " ", encoding="utf-8")
            os.utime(cambridge.STATE_FILE, (t, t))           # --login wrote the session again
            answers[:] = [feed(a1_moved)]
            assert cambridge.tick(w, t + 3 * cambridge.CHECK_SECONDS, today) is not None and w.blocked_at is None
            failsafe.wait_idle()
            assert any("signed in again" in m for m in OUTBOX), OUTBOX

            _fresh_failsafe()
            later = t + 4 * cambridge.CHECK_SECONDS
            for n in range(cambridge.FAILURES_BEFORE_ALERT):
                answers[:] = [requests.ConnectionError("down")]
                assert cambridge.tick(w, later + n * cambridge.RETRY_SECONDS, today) is None
                assert w.next_check == later + n * cambridge.RETRY_SECONDS + cambridge.RETRY_SECONDS
                failsafe.wait_idle()
                assert bool(OUTBOX) == (n == cambridge.FAILURES_BEFORE_ALERT - 1), (n, OUTBOX)
        finally:
            cambridge.SEEN_FILE, cambridge.FEED_FILE, cambridge.STATE_FILE = keep_files
            notifier.send_png_file, notifier.send_long_message, cambridge.fetch, cambridge.visit = keep_calls
            for key, value in keep_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlX3Rlc3Q"       # made up: {"sub": "test"}  gitleaks:allow
    clean = notifier.redact(f"Authorization: Bearer {jwt} Cookie: c1_sid=s3cr3tSID; "
                            "glt_3_AbC-def=GltValue123; gig_loginToken_3_X=TokVal99")
    for secret in (jwt, "s3cr3tSID", "GltValue123", "TokVal99"):
        assert secret not in clean, (secret, clean)
    assert "Bearer <redacted>" in clean and "c1_sid=<redacted>" in clean, clean


def test_cambridge_signs_in_by_itself():
    """Cambridge ends a session about 72 h after the sign-in and its silent renewal does not exist
    (the login token lives 6 h), so the hidden browser signs in with CAMBRIDGE_EMAIL / _PASSWORD
    when the feed refuses the session: a valid session types nothing; an ended one fills Gigya's
    form, presses Enter and saves the new session. Cambridge's own error text makes it
    CredentialsRejected (tried again after a day, not every 6 h: a lockout is worse than waiting);
    no form, or a check only a person can pass, is a plain NeedsLogin; an address that never
    answers is a network blip, not a sign-in alert. Without the two settings nothing is typed and
    the alert says how to stop signing in by hand. The password is in no message or alert."""
    import requests
    from playwright.sync_api import Error
    from uninotifier import cambridge

    email, secret = "student@example.invalid", "fake pass #1 {with} [symbols] & spaces"
    page_url = "https://www.cambridgeone.org/class/learner/org_test/class/c-1/assignments"
    login_url = "https://www.cambridgeone.org/login?returnUrl=%2Fclass"
    scenario: dict = {}
    pages: list = []

    class Locator:
        def __init__(self, page, selector):
            self.page, self.selector = page, selector

        def locator(self, selector):
            return Locator(self.page, f"{self.selector} {selector}")

        def wait_for(self, state=None, timeout=None):
            if scenario.get("no_form"):
                raise Error("Locator.wait_for: Timeout 15000ms exceeded.")

        def fill(self, value, timeout=None):
            self.page.typed.append((self.selector, value))

        def press(self, key, timeout=None):
            self.page.pressed.append(key)

        def all_inner_texts(self):
            return scenario.get("errors", [])

    class Page:
        def __init__(self):
            self.signed, self.typed, self.pressed, self.url = bool(scenario.get("valid")), [], [], "about:blank"

        def goto(self, url, **kwargs):
            self.url = page_url if self.signed else "chrome-error://chromewebdata/" if scenario.get("offline") else login_url

        def expect_response(self, predicate, timeout=None):
            page = self

            class Waiting:
                def __enter__(self):
                    return self

                def __exit__(self, kind, value, tb):
                    if kind is None and not page.signed:
                        raise Error("Page.expect_response: Timeout exceeded.")
                    return False
            return Waiting()

        def locator(self, selector):
            return Locator(self, selector)

        def wait_for_url(self, predicate, timeout=None, wait_until=None):
            assert self.pressed, "nothing was submitted yet"
            if not scenario.get("accepts"):
                raise Error("Page.wait_for_url: Timeout 45000ms exceeded.")
            self.signed, self.url = True, "https://www.cambridgeone.org/"
            assert predicate(self.url) and not predicate(login_url), "leaving /login is what counts"

    class Context:
        def new_page(self):
            pages.append(Page())
            return pages[-1]

        def storage_state(self):
            return {"cookies": [{"name": "c1_sid", "value": "fresh"}], "origins": []}

    class Browser:
        def new_context(self, **kwargs):
            return Context()

        def close(self):
            pass

    class Playwright:
        chromium = type("C", (), {"launch": staticmethod(lambda headless=True: Browser())})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def attempt(**kw):
        scenario.clear()
        scenario.update(kw)
        try:
            return cambridge.visit(False)
        except Exception as e:                       # the point is which one
            return e

    typed_in = lambda: pages[-1].typed               # noqa: E731
    form = "#gigya-login-form"
    keep_env = {k: os.environ.get(k) for k in (cambridge.ENV_URL, cambridge.ENV_UNTIL, cambridge.ENV_EMAIL,
                                               cambridge.ENV_PASSWORD)}
    keep = (_playwright_api.sync_playwright, cambridge.STATE_FILE, cambridge.fetch, cambridge.SEEN_FILE,
            cambridge.FEED_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        cambridge.STATE_FILE = Path(tmp) / "cambridge_state.json"
        cambridge.SEEN_FILE, cambridge.FEED_FILE = Path(tmp) / "cambridge.json", Path(tmp) / "cambridge_feed.json"
        _playwright_api.sync_playwright = lambda: Playwright()

        def old_session():
            cambridge.STATE_FILE.write_text(json.dumps({"cookies": [{"name": "c1_sid", "value": "old"}]}), encoding="utf-8")

        def saved():
            return json.loads(cambridge.STATE_FILE.read_text(encoding="utf-8"))["cookies"][0]["value"]

        try:
            os.environ[cambridge.ENV_URL] = page_url
            os.environ[cambridge.ENV_UNTIL] = "2027-01-31"
            os.environ[cambridge.ENV_EMAIL], os.environ[cambridge.ENV_PASSWORD] = email, secret
            assert cambridge.credentials() == (email, secret)
            for half in (cambridge.ENV_EMAIL, cambridge.ENV_PASSWORD):
                kept = os.environ.pop(half)
                assert cambridge.credentials() is None, f"{half} alone is not enough"
                os.environ[half] = kept

            old_session()
            assert attempt(valid=True) == (True, None) and typed_in() == [] and saved() == "fresh", "valid: nothing typed"

            old_session()
            assert attempt(accepts=True) == (True, None), "ended session: signs in by itself"
            assert typed_in() == [(f"{form} input[name=username]", email), (f"{form} input[name=password]", secret)]
            assert pages[-1].pressed == ["Enter"] and saved() == "fresh", "the new session is saved"

            old_session()
            rejected = attempt(errors=["", "Invalid login or password"])
            assert isinstance(rejected, cambridge.CredentialsRejected), rejected
            assert "Invalid login or password" in str(rejected) and saved() == "old", "a refused sign-in saves nothing"
            stuck = attempt()
            assert type(stuck) is cambridge.NeedsLogin and "check" in str(stuck), stuck
            missing = attempt(no_form=True)
            assert type(missing) is cambridge.NeedsLogin and "could not be filled in" in str(missing), missing
            for failure in (rejected, stuck, missing):
                assert secret not in str(failure) and email not in str(failure), failure
            offline = attempt(offline=True)
            assert isinstance(offline, requests.ConnectionError) and not typed_in(), "no answer is not a sign-in problem"
            assert cambridge.screenshot() is None, "an alert still goes out without its picture"

            # the thread: refused credentials wait a day, a missing form 6 h; the alert says what to do
            t, today = 1_800_000_000.0, date(2026, 11, 1)
            old_session()

            def expired(http, url):
                raise cambridge.Expired("HTTP 401")
            cambridge.fetch = expired
            _fresh_failsafe()
            w = cambridge.Watcher()
            scenario.clear()
            scenario.update(errors=["Invalid login or password"])
            assert cambridge.tick(w, t, today) is None and w.retry_gap == cambridge.CREDENTIALS_RETRY
            failsafe.wait_idle()
            [alert] = [m.replace("\\_", "_") for m in OUTBOX]          # Markdown escapes the underscores
            assert "sign in again" in alert and "automatic sign-in did not work" in alert, alert
            assert "Invalid login or password" in alert and "CAMBRIDGE_PASSWORD" in alert, alert
            assert secret not in alert and email not in alert, alert
            tried = len(pages)
            assert cambridge.tick(w, t + 2 * cambridge.RELOGIN_RETRY, today) is None and len(pages) == tried, \
                "refused credentials are not tried again after 6 h"
            assert cambridge.tick(w, t + cambridge.CREDENTIALS_RETRY + cambridge.CHECK_SECONDS, today) is None
            assert len(pages) == tried + 1, "... but after a day they are"
            old_session()
            os.utime(cambridge.STATE_FILE, (t, t))
            w2 = cambridge.Watcher()
            w2.blocked_at, w2.blocked_mtime, w2.retry_gap = t, t - 100, cambridge.CREDENTIALS_RETRY   # the file has changed since
            cambridge.fetch = lambda http, url: {"classDetails": {}, "assignmentsData": {"entities": []}}
            assert cambridge.tick(w2, t + 60, today) is not None and w2.blocked_at is None, "--login unblocks at once"

            cambridge.fetch = expired
            _fresh_failsafe()
            os.environ.pop(cambridge.ENV_EMAIL), os.environ.pop(cambridge.ENV_PASSWORD)
            w3 = cambridge.Watcher()
            scenario.clear()
            assert cambridge.tick(w3, t, today) is None and w3.retry_gap == cambridge.RELOGIN_RETRY
            assert typed_in() == [], "no settings: nothing is typed"
            failsafe.wait_idle()
            [alert] = [m.replace("\\_", "_") for m in OUTBOX]
            assert "--login" in alert and "CAMBRIDGE_EMAIL" in alert and "CAMBRIDGE_PASSWORD" in alert, alert
        finally:
            (_playwright_api.sync_playwright, cambridge.STATE_FILE, cambridge.fetch, cambridge.SEEN_FILE,
             cambridge.FEED_FILE) = keep
            for key, value in keep_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
