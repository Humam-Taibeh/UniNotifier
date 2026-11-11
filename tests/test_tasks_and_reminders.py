"""What is due: deadlines, quizzes, the task list, the noon digest, reminders."""

from support import (_FIXTURES, _legacy_markdown_ok, _package)

import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from uninotifier import daily
from uninotifier import media
from uninotifier import moodle
from uninotifier import notifier
from uninotifier import schedule


def test_submission_status():
    ok = moodle.parse_submission_status
    assert ok("Submission status Submitted for grading Grading status Not graded")
    assert ok("حالة التسليم تم التسليم للتقييم")
    assert ok("", has_submitted_cell=True)
    assert not ok("Submission status No submissions have been made yet")
    assert not ok("Submission status Draft (not submitted)")
    assert not ok("")


def test_a_task_without_a_name_is_named_in_english():
    """Everything the bot itself says is English: a deadline whose event carries no name used to be
    listed as "(بدون اسم)" while every other nameless item is "Untitled"."""
    ev = [{"modulename": "assign", "eventtype": "due", "timesort": 2e9, "name": "  ",
           "action": {"url": "https://x/mod/assign/view.php?id=9"}, "course": {"fullname": "c"}}]
    assert moodle.update_deadlines({}, ev)["assign-9"]["name"] == moodle.UNTITLED == "Untitled"
    source = Path(moodle.__file__).read_text(encoding="utf-8")
    assert "بدون اسم" not in source, "a name the bot makes up is English"


def test_deadline_flag_and_reminders():
    ev = [{"modulename": "assign", "eventtype": "due", "timesort": 2e9, "name": "HW",
           "action": {"url": "https://x/mod/assign/view.php?id=7"}, "course": {"fullname": "c"}}]
    saved = moodle.update_deadlines({}, ev)
    saved["assign-7"]["submitted"] = True
    assert moodle.update_deadlines(saved, ev)["assign-7"]["submitted"] is True   # survives a refresh
    monitor = _package("uninotifier.monitor")
    sent = []
    monitor.send_telegram = lambda text, url=None: sent.append(text) or True
    soon = {"assign-7": {**saved["assign-7"], "timestamp": time.time() + 1800}}
    monitor.send_deadline_reminders(soon)
    assert sent == [], "reminder sent for a submitted assignment"
    soon["assign-7"]["submitted"] = False
    monitor.send_deadline_reminders(soon)
    # found half an hour before it is due: ONE message, the final reminder — not that one and a
    # "24 hours" reminder beside it — and it says what is really left, not "1 hour"
    assert len(sent) == 1 and "Final Reminder" in sent[0] and "Due in about 30 minutes." in sent[0], sent
    assert soon["assign-7"]["reminded_24h"] and soon["assign-7"]["reminded_1h"], "both windows are settled"
    monitor.send_deadline_reminders(soon)
    assert len(sent) == 1, "nothing is repeated"
    # the ordinary case is untouched: a day ahead, then an hour ahead
    later = {"assign-8": {**saved["assign-7"], "submitted": False, "timestamp": time.time() + 20 * 3600}}
    monitor.send_deadline_reminders(later)
    assert len(sent) == 2 and sent[1].startswith("⏰ *Reminder*") and not later["assign-8"].get("reminded_1h")
    later["assign-8"]["timestamp"] = time.time() + 3500
    monitor.send_deadline_reminders(later)
    assert len(sent) == 3 and "Due in 1 hour." in sent[2], sent[2]
    # a message that fails leaves both flags unset: the next check tries again
    monitor.send_telegram = lambda text, url=None: False
    failed = {"assign-9": {**saved["assign-7"], "submitted": False, "timestamp": time.time() + 1800}}
    monitor.send_deadline_reminders(failed)
    assert not failed["assign-9"].get("reminded_24h") and not failed["assign-9"].get("reminded_1h")


