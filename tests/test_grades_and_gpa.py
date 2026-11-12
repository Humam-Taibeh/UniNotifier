"""Marks and the calculator: marks in files, the gradebook, Grade Needed."""

from support import (_approx, _legacy_markdown_ok, _package, _record, SID)

import tempfile
from pathlib import Path

from uninotifier import gpa
from uninotifier import grades
from uninotifier import media


def test_grades():
    import docx
    import openpyxl

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Course marks"])
        ws.append(["No.", "Student ID", "Name", "Quiz (10)", "Mid (30)", "Total /40"])
        ws.append([1, 202300001, "Someone", 5, 20, 25])
        ws.append([2, 209900001.0, "Me", 9, 27, 36])
        wb.save(tmp / "marks.xlsx")
        g = grades.extract_grade(tmp / "marks.xlsx", SID)
        assert g and g["summary"] == "36/40", g
        assert ("Quiz (10)", "9/10") in g["details"] and ("Mid (30)", "27/30") in g["details"], g

        d = docx.Document()
        t = d.add_table(rows=2, cols=3)
        for c, v in zip(t.rows[0].cells, ["ID", "Name", "Mark (20)"]):
            c.text = v
        for c, v in zip(t.rows[1].cells, [SID, "Me", "17"]):
            c.text = v
        d.save(tmp / "mid.docx")
        g = grades.extract_grade(tmp / "mid.docx", SID)
        assert g and g["summary"] == "17/20", g

        (tmp / "list.csv").write_text(f"id,name,grade\n{SID},Me,88\n", encoding="utf-8")
        g = grades.extract_grade(tmp / "list.csv", SID)
        assert g and g["summary"] == "88", g

        (tmp / "other.csv").write_text("id,name,grade\n1,X,5\n", encoding="utf-8")
        assert grades.extract_grade(tmp / "other.csv", SID) is None
        assert grades.extract_grade(tmp / "slides.pdf", SID) is None

        # a PDF's table comes out as plain lines: the line holding the number is the answer
        def pdf(*lines):
            text = b"BT /F1 12 Tf 50 750 Td 14 TL " + b"".join(b"(" + l.encode("latin-1") + b") Tj T* " for l in lines) + b"ET"
            objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                       b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
                       b"/Resources << /Font << /F1 5 0 R >> >> >>",
                       b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream",
                       b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
            out, offsets = b"%PDF-1.4\n", []
            for n, body in enumerate(objects, 1):
                offsets.append(len(out))
                out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
            xref = len(out)
            out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
            out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
            return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)

        (tmp / "quiz1.pdf").write_bytes(pdf("No ID Name Quiz1", "1 202300001 Someone 3", f"2 {SID} Me 4.5", f"3 {SID}7 Other 1"))
        g = grades.extract_grade(tmp / "quiz1.pdf", SID)
        assert g and g["summary"] == f"2 {SID} Me 4.5" and g["details"] == [], g
        (tmp / "others.pdf").write_bytes(pdf("No ID Name Quiz1", f"3 {SID}7 Other 1"))
        assert grades.extract_grade(tmp / "others.pdf", SID) is None, "a longer number that contains mine is not me"
        (tmp / "broken.pdf").write_bytes(b"not a pdf")
        assert grades.extract_grade(tmp / "broken.pdf", SID) is None

        # a sentence in a document, and a list typed with Arabic digits
        (tmp / "note.txt").write_text(f"Student {SID} got 18 out of 20\n", encoding="utf-8")
        assert grades.extract_grade(tmp / "note.txt", SID)["summary"] == f"Student {SID} got 18 out of 20"
        arabic = SID.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
        (tmp / "ar.csv").write_text(f"الرقم,الاسم,العلامة\n{arabic},أنا,١٧\n", encoding="utf-8")
        assert grades.extract_grade(tmp / "ar.csv", SID)["summary"] == "17"

    update = media.build_update_message({"name": "Marks", "type": "resource", "course": "20261-Big Data-AllSec-3-"},
                                        None, None, grade={"summary": "36/40", "details": [("Mid (30)", "27/30")]})
    assert update.startswith("🎉 *Marks Detected:* *36/40*\n🔄 *File Updated*") and "• Mid (30): 27/30" in update, update


