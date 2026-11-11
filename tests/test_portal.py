"""The registration portal: transcript, balance, study plan, registration plan, finals, guest look-up."""

from support import (_FakePage, _guest_sem, _legacy_markdown_ok, _package, PLAN_CATEGORIES, _plan_html, _record,
                     REGISTRATION_PLAN, _sample_plan, _sample_transcript)

import json
import logging
import os
import re
import tempfile
import time
from datetime import date
from datetime import datetime
from pathlib import Path

import playwright.sync_api as _playwright_api
from uninotifier import gpa
from uninotifier import notifier


def test_portal_parsers():
    from uninotifier import portal

    heads = ["رمز المقرر", "اسم المقرر", "الحالة", "عدد الساعات المعتمدة", "العلامة", "ناجح؟", "معادة"]
    ok, no = "✓", "✗"
    raw = [
        {"legend": "الفصل الأول 2026/2027 20261", "heads": heads,
         "text": "الحالة منتظم المعدل الفصلي المعدل التراكمي 71.3",
         "rows": [["1", "Algebra", "مسجلة", "3", "", "قيد التنفيذ", no]]},
        {"legend": "الفصل الثاني 2025/2026 20252", "heads": heads,
         "text": "المعدل الفصلي 76 المعدل التراكمي 71.3",
         "rows": [["2", "Web", "مسجلة", "3", "88", ok, no], ["2", "Web", "مسجلة", "3", "", "قيد التنفيذ", no],
                  ["3", "Stats", "مسجلة", "3", "74", ok, no]]},
        {"legend": "الفصل الأول 2024/2025 20241", "heads": heads, "text": "المعدل الفصلي 58",
         "rows": [["4", "Old", "مسجلة", "3", "35", no, ok], ["5", "Ethics", "مسجلة", "2", "95", ok, no]]},
        {"legend": "الفصل الثاني 2024/2025 20242", "heads": heads, "text": "", "rows": [["لا يوجد بيانات"]]},
        {"legend": "الفصل الأول 2023/2024 20231", "heads": heads, "text": "المعدل الفصلي 70",
         "rows": [["3", "Stats", "مسجلة", "3", "60", ok, no]]},          # passed twice: counted once
    ]
    semesters = portal.parse_transcript(raw)
    assert [s["term"] for s in semesters] == [
        "First semester 2026/2027", "Second semester 2025/2026", "First semester 2024/2025",
        "Second semester 2024/2025", "First semester 2023/2024"]
    assert semesters[0]["average"] is None and semesters[0]["cumulative"] == 71.3
    assert semesters[1]["average"] == 76 and len(semesters[1]["courses"]) == 2, "the stale duplicate row is hidden"
    assert semesters[0]["courses"][0]["passed"] is None and semesters[2]["courses"][0]["passed"] is False
    assert semesters[2]["courses"][0]["repeated"] is True and semesters[3]["courses"] == []
    assert portal.passed_hours(semesters) == 3 + 3 + 2, "88 + 74 + 95 (hours 3 + 3 + 2); a failed course adds nothing"
    lines = "\n".join(portal.transcript_lines(semesters))
    assert "In Progress" in lines and "✅ *88*" in lines and "🔁 *35*" in lines and "average 76 · cumulative 71.3" in lines
    assert lines.index("In Progress") < lines.index("Second semester 2025/2026"), "the semester in progress comes first"
    assert "⏳ \u2068Algebra\u2069 · 3h" in lines, "an in-progress course shows ⏳, its name isolated from its neighbours"
    assert lines.rstrip().endswith("⏳ in progress_"), "a one-line legend closes the blocks"
    assert "Second semester 2024/2025" not in lines, "an empty semester is skipped"
    assert portal.english_term("الفصل الصيفي 2023/2024 20233") == "Summer semester 2023/2024"

    text = "الرصيد الكلي 1250 مدين"
    tables = [[["", "الفصل الدراسي", "الحالة", "عدد الساعات", "الرصيد"],
               ["", "الفصل الأول 2026/2027", "منتظم", "18", "د.أ1250"],
               ["", "الفصل الثاني 2025/2026", "منتظم", "12", "د.أ0"]]]
    info = portal.parse_balance(text, tables)
    assert info["total"] == 1250 and info["rows"][0] == ("First semester 2026/2027", 18.0, 1250.0)
    out = portal.balance_lines(info)
    assert out[0] == "💰 *Total balance:* 1,250 JD — owed to the university (debit)", out
    assert out[1] == "• First semester 2026/2027 · 18h · 1,250 JD" and "All other semesters: 0 JD" in out[-1]
    assert portal.parse_balance("الرصيد الكلي 40.5 دائن", [])["total"] == -40.5
    assert "in your favour" in portal.balance_lines({"total": -40.5, "rows": []})[0]
    assert "settled" in portal.balance_lines({"total": 0.0, "rows": []})[0]

    empty = [[["رمز المقرر", "اسم المقرر", "العلامة"], ["لا يوجد نتائج للمساقات المسجلة حاليا"]]]
    out = portal.results_lines({"term": "الفصل الأول 2026/2027", "text": "لا يوجد نتائج"}, empty)
    assert out[0] == "ℹ️ No results are published yet for First semester 2026/2027."
    full = [[["رمز المقرر", "اسم المقرر", "العلامة"], ["1", "Algebra", "80"]]]
    out = portal.results_lines({"term": "الفصل الأول 2026/2027", "text": ""}, full)
    assert out[0] == "📅 *First semester 2026/2027*" and out[1] == "*Code | Course | Mark*" and out[2] == "1 | Algebra | 80"
    assert set(portal.SECTIONS) == {"grades", "transcript", "balance", "home"}
    assert all(s["path"].startswith("/faces/ui/pages/student/") for s in portal.SECTIONS.values())


def test_finals_watch():
    from uninotifier import finals
    from uninotifier import portal

    sch = {"finals": {"from": "2027-01-04", "to": "2027-01-31", "interval_minutes": 20}}
    assert finals.in_window(date(2027, 1, 4), sch) and finals.in_window(date(2027, 1, 31), sch)
    assert not finals.in_window(date(2027, 1, 3), sch) and not finals.in_window(date(2027, 2, 1), sch)
    assert not finals.in_window(date(2027, 1, 10), {}), "no window configured: only the switch turns it on"
    assert finals.interval_seconds(sch) == 1200 and finals.interval_seconds({}) == 1800

    with tempfile.TemporaryDirectory() as tmp:
        finals.STATE_FILE = Path(tmp) / "finals.json"
        state = finals.load()
        assert state["enabled"] is False
        noon = datetime(2026, 10, 12, 12, 0)
        assert not finals.active(noon, state, {}) and not finals.due(noon, state, {})
        state["enabled"] = True
        assert finals.active(noon, state, {}) and finals.due(noon, state, {})
        state["last_check"] = noon.timestamp() - 600
        assert not finals.due(noon, state, {}), "checked 10 min ago: not due"
        state["last_check"] = noon.timestamp() - 1801
        assert finals.due(noon, state, {})

        def results(marks, term_marks=None):
            sems = _record()
            for c in sems[0]["courses"]:
                if term_marks and c["code"] in term_marks:
                    c["mark"] = term_marks[c["code"]]
            transcript = portal.Result("transcript", True, figures={"cgpa": 50.0}, data={"semesters": sems})
            grades = portal.Result("grades", True, data={"results": [
                {"code": k, "name": "", "activity": "", "status": "", "mark": v, "passed": None}
                for k, v in marks.items()]})
            return grades, transcript

        state = finals.load()
        # 1) first reading of the term: just the status, nothing announced
        msgs = finals.evaluate(state, *results({}))
        assert len(msgs) == 1 and "Finals Watch" in msgs[0]["text"] and "0 of 3" in msgs[0]["text"]
        assert finals.evaluate(state, *results({})) == [], "nothing new: silence"

        # 2) a grade appears: one alert, in English, noting the retake it replaces
        msgs = finals.evaluate(state, *results({"0101221": 70}))
        assert len(msgs) == 1 and msgs[0]["text"].startswith("🎓 *Grade Released*")
        text = msgs[0]["text"]
        assert "Linear Algebra (1)" in text and "*Mark:* 70  ✅ Passed" in text
        assert "replaces your earlier 35" in text and "1 of 3 grades published" in text
        assert finals.evaluate(state, *results({"0101221": 70})) == [], "the same grade is not announced twice"

        # 3) the last two appear (one below the pass mark, one below the floor): two alerts, then the summary
        msgs = finals.evaluate(state, *results({"0101221": 70, "0135220": 45, "0101272": 33}))
        assert [m["text"].split("\n")[0] for m in msgs] == ["🎓 *Grade Released*", "🎓 *Grade Released*",
                                                            "🏁 *Semester Results — First semester 2026/2027*"]
        assert "❌ Below the 50 pass mark" in msgs[0]["text"] or "❌ Below the 50 pass mark" in msgs[1]["text"]
        assert any("Recorded as 35" in m["text"] for m in msgs[:2])
        summary = msgs[2]["text"]
        assert "*Semester Average:* 50%" in summary
        assert "*New Cumulative GPA:* 57.5%  (+7.5)" in summary, summary
        assert "*Hours in your record:* 12  (was 9" in summary and "Marks under 35 are recorded as 35" in summary
        assert "Linear Algebra (1) 35 → 70" in summary and "Database Systems 35 → 45" in summary
        assert "the portal's own field still shows 50" in summary
        assert msgs[2]["rows"] == [[{"text": "📊 GPA & Grade Hub", "callback_data": "academic"}]]
        assert state["base"] == [50.0, 9.0], "the record before this term: what Grade Needed builds on"
        assert finals.evaluate(state, *results({"0101221": 70, "0135220": 45, "0101272": 33})) == []

        # 4) the portal corrects a mark: announced as an update, and the summary is redone
        msgs = finals.evaluate(state, *results({"0101221": 70, "0135220": 55, "0101272": 33}))
        assert msgs[0]["text"].startswith("🎓 *Grade Updated*") and "55  (was 45)" in msgs[0]["text"]
        assert "*New Cumulative GPA:* 60%" in msgs[-1]["text"]

        # marks the results page lacks are taken from the transcript's current block
        fresh = finals.load()
        fresh["term"] = ""
        msgs = finals.evaluate(fresh, None, results({}, {"0101221": 70, "0135220": 45, "0101272": 33})[1])
        assert [m["text"].split("\n")[0][:12] for m in msgs] == ["🏁 *Finals Wa", "🏁 *Semester "], \
            "everything already out when the watch starts: the status, then the summary at once"

        # a new term starts over
        sems = _record()
        sems[0]["term"] = "Second semester 2026/2027"
        transcript = portal.Result("transcript", True, data={"semesters": sems})
        msgs = finals.evaluate(state, None, transcript)
        assert state["term"] == "Second semester 2026/2027" and "0 of 3" in msgs[0]["text"]

        # an unreadable transcript is an error for the caller, not silence
        try:
            finals.evaluate(state, None, portal.Result("transcript", True, data={}))
        except portal.PortalError:
            pass
        else:
            raise AssertionError("expected PortalError")

        # check(): saves the state on success, leaves it alone when the portal fails
        state = finals.load()
        fetched = []
        msgs, state = finals.check(lambda sections: fetched.append(sections) or dict(
            zip(("grades", "transcript"), results({"0101221": 70}))), state)
        assert fetched == [["grades", "transcript"]] and state["last_check"] > 0
        assert finals.load()["last_check"] == state["last_check"]
        before = finals.load()

        def boom(sections):
            raise portal.PortalError("The portal refused the sign-in: Incorrect username or password.")
        try:
            finals.check(boom, finals.load())
        except portal.PortalError:
            pass
        else:
            raise AssertionError("expected PortalError")
        assert finals.load() == before

        status = finals.status_text(finals.load(), sch)
        assert "Finals Watch" in status and "2027-01-04 to 2027-01-31" in status and "1 of 3" in status


