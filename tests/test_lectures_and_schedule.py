"""The timetable: lectures, links, the portal's class table, rooms, attendance check-in."""

from support import (ATTENDANCE_PAGE, _check_in_form, CHECK_IN_PAGE, _legacy_markdown_ok, _package, _portal_home,
                     PORTAL_ROWS, TEAMS_PAGE)

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from uninotifier import archive
from uninotifier import daily
from uninotifier import lecturelinks
from uninotifier import lectures
from uninotifier import media
from uninotifier import moodle
from uninotifier import notifier
from uninotifier import paths
from uninotifier import schedule


def test_schedule():
    sch = schedule.load_schedule()
    # Sunday 2026-10-11
    # in-person days: no lecture reminders
    assert schedule.due_messages(sch, datetime(2026, 10, 11, 7, 30)) == []
    assert schedule.due_messages(sch, datetime(2026, 10, 11, 8, 40)) == []
    assert schedule.due_messages(sch, datetime(2026, 10, 11, 9, 45)) == []
    # Thursday: no classes
    assert schedule.due_messages(sch, datetime(2026, 10, 15, 8, 0)) == []
    # Monday is an activity, not a lecture: no alert
    assert schedule.due_messages(sch, datetime(2026, 10, 12, 18, 15)) == []

    # Wednesday's online lecture (7:00 PM): a text-only heads-up at 6:00, the alert at 7:00
    def wed(h, m):
        return schedule.due_messages(sch, datetime(2026, 10, 14, h, m))
    assert wed(17, 59) == []
    (key, kind, text, course), = wed(18, 0)
    assert kind == "pre" and course == "Arabic Communication Skills" and "pre-19:00" in key
    assert text.startswith("⏰ *Reminder:* Arabic Communication Skills starts in 1 hour"), text
    assert "🕘 7:00–8:30 PM" in text and "Join" not in text
    assert "starts in 45 min" in wed(18, 15)[0][2] and "starts in 1 min" in wed(18, 59)[0][2]
    (key2, kind2, text2, course2), = wed(19, 0)
    assert kind2 == "start" and key2 != key and text2.startswith("🎥 *Lecture starting now:* Arabic Communication Skills")
    assert "Lecture in progress" in wed(19, 12)[0][2] and "started 12 min ago" in wed(19, 12)[0][2]
    assert wed(20, 30) == [] and wed(23, 0) == []
    assert wed(18, 15)[0][0] == wed(18, 44)[0][0], "one key per lecture and day: sent once"


def test_lecture_links():
    bot = _package("uninotifier.bot")
    from uninotifier import lecturelinks as ll
    monitor = _package("uninotifier.monitor")

    teams = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%7d"
    zoom = "https://us06web.zoom.us/j/8123456789?pwd=xyz"
    assert ll.meeting_urls([f"Join: {teams}.", zoom, "https://example.com/x"]) == [teams, zoom]
    assert ll.meeting_urls(["https://meet.google.com/abc-defg-hij"]) == ["https://meet.google.com/abc-defg-hij"]
    assert ll.meeting_urls(["https://elearning.zuj.edu.jo/mod/page/view.php?id=1"]) == []
    assert ll.online_courses() == ["Arabic Communication Skills"]
    assert ll.online_slots("Arabic Communication Skills") == "Wed 7:00 PM–8:30 PM"

    arabic = "20261-الاتصال والتواصل باللغة العربية-ش31-3-ك"
    with tempfile.TemporaryDirectory() as tmp:
        ll.LINKS_FILE = Path(tmp) / "links.json"
        label = {"name": "Instructor info", "course": arabic, "url": "u1", "type": "label", "cmid": "1", "course_id": "8805",
                 "_body": f'<p>Lecture link: <a href="{teams}">Teams</a></p>'}
        other = {"name": "Week 1", "course": "20261-Big Data-AllSec-3-", "url": "u2", "type": "label", "cmid": "2", "course_id": "8804",
                 "_body": f"<p>{zoom}</p>"}
        content = {"8805-1": label, "8804-2": other}

        changes = ll.update_from_portal(content, {})
        assert [(c["course"], c["url"]) for c in changes] == [("Arabic Communication Skills", teams)]
        assert "Instructor info" in changes[0]["origin"]
        assert changes[0]["portal_url"] == "u1"
        assert ll.get("Big Data") is None, "in-person courses are ignored"
        assert ll.update_from_portal(content, {}) == [], "an unchanged link is not re-announced"

        # /setlink wins over the old label link; a NEW portal link wins over /setlink
        ll.set_link("Arabic Communication Skills", "https://teams.live.com/meet/manual1", "manual")
        assert ll.update_from_portal(content, {}) == []
        assert ll.get("Arabic Communication Skills").endswith("manual1")
        new_link = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_NEW%40thread.v2/0"
        forum = {"name": "Announcements", "course": arabic, "url": "f", "type": "forum"}
        post = {"id": "88", "subject": "Link for this week", "time": 2e9, "url": "https://x/mod/forum/discuss.php?d=5#p88",
                "html": f'<p><a href="{new_link}">join</a></p>'}
        changes = ll.update_from_portal({**content, "8805-9": forum}, {"8805-9": [post]})
        assert changes[0]["url"] == new_link and "Link for this week" in changes[0]["origin"]
        assert changes[0]["shot"] == ("https://x/mod/forum/discuss.php?d=5", "#p88")
        assert changes[0]["portal_url"].endswith("#p88")
        assert ll.get("Arabic Communication Skills") == new_link

        # a URL activity pointing at a meeting is recognised through its target
        url_item = {"name": "Online lecture", "course": arabic, "url": "u3", "target": zoom, "type": "page", "cmid": "3", "course_id": "8805"}
        assert ll.update_from_portal({"8805-3": url_item}, {})[0]["url"] == zoom

        # a portal link comes with where it was published; the screenshot is kept per link
        ll.save_shot("Arabic Communication Skills", b"PNG-fake")
        assert ll.shot_bytes("Arabic Communication Skills") == b"PNG-fake"
        assert ll.entry("Arabic Communication Skills")["portal_url"] == "u3"
        assert ll.join_rows(ll.entry("Arabic Communication Skills")) == [
            [{"text": "🎥 Join Meeting", "url": zoom}], [{"text": "🌐 Open Course Page", "url": "u3"}]]
        ll.set_link("Arabic Communication Skills", zoom, "manual")
        assert "portal_url" not in ll.entry("Arabic Communication Skills"), "a hand-set link has no source post"
        assert ll.shot_bytes("Arabic Communication Skills") is None
        assert ll.join_rows(ll.entry("Arabic Communication Skills")) == [[{"text": "🎥 Join Meeting", "url": zoom}]]

        # a URL activity's card is shot on the course page (its own page would redirect away)
        act = {"type": "url", "cmid": "55", "course_id": "8805", "url": "https://x/mod/url/view.php?id=55"}
        assert moodle.screenshot_target(act) == (moodle.course_url("8805"), "#module-55")
        assert ll._course_anchor(act) == moodle.course_url("8805") + "#module-55"

        # re-establish a portal link with its source, as the monitor would
        ll.set_link("Arabic Communication Skills", zoom, "portal", '"Online lecture"')
        links = ll.load()
        links["Arabic Communication Skills"]["portal_url"] = "u3"
        ll._save(links)
        ll.save_shot("Arabic Communication Skills", b"PNG-fake")

        # lecture messages: text-only heads-up, then the actionable alert with buttons + screenshot
        from uninotifier import lectures
        orig = (lectures.notifier.send_message, lectures.notifier.send_png_file, lectures.CLAIM_DIR)
        lectures.CLAIM_DIR = Path(tmp) / "claims"
        sent, files = [], []
        lectures.notifier.send_message = lambda token, chat, text, url=None, rows=None: sent.append((text, rows)) or True
        lectures.notifier.send_png_file = lambda token, chat, png, name, caption="", url=None, rows=None: (
            files.append((png, caption, rows)) or True)
        try:
            assert lectures.send_due("t", "c", datetime(2026, 10, 14, 17, 59)) == 0
            assert lectures.send_due("t", "c", datetime(2026, 10, 14, 18, 0)) == 1
            assert lectures.send_due("t", "c", datetime(2026, 10, 14, 18, 0)) == 0, "claimed: not sent twice"
            assert lectures.send_due("t", "c", datetime(2026, 10, 14, 18, 20)) == 0
            assert len(sent) == 1 and sent[0][1] is None and files == []
            assert sent[0][0].startswith("⏰ *Reminder:* Arabic Communication Skills starts in 1 hour")

            assert lectures.send_due("t", "c", datetime(2026, 10, 14, 19, 0)) == 1
            assert lectures.send_due("t", "c", datetime(2026, 10, 14, 19, 5)) == 0
            assert len(sent) == 1 and len(files) == 1, "the alert is the image + caption, not a second text"
            png, caption, rows = files[0]
            assert png == b"PNG-fake" and caption.startswith("🎥 *Lecture starting now:*")
            assert "Tap *Join Meeting*" in caption and "attached" in caption
            assert rows == [[{"text": "🎥 Join Meeting", "url": zoom}],
                            [{"text": "🌐 Open Course Page", "url": "u3"}]], rows

            # no link yet: the start alert says so and has no buttons
            ll.set_link("Arabic Communication Skills", None, "manual")
            lectures.CLAIM_DIR = Path(tmp) / "claims2"
            assert lectures.send_due("t", "c", datetime(2026, 10, 14, 19, 0)) == 1
            assert "No meeting link has been posted yet" in sent[-1][0] and sent[-1][1] is None

            # a failed send releases its claim and is retried on the next tick
            lectures.CLAIM_DIR = Path(tmp) / "claims3"
            ok = {"v": False}
            lectures.notifier.send_message = lambda token, chat, text, url=None, rows=None: ok["v"]
            assert lectures.send_due("t", "c", datetime(2026, 10, 14, 18, 0)) == 0
            ok["v"] = True
            assert lectures.send_due("t", "c", datetime(2026, 10, 14, 18, 0)) == 1
        finally:
            lectures.notifier.send_message, lectures.notifier.send_png_file, lectures.CLAIM_DIR = orig
        # the monitor's fallback goes through the same claims
        calls = []
        orig_due = lectures.send_due
        lectures.send_due = lambda token, chat, now=None: calls.append((token, chat))
        try:
            monitor.send_schedule_messages({})
        finally:
            lectures.send_due = orig_due
        assert len(calls) == 1
        ll.set_link("Arabic Communication Skills", zoom, "manual")

        # /setlink is gone: links come from the portal and ride on the lecture reminder
        replies = []
        bot.send_view = lambda text, rows: replies.append((text, rows))
        bot.handle_command("/setlink https://teams.microsoft.com/l/meetup-join/19%3amine")
        assert replies[-1][0].startswith("❓ Unknown command")
        assert ll.load()["Arabic Communication Skills"]["url"] == zoom, "nothing changed"
        assert "/setlink" not in lectures.NO_LINK_NOTE