def test_gradebook_record():
    from uninotifier import gradebook

    sems = _record()
    record, hours = gradebook.cumulative(gradebook.attempts(sems))
    assert (record, hours) == (50.0, 9.0), "failed attempts count, at 35"
    assert gradebook.recorded(12) == 35 and gradebook.recorded(35) == 35 and gradebook.recorded(61) == 61

    # a retake replaces the old attempt — by code, or by name when the code changed — and hours aren't repeated
    marks = {"0101221": 70.0, "0135220": 45.0, "0101272": 33.0}
    rep = gradebook.season_report(sems, marks)
    assert rep["complete"] and rep["published"] == 3 and rep["total"] == 3
    assert rep["before"] == (50.0, 9.0)
    after, after_hours = rep["after"]
    assert after_hours == 12.0 and abs(after - (80 + 70 + 45 + 35) / 4) < 1e-9, "33 is recorded as 35"
    assert abs(rep["semester_average"] - 50.0) < 1e-9
    assert rep["replaced"] == [("0101221", "جبر خطي (1)", 35.0, 70.0), ("0135220", "قواعد البيانات", 35.0, 45.0)]
    assert rep["floored"] == [("تحليل عددي (1)", 33)]
    assert [c["passed"] for c in rep["courses"]] == [True, False, False]

    # partly published: the record only changes for what is out
    partial = gradebook.season_report(sems, {"0101221": 70.0})
    assert not partial["complete"] and partial["published"] == 1 and partial["semester_average"] == 70.0
    assert partial["after"] == (((80 + 70 + 35) * 3) / 9, 9.0)
    # a mark already on the transcript needs no overlay
    sems2 = _record()
    sems2[0]["courses"][0]["mark"] = 90
    assert gradebook.season_report(sems2, {})["published"] == 1
    assert gradebook.season_report([], {})["total"] == 0


def test_results_page_parser():
    from uninotifier import portal_pages

    grids = [{"heads": ["x"], "rows": []},
             {"heads": ["رمز المقرر", "اسم المقرر", "النشاط", "الحالة", "العلامة", "ناجح؟"],
              "rows": [["0101221", "جبر خطي (1)", "نظري", "مسجلة", "70", "\u2713"],
                       ["0135220", "قواعد البيانات", "نظري", "مسجلة", "", "\u2717"],
                       ["لا يوجد نتائج للمساقات المسجلة حاليا"]]}]
    rows = portal_pages.parse_results(grids)
    assert rows == [
        {"code": "0101221", "name": "جبر خطي (1)", "activity": "نظري", "status": "مسجلة", "mark": 70.0, "passed": True},
        {"code": "0135220", "name": "قواعد البيانات", "activity": "نظري", "status": "مسجلة", "mark": None, "passed": False}]
    assert portal_pages.parse_results([{"heads": ["رمز المقرر", "العلامة"], "rows": [["لا يوجد نتائج"]]}]) == []
    lines = portal_pages.results_lines({"term": "الفصل الأول 2026/2027", "text": ""}, [], rows)
    assert lines[0] == "📅 *First semester 2026/2027*" and lines[1].endswith("(نظري) · 70 ✅") and lines[2].endswith("· —")