def test_finals_commands_and_file_destination():
    from uninotifier import archive
    bot = _package("uninotifier.bot")
    from uninotifier import finals

    with tempfile.TemporaryDirectory() as tmp:
        finals.STATE_FILE = Path(tmp) / "finals.json"
        replies = []
        orig = (bot.send_view, bot.edit_view, bot.portal.credentials)
        bot.send_view = lambda text, rows: replies.append((text, rows))
        bot.edit_view = lambda mid, text, rows: replies.append((f"edit:{mid}", text, rows))
        bot.portal.credentials = lambda: ("u", "p")
        try:
            bot.handle_command("/finals")
            assert "Finals Watch" in replies[-1][0] and "*Off*" in replies[-1][0]
            assert [b["callback_data"] for row in replies[-1][1] for b in row] == ["finals:check", "finals:on", "academic", "menu"]
            assert replies[-1][1][0][0]["text"] == "🔄 Check Grades Now" and "Background Checks" in replies[-1][1][1][0]["text"]
            bot.handle_command("/finals on")
            assert "*On*" in replies[-1][0] and finals.load()["enabled"] and finals.load()["last_check"] == 0
            assert replies[-1][1][1][0]["callback_data"] == "finals:off"
            bot.handle_finals("off", 5)                                   # a button edits its message
            assert replies[-1][0] == "edit:5" and "*Off*" in replies[-1][1] and not finals.load()["enabled"]
            bot.portal.credentials = lambda: None
            bot.handle_command("/finals on")
            assert "not configured" in replies[-1][0], "turning it on without a portal login explains .env"
            bot.handle_command("/finals check")
            assert "not configured" in replies[-1][0]
        finally:
            bot.send_view, bot.edit_view, bot.portal.credentials = orig
        assert not bot._portal_lock.locked()
        assert "finals" in {c for c, _ in bot.COMMANDS}
        assert "finals:status" not in [b.get("callback_data") for row in bot.view_menu()[1] for b in row]
        assert "finals:status" in [b.get("callback_data") for row in bot.view_academic()[1] for b in row], "nested in the hub"

    # where a new file is filed is part of its Telegram message
    root = archive.ARCHIVE_ROOT
    assert archive.destination(root / "Big Data" / archive.LECTURES / "Chapter 1.pdf") == \
        "`Big Data/01_Lectures` (Lectures & Slides)"
    assert archive.destination(root / "Big Data" / archive.ASSIGNMENTS / "HW1" / "a.zip") == \
        "`Big Data/02_Assignments` (Assignments & Projects)"
    assert archive.destination(root / archive.GENERAL / "My timetable.pdf") is None, "the personal folder is not a filing place"
    assert archive.destination(Path("C:/Windows/x.txt")) is None and archive.destination(root / "Big Data") is None


def test_finals_check_ends_on_the_grades_screen():
    """Finals Watch is the one place for this term's grades: Check Grades Now shows progress in the
    message that was pressed (no extra message), ends on the screen listing every course with its mark
    or ⏳, announcements land first and the screen returns under them, and /grades and the old
    "This Term's Grades" button lead here."""
    from uninotifier import finals, portal
    bot = _package("uninotifier.bot")

    def results(marks):
        sems = _record()
        grades = portal.Result("grades", True, data={"results": [
            {"code": k, "name": "", "activity": "", "status": "", "mark": v, "passed": None} for k, v in marks.items()]})
        return {"grades": grades, "transcript": portal.Result("transcript", True, figures={"cgpa": 50.0}, data={"semesters": sems})}

    shown, announced = [], []
    reading = {"marks": {}, "error": None}

    def fetch_many(sections, ask, **kwargs):
        if reading["error"]:
            raise reading["error"]
        return results(reading["marks"])

    with tempfile.TemporaryDirectory() as tmp:
        keep = (finals.STATE_FILE, bot.send_view, bot.edit_view, bot.portal.fetch_many, bot.portal.credentials,
                notifier.send_message, bot.api)
        finals.STATE_FILE = Path(tmp) / "finals.json"
        bot.api = lambda method, **data: True                            # the chat action of the worker
        bot.send_view = lambda text, rows: shown.append(("send", None, text, rows))
        bot.edit_view = lambda mid, text, rows: shown.append(("edit", mid, text, rows))
        bot.portal.fetch_many, bot.portal.credentials = fetch_many, lambda: ("u", "p")
        notifier.send_message = lambda token, chat, text, url=None, rows=None, **kw: announced.append(text) or True

        def check(message_id=7):
            shown.clear()
            announced.clear()
            assert bot._portal_lock.acquire(blocking=False)
            bot.run_finals_check(True, message_id)                      # the worker, run in line
            assert not bot._portal_lock.locked()

        try:
            before = bot.finals_status_view()[0]
            assert "not read yet" in before and "Check Grades Now" in before

            # first reading: the "started" announcement, then the screen at the bottom — grades listed, none published
            check()
            assert len(announced) == 1 and "Finals Watch" in announced[0]
            kind, mid, text, rows = shown[-1]
            assert kind == "send" and len(shown) == 1, "announcements landed first: the screen returns under them"
            assert "0 of 3 published" in text and text.count("⏳") == 3 and "Linear Algebra (1)" in text
            assert rows[0][0]["callback_data"] == "finals:check"

            # a grade appears: announced, then the screen shows the mark
            reading["marks"] = {"0101221": 70}
            check()
            assert announced and announced[0].startswith("🎓 *Grade Released*")
            text = shown[-1][2]
            assert "1 of 3 published" in text and "✅ *70* · Linear Algebra (1)" in text and text.count("⏳") == 2

            # nothing new: no announcement, the screen is updated WHERE IT IS (the pressed message), nothing else is sent
            check()
            assert announced == [] and len(shown) == 1 and shown[0][:2] == ("edit", 7), shown
            assert "1 of 3 published" in shown[0][2] and "🕒 *Last check:*" in shown[0][2]
            check(None)                                                 # typed (/grades): the screen goes to the bottom
            assert shown[0][0] == "send" and "Finals Watch" in shown[0][2]
            assert [c["code"] for c in finals.load()["courses"]] == ["0101221", "0101272", "0135220"]
            assert _legacy_markdown_ok(shown[0][2])

            # a failed reading: the message that was pressed says why and offers to retry
            reading["error"] = portal.PortalError("The portal refused the sign-in: Incorrect username or password.")
            finals_state = finals.load()
            finals_state["failure_notice"] = 0
            finals.save(finals_state)
            check()
            kind, mid, text, rows = shown[-1]
            assert (kind, mid) == ("edit", 7) and "Incorrect username" in text
            assert "finals:check" in [b["callback_data"] for row in rows for b in row if "callback_data" in b]
            reading["error"] = None

            # the button shows progress in its own message (no new one), and /grades + the old button lead here
            shown.clear()
            keep_run, keep_handle = bot.finals_flow.run_finals_check, bot.finals_flow.handle_finals
            started = []
            bot.finals_flow.run_finals_check = lambda manual, message_id=None: (started.append((manual, message_id)),
                                                                                  bot._portal_lock.release())
            try:
                bot.handle_finals("check", 9)
                assert shown == [("edit", 9, "🏁 *Finals Watch*\n\n⏳ Reading your grades from the portal…", [])], shown
                for _ in range(100):
                    if started:
                        break
                    time.sleep(0.01)
                assert started == [(True, 9)]
                calls = []
                bot.finals_flow.handle_finals = lambda arg, message_id=None: calls.append((arg, message_id))
                bot.handle_command("/grades")
                bot.handle_callback({"id": "g1", "data": "portal:grades", "message": {"message_id": 11}})
                assert calls == [("check", None), ("check", 11)], calls
                assert "grades" not in bot.PORTAL_COMMANDS and "Check Grades Now" in dict(bot.COMMANDS)["grades"]
            finally:
                bot.finals_flow.run_finals_check, bot.finals_flow.handle_finals = keep_run, keep_handle
        finally:
            (finals.STATE_FILE, bot.send_view, bot.edit_view, bot.portal.fetch_many, bot.portal.credentials,
             notifier.send_message, bot.api) = keep
            bot._portal_lock.acquire(blocking=False)
            bot._portal_lock.release()