def test_quizzes_keep_their_opening_and_closing_times():
    """This Moodle lists a quiz among the action events by its CLOSE event only (a quiz
    used to get no date and no reminder at all); its opening time comes from the
    upcoming-events view. Both land on one entry, whose reminders count down to the
    opening — or to the close while that is all there is. A quiz page that lists an
    attempt of yours, or a submitted assignment, ends the task; one without a date is
    read too."""
    from uninotifier.monitor import scan
    url = "https://x/mod/quiz/view.php?id=9"
    close = {"modulename": "quiz", "eventtype": "close", "timesort": 2_000_003_600, "name": "Quiz 4 closes",
             "instance": 77, "action": {"url": url, "name": "Attempt quiz now"}, "course": {"fullname": "c"}}
    opening = {"modulename": "quiz", "eventtype": "open", "timestart": 2_000_000_000, "name": "Quiz 4 opens",
               "instance": 77, "course": {"fullname": "c"}}
    calls, actions = [], {"events": [close]}

    def fake_ajax(context, sesskey, method, args):
        calls.append(method)
        if method == "core_calendar_get_action_events_by_timesort":
            return {"data": actions}
        return {"data": {"events": [opening, {**opening, "instance": 78},
                                    {"modulename": "attendance", "eventtype": "attendance", "instance": 77}]}}
    keep = moodle.ajax_call
    moodle.ajax_call = fake_ajax
    try:
        events = moodle.fetch_deadline_events(None, "key")
        assert [e["eventtype"] for e in events] == ["close", "open"] and events[1]["url"] == url, events
        actions["events"], calls[:] = [], []
        assert moodle.fetch_deadline_events(None, "key") == [] and len(calls) == 1, "no quiz, no second call"
    finally:
        moodle.ajax_call = keep

    saved = moodle.update_deadlines({}, [close])
    q = saved["quiz-9"]
    assert (q["opens"], q["closes"], q["timestamp"], q["event"], q["name"]) == (
        None, 2_000_003_600, 2_000_003_600, "close", "Quiz 4"), q
    q["reminded_24h"] = True
    q = moodle.update_deadlines(saved, events)["quiz-9"]
    assert (q["opens"], q["closes"], q["timestamp"], q["event"]) == (2_000_000_000, 2_000_003_600, 2_000_000_000, "open")
    assert q["reminded_24h"] is False, "the reminder sent before the close was about another moment"
    q["reminded_24h"] = True
    again = moodle.update_deadlines({"quiz-9": q}, [close])["quiz-9"]       # the upcoming view failed this time
    assert again["opens"] == 2_000_000_000 and again["reminded_24h"] is True
    assert moodle.quiz_times({"type": "quiz", "timestamp": 5}) == (5, None), "an entry written before"
    assert moodle.update_deadlines({}, [{**close, "eventtype": "due"}]) == {}
    hw = moodle.update_deadlines({}, [{"modulename": "assign", "eventtype": "due", "timesort": 2e9, "name": "HW 2 is due",
                                       "action": {"url": "https://x/mod/assign/view.php?id=4"}}])
    assert hw["assign-4"]["name"] == "HW 2", "the activity's own name, not the calendar's sentence"

    rem = media.build_reminder_message(q, "1h")
    assert "*Opens:*" in rem and "*Closes:*" in rem and "Opens in 1 hour." in rem, rem
    closing = media.build_reminder_message({**q, "opens": None, "timestamp": 2_000_003_600, "event": "close"}, "24h")
    assert "*Opens:*" not in closing and "Closes in 24 hours." in closing, closing
    item = {"name": "Quiz 4", "type": "quiz", "course": "20261-Big Data-AllSec-3-", "url": url, "cmid": "9"}
    assert "*Opens:*" in media.build_item_message(item, q) and "*Closes:*" in media.build_item_message(item, q)

    assert moodle.quiz_attempted('<div class="box quizinfo"><p>Attempts allowed: 1</p></div>') is False
    assert moodle.quiz_attempted('<div class="quizinfo"></div><table class="generaltable quizattemptsummary">'
                                 '<tr><td>Finished</td></tr></table>') is True
    assert moodle.quiz_attempted("<html><body><p>Log in</p></body></html>") is None

    def assign(cmid, **extra):
        return {"name": f"HW {cmid}", "type": "assign", "course": "c", "url": f"https://x/mod/assign/view.php?id={cmid}",
                "cmid": str(cmid), **extra}
    content = {"c-9": item, "c-4": assign(4), "c-5": assign(5), "c-6": {**assign(6), "done": True}}
    deadlines = {"quiz-9": dict(q), "assign-4": {"type": "assign", "timestamp": time.time() - 60}}
    read = []
    keep = (moodle.fetch_quiz_attempted, moodle.fetch_submission_status)
    moodle.fetch_quiz_attempted = lambda http, u: read.append(u) or True
    moodle.fetch_submission_status = lambda http, u: read.append(u) or False
    try:
        saved_state = {"content": dict(content)}
        found = scan.check_submissions(None, content, deadlines, saved_state)
    finally:
        moodle.fetch_quiz_attempted, moodle.fetch_submission_status = keep
    assert sorted(read) == sorted([url, assign(5)["url"]]), "a past deadline and a task marked done are not read"
    assert found == {"quiz-9": True, "assign-5": False} and deadlines["quiz-9"]["submitted"] is True
    assert saved_state["undated"] == {"assign-5": False}, "an undated task's status is kept beside the deadlines"

    # a reminder sent late (the quiz was published 9 hours before it opens) says the real time left
    soon = media.build_reminder_message({**q, "opens": time.time() + 9 * 3600, "timestamp": time.time() + 9 * 3600}, "24h")
    assert "Opens in about 9 hours." in soon, soon

    # Moodle 5 lists attempts as cards (quizreviewsummary), with the mark on the page
    finished = ('<div role="main"><div class="box py-3 quizinfo"><p>Time limit: 20 mins</p></div>'
                '<div id="feedback" class="box generalbox"><h3>Your final grade for this quiz is 4.50/5.00.</h3></div>'
                '<table class="table generaltable generalbox quizreviewsummary"><tr><th>Status</th><td>Finished</td></tr>'
                '<tr><th>Grade</th><td><b>4.50</b> out of 5.00 (<b>90</b>%)</td></tr></table></div>')
    assert moodle.quiz_attempted(finished) is True and moodle.task_grade(finished) == "4.5/5"
    two_attempts = ('<div role="main"><div class="quizinfo"></div><table class="quizreviewsummary">'
                    '<tr><th>Grade</th><td>2.00 out of 5.00</td></tr></table><table class="quizreviewsummary">'
                    '<tr><th>Grade</th><td>4.00 out of 5.00</td></tr></table></div>')
    assert moodle.task_grade(two_attempts) == "4/5", "without the final-grade line: the best attempt"
    graded = ('<div role="main"><table class="generaltable"><tr><th>Grading status</th><td>Graded</td></tr></table>'
              '<table class="generaltable"><tr><th>Grade</th><td>80.00 / 100.00</td></tr></table></div>')
    ungraded = '<div role="main"><table class="generaltable"><tr><th>Grading status</th><td>Not graded</td></tr></table></div>'
    assert moodle.task_grade(graded) == "80/100" and moodle.task_grade(ungraded) is None
    assert "🏆 *Grade:* *4.5/5* (90%)" in media.build_grade_message(item, "4.5/5")

    # check_grades: a closed quiz is read once it has opened; its mark is returned until it is told
    now = time.time()
    closed = {"type": "quiz", "opens": now - 7200, "closes": now - 3600, "timestamp": now - 7200, "event": "open"}
    ahead = {"type": "quiz", "opens": now + 7200, "closes": now + 9000, "timestamp": now + 7200, "event": "open"}
    later = {**item, "url": "https://x/mod/quiz/view.php?id=10", "cmid": "10"}
    content = {"c-9": item, "c-10": later, "c-4": assign(4), "c-5": assign(5)}
    deadlines = {"quiz-9": dict(closed), "quiz-10": dict(ahead),
                 "assign-4": {"type": "assign", "timestamp": now + 86400, "submitted": True}}
    pages = {url: (True, "5/5"), assign(4)["url"]: (None, None)}
    read = []
    keep = moodle.fetch_task_page
    moodle.fetch_task_page = lambda http, u: read.append(u) or pages[u]
    try:
        saved_state = {"content": dict(content)}
        fresh = scan.check_grades(None, content, deadlines, saved_state)
        assert sorted(read) == sorted([url, assign(4)["url"]]), "not a quiz still ahead, nor an open assignment not handed in"
        assert list(fresh) == ["quiz-9"] and saved_state["grades"]["quiz-9"]["grade"] == "5/5"
        assert deadlines["quiz-9"]["submitted"] is True, "the attempt is seen after closing too"
        read.clear()
        assert list(scan.check_grades(None, content, deadlines, saved_state)) == ["quiz-9"] and read == [], \
            "an untold mark stays owed; nothing is read again within the half hour"
        saved_state["grades"]["quiz-9"]["told"] = "5/5"
        assert scan.check_grades(None, content, deadlines, saved_state) == {}
    finally:
        moodle.fetch_task_page = keep