def test_a_course_cannot_be_asked_for_more_than_it_can_still_reach():
    """Coursework 30 of 50 leaves 80 at best: the target is shared out so that course gives
    what it can and the others make up for it; out of reach is said from the real ceilings."""
    def course(marks=None):
        return dict(gpa.new_course(), syllabus=[["Coursework", 50.0], ["Final", 50.0]] if marks is not None else None,
                    marks=marks or {})
    courses = {"Lost Twenty": course({"Coursework": 30.0}), "No Syllabus": course(), "Lost Five": course({"Coursework": 45.0})}
    assert [gpa.ceiling(c) for c in courses.values()] == [80.0, 100.0, 95.0]
    even = gpa.plan(courses, 70, "semester")
    assert even["status"] == "reachable" and set(even["each"].values()) == {70.0}, "below every ceiling: shared equally"
    tilted = gpa.plan(courses, 85, "semester")
    assert tilted["status"] == "reachable" and tilted["each"] == {"Lost Twenty": 80.0, "No Syllabus": 87.5, "Lost Five": 87.5}
    assert abs(tilted["semester"] - 85) < 1e-9, "the shares still come to the target"
    beyond = gpa.plan(courses, 92, "semester")
    assert beyond["status"] == "out_of_reach" and abs(beyond["semester"] - 275 / 3) < 1e-9,         "80 + 100 + 95 is the most: 92 cannot be reached although it is under 100"
    from uninotifier import gpa_ui
    keep = (gpa.load, gpa.save)
    state = dict(gpa.load(), courses=courses)
    gpa.load, gpa.save = (lambda: state), (lambda s: None)
    try:
        text = gpa_ui.view_plan(list(courses), "s", 85)[0]
        assert "*Lost Twenty* · 3h → needs *80* — the most it can still reach" in text, text
        assert "🔝 Best possible: 80 (20 already lost)" in text and "*No Syllabus* · 3h → needs *87.5*" in text
        assert "Needed on average:* 85%" in text
        assert "Out of reach" in gpa_ui.view_plan(list(courses), "s", 92)[0]
    finally:
        gpa.load, gpa.save = keep


def test_gpa_formulas():
    from uninotifier import gpa

    def course(hours=3, grade=None, retake=False, old=None):
        return {**gpa.new_course(), "hours": hours, "grade": grade, "retake": retake, "old_grade": old}

    courses = {"A": course(3, 80), "B": course(3, 60), "C": course(2, 90), "D": course()}
    assert _approx(gpa.semester_average(courses), 75.0), "ungraded courses are left out"
    assert gpa.semester_average({"D": course()}) is None
    assert gpa.DEFAULT_HOURS == 3 and gpa.PASS_MARK == 50
    assert not hasattr(gpa, "SCHOLARSHIP_MARK") and not hasattr(gpa, "standing")

    # CGPA = (CGPA * passed + semester average * semester hours) / (passed + semester hours)
    assert _approx(gpa.cumulative(courses, 70, 30), (70 * 30 + 75 * 8) / 38)
    assert gpa.cumulative(courses, None, None) is None and gpa.cumulative(courses, None, 30) is None
    assert _approx(gpa.cumulative(courses, None, 0), 75.0), "a first semester needs no CGPA"

    # a retake replaces the old grade in the points and adds no hours
    retake = {"R": course(3, 80, True, 55)}
    assert _approx(gpa.cumulative(retake, 70, 30), (70 * 30 - 55 * 3 + 80 * 3) / 30)
    mixed = {**retake, "A": course(3, 80)}
    assert _approx(gpa.cumulative(mixed, 70, 30), (70 * 30 - 55 * 3 + 80 * 3 + 80 * 3) / 33)
    assert _approx(gpa.semester_average(mixed), 80.0), "retakes still count in the semester average"

    # grade from marks once every part has one
    c = {**gpa.new_course(), "syllabus": [["Midterm", 30.0], ["Final", 70.0]], "marks": {"Midterm": 24.0}}
    assert gpa.course_grade(c) is None
    c["marks"]["Final"] = 50.0
    assert gpa.course_grade(c) == 74.0
    c["grade"] = 80.0
    assert gpa.course_grade(c) == 80.0, "a grade typed by hand wins"