def test_registration_plan():
    """What to register next term, from the plan and its prerequisites: this term's courses count
    as passed, a failed course comes first, a course is held back until what comes before it is
    passed, training and the project wait for the final year, and the hours follow the term."""
    import inspect
    from uninotifier import degreeplan, portal, registration
    from uninotifier.bot import chat, registration_flow, portal_flow

    plan = degreeplan.parse(_plan_html(REGISTRATION_PLAN))
    by_code = {c["code"]: c for cat in plan["categories"] for c in cat["courses"]}
    assert by_code["0900212"]["prereqs"] == ["0900211"] and by_code["0900101"]["prereqs"] == []
    assert by_code["0900352"]["prereqs"] == ["0900251"], "a name with brackets of its own"
    assert degreeplan.prerequisites("(0900211 مبادئ البرمجة)(0900251 قواعد البيانات (1))") == ["0900211", "0900251"]

    advice = registration.advise(plan)
    state = {e["course"]["code"]: e for e in advice["entries"]}
    # 3 + (16 - 3 this term) + 3 elective + 3 remedial owed
    assert advice["left"] == 22 and advice["passed"] == 15 and advice["term_courses"] == 1
    assert "0900251" not in state and "0900101" not in state, "this term's and the passed courses are not offered"
    assert [e["course"]["code"] for e in advice["entries"]][:2] == ["0900002", "0900212"], "remedial, then the retake"
    assert state["0900212"]["kind"] == "retake" and state["0900212"]["state"] == "open" and state["0900212"]["unlocks"] == 1
    assert state["0900313"]["state"] == "blocked" and "هياكل البيانات" in state["0900313"]["why"]
    assert state["0900352"]["state"] == "if", "open once this term's course is passed"
    assert state["0900314"]["state"] == "open" and state["0900314"]["unknown"] == ["0900999"], "said, never hidden"
    assert state["0900490"]["state"] == "blocked" and state["0900490"]["approval"] and state["0900490"]["reason"] == "senior"
    assert state["0900313"]["reason"] == "prereq" and state["0900103"]["reason"] == "military"
    assert advice["held_hours"] == 7 and advice["open_hours"] + advice["held_hours"] == advice["left"], "nothing lost"
    assert state["0900492"]["state"] == "blocked" and state["0900103"]["why"] == "graduation term only"
    assert advice["needs"] == {"متطلبات تخصص (اختيارية)": 3} and not advice["graduating"] and not advice["senior"]
    assert advice["open_hours"] == 15, "remedial 3 + retake 3 + two majors 6 + ONE elective 3"

    assert [h for h, _ in registration.options(advice)] == [12, 15, 18, 21]
    assert registration.options(advice)[-1][1] == "21 hrs"
    assert [h for h, _ in registration.options(registration.advise(plan, registration.SUMMER))] == [3, 6, 9]
    twelve = registration.propose(advice, 12)
    assert [e["course"]["code"] for e in twelve["entries"]] == ["0900002", "0900212", "0900314", "0900352"]
    assert twelve["total"] == 12 and not twelve["short"]
    all_open = registration.propose(advice, 18)
    assert all_open["total"] == 15 and all_open["short"], "never filled with a course that is not open"
    assert sum(1 for e in all_open["entries"] if e["kind"] == "elective") == 1, "an elective only up to the category's need"
    text = "\n".join(registration.proposal_lines(advice, 18, "Second semester 2030/2031"))
    assert "Only 15 hrs could be filled" in text and "if you pass" in text and "⚠️ needs 0900999" in text
    assert "excellent CGPA" not in text, "15 hrs were filled: nothing above 18 to warn about"
    held = registration.blocked_lines(advice)
    assert held[1] == "7 hrs towards your plan · 4 courses"
    at = held.index("🔗 *Another course comes first*")
    assert "الخوارزميات" in held[at + 1] and held[at + 2].lstrip(degreeplan.LRM).strip().startswith("└ first:")
    assert "هياكل البيانات" in held[at + 2], "what comes first is named under the course"
    assert "🎓 *After 90 hrs passed* — you have 15 after this term" in held and "🏁 *Graduation term only*" in held
    assert "_Each needs the department head's approval._" in held
    listed = registration.open_lines(advice)
    assert "🔁 *Retake first*" in listed and "🎯 *Major · Elective* — choose 3 hrs" in listed
    assert not any("elective ·" in line for line in listed), "the heading says what they are: not repeated on every line"
    assert "⛔ *Not yet:* 7 hrs · 4 courses" in registration.overview_lines(advice, "x")

    # your own choice: the hours, and what the rules say
    mine = registration.check(advice, ["0900212", "0900331", "0900332", "0900313"])
    assert [e["course"]["code"] for e in mine["entries"]] == ["0900212", "0900331", "0900332"], "a held course cannot be chosen"
    assert mine["total"] == 9 and mine["counted"] == 6
    assert any("needs only 3 more hrs: 3 of these add nothing" in n for n in mine["notes"])
    assert any("needs at least 12 hrs" in n for n in mine["notes"])
    assert registration.check(advice, [])["notes"] == []
    summer_choice = registration.check(registration.advise(plan, registration.SUMMER), [e["course"]["code"] for e in advice["entries"]])
    assert summer_choice["total"] == 18 and any("12 hrs at the very most" in n for n in summer_choice["notes"])
    for lines in (registration.overview_lines(advice, "Second semester 2030/2031"), registration.open_lines(advice)):
        assert _legacy_markdown_ok("\n".join(lines))

    # the graduation term: everything left fits and nothing waits — military science opens, the choice is "all"
    last = [list(c) for c in REGISTRATION_PLAN]
    last[1] = ["متطلبات تخصص (اجبارية)", 22, 18, 4, [(("مجتازة", *c[1:]) if c[1] not in ("0900490", "0900492") else c)
                                                    for c in REGISTRATION_PLAN[1][4]]]
    last[2] = ["متطلبات تخصص (اختيارية)", 3, 3, 0, [("مجتازة", "0900331", "أمن الشبكات", 3)]]
    last[3] = ["مواد استدراكية (اجبارية)", 6, 6, 0, [("معفاة", "0900001", "اللغة الانجليزية الاستدراكية", 3)]]
    final = registration.advise(degreeplan.parse(_plan_html(last)))
    assert final["left"] == 7 and not final["senior"] and final["passed"] == 27, "under 90 hours passed in this small plan"
    assert not final["graduating"], "training and the project are still held back: not the last term yet"
    assert [h for h, _ in registration.options(final)] == [12], "the least a regular term takes is still offered"
    summer = registration.advise(degreeplan.parse(_plan_html(last)), registration.SUMMER)
    assert "12 only in your last term" in registration.allowed_line(summer)
    # with only military science left it is the last term: it opens, and the one choice is all of it
    done = [list(c) for c in last]
    done[1] = ["متطلبات تخصص (اجبارية)", 22, 22, 0, [("مجتازة", *c[1:]) for c in REGISTRATION_PLAN[1][4]]]
    end = registration.advise(degreeplan.parse(_plan_html(done)))
    assert end["left"] == 3 and end["graduating"] and end["entries"][0]["state"] == "open"
    assert registration.options(end) == [(3, "3 hrs · all that is left")]
    end_summer = registration.advise(degreeplan.parse(_plan_html(done)), registration.SUMMER)
    assert "12 now, as it is your last term" in registration.allowed_line(end_summer)

    # the terms, in order
    assert registration.next_term("First semester 2030/2031") == ("Second semester 2030/2031", registration.REGULAR)
    assert registration.next_term("Second semester 2030/2031") == ("Summer semester 2030/2031", registration.SUMMER)
    assert registration.next_term("Summer semester 2030/2031") == ("First semester 2031/2032", registration.REGULAR)
    assert registration.other_term("Summer semester 2030/2031", registration.SUMMER)[0] == "First semester 2031/2032"
    assert registration.latest_term([{"term": "Second semester 2029/2030"}, {"term": "First semester 2030/2031"},
                                     {"term": "Summer semester 2029/2030"}]) == "First semester 2030/2031"

    # the screens: from the saved transcript, never the portal; nothing here registers anything
    keep = portal_flow.load_cached
    try:
        portal_flow.load_cached = lambda section: None
        text, rows = registration_flow.view_home()
        assert "open 📜 Transcript once" in text and rows[0][0]["callback_data"] == "portal:transcript"
        result = portal.Result(section="transcript", found=True,
                               data={"plan": plan, "semesters": [{"term": "First semester 2030/2031", "courses": []}]})
        portal_flow.load_cached = lambda section: (result, 0.0)
        registration_flow._kind = None
        text, rows = registration_flow.view_home()
        assert "Registration Plan* — Second semester 2030/2031" in text and "Left to take:* 22 hrs" in text
        assert [b["callback_data"] for b in rows[0]] == ["reg:h:12", "reg:h:15", "reg:h:18", "reg:h:21"]
        assert rows[1][0]["callback_data"] == "reg:pick" and rows[-2][1]["callback_data"] == "portal:transcript"
        assert rows[-1] == chat.RECORD_ROW and "open with the portal's registration period" in text
        assert "Your plan as read from the portal" in text and "Update My Plan" in text, "read long ago: said"
        assert "Update My Plan" not in registration_flow.read_line(1000.0, now=1500.0)
        suggested, rows = registration_flow.view_screen("h:12")
        assert "✨ *Suggested: 12 hrs*" in suggested and "*Total:* 12 hrs" in suggested and rows[0][0]["callback_data"] == "reg:pick"
        # ✋ starts from the suggestion; a tap takes a course out or adds one; every open course is a button
        picked, rows = registration_flow.view_screen("pick")
        assert "*Total:* 12 hrs · 4 courses" in picked and len([r for r in rows if r[0]["callback_data"].startswith("reg:t:")]) == 6
        assert rows[0][0]["text"].startswith("✅") and rows[-1] == registration_flow.HOME_ROW
        assert "*Total:* 9 hrs · 3 courses" in registration_flow.view_screen("t:0900212")[0]
        assert "*Total:* 12 hrs · 4 courses" in registration_flow.view_screen("t:0900331")[0]
        assert "Nothing chosen yet." in registration_flow.view_screen("clear")[0]
        assert "Nothing chosen yet." in registration_flow.view_screen("t:0900490")[0], "a held course is never chosen"
        assert registration_flow.view_screen("held")[1] == [registration_flow.HOME_ROW]
        assert all(len(b["callback_data"].encode()) <= 64 for r in rows for b in r)
        old = {"categories": [dict(cat, courses=[{k: v for k, v in c.items() if k != "prereqs"} for c in cat["courses"]])
                              for cat in plan["categories"]]}
        result.data["plan"] = old
        assert "open 📜 Transcript once" in registration_flow.view_home()[0], "a plan saved before prerequisites were read"
    finally:
        portal_flow.load_cached = keep
        registration_flow._kind = None
        registration_flow._chosen.clear()
    source = inspect.getsource(registration) + inspect.getsource(registration_flow)
    assert "fetch" not in source and "playwright" not in source, "advice only: the portal is not touched"


def test_study_plan_from_the_portal():
    """The degree's hours come from the student's own plan on the portal — any major, any plan year —
    never from a fixed number: what is required, passed, registered this term, failed and left."""
    from uninotifier import degreeplan, transcript

    plan = degreeplan.parse(_plan_html())
    assert [c["name"] for c in plan["categories"]] == [name for name, *_ in PLAN_CATEGORIES], "each category once"
    first = plan["categories"][0]
    assert (first["required"], first["passed"], first["remaining"]) == (9, 3, 6)
    assert first["courses"][0] == {"code": "0900101", "name": "مهارات الدراسة", "hours": 3.0, "status": "passed",
                                   "prereqs": []}
    assert [c["status"] for cat in plan["categories"] for c in cat["courses"]] == [
        "passed", "in_progress", "open", "passed", "in_progress", "open", "open", "failed", "open", "open", "exempt", "exempt"]
    assert degreeplan.parse("<html><body><p>الصفحة الرئيسية</p></body></html>") is None

    # remedial courses are not degree hours; an elective registered beyond what its category needs adds nothing
    assert degreeplan.progress(plan) == {"degree": 22, "passed": 6, "remaining": 16, "term": 6, "term_courses": 2,
                                         "after": 10, "failed": 3, "failed_courses": 1}
    extra = [list(c) for c in PLAN_CATEGORIES]
    extra[1][4] = [("قيد الدراسة", *course[1:]) if course[0] == "" else course for course in extra[1][4]]
    crowded = degreeplan.progress(degreeplan.parse(_plan_html(extra)))
    assert crowded["term"] == 12 and crowded["after"] == 10, "three electives registered, the category needs one more"

    assert degreeplan.progress_lines(plan, retakes=1, repeated=3) == [
        "📊 *Degree Plan:* 22 hrs", "✅ *Passed:* 6 hrs", "⏳ *This Term:* 6 hrs · 2 courses (1 retake)",
        "❌ *Failed, Not Retaken Yet:* 3 hrs · 1 course", "🔁 *Repeated Courses:* 3 (2 done · 1 this term)",
        "🏁 *Remaining:* 16 hrs · 10 after this term",
        "📝 *Placement Exams:* all 2 exempt — nothing added to the plan"]
    done = degreeplan.parse(_plan_html([("متطلبات الكلية (اجبارية)", 3, 3, 0, [("مجتازة", "0900401", "مقدمة", 3)])]))
    assert degreeplan.progress_lines(done) == ["📊 *Degree Plan:* 3 hrs", "✅ *Passed:* 3 hrs", "🏁 *Remaining:* 0 hrs"]

    assert degreeplan.english("متطلبات تخصص (اختيارية)") == "Major · Elective"
    assert degreeplan.english("م. عائلة تخصص (اجبارية)") == "Major Family · Compulsory"
    assert degreeplan.english("متطلبات الكلية (اجبارية)") == "Faculty · Compulsory"
    assert degreeplan.english("متطلبات حرة") == "Free" and degreeplan.english("مساقات متقدمة (اجبارية)") == "مساقات متقدمة (اجبارية)"

    text = "\n".join(degreeplan.remaining_lines(plan))
    lines = text.splitlines()
    assert lines[:2] == ["📚 *Remaining Courses*", "🏁 *16 hrs* left of 22 · *10 hrs* after this term"]
    assert "*University · Compulsory* — 6 of 9 hrs left" in lines and "*Major · Elective* — 3 of 6 hrs left · choose from:" in lines
    block = lines[lines.index("*Major · Compulsory* — 7 of 7 hrs left") + 1:][:3]
    assert block[0].endswith("· 3h — failed, retake") and "❌" in block[0] and "هياكل البيانات" in block[0]
    assert "▫️" in block[1] and block[2].endswith("· 1h")
    assert "مهارات الدراسة" not in text, "nothing that was passed"
    # the placement exams are said on their own, never among the plan's hours
    at = lines.index("*Placement Exams* — nothing added to the plan")
    assert [l.lstrip(degreeplan.LRM) for l in lines[at + 1:at + 3]] == ["✅ English — exempt", "✅ Computer Skills — exempt"]

    # one that was not passed is a course on top of the plan: its hours are said, apart from the degree's
    owed = [list(c) for c in PLAN_CATEGORIES]
    owed[3] = ["مواد استدراكية (اجبارية)", 9, 3, 6, [("مجتازة", "0900001", "اللغة الانجليزية الاستدراكية", 3),
                                                     ("قيد الدراسة", "0900002", "مهارات الحاسوب الاستدراكية", 3),
                                                     ("", "0900003", "اللغة العربية الاستدراكية", 3)]]
    behind = degreeplan.parse(_plan_html(owed))
    assert degreeplan.progress(behind)["degree"] == 22, "the degree's hours stay the plan's"
    assert degreeplan.placement(behind) | {"courses": None} == {"courses": None, "extra": 9, "left": 6, "term": 3}
    tail = degreeplan.progress_lines(behind)[-2:]
    assert tail[0] == "🏁 *Remaining:* 16 hrs · 10 after this term · +6 remedial"
    assert tail[1] == ("📝 *Placement Exams:* English ✅ · Computer Skills ⏳ · Arabic ▫️ — "
                       "+9 hrs on top of the plan (6 still to pass)")
    listed = [l.lstrip(degreeplan.LRM) for l in degreeplan.remaining_lines(behind)]
    assert "*Placement Exams* — +9 hrs on top of the plan, 6 still to pass" in listed
    assert "✅ English · 3h — taken as a course and passed" in listed and "▫️ Arabic · 3h — to take" in listed
    assert degreeplan.placement(done) is None and not any("Placement" in l for l in degreeplan.remaining_lines(done))
    assert text.index("الكتابة الأكاديمية") < text.index("العمل التطوعي"), "this term's courses first"
    assert _legacy_markdown_ok(text) and len(text) < notifier.MESSAGE_LIMIT

    # the transcript's summary with the plan: the plan's own hours, not a number from schedule.json
    sems = _sample_transcript()
    figures = {"record_cgpa": 71.26, "cgpa": 71.3, "passed_hours": 8.0, "record_hours": 14.0}
    summary = transcript.summary_lines(sems, figures, "Regular / Good Standing", remaining=124.0, plan=plan, retakes=1)
    assert summary[:2] == ["🎓 *Cumulative GPA:* 71.26% (Portal display: 71.3%)", "📌 *Academic Status:* Regular / Good Standing"]
    assert summary[2] == "" and summary[3] == "📊 *Degree Plan:* 22 hrs" and summary[-1] == "📚 *Total Attempted Hours:* 14 hrs"
    assert "🏁 *Remaining:* 16 hrs · 10 after this term" in summary and not any("124" in line for line in summary)