def test_open_tasks_and_the_noon_digest():
    """Every open task — assignments, quizzes, Cambridge One — in the ⏳ screen and in a
    message every day at noon until it is done: anything due within a day comes first,
    loudly; a quiz that has not opened yet waits; nothing open, no message; once a day."""
    from datetime import datetime
    from uninotifier import cambridge, tasks
    bot = _package("uninotifier.bot")
    noon = datetime(2030, 3, 4, 12, 0)
    h, day = 3600, 86400

    def dl(kind, cmid, name, **fields):
        return {"type": kind, "name": name, "course": "20261-Big Data-AllSec-3-",
                "url": f"https://x/mod/{kind}/view.php?id={cmid}", **fields}

    def at(t):                  # the same tasks around the moment t
        return {
            "deadlines": {
                "assign-1": dl("assign", 1, "Report 1", timestamp=t + 20 * h),
                "assign-2": dl("assign", 2, "Project", timestamp=t + 6 * day),
                "assign-3": dl("assign", 3, "Handed in", timestamp=t + 2 * day, submitted=True),
                "assign-8": dl("assign", 8, "Too late", timestamp=t - h),
                "quiz-9": dl("quiz", 9, "Quiz 9", opens=t + 3 * day, closes=t + 3 * day + h, timestamp=t + 3 * day, event="open"),
                "quiz-10": dl("quiz", 10, "Quiz 10", opens=t - h, closes=t + 5 * h, timestamp=t - h, event="open"),
                "quiz-11": dl("quiz", 11, "Old quiz", timestamp=t - 2 * day),
            },
            "content": {
                "c-1": {**dl("assign", 1, "Report 1"), "cmid": "1"},
                "c-5": {**dl("assign", 5, "Reading notes"), "cmid": "5"},
                "c-6": {**dl("assign", 6, "Marked done"), "cmid": "6", "done": True},
                "c-7": {**dl("quiz", 7, "Practice quiz"), "cmid": "7"},
                "c-12": {**dl("resource", 12, "Slides"), "cmid": "12"},
            },
            "undated": {"quiz-7": True},
        }
    t = noon.timestamp()
    state = at(t)
    feed = json.loads((_FIXTURES / "cambridge_assignment.json").read_text(encoding="utf-8"))
    ident = cambridge.parse(feed)[0]["id"]
    with tempfile.TemporaryDirectory() as tmp:
        keep = (cambridge.active, cambridge.SEEN_FILE, cambridge.FEED_FILE, cambridge.assignments_url,
                daily.CLAIM_DIR, notifier.send_message, bot.load_state)
        cambridge.active, cambridge.assignments_url = (lambda today=None: True), (lambda: "https://cambridge.example/a")
        cambridge.SEEN_FILE, cambridge.FEED_FILE = Path(tmp) / "c.json", Path(tmp) / "f.json"
        cambridge.SEEN_FILE.write_text(json.dumps({"seen": {
            ident: {"title": "Assignment 1", "due": t + 3 * day - h, "submitted": False},
            "done": {"title": "Assignment 0", "due": t + day, "submitted": True}}}), encoding="utf-8")
        cambridge.FEED_FILE.write_text(json.dumps(feed["assignmentsData"]), encoding="utf-8")
        daily.CLAIM_DIR = Path(tmp) / "claims"
        try:
            found = tasks.open_tasks(state, t)
            assert [x["name"] for x in found] == ["Quiz 10", "Report 1", "Assignment 1", "Quiz 9", "Project",
                                                  "Reading notes"], found
            assert found[2]["progress"] == "0 of 15 activities done — 15 left" and found[2]["kind"] == "cambridge"

            text, rows = tasks.digest(found, t)
            assert _legacy_markdown_ok(text), text
            last_day, still = text.split("⏳ *Still open*")
            assert "LAST DAY" in last_day and "Quiz 10" in last_day and "Report 1" in last_day and "Project" in still
            assert "Quiz 9" not in text, "a quiz that has not opened yet waits for its own reminders"
            assert "*Closes:*" in last_day and "5h 0m left" in last_day and "20h 0m left" in last_day, last_day
            assert "No due date set" in still and "0 of 15 activities done — 15 left" in still
            assert rows[0][0]["text"] == "🔗 Open Task 1 · Quiz 10" and rows[2][0]["url"] == "https://cambridge.example/a"
            assert len(rows) == 5, "every task with a link has a button (the 5 that can be done today)"
            assert tasks.digest([], t) is None and tasks.digest([found[3]], t) is None, "nothing to do today: silence"

            sent, ok = [], {"v": True}
            notifier.send_message = lambda token, chat, text, url=None, rows=None, silent=False: \
                sent.append((text, rows)) or ok["v"]
            assert not tasks.send_digest("t", "c", state, datetime(2030, 3, 4, 11, 59)) and not sent, "before noon"
            ok["v"] = False
            assert not tasks.send_digest("t", "c", state, noon) and len(sent) == 1
            ok["v"] = True
            assert tasks.send_digest("t", "c", state, datetime(2030, 3, 4, 12, 0, 20)), "a failed send is tried again"
            assert not tasks.send_digest("t", "c", state, datetime(2030, 3, 4, 18, 0)), "bot + monitor: once a day"
            assert tasks.send_digest("t", "c", state, datetime(2030, 3, 5, 12, 0)), "and again the next day"
            assert sent[-1][0].startswith("📋 *Your Open Tasks*")

            bot.load_state = lambda: at(time.time())     # the screen counts from the real clock
            screen, buttons = bot.view_tasks()
            assert "Course tasks" in screen and "Quiz 9" in screen and "*Opens:*" in screen, screen
            assert "Assignment 1" in screen and "Old quiz" not in screen and "Practice quiz" not in screen, screen
            assert _legacy_markdown_ok(screen)
        finally:
            (cambridge.active, cambridge.SEEN_FILE, cambridge.FEED_FILE, cambridge.assignments_url,
             daily.CLAIM_DIR, notifier.send_message, bot.load_state) = keep

    import inspect
    from uninotifier.monitor import run
    assert "tasks.send_digest" in inspect.getsource(bot.lecture_loop), "the bot's clock sends it at noon"
    assert "tasks.send_digest" in inspect.getsource(run.run), "a check sends it when the bot could not"