def test_gpa_parsing_and_need():
    from uninotifier import gpa

    assert gpa.parse_number(" 85 % ") == 85 and gpa.parse_number("62.5") == 62.5
    for bad in ("abc", "101", "-1", ""):
        try:
            gpa.parse_number(bad)
        except gpa.InputError:
            continue
        raise AssertionError(bad)
    assert gpa.parse_cgpa("72.5 33") == (72.5, 33.0) and gpa.parse_cgpa("72.5, 33") == (72.5, 33.0)
    for bad in ("72", "101 30", "70 400"):
        try:
            gpa.parse_cgpa(bad)
        except gpa.InputError:
            continue
        raise AssertionError(bad)

    syl = gpa.parse_syllabus("Midterm 30, Assignments 10, Project: 20, Final Exam 40")
    assert syl == [["Midterm", 30.0], ["Assignments", 10.0], ["Project", 20.0], ["Final Exam", 40.0]]
    for bad, why in (("Midterm 30, Final 50", "add up"), ("Midterm 60, Quiz 40", "Final"),
                     ("Midterm 30, Midterm 30, Final 40", "twice"), ("Final 100", "at least two"),
                     ("Midterm, Final 40", "Couldn't read")):
        try:
            gpa.parse_syllabus(bad)
        except gpa.InputError as e:
            assert why in str(e), (bad, str(e))
            continue
        raise AssertionError(bad)

    marks = gpa.parse_marks("mid 24, Assignments 8/10, proj 15", syl)
    assert marks == {"Midterm": 24.0, "Assignments": 8.0, "Project": 15.0}, marks
    assert gpa.parse_marks("Midterm 12/15", syl) == {"Midterm": 24.0}, "scaled to the part's weight"
    for bad in ("Midterm 31", "Quiz 5", "", "Midterm"):
        try:
            gpa.parse_marks(bad, syl)
        except gpa.InputError:
            continue
        raise AssertionError(bad)

    course = {**gpa.new_course(), "syllabus": syl, "marks": marks}      # 47 of 60 before the final
    need = gpa.need_for(course, 60)
    assert need["earned"] == 47 and need["required"] == 13 and need["weight"] == 40 and need["missing"] == []
    assert gpa.need_for(course, 90)["required"] == 43, "above the final's weight: out of reach"
    assert gpa.need_for(gpa.new_course(), 60) is None
    partial = {**course, "marks": {"Midterm": 24.0}}
    assert gpa.need_for(partial, 60)["missing"] == ["Assignments", "Project"]