def test_portal_timetable():
    """The bot reads the weekly timetable from the portal's home page by itself — only with
    "auto_check_in" on and the portal login in .env — and keeps it: which class, on which day and
    time, is online (ك), in person (و, م) or an activity (ن). A session is matched by its day and
    time alone, so no course names need to match. A failure is told once and retried later."""
    import threading
    from datetime import datetime
    from uninotifier import checkin, classtable, failsafe, maintenance, portal, schedule
    from uninotifier import portal_pages

    rows = portal_pages.timetable_rows(_portal_home(PORTAL_ROWS))
    assert [r["code"] for r in rows] == ["0900111", "0900222", "0900333", "0900444"], rows
    assert rows[2] == {"code": "0900333", "name": "مادة إلكترونية", "section": "3", "activity": "نظري", "room": "",
                       "time": "[ 19:00_20:30 ] ن (ن) ر (ك)"}
    assert portal_pages.timetable_rows("") == [] and portal_pages.timetable_rows("<html><body>nothing</body></html>") == []
    assert portal_pages.timetable_rows(_portal_home([]).replace("</tbody>", '<tr class="ui-datatable-empty-message">'
                                                          '<td colspan="6">No records found.</td></tr></tbody>')) == []
    assert [s["mode"] for s in schedule.parse_codes(rows[1]["time"])] == ["Blended", "Activity"], "م is the in-person day"
    slots = classtable.slots_from_rows(rows)
    assert len(slots) == 6, "the Saturday lab is left out: Sunday to Wednesday are the university days"
    assert slots[5]["course_ar"] == "مادة إلكترونية" and slots[5]["day"] == "wed" and slots[5]["mode"] == "Online"

    def at(day, hh, mm):
        return datetime(2026, 11, day, hh, mm)       # 1 Nov 2026 is a Sunday

    calls, reports, told = [], [], []
    answers = {"home": [{"home": portal.Result(section="home", found=True, data={"timetable": rows})}]}

    def fake_fetch(sections, ask_code, creds=None, keep_debug=True):
        calls.append((sections, ask_code(0, False)))
        answer = answers["home"][0]
        if isinstance(answer, Exception):
            raise answer
        return answer
    with tempfile.TemporaryDirectory() as tmp:
        keep = (classtable.CACHE_FILE, schedule.load_schedule, portal.credentials, portal.fetch_many, failsafe.report,
                notifier.send_message)
        classtable.CACHE_FILE = Path(tmp) / "class_table.json"
        sched = {"auto_check_in": True}
        schedule.load_schedule = lambda: sched
        portal.credentials = lambda: ("student", "secret")
        portal.fetch_many = fake_fetch
        failsafe.report = lambda kind, headline, **kw: reports.append((kind, headline, kw.get("detail", ""))) or True
        notifier.send_message = lambda token, chat, text, **kw: told.append(text) or True
        try:
            now = 1_800_000_000.0
            assert classtable.mode_at(at(4, 19, 0), now) is None and not classtable.known(now), "no table yet"

            # switched off, or no login: the portal is never opened
            sched["auto_check_in"] = False
            assert not classtable.update("t", "c", now=now) and calls == []
            sched["auto_check_in"] = True
            portal.credentials = lambda: None
            assert not classtable.update("t", "c", now=now, force=True) and calls == []
            portal.credentials = lambda: ("student", "secret")
            lock = threading.Lock()
            lock.acquire()
            assert not classtable.update("t", "c", lock, now) and calls == [], "a command of yours is using the portal"
            lock.release()

            # no table yet: read at once, with nobody to type a verification code — and told what it found
            assert classtable.update("t", "c", lock, now) and calls == [(["home"], None)] and not lock.locked()
            assert classtable.known(now) and len(classtable.load()["slots"]) == 6
            assert classtable.mode_at(at(4, 19, 5), now) == "Online" and classtable.mode_at(at(2, 19, 0), now) == "Activity"
            assert classtable.mode_at(at(1, 12, 30), now) == "Blended" and classtable.mode_at(at(3, 12, 30), now) == "Activity"
            assert classtable.mode_at(at(1, 9, 30), now) == "In-Person" and classtable.mode_at(at(7, 16, 0), now) is None, \
                "Saturday never counts"
            assert classtable.mode_at(at(4, 15, 0), now) is None, "no class then"
            assert classtable.mode_at(at(4, 19, 40), now) is None, "too far from the start of a class"
            assert len(told) == 1 and told[0].startswith("🗓️ *Timetable read from the portal*"), told
            assert "• Wed · 7:00 PM – 8:30 PM" in told[0] and "Mon" not in told[0] and "/check" in told[0], told[0]
            assert "🏫 3 in-person day(s) · 🧩 2 activity day(s)" in told[0] and _legacy_markdown_ok(told[0]), told[0]

            # once is enough: not weeks later; /check reads it again — and says nothing when nothing changed
            assert not classtable.update("t", "c", now=now + 70 * 86400) and len(calls) == 1
            assert classtable.update("t", "c", force=True, now=now + 100) and len(calls) == 2 and len(told) == 1

            # a timetable that changed is told (the Wednesday lecture became an activity)
            moved = [(*PORTAL_ROWS[2][:5], "[ 19:00_20:30 ] ن (ن) ر (ن)") if row[0] == "0900333" else row for row in PORTAL_ROWS]
            answers["home"] = [{"home": portal.Result(section="home", found=True, data={
                "timetable": portal_pages.timetable_rows(_portal_home(moved))})}]
            assert classtable.update("t", "c", force=True, now=now + 200) and len(told) == 2
            assert told[1].startswith("🗓️ *Your timetable on the portal changed*") and "No online lectures" in told[1], told[1]
            assert classtable.mode_at(at(4, 19, 5), now) == "Activity"

            # a past semester's table (120 days) is read again by itself — down, then in need of a person, then empty
            later = now + 200 + classtable.STALE_AFTER + 10
            assert not classtable.known(later) and classtable.mode_at(at(4, 19, 0), later) is None
            answers["home"] = [maintenance.PortalUnavailable("registration portal", "maintenance")]
            assert not classtable.update("t", "c", now=later) and len(calls) == 4 and reports == []
            assert not classtable.update("t", "c", now=later + classtable.RETRY_AFTER - 60) and len(calls) == 4
            answers["home"] = [portal.PortalError("No verification code arrived in time — try the command again.")]
            retry = later + classtable.RETRY_AFTER
            assert not classtable.update("t", "c", now=retry) and len(calls) == 5
            assert len(reports) == 1 and reports[0][0] == "classtable:portal" and "/check" in reports[0][2], reports
            # an error nobody expected (the browser missing, a page that changed) is not retried every
            # 10 minutes — each try is a sign-in to the portal — but after RETRY_AFTER like the others
            answers["home"] = [RuntimeError("Executable doesn't exist at chrome.exe")]
            odd = retry + classtable.RETRY_AFTER
            try:
                classtable.update("t", "c", now=odd)
                raise AssertionError("the bot's loop reports it (failsafe.bug)")
            except RuntimeError:
                pass
            assert len(calls) == 6 and not classtable.update("t", "c", now=odd + classtable.TICK) and len(calls) == 6
            answers["home"] = [{"home": portal.Result(section="home", found=True, data={"timetable": []})}]
            assert not classtable.update("t", "c", now=odd + classtable.RETRY_AFTER) and len(calls) == 7
            assert len(classtable.load()["slots"]) == 6, "a home page without a timetable keeps the old one"
            answers["home"] = [{"home": portal.Result(section="home", found=True, data={"timetable": rows})}]
            assert classtable.update("t", "c", now=odd + 2 * classtable.RETRY_AFTER) and classtable.known(odd + 2 * classtable.RETRY_AFTER)

            # /check reads it again, behind the portal lock, and says so only when something changed
            from uninotifier.bot import checks, portal_flow
            forced, real_update = [], classtable.update
            classtable.update = lambda token, chat, lock=None, now=None, force=False:                 forced.append((lock is portal_flow._portal_lock, force)) or True
            try:
                checks._read_timetable()
            finally:
                classtable.update = real_update
            assert forced == [(True, True)], forced

            # the check-in uses it before anything else, whatever the course is called
            classtable.CACHE_FILE.write_text(json.dumps({"read": now, "tried": now, "slots": slots}), encoding="utf-8")
            wed, mon = at(4, 19, 0), at(2, 19, 0)
            loud = {"course": "Whatever", "name": "[ 19:00_20:30 ] ن (ن) ر (و)", "marked": False}
            assert checkin.day_mode(loud, wed, {"days": {}}) == "Online", "the portal over the activity's own codes"
            assert checkin.day_mode({"course": "X", "name": "الحضور", "marked": True}, mon, {"days": {}}) == "Activity", \
                "the portal over the ك of the course name"
            assert checkin.day_mode({"course": "X", "name": "الحضور", "marked": True}, at(5, 13, 0), {"days": {}}) == "Online", \
                "no class then: the rules below take over"
            content = {"a": {"type": "attendance", "course": "20261-Algebra-AllSec-3-F", "name": "Attendance", "url": "u", "cmid": "1"},
                       "b": {"type": "attendance", "course": "20261-X-ش1-3-ك", "name": "Attendance", "url": "u", "cmid": "2"}}
            assert [a["cmid"] for a in checkin.watched({"days": {}}, content)] == ["1", "2"], "every attendance: the table says which days"
            assert not checkin.online_session(checkin.watched({"days": {}}, content)[0], at(1, 9, 30), {"days": {}})

            # the check-in follows at once when the activities to watch change (the table arrived after its first look)
            seen = {"list": [{"cmid": "1", "course": "A", "name": "n", "url": "u", "marked": False}], "reads": 0}

            def count_reads(http, activity):
                seen["reads"] += 1
                return []
            kept = (checkin.CACHE_FILE, checkin._load_content, checkin.watched, checkin.read_sessions, checkin._http)
            checkin.CACHE_FILE, checkin._load_content = Path(tmp) / "checkin.json", lambda: {}
            checkin.watched, checkin.read_sessions, checkin._http = (lambda s, c: seen["list"]), count_reads, object
            try:
                watch = checkin.Watch()
                checkin.refresh(watch, {}, 1000.0)
                assert json.loads(checkin.CACHE_FILE.read_text(encoding="utf-8"))["watch"] == ["1"] and seen["reads"] == 1
                checkin.refresh(watch, {}, 1500.0)
                assert seen["reads"] == 1, "nothing changed and it is fresh: not read again"
                seen["list"] = [*seen["list"], {"cmid": "2", "course": "B", "name": "n", "url": "u", "marked": False}]
                checkin.refresh(watch, {}, 1530.0)
                assert json.loads(checkin.CACHE_FILE.read_text(encoding="utf-8"))["watch"] == ["1", "2"] and seen["reads"] == 3,                     "a changed list is followed at once, not after a quarter of an hour"
                seen["list"] = [*seen["list"], {"cmid": "3", "course": "C", "name": "n", "url": "u", "marked": False}]
                checkin.refresh(watch, {}, 1560.0)
                assert seen["reads"] == 3, "but not twice within a minute"
                checkin.refresh(watch, {}, 1591.0)
                assert seen["reads"] == 6 and json.loads(checkin.CACHE_FILE.read_text(encoding="utf-8"))["watch"] == ["1", "2", "3"]
            finally:
                checkin.CACHE_FILE, checkin._load_content, checkin.watched, checkin.read_sessions, checkin._http = kept

            # a course with no online session by the table is idle: its attendance page is not read again
            rows_in = [{"start": datetime(2026, 11, 1, 9, 30), "end": datetime(2026, 11, 1, 11, 0)}]     # a Sunday: in person
            rows_on = [{"start": datetime(2026, 11, 4, 19, 0), "end": datetime(2026, 11, 4, 20, 30)}]     # a Wednesday: online
            looks = {"rows": rows_in, "reads": 0}

            def read_rows(http, activity):
                looks["reads"] += 1
                return looks["rows"]
            kept = (checkin.CACHE_FILE, checkin._load_content, checkin.watched, checkin.read_sessions, checkin._http)
            checkin.CACHE_FILE, checkin._load_content = Path(tmp) / "checkin2.json", lambda: {}
            checkin.watched = lambda s, c: [{"cmid": "7", "course": "Z", "name": "n", "url": "u", "marked": False}]
            checkin.read_sessions, checkin._http = read_rows, object
            classtable.CACHE_FILE.write_text(json.dumps({"read": now, "tried": now, "slots": slots}), encoding="utf-8")
            try:
                watch = checkin.Watch()
                checkin.refresh(watch, {}, 5000.0)
                cached = json.loads(checkin.CACHE_FILE.read_text(encoding="utf-8"))
                assert cached["activities"][0]["idle"] is True and cached["table"] == now and looks["reads"] == 1
                checkin.refresh(watch, {}, 5000.0 + checkin.SESSIONS_EVERY + 1)
                assert looks["reads"] == 1, "idle: not read again, hours later"
                classtable.CACHE_FILE.write_text(json.dumps({"read": now + 5, "tried": now, "slots": slots}), encoding="utf-8")
                checkin.refresh(watch, {}, 5000.0 + 2 * checkin.SESSIONS_EVERY)
                assert looks["reads"] == 2, "a new timetable: judged again"
                looks["rows"] = rows_on
                checkin.refresh(watch, {}, 5000.0 + 4 * checkin.SESSIONS_EVERY)
                assert looks["reads"] == 2, "same timetable: still idle"
                classtable.CACHE_FILE.write_text(json.dumps({"read": now + 9, "tried": now, "slots": slots}), encoding="utf-8")
                checkin.refresh(watch, {}, 5000.0 + 6 * checkin.SESSIONS_EVERY)
                cached = json.loads(checkin.CACHE_FILE.read_text(encoding="utf-8"))
                assert looks["reads"] == 3 and cached["activities"][0]["idle"] is False, "an online session is never idle"
            finally:
                checkin.CACHE_FILE, checkin._load_content, checkin.watched, checkin.read_sessions, checkin._http = kept
        finally:
            (classtable.CACHE_FILE, schedule.load_schedule, portal.credentials, portal.fetch_many, failsafe.report,
             notifier.send_message) = keep