def test_transcript_layout():
    """The transcript reads like a report: an executive summary (GPA, passed hours with what is in
    progress or still to repeat, attempted hours, status — and for a guest the major and the hours
    left), then the semester in progress first and every other semester with ✅ ❌ 🔁 marks."""
    from uninotifier import transcript

    sems = _sample_transcript()
    state = transcript.standing(sems)
    # Linear Algebra and Databases failed once but are being taken again (in progress, not "to repeat", their old
    # attempts replaced); Data Structures failed and has not been retaken: that is what is still "to repeat"
    assert state == {"passed": 8.0, "in_progress": 9.0, "to_repeat": 3.0, "retaken": 2, "courses_passed": 3}, state
    figures = {"record_cgpa": 71.26, "cgpa": 71.3, "passed_hours": 8.0, "record_hours": 14.0}
    lines = transcript.summary_lines(sems, figures, "Regular / Good Standing")
    assert lines == [
        "🎓 *Cumulative GPA:* 71.26% (Portal display: 71.3%)",
        "⏱️ *Passed Hours:* 8 hrs",
        "      └ ⏳ 9h in progress · ❌ 3h to repeat · 🔁 2 retaken",
        "📚 *Total Attempted Hours:* 14 hrs",
        "📌 *Academic Status:* Regular / Good Standing"], lines
    # something failed and not taken again shows up as "to repeat"; zero parts are not listed
    failed_only = [{"term": "T", "average": None, "cumulative": None, "courses": [
        {"code": "1", "name": "X", "hours": 3, "mark": 40, "passed": False, "repeated": False},
        {"code": "2", "name": "Y", "hours": 3, "mark": 80, "passed": True, "repeated": False}]}]
    assert "      └ ❌ 3h to repeat" in transcript.summary_lines(failed_only, {"passed_hours": 3.0})
    assert not any("└" in l for l in transcript.summary_lines(sems[1:2], {"passed_hours": 6.0}))
    # a guest: the major first, the hours left after the status block
    guest = transcript.summary_lines(sems, figures, None, major="نظم المعلومات", remaining=124.0)
    assert guest[0].startswith("📘 *Major:*") and "نظم المعلومات" in guest[0] and guest[-1] == "🏁 *Remaining Hours:* 124 hrs"
    assert transcript.summary_lines(sems, figures, None, remaining=-5.0)[-1] == "🏁 *Remaining Hours:* 0 hrs"
    assert transcript.summary_lines([], {}) == []

    text = "\n".join(transcript.blocks(sems))
    order = [text.index(m) for m in ("In Progress", "Second semester 2025/2026", "First semester 2024/2025")]
    assert order == sorted(order), "the semester in progress first"
    assert text.startswith("⏳ *First semester 2026/2027* · In Progress")
    assert "📅 *Second semester 2025/2026* · average 76 · cumulative 71.3" in text
    for fragment in ("✅ *88* ·", "✅ *74* ·", "🔁 *35* · \u2068جبر خطي (1)\u2069 · 3h", "🔁 *35* · \u2068قواعد البيانات\u2069 · 3h",
                     "❌ *30* · \u2068تراكيب البيانات\u2069 · 3h", "✅ *95* ·"):
        assert fragment in text, fragment       # Databases: failed, replaced by this term's retake (the portal did not flag it)
    assert text.count("⏳ \u2068") == 3, "the three courses of the term in progress"
    assert text.count("🔁 *") == 2 and text.count("❌ *") == 1
    # an Arabic name cannot reorder its neighbours: every course line starts with a left-to-right mark, the name is isolated
    for line in text.splitlines():
        if "· 3h" in line or "· 2h" in line:
            assert line.startswith("\u200e") and "\u2068" in line and "\u2069" in line, repr(line)
    assert "Big\\_Data \\*2\\*" in text and _legacy_markdown_ok(text), "Markdown characters in a name are escaped"
    assert text.rstrip().endswith("⏳ in progress_") and transcript.blocks([]) == []
    # marks the results page already shows for the term in progress
    published = "\n".join(transcript.blocks(sems, {"0135220": 74.0, "0101221": 40.0}))
    assert "✅ *74* · \u2068قواعد البيانات\u2069" in published and "❌ *40* · \u2068جبر خطي (1)\u2069" in published
    assert "🔁 *35*" in published, "a replaced attempt is still marked as such"
    # a full record fits one message
    many = sems * 3
    assert len("\n".join(transcript.blocks(many))) < notifier.MESSAGE_LIMIT


def test_portal_answer_is_one_message():
    """A portal answer is ONE message (the screenshot with its caption); a balance holds everything
    in it, only a transcript's semesters follow as a single text. No "retrieved" status message, no
    "details" heading, and the hub it came from returns under the answer."""
    from uninotifier import portal, maintenance
    bot = _package("uninotifier.bot")

    sems = _sample_transcript()
    sent_files, sent_msgs, screens, edits, api_calls = [], [], [], [], []
    keep = (bot.notifier.send_png_file, bot.notifier.send_message, bot.notifier.send_long_message, bot.send_view,
            bot.edit_view, bot.api, bot.portal.fetch_many, bot.sync_base, bot.sync_retakes, bot.lock_grades,
            bot.SWR_SECTIONS)
    bot.SWR_SECTIONS = ()          # every request here reads afresh (test_portal_answers_from_the_saved_copy has the cache)
    bot.notifier.send_png_file = lambda tok, chat, png, name, caption="", url=None, rows=None: (
        sent_files.append((name, caption, rows)) or True)
    bot.notifier.send_message = lambda tok, chat, text, url=None, rows=None: sent_msgs.append(("msg", text)) or True
    bot.notifier.send_long_message = lambda tok, chat, text, url=None, rows=None: sent_msgs.append(("long", text)) or True
    bot.send_view = lambda text, rows: screens.append(text) or {"message_id": 1}
    bot.edit_view = lambda mid, text, rows: edits.append((mid, text, rows))
    bot.api = lambda method, **d: api_calls.append(method) or True
    bot.sync_base = bot.sync_retakes = lambda *a: None
    bot.lock_grades = lambda *a: None

    def reset():
        sent_files.clear(); sent_msgs.clear(); screens.clear(); edits.clear()

    try:
        # a transcript: the summary in the caption of the screenshot, the semesters in one text message
        transcript_result = portal.Result("transcript", True, b"png", [], {"record_cgpa": 71.26, "cgpa": 71.3, "passed_hours": 8.0,
                                          "record_hours": 14.0, "base_cgpa": 70.0, "base_hours": 14.0},
                                          [], [], {"semesters": sems, "status": "منتظم", "retakes": {}})
        bot.send_portal_result(transcript_result)
        assert len(sent_files) == 1 and sent_msgs == [], "a transcript is one message: its courses are a button away"
        caption, rows = sent_files[0][1], sent_files[0][2]
        assert caption.startswith("📜 *ZUJ Portal — Transcript*") and "⏱️ *Passed Hours:* 8 hrs" in caption
        assert "      └ ⏳ 9h in progress · ❌ 3h to repeat · 🔁 2 retaken" in caption and "Total Attempted Hours" in caption \
            and len(caption) < 1024
        assert rows[0] == [{"text": "✅ Courses Taken", "callback_data": "rec:taken"}], "no plan was read: no Remaining Courses"
        taken = bot.record_text("taken", sems, None)
        assert taken.startswith("✅ *Courses Taken*\n\n⏳ *First semester 2026/2027* · In Progress")
        assert "details" not in taken and "Retrieved" not in taken and bot.record_text("left", sems, None) == ""

        # a balance: everything in the one message
        reset()
        info = {"total": 1250.0, "rows": [("First semester 2026/2027", 18.0, 1250.0), ("Second semester 2025/2026", 12.0, 0.0)]}
        balance = portal.Result("balance", True, b"png", [], {"balance": 1250.0}, [], portal.balance_lines(info))
        bot.send_portal_result(balance)
        assert len(sent_files) == 1 and sent_msgs == [], "a balance is one message"
        assert "Total balance:* 1,250 JD" in sent_files[0][1] and "First semester 2026/2027 · 18h · 1,250 JD" in sent_files[0][1]
        # ... unless it cannot fit a caption: the total stays there, the rest follows once
        reset()
        long_rows = [(f"Semester {n}", 15.0, 100.0 + n) for n in range(60)]
        big = portal.Result("balance", True, b"png", [], {"balance": 9.0}, [], portal.balance_lines({"total": 9000.0, "rows": long_rows}))
        bot.send_portal_result(big)
        assert len(sent_files) == 1 and len(sent_files[0][1]) <= notifier.CAPTION_LIMIT and "Total balance" in sent_files[0][1]
        assert [k for k, _ in sent_msgs] == ["long"] and "Semester 59" in sent_msgs[0][1] and "Total balance" not in sent_msgs[0][1]

        # the request: a button's own message shows progress; a typed command shows none; the hub returns under the answer
        bot.portal.fetch_many = lambda sections, ask: {"transcript": transcript_result}
        for message_id in (5, None):
            reset()
            api_calls.clear()
            assert bot._portal_lock.acquire(blocking=False)
            bot.run_portal("transcript", message_id)
            assert not bot._portal_lock.locked()
            assert screens and screens[-1].startswith("📜 *Academic Record & Balance*"), screens
            assert not any("retrieved" in t for t in screens) and len(screens) == 1, "no status message, just the hub"
            if message_id:
                assert [e[0] for e in edits] == [5] and "signing in" in edits[0][1] and edits[0][2] == []
            else:
                assert edits == [] and screens == [screens[-1]], "typed: nothing but the answer and the hub"
            assert "sendChatAction" in api_calls and len(sent_files) == 1

        # a failure is said in the message that was pressed, with a way to retry; typed: as the menu itself
        for exc in (portal.PortalError("The portal refused the sign-in."), RuntimeError("boom")):
            for message_id in (5, None):
                reset()
                bot.portal.fetch_many = lambda sections, ask, exc=exc: (_ for _ in ()).throw(exc)
                assert bot._portal_lock.acquire(blocking=False)
                bot.run_portal("balance", message_id)
                shown = edits[-1][1:] if message_id else (screens[-1], None)
                assert shown[0].startswith("❌ *Tuition Balance*") and "refused" in shown[0] or "couldn't be read" in shown[0]
                rows = edits[-1][2] if message_id else None
                if rows:
                    assert rows[0][0] == {"text": "🔄 Try Again", "callback_data": "portal:balance"}
                assert sent_files == [] and not bot._portal_lock.locked()
        # maintenance: the screenshot says why, and the hub returns under it
        reset()
        bot.portal.fetch_many = lambda sections, ask: (_ for _ in ()).throw(maintenance.PortalUnavailable(
            "registration portal", "the sign-in page shows a maintenance notice", "قيد الصيانة", b"PNG"))
        assert bot._portal_lock.acquire(blocking=False)
        bot.run_portal("transcript", None)
        assert len(sent_files) == 1 and sent_files[0][2] == [] and screens[-1].startswith("📜 *Academic Record & Balance*")
    finally:
        (bot.notifier.send_png_file, bot.notifier.send_message, bot.notifier.send_long_message, bot.send_view,
         bot.edit_view, bot.api, bot.portal.fetch_many, bot.sync_base, bot.sync_retakes, bot.lock_grades,
         bot.SWR_SECTIONS) = keep