def test_gpa_screens_and_flow():
    bot = _package("uninotifier.bot")
    from uninotifier import gpa
    from uninotifier import gpa_ui

    names = ["Arabic Communication Skills", "Big Data", "Database Systems"]
    with tempfile.TemporaryDirectory() as tmp:
        gpa.STATE_FILE = Path(tmp) / "gpa.json"

        text, rows = gpa_ui.view_home(names)
        assert "Course Adjustments" in text and "Big Data" in text and "set your current CGPA" in text
        assert [r[0]["callback_data"] for r in rows[:3]] == ["gpa:c:0", "gpa:c:1", "gpa:c:2"]
        assert all(len(b["callback_data"].encode()) <= 64 for r in rows for b in r)
        assert gpa.load()["courses"]["Big Data"]["hours"] == 3, "3 credit hours by default"

        # quick toggles: hours
        text, rows = gpa_ui.on_callback(names, "gpa:c:0")
        assert "Credit hours:* 3" in text
        text, rows = gpa_ui.on_callback(names, "gpa:h:0:2")
        assert "Credit hours:* 2" in text and any(b["text"] == "✅ 2h" for b in rows[0])

        # grade entry through a question
        prompt = gpa_ui.on_callback(names, "gpa:g:0")
        assert isinstance(prompt, gpa_ui.Prompt) and prompt.kind == "grade"
        err = gpa_ui.on_text(names, prompt, "150")
        assert isinstance(err, str) and "between 0 and 100" in err
        text, rows = gpa_ui.on_text(names, prompt, "85")
        assert "Final grade:* 85" in text
        gpa_ui.on_text(names, gpa_ui.on_callback(names, "gpa:g:1"), "40")
        text, _ = gpa_ui.view_home(names)
        assert "✅ 85" in text and "❌ 40" in text and "Below the 50% pass mark:* Big Data" in text
        assert "Semester Average:* 58%" in text and "60%" not in text   # (85*2 + 40*3) / 5

        # retake: asks for the old grade first; turning it off clears it
        prompt = gpa_ui.on_callback(names, "gpa:r:1")
        assert prompt.kind == "old_grade"
        assert not gpa.load()["courses"]["Database Systems"]["retake"], "not a retake until the old grade is given"
        gpa_ui.on_text(names, prompt, "45")
        c = gpa.load()["courses"]["Big Data"]
        assert c["retake"] and c["old_grade"] == 45
        assert "retake, was 45" in gpa_ui.view_home(names)[0]
        assert "previous attempt was a fail" in gpa_ui.view_course(names, 1)[0]
        gpa_ui.on_callback(names, "gpa:r:1")
        assert gpa.load()["courses"]["Big Data"]["retake"] is False

        # CGPA and passed hours -> projected CGPA
        prompt = gpa_ui.on_callback(names, "gpa:cg")
        gpa_ui.on_text(names, prompt, "70 30")
        text, _ = gpa_ui.view_home(names)
        assert "Projected CGPA:* 68.29%" in text, text           # (70*30 + 85*2 + 40*3) / 35

        # syllabus -> marks -> /need
        prompt = gpa_ui.on_callback(names, "gpa:m:0")
        assert "Set the syllabus" in prompt[0], "marks need a syllabus first"
        prompt = gpa_ui.on_callback(names, "gpa:s:0")
        assert prompt.kind == "syllabus"
        err = gpa_ui.on_text(names, prompt, "Midterm 30, Final 50")
        assert isinstance(err, str) and "add up to 80" in err
        gpa_ui.on_text(names, prompt, "Midterm 30, Assignments 30, Final 40")
        prompt = gpa_ui.on_callback(names, "gpa:m:0")
        assert prompt.kind == "marks"
        text, _ = gpa_ui.on_text(names, prompt, "Midterm 24, Assignments 17")
        assert "Midterm 24/30" in text and "Assignments 17/30" in text and "Final 40" in text
        assert "Coursework:* 41 / 60" in text and "Best possible:* 81" in text and "To pass (50):* 9 / 40 on the final" in text

        # the coursework total: one figure before the final, typed as marks/out of — the simple way in
        text, rows = gpa_ui.on_callback(names, "gpa:c:2")
        assert "Coursework:* not entered" in text
        assert [b["text"] for b in rows[2]] == ["📝 Coursework Total"] and [b["text"] for b in rows[3]] == ["✏️ Final Grade"]
        prompt = gpa_ui.on_callback(names, "gpa:w:2")
        assert prompt.kind == "coursework"
        for bad in ("42", "70/60", "42/100", "nothing"):
            assert isinstance(gpa_ui.on_text(names, prompt, bad), str), bad
        text, rows = gpa_ui.on_text(names, prompt, "٣٠ من ٥٠")
        assert "Coursework:* 30 / 50" in text and "Best possible:* 80" in text and "To pass (50):* 20 / 50 on the final" in text
        assert [b["text"] for b in rows[2]] == ["📝 Coursework Total", "🧹 Clear"]
        assert "already there" in gpa_ui.on_text(names, prompt, "55/60")[0]
        assert "out of reach" in gpa_ui.on_text(names, prompt, "5/60")[0]
        text, rows = gpa_ui.on_callback(names, "gpa:wx:2")
        assert "Coursework:* not entered" in text and gpa.load()["courses"][names[2]]["syllabus"] is None

        # Grade Needed: scope -> target -> plan; graded courses are locked, the rest share the target
        text, rows = gpa_ui.on_callback(names, "gpa:need")
        assert "What are you aiming for?" in text and "/need" not in text
        assert [r[0]["callback_data"] for r in rows[:3]] == ["gpa:t:s", "gpa:t:c", "gpa:home"]
        text, rows = gpa_ui.on_callback(names, "gpa:t:c")
        assert [b["text"] for r in rows[:3] for b in r] == ["50", "60", "70", "75", "80", "85",
                                                            "90", "✏️ Custom Value"]
        assert rows[0][1]["callback_data"] == "gpa:n:c:60"
        gpa_ui.on_callback(names, "gpa:x:0")                     # Arabic: no grade any more, its syllabus marks stay
        text, rows = gpa_ui.on_callback(names, "gpa:n:s:60")
        # open: Arabic (2h, marks 41/60) and Database Systems (3h); locked: Big Data 40 (3h)
        # semester: (60*8 - 40*3) / 5 = 72 on each open course
        assert "🔒 *Big Data* · 3h · 40 (entered)" in text and "Needed on each remaining course:* 72%" in text, text
        assert "Final needed: *31 / 40*" in text, "Arabic: 72 - 41 earned before the final"
        assert _legacy_markdown_ok(text)
        assert "Out of reach" in gpa_ui.view_plan(names, "s", 95)[0]
        assert "Already secured" in gpa_ui.view_plan(names, "s", 10)[0]
        # cumulative (70% over 30 h): (65*38 - 70*30 - 40*3) / 5 = 50
        text, rows = gpa_ui.view_plan(names, "c", 65)
        assert "Needed on each remaining course:* 50%" in text and "CGPA at that pace:* 65%" in text, text
        assert rows[0][1]["callback_data"] == "gpa:n:s:65", "the same target as a semester one"
        assert gpa_ui.on_callback(names, "gpa:n:60")[0].startswith("📊 *Target Semester GPA: 60%*"), "old buttons"
        # a grade published on the portal is locked and wins over a typed one
        assert gpa.record_portal_grades({"Database Systems": 90, "Not A Course": 50}) == ["Database Systems"]
        text, _ = gpa_ui.view_plan(names, "s", 60)
        assert "🔒 *Database Systems* · 3h · 90 (portal)" in text and "Needed on each remaining course:* 45%" in text and "(and 50 to pass)" in text
        assert gpa.record_portal_grades({"Database Systems": 90}) == [], "unchanged: nothing to save"
        state = gpa.load()
        state["courses"]["Database Systems"]["portal_grade"] = None
        gpa.save(state)

        # the bot: commands, typed answers, cancel
        sent = []
        orig = (bot.send_view, bot.edit_view, bot.course_names)
        bot.send_view = lambda text, rows: sent.append((text, rows))
        bot.edit_view = lambda mid, text, rows: sent.append((f"edit:{mid}", text, rows))
        bot.course_names = lambda: names
        try:
            bot.handle_command("/gpa")
            assert "GPA & Grade Hub" in sent[-1][0]
            bot.handle_command("/need")
            assert "Grade Needed" in sent[-1][0] and "What are you aiming for?" in sent[-1][0]
            bot.handle_command("/need 60")
            assert "What are you aiming for?" in sent[-1][0], "no syntax: always the buttons"

            bot.handle_gpa_callback("gpa:v:c", 7)             # ✏️ Custom Value asks for the number
            assert "Send the target" in sent[-1][0] and bot._pending_prompt.kind == "target_c"
            bot.handle_command("77.5")
            assert sent[-1][0].startswith("🎓 *Target Cumulative CGPA: 77.5%*") and bot._pending_prompt is None

            bot.handle_gpa_callback("gpa:g:2", 7)             # asks, does not edit
            assert "Send the course's mark" in sent[-1][0] and sent[-1][1] == [gpa_ui.CANCEL_ROW]
            assert bot._pending_prompt is not None
            bot.handle_command("not a number")                # an unusable answer keeps the question open
            assert "Send just a number" in sent[-1][0] and bot._pending_prompt is not None
            bot.handle_command("92")
            assert bot._pending_prompt is None and "Final grade:* 92" in sent[-1][0]

            bot.handle_gpa_callback("gpa:c:2", 7)             # a tapped button edits its message
            assert sent[-1][0] == "edit:7"
            bot.handle_gpa_callback("gpa:g:2", 7)
            bot.handle_gpa_callback("gpa:cancel", 7)
            assert bot._pending_prompt is None and sent[-1][0] == "edit:7" and "Course Adjustments" in sent[-1][1]
            bot.handle_gpa_callback("gpa:g:2", 7)
            bot.handle_command("/gpa")                        # any command drops the open question
            assert bot._pending_prompt is None

            # passed hours / CGPA taken from the transcript button arrive as a new message
            bot.handle_gpa_callback("gpa:use:33:72.5", 9)
            assert not sent[-1][0].startswith("edit:") and "72.5%" in sent[-1][0] and "33 record hours" in sent[-1][0]
        finally:
            bot.send_view, bot.edit_view, bot.course_names = orig
            bot._pending_prompt = None
        assert gpa.load()["cgpa"] == 72.5 and gpa.load()["passed_hours"] == 33

        # the GPA & Grade Hub: Grade Needed and Finals Watch only (Course Adjustments lives inside Grade Needed,
        # this term's grades inside Finals Watch)
        buttons = [b["callback_data"] for row in bot.view_academic()[1] for b in row if "callback_data" in b]
        assert buttons == ["gpa:need", "finals:status", "menu"], buttons
        from uninotifier import gpa_ui as _gpa_ui
        need_buttons = [b["callback_data"] for row in _gpa_ui.view_need_picker()[1] for b in row if "callback_data" in b]
        assert "gpa:home" in need_buttons, "Course Adjustments sits inside Grade Needed"
        hub_text = bot.view_academic()[0]
        assert "This Term" not in hub_text and "Course Adjustments" not in hub_text.split("Grade Needed")[0]
        record = [b["callback_data"] for row in bot.view_record()[1] for b in row if "callback_data" in b]
        assert record == ["portal:transcript", "portal:balance", "reg:home", "guest:start", "menu"], "no combined button"
        record_text = bot.view_record()[0]
        assert "verification code" not in record_text and "re-syncs" not in record_text
        assert {"gpa", "need", "grades", "transcript", "balance"} <= {c for c, _ in bot.COMMANDS}