def test_week_logic():
    """The whole week, as a student can have it: courses split over Sunday–Wednesday (Sun + Tue,
    Mon + Wed, or all four), each on two days with its own mode on each, in any order; a course on
    one day only; Thursday and Saturday never; a row with a code the bot does not know is told, not
    dropped. Only a day that is an online lecture (ك) is ever checked in — whatever the course."""
    from datetime import datetime
    from uninotifier import checkin, classtable

    def row(code, time):
        return {"code": code, "name": "x", "section": "1", "activity": "نظري", "room": "", "time": time}
    rows = [row("A", "[ 09:30_11:00 ] ح (و) ث (و)"),            # in person both days
            row("B", "[ 11:00_12:30 ] ح (ن) ث (و)"),            # an activity first, then in person
            row("C", "[ 12:30_14:00 ] ح (و) ث (ن)"),            # in person first, then an activity
            row("D", "[ 14:00_15:30 ] ح (ك) ث (ك)"),            # online both days
            row("E", "[ 15:30_17:00 ] ح (م) ث (ن)"),            # blended: م is the in-person day
            row("F", "[ 09:30_11:00 ] ن (ك) ر (ن)"),            # online, then an activity
            row("G", "[ 11:00_12:30 ] ن (ن) ر (ك)"),            # an activity, then online
            row("H", "[ 12:30_14:00 ] ن (و) ر (و)"),
            row("I", "[ 14:00_15:30 ] ن (ن) ر (م)"),            # an activity, then the blended in-person day
            row("J", "[ 19:00_20:30 ] ن (ك) ر (ك)"),            # online both days, in the evening
            row("K", "[ 16:00_17:30 ] ن (ك)"),                  # one day only
            row("L", "[ 10:00_11:30 ] خ (ك)"),                  # Thursday: not a university day
            row("M", "[ 16:00_18:00 ] س (و)"),                  # a Saturday lab
            row("N", "[ 7:30_9:00 ] ث (ك)")]                    # a one-digit hour: still read
    unclear = [row("X", "[ 08:00_09:30 ] ح (ز) ث (و)"),         # an unknown mode letter
               row("Y", "[ 10:00_11:30 ]"),                      # no codes at all
               row("Z", "[ 10:00_11:30 ] ح ث (ك)")]              # one mode for two days: not the portal's way
    slots = classtable.slots_from_rows(rows + unclear)
    assert {s["code"] for s in slots} == set("ABCDEFGHIJKN"), "Thursday, Saturday and the unreadable rows are not slots"
    assert classtable.unreadable(rows) == [] and [r["code"] for r in classtable.unreadable(rows + unclear)] == ["X", "Y", "Z"]

    #            day of Nov 2026 (1 = Sunday), time -> the mode that day
    expected = {(1, 9, 30): "In-Person", (1, 11, 0): "Activity", (1, 12, 30): "In-Person", (1, 14, 0): "Online",
                (1, 15, 30): "Blended",
                (3, 9, 30): "In-Person", (3, 7, 30): "Online", (3, 11, 0): "In-Person", (3, 12, 30): "Activity", (3, 14, 0): "Online",
                (3, 15, 30): "Activity",
                (2, 9, 30): "Online", (2, 11, 0): "Activity", (2, 12, 30): "In-Person", (2, 14, 0): "Activity",
                (2, 19, 0): "Online", (2, 16, 0): "Online",
                (4, 9, 30): "Activity", (4, 11, 0): "Online", (4, 12, 30): "In-Person", (4, 14, 0): "Blended",
                (4, 19, 0): "Online",
                (5, 10, 0): None, (7, 16, 0): None}
    with tempfile.TemporaryDirectory() as tmp:
        keep = classtable.CACHE_FILE
        classtable.CACHE_FILE = Path(tmp) / "class_table.json"
        classtable.CACHE_FILE.write_text(json.dumps({"read": 1_800_000_000.0, "slots": slots}), encoding="utf-8")
        try:
            for (day, hh, mm), mode in expected.items():
                start = datetime(2026, 11, day, hh, mm)
                assert classtable.mode_at(start) == mode, (start.strftime("%a %H:%M"), mode, classtable.mode_at(start))
                activity = {"course": "Any name at all", "name": "Attendance", "marked": False}
                assert checkin.online_session(activity, start, {"days": {}}) is (mode == "Online"), \
                    ("only an online day is checked in", start.strftime("%a %H:%M"), mode)
            assert sum(mode == "Online" for mode in expected.values()) == 8, "D×2, F, G, J×2, K, N"
            # a session a little off the class's start is still that class; far off is none
            assert classtable.mode_at(datetime(2026, 11, 2, 9, 55)) == "Online" and classtable.mode_at(datetime(2026, 11, 2, 10, 5)) is None
            assert classtable.mode_at(datetime(2026, 11, 3, 7, 30)) == "Online", "the one-digit hour of row N"

            told = classtable.summary(slots, first=True, unclear=[{"code": "X", "time": "x"}, {"code": "Y", "time": "y"}])
            assert "⚠️ I could not read 2 row(s) of the timetable (course X, Y)" in told and _legacy_markdown_ok(told), told
            assert "• Mon · 9:30 AM – 11:00 AM" in told and "• Wed · 7:00 PM – 8:30 PM" in told, told
            assert "⚠️" not in classtable.summary(slots, first=True)

            # a table kept by an older version may still hold a Saturday class: it never counts
            classtable.CACHE_FILE.write_text(json.dumps({"read": 1_800_000_000.0, "slots": [
                *slots, {"day": "sat", "start": "16:00", "end": "18:00", "mode": "Online"}]}), encoding="utf-8")
            assert classtable.mode_at(datetime(2026, 11, 7, 16, 0)) is None
        finally:
            classtable.CACHE_FILE = keep