def test_portal():
    bot = _package("uninotifier.bot")
    monitor = _package("uninotifier.monitor")
    from uninotifier import portal

    # on demand only: the scheduled monitor never touches the portal
    assert not hasattr(monitor, "portal")
    assert not re.search(r"^\s*(import|from)\s+portal\b", Path("monitor.py").read_text(encoding="utf-8"), re.M)

    raw = [[["Course", "Mark"], ["Big Data", " 85 "], ["", ""]],
           [["menu"], ["x"]],                         # a one-column table is layout, not data
           [["Code", "Name", "Hours"], ["1", "Databases", "3"]]]
    tables = portal.clean_tables(raw)
    assert tables == [[["Course", "Mark"], ["Big Data", "85"]], [["Code", "Name", "Hours"], ["1", "Databases", "3"]]]
    lines = portal.table_lines(tables)
    assert lines[0] == "*Course | Mark*" and lines[1] == "Big Data | 85"
    assert "more rows" in portal.table_lines(tables, limit=2)[-1]

    text = "الساعات المنجزة: 33\nالمعدل التراكمي 72.5\nالرصيد: -1,250.50"
    assert portal.find_figures(text) == {"passed_hours": 33.0, "cgpa": 72.5, "balance": -1250.5}
    assert portal.find_figures("Passed credit hours 45 | Cumulative GPA 68.4 | Balance 120") == {
        "passed_hours": 45.0, "cgpa": 68.4, "balance": 120.0}
    assert portal.find_figures("nothing useful") == {}

    # credentials: from .env, never asked for in chat
    with tempfile.TemporaryDirectory() as tmp:
        portal.ENV_FILE = Path(tmp) / ".env"
        saved_env = {k: os.environ.pop(k, None) for k in ("PORTAL_USERNAME", "PORTAL_PASSWORD")}
        try:
            assert portal.credentials() is None
            portal.ENV_FILE.write_text("PORTAL_USERNAME=209900001\nPORTAL_PASSWORD=secret\n", encoding="utf-8")
            assert portal.credentials() == ("209900001", "secret")
        finally:
            for k, v in saved_env.items():
                if v is not None:
                    os.environ[k] = v
    text = bot.portal_setup_text()
    assert "PORTAL_USERNAME" in text and "PORTAL_PASSWORD" in text and "never send" in text

    # the portal's verification code is relayed through the chat
    replies = []
    orig = bot.send_view
    bot.send_view = lambda text, rows: replies.append(text)
    try:
        assert not bot.handle_portal_code("123456"), "nothing is waiting for a code"
        import queue
        bot._portal_code = waiting = queue.Queue()
        assert not bot.handle_portal_code("what is this")
        assert bot.handle_portal_code("481 516") and waiting.get_nowait() == "481516"
        assert "Code received" in replies[-1]
    finally:
        bot.send_view, bot._portal_code = orig, None

    # results: screenshot + figures + the full text; the transcript offers "use in /gpa"
    sent_files, sent_msgs = [], []
    orig = (bot.notifier.send_png_file, bot.notifier.send_message, bot.notifier.send_long_message)
    bot.notifier.send_png_file = lambda tok, chat, png, name, caption="", url=None, rows=None: (
        sent_files.append((name, caption, rows)) or True)
    bot.notifier.send_message = lambda tok, chat, text, url=None, rows=None: sent_msgs.append((text, rows)) or True
    bot.notifier.send_long_message = lambda tok, chat, text, url=None, rows=None: sent_msgs.append((text, rows)) or True
    try:
        from uninotifier import gpa
        keep_gpa = gpa.STATE_FILE
        gpa_tmp = tempfile.TemporaryDirectory()
        gpa.STATE_FILE = Path(gpa_tmp.name) / "gpa.json"
        gpa.save({**gpa.load(), "courses": {"Linear Algebra (1)": gpa.new_course(), "Big Data": gpa.new_course()}})
        bot.send_portal_result(portal.Result("transcript", True, b"png", tables,
                                             {"passed_hours": 33.0, "cgpa": 72.5, "record_hours": 36.0, "record_cgpa": 71.25,
                                              "base_cgpa": 71.25, "base_hours": 36.0}, [], [],
                                             {"status": "منتظم", "retakes": {"0101221": 35.0}}))
        name, caption, rows = sent_files[-1]
        assert name == "ZUJ Portal - Transcript.png"
        assert "🎓 *Cumulative GPA:* 71.25% (Portal display: 72.5%)" in caption, caption
        assert "⏱️ *Passed Hours:* 33 hrs" in caption and "📌 *Academic Status:* Regular / Good Standing" in caption
        assert "📚 *Total Attempted Hours:* 36 hrs" in caption
        assert "🏁 *Remaining Hours:* 99 hrs" in caption, "your own transcript shows the hours left too (132 − 33 passed)"
        body = caption.split("Retrieved")[1].split("\n", 1)[1]        # without the clock: "6:18:35 PM" holds a 35
        for internal in ("35", "attempted", "synced", "record"):
            assert internal not in body, (internal, caption)
        assert rows == [[{"text": "🏛️ Open Registration Portal ↗️", "url": portal.LOGIN_URL}], bot.RECORD_ROW],             "the portal link, and the way back on the answer itself"
        # under the hood: the 66-hour-style base and the retake are synced silently
        assert (gpa.load()["cgpa"], gpa.load()["passed_hours"], gpa.load()["base_source"]) == (71.25, 36.0, "portal")
        la = gpa.load()["courses"]["Linear Algebra (1)"]
        assert (la["retake"], la["old_grade"], la["retake_source"]) == (True, 35.0, "portal")
        assert not gpa.load()["courses"]["Big Data"]["retake"]
        # a transcript whose record couldn't be read leaves the retakes alone
        bot.send_portal_result(portal.Result("transcript", True, None, [], {"passed_hours": 33.0}))
        assert gpa.load()["courses"]["Linear Algebra (1)"]["retake"], "no retake data: nothing cleared"
        gpa.STATE_FILE = keep_gpa
        gpa_tmp.cleanup()
        assert any("Big Data | 85" in m[0] for m in sent_msgs), sent_msgs
        bot.send_portal_result(portal.Result("grades", False, b"png", [], {}, ["Home", "Student Services"]))
        assert "couldn't find the Grades page" in sent_files[-1][1] and "Student Services" in sent_files[-1][1]
        bot.send_portal_result(portal.Result("balance", True, None, [], {"balance": 120.0}, [],
                                             ["💰 *Total balance:* 120 JD — owed", "• First semester 2026/2027 · 120 JD"]))
        assert "Total balance:* 120" in sent_msgs[-1][0] and sent_msgs[-1][0].endswith("• First semester 2026/2027 · 120 JD"),             "no screenshot: the caption, with the details in it, goes out as ONE text message"
        bot.send_portal_result(portal.Result("transcript", True, b"png", [], {"passed_hours": 63.0, "cgpa": 71.3, "record_hours": 72.0, "record_cgpa": 71.26}, [],
                                             ["📅 *First semester 2026/2027*"]))
        assert "⏱️ *Passed Hours:* 63 hrs" in sent_files[-1][1], sent_files[-1][1]
    finally:
        bot.notifier.send_png_file, bot.notifier.send_message, bot.notifier.send_long_message = orig

    # no credentials: the bot explains .env and does not start a request
    replies = []
    orig = (bot.send_view, portal.credentials)
    bot.send_view = lambda text, rows: replies.append(text)
    portal.credentials = lambda: None
    try:
        assert bot.start_portal("grades") and "not configured" in replies[-1]
    finally:
        bot.send_view, portal.credentials = orig
    assert not bot._portal_lock.locked()


def test_maintenance_and_finals_pause():
    """A maintenance page is recognised (Arabic or English) and reported with a
    screenshot — the run ends cleanly, no exit code 1; an unexpected sign-in failure
    gets the screenshot too; a wrong password keeps its own message. Finals Watch
    pauses itself once every grade is published."""
    from datetime import timedelta

    bot = _package("uninotifier.bot")
    from uninotifier import finals
    from uninotifier import maintenance
    monitor = _package("uninotifier.monitor")
    from uninotifier import portal
    from uninotifier import zuj_auth

    # what counts as a maintenance notice
    for text, phrase in (("الموقع قيد الصيانة حالياً، يرجى المحاولة لاحقاً", "قيد الصيانة"),
                         ("نعتذر، النظام تحت  الصيانة", "تحت الصيانة"),
                         ("This site is undergoing maintenance and is currently not available", "undergoing maintenance"),
                         ("Under Maintenance", "Under Maintenance"),
                         ("503 Service Unavailable", "Service Unavailable"),
                         ("502 Bad Gateway — nginx", "Bad Gateway")):
        assert maintenance.detect(text) == phrase, (text, maintenance.detect(text))
    for text in ("Dashboard · Big Data · Database Systems", "اسم المستخدم / كلمة المرور غير صحيحة",
                 "Incorrect username or password", "", None):
        assert maintenance.detect(text) is None, text

    with tempfile.TemporaryDirectory() as tmp:
        keep_snapshot = maintenance.SNAPSHOT
        maintenance.SNAPSHOT = Path(tmp) / "maintenance_snapshot.png"
        logging.disable(logging.CRITICAL)          # the simulated failures stay out of the real logs
        try:
            # unavailable(): reads the page, keeps the screenshot, never raises itself
            e = maintenance.unavailable(_FakePage("نعتذر، البوابة قيد الصيانة"), "registration portal",
                                        TimeoutError("Timeout 30000ms exceeded"))
            assert e.phrase == "قيد الصيانة" and e.png == b"PNG" and maintenance.SNAPSHOT.read_bytes() == b"PNG"
            text = maintenance.alert_text(e)
            assert text.startswith("⚠️ *تعذر الدخول للبوابة، مرفق لقطة للشاشة الحالية للتحقق من الحالة*"), text
            assert "Under maintenance" in text and "قيد الصيانة" in text and _legacy_markdown_ok(text)
            e = maintenance.unavailable(_FakePage(broken=True), "e-learning portal", "the form never appeared")
            assert e.phrase is None and e.png is None and e.detail == "the form never appeared"
            text = maintenance.alert_text(e, "Try again later.")
            assert text.splitlines()[0] == maintenance.HEADLINE and "not a wrong password" in text
            assert "E-learning portal" in text and "مرفق لقطة" not in text, "no picture: no mention of one"
            assert _legacy_markdown_ok(text)

            # a failure on a page that is already inside the portal says so, instead of a bare timeout
            timeout = RuntimeError('Page.fill: Timeout 30000ms exceeded. Call log: - waiting for locator("input[type=\\"email\\"]")')
            e = maintenance.unavailable(_FakePage("Dashboard · Course overview"), "e-learning portal", timeout, signed_in=True)
            assert e.phrase is None and e.signed_in and "already signed in" in str(e)
            text = maintenance.alert_text(e)
            assert "Your password is fine" in text and "already inside the portal" in text, text
            assert "uninotifier.tools.login_refresh" in text and "next check tries again by itself" in text, text
            assert "not a wrong password" not in text and "Timeout 30000ms" in text and _legacy_markdown_ok(text), text
            e = maintenance.unavailable(_FakePage("Service Unavailable"), "e-learning portal", timeout, signed_in=True)
            assert "Under maintenance" in maintenance.alert_text(e), "a maintenance notice still wins"

            # the Moodle sign-in sorts its failures
            browser = monitor.Browser()
            keep_auth = monitor.is_authenticated
            try:
                for error, page_text, expected in (
                        (zuj_auth.InvalidCredentialsError("Your password is incorrect"), "", zuj_auth.InvalidCredentialsError),
                        (zuj_auth.MFARequiredError("2FA"), "", zuj_auth.MFARequiredError),
                        (RuntimeError("net::ERR_CONNECTION_RESET at https://login.microsoftonline.com"), "", RuntimeError),
                        (RuntimeError("net::ERR_CONNECTION_RESET"), "Service Unavailable", maintenance.PortalUnavailable),
                        (RuntimeError("Page.fill: Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator('input')"),
                         "", maintenance.PortalUnavailable),
                        (zuj_auth.LoginError("Sign-in flow did not reach the portal"), "", maintenance.PortalUnavailable)):
                    browser._page = _FakePage(page_text)
                    monitor.is_authenticated = lambda page, error=error: (_ for _ in ()).throw(error)
                    try:
                        browser.sign_in()
                        raise AssertionError("should have raised")
                    except Exception as raised:
                        assert type(raised) is expected, (error, raised)
                # the same timeout, with the browser on the dashboard / on Microsoft's page / on the OIDC redirect
                for url, inside in (("https://elearning.zuj.edu.jo/my/", True),
                                    ("https://login.microsoftonline.com/common/oauth2/authorize", False),
                                    ("https://elearning.zuj.edu.jo/auth/oidc/?source=loginpage", False)):
                    browser._page = _FakePage("Dashboard", url=url)
                    monitor.is_authenticated = lambda page: (_ for _ in ()).throw(RuntimeError(
                        'Page.fill: Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator("input[type=\\"email\\"]")'))
                    try:
                        browser.sign_in()
                        raise AssertionError("should have raised")
                    except maintenance.PortalUnavailable as raised:
                        assert raised.signed_in is inside, (url, raised.signed_in)
            finally:
                monitor.is_authenticated = keep_auth
                browser._page = None

            # the registration portal: a maintenance page instead of the sign-in form
            try:
                portal.login(_FakePage("Service Unavailable"), "u", "p", lambda *a: None)
                raise AssertionError("should have raised")
            except maintenance.PortalUnavailable as raised:
                assert raised.site == "registration portal" and raised.phrase == "Service Unavailable"

            # monitor.run: maintenance ends cleanly (exit 0) with the screenshot; then "back to normal"
            keep = {k: getattr(monitor, k) for k in ("DATA_FILE", "USERNAME", "PASSWORD", "BOT_TOKEN", "CHAT_ID",
                                                     "run_scan", "send_schedule_messages", "mfa_allowed",
                                                     "send_telegram", "SCAN_RETRY_SECONDS")}
            keep_png, keep_summary = monitor.notifier.send_png_file, dict(monitor.SUMMARY)
            monitor.DATA_FILE = Path(tmp) / "saved_items.json"
            monitor.USERNAME = monitor.PASSWORD = monitor.BOT_TOKEN = monitor.CHAT_ID = "x"
            monitor.send_schedule_messages = lambda saved: None
            monitor.mfa_allowed = lambda saved: False
            monitor.SCAN_RETRY_SECONDS = (0, 0)
            pngs, texts, scans = [], [], []
            monitor.notifier.send_png_file = lambda token, chat, png, name, caption="", url=None, rows=None: \
                pngs.append((name, caption)) or True
            monitor.send_telegram = lambda text, url=None: texts.append(text) or True
            course = {"1": {"name": "20261-Big Data-AllSec-3-", "url": "https://x/c"}}
            item = {"1-1": {"name": "Lecture 1", "type": "label", "course": "20261-Big Data-AllSec-3-", "cmid": "1",
                            "url": "https://x/mod/label/view.php?id=1", "hash": "h"}}
            monitor.save_data({**monitor.load_saved_data(), "courses": dict(course), "content": dict(item)})
            before = json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))

            def failing(error):
                def run_scan(saved, notify_new, mfa_handler=None):
                    scans.append(1)
                    raise error
                return run_scan

            def exit_code():
                try:
                    monitor.run()
                except SystemExit as ex:
                    return ex.code
                return 0
            try:
                monitor.run_scan = failing(maintenance.PortalUnavailable(
                    "e-learning portal", "the session was refused", "Under Maintenance", b"PNG"))
                assert exit_code() == 0 and len(scans) == 1, "no crash, and no pointless retries"
                assert pngs[-1][0] == "maintenance_snapshot.png" and "Under maintenance" in pngs[-1][1]
                assert "tell you when it's back" in pngs[-1][1] and monitor.SUMMARY["maintenance"] == "Under Maintenance"
                after = json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))
                assert after["content"] == before["content"] and after["courses"] == before["courses"], "state untouched"
                assert exit_code() == 0 and len(pngs) == 1, "a second maintenance run: no repeat alert (cooldown)"

                monitor.run_scan = failing(maintenance.PortalUnavailable(
                    "e-learning portal", "the form never appeared", None, b"PNG"))
                assert exit_code() == 1 and "the form never appeared (not a wrong password)" in pngs[-1][1]

                monitor.run_scan = failing(zuj_auth.InvalidCredentialsError("Your password is incorrect"))
                assert exit_code() == 1 and "Portal login failed" in texts[-1] and len(pngs) == 2, "no screenshot for a wrong password"

                monitor.SUMMARY.clear()
                monitor.run_scan = lambda saved, notify_new, mfa_handler=None: {
                    "courses": dict(course), "content": dict(item), "deadlines": {}, "artifacts": {}, "updates": {},
                    "edits": {}, "posts": {}, "forum_posts": {}, "submissions": {}, "link_changes": [],
                    "seconds": 1.0, "browser_used": False}
                assert exit_code() == 0 and any("Back to normal" in t for t in texts), texts
            finally:
                for k, v in keep.items():
                    setattr(monitor, k, v)
                monitor.notifier.send_png_file = keep_png
                monitor.SUMMARY.clear()
                monitor.SUMMARY.update(keep_summary)

            # the bot: an on-demand portal request that meets maintenance
            sent_png, views = [], []
            orig = (bot.portal.fetch_many, bot.notifier.send_png_file, bot.send_view, bot.edit_view, bot.load_last_run)
            bot.portal.fetch_many = lambda sections, ask: (_ for _ in ()).throw(maintenance.PortalUnavailable(
                "registration portal", "the sign-in page shows a maintenance notice", "قيد الصيانة", b"PNG"))
            bot.notifier.send_png_file = lambda token, chat, png, name, caption="", url=None, rows=None: \
                sent_png.append(caption) or True
            bot.send_view = lambda text, rows: views.append(text) or {"message_id": 1}
            bot.edit_view = lambda message_id, text, rows: views.append(text)
            try:
                assert bot._portal_lock.acquire(blocking=False)
                bot.run_portal("grades", None)
                assert "Registration portal" in sent_png[-1] and "Under maintenance" in sent_png[-1]
                assert sent_png[-1].startswith("⚠️ *تعذر الدخول للبوابة") and "Try again" in sent_png[-1]
                assert views[-1].startswith("📊 *GPA & Grade Hub*") and not bot._portal_lock.locked(),                     "the screenshot says why; the hub it came from returns under it (no extra status message)"
                assert not any("retrieved" in v or "maintenance." in v for v in views)
                bot.load_last_run = lambda: {"finished": time.time() - 60, "ok": False, "maintenance": "Under Maintenance"}
                assert bot.monitor_status().startswith("🛠️ *Portal under maintenance*")
            finally:
                (bot.portal.fetch_many, bot.notifier.send_png_file, bot.send_view, bot.edit_view, bot.load_last_run) = orig
        finally:
            maintenance.SNAPSHOT = keep_snapshot
            logging.disable(logging.INFO)

    # Finals Watch pauses itself when every grade is out
    with tempfile.TemporaryDirectory() as tmp:
        keep = (finals.STATE_FILE, finals.evaluate)
        finals.STATE_FILE = Path(tmp) / "finals.json"
        published = {"n": 2}

        def evaluate(state, grades, transcript):
            state.update(term="First semester 2026/2027", published=published["n"], total=3,
                         complete=published["n"] == 3)
            return [{"text": "🎓 *Grade Released*", "rows": []}]
        finals.evaluate = evaluate
        try:
            today = date.today()
            sch = {"finals": {"from": str(today - timedelta(days=1)), "to": str(today + timedelta(days=20))}}
            state = finals.load()
            assert finals.active(state=state, sch=sch), "inside the window: on by itself"
            msgs, state = finals.check(lambda sections: {"transcript": None}, state, sch)
            assert len(msgs) == 1 and finals.active(state=state, sch=sch), "2 of 3: keeps watching"
            published["n"] = 3
            msgs, state = finals.check(lambda sections: {"transcript": None}, state, sch)
            assert [m["text"].splitlines()[0] for m in msgs] == ["🎓 *Grade Released*", "⏸️ *Finals Watch Paused*"]
            assert msgs[-1]["rows"][0][0]["callback_data"] == "finals:on"
            assert not finals.active(state=finals.load(), sch=sch) and not finals.due(state=finals.load(), sch=sch)
            assert "paused automatically" in finals.status_text(finals.load(), sch)
            msgs, _ = finals.check(lambda sections: {"transcript": None}, finals.load(), sch)
            assert len(msgs) == 1, "a manual check while paused doesn't announce the pause again"
            # /finals on resumes; the next complete reading pauses again
            state = finals.load()
            state["enabled"] = True
            assert finals.active(state=state, sch=sch)
            msgs, state = finals.check(lambda sections: {"transcript": None}, state, sch)
            assert not state["enabled"] and "Paused" in msgs[-1]["text"]
            # next season's window arms it again
            later = {"finals": {"from": str(today), "to": str(today + timedelta(days=20))}}
            assert finals.active(state=state, sch=later)
            for m in msgs:
                assert _legacy_markdown_ok(m["text"])
        finally:
            finals.STATE_FILE, finals.evaluate = keep