def test_retake_replacement_in_plan():
    """A retaken course's old (recorded) grade leaves the weighted sum and its hours
    count once; retakes found on the portal are synced, hand-set ones left alone."""
    from uninotifier import gpa

    with tempfile.TemporaryDirectory() as tmp:
        keep = gpa.STATE_FILE
        gpa.STATE_FILE = Path(tmp) / "gpa.json"
        try:
            gpa.save({**gpa.load(), "courses": {"Calculus": gpa.new_course(), "Physics": gpa.new_course(),
                                                "Ethics": dict(gpa.new_course(), retake=True, old_grade=55.0)}})
            assert gpa.sync_retakes({"Calculus": 35.0, "Not here": 40.0}) == ["Calculus"]
            courses = gpa.load()["courses"]
            assert (courses["Calculus"]["retake"], courses["Calculus"]["old_grade"]) == (True, 35.0)
            assert courses["Ethics"]["retake"] and courses["Ethics"].get("retake_source") is None, "hand-set: untouched"
            # base: 70% over 30 h, which includes Calculus's 35 and Ethics's 55 (3 h each).
            # target CGPA 72: hours = 30 + 3 (Physics only) = 33; points = 72*33 = 2376
            # = 70*30 - 35*3 - 55*3 + 9x  ->  x = (2376 - 2100 + 105 + 165) / 9 = 60.67
            result = gpa.plan(courses, 72, "cgpa", 70.0, 30.0)
            assert abs(result["needed"] - 546 / 9) < 1e-9 and abs(result["cgpa"] - 72) < 1e-9, result
            # once Calculus's new mark is published it replaces the 35
            gpa.record_portal_grades({"Calculus": 80.0})
            result = gpa.plan(gpa.load()["courses"], 72, "cgpa", 70.0, 30.0)
            assert abs(result["needed"] - (2376 - 2100 + 105 + 165 - 240) / 6) < 1e-9, result
            assert gpa.sync_retakes({}) == ["Calculus"] and not gpa.load()["courses"]["Calculus"]["retake"], \
                "no longer listed on the portal: cleared"
        finally:
            gpa.STATE_FILE = keep