def test_a_renamed_course_is_still_that_course():
    """Moodle's name for a course can change mid-term; its id cannot. Each check remembers the
    timetable course of every id, so a name that matches nothing under a known id is that course."""
    from uninotifier import coursenames
    days = {"sun": [{"start": "09:00", "end": "10:00", "course": "Technical Writing", "course_ar": "الكتابة التقنية",
                     "section": "01", "mode": "In-Person", "label": "In-Person Lecture"}]}
    keep = schedule.load_schedule
    schedule.load_schedule = lambda: {"days": days}
    schedule._arabic_to_english.cache_clear()
    coursenames.STATE_FILE.unlink(missing_ok=True)
    try:
        before = {"801": {"name": "20261-الكتابة التقنية-ش07-3-ك"}, "802": {"name": "20261-Unlisted Course-AllSec-3-"}}
        assert schedule.learn_course_names(before) == [] and coursenames.load()["ids"] == {"801": "Technical Writing"}
        # the university renames it to words the timetable does not have
        after = {"801": {"name": "20261-Report Composition-Sec07-3-E"}, "802": {"name": "20261-Unlisted Course-AllSec-3-"}}
        assert schedule.canonical_course("Report Composition") == "Report Composition", "unknown before the check"
        assert schedule.learn_course_names(after) == [("Report Composition", "Technical Writing")]
        assert schedule.canonical_course("Report Composition") == "Technical Writing"
        assert media.clean_course_name("20261-Report Composition-Sec07-3-E") == "Technical Writing"
        assert archive.english_course_name("20261-Report Composition-Sec07-3-E") == "Technical Writing", "the same folder"
        assert schedule.lectures_for(media.clean_course_name(after["801"]["name"])), "its timetable is still found"
        assert schedule.canonical_course("Unlisted Course") == "Unlisted Course", "a course outside the timetable stays as it is"
        # a damaged file is no names, not a crash
        coursenames.STATE_FILE.write_text("{not json", encoding="utf-8")
        assert coursenames.alias("Report Composition") is None
    finally:
        schedule.load_schedule = keep
        schedule._arabic_to_english.cache_clear()
        coursenames.STATE_FILE.unlink(missing_ok=True)