def test_guest_lookup():
    """guest.py reads someone else's record and touches nothing of the owner's: the
    report lists passed and owed courses (a renumbered retake counts), the status,
    this term's retakes and the averages CGPA targets need."""
    from uninotifier import guest
    from uninotifier import portal

    semesters = [
        _guest_sem("First semester 2026/2027", [("0101221", "جبر خطي (1)", 3, None), ("0135220", "قواعد البيانات", 3, None),
                                           ("0135321", "البيانات الضخمة", 3, None)]),
        _guest_sem("First semester 2025/2026", [("0112212", "تراكيب البيانات", 3, 30), ("0135323", "تنقيب البيانات", 3, 72)], 51),
        _guest_sem("First semester 2024/2025", [("0101221", "جبر خطي (1)", 3, 35), ("0142251", "قواعد البيانات", 3, 20),
                                           ("0112220", "برمجة كينونية", 3, 75)], 43),
    ]
    result = portal.Result("transcript", True, None, [], {"passed_hours": 6.0, "cgpa": 52.0, "record_cgpa": 52.33,
                                                          "record_hours": 15.0}, [], [],
                           {"semesters": semesters, "status": "منتظم"})
    text = guest.build_report("202400001", result)
    assert "Cumulative GPA:       52.33%  (the portal shows 52%)" in text and "Passed credit hours:  6" in text
    assert "Academic status:      Regular / Good Standing" in text and "COURSES TAKEN — 2 passed" in text
    owed = text.split("FAILED AND NOT YET PASSED")[1].split("THIS TERM")[0]
    assert "0112212" in owed and "being retaken" not in owed.split("0112212")[1].split("\n")[0]
    assert "0142251" in owed and "→ being retaken this term" in owed.split("0142251")[1].split("\n")[0], \
        "renumbered Databases: same name, so it counts as retaken"
    assert "0101221  جبر خطي (1)  · 3h  🔁 retake — replaces 35" in text
    assert "0135220  قواعد البيانات  · 3h  🔁 retake — replaces 35" in text, "20 is recorded as 35"
    # targets: base = record before this term (3 attempts + 2 = 15 h); retakes replace, no extra hours
    plans = dict(guest.targets(semesters))
    base, hours = (30 * 0 + 35 * 3 + 35 * 3 + 75 * 3 + 35 * 3 + 72 * 3) / 15, 15     # failures floored at 35
    assert abs(plans[60]["needed"] - (60 * (hours + 3) - base * hours + 35 * 3 + 35 * 3) / 9) < 1e-9
    assert "TO REACH A CUMULATIVE GPA OF" in text and "60%  →" in text
    assert guest.build_report("x", portal.Result("transcript", True)).endswith("no courses could be read from it.")

    # the portal read: the guest's credentials, and nothing written to portal_debug/
    import inspect
    params = inspect.signature(portal.fetch_many).parameters
    assert "creds" in params and params["keep_debug"].default is True
    src = inspect.getsource(guest)
    for forbidden in (r"\bnotifier\b", r"gpa\.save", r"save_data", r"send_message", r"\breminders\b", r"set_base"):
        assert not re.search(forbidden, src), forbidden
    assert "keep_debug=False" in src and "creds=(username, password)" in src and "getpass" in src