def test_reminders_list_is_tidy():
    """A reminder in the list is its title, then the course and the time on lines of their own: an
    Arabic note never runs into an English course name, and a pinned post is not poured into the list."""
    from uninotifier.bot import views
    arabic = views.reminder_lines(1, {"note": "امتحان قصير / شعبة 2", "course": "Technical Writing", "due": 1900000000})
    assert len(arabic) == 3 and arabic[0].endswith(views.PDI) and views.FSI + "امتحان قصير / شعبة 2" in arabic[0]
    assert "Technical Writing" in arabic[1] and "Technical Writing" not in arabic[0] and "🕐" in arabic[2]
    post = views.reminder_lines(2, {"note": "Exam Reminder — Technical Writing\nCourse: Technical Writing\nRoom 12",
                                    "course": None, "due": 1900000000})
    assert len(post) == 2 and "Room 12" not in "".join(post), "only the first line of a long note"
    same = views.reminder_lines(3, {"note": "Technical Writing quiz", "course": "Technical Writing", "due": 1900000000})
    assert len(same) == 2, "a course already in the title is not said twice"
    assert all(line.startswith(views.LRM) for line in arabic + post + same)


def test_an_overdue_assignment_that_still_accepts_work_is_a_task():
    """A due date left over from last year's copy of a course: the assignment is overdue the day it
    appears, yet Moodle still shows "Add submission". It is read, listed as a task until it is
    handed in, marked done or closed, and one handed in late stops being "pending"."""
    from uninotifier import tasks
    scan = _package("uninotifier.monitor").scan
    table = ('<div class="submissionstatustable"><table><tr><td>Submission status</td><td>{status}</td></tr></table></div>')
    button = ('<div class="singlebutton"><form method="get" action="https://x/mod/assign/view.php">'
              '<input type="hidden" name="id" value="5"><input type="hidden" name="action" value="editsubmission">'
              '<button type="submit">Add submission</button></form></div>')
    open_page = "<html><body>" + table.format(status="No submissions have been made yet") + button + "</body></html>"
    closed_page = "<html><body>" + table.format(status="No submissions have been made yet") + "</body></html>"
    done_page = "<html><body>" + table.format(status="Submitted for grading") + button + "</body></html>"
    assert moodle.read_submission(open_page) == {"submitted": False, "accepting": True}
    assert moodle.read_submission(closed_page) == {"submitted": False, "accepting": False}
    assert moodle.read_submission(done_page) == {"submitted": True, "accepting": True}
    assert moodle.read_submission("<html><body><p>Error</p></body></html>") is None

    now = time.time()
    year = 365 * 86400

    def entry(cmid, name, ts, **more):
        return {"name": name, "course": "20261-Technical Writing-AllSec-3-", "type": "assign", "timestamp": ts,
                "url": f"https://x/mod/assign/view.php?id={cmid}", "submitted": False, **more}

    deadlines = {"assign-5": entry(5, "Activity 4", now - year), "assign-6": entry(6, "Activity 5", now - year),
                 "assign-7": entry(7, "Activity 1", now - year), "assign-8": entry(8, "Old and closed", now - year, late_open=False),
                 "assign-9": entry(9, "Ahead", now + 86400)}
    content = {f"c-{n}": {"type": "assign", "name": f"a{n}", "url": f"https://x/mod/assign/view.php?id={n}", "done": False}
               for n in (5, 6, 7, 8, 9)}
    pages = {"5": open_page, "6": closed_page, "7": done_page}
    read = []
    keep = moodle.fetch_submission_state
    moodle.fetch_submission_state = lambda http, url: read.append(url[-1]) or moodle.read_submission(pages[url[-1]])
    try:
        assert scan.check_overdue(None, content, deadlines) == ["assign-5"]
        assert sorted(read) == ["5", "6", "7"], "not the one known to be closed, nor one with its date ahead"
        assert deadlines["assign-5"]["late_open"] is True and deadlines["assign-6"]["late_open"] is False
        assert deadlines["assign-7"]["submitted"] is True, "handed in after the date: no longer pending"
        read.clear()
        scan.check_overdue(None, content, deadlines)
        assert read == ["5"], "only the one still open is read again"
    finally:
        moodle.fetch_submission_state = keep

    state = {"deadlines": deadlines, "content": content}
    listed = tasks.open_tasks(state, now)
    assert [(t["name"], t["late"]) for t in listed] == [("Activity 4", True), ("Ahead", False)]
    lines = tasks.task_lines(1, listed[0], now)
    assert any("Overdue — Moodle still accepts it" in line for line in lines) and not any("left" in line for line in lines)
    text, _ = tasks.digest(listed, now)
    assert text.index("LAST DAY") < text.index("Ahead") < text.index("Overdue — still accepted") < text.index("Activity 4")
    content["c-5"]["done"] = True
    assert [t["name"] for t in tasks.open_tasks(state, now)] == ["Activity 4", "Ahead"], \
        "Moodle's done mark (opening the page can set it) never ends an assignment that is not handed in"

    # closed while it was not handed in: told once, and off the list
    timed = _package("uninotifier.monitor").timed
    alerts_module = _package("uninotifier.monitor").alerts
    pages["5"] = closed_page
    keep_state, keep_send = moodle.fetch_submission_state, alerts_module.send_telegram
    keep_exists = moodle.activity_exists
    told = []
    moodle.fetch_submission_state = lambda http, url: moodle.read_submission(pages[url[-1]])
    alerts_module.send_telegram = lambda text, url=None: told.append((text, url)) or True
    try:
        assert scan.check_overdue(None, content, deadlines) == [] and deadlines["assign-5"]["notice"] == "closed"
        assert [t["name"] for t in tasks.open_tasks(state, now)] == ["Ahead"]
        timed.send_assignment_notices(deadlines)
        timed.send_assignment_notices(deadlines)
        assert len(told) == 1 and "Assignment Closed — Not Submitted" in told[0][0] and "Activity 4" in told[0][0]
        assert told[0][1] == deadlines["assign-5"]["url"] and deadlines["assign-5"]["notice"] == "told"
        # one that closed long ago and was never seen open is not news
        deadlines["assign-10"] = entry(10, "Ancient", now - year)
        content["c-10"] = {"type": "assign", "name": "a10", "url": "https://x/mod/assign/view.php?id=10", "done": False}
        pages["0"] = closed_page
        scan.check_overdue(None, content, deadlines)
        assert deadlines["assign-10"]["late_open"] is False and "notice" not in deadlines["assign-10"]
        # ... but one whose date has only just passed is
        deadlines["assign-11"] = entry(11, "Yesterday", now - 3600)
        content["c-11"] = {"type": "assign", "name": "a11", "url": "https://x/mod/assign/view.php?id=11", "done": False}
        pages["1"] = closed_page
        scan.check_overdue(None, content, deadlines)
        assert deadlines["assign-11"]["notice"] == "closed"

        # removed from the course while still to do: asked for by its own address, told once, forgotten
        del content["c-9"]
        asked = []
        moodle.activity_exists = lambda http, url: asked.append(url) or None
        scan.check_overdue(None, content, deadlines)
        assert asked == ["https://x/mod/assign/view.php?id=9"] and "notice" not in deadlines["assign-9"], \
            "unreachable is not removed"
        moodle.activity_exists = lambda http, url: False
        scan.check_overdue(None, content, deadlines)
        assert deadlines["assign-9"]["notice"] == "removed" and tasks.open_tasks(state, now) == []
        told.clear()
        timed.send_assignment_notices(deadlines)
        removed = [text for text, url in told if "Assignment Removed" in text]
        assert len(removed) == 1 and "Ahead" in removed[0] and "assign-9" not in deadlines

        # no date at all and marked done: read, so "done" alone does not hide it
        loose = {"c-20": {"type": "assign", "name": "Loose", "url": "https://x/mod/assign/view.php?id=20", "done": True}}
        undated: dict = {}
        loose_state = {"deadlines": {}, "content": loose, "undated": undated}
        assert tasks.open_tasks(loose_state, now) == [], "not read yet: nothing is claimed"
        pages["0"] = open_page
        scan.check_overdue(None, loose, {}, undated)
        assert undated == {"assign-20": False} and [t["name"] for t in tasks.open_tasks(loose_state, now)] == ["Loose"]
        pages["0"] = done_page
        scan.check_overdue(None, loose, {}, undated)
        assert undated == {"assign-20": True} and tasks.open_tasks(loose_state, now) == []
    finally:
        moodle.fetch_submission_state, alerts_module.send_telegram = keep_state, keep_send
        moodle.activity_exists = keep_exists
    deadlines["assign-5"].update(late_open=True, notice="told")
    # the flag survives the next calendar read
    merged = moodle.update_deadlines(deadlines, [{"modulename": "assign", "eventtype": "due", "timesort": int(now - year),
                                                  "name": "Activity 4 is due", "course": {"fullname": "c"},
                                                  "action": {"url": "https://x/mod/assign/view.php?id=5"}}])
    assert merged["assign-5"]["late_open"] is True
    assert "already passed" in media.deadline_lines(deadlines["assign-5"])[0]
    assert "already passed" not in media.deadline_lines(entry(9, "Ahead", now + 86400))[0]