def test_course_list_is_a_timetable():
    """The course list is ordered like the week: grouped by the days a course meets (Sun · Tue together,
    Mon · Wed together), each group by the time of its lectures, a course that meets at the same time on
    both days says it once and only spells out what differs, and the rest stays short."""
    from uninotifier import timetable
    bot = _package("uninotifier.bot")

    def lec(course, start, end, mode="In-Person", label="In-Person Lecture", place=None):
        d = {"start": start, "end": end, "course": course, "course_ar": course, "section": "01", "mode": mode, "label": label}
        if place:
            d["place"] = place
        return d

    days = {
        "sat": [lec("Big Data", "08:00", "10:00", label="Lab")],
        "sun": [lec("Late Course", "15:30", "17:00", place="Room 5"), lec("Early Course", "09:30", "11:00", place="Room 1"),
                lec("Big Data", "11:00", "12:30", place="Room 60"), lec("Same Time Other Room", "13:00", "14:00", place="A")],
        "mon": [lec("Evening", "19:00", "20:30", mode="Activity", label="Activity / Assignment")],
        "tue": [lec("Early Course", "09:30", "11:00", place="Room 1"), lec("Late Course", "15:30", "17:00", place="Room 5"),
                lec("Big Data", "11:00", "12:30", mode="Activity", label="Activity / Self-Study"),
                lec("Same Time Other Room", "13:00", "14:00", place="B")],
        "wed": [lec("Evening", "19:00", "20:30", mode="Online", label="Online Lecture"),
                lec("Odd Times", "08:00", "09:00")],
        "thu": [lec("Odd Times", "10:00", "11:00", place="Hall")],
    }
    names = ["Late Course", "Early Course", "Big Data", "Same Time Other Room", "Evening", "Odd Times", "No Timetable"]
    state = {"courses": {str(n): {"name": f"20261-{name}-AllSec-3-", "url": f"https://x/c{n}"} for n, name in enumerate(names)},
             "teachers": {}}
    keep = (schedule.load_schedule, bot.load_state)
    schedule.load_schedule = lambda: {"days": days, "semester": "S"}
    schedule._arabic_to_english.cache_clear()
    bot.load_state = lambda: state
    try:
        # the portal words a name differently from the timetable: the same words in another order are the same course
        assert schedule.canonical_course("Course Early") == "Early Course"
        assert schedule.canonical_course("The Times of Odd") == "Odd Times"
        assert schedule.canonical_course("Course") == "Course", "some of the words is not the course"
        text = bot.view_course_list()[0]
        titles = [l for l in text.splitlines() if l.startswith("📅")]
        assert titles == ["📅 *Sun · Tue*", "📅 *Mon · Wed*", "📅 *Wed · Thu*", "📅 *No timetable entry*"], titles
        listed = [l.split("[")[1].split("]")[0] for l in text.splitlines() if l[:2].rstrip(".").isdigit() or l[:3].rstrip(".").isdigit()]
        # Sun · Tue: by the time of the lectures, earliest first (9:30, 11:00, 13:00, 15:30); then the other patterns
        assert listed[:4] == ["Early Course", "Big Data", "Same Time Other Room", "Late Course"], listed
        assert listed.index("Evening") > listed.index("Late Course") and listed[-1] == "No Timetable"
        assert [l for l in text.splitlines() if l.startswith("📅")][0] == "📅 *Sun · Tue*"
        N = "\u00a0"                       # every indent is a non-breaking space: it cannot be trimmed
        assert not any(l.startswith(" ") for l in text.splitlines()), "no ordinary leading spaces"
        # a course block: name, instructor, section, the hour right under it; the kind of session and its room
        # on lines of their own, the room hanging from the kind
        assert ("1. 📘 [Early Course](https://x/c1)\n" + N * 4 + "🔢 Section 01\n" + N * 4 + "🕘 Sun · Tue · 9:30–11:00 AM\n"
                + N * 7 + "└ 🏫 In-Person\n" + N * 9 + "└ 📍 Room 1") in text
        assert "\n\n" + N * 4 + "🕘" not in text, "no blank line between the section and the hour"
        assert text.count("9:30–11:00 AM") == 1, "the same hour on both days is said once, alike days once"
        assert not any("In-Person · 📍" in line or "Activity · 📍" in line for line in text.splitlines())
        # the same hour but another kind of session / another room: the hour once, a branch per day
        assert (N * 4 + "🕘 Sun · Tue · 11:00 AM–12:30 PM\n" + N * 7 + "├ Sun — 🏫 In-Person\n" + N * 7 + "│" + N * 2 + "└ 📍 Room 60\n"
                + N * 7 + "└ Tue — 🧩 Activity") in text
        assert (N * 4 + "🕘 Sun · Tue · 1:00–2:00 PM\n" + N * 7 + "├ Sun — 🏫 In-Person\n" + N * 7 + "│" + N * 2 + "└ 📍 A\n"
                + N * 7 + "└ Tue — 🏫 In-Person\n" + N * 10 + "└ 📍 B") in text
        assert N * 4 + "🕘 Mon · Wed · 7:00–8:30 PM\n" + N * 7 + "├ Mon — 🧩 Activity\n" + N * 7 + "└ Wed — 💻 Online" in text
        # short words only; a day outside the campus days is its own entry when there is one; different hours: an entry each
        assert not any(w in text for w in ("In-Person Lecture", "Online Lecture", "Self-Study", "Assignment"))
        assert N * 4 + "🕘 Wed · 8:00–9:00 AM\n" + N * 7 + "└ 🏫 In-Person\n" in text
        assert N * 4 + "🕘 Thu · 10:00–11:00 AM\n" + N * 7 + "└ 🏫 In-Person\n" + N * 9 + "└ 📍 Hall" in text
        assert N * 4 + "🕘 Sat · 8:00–10:00 AM\n" + N * 7 + "└ 🏫 Lab" in text, "a day outside Sun–Thu is shown when the schedule has one"
        assert "──" not in text and _legacy_markdown_ok(text) and len(text) < notifier.MESSAGE_LIMIT
    finally:
        schedule.load_schedule, bot.load_state = keep
        schedule._arabic_to_english.cache_clear()

    # the pure helpers
    slots = [("sun", lec("A", "09:00", "10:00")), ("tue", lec("A", "09:00", "10:00"))]
    assert timetable.pattern(slots) == ("sun", "tue") and timetable.group_title(("sun", "tue")) == "Sun · Tue"
    assert timetable.group_title(()) == "Other days"
    N = "\u00a0"
    assert timetable.describe(slots) == ["🕘 Sun · Tue · 9:00–10:00 AM", N * 3 + "└ 🏫 In-Person"]
    assert timetable.time_range({"start": "09:30", "end": "11:00"}) == "9:30–11:00 AM"
    assert timetable.time_range({"start": "11:00", "end": "12:30"}) == "11:00 AM–12:30 PM"
    assert timetable.time_range({"start": "12:30", "end": "14:00"}) == "12:30–2:00 PM"
    two_a_day = [("sun", lec("A", "09:00", "10:00")), ("sun", lec("A", "12:00", "13:00"))]
    assert timetable.describe(two_a_day) == ["🕘 Sun · 9:00–10:00 AM", N * 3 + "└ 🏫 In-Person", "🕘 Sun · 12:00–1:00 PM", N * 3 + "└ 🏫 In-Person"], "two sessions on one day: an entry each"
    assert timetable.order_key([]) > timetable.order_key([("sat", lec("A", "08:00", "09:00"))]) > timetable.order_key(slots)

    # the same hierarchy in the lecture alerts
    one = lec("Arabic Communication Skills", "19:00", "20:30", mode="Online", label="Online Lecture", place="Hall 3")
    assert timetable.lecture_lines(one) == ["🕘 7:00–8:30 PM", N * 3 + "└ 💻 Online"], "online: no room, even if one is listed"
    live = lec("Big Data", "09:00", "10:00", place="Building 5, Room 2147")
    assert timetable.lecture_lines(live) == ["🕘 9:00–10:00 AM", N * 3 + "└ 🏫 In-Person", N * 5 + "└ 📍 Room 2147"]
    sched = {"lecture_reminder_minutes": 60, "remind_modes": ["Online"], "days": {"wed": [{**one, "section": "31"}]}}
    wed = datetime(2026, 10, 21, 18, 30)                      # a Wednesday, half an hour before the hour
    by_kind = {kind: text for _, kind, text, _ in schedule.due_messages(sched, wed)}
    assert by_kind["pre"].endswith("\n🕘 7:00–8:30 PM\n" + N * 3 + "└ 💻 Online"), by_kind["pre"]
    assert schedule.due_messages(sched, datetime(2026, 10, 21, 8, 0)) == [], "nothing is sent in the morning: only the lecture's own reminders"
    start = {kind: text for _, kind, text, _ in schedule.due_messages(sched, datetime(2026, 10, 21, 19, 2))}["start"]
    assert start.startswith("🎥 *Lecture starting now:*") and "\n🕘 7:00–8:30 PM\n" in start


def test_rooms_are_always_short():
    """A location is "📍 Room <number>" and nothing more — no building or faculty name, no block prefix —
    and a session that is not in a room (Activity, Online) has none, so a location line can never wrap."""
    from uninotifier import timetable
    N = "\u00a0"
    for raw, short in (("Building 5, Room 2147", "Room 2147"), ("Building 5, Room 2135", "Room 2135"),
                       ("Engineering College, Room 32123", "Room 32123"), ("Room 60-2104", "Room 2104"),
                       ("Room 449", "Room 449"), ("room 2147", "Room 2147"), ("Faculty of IT Room 12-345", "Room 345"),
                       ("Room A-12", "Room A-12"), ("Hall 3", "Hall 3"), ("  ", ""), (None, ""), ("", "")):
        assert timetable.room(raw) == short, (raw, timetable.room(raw))

    def lec(mode, place):
        return {"start": "09:30", "end": "11:00", "course": "X", "course_ar": "x", "section": "01", "mode": mode,
                "label": mode, "place": place}
    for mode in ("Activity", "Online"):
        lines = timetable.describe([("sun", lec(mode, "Building 5, Room 2147")), ("tue", lec(mode, "Room 60-2104"))])
        assert not any("📍" in line for line in lines), (mode, lines)
    # two days, one in a room and one remote: the room only under its day, in the one format
    lines = timetable.describe([("sun", lec("In-Person", "Room 60-2104")), ("tue", lec("Activity", "Room 60-2104"))])
    assert lines == ["🕘 Sun · Tue · 9:30–11:00 AM", N * 3 + "├ Sun — 🏫 In-Person", N * 3 + "│" + N * 2 + "└ 📍 Room 2104",
                     N * 3 + "└ Tue — 🧩 Activity"], lines

    # the real schedule: every room is already in that form, nothing remote has one, and the course list shows
    # nothing but "📍 Room <number>"
    import json
    schedule_data = json.loads(paths.SCHEDULE_FILE.read_text(encoding="utf-8"))
    for day, entries in schedule_data["days"].items():
        for item in entries:
            assert timetable.room(item.get("place")) == item.get("place", ""), (day, item["course"], item.get("place"))
            assert not (item["mode"] in timetable.REMOTE_MODES and item.get("place")), (day, item["course"])
    bot = _package("uninotifier.bot")
    text = bot.view_course_list()[0]
    places = [line.strip(N + " ") for line in text.splitlines() if "📍" in line]
    assert places and all(re.fullmatch("(│\u00a0*)?└ 📍 Room \\d+", p) for p in places), places
    assert "Building" not in text and "College" not in text and "60-2104" not in text and max(len(p) for p in places) <= 16