def test_guest_summary_and_target_math():
    """The guest's major comes from the financial page's column (nothing else of that page is used), the
    hours left from `degree_hours` when the plan is known (never invented), and the target calculator
    follows the university's rules and agrees with the existing planner for the term in progress."""
    from uninotifier import guest, portal
    bot = _package("uninotifier.bot")

    # --- the major
    table = [["", "الفصل الدراسي", "الحالة", "التخصص", "عدد الساعات", "الرصيد"],
             ["", "الفصل الأول 2026/2027", "منتظم", "نظم المعلومات", "18", "د.أ1250"],
             ["", "الفصل الثاني 2025/2026", "منتظم", "علم الحاسوب", "12", "د.أ0"]]
    assert portal.parse_major([table]) == "نظم المعلومات", "the newest semester's"
    assert portal.parse_major([[["الفصل الدراسي", "الرصيد"], ["x", "1"]]]) is None
    assert portal.parse_major([[["التخصص"], [""], ["12"], ["علم الحاسوب"]]]) == "علم الحاسوب", "empty and numeric cells skipped"
    assert portal.parse_major([]) is None

    # --- degree_hours is a setting like the others
    from uninotifier import schedule
    base = {"days": {}, "degree_hours": 132}
    assert schedule.validate(base) == [] and schedule.validate({"days": {}, "degree_hours": 132.5}) == []
    assert schedule.validate({"days": {}, "degree_hours": None}) == [], "null = not set (the example's value)"
    for bad in ("132", 10, 500, True):
        assert schedule.validate({"days": {}, "degree_hours": bad}), bad
    assert "degree_hours" in schedule.KNOWN_KEYS

    # --- the trajectory: the same answer as the planner for this term, and the formula for more terms
    semesters = [
        _guest_sem("First semester 2026/2027", [("0101221", "جبر خطي (1)", 3, None), ("0135220", "قواعد البيانات", 3, None),
                                           ("0135321", "البيانات الضخمة", 3, None)]),
        _guest_sem("First semester 2025/2026", [("0112212", "تراكيب البيانات", 3, 30), ("0135323", "تنقيب البيانات", 3, 72)], 51),
        _guest_sem("First semester 2024/2025", [("0101221", "جبر خطي (1)", 3, 35), ("0142251", "قواعد البيانات", 3, 20),
                                           ("0112220", "برمجة كينونية", 3, 75)], 43),
    ]
    plans = dict(guest.targets(semesters))
    for target in guest.TARGETS:
        plan = guest.trajectory(semesters, target)
        n, hours, needed, verdict = plan["rows"][0]
        assert n == 1 and hours == 9.0 and abs(needed - plans[target]["needed"]) < 1e-9, (target, needed, plans[target])
    plan = guest.trajectory(semesters, 70)
    assert plan["in_progress"] and plan["term_hours"] == 9.0 and plan["base"][1] == 9.0, plan["base"]   # the two retaken courses left the record: 3 courses, 9 h
    two = plan["rows"][1]
    base_cgpa, base_hours = plan["base"]
    assert abs(two[2] - (70 * (base_hours + 18) - base_cgpa * base_hours) / 18) < 1e-9 and two[1] == 18.0
    assert plan["rows"][0][2] > plan["rows"][1][2] > plan["rows"][2][2], "more terms: a lower average is enough"

    # no term in progress: a full load (18 h) is assumed, and the verdicts
    done = [_guest_sem("First semester 2025/2026", [("1", "A", 3, 60)], 60)]
    nothing = guest.trajectory(done, 70)
    assert not nothing["in_progress"] and nothing["term_hours"] == 18 and abs(nothing["rows"][0][2] - (70 * 21 - 180) / 18) < 1e-9
    assert [guest.trajectory(done, t)["rows"][0][3] for t in (5, 40, 70, 100)] == ["secured", "pass", "ok", "out"]
    assert guest.trajectory([], 80)["rows"][0][2] == 80, "no record yet: the target itself"

    # --- the summary carries the hours left only when the plan's size is known
    results = {"transcript": portal.Result("transcript", True, b"PNG", [], {"passed_hours": 33.0, "cgpa": 60.0},
                                           [], [], {"semesters": semesters, "status": "منتظم"}),
               "balance": portal.Result("balance", True, b"PNG", [table], {}, [], [])}
    captions, texts = [], []
    keep = (bot.api, bot.notifier.upload, schedule.load_schedule)
    bot.api = lambda method, **d: texts.append(d.get("text", "")) or {"message_id": 1}
    bot.notifier.upload = lambda token, method, data, field, name, payload: captions.append(data["caption"]) or {"message_id": 2}
    bot._guest.clear()
    try:
        schedule.load_schedule = lambda: {"days": {}}
        bot.send_guest_results("2024", results)
        assert "Remaining Hours" not in captions[-1] and "📘 *Major:*" in captions[-1]
        schedule.load_schedule = lambda: {"days": {}, "degree_hours": 132}
        bot.send_guest_results("2024", results)
        assert "🏁 *Remaining Hours:* 99 hrs" in captions[-1], captions[-1]
        assert captions[-1].index("📘 *Major:*") < captions[-1].index("🎓 *Cumulative GPA:*") < captions[-1].index("🏁 *Remaining Hours:*")
        assert bot._guest["record"]["figures"]["passed_hours"] == 33.0
        # the financial page is only for the major: a page that failed to load simply leaves it out
        results["balance"] = portal.Result("balance", False)
        bot.send_guest_results("2024", results)
        assert "📘 *Major:*" not in captions[-1]
    finally:
        bot.api, bot.notifier.upload, schedule.load_schedule = keep
        bot._guest.clear()


def test_guest_lookup_in_telegram():
    """/guest: student ID, then the password (deleted from the chat at once), their
    verification code relayed, their record sent one message after another — and none
    of the owner's data touched; 🚪 Sign Out removes it all from the chat."""
    bot = _package("uninotifier.bot")
    from uninotifier import portal

    semesters = [_guest_sem("First semester 2026/2027", [("0101221", "جبر خطي (1)", 3, None)]),
                 _guest_sem("First semester 2025/2026", [("0101221", "جبر خطي (1)", 3, 40),
                                                          ("0135323", "تنقيب البيانات", 3, 72)], 56)]
    results = {
        "transcript": portal.Result("transcript", True, b"PNG", [], {"passed_hours": 3.0, "cgpa": 56.0,
                                                                     "base_cgpa": 56.0, "base_hours": 6.0},
                                    [], [], {"semesters": semesters, "status": "منتظم", "retakes": {"0101221": 40.0}}),
        "grades": portal.Result("grades", True, b"PNG", [], {}, [], ["ℹ️ No results are published yet."]),
        "balance": portal.Result("balance", True, b"PNG-FINANCE", [[
            ["", "الفصل الدراسي", "الحالة", "التخصص", "عدد الساعات", "الرصيد"],
            ["", "الفصل الأول 2026/2027", "منتظم", "نظم المعلومات", "18", "د.أ1250"]]],
            {"balance": 1250.0}, [], ["💰 *Total balance:* 1,250 JD — owed to the university (debit)"]),
    }
    calls, files, fetched, threads, markups = [], [], [], [], []

    def fake_api(method, **data):
        calls.append((method, data))
        return {"message_id": 100 + len(calls)} if method == "sendMessage" else True

    class Inline:                                    # run the worker in this thread
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            threads.append(1)
            self.target(*self.args)
    gpa_before = gpa.STATE_FILE.read_text(encoding="utf-8") if gpa.STATE_FILE.exists() else None
    keep = (bot.api, bot.send_view, bot.notifier.upload, bot.portal.fetch_many, bot.threading.Thread)
    bot.api = fake_api
    bot.send_view = lambda text, rows: calls.append(("view", {"text": text})) or {"message_id": 1}
    bot.notifier.upload = lambda token, method, data, field, name, payload: files.append((name, data["caption"])) \
        or markups.append(json.loads(data["reply_markup"])["inline_keyboard"]) or {"message_id": 500 + len(files)}

    def fake_fetch(sections, ask_code, creds=None, keep_debug=True):
        fetched.append((sections, creds, keep_debug))
        return results
    bot.portal.fetch_many = fake_fetch
    bot.threading.Thread = Inline
    try:
        bot.handle_command("/guest", 10)
        assert bot._guest["step"] == "user" and "student ID" in calls[-1][1]["text"]
        bot.handle_command("not an id!", 11)
        assert "doesn't look like a student ID" in calls[-1][1]["text"] and bot._guest["step"] == "user"
        bot.handle_command("202400001", 12)
        assert bot._guest["step"] == "password"
        bot.handle_command("s3cret-Pass", 13)
        assert ("deleteMessage", {"chat_id": bot.CHAT_ID, "message_id": 13}) in calls, "password deleted at once"
        assert fetched == [(["transcript", "grades", "balance"], ("202400001", "s3cret-Pass"), False)], \
            "the record, this term's marks, and the financial page for the major only"
        texts = [d.get("text", "") for m, d in calls if m == "sendMessage"]
        assert not any("s3cret" in t for t in texts + [c for _, c in files]), "the password is never echoed"
        # the owner's transcript layout: the screenshot carries the executive summary (with the major) ...
        assert [n for n, _ in files] == ["Guest 202400001 - Transcript.png"], "one file: no grades or finance screenshot"
        caption = files[0][1]
        assert caption.startswith("👤 *Guest 202400001* — 📜 Transcript") and "📘 *Major:* \u2068نظم المعلومات\u2069" in caption
        assert "⏱️ *Passed Hours:* 3 hrs" in caption and "🎓 *Cumulative GPA:*" in caption
        assert "📌 *Academic Status:* Regular / Good Standing" in caption
        assert "🏁 *Remaining Hours:* 129 hrs" in caption, "the degree plan (132 h in schedule.json) minus the 3 passed"
        # ... and the courses are a button away: no wall of text follows the transcript
        assert not any("In Progress" in t for t in texts), texts
        assert markups == [[[{"text": "\u2705 Courses Taken", "callback_data": "guest:taken"}]]], markups
        assert not any("1,250" in t or "1250" in t or "debit" in t for t in texts + [c for _, c in files]), \
            "nothing of the financial page is shown"
        assert not any("Schedule" in t for t in texts), "no class or exam schedule"
        assert bot._guest["step"] == "done" and not bot._portal_lock.locked()
        assert bot._guest["record"]["semesters"] and "balance" not in bot._guest["record"]
        final = [d for m, d in calls if m == "sendMessage"][-1]
        assert [r[0]["callback_data"] for r in final["reply_markup"]["inline_keyboard"]] == ["guest:target", "guest:wipe", "guest:out"]
        panel = bot._guest["panel"]

        # ✅ Courses Taken: the semesters, as one message of the session (🚪 removes it too)
        calls.clear()
        bot.handle_callback({"id": "taken", "data": "guest:taken", "message": {"message_id": 501}})
        taken = [d["text"] for m, d in calls if m == "sendMessage"]
        assert len(taken) == 1 and taken[0].startswith("✅ *Courses Taken*") and "In Progress" in taken[0], taken
        assert "🔁 *40*" in taken[0] and "✅ *72*" in taken[0] and bot._guest["messages"][-1] > 100, "kept with the session"
        calls.clear()
        bot.handle_callback({"id": "left", "data": "guest:left", "message": {"message_id": 501}})
        assert [d["text"] for m, d in calls if m == "sendMessage"] == ["🚪 This guest session has ended."], \
            "no plan was read: nothing to list (the button is not even offered)"

        # 🎯 Target GPA: the panel itself becomes the picker, then the answer; typed values; a command keeps the session
        def press(data, mid=None):
            calls.clear()
            bot.handle_callback({"id": f"g{len(calls)}{data}", "data": data, "message": {"message_id": mid or panel}})
            return [d for m, d in calls if m == "editMessageText"]

        edit = press("guest:target")[-1]
        assert edit["message_id"] == panel and "Target GPA" in edit["text"]
        picks = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
        assert picks[:7] == [f"guest:t:{t}" for t in (60, 65, 70, 75, 80, 85, 90)] and picks[-2:] == ["guest:tcustom", "guest:back"]
        edit = press("guest:t:75")[-1]
        assert "Target GPA — Guest 202400001" in edit["text"] and "To reach *75%* cumulative" in edit["text"]
        assert "This term (3h)" in edit["text"] and "+ the next one (6h)" in edit["text"] and "+ the next two (9h)" in edit["text"]
        assert "🎓 *Now:* " in edit["text"] and "university's own rules" in edit["text"] and _legacy_markdown_ok(edit["text"])
        buttons = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
        assert buttons == ["guest:target", "guest:wipe", "guest:out"], "another target, or sign out"
        assert press("guest:back")[-1]["text"].startswith("👤 *Guest session finished*")
        press("guest:tcustom")
        assert bot._guest["step"] == "target"
        calls.clear()
        bot.handle_command("not a number", 30)
        assert "between 40 and 100" in calls[-1][1]["text"] and bot._guest["step"] == "target"
        calls.clear()
        bot.handle_command("78", 31)
        edit = [d for m, d in calls if m == "editMessageText"][-1]
        assert "To reach *78%* cumulative" in edit["text"] and edit["message_id"] == panel and bot._guest["step"] == "done"
        assert 30 in bot._guest["messages"] and 31 in bot._guest["messages"], "typed answers go with the session"
        press("guest:tcustom")
        bot.handle_command("/start", 32)                                   # a command drops the prompt, not the session
        assert bot._guest["step"] == "done" and bot._guest.get("record")
        # nothing of the owner's: no GPA base, no retakes, no portal_debug dump (keep_debug=False above)
        now = gpa.STATE_FILE.read_text(encoding="utf-8") if gpa.STATE_FILE.exists() else None
        assert now == gpa_before
        # 🚪 Sign Out & Delete Messages: every message of the session goes, the student ID included
        recorded = list(bot._guest["messages"])
        assert 12 in recorded and 13 not in recorded
        bot.handle_callback({"id": "1", "data": "guest:wipe", "message": {"message_id": recorded[-1]}})
        deleted = {d["message_id"] for m, d in calls if m == "deleteMessage"}
        assert set(recorded) <= deleted and bot._guest == {}
        assert "Signed out of the guest account" in calls[-1][1]["text"]
        late = press("guest:t:75")[-1]                                     # a stale button after sign-out
        assert late["text"] == "🚪 This guest session has ended." and late["reply_markup"]["inline_keyboard"] == []

        # a command while it waits for the password drops the prompt; so does the timeout
        bot.handle_command("/guest", 20)
        bot.handle_command("202400001", 21)
        bot.handle_command("/start", 22)
        assert bot._guest == {}
        bot.handle_command("/guest", 23)
        bot._guest["at"] -= bot.GUEST_TIMEOUT + 1
        bot.handle_command("202400001", 24)
        assert bot._guest == {}
    finally:
        (bot.api, bot.send_view, bot.notifier.upload, bot.portal.fetch_many, bot.threading.Thread) = keep
        bot._guest.clear()