def test_claims_are_exclusive_and_old_ones_are_forgotten():
    """A claim is True once per key (the bot and the monitor never both send), can be given
    back when nothing was delivered, and markers older than a week are pruned."""
    from uninotifier import daily
    with tempfile.TemporaryDirectory() as tmp:
        keep = daily.CLAIM_DIR
        daily.CLAIM_DIR = Path(tmp) / "daily_sent"
        try:
            assert daily.claim("k") and not daily.claim("k") and daily.claimed("k")
            daily.release("k")
            assert not daily.claimed("k") and daily.claim("k"), "given back: the next one may try again"
            old, fresh = daily._marker("old"), daily._marker("fresh")
            old.write_text("")
            fresh.write_text("")
            week_and_a_bit = time.time() - (daily.KEEP_DAYS * 86400 + 60)
            os.utime(old, (week_and_a_bit, week_and_a_bit))
            daily.prune()
            assert not old.exists() and fresh.exists() and daily.claimed("k")
            daily.prune()                                   # nothing to do: still fine
        finally:
            daily.CLAIM_DIR = keep
        daily.CLAIM_DIR = Path(tmp) / "never_made"
        try:
            daily.prune()                                   # no folder yet: no error
        finally:
            daily.CLAIM_DIR = keep


def test_tasks_and_reminders_planner():
    """Upcoming Tasks & Reminders: deadlines with 🔗 Open Task, personal reminders
    (➕ typed, 📌 from a post) scheduled by buttons, delivered once at the minute;
    Monday stays an activity — no lecture alerts."""
    from datetime import timedelta

    bot = _package("uninotifier.bot")
    monitor = _package("uninotifier.monitor")
    from uninotifier import reminders

    sch = schedule.load_schedule()
    # Monday is an activity: no lecture alert or Join button, Wednesday's online lecture keeps them
    for hm in ((18, 0), (19, 0)):
        assert schedule.due_messages(sch, datetime(2026, 10, 19, *hm)) == [], "Monday"
    assert [k for _, k, _, _ in schedule.due_messages(sch, datetime(2026, 10, 21, 19, 0))] == ["start"]

    # when: next campus day, before the next lecture
    tue = datetime(2026, 10, 13, 18, 0)
    # campus days are Sunday–Wednesday only: Saturday's labs and Wednesday's online lecture don't count
    assert reminders.next_campus_day(tue, sch) == datetime(2026, 10, 18, 8, 30), "Sunday, not Saturday's lab"
    assert reminders.next_campus_day(datetime(2026, 10, 17, 7, 30), sch) == datetime(2026, 10, 18, 8, 30)
    assert reminders.next_campus_day(datetime(2026, 10, 18, 9, 0), sch) == datetime(2026, 10, 20, 8, 30), "then Tuesday"
    # before next lecture: in-person lectures only (Tuesday's Big Data slot is an activity)
    assert reminders.before_next_lecture("Big Data", tue, sch) == (datetime(2026, 10, 18, 10, 30), "Big Data")
    assert reminders.before_next_lecture("Big Data", datetime(2026, 10, 18, 12, 0), sch)[0] == \
        datetime(2026, 10, 25, 10, 30), "not Tuesday's activity slot"
    assert reminders.before_next_lecture("Arabic Communication Skills", tue, sch) == \
        (datetime(2026, 10, 18, 9, 0), "Linear Algebra (1)"), "an online-only course: the next class on campus"
    assert reminders.before_next_lecture(None, tue, sch)[0] == datetime(2026, 10, 18, 9, 0), "any course"
    bot._reminder_draft.update(note="x", course="Arabic Communication Skills")
    assert "has no in-person lecture" in bot.view_when(bot._reminder_draft)[0]
    bot._reminder_draft.clear()
    for text, hm in (("18:45", (18, 45)), ("6:45 pm", (18, 45)), ("6PM", (18, 0)), ("12 am", (0, 0)), ("9", (9, 0))):
        assert reminders.parse_time(text) == hm, text
    for bad in ("25:00", "abc", "6:75"):
        try:
            reminders.parse_time(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    labels = [label for label, _ in reminders.day_choices(datetime(2026, 10, 13, 10, 0))]
    assert labels[:3] == ["Today", "Tomorrow", "Next Sunday"] and "Thu 15" in labels and len(labels) == 9
    assert reminders.day_choices(datetime(2026, 10, 13, 23, 0))[0][0] == "Tomorrow"
    assert reminders.when_text(datetime(2026, 10, 14, 18, 0).timestamp()) == "Wed 14 Oct · 6:00 PM"

    with tempfile.TemporaryDirectory() as tmp:
        keep = (reminders.STATE_FILE, reminders.SOURCES_FILE, reminders.CLAIM_DIR)
        reminders.STATE_FILE, reminders.SOURCES_FILE = Path(tmp) / "r.json", Path(tmp) / "s.json"
        reminders.CLAIM_DIR = Path(tmp) / "sent"
        sent, views, edits, answers = [], [], [], []
        orig = (reminders.notifier.send_message, bot.send_view, bot.edit_view, bot.api, bot.load_state)
        replies = []
        reminders.notifier.send_message = lambda token, chat, text, url=None, rows=None, silent=False, reply_to=None: \
            sent.append((text, rows)) or replies.append(reply_to) or True
        bot.send_view = lambda text, rows: views.append((text, rows)) or {"message_id": 1}
        bot.edit_view = lambda mid, text, rows: edits.append((text, rows))
        api_calls, api_ok = [], {"v": False}

        def fake_api(method, **data):
            answers.append((method, data.get("text")))
            api_calls.append((method, data))
            return {"message_id": 99} if api_ok["v"] and method == "sendMessage" else None
        bot.api = fake_api
        try:
            # delivery: once, at the minute; snooze makes it a new delivery
            item = reminders.add("Buy the textbook", time.time() - 5, "Big Data", "https://x/post")
            assert reminders.send_due("t", "c") == 1 and reminders.send_due("t", "c") == 0
            text, rows = sent[-1]
            assert text.startswith("⏰ *Reminder*") and "Buy the textbook" in text and _legacy_markdown_ok(text)
            assert rows[0][0] == {"text": "🌐 Open on Moodle", "url": "https://x/post"}
            assert replies[-1] is None, "a typed reminder quotes nothing"
            assert [b["callback_data"] for b in rows[1]] == [f"rem:z:{item['id']}", f"rem:done:{item['id']}"]
            assert reminders.pending() == [], "delivered: no longer upcoming"
            reminders.snooze(item["id"], now=time.time() - 3600 - 60)   # due a minute ago: a new delivery
            assert reminders.send_due("t", "c") == 1, "snoozed: sent again"

            # 📌 under a forum post: the monitor files the source, the button carries a short key
            delivered = []
            keep_deliver = monitor.deliver
            monitor.deliver = lambda header, body="", url=None, rows=None, shots=(), shot_name="", document=None: \
                delivered.append(rows) or True
            try:
                forum = {"name": "Announcements", "course": "20261-Big Data-AllSec-3-", "url": "https://x/f"}
                post = {"id": "101", "subject": "Install Hadoop before Sunday", "author": "Dr", "time": 1,
                        "html": "<p>x</p>", "is_reply": False, "url": "https://x/d#p101", "attachments": []}
                assert monitor.notify_forum_post(forum, post)
            finally:
                monitor.deliver = keep_deliver
            assert delivered[-1] == [[{"text": "📌 Add to Reminders", "callback_data": "rem:s:p101"}]]
            assert reminders.source("p101") == {**reminders.source("p101"), "note": "Install Hadoop before Sunday",
                                                "course": "Big Data", "url": "https://x/d#p101"}

            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
            press = iter(range(10 ** 6))                       # every press has its own id, as on Telegram
            cb = lambda data, mid=5: bot.handle_callback({"id": f"p{next(press)}", "data": data, "message": {"message_id": mid}})
            cb("rem:s:p101")
            text, rows = views[-1]
            assert text.startswith("📌 *Add to Reminders*") and "Install Hadoop" in text and "Big Data" in text
            assert [r[0]["callback_data"] for r in rows] == ["rem:q:campus", "rem:q:lecture", "rem:q:custom", "rem:cancel"]
            assert rows[0][0]["text"].startswith("🏫 Next Campus Day · ") and rows[1][0]["text"].startswith("⏰ Before Next Lecture · ")
            cb("rem:q:custom")
            assert edits[-1][0].startswith("🗓️ *Pick a day*")
            cb(f"rem:day:{tomorrow}")
            assert edits[-1][0].startswith("🕐 *Pick a time") and edits[-1][1][0][0]["callback_data"] == f"rem:hr:{tomorrow}:0800"
            cb(f"rem:hr:{tomorrow}:1800")
            assert edits[-1][0].startswith("✅ *Reminder set*")
            mine = reminders.pending()
            assert len(mine) == 1 and mine[0]["note"] == "Install Hadoop before Sunday" and mine[0]["url"] == "https://x/d#p101"
            assert mine[0]["message_id"] == 5, "remembers the chat message the 📌 sat under"
            assert datetime.fromtimestamp(mine[0]["due"]).strftime("%Y%m%d %H:%M") == f"{tomorrow} 18:00"
            cb("rem:s:p101")
            cb("rem:q:lecture")
            assert edits[-1][0].startswith("✅ *Reminder set*") and len(reminders.pending()) == 2
            cb("rem:q:campus")
            assert answers[-1] == ("answerCallbackQuery", "⌛ That reminder draft expired — start again."), "used up"
            cb("rem:s:p999")
            assert "too old" in answers[-1][1]

            # ➕ typed note -> typed time
            bot.handle_command("/remind")
            assert views[-1][0].startswith("➕ *New Custom Reminder*") and bot._reminder_wait == "note"
            bot.handle_command("اشتري كتاب كلية الآداب طابق 1")
            assert views[-1][0].startswith("📌 *Add to Reminders*") and "كلية الآداب" in views[-1][0]
            cb("rem:q:custom")
            cb(f"rem:type:{tomorrow}")
            assert bot._reminder_wait == "time"
            bot.handle_command("25:99")
            assert views[-1][0].startswith("⚠️") and bot._reminder_wait == "time", "a bad time keeps the question"
            bot.handle_command("6:45 pm")
            assert views[-1][0].startswith("✅ *Reminder set*") and bot._reminder_wait is None
            assert len(reminders.pending()) == 3
            bot.handle_command("/remind")
            cb("rem:cancel")
            assert bot._reminder_wait is None and edits[-1][0].startswith("✖️")

            # the planner view: deadlines with Open Task, reminders, ➕ and 🗑️
            bot.load_state = lambda: {"deadlines": {"assign-1": {
                "name": "Assignment 2 — ER Diagram", "type": "assign", "course": "20261-Databases-AllSec-3-F",
                "url": "https://x/mod/assign/view.php?id=1", "timestamp": time.time() + 86400 * 2}}}
            text, rows = bot.view_tasks()
            assert "Course tasks" in text and "Assignment 2" in text and "Your reminders" in text
            assert "كلية الآداب" in text and _legacy_markdown_ok(text)
            assert rows[0][0]["text"].startswith("🔗 Open Task 1 · Assignment 2") and rows[0][0]["url"].endswith("id=1")
            views_btns = [r[0] for r in rows if r[0].get("callback_data", "").startswith("rem:view:")]
            pinned = [r for r in reminders.pending() if r.get("message_id")]
            assert len(views_btns) == len(pinned) == 2, "one per pinned reminder; the typed one has none"
            assert views_btns[0]["text"].startswith("🔗 View Original Post · Install Hadoop")
            assert [b["callback_data"] for b in rows[3]] == ["rem:new", "rem:list"]

            # 🔗 View Original Post: a reply quoting the pinned message (tap the quote to jump)
            api_ok["v"] = True
            cb(views_btns[0]["callback_data"])
            method, data = api_calls[-2]
            assert method == "sendMessage" and data["reply_parameters"] == {"message_id": 5}
            assert data["text"].startswith("📌 *Original post*") and "Install Hadoop" in data["text"]
            # ...and if that message was deleted, the Moodle link instead
            api_ok["v"] = False
            cb(views_btns[0]["callback_data"])
            assert views[-1][0].startswith("⚠️ The original message was deleted")
            assert views[-1][1][0][0] == {"text": "🌐 Open on Moodle", "url": "https://x/d#p101"}
            typed = next(r for r in reminders.pending() if not r.get("message_id"))
            cb(f"rem:view:{typed['id']}")
            assert answers[-1][1] == "That reminder has no original post."

            cb("rem:list")
            assert edits[-1][0].startswith("🗑️ *Manage Reminders*") and len(edits[-1][1]) == 4
            first = reminders.pending()[0]["id"]
            cb(f"rem:del:{first}")
            assert len(reminders.pending()) == 2 and len(edits[-1][1]) == 3
            # the reminder itself arrives as a reply to the original post
            state = reminders.load()
            for r in state["items"]:
                if r.get("message_id"):
                    r["due"] = time.time() - 1
            reminders.save(state)
            assert reminders.send_due("t", "c") == 1 and replies[-1] == 5
        finally:
            (reminders.notifier.send_message, bot.send_view, bot.edit_view, bot.api, bot.load_state) = orig
            reminders.STATE_FILE, reminders.SOURCES_FILE, reminders.CLAIM_DIR = keep
            bot._reminder_draft.clear()
            bot._reminder_wait = None


def test_pin_rule_and_forum_announcements():
    """📌 Add to Reminders on every notification except assignments and quizzes (those are
    tracked by themselves); a forum whose description carries an announcement is announced,
    an empty forum stays silent."""
    monitor = _package("uninotifier.monitor")
    from uninotifier import reminders

    course = "20261-الاتصال والتواصل باللغة الإنجليزية-ش27-3-و"
    notice = ("<h3>إعلان هام لطلبة المساق</h3><p>يرجى من جميع الطلبة شراء الكتاب يومي الثلاثاء والأربعاء، "
              "من كلية الآداب، الطابق الأول.</p>")

    def item(kind, cmid, **extra):
        return {"name": f"{kind} {cmid}", "type": kind, "course": course, "cmid": str(cmid),
                "url": f"https://x/mod/{kind}/view.php?id={cmid}", **extra}
    forum = item("forum", 66689, name="اعلان هام", _body=notice)
    assert not media.silent_container(forum) and media.silent_container(item("forum", 14908))
    assert not media.silent_container(item("page", 1))

    with tempfile.TemporaryDirectory() as tmp:
        keep = (reminders.SOURCES_FILE, monitor.deliver)
        reminders.SOURCES_FILE = Path(tmp) / "s.json"
        sent = []
        monitor.deliver = lambda header, body="", url=None, rows=None, shots=(), shot_name="", document=None: \
            sent.append((header, body, rows or [])) or True
        try:
            def pins(rows):
                return [b["callback_data"] for row in rows for b in row if b.get("callback_data", "").startswith("rem:s:")]
            pdf = Path(tmp) / "Chapter 1.pdf"
            pdf.write_bytes(b"%PDF")
            cases = [(item("assign", 1), {"kind": "activity"}, []),
                     (item("quiz", 2), {"kind": "activity"}, []),
                     (item("resource", 3), {"kind": "file", "path": str(pdf), "size": 4}, ["rem:s:i3"]),
                     (item("url", 4), {"kind": "link", "target": "https://youtu.be/x"}, ["rem:s:i4"]),
                     (item("page", 5, _body="<p>Install Outlook</p>"), {"kind": "text"}, ["rem:s:i5"]),
                     (item("book", 6), {"kind": "activity"}, ["rem:s:i6"]),
                     (forum, {"kind": "activity"}, ["rem:s:i66689"]),
                     (item("teamscheduler", 7), {"kind": "meeting", "meeting": {"url": "https://teams.microsoft.com/l/x",
                                                                               "details": []}}, ["rem:s:i7"])]
            for it, art, expected in cases:
                assert monitor.notify_new_item(it, None, art)
                assert pins(sent[-1][2]) == expected, (it["type"], sent[-1][2])
            header, body, rows = sent[-2]                       # the forum announcement
            assert header.startswith("📢 *New Announcement*") and "شراء الكتاب" in body
            assert reminders.source("i66689")["course"] == "English Communication Skills"
            assert sent[-1][2][0][0]["text"] == "🔗 Join Teams Lecture", "the meeting keeps its Join button first"

            # updates: a re-uploaded file and an edited page get 📌 too, under their own keys
            monitor.notify_file_update(item("resource", 3), {"path": str(pdf), "size": 4, "modified": 2,
                                                             "old_modified": 1})
            assert pins(sent[-1][2]) == ["rem:s:u3"]
            monitor.notify_content_update(item("page", 5, _body="<p>v2</p>"), {"shots": []})
            assert pins(sent[-1][2]) == ["rem:s:e5"]
        finally:
            reminders.SOURCES_FILE, monitor.deliver = keep

    # monitor.run: the forum with an announcement in its description is reported, the empty one is not
    with tempfile.TemporaryDirectory() as tmp:
        keep = {k: getattr(monitor, k) for k in ("DATA_FILE", "USERNAME", "PASSWORD", "BOT_TOKEN", "CHAT_ID",
                                                 "run_scan", "notify_new_item", "send_schedule_messages",
                                                 "mfa_allowed", "notify_new_course")}
        monitor.DATA_FILE = Path(tmp) / "saved_items.json"
        monitor.USERNAME = monitor.PASSWORD = monitor.BOT_TOKEN = monitor.CHAT_ID = "x"
        monitor.send_schedule_messages = lambda saved: None
        monitor.mfa_allowed = lambda saved: False
        monitor.notify_new_course = lambda c: True
        announced = []
        monitor.notify_new_item = lambda it, deadline, art, submitted=None: announced.append(it["name"]) or True
        content = {"8806-66689": forum, "8806-14908": item("forum", 14908, name="Announcements")}
        monitor.run_scan = lambda saved, notify_new, mfa_handler=None: {
            "courses": {"8806": {"name": course, "url": "https://x/c"}}, "content": dict(content), "deadlines": {},
            "artifacts": {}, "updates": {}, "edits": {}, "posts": {}, "forum_posts": {}, "submissions": {},
            "link_changes": [], "seconds": 1.0, "browser_used": False}
        try:
            monitor.save_data({**monitor.load_saved_data(), "courses": {"8806": {"name": course, "url": "https://x/c"}},
                               "content": {"8806-1": item("page", 1)}})
            monitor.run()
            assert announced == ["اعلان هام"], announced
            assert {"8806-66689", "8806-14908"} <= set(json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))["content"])
        finally:
            for k, v in keep.items():
                setattr(monitor, k, v)