def test_teams_meeting_and_attendance_items():
    """teamscheduler and attendance are no longer "Unclassified": a Teams meeting comes
    with its details and a 🔗 Join Teams Lecture button (its link also feeds the lecture
    reminders); attendance with the next session and ✍️ Check In. A dry-run check-in
    finds the open session and plans the POST without sending it."""
    from uninotifier import activities
    monitor = _package("uninotifier.monitor")

    course = "20261-الاتصال والتواصل باللغة العربية-ش31-3-ك"
    teams = {"name": "رابط الدخول إلى المحاضرة", "type": "teamscheduler", "course": course, "cmid": "76459",
             "url": "https://elearning.zuj.edu.jo/mod/teamscheduler/view.php?id=76459", "section": "روابط هامة ورئيسية"}
    attend = {**teams, "name": "الحضور", "type": "attendance", "cmid": "75482",
              "url": "https://elearning.zuj.edu.jo/mod/attendance/view.php?id=75482",
              "_body": "<p>الرجاء تأكيد الحضور بالدخول إلى هذا الرابط .</p>"}
    assert media.classify(teams) == "meeting" and media.classify(attend) == "attendance"

    meeting = activities.parse_meeting(TEAMS_PAGE)
    assert meeting["url"].startswith("https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC")
    assert ("Repeat on", "Monday, Wednesday") in meeting["details"] and len(meeting["details"]) == 4
    assert activities.parse_meeting("<html><body>nothing</body></html>") == {"url": None, "details": []}

    sessions = activities.parse_sessions(ATTENDANCE_PAGE)
    assert [s["date"] for s in sessions] == ["Mon 12 Oct 2026", "Wed 14 Oct 2026", "Mon 19 Oct 2026"]
    assert sessions[0]["start"] == datetime(2026, 10, 12, 19, 0) and sessions[0]["status"] == "Present"
    nxt = activities.next_session(sessions, datetime(2026, 10, 13, 9, 0))
    assert nxt["date"] == "Wed 14 Oct 2026"
    assert activities.next_session(sessions, datetime(2026, 10, 14, 20, 0))["date"] == "Wed 14 Oct 2026", "in progress"
    assert activities.open_session(sessions)["submit_url"].endswith("sessid=55&sesskey=SECRET")

    # the messages: no raw URL, a button instead; nothing leaks the session key
    text = media.build_meeting_message(teams, meeting)
    assert text.startswith("💻 *New Online Lecture*") and "Microsoft Teams Meeting" in text
    assert "📆 *Repeat on:* Monday, Wednesday" in text and "teams.microsoft.com" not in text
    assert media.meeting_rows(teams, meeting) == [[{"text": "🔗 Join Teams Lecture", "url": meeting["url"]}]]
    assert media.meeting_rows(teams, None)[0][0]["url"] == teams["url"], "no link read: the portal page"
    text = media.build_attendance_message(attend, sessions, datetime(2026, 10, 13, 9, 0))
    assert text.startswith("📝 *Attendance Check-in*") and "⏱️ *Type:* Attendance Check-in" in text
    assert "Next session:* Wed 14 Oct 2026 · 7PM - 8:30PM" in text and "SECRET" not in text
    assert media.attendance_rows(attend) == [[{"text": "✍️ Check In / تأكيد الحضور", "url": attend["url"]}]]
    for t in (text, media.build_meeting_message(teams, meeting), media.build_meeting_message(teams, None)):
        assert _legacy_markdown_ok(t), t

    # the monitor: reads the page, keeps the join link as the item's target, sends the right message
    class Ctx:
        def __init__(self):
            self.request = self

        def get(self, url, timeout=None, max_redirects=None):
            class R:
                ok, status = True, 200

                def text(self_inner):
                    return TEAMS_PAGE if "teamscheduler" in url else ATTENDANCE_PAGE
            return R()
    art = monitor.process_item(Ctx(), "s", teams)
    assert art["kind"] == "meeting" and art["target"] == meeting["url"] and not monitor.needs_screenshot(art)
    art2 = monitor.process_item(Ctx(), "s", attend)
    assert art2["kind"] == "attendance" and len(art2["sessions"]) == 3 and not monitor.needs_screenshot(art2)
    sent = []
    orig = monitor.deliver
    monitor.deliver = lambda header, body="", url=None, rows=None, shots=(), shot_name="", document=None: \
        sent.append((header, body, rows)) or True
    try:
        assert monitor.notify_new_item(teams, None, art) and monitor.notify_new_item(attend, None, art2)
    finally:
        monitor.deliver = orig
    assert sent[0][0].startswith("💻") and sent[0][2][0][0]["text"] == "🔗 Join Teams Lecture"
    assert sent[1][0].startswith("📝") and sent[1][2][0][0]["text"].startswith("✍️ Check In")
    assert "الرجاء تأكيد الحضور" in sent[1][1], "the instructor's note follows as the body"

    # a dry-run check-in plans the POST for "Present" and sends nothing (the real one: test_automatic_check_in)
    posted = []

    class Http:
        def get(self, url, timeout=None):
            class R:
                ok, status = True, 200

                def text(self_inner):
                    return CHECK_IN_PAGE
            return R()

        def post(self, url, data=None, headers=None):
            posted.append((url, data))
    result = activities.check_in(Http(), activities.open_session(sessions))
    assert result["reason"] == "dry run" and result["plan"]["url"].endswith("/mod/attendance/attendance.php")
    assert result["plan"]["data"] == {"sessid": "55", "sesskey": "SECRET", "status": "101",
                                      "submitbutton": "Save changes"}, result["plan"]
    assert posted == [], "a dry run sends nothing"
    assert activities.check_in(Http(), sessions[0])["reason"] == "check-in is not open for this session"
    assert sessions[2]["end"] == datetime(2026, 10, 19, 20, 30) and not activities.recorded(sessions[2])
    assert activities.recorded(sessions[0]) and not activities.recorded(sessions[1]), "a link is not a status"


