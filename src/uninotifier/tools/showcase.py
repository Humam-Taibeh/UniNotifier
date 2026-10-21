"""
Sends a sample of every notification the bot can send to the Telegram chat,
so the layout and buttons can be reviewed on a phone — a reference gallery.

Every message is built by the same code the monitor, the bot, Finals Watch and
the Microsoft sign-in relay use (media, notify.deliver, lectures, finals,
mfa), fed with sample data. Nothing touches the portal or the university
archive: the sample files live in a temp folder. Buttons that act (Mark as
Done, Text me a code, Use as my CGPA) are real buttons; tapping them in the
gallery acts on nothing that exists.

Run:  .venv\\Scripts\\python -m uninotifier.tools.showcase
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from uninotifier import archive
from uninotifier import daily
from uninotifier import finals
from uninotifier import gradebook
from uninotifier import grades
from uninotifier import lectures
from uninotifier import maintenance
from uninotifier import media
from uninotifier import mfa
from uninotifier.monitor import notify
from uninotifier import reminders
from uninotifier import notifier
from uninotifier import schedule
from uninotifier.zuj_auth import BASE_URL
from uninotifier import paths

load_dotenv(paths.ENV_FILE)
TOKEN, CHAT = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
STUDENT_ID = os.getenv("STUDENT_ID") or os.getenv("PORTAL_USERNAME") or ""   # for finding your marks in files

PORTAL = f"{BASE_URL}/my/"
COURSE = "20261-Big Data-AllSec-3-"
HOUR = 3600


def item(kind: str, name: str, section: str = "Week 5", **extra) -> dict:
    return {"name": name, "type": kind, "course": COURSE, "section": section, "url": PORTAL,
            "cmid": "0", **extra}


def deadline(kind: str, name: str, in_seconds: float) -> dict:
    return {"name": name, "type": kind, "course": COURSE, "url": PORTAL,
            "timestamp": time.time() + in_seconds}


def saved_to(*parts: str) -> str:
    """The "📂 Saved to" line exactly as archive.destination words it."""
    return f"📂 *Saved to:* {archive.destination(archive.ARCHIVE_ROOT.joinpath(*parts, 'x.pdf'))}"


def sample_pdf(path: Path, line: str) -> Path:
    """A real one-page PDF, so the sample attachment opens on the phone."""
    text = line.replace("\\", "").replace("(", "").replace(")", "")
    stream = f"BT /F1 20 Tf 60 760 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def make_marks_sheet(folder: Path) -> Path:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["No.", "Student ID", "Name", "Quiz (10)", "Mid (30)", "Total /40"])
    ws.append([1, 202300001, "Sample Student", 5, 20, 25])
    ws.append([2, int(STUDENT_ID), "You", 9, 27, 36])
    path = folder / "Midterm_Marks.xlsx"
    wb.save(path)
    return path


def sample_record() -> list[dict]:
    """A transcript, newest semester first: this semester complete, one retake."""
    def sem(term, courses):
        return {"term": term, "courses": [{"code": c, "name": n, "hours": h, "mark": m} for c, n, h, m in courses]}
    return [
        sem("First semester 2026/2027", [("0135321", "Big Data", 3, 88), ("0135220", "Database Systems", 3, 81),
                                          ("0101221", "Linear Algebra (1)", 3, 74), ("0101272", "Numerical Analysis (1)", 3, 69)]),
        sem("Second semester 2025/2026", [("0101221", "Linear Algebra (1)", 3, 48), ("0112110", "Discrete Mathematics", 3, 77)]),
        sem("First semester 2025/2026", [("0111101", "Programming (1)", 3, 72), ("0112111", "Calculus (1)", 3, 66)]),
    ]


def next_weekday(weekday: int, hhmm: str) -> datetime:
    now = datetime.now()
    day = now + timedelta(days=(weekday - now.weekday()) % 7 or 7)
    h, m = map(int, hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if not (TOKEN and CHAT):
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env")

    results = []

    def send(label: str, ok: bool) -> None:
        results.append((label, ok))
        print(("✅ " if ok else "❌ ") + label)
        time.sleep(1.2)  # keep the messages in order and under rate limits

    def msg(text: str, url: str | None = PORTAL, rows: list | None = None) -> bool:
        return notifier.send_message(TOKEN, CHAT, text, url, rows)

    def chapter(title: str) -> None:
        send(f"— {title}", notifier.send_message(TOKEN, CHAT, f"📁 *{title}*"))

    send("intro", notifier.send_message(
        TOKEN, CHAT, "🖼️ *Notification Gallery*\n\n"
                     "One sample of every alert the bot sends, grouped by topic.\n"
                     "_All data below is sample data — nothing here happened on the portal._"))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # ------------------------------------------------ assignments & deadlines
        chapter("1 · Assignments & Deadlines")
        send("new assignment", notify.deliver(
            media.build_item_message(item("assign", "Assignment 2 - MapReduce"),
                                     deadline("assign", "Assignment 2 - MapReduce", 3 * 86400 + 5 * HOUR),
                                     status=media.STATUS_PENDING),
            "Implement word count with MapReduce and submit the .py file and a one-page report.", PORTAL))
        send("assignment (submitted)", notify.deliver(
            media.build_item_message(item("assign", "Assignment 1 - Intro"),
                                     {**deadline("assign", "Assignment 1 - Intro", 2 * 86400), "submitted": True}),
            url=PORTAL))
        send("new quiz / exam", notify.deliver(
            media.build_item_message(item("quiz", "Quiz 1 - Hadoop"), deadline("quiz", "Quiz 1 - Hadoop", 4 * 86400)),
            url=PORTAL))
        send("assignment 24h", msg(media.build_reminder_message(
            deadline("assign", "Assignment 2 - MapReduce", 23.5 * HOUR), "24h")))
        send("assignment 1h", msg(media.build_reminder_message(
            deadline("assign", "Assignment 2 - MapReduce", 0.9 * HOUR), "1h")))
        send("exam 24h", msg(media.build_reminder_message(deadline("quiz", "Quiz 1 - Hadoop", 23.5 * HOUR), "24h")))
        send("exam 1h", msg(media.build_reminder_message(deadline("quiz", "Quiz 1 - Hadoop", 0.9 * HOUR), "1h")))

        # ------------------------------------------------ course information
        chapter("2 · Course Information")
        send("new course", notify.notify_new_course({"name": COURSE, "url": PORTAL}))
        info = item("label", "Course Information", "Course Home", manual=True, done=False)
        send("new course text", notify.deliver(
            media.build_item_message(info, None),
            "Instructor: Dr. Hani Omar\nOffice hours: Sunday & Tuesday 10:00–11:00\n"
            "Grading: Midterm 30%, Assignments 20%, Final 50%", PORTAL, media.completion_rows(info)))
        send("syllabus edited", notify.deliver(
            media.build_content_update_message(info),
            "Instructor: Dr. Hani Omar\nOffice hours: *Monday* & Tuesday 10:00–11:00\n"
            "Grading: Midterm 30%, Assignments 20%, Final 50%\nMidterm date: Sunday 2026-11-22",
            PORTAL, media.completion_rows(info)))

        # ------------------------------------------------ forum
        chapter("3 · Announcements & Forum")
        post = {"id": "1", "subject": "Midterm moved to Sunday", "author": "Dr. Hani Omar",
                "time": time.time() - 600, "is_reply": False, "attachments": [], "url": PORTAL,
                "html": "<p>Dear students,</p><p>The midterm exam is moved to <b>Sunday 22 November</b>, "
                        "same time and room.</p>"}
        forum = {"name": "Announcements", "course": COURSE, "type": "forum"}
        send("instructor announcement", notify.notify_forum_post(forum, post))
        send("forum topic", notify.notify_forum_post(
            {**forum, "name": "Q&A"}, {**post, "id": "2", "subject": "Question about Lab 3",
                                       "author": "A classmate", "html": "<p>Is the lab due on Moodle?</p>"}))
        send("forum reply", notify.notify_forum_post(
            {**forum, "name": "Q&A"}, {**post, "id": "3", "is_reply": True, "subject": "Re: Question about Lab 3",
                                       "html": "<p>Yes — upload it before Thursday.</p>"}))

        # ------------------------------------------------ files
        chapter("4 · Files & Links")
        lecture = sample_pdf(tmp / "Lecture 05 - Spark Basics.pdf", "Lecture 05 - Spark Basics (sample)")
        lec_item = item("resource", "Lecture 05 - Spark Basics", manual=True, done=True)
        send("new file (lectures)", notify.deliver(
            media.build_item_message(lec_item, None, saved_to("Big Data", archive.LECTURES)),
            url=PORTAL, rows=media.completion_rows(lec_item), document=lecture))
        syllabus = sample_pdf(tmp / "Course Syllabus.pdf", "Big Data - Course Syllabus (sample)")
        syl_item = item("resource", "Course Syllabus", "Course Home", manual=True, done=True)
        send("new file (syllabus)", notify.deliver(
            media.build_item_message(syl_item, None, saved_to("Big Data", archive.LECTURES)),
            url=PORTAL, rows=media.completion_rows(syl_item), document=syllabus))
        send("same file, second item", notify.notify_new_item(
            item("resource", "Lecture 05 - Spark Basics (Week 6 copy)", "Week 6"), None,
            {"kind": "file", "path": str(lecture), "size": lecture.stat().st_size, "duplicate": True}))
        updated = sample_pdf(tmp / f"Lecture 05 - Spark Basics (updated {datetime.now():%Y-%m-%d}).pdf",
                             "Lecture 05 - Spark Basics v2 (sample)")
        send("file updated", notify.deliver(
            media.build_update_message(lec_item, time.time() - 3 * 86400 - 4 * HOUR, time.time() - 120,
                                       old_name=lecture.name, new_name=updated.name,
                                       where=archive.destination(
                                           archive.ARCHIVE_ROOT / "Big Data" / archive.LECTURES / updated.name)),
            url=PORTAL, document=updated))
        vid = item("url", "Lecture 06 Recording", manual=True, done=True)
        send("video link", msg(media.build_link_message(vid, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
                               rows=media.completion_rows(vid)))
        lecture = item("teamscheduler", "رابط الدخول إلى المحاضرة", "روابط هامة ورئيسية")
        meeting = {"url": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_sample%40thread.v2/0",
                   "details": [("Meeting type", "Recurring meeting"), ("Repeat on", "Monday, Wednesday"),
                               ("Meeting time", "7:00 PM – 8:30 PM"), ("Organizer", "Dr. Sample")]}
        send("teams lecture", msg(media.build_meeting_message(lecture, meeting), url=PORTAL,
                                  rows=media.meeting_rows(lecture, meeting)))
        check = item("attendance", "الحضور", "روابط هامة ورئيسية")
        start = datetime.now().replace(hour=19, minute=0) + timedelta(days=1)
        sessions = [{"date": f"{start:%a %d %b %Y}", "time": "7PM - 8:30PM", "start": start,
                     "description": "Regular class session", "status": "?", "points": "? / 2", "submit_url": None}]
        send("attendance check-in", notify.deliver(media.build_attendance_message(check, sessions),
                                                    "الرجاء تأكيد الحضور بالدخول إلى هذا الرابط .", PORTAL,
                                                    media.attendance_rows(check)))

        # ------------------------------------------------ grades
        chapter("5 · Grades & Finals")
        sheet = make_marks_sheet(tmp)
        send("marks found in a file", notify.deliver(
            media.build_item_message(item("resource", "Midterm Marks", "Results"), None,
                                     saved_to("Big Data", archive.EXAMS), grade=grades.extract_grade(sheet, STUDENT_ID)),
            url=PORTAL, document=sheet))
        record = sample_record()
        partial = gradebook.season_report([{**record[0], "courses": [
            {**c, "mark": None} for c in record[0]["courses"]]}] + record[1:], {})
        send("finals watch started", msg(finals.started_message(partial), url=None))
        report = gradebook.season_report(record, {})
        send("grade released", msg(finals._released(report["courses"][0], {**report, "published": 1}, None), url=None))
        send("grade updated", msg(finals._released({**report["courses"][3], "mark": 71}, report, 69.0), url=None))
        cgpa, hours = report["after"]
        send("finals watch paused", msg(finals.paused_message(
            {"total": 6, "term": "First semester 2026/2027"})["text"], url=None,
            rows=finals.paused_message({"total": 6, "term": ""})["rows"]))
        send("semester results + CGPA", msg(finals.results_message(report, 69.9), url=None, rows=[
            [{"text": "✅ Use as my CGPA in /gpa", "callback_data": f"gpa:use:{finals.fmt(hours)}:{finals.fmt(cgpa)}"}],
            [{"text": "🧮 Open GPA Calculator", "callback_data": "gpa:home"}]]))

    # ---------------------------------------------------- lectures
    chapter("6 · Online Lectures")
    sch = schedule.load_schedule()
    online = [(d, lec) for d, lst in sch.get("days", {}).items() for lec in lst
              if lec["mode"] in sch.get("remind_modes", ["Online"])]
    if online:
        day, lec = online[0]
        weekday = schedule.DAY_KEYS.index(day)
        start = next_weekday(weekday, lec["start"])
        pre = [t for _, kind, t, _ in schedule.due_messages(sch, start - timedelta(minutes=50)) if kind == "pre"]
        begin = [(t, c) for _, kind, t, c in schedule.due_messages(sch, start) if kind == "start"]
        if pre:
            send("lecture in 1 hour", notifier.send_message(TOKEN, CHAT, pre[0]))
        if begin:
            send("lecture starting", lectures.send_with_link(TOKEN, CHAT, *begin[0]))
        send("lecture link saved", notify.notify_lecture_link(
            {"course": lec["course"], "origin": "Announcements — \"Online lecture link\""}))

    # ---------------------------------------------------- sign-in & health
    chapter("7 · Microsoft Sign-in & Health")
    send("2FA number", msg(mfa.approval_text("47", "5 min"), url=None, rows=mfa.SMS_ROWS))
    send("2FA SMS code", msg(mfa.code_text("We texted your phone +XXX XXXXXXX42.", "4 min"), url=None))
    send("2FA code accepted", msg("🔑 *Code received — verifying…*", url=None))
    send("session needs renewal", msg(
        "⚠️ *Session needs renewal*\n\nMicrosoft is asking for verification (Authenticator / SMS). Send /check and "
        "I'll relay it here — the Authenticator number, or a request for the SMS code.", url=None))
    shot = paths.SCHEDULE_IMAGE   # any picture stands in for the page
    down = maintenance.PortalUnavailable("e-learning portal", "the session was refused", "قيد الصيانة",
                                         shot.read_bytes() if shot.is_file() else None)
    send("portal under maintenance", notifier.send_png_file(
        TOKEN, CHAT, down.png, maintenance.SNAPSHOT.name,
        maintenance.alert_text(down, "Checks carry on every 15 minutes — I'll tell you when it's back."))
        if down.png else msg(maintenance.alert_text(down), url=None))
    send("portal unreachable", msg(
        "📡 *Cannot reach the portal*\n\nThe university site or the internet connection "
        "is down. Checks continue automatically.", url=None))

    # ------------------------------------------------------------ reminders
    chapter("8 · Reminders")
    sample = {"id": 0, "note": "Buy the textbook at the Faculty of Arts, floor 1", "due": time.time(),
              "course": "Big Data", "url": PORTAL}
    text, rows = reminders.message(sample)
    send("personal reminder", msg(text, url=None, rows=rows))
    send("📌 under an announcement", msg("📢 *New Announcement* — every announcement and course page "
                                        "carries this button.", url=None, rows=[reminders.pin_row("sample")]))

    # ------------------------------------------------------------ the day
    chapter("9 · Day Separator & Daily Recap")
    today = datetime.now().date()
    send("day separator", notifier.send_message(TOKEN, CHAT, daily.separator_text(today), silent=True))
    send("quiet day (11:59 PM)", msg(daily.recap_text(today, today), url=None))
    send("quiet day (caught up next morning)", msg(daily.recap_text(today - timedelta(days=1), today), url=None))

    ok = sum(ok for _, ok in results)
    send("outro", notifier.send_message(
        TOKEN, CHAT, f"✅ *Gallery complete* — {ok}/{len(results)} messages sent.\n\n"
                     f"_Everything after this point is live data from the portal._"))

    if not all(ok for _, ok in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