def test_portal_session_is_reused_for_15_minutes():
    """The owner's portal session is kept after a read and reused within REUSE_SECONDS: the browser
    opens with its cookies and no sign-in happens. An ended session (the sign-in form comes back)
    signs in again; an older one is not even tried; a guest's sign-in never uses or touches it;
    a read that fails drops it."""
    from uninotifier import portal

    class Locator:
        def __init__(self, page, selector):
            self.page, self.selector = page, selector

        def count(self):
            return 0 if self.page.signed or "lognForm" not in self.selector else 1

    class Page:
        def __init__(self, signed):
            self.signed, self.url = signed, ""

        def goto(self, url, **kwargs):
            self.url = url

        def locator(self, selector):
            return Locator(self, selector)

        def wait_for_load_state(self, *a, **k):
            pass

        def wait_for_selector(self, *a, **k):
            pass

        def evaluate(self, *a, **k):
            return 1

        def wait_for_timeout(self, *a):
            pass

    server = {"alive": True, "states": [], "fail": False}

    class Context:
        def __init__(self, state):
            self.page = Page(signed=bool(state) and server["alive"])

        def new_page(self):
            return self.page

        def storage_state(self):
            return {"cookies": [{"name": "JSESSIONID", "value": f"s{len(server['states'])}"}], "origins": []}

    class Browser:
        def new_context(self, storage_state=None, **kwargs):
            server["states"].append(storage_state)
            return Context(storage_state)

        def close(self):
            pass

    class Playwright:
        chromium = type("C", (), {"launch": staticmethod(lambda headless=True: Browser())})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    sign_ins = []

    def login(page, user, password, ask):
        sign_ins.append(user)
        page.signed = True

    def goto_section(page, spec):
        if server["fail"]:
            raise RuntimeError("the page broke")
        return False

    keep = (_playwright_api.sync_playwright, portal.login, portal._goto_section, portal._read, portal._screenshot,
            portal._keep_debug, portal.credentials, portal.SESSION_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        _playwright_api.sync_playwright = lambda: Playwright()
        portal.login, portal._goto_section = login, goto_section
        portal._read = lambda page, script, default: default
        portal._screenshot = lambda page: b"png"
        portal._keep_debug = lambda *a: None
        portal.credentials = lambda: ("owner", "pw")
        portal.SESSION_FILE = Path(tmp) / "portal_session.json"
        try:
            portal.fetch_many(["transcript"], None)
            assert sign_ins == ["owner"] and server["states"] == [None] and portal.SESSION_FILE.exists()
            saved = json.loads(portal.SESSION_FILE.read_text(encoding="utf-8"))
            assert saved["state"]["cookies"][0]["value"] == "s1"

            portal.fetch_many(["balance"], None)
            assert sign_ins == ["owner"], "within 15 min: the session is reused, no sign-in"
            assert server["states"][-1]["cookies"][0]["value"] == "s1"

            server["alive"] = False                       # the portal ended it: the sign-in form comes back
            portal.fetch_many(["balance"], None)
            assert sign_ins == ["owner", "owner"], "an ended session signs in again, silently"

            server["alive"] = True
            saved = json.loads(portal.SESSION_FILE.read_text(encoding="utf-8"))
            saved["at"] -= portal.REUSE_SECONDS
            portal.SESSION_FILE.write_text(json.dumps(saved), encoding="utf-8")
            assert portal.saved_session() is None
            portal.fetch_many(["balance"], None)
            assert server["states"][-1] is None and len(sign_ins) == 3, "an old session is not even tried"

            before = portal.SESSION_FILE.read_text(encoding="utf-8")
            portal.fetch_many(["transcript"], None, creds=("guest", "gpw"), keep_debug=False)
            assert sign_ins[-1] == "guest" and server["states"][-1] is None, "a guest never uses the owner's session"
            assert portal.SESSION_FILE.read_text(encoding="utf-8") == before, "... nor replaces it"

            server["fail"] = True
            try:
                portal.fetch_many(["balance"], None)
                raise AssertionError("the failure must reach the caller")
            except RuntimeError:
                pass
            assert not portal.SESSION_FILE.exists(), "a failed read drops the session"
        finally:
            (_playwright_api.sync_playwright, portal.login, portal._goto_section, portal._read, portal._screenshot,
             portal._keep_debug, portal.credentials, portal.SESSION_FILE) = keep

    assert "JSESSIONID=<redacted>" in notifier.redact("Cookie: JSESSIONID=AbC123secret; Path=/")


def test_portal_answers_from_the_saved_copy():
    """Transcript and Balance answer at once from the last read ("As of"): younger than
    FRESH_SECONDS, nothing is read; older, the portal is read behind it and the same message is
    updated in place — the stamp only when nothing changed, the screenshot (and the text that
    follows) when something did. A failed refresh leaves the saved copy, saying so. The screenshot
    of a saved answer is sent from the file (by its file_id). Grades are never answered from it."""
    bot = _package("uninotifier.bot")

    sent, calls, screens, reads = [], [], [], []
    ids = iter(range(100, 1000))

    def send_document(tok, chat, path, caption, url=None, rows=None, reply=None):
        mid = next(ids)
        sent.append(("doc", path.name, caption, mid))
        if reply is not None:
            reply["body"] = {"result": {"message_id": mid}}
        return True

    def send_message(tok, chat, text, url=None, rows=None, silent=False, reply_to=None, reply=None):
        mid = next(ids)
        sent.append(("msg", text, mid))
        if reply is not None:
            reply["body"] = {"result": {"message_id": mid}}
        return True

    answers = []

    def fetch_many(sections, ask):
        reads.append(sections)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {sections[0]: answer}

    keep = (bot.notifier.send_png_file, bot.notifier.send_message, bot.notifier.send_long_message,
            bot.notifier.send_document, bot.notifier.upload, bot.send_view, bot.edit_view, bot.api,
            bot.portal.fetch_many, bot.sync_base, bot.sync_retakes, bot.lock_grades)
    bot.notifier.send_png_file = lambda tok, chat, png, name, caption="", url=None, rows=None: (
        sent.append(("png", name, caption)) or True)
    bot.notifier.send_long_message = lambda tok, chat, text, url=None, rows=None: sent.append(("long", text, rows)) or True
    bot.notifier.send_message, bot.notifier.send_document = send_message, send_document
    bot.notifier.upload = lambda tok, method, data, field, name, payload: (
        calls.append((method, data)) or {"message_id": 100, "document": {"file_id": "F2"}})
    bot.send_view = lambda text, rows: screens.append(text) or {"message_id": 1}
    bot.edit_view = lambda mid, text, rows: None
    bot.api = lambda method, **d: calls.append((method, d)) or True
    bot.portal.fetch_many = fetch_many
    bot.sync_base = bot.sync_retakes = bot.lock_grades = lambda *a: None

    def run(section):
        assert bot._portal_lock.acquire(blocking=False)
        bot.run_portal(section, None)
        assert not bot._portal_lock.locked()

    def age(section, seconds):
        meta = bot.CACHE_DIR / f"{section}.json"
        saved = json.loads(meta.read_text(encoding="utf-8"))
        saved["at"] = time.time() - seconds
        meta.write_text(json.dumps(saved), encoding="utf-8")

    def reset():
        sent.clear(); calls.clear(); screens.clear(); reads.clear()

    balance = lambda total: portal_result("balance", {"balance": total}, [f"💰 *Total balance:* {total:,.0f} JD"])  # noqa: E731

    def portal_result(section, figures, lines, data=None):
        from uninotifier import portal
        return portal.Result(section, True, b"png-" + str(figures).encode(), [], figures, [], lines, data or {})

    try:
        # no saved copy: read now (as before), and the answer is kept
        answers[:] = [balance(1250)]
        run("balance")
        assert [s[0] for s in sent] == ["png"] and reads == [["balance"]]
        assert (bot.CACHE_DIR / "balance.json").exists() and (bot.CACHE_DIR / "ZUJ Portal - Tuition Balance.png").exists()

        # a fresh copy: answered from it, nothing read
        reset()
        run("balance")
        assert reads == [] and sent[0][0] == "doc" and sent[0][1] == "ZUJ Portal - Tuition Balance.png"
        assert "🕒 *As of:*" in sent[0][2] and bot.REFRESHING not in sent[0][2] and "1,250 JD" in sent[0][2]
        assert screens == [], "the answer carries its own way back: no menu is repeated under it"

        # an old copy, nothing changed: answered at once, then only its stamp changes
        reset()
        age("balance", bot.FRESH_SECONDS + 1)
        answers[:] = [balance(1250)]
        run("balance")
        assert bot.REFRESHING in sent[0][2] and reads == [["balance"]]
        assert [c[0] for c in calls] == ["editMessageCaption"], calls
        caption = calls[0][1]["caption"]
        assert "🕒 *Retrieved:*" in caption and bot.UPDATED not in caption
        assert calls[0][1]["message_id"] == sent[0][3], "the message that was sent is the one updated"
        assert json.loads((bot.CACHE_DIR / "balance.json").read_text(encoding="utf-8"))["at"] > time.time() - 60

        # an old copy, the balance changed: the screenshot and caption are replaced in the same message
        reset()
        age("balance", bot.FRESH_SECONDS + 1)
        answers[:] = [balance(900)]
        run("balance")
        assert "1,250 JD" in sent[0][2], "first the saved copy"
        assert [c[0] for c in calls] == ["editMessageMedia"], calls
        media = json.loads(calls[0][1]["media"])
        assert "900 JD" in media["caption"] and bot.UPDATED in media["caption"] and media["type"] == "document"

        # the refresh fails: the saved copy stays, and says so
        from uninotifier import portal
        reset()
        age("balance", bot.FRESH_SECONDS + 1)
        answers[:] = [portal.PortalError("The portal refused the sign-in.")]
        run("balance")
        assert [c[0] for c in calls] == ["editMessageCaption"] and bot.REFRESH_FAILED in calls[0][1]["caption"]
        assert "900 JD" in calls[0][1]["caption"]

        # a transcript: one message (summary + the two buttons), replaced in place when the record changes
        sems = _sample_transcript()
        record = lambda cgpa: portal_result("transcript", {"cgpa": cgpa, "passed_hours": 8.0, "record_cgpa": cgpa, "record_hours": 14.0},  # noqa: E731
                                            [], {"semesters": sems, "status": "منتظم", "retakes": {}, "plan": _sample_plan()})
        reset()
        answers[:] = [record(71.3)]
        run("transcript")
        reset()
        age("transcript", bot.FRESH_SECONDS + 1)
        answers[:] = [record(71.2)]
        run("transcript")
        assert [s[0] for s in sent] == ["doc"], sent
        assert "📊 *Degree Plan:* 22 hrs" in sent[0][2] and "🏁 *Remaining:* 16 hrs · 10 after this term" in sent[0][2]
        assert [c[0] for c in calls] == ["editMessageMedia"], [c[0] for c in calls]
        assert calls[0][1]["message_id"] == sent[0][3]
        assert "71.2" in json.loads(calls[0][1]["media"])["caption"], "the new record is in the caption"
        buttons = json.loads(calls[0][1]["reply_markup"])["inline_keyboard"][0]
        assert [b["callback_data"] for b in buttons] == ["rec:taken", "rec:left"]

        assert json.loads(calls[0][1]["reply_markup"])["inline_keyboard"][-1] == bot.RECORD_ROW, "the way back is on the answer"

        # the two buttons answer from that saved transcript — no portal read. Each list ends with the other
        # list and the way back; no menu is repeated under it
        for data, start, other in (("rec:taken", "✅ *Courses Taken*", "rec:left"),
                                   ("rec:left", "📚 *Remaining Courses*", "rec:taken")):
            reset()
            bot.handle_callback({"id": data, "data": data, "message": {"message_id": 7}})
            assert [s[0] for s in sent] == ["long"] and sent[0][1].startswith(start) and reads == [], sent
            assert [b["callback_data"] for b in sent[0][2][0]] == [other] and sent[0][2][1] == bot.RECORD_ROW
            assert screens == []

        # a menu button under a file opens the menu below it; the file is never turned into a screen
        reset()
        bot.handle_callback({"id": "m", "data": "record", "message": {"message_id": 9, "document": {"file_id": "F"}}})
        assert len(screens) == 1 and screens[0].startswith("📜 *Academic Record & Balance*")
        for path in bot.CACHE_DIR.glob("transcript.*"):
            path.unlink()
        reset()
        bot.handle_callback({"id": "gone", "data": "rec:left", "message": {"message_id": 8}})
        assert sent == [] and "Open 📜 Transcript again" in calls[-1][1]["text"], "an old button says what to do"

        # grades (the Finals Watch check) are never answered from a saved copy
        assert bot.load_cached("grades") is None
    finally:
        (bot.notifier.send_png_file, bot.notifier.send_message, bot.notifier.send_long_message,
         bot.notifier.send_document, bot.notifier.upload, bot.send_view, bot.edit_view, bot.api,
         bot.portal.fetch_many, bot.sync_base, bot.sync_retakes, bot.lock_grades) = keep