def test_automatic_check_in():
    """Online lectures check themselves in: during each session the attendance page is read
    every minute; when check-in opens, Present + Save changes; then Moodle itself must show it
    — ✅ with a screenshot of your row. When it cannot be done (a password, no Present choice,
    Moodle does not record it, the session cannot be read for 10 minutes) — 🚨 check in by
    hand, why, the page, a button; and ✅ follows once you did. A lecture whose check-in never
    opened ends with a silent ℹ️. Only online days (ك) — never in person (و, م) nor an activity
    day (ن), whatever opens there. Off unless "auto_check_in" is true."""
    from datetime import datetime
    from uninotifier import activities, checkin, probe, schedule

    # the form: the right one of two, Present even with its time limit, never Cancel
    form = activities.read_form(_check_in_form(), "https://elearning.zuj.edu.jo/mod/attendance/attendance.php?sessid=55")
    assert form.action == "https://elearning.zuj.edu.jo/mod/attendance/attendance.php" and not form.password
    assert list(form.choices) == ["Present (Available for 10 minutes)", "Late", "Absent"]
    assert "cancel" not in form.fields and form.fields["submitbutton"] == "Save changes" and "q" not in form.fields
    assert activities.read_form(_check_in_form(("حاضر", "غائب")), "x").choices["حاضر"] == ("status", "101")
    assert activities.read_form(_check_in_form(extra='<input type="text" name="studentpassword" id="id_studentpassword">'),
                                "x").password, "a session password is a text field on Moodle"
    assert activities.read_form(_check_in_form(()), "x").choices == {}, "a session that sets the status itself"
    assert activities.read_form("<html><body><form><input name='q'></form></body></html>", "x") is None

    sched = {"auto_check_in": True, "days": {
        "wed": [{"start": "19:00", "end": "20:30", "course": "Big Data", "course_ar": "البيانات الضخمة",
                 "section": "01", "mode": "Online"}],
        "mon": [{"start": "19:00", "end": "20:30", "course": "Big Data", "course_ar": "البيانات الضخمة",
                 "section": "01", "mode": "Activity"}],
        "sun": [{"start": "09:30", "end": "11:00", "course": "Databases", "course_ar": "قواعد البيانات",
                 "section": "01", "mode": "In-Person"}]}}
    assert schedule.validate({**sched, "auto_check_in": "yes"}) == ['"auto_check_in" must be true or false']
    assert schedule.validate(sched) == [], schedule.validate(sched)
    assert checkin.covers("Big Data", sched, {}) and not checkin.covers("Databases", sched, {})
    assert not checkin.covers("Big Data", {**sched, "auto_check_in": False}, {})
    # Moodle says which courses are online by itself: ZUJ's mark at the end of the name (ك), or a Teams meeting
    marked = {"a": {"type": "attendance", "course": "20261-الاتصال والتواصل باللغة العربية-ش12-3-ك"},
              "b": {"type": "attendance", "course": "20261-الاتصال والتواصل باللغة الإنجليزية-ش15-3-و"},
              "c": {"type": "attendance", "course": "20261-Databases-AllSec-3-F"},
              "d": {"type": "teamscheduler", "course": "20261-Statistics-AllSec-3-"},
              "e": {"type": "attendance", "course": "20261-Numerical Analysis(1)-AllSec-3-"}}
    arabic, statistics = (media.clean_course_name(marked[k]["course"]) for k in "ad")
    assert checkin.online_courses({"days": {}}, marked) == {arabic, statistics}, "no timetable needed"
    assert checkin.covers(arabic, {"auto_check_in": True, "days": {}}, marked)

    # ZUJ's codes: the day letter, then that day's mode in brackets
    assert schedule.parse_codes("[ 19:00_20:30 ] ن (ن) ر (ك)") == [
        {"day": "mon", "start": "19:00", "end": "20:30", "mode": "Activity"},
        {"day": "wed", "start": "19:00", "end": "20:30", "mode": "Online"}]
    assert [s["mode"] for s in schedule.parse_codes("[ 12:30_14:00 ] ح (م) ث (ن) · [ 16:00_18:00 ] س (و)")] == \
        ["Blended", "Activity", "In-Person"] and schedule.parse_codes("الحضور") == []

    # which day is an online lecture: the activity's own codes, then the timetable, then the course's ك
    mon, wed = datetime(2026, 11, 2, 19, 0), datetime(2026, 11, 4, 19, 0)
    ar = {"course": arabic, "name": "الحضور", "marked": True}
    assert checkin.day_mode(ar, mon, {"days": {}}) == "Online", "ك and nothing else known: every session"
    listed = {"days": {"mon": [{"start": "19:00", "course": arabic, "mode": "Activity"}],
                       "wed": [{"start": "19:00", "course": arabic, "mode": "Online"}]}}
    assert checkin.day_mode(ar, mon, listed) == "Activity" and checkin.day_mode(ar, wed, listed) == "Online"
    coded = {"course": "Databases", "name": "Attendance- SEC- 2 - [ 19:00_20:30 ] ن (و) ر (ك)", "marked": False}
    assert checkin.online_session(coded, wed, listed) and not checkin.online_session(coded, mon, listed)
    assert checkin.day_mode({**ar, "name": "[ 19:00_20:30 ] ن (ن) ر (ن)"}, wed, listed) == "Activity", \
        "the activity's own codes come first"
    assert checkin.day_mode({"course": "Databases", "name": "Attendance", "marked": False}, wed, listed) is None
    assert [a["cmid"] for a in checkin.watched({"days": {}}, {
        "x": {"type": "attendance", "course": "20261-Databases-AllSec-3-F", "cmid": "9", "url": "u",
              "name": "[ 09:30_11:00 ] ن (و) ر (ك)"},
        "y": {"type": "attendance", "course": "20261-Databases-AllSec-3-F", "cmid": "8", "url": "u",
              "name": "[ 09:30_11:00 ] ن (و) ر (و)"}})] == ["9"], "an online day in the name is enough"

    view = "https://elearning.zuj.edu.jo/mod/attendance/view.php?id=4242"
    content = {"5302-4242": {"type": "attendance", "course": "20261-Big Data-AllSec-3-", "name": "Attendance",
                             "url": view, "cmid": "4242"},
               "5296-77": {"type": "attendance", "course": "20261-Databases-AllSec-3-F", "name": "Attendance",
                           "url": "https://elearning.zuj.edu.jo/mod/attendance/view.php?id=77", "cmid": "77"}}
    # the sessions: date -> [status cell, remarks, form behind the link, what a POST does]
    days = {"Wed 4 Nov 2026": ["?", "", _check_in_form(), "records"],
            "Wed 11 Nov 2026": ["?", "", _check_in_form(extra='<input type="text" name="studentpassword">'), "records"],
            "Wed 18 Nov 2026": ["?", "", _check_in_form(), "ignores"],
            "Mon 23 Nov 2026": ["?", "", _check_in_form(), "records"],
            "Wed 25 Nov 2026": ["?", "", "", ""],
            "Wed 2 Dec 2026": ["?", "", "", ""]}
    sessid = {d: 60 + n for n, d in enumerate(days)}
    moodle = {"expired": False, "gets": [], "posts": []}

    def open_check_in(day):
        days[day][0] = (f'<a href="https://elearning.zuj.edu.jo/mod/attendance/attendance.php?sessid={sessid[day]}'
                        f'&amp;sesskey=SECRETKEY1">Submit attendance</a>')

    def page():
        rows = "".join(f'<tr><td class="datecol cell c0"><nobr>{d}</nobr> <nobr>7PM - 8:30PM</nobr></td>'
                       f'<td class="desccol cell c1">Regular class session</td><td class="statuscol cell c2">{v[0]}'
                       f'</td><td class="pointscol cell c3">? / 2</td><td class="remarkscol cell c4">{v[1]}</td></tr>'
                       for d, v in days.items())
        return (f'<html><body><div role="main"><table class="generaltable attwidth"><tbody>{rows}</tbody></table>'
                f'</div></body></html>')

    class R:
        def __init__(self, text, url, status=200):
            self._text, self.url, self.status, self.ok = text, url, status, 200 <= status < 300

        def text(self):
            return self._text

    class Http:
        def get(self, url, timeout=None, max_redirects=None):
            moodle["gets"].append(url)
            if moodle["expired"]:
                return R("<html>log in</html>", "https://elearning.zuj.edu.jo/login/index.php")
            if "view.php" in url:
                assert url == view + "&view=5", "always every session (Moodle remembers the last view)"
                return R(page(), url)
            day = next(d for d, n in sessid.items() if f"sessid={n}&" in url)
            return R(days[day][2], url)

        def post(self, url, data=None, headers=None):
            moodle["posts"].append(data)
            day = next(d for d, n in sessid.items() if n == int(data["sessid"]) or d == current["day"])
            if days[day][3] == "records" and data.get("status") == "101" and "cancel" not in data:
                days[day][:2] = ["Present", "Self-recorded"]
            return R(page(), view)

    current = {"day": None}
    sent, shots, launched = [], [], []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        keep = (checkin.STATE_FILE, checkin.CACHE_FILE, checkin.PAGES_DIR, checkin._http, checkin.screenshot,
                daily.CLAIM_DIR, schedule.load_schedule, notifier.send_message, notifier.send_png_file,
                probe.launch_check, probe.MONITOR_LOCK)
        checkin.STATE_FILE, checkin.CACHE_FILE, checkin.PAGES_DIR = tmp / "saved.json", tmp / "checkin.json", tmp / "pages"
        checkin.STATE_FILE.write_text(json.dumps({"content": content}), encoding="utf-8")
        checkin._http = lambda: Http()
        checkin.screenshot = lambda url, want=None: shots.append((url, want)) or b"PNG"
        daily.CLAIM_DIR, probe.MONITOR_LOCK = tmp / "claims", tmp / "monitor.lock"
        schedule.load_schedule = lambda: sched
        notifier.send_message = lambda token, chat, text, url=None, rows=None, silent=False, **kw: \
            sent.append(("text", text, rows, silent)) or True
        notifier.send_png_file = lambda token, chat, png, name, caption="", url=None, rows=None: \
            sent.append(("png", caption, rows, False)) or True
        probe.launch_check = lambda: launched.append(1)
        try:
            watch = checkin.Watch()

            def tick(day, hh, mm, ss=0):
                current["day"] = day
                checkin.tick("t", "c", watch, datetime.strptime(f"{day} {hh}:{mm}:{ss}", "%a %d %b %Y %H:%M:%S"))

            # off: nothing is read at all
            schedule.load_schedule = lambda: {**sched, "auto_check_in": False}
            tick("Wed 4 Nov 2026", 19, 5)
            assert moodle["gets"] == [] and not checkin.CACHE_FILE.exists()
            schedule.load_schedule = lambda: sched

            # the sessions are read once: only the online course's attendance is watched
            tick("Wed 4 Nov 2026", 18, 40)
            cache = json.loads(checkin.CACHE_FILE.read_text(encoding="utf-8"))
            assert [a["cmid"] for a in cache["activities"]] == ["4242"] and len(cache["activities"][0]["sessions"]) == 6
            assert cache["activities"][0]["sessions"][0] == {"start": "2026-11-04T19:00:00", "end": "2026-11-04T20:30:00"}
            assert len(moodle["gets"]) == 1 and sent == [], "18:40 is before the watch (10 min early)"

            # ✅ during the session: read every minute; check-in opens -> Present, Save changes, recorded
            tick("Wed 4 Nov 2026", 18, 55)
            tick("Wed 4 Nov 2026", 18, 55, 30)
            assert len(moodle["gets"]) == 2 and sent == [], "one read a minute, nothing open yet"
            open_check_in("Wed 4 Nov 2026")
            tick("Wed 4 Nov 2026", 19, 2)
            assert moodle["posts"] == [{"sessid": "55", "sesskey": "SECRETKEY1", "_qf__mod_attendance\\form\\studentattendance": "1",
                                        "mform_isexpanded_id_session": "1", "submitbutton": "Save changes",
                                        "status": "101"}], moodle["posts"]
            kind, text, rows, _ = sent[-1]
            assert kind == "png" and text.startswith("✅ *Checked In — Attendance Recorded*"), text
            assert "📘 *Course:* Big Data" in text and "Wed 4 Nov 2026 · 7PM - 8:30PM" in text
            assert "*Status:* Present · Self-recorded" in text and "🤖 I checked you in at" in text and _legacy_markdown_ok(text)
            assert rows == [[{"text": "📝 Open Attendance", "url": view}]] and "SECRETKEY1" not in text
            assert shots == [(view + "&view=5", "Wed 4 Nov 2026 7PM - 8:30PM")], "a screenshot of your row"
            kept = list(checkin.PAGES_DIR.glob("*.html"))
            assert len(kept) == 1 and "SECRETKEY1" not in kept[0].read_text(encoding="utf-8"), "kept, without the key"
            reads = len(moodle["gets"])
            tick("Wed 4 Nov 2026", 19, 10)
            assert len(moodle["gets"]) == reads and len(sent) == 1, "done: the session is not read again"

            # 🚨 a password: never submitted, told at once with the page; ✅ once you checked in yourself
            open_check_in("Wed 11 Nov 2026")
            tick("Wed 11 Nov 2026", 19, 1)
            kind, text, rows, _ = sent[-1]
            assert text.startswith("🚨 *Check In by Hand — Auto Check-in Failed*") and "password or a QR code" in text, text
            assert rows == [[{"text": "✍️ Check In by Hand", "url": view}]] and _legacy_markdown_ok(text)
            assert shots[-1] == (days["Wed 11 Nov 2026"][0].split('"')[1].replace("&amp;", "&"), None), "the form it saw"
            assert len(moodle["posts"]) == 1, "nothing was posted"
            tick("Wed 11 Nov 2026", 19, 3)
            assert len(sent) == 2 and not any(f"sessid={sessid['Wed 11 Nov 2026']}" in u for u in moodle["gets"][-1:]), \
                "one alert; the form is not tried again — only watched"
            days["Wed 11 Nov 2026"][:2] = ["Present", "Self-recorded"]
            tick("Wed 11 Nov 2026", 19, 5)
            assert sent[-1][1].startswith("✅") and "Moodle shows you as checked in." in sent[-1][1]

            # 🚨 Moodle takes the form but does not record it
            open_check_in("Wed 18 Nov 2026")
            tick("Wed 18 Nov 2026", 19, 1)
            assert "still does not show you as checked in" in sent[-1][1] and len(moodle["posts"]) == 2
            assert shots[-1] == (view + "&view=5", "Wed 18 Nov 2026 7PM - 8:30PM")

            # an activity day (ن): never touched, even when check-in opens there
            before, reads = len(sent), len(moodle["gets"])
            open_check_in("Mon 23 Nov 2026")
            tick("Mon 23 Nov 2026", 19, 5)
            tick("Mon 23 Nov 2026", 21, 5)
            assert len(sent) == before and len(moodle["posts"]) == 2, "no check-in, no message"
            assert all("view.php" in u for u in moodle["gets"][reads:]), "its form is never even opened"

            # ℹ️ an online lecture whose check-in never opened: silently, once
            tick("Wed 25 Nov 2026", 21, 5)
            tick("Wed 25 Nov 2026", 21, 30)
            kind, text, rows, silent = sent[-1]
            assert len(sent) == before + 1 and text.startswith("ℹ️ *No Check-in Opened*") and silent, sent[-1]
            assert _legacy_markdown_ok(text)

            # 🚨 the saved session has expired: a check signs in again; told after 10 minutes, once
            moodle["expired"] = True
            before = len(sent)
            tick("Wed 2 Dec 2026", 18, 55)
            tick("Wed 2 Dec 2026", 19, 0)
            assert launched == [1] and len(sent) == before, "one sign-in started; not told yet"
            tick("Wed 2 Dec 2026", 19, 5)
            kind, text, rows, _ = sent[-1]
            assert kind == "text" and "*Why:* My saved Moodle session has expired." in text and "Session:" not in text, text
            tick("Wed 2 Dec 2026", 19, 6)
            assert len(sent) == before + 1, "told once"
            moodle["expired"] = False
        finally:
            (checkin.STATE_FILE, checkin.CACHE_FILE, checkin.PAGES_DIR, checkin._http, checkin.screenshot,
             daily.CLAIM_DIR, schedule.load_schedule, notifier.send_message, notifier.send_png_file,
             probe.launch_check, probe.MONITOR_LOCK) = keep


def test_attendance_on_online_lecture_alert():
    """✍️ Check In rides on an online lecture's start alert — for whichever course it is,
    one button per attendance activity — and never on the hour-before heads-up."""
    import json as _json

    course = "Arabic Communication Skills"
    portal_name = "20261-الاتصال والتواصل باللغة العربية-ش31-3-ك"
    with tempfile.TemporaryDirectory() as tmp:
        keep = (lectures.STATE_FILE, lectures.CLAIM_DIR, lectures.notifier.send_message,
                lectures.notifier.send_png_file, lecturelinks.LINKS_FILE)
        lectures.STATE_FILE = Path(tmp) / "saved_items.json"
        lectures.CLAIM_DIR = Path(tmp) / "claims"
        lecturelinks.LINKS_FILE = Path(tmp) / "links.json"
        sent = []
        lectures.notifier.send_message = lambda token, chat, text, url=None, rows=None, **kw: sent.append(rows) or True
        lectures.notifier.send_png_file = lambda token, chat, png, name, caption="", url=None, rows=None: \
            sent.append(rows) or True
        try:
            def state(*cmids):
                lectures.STATE_FILE.write_text(_json.dumps({"content": {
                    f"8805-{c}": {"name": f"الحضور {c}", "type": "attendance", "course": portal_name, "cmid": str(c),
                                  "url": f"https://x/mod/attendance/view.php?id={c}"} for c in cmids}}), encoding="utf-8")
            state(75482)
            assert lectures.attendance_rows(course) == [[{"text": "✍️ Check In / تأكيد الحضور",
                                                          "url": "https://x/mod/attendance/view.php?id=75482"}]]
            assert lectures.attendance_rows("Big Data") == [], "only the course's own activities"
            # no meeting link yet: the alert still carries Check In
            assert lectures.send_due("t", "c", datetime(2026, 10, 14, 19, 0)) == 1
            assert sent[-1] == lectures.attendance_rows(course)
            # with a link: Join Meeting first, then Check In; the heads-up an hour before has neither
            lecturelinks.set_link(course, "https://teams.microsoft.com/l/meetup-join/19%3ax", "portal", "post")
            state(75482, 70299)
            assert lectures.send_due("t", "c", datetime(2026, 10, 21, 18, 0)) == 1 and sent[-1] is None
            assert lectures.send_due("t", "c", datetime(2026, 10, 21, 19, 0)) == 1
            texts = [b["text"] for row in sent[-1] for b in row]
            assert texts[0] == "🎥 Join Meeting" and [t for t in texts if t.startswith("✍️")] == [
                "✍️ Check In / تأكيد الحضور · الحضور 75482", "✍️ Check In / تأكيد الحضور · الحضور 70299"], texts
            lectures.STATE_FILE.unlink()
            assert lectures.attendance_rows(course) == [], "no saved state: no button, no crash"
        finally:
            (lectures.STATE_FILE, lectures.CLAIM_DIR, lectures.notifier.send_message,
             lectures.notifier.send_png_file, lecturelinks.LINKS_FILE) = keep
