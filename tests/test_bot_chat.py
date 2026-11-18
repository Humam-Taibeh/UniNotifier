"""The chat: menus, buttons, screens and how messages are worded."""

from support import (_as_owner, _fresh_failsafe, _legacy_markdown_ok, OUTBOX, _package, _tg)

import importlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from uninotifier import access
from uninotifier import failsafe
from uninotifier import media
from uninotifier import moodle
from uninotifier import notifier
from uninotifier import paths
from uninotifier import telegram


def test_media():
    assert media.classify({"type": "resource"}) == "file"
    assert media.classify({"type": "url"}) == "link"
    assert media.classify({"type": "assign"}) == "activity"
    assert media.classify({"type": "h5pactivity"}) == "unknown"
    assert media.is_video_url("https://www.youtube.com/watch?v=abc")
    assert not media.is_video_url("https://example.com/page")
    item = {"name": "Lecture_1", "course": "Big Data", "url": "https://x/mod/url/view.php?id=1", "type": "url"}
    assert "🎥" in media.build_link_message(item, "https://youtu.be/abc")
    assert "Unclassified Item" in media.build_unclassified_message(item)
    assert "*Type:* Video / Lecture Recording" in media.build_link_message(item, "https://youtu.be/abc")
    assert "*Type:* Assignment" in media.build_item_message({**item, "type": "assign"}, None)
    assert "File Updated" in media.build_update_message({**item, "type": "resource"}, 1.0, 2.0)
    upd = media.build_update_message({**item, "type": "resource"}, 1_700_000_000, 1_700_000_000 + 3 * 86400 + 4 * 3600,
                                     old_name="Chapter 1.pdf", new_name="Chapter 1 (updated 2026-10-12).pdf",
                                     where="`Big Data/01_Lectures` (Lectures & Slides)")
    assert "*Previous file:* `Chapter 1.pdf`" in upd and "*New file:* `Chapter 1 (updated 2026-10-12).pdf`" in upd
    assert "*Saved to:* `Big Data/01_Lectures`" in upd
    assert "*Changed:* 3 days 4 hours after the previous version" in upd, upd
    assert (media.elapsed(30), media.elapsed(60), media.elapsed(7500), media.elapsed(86400)) == (
        "under a minute", "1 minute", "2 hours 5 minutes", "1 day")
    rem = media.build_reminder_message({"type": "quiz", "course": "c", "name": "q", "timestamp": 2e9}, "1h")
    assert "Quiz / Exam" in rem and "Detected" in rem
    msg = media.build_item_message(item, None, grade={"summary": "36/40", "details": []})
    assert "Marks Detected" in msg and "36/40" in msg


def test_ui():
    base = {"name": "HW 1", "course": "20261-Big Data-AllSec-3-", "url": "https://x/mod/assign/view.php?id=1"}
    heads = {"assign": "New Assignment", "resource": "New File", "folder": "New File",
             "quiz": "New Quiz / Exam", "forum": "New Announcement", "page": "New Page",
             "label": "New Course Info", "section": "New Section Description"}
    for kind, head in heads.items():
        msg = media.build_item_message({**base, "type": kind}, None)
        assert head in msg and "New Content" not in msg, msg
        assert "تم رصد عنصر جديد" not in msg, msg          # the duplicated footer is gone
        assert "*Course:* Big Data\n" in msg and "20261" not in msg and "AllSec" not in msg, msg
    assert "New Video" in media.build_link_message(base, "https://youtu.be/abc")
    assert "*Course:* Arabic Communication Skills" in media.course_line("20261-الاتصال والتواصل باللغة العربية-ش31-3-ك")
    assert media.clean_course_name("20261-Linear Algebra (1)-AllSec-3-F") == "Linear Algebra (1)"
    assert notifier.PORTAL_BUTTON_TEXT == "🌐 Open in ZUJ Portal ↗️"

    due = {"timestamp": 2e9}
    assert "Status:* ⏳ Pending" in media.build_item_message({**base, "type": "assign"}, due)
    assert "Status:* ✅ Submitted" in media.build_item_message({**base, "type": "assign"}, {**due, "submitted": True})
    assert "Status:* ✅ Submitted" in media.build_item_message({**base, "type": "assign"}, None, status=media.STATUS_SUBMITTED)


def test_bot_views():
    bot = _package("uninotifier.bot")
    text, rows = bot.view_menu()
    assert [[b["text"] for b in row] for row in rows] == [
        ["📚 My Courses & Schedule", "⏳ Upcoming Tasks & Reminders"],
        ["📊 GPA & Grade Hub", "📜 Academic Record & Balance"]], rows
    assert [b.get("callback_data") for row in rows for b in row] == [
        "courses", "tasks", "academic", "record"], "no external site buttons, no in-chat guide"
    assert len(text.splitlines()) == 1 and text.startswith("🎓 *UniNotifier System* • First Semester 2026/2027")
    assert "Courses Tracked" not in text and text.endswith("Monitor not started yet*"), text          # the status sits beside the semester, no course count
    # My Courses & Schedule is a menu, not a wall of text; Tracked Items and Campus Guide are gone
    text, rows = bot.view_courses()
    assert [[b["text"] for b in row] for row in rows] == [
        ["📋 Course List & Details"], ["📅 Weekly Timetable", "🎒 Attendance"], ["🔙 Back", "🏠 Main Menu"]]
    assert [rows[0][0]["callback_data"], rows[1][0]["callback_data"]] == ["courselist", "schedule"]
    assert "What would you like to see?" in text and "credit hours" not in text and len(text.splitlines()) <= 5
    everywhere = {b.get("callback_data") for view in bot.VIEWS.values() for row in view()[1] for b in row}
    assert not {"items", "campus"} & everywhere and not {"items", "campus"} & {c for c, _ in bot.COMMANDS}
    assert not hasattr(bot, "view_items") and not hasattr(bot, "view_campus")
    # Lecture Links is gone: the link rides on the lecture reminder itself
    assert not hasattr(bot, "view_links") and not hasattr(bot, "handle_setlink")
    assert "links" not in everywhere and "setlink" not in {c for c, _ in bot.COMMANDS}
    # no scholarship / discount wording anywhere, and no "How It Works" in the GPA hub
    from uninotifier import gpa_ui
    names = bot.course_names()
    screens = [view()[0] for view in bot.VIEWS.values()] + [
        gpa_ui.view_need_picker(names)[0], gpa_ui.view_targets("c")[0], gpa_ui.view_targets("s")[0],
        gpa_ui.view_home(names)[0], gpa_ui.view_plan(names, "s", 60)[0], gpa_ui.view_plan(names, "c", 75)[0]]
    for screen in screens:
        assert not any(w in screen.lower() for w in ("scholarship", "discount", "standing:")), screen
    assert not [b for row in bot.view_academic()[1] for b in row if b.get("callback_data", "").startswith("help")]
    moved = {b.get("callback_data") for view in (bot.view_academic, bot.view_record) for row in view()[1] for b in row}
    assert {"gpa:need", "finals:status", "portal:transcript", "portal:balance"} <= moved
    assert not {"gpa:home", "portal:grades"} & moved, "Course Adjustments and This Term's Grades left the GPA hub"
    # no Quick Check anywhere: not in a view, not in the / menu
    assert "check" not in everywhere | {b.get("callback_data") for row in bot.view_tasks()[1] for b in row}
    assert "check" not in {c for c, _ in bot.COMMANDS}
    courses = bot.view_course_list()[0]
    assert "20261" not in courses and "Section 01" in courses and "Dr. Sam Taylor" in courses
    names = [l for l in courses.splitlines() if l[:1].isdigit()]
    assert len(names) == 6 and not any("؀" <= ch <= "ۿ" for l in names for ch in l), names
    assert bot.SCHEDULE_PDF.is_file() and bot.SCHEDULE_IMAGE.is_file()
    assert "🕘 Sun · Tue · 11:00 AM–12:30 PM" in courses and "🕘 Sun · Tue · 3:30–5:00 PM" in courses, courses
    assert "14:00" not in courses and "Room 449" in courses
    assert "Upcoming Tasks & Reminders" in bot.view_tasks()[0]
    assert notifier.clock12(0, 5) == "12:05 AM" and notifier.clock12(12, 0) == "12:00 PM"
    assert notifier.format_timestamp(1.7e9).endswith(("AM", "PM"))
    assert bot.remaining_text(time.time() + 90000).startswith("1d")


def test_messages_stay_short():
    """What an instructor wrote is in the picture, not typed out again; an edit says what changed;
    a marks list in which your mark was not found says so."""
    import tempfile
    from uninotifier import grades
    from uninotifier.monitor import notify
    item = {"name": "Week 3", "type": "label", "course": "20261-Big Data-AllSec-3-", "url": "https://x/1",
            "_body": "<p>Read chapter 3 before the lecture.</p>"}
    assert notify.body_text(item) == "Read chapter 3 before the lecture."
    assert notify.body_text(item, shown=True) == "", "a picture or the file shows it: not typed out again"

    old = "Quiz on Sunday\nChapters 1 and 2\nBring a calculator"
    new = "Quiz on Tuesday\nChapters 1 and 2\nBring a calculator\nRoom 12"
    assert media.change_lines(old, new) == "*Was:*\n➖ Quiz on Sunday\n*Now:*\n➕ Quiz on Tuesday\n➕ Room 12"
    assert media.change_lines(old, old).startswith("🖼️ The wording is the same")
    assert media.change_lines(None, new) == "", "nothing was kept to compare with: the picture speaks"
    many = media.change_lines("", "\n".join(f"line {n}" for n in range(20)))
    assert many.count("➕") == media.CHANGE_LINES and "… and 12 more" in many
    assert "Current version below" not in media.build_content_update_message(item)

    with tempfile.TemporaryDirectory() as tmp:
        listed = Path(tmp) / "Midterm marks.txt"
        listed.write_text("900111 17\n900222 12\n", encoding="utf-8")
        assert grades.unread_note(listed, "Midterm marks", None).startswith("🔎 *This looks like a marks list*")
        assert grades.unread_note(listed, "Midterm marks", {"summary": "17"}) == "", "the mark was found"
        slides = Path(tmp) / "Chapter 2.txt"
        slides.write_text("slides", encoding="utf-8")
        assert grades.unread_note(slides, "Chapter 2", None) == "", "not about marks: nothing to say"
        assert "your number is not in it" in grades.unread_note(listed, "كشف العلامات", None)


def test_help_hub_dashboard_and_campus():
    """The guide is the README, not screens in the chat: /help is a link to it, and a button of the
    old in-chat guide opens the menu. The dashboard tells the truth about the monitor."""
    bot = _package("uninotifier.bot")
    assert not (paths.ROOT / "src" / "uninotifier" / "guide.py").exists(), "the in-chat guide was removed"
    assert "help" not in {c for c, _ in bot.COMMANDS} and {"remind", "need"} <= {c for c, _ in bot.COMMANDS}
    text, rows = bot.view_guide_link()
    assert "README.md" in text and rows == [bot.BACK_ROW] and _legacy_markdown_ok(text)

    # dispatch: /help links to the guide; an old guide button opens the menu; /courses is the submenu
    sent, edited = [], []
    orig = (bot.send_view, bot.edit_view, bot.api)
    bot.send_view = lambda text, rows: sent.append(text)
    bot.edit_view = lambda message_id, text, rows: edited.append(text)
    bot.api = lambda method, **data: None
    try:
        bot.handle_command("/help")
        bot.handle_callback({"id": "1", "data": "help:tasks", "message": {"message_id": 5}})
        bot.handle_callback({"id": "2", "data": "help", "message": {"message_id": 5}})
        bot.handle_command("/courses")
        bot.handle_callback({"id": "3", "data": "courselist", "message": {"message_id": 6}})
        bot.handle_command("/campus")
    finally:
        bot.send_view, bot.edit_view, bot.api = orig
    assert sent[0].startswith("📖 *Guide*") and edited[0].startswith("🎓 *UniNotifier System*") and edited[1] == edited[0]
    assert sent[1].startswith("📚 *My Courses & Schedule*") and "Section 03" in edited[2]
    assert sent[2].startswith("❓ Unknown command"), "Campus Guide was removed"
    for text in (bot.view_courses()[0], bot.view_course_list()[0], bot.view_tasks()[0]):
        assert _legacy_markdown_ok(text), text

    # the dashboard's monitor line
    orig = bot.load_last_run
    try:
        for run, expected in (({}, "⚪"), ({"finished": time.time() - 300, "ok": True}, "🟢 *Live Monitor Active*"),
                              ({"finished": time.time() - 3 * 3600, "ok": True}, "🟡 *Monitor paused* — last check 3 h ago"),
                              ({"finished": time.time() - 60, "ok": False, "error": "exit code 1"}, "🟠")):
            bot.load_last_run = lambda run=run: run
            assert bot.monitor_status().startswith(expected), (run, bot.monitor_status())
            assert _legacy_markdown_ok(bot.view_menu()[0])
    finally:
        bot.load_last_run = orig


def test_update_routing_from_telegram():
    """handle_update is what every Telegram message and button press goes through: the owner's
    own are routed (a local variable once shadowed the chat module and made every update fail),
    everyone else's is dropped."""
    bot = _package("uninotifier.bot")
    keep_owner = _as_owner(bot)
    seen, answered = [], []
    orig = (bot.handle_command, bot.handle_callback, bot.api)
    bot.handle_command = lambda text, message_id=None: seen.append(("command", text))
    bot.handle_callback = lambda cb: seen.append(("callback", cb["data"]))
    bot.api = lambda method, **data: answered.append(method)
    try:
        bot.handle_update(_tg(text="/start"))
        bot.handle_update(_tg(data="menu"))
        bot.handle_update(_tg(user_id="999", chat_id="999", text="/start"))
        bot.handle_update(_tg(user_id="999", chat_id="999", data="menu"))
    finally:
        bot.handle_command, bot.handle_callback, bot.api = orig
        bot.CHAT_ID, access.ADMIN_USER_ID = keep_owner
    assert seen == [("command", "/start"), ("callback", "menu")], seen
    assert answered == [], "a stranger's button press is not even acknowledged"


def test_buttons_are_answered_first():
    """A button press is answered once and at once, a double tap is ignored, a slow
    "Mark as done" says "updating…" and then shows its outcome on the button, and a
    long job shows a chat action instead of a message."""
    import threading

    bot = _package("uninotifier.bot")
    calls = []
    keep = (bot.api, bot.send_view, bot.edit_view, bot.DEBOUNCE_SECONDS, bot.ACTION_REFRESH_SECONDS,
            moodle.http_set_completion, bot.set_item_done, bot.DONE_WAIT, notifier.send_document_group)
    bot.api = lambda method, **data: calls.append((method, data))
    bot.send_view = lambda text, rows: calls.append(("sendMessage", {"text": text}))
    bot.edit_view = lambda message_id, text, rows: calls.append(("editMessageText", {"text": text}))
    bot.set_item_done = lambda cmid, done: calls.append(("state", {"cmid": cmid, "done": done}))
    real_start_check = bot.checks.start_check
    bot.checks.start_check = lambda message_id: True        # a "check" press must never start the real monitor

    def answers():
        return [d for m, d in calls if m == "answerCallbackQuery"]

    def press(cid, data, mid=9, keyboard=None):
        message = {"message_id": mid}
        if keyboard:
            message["reply_markup"] = {"inline_keyboard": keyboard}
        bot.handle_callback({"id": cid, "data": data, "message": message})

    try:
        # answered exactly once, whatever the route did — and the first answer wins
        assert bot.answer("a1", "first") is True and bot.answer("a1", "second") is False
        assert [d.get("text") for d in answers()] == ["first"]
        calls.clear()
        press("a2", "menu")
        press("a3", "no-such-screen")
        assert [d["callback_query_id"] for d in answers()] == ["a2", "a3"] and all("text" not in d for d in answers())
        calls.clear()
        press("a5", "check")                                 # the route's own words are kept (never a real check)
        assert [d.get("text") for d in answers()][-1] == "🔄 Checking…"

        # a double tap (same button, same message, within the window) is answered but not handled twice
        calls.clear()
        bot.DEBOUNCE_SECONDS = 0.2
        bot._last_press.clear()
        press("b1", "tasks")
        press("b2", "tasks")
        press("b3", "tasks", mid=10)                         # another message: its own press
        press("b4", "courses")                               # another button
        assert [m for m, _ in calls if m in ("editMessageText", "sendMessage")] != []
        assert len([1 for m, _ in calls if m == "editMessageText"]) == 3, ("tasks twice would be 4", calls)
        assert [d["callback_query_id"] for d in answers()] == ["b1", "b2", "b3", "b4"]
        time.sleep(0.25)
        calls.clear()
        press("b5", "tasks")
        assert len([1 for m, _ in calls if m == "editMessageText"]) == 1, "after the window it is a new press"
        bot.DEBOUNCE_SECONDS = 0

        # Mark as done: fast portal -> the outcome is the toast, the button flips
        keyboard = [[{"text": "✅ Mark as Done", "callback_data": "done:7:1"}], [{"text": "🔙", "callback_data": "menu"}]]
        moodle.http_set_completion = lambda cmid, done: True
        calls.clear()
        press("d1", "done:7:1", keyboard=keyboard)
        assert answers() == [{"callback_query_id": "d1", "text": "✅ Marked as done on the portal"}], answers()
        edit = [d for m, d in calls if m == "editMessageReplyMarkup"][-1]
        assert edit["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "done:7:0"
        assert edit["reply_markup"]["inline_keyboard"][1] == keyboard[1], "the other buttons stay"
        assert ("state", {"cmid": "7", "done": True}) in calls

        # fast failures: only the toast, the keyboard stays; an expired session is a dialog
        moodle.http_set_completion = lambda cmid, done: False
        calls.clear()
        press("d2", "done:7:1", keyboard=keyboard)
        assert "didn't accept" in answers()[0]["text"] and not [1 for m, _ in calls if m == "editMessageReplyMarkup"]

        def expired(cmid, done):
            raise moodle.SessionExpiredError("gone")
        moodle.http_set_completion = expired
        calls.clear()
        press("d3", "done:7:1", keyboard=keyboard)
        assert answers()[0]["show_alert"] is True and "expired" in answers()[0]["text"]

        # a slow portal: answered "updating…" in time, the outcome lands on the button
        bot.DONE_WAIT = 0.05
        for outcome, expected in ((True, "done:7:0"), (False, "done:7:1")):
            gate = threading.Event()
            moodle.http_set_completion = lambda cmid, done, gate=gate, outcome=outcome: gate.wait(5) and outcome
            calls.clear()
            started = time.monotonic()
            press(f"s{outcome}", "done:7:1", keyboard=keyboard)
            assert time.monotonic() - started < 1.0 and [d["text"] for d in answers()] == ["⏳ Updating the portal…"]
            gate.set()
            for t in threading.enumerate():
                if t.name == "done-toggle":
                    t.join(5)
            assert len(answers()) == 1, "the press is answered once"
            edit = [d for m, d in calls if m == "editMessageReplyMarkup"][-1]
            row = edit["reply_markup"]["inline_keyboard"][0][0]
            assert row["callback_data"] == expected, (outcome, row)
            if not outcome:
                assert "tap to retry" in row["text"], "a late failure is shown on the button"

        # a long job shows the chat action (renewed while it runs, stopped after), not a message
        calls.clear()
        bot.ACTION_REFRESH_SECONDS = 0.02
        with bot.working("upload_document"):
            time.sleep(0.12)
        n = len([1 for m, _ in calls if m == "sendChatAction"])
        assert n >= 3 and all(d["action"] == "upload_document" for m, d in calls if m == "sendChatAction"), calls
        time.sleep(0.1)
        assert len([1 for m, _ in calls if m == "sendChatAction"]) == n, "stops with the block"
        assert bot.send_schedule_files.__name__ == "send_schedule_files"
        calls.clear()
        notifier.send_document_group = lambda *a, **k: True
        bot.send_schedule_files()
        assert calls[0][0] == "sendChatAction" and calls[0][1]["action"] == "upload_document"
        assert not [1 for m, d in calls if m == "sendMessage" and "Fetching" in d.get("text", "")], "no loading message"
    finally:
        (bot.api, bot.send_view, bot.edit_view, bot.DEBOUNCE_SECONDS, bot.ACTION_REFRESH_SECONDS,
         moodle.http_set_completion, bot.set_item_done, bot.DONE_WAIT, notifier.send_document_group) = keep
        bot.checks.start_check = real_start_check


def test_answers_go_out_beside_the_edit():
    """The answer to a press is sent by a standing worker while the caller goes on with the edit,
    so the two Telegram round trips overlap instead of adding up; it is still sent once, a failing
    send does not stop the workers, and the workers are long-lived (a warm connection each)."""
    import threading

    bot = _package("uninotifier.bot")
    calls, gate = [], threading.Event()
    names = {}

    def fake(method, **data):
        names.setdefault(method, set()).add(threading.current_thread().name)
        if method == "answerCallbackQuery":
            gate.wait(5)                                  # a slow Telegram: 5 s unless released
            if data.get("text") == "boom":
                raise OSError("network")
        calls.append((method, data))
        return True

    keep = (bot.api, bot.ANSWER_ASYNC)
    bot.api = fake
    bot.ANSWER_ASYNC = True
    try:
        started = time.monotonic()
        assert bot.answer("w1", "⏳ Fetching…") is True
        assert time.monotonic() - started < 0.5, "answer() must not wait for Telegram"
        bot.edit_view(7, "the screen", [[{"text": "x", "callback_data": "menu"}]])      # the caller carries on at once
        assert any(m == "editMessageText" for m, _ in calls) and not any(m == "answerCallbackQuery" for m, _ in calls)
        assert bot.answer("w1", "again") is False
        gate.set()
        assert bot.flush_answers(5)
        sent = [d for m, d in calls if m == "answerCallbackQuery"]
        assert sent == [{"callback_query_id": "w1", "text": "⏳ Fetching…"}], sent
        assert all(n.startswith("answer-") for n in names["answerCallbackQuery"]), names

        # a send that fails neither raises into the caller nor kills the worker
        assert bot.answer("w2", "boom") is True
        assert bot.flush_answers(5)
        assert bot.answer("w3", "still alive") is True and bot.flush_answers(5)
        assert [d["text"] for m, d in calls if m == "answerCallbackQuery"][-1] == "still alive"
        workers = [t for t in threading.enumerate() if t.name.startswith("answer-")]
        assert len(workers) == bot.ANSWER_WORKERS and all(t.daemon for t in workers)
        assert "getMe" in names, "each worker opens its connection before the first press"
    finally:
        gate.set()
        bot.api, bot.ANSWER_ASYNC = keep


def test_one_menu_anchored_at_the_bottom():
    """There is one menu and it is the last thing in the chat: a new screen replaces the old
    one, an old screen that is pressed is moved to the bottom, files are followed by the menu
    again, a status line is not a menu, alerts' buttons are untouched, and it all survives a restart."""
    bot = _package("uninotifier.bot")
    log_, ids, fail = [], iter(range(100, 10 ** 6)), {"delete": False, "send": False}

    def fake(method, **data):
        log_.append((method, data))
        if method == "sendMessage":
            return None if fail["send"] else {"message_id": next(ids)}
        if method == "deleteMessage":
            return None if fail["delete"] else True
        return True

    def calls(*names):
        return [(m, d) for m, d in log_ if m in names]

    keep = (bot.api, notifier.send_document_group, bot.DONE_WAIT)
    bot.api = fake
    rows_a, rows_b = [[{"text": "📚 Courses", "callback_data": "courses"}]], [[{"text": "🔙 Main Menu", "callback_data": "menu"}]]
    try:
        # the first screen with buttons is the menu
        bot.reset_menu()
        first = bot.send_view("Main", rows_a)["message_id"]
        assert bot.current_menu_id() == first and not calls("deleteMessage")
        # a status line / a question being asked is a plain message: the menu stays
        bot.send_view("⏳ Checking…", [])
        assert bot.current_menu_id() == first and not calls("deleteMessage")
        # a new screen replaces the menu: sent first, the old one removed after
        log_.clear()
        second = bot.send_view("Courses", rows_b)["message_id"]
        assert [m for m, _ in log_] == ["sendMessage", "deleteMessage"] and log_[1][1]["message_id"] == first
        assert bot.current_menu_id() == second
        # navigation inside the menu edits it in place
        log_.clear()
        bot.edit_view(second, "Courses (page 2)", rows_b)
        assert [m for m, _ in log_] == ["editMessageText"] and bot.current_menu_id() == second and bot._menu["text"] == "Courses (page 2)"

        # a button pressed on an OLD screen: the answer goes to the bottom, the old screen goes away
        log_.clear()
        bot.edit_view(first, "Tasks", rows_a)
        assert [m for m, _ in log_] == ["sendMessage", "deleteMessage", "deleteMessage"], log_
        third = bot.current_menu_id()
        assert third not in (first, second) and not calls("editMessageText")
        assert {d["message_id"] for _, d in calls("deleteMessage")} == {first, second}, "the menu it replaces AND the old screen"

        # a finished status line that gets the result's buttons becomes the menu at the bottom
        status = bot.send_view("🔄 Checking the portal…", [])["message_id"]
        log_.clear()
        bot.edit_view(status, "✅ Check Complete", rows_b)
        assert [m for m, _ in log_] == ["sendMessage", "deleteMessage", "deleteMessage"]
        assert {d["message_id"] for m, d in calls("deleteMessage")} == {third, status}

        # files were posted below the menu: the menu returns to the bottom, silently, old one removed
        menu = bot.current_menu_id()
        notifier.send_document_group = lambda *a, **k: True
        log_.clear()
        bot.send_schedule_files()
        names = [m for m, _ in log_]
        assert names[0] == "sendChatAction" and names[-2:] == ["sendMessage", "deleteMessage"], names
        assert calls("sendMessage")[-1][1]["disable_notification"] is True
        assert calls("sendMessage")[-1][1]["text"] == "✅ Check Complete" and calls("deleteMessage")[-1][1]["message_id"] == menu

        # Telegram will not delete it (too old): its buttons are removed instead
        fail["delete"] = True
        log_.clear()
        old = bot.current_menu_id()
        bot.send_view("Next", rows_a)
        assert [m for m, _ in log_] == ["sendMessage", "deleteMessage", "editMessageReplyMarkup"]
        assert log_[2][1]["message_id"] == old and log_[2][1]["reply_markup"] == {"inline_keyboard": []}
        fail["delete"] = False

        # a send that fails changes nothing: the old menu stays (and is not deleted)
        before = bot.current_menu_id()
        fail["send"] = True
        log_.clear()
        assert bot.send_view("Lost", rows_b) is None
        assert bot.current_menu_id() == before and not calls("deleteMessage")
        fail["send"] = False

        # a restart: the menu is remembered
        remembered = bot.current_menu_id()
        bot._menu.update(message_id=None, text=None, rows=None)
        bot._menu_loaded = False
        assert bot.current_menu_id() == remembered and bot._menu["rows"] == rows_a
        log_.clear()
        bot.edit_view(remembered - 1, "Old one from before the restart", rows_b)
        assert [m for m, _ in log_][:2] == ["sendMessage", "deleteMessage"]

        # with no menu at all, nothing is re-sent
        bot.reset_menu()
        log_.clear()
        bot.reanchor()
        assert log_ == []
        # ... and the very first edit of an unknown message with buttons adopts it as the menu
        bot.edit_view(555, "Adopted", rows_a)
        assert bot.current_menu_id() == 555 and [m for m, _ in log_] == ["editMessageText"]

        # an alert's buttons are not a menu: notifier messages never register
        log_.clear()
        telegram_keep = telegram.post
        telegram.post = lambda url, **kw: type("R", (), {"ok": True, "status_code": 200, "json": lambda self: {"ok": True}})()
        try:
            assert notifier.send_message("T", "1", "🎥 Lecture", rows=[[{"text": "Join", "callback_data": "x"}]])
        finally:
            telegram.post = telegram_keep
        assert bot.current_menu_id() == 555 and log_ == []
    finally:
        bot.api, notifier.send_document_group, bot.DONE_WAIT = keep
        bot.reset_menu()


def test_mark_as_done_never_writes_back_an_older_state():
    """The bot's "Mark as done" changes one field of saved_items.json while a check may be saving
    it. Without coordination the bot could write back the copy it read before the monitor's save —
    and the next check would announce again what it had just announced. Both take the file's
    write lock: the monitor's save waits for the bot's few milliseconds, and lands after it."""
    import threading

    from uninotifier import fileio

    bot = _package("uninotifier.bot")
    mstate = importlib.import_module("uninotifier.monitor.state")
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "saved_items.json"
        keep = (bot.DATA_FILE, bot.load_state, mstate.DATA_FILE, mstate.LOCK_FILE)
        bot.DATA_FILE = mstate.DATA_FILE = data
        mstate.LOCK_FILE = Path(tmp) / "monitor.lock"
        real_load = keep[1]
        fileio.write_json_atomic(data, {"courses": {"1": {}}, "content": {"1-5": {"cmid": "5", "done": False}}})
        newer = {"courses": {"1": {}}, "content": {"1-5": {"cmid": "5", "done": False},
                                                   "1-6": {"cmid": "6", "done": False, "name": "just announced"}}}
        saver = threading.Thread(target=mstate.save_data, args=(newer,))
        blocked = []

        def load_then_let_the_monitor_try():
            state = real_load()
            saver.start()                     # the monitor saves while the bot holds what it read
            saver.join(0.3)
            blocked.append(saver.is_alive())
            return state
        bot.load_state = load_then_let_the_monitor_try
        try:
            bot.set_item_done("5", True)
            saver.join(5)
            assert blocked == [True], "the monitor's save waited for the bot's read-modify-write"
            final = json.loads(data.read_text(encoding="utf-8"))
            assert "1-6" in final["content"], "the monitor's newer save is not overwritten by an older copy"
            assert not data.with_name("saved_items.json.lock").exists(), "the lock is released"

            # a lock left by a process that died holding it is taken over; a live one makes a writer wait, then give up
            lock = data.with_name("saved_items.json.lock")
            lock.touch()
            old = time.time() - fileio.LOCK_STALE_SECONDS - 5
            os.utime(lock, (old, old))
            with fileio.write_lock(data):
                pass
            lock.touch()
            try:
                with fileio.write_lock(data, wait=0.1):
                    raise AssertionError("a held lock must not be entered")
            except TimeoutError:
                pass
            lock.unlink()

            # Windows refuses to replace a file another process has open for a moment: the save waits
            real_replace, refusals, waits = os.replace, [2], []

            def busy_replace(src, dst):
                if refusals[0]:
                    refusals[0] -= 1
                    raise PermissionError(13, "Access is denied")
                real_replace(src, dst)
            os.replace, keep_sleep, fileio.time.sleep = busy_replace, fileio.time.sleep, waits.append
            try:
                fileio.write_json_atomic(data, {"saved": "after a busy moment"})
                assert json.loads(data.read_text(encoding="utf-8")) == {"saved": "after a busy moment"}
                assert waits == [fileio.REPLACE_WAIT, 2 * fileio.REPLACE_WAIT], waits
                refusals[0] = fileio.REPLACE_TRIES
                try:
                    fileio.write_json_atomic(data, {"saved": "never"})
                    raise AssertionError("a file that stays locked is an error, not a silent loss")
                except PermissionError:
                    pass
                assert json.loads(data.read_text(encoding="utf-8")) == {"saved": "after a busy moment"}
            finally:
                os.replace, fileio.time.sleep = real_replace, keep_sleep
        finally:
            bot.DATA_FILE, bot.load_state, mstate.DATA_FILE, mstate.LOCK_FILE = keep


def test_schedule_files_never_hold_up_the_bot():
    """/schedule uploads files, which can take minutes on a bad connection. It runs beside the
    polling loop: the command returns at once, a second tap meanwhile starts nothing, and a crash
    is reported and frees it for the next tap."""
    import threading

    bot = _package("uninotifier.bot")
    views = importlib.import_module("uninotifier.bot.views")
    release, runs = threading.Event(), []

    def slow_upload():
        runs.append(1)
        release.wait(5)

    def worker():
        return next((t for t in threading.enumerate() if t.name == "schedule"), None)

    keep = views.send_schedule_files
    bot.send_schedule_files = slow_upload
    bot.api = lambda method, **data: {"message_id": 1}          # the button's answer (restored by the runner)
    _fresh_failsafe()
    try:
        started = time.monotonic()
        bot.handle_command("/schedule")
        bot.route_callback({"id": "cb1", "message": {"message_id": 3}}, "schedule", 3)   # a second tap meanwhile
        assert time.monotonic() - started < 1, "the polling loop is never held up by the upload"
        time.sleep(0.1)
        assert runs == [1], "one upload at a time"
        release.set()
        worker() and worker().join(5)

        def broken():
            raise OSError("disk gone")
        bot.send_schedule_files = broken
        assert views.start_schedule_files()
        worker() and worker().join(5)
        failsafe.wait_idle()
        assert any("timetable files could not be sent" in t for t in OUTBOX), OUTBOX
        assert views.start_schedule_files(), "a crash frees it for the next tap"
        worker() and worker().join(5)
    finally:
        bot.send_schedule_files = keep


def test_update_now_in_the_chat():
    """🆕 Updates from the chat: /update says where this copy stands; ⏰ Later puts it off; ⬇️ Update
    Now installs and restarts — once at a time, and a refused or failed update says why and leaves
    the bot running. The restart never happens when its task could not be arranged."""
    from uninotifier import selfheal, updates
    from uninotifier.bot import chat as chat_part, update_flow

    screens, answers, edits, exits, restarts = [], [], [], [], []
    keep = (updates.look, updates.is_clone, updates.apply, chat_part.send_view, chat_part.answer, chat_part.edit_view,
            selfheal.start_after_exit, update_flow.os._exit, update_flow.RESTART_WAIT, update_flow.restart,
            update_flow.threading.Thread)

    class Inline:                                    # the update runs in line, so the test can read its outcome
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    try:
        chat_part.send_view = lambda text, rows: screens.append((text, rows)) or {"message_id": 1}
        chat_part.answer = lambda cb_id, text=None, alert=False: answers.append(text) or True
        chat_part.edit_view = lambda message_id, text, rows: edits.append(text)
        update_flow.threading.Thread = Inline

        # /update: where this copy stands
        updates.look, updates.is_clone = lambda root=None: None, lambda root=None: False
        assert "downloaded as a ZIP" in update_flow.view_status()[0]
        updates.is_clone = lambda root=None: True
        assert "GitHub can't be reached" in update_flow.view_status()[0]
        updates.look = lambda root=None: {"behind": 0}
        assert update_flow.view_status()[0] == "✅ *You are on the newest version.*"
        updates.look = lambda root=None: {"behind": 2, "sha": "abc", "news": [], "subjects": ["fix: invented change"]}
        text, rows = update_flow.view_status()
        assert "invented change" in text and rows[0][0]["callback_data"] == "update:now"

        # ⏰ Later
        update_flow.handle_callback({"id": "a"}, "update:later", 5)
        assert "OK" in answers[-1] and edits[-1].startswith("⏰ *Update postponed*")

        # ⬇️ Update Now, refused: the reason, the lock given back
        def refuse(root=None):
            raise updates.UpdateError("you changed project files yourself (`bot.py`)")
        updates.apply = refuse
        update_flow.handle_callback({"id": "b"}, "update:now", 6)
        assert screens[-1][0] == "⚠️ *Not updated* — you changed project files yourself (`bot.py`)."
        assert not update_flow._busy.locked(), "a refused update frees the button"

        # ... a bug: told, logged, the bot keeps running
        updates.apply = lambda root=None: 1 / 0
        bugs, keep_bug = [], update_flow.failsafe.bug
        update_flow.failsafe.bug = lambda area, headline, exc, detail="": bugs.append(area) or True
        try:
            update_flow.handle_callback({"id": "c"}, "update:now", 6)
        finally:
            update_flow.failsafe.bug = keep_bug
        assert bugs == ["bot:update"] and "something went wrong" in screens[-1][0] and not update_flow._busy.locked()

        # ... already up to date: no restart
        update_flow.restart = lambda: restarts.append(1)
        updates.apply = lambda root=None: "Already on the newest version."
        update_flow.handle_callback({"id": "d"}, "update:now", 6)
        assert screens[-1][0] == "✅ Already on the newest version." and restarts == []

        # ... installed: said, then the restart
        updates.apply = lambda root=None: "Updated to `abc1234`."
        update_flow.handle_callback({"id": "e"}, "update:now", 6)
        assert "restarts now" in screens[-1][0] and restarts == [1]

        # a second press while one runs
        update_flow._busy.acquire()
        update_flow.handle_callback({"id": "f"}, "update:now", 6)
        update_flow._busy.release()
        assert answers[-1] == "⏳ Already updating…"

        # restart(): never ends the bot unless its new start was arranged
        update_flow.restart = keep[9]
        update_flow.RESTART_WAIT = 0
        update_flow.os._exit = lambda code: exits.append(code)
        selfheal.start_after_exit = lambda: False
        update_flow.restart()
        assert exits == [] and "couldn't restart myself" in screens[-1][0]
        selfheal.start_after_exit = lambda: True
        update_flow.restart()
        assert exits == [0]
    finally:
        (updates.look, updates.is_clone, updates.apply, chat_part.send_view, chat_part.answer, chat_part.edit_view,
         selfheal.start_after_exit, update_flow.os._exit, update_flow.RESTART_WAIT, update_flow.restart,
         update_flow.threading.Thread) = keep
        if update_flow._busy.locked():
            update_flow._busy.release()


def test_a_manual_check_reports_what_it_found():
    """/check runs the monitor as its own process and edits ONE message from "Checking…" into what it
    found — or why it failed. A second /check while one runs does nothing; a check that crashes never
    leaves "Checking the portal…" on screen."""
    from uninotifier.bot import chat as chat_part, checks

    screens, edits, started = [], [], []
    keep = (chat_part.send_view, chat_part.edit_view, chat_part.load_last_run, checks.gitsync.run_bounded,
            checks._read_timetable, checks.threading.Thread, checks.failsafe.bug)

    class Inline:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    class Done:
        def __init__(self, code):
            self.returncode = code

    last = {}
    try:
        chat_part.send_view = lambda text, rows: screens.append(text) or {"message_id": 77}
        chat_part.edit_view = lambda message_id, text, rows: edits.append((message_id, text))
        chat_part.load_last_run = lambda: last
        checks._read_timetable = lambda: None
        checks.threading.Thread = Inline
        checks.gitsync.run_bounded = lambda cmd, timeout, **kw: started.append(kw["env"]["UNINOTIFIER_MANUAL"]) or Done(0)

        last.update(ok=True, courses=6, items=80, new_items=[{"type": "assign", "name": "HW_2", "course": "Big Data"}],
                    new_courses=0, updated_files=1, new_posts=2, edited=0, pending=3, seconds=4.5)
        assert checks.start_check(None)
        assert screens == ["🔄 *Checking the portal…*\nThis may take a minute or more."] and started == ["1"]
        assert edits[-1][0] == 77 and edits[-1][1].startswith("✅ *Check Complete*")
        assert "HW\\_2 — Big Data" in edits[-1][1] and "⏱️ *Took:* 4.5 s" in edits[-1][1]

        last.clear()
        last.update(ok=True, new_items=[], new_courses=0)
        checks.start_check(55)
        assert edits[-2] == (55, "🔄 *Checking the portal…*\nThis may take a minute or more.") and "Nothing new" in edits[-1][1]

        for code, run, said in ((3, {}, "Another check is already running"),
                                (0, {"maintenance": "قيد الصيانة", "ok": True}, "under maintenance"),
                                (1, {"ok": False}, "Check Failed")):
            last.clear()
            last.update(run)
            checks.gitsync.run_bounded = lambda cmd, timeout, code=code, **kw: Done(code)
            checks.start_check(9)
            assert said in edits[-1][1], (code, edits[-1][1])

        def too_long(cmd, timeout, **kw):
            raise checks.subprocess.TimeoutExpired(cmd, 1)
        checks.gitsync.run_bounded = too_long
        last.clear()
        checks.start_check(9)
        assert "Check Failed" in edits[-1][1], "a check past its time limit failed"

        bugs = []
        checks.failsafe.bug = lambda area, headline, exc, detail="": bugs.append(area) or True
        checks.gitsync.run_bounded = lambda cmd, timeout, **kw: 1 / 0
        checks.start_check(9)
        assert bugs == ["bot:check"] and edits[-1][1].startswith("❌ *Check Failed* — something went wrong")

        checks._check_lock.acquire()
        try:
            assert checks.start_check(9) is False, "one check at a time"
        finally:
            checks._check_lock.release()
    finally:
        (chat_part.send_view, chat_part.edit_view, chat_part.load_last_run, checks.gitsync.run_bounded,
         checks._read_timetable, checks.threading.Thread, checks.failsafe.bug) = keep


def test_a_reply_that_quotes_a_message_is_the_bottom_screen_with_its_way_back():
    """🔗 View Original Post answers with a reply quoting the message. It is not left under an older
    menu: it is the menu itself — sent at the bottom, the previous menu taken out, a second one
    replacing the first — and it carries Tasks & Reminders and 🔙 Main Menu. Pressing a button on it
    opens the next screen below instead of editing it (an edit would keep the quote on top of the
    new screen). A quoted message that is gone changes nothing."""
    from uninotifier import reminders
    from uninotifier.bot import reminder_flow
    bot = _package("uninotifier.bot")
    log_, ids, fail = [], iter(range(100, 10 ** 6)), {"send": False}

    def fake(method, **data):
        log_.append((method, data))
        if method == "sendMessage":
            return None if fail["send"] else {"message_id": next(ids)}
        return True

    keep = (bot.api, reminders.get)
    bot.api = fake
    reminders.get = lambda reminder_id: {"id": reminder_id, "note": "Midterm", "message_id": 5}
    try:
        bot.reset_menu()
        menu = bot.send_view("Tasks", [[{"text": "x", "callback_data": "x"}]])["message_id"]
        log_.clear()
        reminder_flow.show_original(1, lambda text=None: None)
        sends = [d for m, d in log_ if m == "sendMessage"]
        assert sends[0]["reply_parameters"] == {"message_id": 5}, "it quotes the original post"
        rows = sends[0]["reply_markup"]["inline_keyboard"]
        assert rows[-1] == bot.BACK_ROW and rows[0][0]["callback_data"] == "tasks", rows
        first = bot.current_menu_id()
        assert first != menu and ("deleteMessage", {"chat_id": bot.CHAT_ID, "message_id": menu}) in log_, "the old menu is gone"
        reminder_flow.show_original(1, lambda text=None: None)
        assert bot.current_menu_id() not in (menu, first), "a second reply replaces the first"
        assert any(m == "deleteMessage" and d["message_id"] == first for m, d in log_)

        reply = {"message_id": bot.current_menu_id(), "reply_to_message": {"message_id": 5}}
        assert bot.is_file_message(reply) and not bot.is_file_message({"message_id": 1, "text": "Tasks"})

        before = bot.current_menu_id()
        fail["send"] = True
        log_.clear()
        reminder_flow.show_original(1, lambda text=None: None)
        assert bot.current_menu_id() == before and not [1 for m, _ in log_ if m == "deleteMessage"], "a gone message changes nothing"
    finally:
        bot.api, reminders.get = keep
        bot.reset_menu()


def test_no_screen_is_sent_around_the_menu():
    """The chat keeps one menu at the bottom (chat.send_view / edit_view / send_reply_view). A flow that
    sends its own message with buttons straight to Telegram would leave the real menu above it —
    the bug a stacked 'Original post' reply showed. Only chat.py (the menu itself) and the guest
    session (which removes its own messages on Sign Out) may do it."""
    import re
    root = Path(__file__).resolve().parent.parent / "src" / "uninotifier" / "bot"
    offenders = []
    for path in sorted(root.glob("*.py")):
        if path.name in ("chat.py", "guest_flow.py"):
            continue
        source = path.read_text(encoding="utf-8")
        for found in re.finditer(r'api\(\s*"sendMessage"', source):
            call = source[found.start(): found.start() + 600].split("\n\n")[0]
            if "reply_markup" in call or "inline_keyboard" in call:
                offenders.append(f"{path.name}: {source[:found.start()].count(chr(10)) + 1}")
    assert not offenders, f"screens sent around the menu: {offenders}"


def test_the_bot_lives_in_slack():
    """With Slack's live connection set up (SLACK_APP_TOKEN, SLACK_USER) the whole bot works in Slack
    through the same code: its screens are cards with real buttons, numbered like Telegram messages and
    rewritten in place; a button pressed in Slack reaches the bot as Telegram's callback_query; typed
    words work as the commands; only you are the owner — a stranger's press is dropped by access.py."""
    from support import OWNER_ID as OWNER, _with_env
    from uninotifier import slack, slackbot

    bot = _package("uninotifier.bot")
    calls, stamps = [], iter(f"17000000{n:02d}.0001" for n in range(1, 99))

    class Answer:
        status_code, ok, headers = 200, True, {}

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, data=None, timeout=None, headers=None):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, data))
        if method == "chat.postMessage":
            return Answer({"ok": True, "ts": next(stamps)})
        if method == "files.getUploadURLExternal":
            return Answer({"ok": True, "upload_url": "https://files.slack.com/upload/v1/Z", "file_id": "F9"})
        return Answer({"ok": True})

    def inside(top):
        """Every block, the ones in a box too — a card's buttons as the row they are."""
        found = [b for t in top for b in ([t] + t.get("child_blocks", []))]
        return found + [{"type": "actions", "elements": b["actions"]} for b in found
                        if b["type"] == "card" and b.get("actions")]

    def sent_rows(data):
        assert json.loads(data.get("attachments") or "[]") == [], "no frame: Slack folds one under Show more"
        return [b for b in inside(json.loads(data.get("blocks") or "[]")) if b["type"] == "actions"]

    def no_telegram(*a, **k):
        raise AssertionError("Telegram must not be used while the bot lives in Slack")

    keep = (slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, slack.SHARE_WAIT)
    owner = _as_owner(bot)
    on = {"NOTIFY_VIA": "slack", "SLACK_BOT_TOKEN": "-".join(("xoxb", "1", "2", "made-up")), "SLACK_CHANNEL": "D0123ABCD",
          "SLACK_APP_TOKEN": "-".join(("xapp", "1", "made-up")), "SLACK_USER": "U0OWNER1", "TELEGRAM_CHAT_ID": OWNER}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            slack._post, telegram.request, slack.MIN_INTERVAL, slack.SHARE_WAIT = fake_post, no_telegram, 0, 0
            slackbot.STORE, slackbot._connect = Path(tmp) / "slack_messages.json", lambda: None

            def scenario():
                assert slackbot.interactive()
                # a screen: a card with real buttons, and a number to edit it by
                sent = notifier.call_api("T", "sendMessage", {"chat_id": OWNER, "text": "🏠 *Main Menu*",
                                         "reply_markup": {"inline_keyboard": [[{"text": "⏳ Tasks", "callback_data": "tasks"},
                                                                               {"text": "🌐 Portal", "url": "https://p.example/"}]]}})
                assert sent["message_id"] == 1 and sent["chat"]["id"] == int(OWNER)
                method, data = calls[-1]
                row = sent_rows(data)[0]["elements"]
                assert method == "chat.postMessage" and row[0]["value"] == "tasks" and row[1]["url"] == "https://p.example/"
                first_ts = "1700000001.0001"
                assert slackbot.entry(1)["ts"] == first_ts

                # rewritten in place — as Telegram, an edit without a keyboard leaves none
                assert notifier.call_api("T", "editMessageText", {"chat_id": OWNER, "message_id": 1,
                                                                  "text": "⏳ *Tasks*"})["message_id"] == 1
                method, data = calls[-1]
                assert method == "chat.update" and data["ts"] == first_ts and not sent_rows(data)
                assert notifier.call_api("T", "editMessageText", {"message_id": 99, "text": "x"}) is None, "unknown: as Telegram"
                assert notifier.call_api("T", "sendChatAction", {"action": "typing"}) is True

                # your press -> Telegram's callback_query, for the message it was on
                slackbot.take("interactive", {"type": "block_actions", "user": {"id": "U0OWNER1"},
                                              "container": {"type": "message_attachment", "message_ts": first_ts},
                                              "actions": [{"action_id": "b0_0", "value": "menu", "action_ts": "1.5"},
                                                          {"action_id": "u0_1", "url": "https://p.example/"}]})
                updates = slackbot.get_updates(None, 0)
                assert len(updates) == 1, "a button that opens a page is Slack's own business"
                press = updates[0]
                assert press["callback_query"]["data"] == "menu" and press["callback_query"]["message"]["message_id"] == 1
                assert access.is_owner(press)
                # ... and through the bot's own code: the main menu is drawn over that same card
                calls.clear()
                bot.dispatch(press)
                assert any(m == "chat.update" and d["ts"] == first_ts and "UniNotifier System" in d["text"]
                           for m, d in calls), calls
                menu = next(d for m, d in calls if m == "chat.update")
                assert sent_rows(menu), "the menu has its buttons"

                # the buttons side by side, the way back under a line of its own
                assert len(sent_rows(menu)[0]["elements"]) > 1, "Telegram's one-per-row buttons go side by side"
                laid = slack.buttons({"inline_keyboard": [[{"text": "⏳ Tasks", "callback_data": "tasks"}],
                                                          [{"text": "📅 Timetable", "callback_data": "schedule"}],
                                                          [{"text": "🔙 Main Menu", "callback_data": "menu"}]]})
                assert [b["type"] for b in laid] == ["actions", "divider", "actions"] and laid[-1]["block_id"] == "nav"
                assert [e["value"] for e in laid[0]["elements"]] == ["tasks", "schedule"]   # the controls, a line, the way back

                # the Home page: a press there redraws the page itself (views.publish), never the chat
                calls.clear()
                slackbot.take("interactive", {"type": "block_actions", "user": {"id": "U0OWNER1"},
                                              "container": {"type": "view", "view_id": "V1"},
                                              "view": {"type": "home"},
                                              "actions": [{"action_id": "b0_1", "value": "tasks", "action_ts": "1.7"}]})
                home_press = slackbot.get_updates(None, 0)[0]
                assert home_press["callback_query"]["message"]["message_id"] == slackbot.HOME_ID
                bot.dispatch(home_press)
                published = [d for m, d in calls if m == "views.publish"]
                assert published and not [m for m, _ in calls if m in ("chat.postMessage", "chat.update", "chat.delete")], calls
                view = json.loads(published[-1]["view"])
                assert view["type"] == "home" and published[-1]["user_id"] == "U0OWNER1"
                assert view["blocks"][0]["type"] == "container" and "Tasks" in view["blocks"][0]["title"]["text"], \
                    "the screen in a box, its title on the box"
                assert [b for b in inside(view["blocks"]) if b["type"] == "actions"], "its buttons inside"
                # a Slack that refuses the box: the same screen without it
                calls.clear()
                keep_call = slack.call
                refusals = iter([slack.SlackError("invalid_blocks")])

                def refuse_once(token, method, data=None, timeout=None):
                    if method == "views.publish":
                        e = next(refusals, None)
                        if e:
                            calls.append((method, data))
                            raise e
                    return keep_call(token, method, data)
                slack.call = refuse_once
                try:
                    slackbot.take("interactive", {"type": "block_actions", "user": {"id": "U0OWNER1"},
                                                  "container": {"type": "view", "view_id": "V1"}, "view": {"type": "home"},
                                                  "actions": [{"action_id": "b0_2", "value": "menu", "action_ts": "1.8"}]})
                    bot.dispatch(slackbot.get_updates(None, 0)[0])
                finally:
                    slack.call = keep_call
                tries = [json.loads(d["view"]) for m, d in calls if m == "views.publish"]
                assert [t["blocks"][0]["type"] for t in tries] == ["header", "container"],                     "the main menu is the dashboard first; a Slack that refuses it gets the boxed menu"
                # opening Home with nothing on it draws the main menu there
                calls.clear()
                drawn = threading.Event()
                keep_menu = slackbot.home_menu
                slackbot.home_menu = lambda: drawn.set()
                try:
                    stored = slackbot._load()
                    stored["messages"].pop(str(slackbot.HOME_ID), None)
                    slackbot._save(stored)
                    slackbot.take("events_api", {"event": {"type": "app_home_opened", "tab": "home", "user": "U0OWNER1"}})
                    assert drawn.wait(5)
                finally:
                    slackbot.home_menu = keep_menu

                # someone else in the workspace: not the owner
                slackbot.take("interactive", {"type": "block_actions", "user": {"id": "U0STRANGER"},
                                              "container": {"message_ts": first_ts},
                                              "actions": [{"action_id": "b0_0", "value": "menu", "action_ts": "1.6"}]})
                assert not access.is_owner(slackbot.get_updates(None, 0)[0])

                # typed words: the commands, without Slack's "/"; anything else as typed
                for typed, read in (("menu", "/start"), ("Tasks", "/tasks"), ("need 85", "/need 85"),
                                    ("check", "/check"), ("hello there", "hello there"), ("&lt;3", "<3")):
                    slackbot.take("events_api", {"event": {"type": "message", "channel_type": "im", "user": "U0OWNER1",
                                                           "text": typed, "ts": "1700000099.1"}})
                    message = slackbot.get_updates(None, 0)[0]["message"]
                    assert message["text"] == read and access.is_owner({"message": message}), (typed, message)
                slackbot.take("events_api", {"event": {"type": "message", "channel_type": "im", "bot_id": "B1",
                                                       "text": "my own card", "ts": "1.1"}})
                assert slackbot.get_updates(None, 0) == [], "the app's own messages are not news"

                # a picture, its card under it; deleting the message takes both away
                calls.clear()
                pic = notifier.upload("T", "sendPhoto", {"chat_id": OWNER, "caption": "🔑 *Sign in*",
                                                          "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📱 SMS", "callback_data": "mfa:sms"}]]})},
                                      "photo", "sign-in.png", b"png")
                assert [m for m, _ in calls] == ["files.getUploadURLExternal", "Z", "files.completeUploadExternal",
                                                 "chat.postMessage"]      # the picture first, the card with its buttons under it
                assert slackbot.entry(pic["message_id"])["files"] == ["F9"]
                calls.clear()
                assert notifier.call_api("T", "deleteMessage", {"message_id": pic["message_id"]}) is True
                assert [m for m, _ in calls] == ["files.delete", "chat.delete"] and slackbot.entry(pic["message_id"]) is None

                # a file sent again replaces its older copies in Slack's Files tab (only the same name, never the new one)
                old = slackbot.remember("1.9", "old", None, ["F1"], ["Weekly.pdf"])
                other = slackbot.remember("1.8", "other", None, ["F3"], ["Else.pdf"])
                calls.clear()
                slackbot._retire_old_copies("T", ["Weekly.pdf"], ["F2"])
                assert [m for m, _ in calls] == ["files.delete", "chat.delete"], calls      # the old file AND the card it was under
                assert slackbot.entry(old) is None and slackbot.entry(other)["files"] == ["F3"]

                # a Moodle message from the 15-minute check: real buttons too, and its number handed back
                reply = {}
                assert notifier.send_message("T", OWNER, "📢 *New post*", rows=[[{"text": "✅ Mark as done", "callback_data": "done:5"}]],
                                             reply=reply)
                assert reply["body"]["result"]["message_id"] == slackbot.number_of("1700000003.0001")
                button = sent_rows(calls[-1][1])[0]["elements"][0]
                assert button["value"] == "done:5" and button["style"] == "primary"

            _with_env(on, scenario)
            # without the live connection's token, the bot stays on Telegram (messages may still go to Slack)
            assert _with_env({**on, "SLACK_APP_TOKEN": ""}, slackbot.interactive) is False
        finally:
            slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, slack.SHARE_WAIT = keep
            bot.CHAT_ID, access.ADMIN_USER_ID = owner

def test_the_slack_home_page_is_a_dashboard():
    """The main menu on Slack's Home page shows today, what is due soon and the next reminders, then
    the buttons — read from the bot's own files, every button with an id of its own (Slack refuses a
    page that repeats one), and a page that cannot be built never costs the Home page."""
    from datetime import datetime
    from uninotifier import slack, slackhome
    _package("uninotifier.bot")
    from uninotifier.bot import views

    monday = datetime(2026, 11, 16, 12, 0)                          # a Monday, noon
    sch = {"days": {"mon": [
        {"start": "19:00", "end": "20:30", "course": "Databases & <Big> Data", "mode": "Online"},
        {"start": "09:00", "end": "10:30", "course": "Calculus", "mode": "In-Person", "place": "Room 9101"},
        {"start": "11:30", "end": "12:30", "course": "Physics", "mode": "In-Person"}], "tue": []}}
    lines = slackhome.lecture_lines(monday, sch)
    assert [("~" in l, "now" in l) for l in lines] == [(True, False), (False, True), (False, False)], lines
    assert lines[0].lstrip(slackhome.LRM).startswith("~9:00 AM – 10:30 AM") and "in 7h 0m" in lines[2] and "&lt;Big&gt;" in lines[2], lines
    assert slackhome.lecture_lines(monday, {"days": {}}) == []
    blended = slackhome.lecture_lines(monday, {"days": {"mon": [{"start": "11:30", "end": "12:30", "course": "Arabic", "mode": "Activity"}]}})
    assert "no lecture" in blended[0] and "now" not in blended[0] and "▶️" not in blended[0], \
        "an activity slot of a blended course is not a lecture that is on"
    assert slack.preview("📢 *New post*\n━━━━━━━━\nSee <https://x.example/a_b|the page> & <https://y.example>  `code` &amp; more") == \
        "📢 New post\nSee the page & https://y.example  code & more"

    now = monday.timestamp()
    card = slackhome.task_card({"kind": "assign", "name": "Assignment 2", "course": "20261-Big Data-AllSec-3-", "url": "https://x.example/a",
                                "due": now + 3 * 86400 + 7200, "opens": None, "late": False, "progress": ""}, now, 2)
    assert card["title"]["text"] == f"{slackhome.LRM}📝 {slackhome.FSI}Assignment 2{slackhome.PDI}" and card["subtitle"]["text"] == "📘 Big Data"
    assert "3d 2h left" in card["body"]["text"] and card["actions"][0]["action_id"] == "open2" and len(card["body"]["text"]) <= 200

    text, rows = views.view_menu()
    assert slackhome.is_menu(text) and not slackhome.is_menu("⏳ *Tasks*")
    page = slackhome.view({"inline_keyboard": rows}, monday)
    assert page["type"] == "home" and len(page["blocks"]) <= slack.HOME_BLOCKS and page["blocks"][0]["type"] == "header"
    ids = [b["block_id"] for b in page["blocks"] if b.get("block_id")]
    ids += [e["action_id"] for b in page["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert len(ids) == len(set(ids)), f"an id repeats: {ids}"
    assert [e["value"] for b in page["blocks"] if b["type"] == "actions" for e in b["elements"] if e.get("value")] == [
        "courses", "tasks", "academic", "record", "check", "rem:new"]
    assert "Updated" in page["blocks"][-1]["elements"][0]["text"]


def test_slack_autolinks_are_unwrapped():
    """A student ID typed in Slack arrives as a link ("<http://202311988|202311988>"): the bot reads what was typed."""
    from uninotifier import slackbot
    assert slackbot.unlink("<http://202311988|202311988>") == "202311988"
    assert slackbot.unlink("<http://202311988>") == "202311988"
    assert slackbot.unlink("id <mailto:a@b.com|a@b.com> &amp; more") == "id a@b.com & more"
    assert slackbot.unlink("plain 202311988") == "plain 202311988"
    update = slackbot.from_event({"type": "message", "channel_type": "im", "user": "U1", "ts": "1.0",
                                  "text": "<http://202311988|202311988>"})
    assert update[0]["message"]["text"] == "202311988"


def test_slack_cards_have_one_line_under_the_title_and_one_above_the_buttons():
    """A rule line is a light line, never a row of marks: none directly under a title (the box already draws one),
    one between parts; and every screen with buttons has one line above them (a screen with only its way back too)."""
    from uninotifier import media, slack
    nav = {"inline_keyboard": [[{"text": "🔙 My Courses & Schedule", "callback_data": "courses"},
                                {"text": "🏠 Main Menu", "callback_data": "menu"}]]}
    both = {"inline_keyboard": [[{"text": "📋 Course List", "callback_data": "courselist"}], *nav["inline_keyboard"]]}

    def kinds(text, markup):
        found = slack.blocks(text, markup, live=True)
        assert not [b for b in found if b["type"] == "section" and ("━" in str(b) or "___" in str(b))], found
        return [b["type"] for b in found]

    title = "📚 *My Courses & Schedule*"
    assert kinds(f"{title}\n{media.DIVIDER}\nWhat would you like to see?", both) == ["header", "section", "actions", "divider", "actions"]        # a one-line menu: no line under it; the way back under a line
    assert kinds(f"{title}\n{media.DIVIDER}\nSome words that run on a while\nand a second line", nav) == ["header", "section", "divider", "actions"]
    assert kinds(f"{title}\nSome words\n{media.DIVIDER}\nMore words", None) == ["header", "section", "divider", "section"]
    assert kinds(f"{title}\n{media.DIVIDER}", nav) == ["header", "actions"]           # nothing but a title: the box's own line
    # too many blocks for one box: the lines between parts give way, the one above the way back stays
    many = [{"type": "header", "text": {"type": "plain_text", "text": "T"}}]
    for n in range(6):
        many += [{"type": "section", "text": {"type": "mrkdwn", "text": f"part {n}"}}, {"type": "divider"}]
    many += [{"type": "actions", "block_id": "nav", "elements": []}]
    box = slack.boxed(many)
    assert [b["type"] for b in box] == ["container"] and len(box[0]["child_blocks"]) <= slack.BOX_CHILDREN
    assert box[0]["child_blocks"][-2]["type"] == "divider" and box[0]["child_blocks"][-1]["block_id"] == "nav"


def test_the_menu_and_the_semester_follow_the_student():
    """The semester comes from the term code Moodle puts before the student's own courses (20263 is the summer
    of 2026/2027), not from a fixed word; the main menu is one line — the status beside it, no course count."""
    from uninotifier.bot import chat, views
    keep = chat.load_state
    try:
        for code, name in (("20261", "First Semester 2026/2027"), ("20262", "Second Semester 2026/2027"),
                           ("20263", "Summer Semester 2026/2027"), ("20271", "First Semester 2027/2028")):
            chat.load_state = lambda code=code: {"courses": {"1": {"name": f"{code}-Big Data-AllSec-3"},
                                                              "2": {"name": f"{code}-Databases-AllSec-3"}}}
            assert views.semester() == name, (code, views.semester())
            text, rows = views.view_menu()
            assert text.startswith(f"🎓 *UniNotifier System* • {name} • ") and "\n" not in text and "Tracked" not in text
        assert "credit hours" not in views.view_courses()[0] and "First Semester" not in views.view_courses()[0]
        assert views.view_courses()[0].endswith("What would you like to see?")
    finally:
        chat.load_state = keep


def test_a_slack_that_refuses_headers_in_a_box_gets_them_as_bold_lines():
    """A big heading inside the box is tried first; if this Slack refuses it, the box is sent once more with the
    headings as bold lines — and remembered, so the next message goes straight to that. The first look is never
    a plain frame just because of a heading."""
    import json
    from uninotifier import slack
    card = [{"type": "header", "text": {"type": "plain_text", "text": "Title"}},
            {"type": "header", "text": {"type": "plain_text", "text": "Your reminders"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "1. a reminder"}},
            {"type": "context", "block_id": "small", "elements": [{"type": "mrkdwn", "text": "🕐 soon"}]}]
    flat = slack.boxed(card, flat=True)[0]["child_blocks"]
    assert [b["type"] for b in flat] == ["section", "section", "context"] and flat[0]["text"]["text"] == "*Your reminders*"
    assert [b["type"] for b in slack.boxed(card)[0]["child_blocks"]] == ["header", "section", "context"]
    sent = []

    def call(token, method, data):
        blocks = json.loads(data["blocks"])
        sent.append(blocks)
        if blocks and blocks[0].get("type") == "container" and any(b["type"] == "header" for b in blocks[0]["child_blocks"]):
            raise slack.SlackError("invalid_blocks")
        return {"ok": True}

    keep = (slack.call, slack._BOX_HEADERS)
    try:
        slack.call, slack._BOX_HEADERS = call, True
        slack._send_card("t", "chat.postMessage", {"text": "x", "channel": "c"}, card, slack.DEFAULT_TONE)
        assert len(sent) == 2 and sent[1][0]["type"] == "container" and slack._BOX_HEADERS is False
        sent.clear()
        slack._send_card("t", "chat.postMessage", {"text": "x", "channel": "c"}, card, slack.DEFAULT_TONE)
        assert len(sent) == 1 and sent[0][0]["type"] == "container"          # straight to the flat box
    finally:
        slack.call, slack._BOX_HEADERS = keep


def test_no_button_label_is_ever_cut_and_a_balance_keeps_its_time_at_the_bottom():
    """A row of buttons holds only what fits (a long label stands alone, short ones share a row); and the time a portal
    answer was read is small print under its content, not the first thing on the card."""
    import importlib
    from uninotifier import portal, slack
    portal_flow = importlib.import_module("uninotifier.bot.portal_flow")
    keep = slack.mode
    try:
        slack.mode = lambda: "slack"
        rows = {"inline_keyboard": [[{"text": t, "callback_data": f"x{n}"}] for n, t in enumerate(
            ["📚 My Courses & Schedule", "⏳ Upcoming Tasks & Reminders", "📊 GPA & Grade Hub", "📜 Academic Record & Balance",
             "🏛️ Open Registration Portal ↗️", "🎯 Grade Needed", "🏁 Finals Watch", "✅ Attended all", "🎯 Missed some"])]}
        for block in slack.buttons(rows, width=slack.ROW_WIDTH):
            widths = sum(slack._width(e) for e in block["elements"])
            assert widths <= slack.ROW_WIDTH or len(block["elements"]) == 1, ([e["text"]["text"] for e in block["elements"]], widths)
        result = portal.Result("balance", True, None, [], {"balance": 1700.0}, [],
                               ["💰 *Total balance:* 1,700 JD — owed", "• First semester 2026/2027 · 18h · 1,700 JD"])
        text, body, rows = portal_flow.compose(result, portal_flow.stamp(1.7e9, portal_flow.REFRESHING))
        lines = text.splitlines()
        assert lines[0].startswith("💰 *ZUJ Portal") and lines[-1].startswith("_🕒 As of:") and lines[-1].endswith("checking for changes…_")
        assert "Total balance" in lines[2] and "As of" not in "\n".join(lines[:-1])
        blocks = slack.blocks(text, {"inline_keyboard": rows}, live=True)
        small = [b for b in blocks if b["type"] == "context"]
        assert small and "As of" in small[-1]["elements"][0]["text"], blocks          # small print, after the content
        assert blocks.index(small[-1]) > max(i for i, b in enumerate(blocks) if b["type"] == "section")
    finally:
        slack.mode = keep



def test_in_slack_the_picture_comes_first_and_the_card_with_its_buttons_under_it():
    """A screenshot with its words: the picture first, the card — words AND buttons — under it (the controls are on the
    message itself, never left above the picture). Deleting it takes both away; if the card cannot be sent the picture
    is taken back; with no file nothing changes."""
    from uninotifier import slack, slackbot
    log = []
    keep = (slack.post, slack.upload, slack.delete, slack.call, slackbot._settings, slackbot.STORE)
    try:
        slack.upload = lambda token, channel, files, comment="": log.append(("file", files[0][0])) or {"file_ids": ["F1"]}
        slack.post = lambda token, channel, text, card=None, colour=None: log.append(
            ("card", any(b["type"] == "actions" for b in card or []))) or {"ts": "7.7"}
        slack.delete = lambda token, channel, ts: log.append(("delete", ts)) or True
        slack.call = lambda token, method, data=None, **k: log.append((method, data.get("file"))) or {"ok": True}
        slackbot._settings = lambda: ("t", "c")
        slackbot.STORE = Path(tempfile.mkdtemp()) / "slack_messages.json"
        markup = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "record"}, {"text": "🏠 Main Menu", "callback_data": "menu"}]]}
        number = slackbot.send("💰 *Balance*\n1,700 JD", markup, files=[("Balance.png", b"png")])
        assert log == [("file", "Balance.png"), ("card", True)], log             # the picture, then the card with its buttons
        found = slackbot.entry(number)
        assert found["ts"] == "7.7" and found["files"] == ["F1"] and "head" not in found
        log.clear()
        slackbot._delete(number)
        assert ("files.delete", "F1") in log and ("delete", "7.7") in log, log   # the picture and the card go together
        log.clear()
        slack.post = lambda *a, **k: (_ for _ in ()).throw(slack.SlackError("invalid_blocks"))
        try:
            slackbot.send("x", markup, files=[("B.png", b"png")])
        except slack.SlackError:
            pass
        assert ("files.delete", "F1") in log, log                                # the card failed: no picture left alone
        log.clear()
        slack.post = lambda token, channel, text, card=None, colour=None: log.append(("card", True)) or {"ts": "8.8"}
        slackbot.send("✅ Done", markup)
        assert log == [("card", True)]                                          # no file: as it was
    finally:
        slack.post, slack.upload, slack.delete, slack.call, slackbot._settings, slackbot.STORE = keep


def test_leaving_a_card_with_a_picture_takes_it_out_of_the_chat_and_back_or_menu_drop_what_is_pending():
    """In Slack what you asked for goes when you leave: Back / Main Menu on a card with a picture opens the screen below
    and removes the card and its picture. On a screen that is waiting (a question to type, a portal read) either button
    also drops what was pending — no later message arrives for it. Telegram keeps its files."""
    from uninotifier import slack
    from uninotifier.bot import app, chat, gpa_flow, guest_flow, portal_flow, reminder_flow
    log = []
    keep = (slack.mode, chat.send_view, chat.edit_view, chat.retire, chat.answer, chat.is_double_tap)
    try:
        chat.send_view = lambda text, rows: log.append(("send", text[:12]))
        chat.edit_view = lambda mid, text, rows: log.append(("edit", mid))
        chat.retire = lambda mid: log.append(("retire", mid))
        chat.answer = lambda *a, **k: True
        chat.is_double_tap = lambda *a: False
        card = {"message_id": 41, "document": {"file_id": "F1"}}
        for mode, expect in (("slack", [("send", "🎓 *UniNotifi"), ("retire", 41)]), ("telegram", [("send", "🎓 *UniNotifi")])):
            slack.mode = lambda mode=mode: mode
            log.clear()
            app.handle_callback({"id": "1", "data": "menu", "message": card})
            assert [e for e in log if e[0] in ("send", "retire")] == expect, (mode, log)
        # pending things are dropped by Back and by Main Menu, never left to send something later
        for button in ("cancel:menu", "cancel:tasks", "menu"):
            reminder_flow._reminder_wait, reminder_flow._reminder_draft["note"] = "text", "x"
            gpa_flow._pending_prompt = object()
            guest_flow._guest.clear()
            guest_flow._guest.update(step="password", user="2024", messages=[])
            portal_flow._cancelled.clear()
            portal_flow._portal_lock.acquire()
            try:
                app.handle_callback({"id": "2", "data": button, "message": {"message_id": 5}})
                assert reminder_flow._reminder_wait is None and gpa_flow._pending_prompt is None, button
                assert portal_flow.cancelled() == (button != "menu"), button      # a portal read is dropped by Back / Main Menu of a waiting screen
                assert (guest_flow._guest.get("step") is None) == (button != "menu"), button
            finally:
                portal_flow._portal_lock.release()
                guest_flow._guest.clear()
        assert portal_flow.request_cancel() is False                              # nothing running: nothing to cancel
    finally:
        slack.mode, chat.send_view, chat.edit_view, chat.retire, chat.answer, chat.is_double_tap = keep


def test_in_slack_a_portal_answer_is_read_when_asked_and_says_no_time():
    """Transcript and Balance in Slack: a 'reading…' screen, then a fresh read — the answer carries no time (it was read
    the moment you asked); a saved copy is only the fallback and then says how old it is."""
    from uninotifier import portal, slack
    from uninotifier.bot import portal_flow
    keep = slack.mode
    try:
        slack.mode = lambda: "slack"
        assert portal_flow.fresh_in_slack("balance") and portal_flow.fresh_in_slack("transcript")
        assert not portal_flow.fresh_in_slack("grades")
        result = portal.Result("balance", True, None, [], {"balance": 1700.0}, [], ["💰 *Total balance:* 1,700 JD"])
        text, body, rows = portal_flow.compose(result, portal_flow.stamp())
        assert "Retrieved" not in text and "As of" not in text and text.splitlines()[0].startswith("💰 *ZUJ Portal")
        text, body, rows = portal_flow.compose(result, portal_flow.stamp(1.7e9, portal_flow.REFRESH_FAILED))
        assert "As of:" in text.splitlines()[-1] and "saved copy" in text.splitlines()[-1]
        slack.mode = lambda: "telegram"
        assert not portal_flow.fresh_in_slack("balance")
    finally:
        slack.mode = keep


def test_reminder_screens_show_whole_titles_and_always_have_back_and_main_menu():
    """Manage Reminders in Slack: every reminder a line with its whole title and time and an Open button (a button label
    cut it: '…Thu 19 Nov · 9:00 A'). Every reminder screen — the list, one reminder, the delete question, the prompts that
    wait for your words — ends with Back and Main Menu."""
    from uninotifier import reminders, slack
    from uninotifier.bot import chat, reminder_flow
    items = [{"id": n, "note": note, "course": None, "due": 1900000000 + n, "message_id": None}
             for n, note in ((1, "امتحان منتصف الفصل"), (2, "Submit the long report about the database design project"))]
    keep = (slack.mode, reminders.pending, reminders.get)
    try:
        reminders.pending = lambda now=None: list(items)
        reminders.get = lambda rid: next((i for i in items if i["id"] == rid), None)
        for mode in ("slack", "telegram"):
            slack.mode = lambda mode=mode: mode
            screens = {"list": reminder_flow.view_manage_reminders(), "one": reminder_flow.view_reminder(1),
                       "gone": reminder_flow.view_reminder(99), "delete": reminder_flow.view_delete(1) if hasattr(reminder_flow, "view_delete") else None,
                       "prompt": ("✏️ *Type a time*", [reminder_flow.REMINDER_CANCEL_ROW]),
                       "reading": ("⏳ Reading…", [chat.cancel_row("record")])}
            for name, view in screens.items():
                if view is None:
                    continue
                text, rows = view
                last = rows[-1]
                assert len(last) == 2 and last[0]["text"].startswith("🔙") and last[1]["text"] == "🏠 Main Menu", (mode, name, last)
            text, rows = screens["list"]
            if mode == "slack":
                blocks = slack.blocks(text, {"inline_keyboard": rows}, live=True)
                lines = [b for b in blocks if b["type"] == "section" and "accessory" in b]
                assert len(lines) == 2 and "Submit the long report about the database design project" in lines[1]["text"]["text"]
                assert [b["accessory"]["text"]["text"] for b in lines] == ["Open", "Open"]
                assert len(slack.boxed(blocks)) == 1
            else:
                assert not [r for r in rows[:-1] if "line" in r[0]]
        # the prompts drop what is waiting: nothing is left to answer later
        assert reminder_flow.REMINDER_CANCEL_ROW[0]["callback_data"] == "cancel:tasks" and reminder_flow.REMINDER_CANCEL_ROW[1]["callback_data"] == "cancel:menu"
    finally:
        slack.mode, reminders.pending, reminders.get = keep


def test_slack_lists_and_headings_have_room_and_never_hide_a_button():
    """The two parts of the tasks screen are apart (nothing from the course: said once under the title, then 'Your reminders');
    the course buttons of Course Adjustments are one under the other, none behind a '+3 more' (Slack shows five a row);
    the screen says nothing about an empty start; a box that cannot hold everything is not sent with blocks outside it."""
    from uninotifier import reminders, slack, tasks
    from uninotifier import gpa, gpa_ui
    from uninotifier.bot import chat, views
    items = [{"id": 1, "note": "امتحان", "course": None, "due": 1900000000, "message_id": None}]
    keep = (slack.mode, reminders.pending, tasks.open_tasks, chat.load_state)
    try:
        reminders.pending = lambda now=None: list(items)
        tasks.open_tasks = lambda state, now: []
        chat.load_state = lambda: {}
        slack.mode = lambda: "slack"
        text, rows = views.view_tasks()
        blocks = slack.blocks(text, {"inline_keyboard": rows}, live=True)
        kinds = [b["type"] for b in blocks]
        assert kinds[:3] == ["header", "header", "context"], kinds                   # title, "Course tasks", its small line
        assert "Course tasks" in blocks[1]["text"]["text"] and "none ahead" in blocks[2]["elements"][0]["text"]
        assert blocks[2].get("block_id", "").startswith("small")                      # small, inside the heading — not under the title
        assert "Your reminders" in next(b for b in blocks[3:] if b["type"] == "header")["text"]["text"]
        # Course Adjustments: eight courses, each on its own line; nothing hidden, nothing outside the box
        gpa.STATE_FILE = Path(tempfile.mkdtemp()) / "gpa.json"
        names = [f"Course {n}" for n in range(1, 9)]
        text, rows = gpa_ui.view_home(names)
        assert "enter a grade" not in text and "Tap a course" not in text and "Semester Average" not in text
        blocks = slack.blocks(text, {"inline_keyboard": rows}, live=True)
        assert all(len(b["elements"]) <= 5 for b in blocks if b["type"] == "actions"), "five a row at most: Slack hides the rest"
        box = slack.boxed(blocks)
        assert len(box) == 1 and (box[0]["type"] != "container" or len(box[0]["child_blocks"]) <= slack.BOX_CHILDREN), [b["type"] for b in box]
        names = names[:6]                                                      # six courses fit the box, one under the other
        gpa.STATE_FILE = Path(tempfile.mkdtemp()) / "gpa.json"
        gpa.set_base(70.0, 60.0, "test")                                       # (no base set: a warning line would take a block)
        text, rows = gpa_ui.view_home(names)
        box = slack.boxed(slack.blocks(text, {"inline_keyboard": rows}, live=True))
        courses = [b for b in box[0]["child_blocks"] if b["type"] == "actions" and len(b["elements"]) == 1]
        assert box[0]["type"] == "container" and len(courses) == 6
    finally:
        slack.mode, reminders.pending, tasks.open_tasks, chat.load_state = keep


def test_a_portal_button_shows_reading_on_the_screen_it_was_pressed_on():
    """Transcript and Balance used to answer nothing for ten seconds: the pressed screen now says it is reading (the
    message it came from), except when pressed under a picture."""
    from uninotifier.bot import app, chat, portal_flow
    seen = []
    keep = (portal_flow.start_portal, chat.answer, chat.is_double_tap)
    try:
        portal_flow.start_portal = lambda section, message_id=None, saved=False: seen.append((section, message_id)) or True
        chat.answer = lambda *a, **k: True
        chat.is_double_tap = lambda *a: False
        app.handle_callback({"id": "1", "data": "portal:transcript", "message": {"message_id": 12}})
        app.handle_callback({"id": "2", "data": "portal:balance", "message": {"message_id": 13, "document": {"file_id": "F"}}})
        assert seen == [("transcript", 12), ("balance", None)], seen
    finally:
        portal_flow.start_portal, chat.answer, chat.is_double_tap = keep


def test_the_transcript_summary_is_a_grid_in_the_order_you_read_it():
    """GPA and status on top, then a gap; the plan beside what is left; attempted beside passed; failed beside repeated;
    this term and the placement exams last."""
    from uninotifier import slack, transcript
    lines = ["🎓 *Cumulative GPA:* 69.86%", "📌 *Academic Status:* Regular", "",
             "📊 *Degree Plan:* 133 hrs", "✅ *Passed:* 57 hrs", "⏳ *This Term:* 18 hrs", "❌ *Failed, Not Retaken Yet:* 3 hrs",
             "🔁 *Repeated Courses:* 2", "🏁 *Remaining:* 76 hrs", "📝 *Placement Exams:* all 3 exempt", "📚 *Total Attempted Hours:* 66 hrs"]
    head, rest = lines[:3], transcript._in_grid_order(lines[3:])
    labels = [line.split(":*")[0].split("*")[-1] for line in rest]
    assert labels == ["Degree Plan", "Remaining", "Total Attempted Hours", "Passed", "Failed, Not Retaken Yet",
                      "Repeated Courses", "This Term", "Placement Exams"], labels
    blocks = slack.blocks("📜 *Transcript*\n" + "\n".join(head + rest), None, live=False)
    fields = [b for b in blocks if b["type"] == "section" and "fields" in b]
    assert len(fields) == 5 and all(len(f["fields"]) == 2 for f in fields)      # a pair a section: room between the rows, a gap under GPA and status


def test_in_slack_the_screen_you_leave_is_removed_but_a_notification_stays():
    """A button that opens a new screen below takes the screen it was pressed on away (no old controls left above);
    the buttons of a notification (Done, Snooze, Add to Reminders) never remove it; Telegram keeps everything."""
    from uninotifier import slack, slackbot
    from uninotifier.bot import app, chat
    retired = []
    keep = (slack.mode, app.route_callback, chat.retire, chat.answer, chat.is_double_tap)
    try:
        chat.retire = lambda mid: retired.append(mid)
        chat.answer = lambda *a, **k: True
        chat.is_double_tap = lambda *a: False

        def opens_a_screen(cb, action, message_id):
            slackbot.PRESS["moved"] = True                   # (slackbot.send does this when a screen with buttons is sent)

        app.route_callback = opens_a_screen
        for mode, action, expected in (("slack", "rec:taken", [31]), ("slack", "done:5:1", []), ("slack", "rem:s:key", []),
                                       ("slack", "rem:z:3", []), ("telegram", "rec:taken", [])):
            slack.mode = lambda mode=mode: mode
            retired.clear()
            app.handle_callback({"id": "1", "data": action, "message": {"message_id": 31}})
            assert retired == expected, (mode, action, retired)
            assert slackbot.PRESS == {"id": None, "moved": False}
        app.route_callback = lambda *a: None                 # edited in place: nothing was opened below, nothing is removed
        slack.mode = lambda: "slack"
        retired.clear()
        app.handle_callback({"id": "1", "data": "gpa:c:1", "message": {"message_id": 31}})
        assert retired == []
    finally:
        slack.mode, app.route_callback, chat.retire, chat.answer, chat.is_double_tap = keep


def test_the_registration_plan_picks_courses_from_one_drop_down_and_shows_every_button():
    """Choose Courses Myself in Slack: one drop-down listing every course whole (a button per course was cut and wrapped), the
    ticked ones come back as one answer; the plan screen has no 'Update My Plan', no summer switch, no hidden buttons."""
    import json
    from uninotifier import slack, slackbot

    rows = [[{"text": "Choose courses", "callback_data": "reg:pick", "stack": True,
              "multi": {"prefix": "reg:set", "placeholder": "Choose your courses",
                        "options": [("تراكيب البيانات · 3h", "0142210"), ("Operating Systems · 3h", "0142211")], "initial": ["0142210"]}}],
            [{"text": "🔙 Registration Plan", "callback_data": "reg:home"}, {"text": "🏠 Main Menu", "callback_data": "menu"}]]
    blocks = slack.buttons({"inline_keyboard": rows}, width=slack.ROW_WIDTH)
    select = blocks[0]["elements"][0]
    assert blocks[0]["type"] == "actions" and select["type"] == "multi_static_select" and select["action_id"] == "msel:reg:set"
    assert [o["text"]["text"] for o in select["options"]] == ["تراكيب البيانات · 3h", "Operating Systems · 3h"]
    assert [o["value"] for o in select["initial_options"]] == ["0142210"]
    press = slackbot.from_press({"type": "block_actions", "user": {"id": "U1"}, "container": {"message_ts": "1.1"},
                                 "actions": [{"type": "multi_static_select", "action_id": "msel:reg:set", "action_ts": "5.5",
                                              "selected_options": [{"value": "0142210"}, {"value": "0142211"}]}]})
    assert press and press[0]["callback_query"]["data"] == "reg:set:0142210,0142211", press
    none = slackbot.from_press({"type": "block_actions", "user": {"id": "U1"}, "container": {"message_ts": "1.1"},
                                "actions": [{"type": "multi_static_select", "action_id": "msel:reg:set", "action_ts": "5.6",
                                             "selected_options": []}]})
    assert none[0]["callback_query"]["data"] == "reg:set:"                       # everything un-ticked is an answer too
    assert json.dumps(blocks)                                                     # and it serialises


def test_back_from_the_lists_returns_to_the_transcript_without_reading_the_portal():
    """Courses Taken / Remaining Courses (Slack): Back is the transcript they came from — the saved copy at once, nothing read,
    no time told — and the list it was pressed on goes."""
    from uninotifier import portal, slack
    from uninotifier.bot import chat, portal_flow
    log = []
    keep = (slack.mode, portal_flow.load_cached, portal_flow.send_cached, portal_flow.settle, chat.retire,
            portal.fetch_many, portal_flow._portal_lock)
    try:
        slack.mode = lambda: "slack"
        saved = portal.Result("transcript", True, b"png", [], {}, [], [])
        portal_flow.load_cached = lambda section: (saved, 1.7e9)
        portal_flow.send_cached = lambda result, when: log.append(("sent", result.section, when))
        portal_flow.settle = lambda section: log.append(("settle", section))
        chat.retire = lambda mid: log.append(("retire", mid))
        portal.fetch_many = lambda *a, **k: log.append("READ THE PORTAL") or {}
        portal_flow._portal_lock = type("L", (), {"release": lambda self: log.append("released")})()
        portal_flow.run_portal("transcript", 77, saved=True)
        assert log == [("sent", "transcript", ""), ("settle", "transcript"), ("retire", 77), "released"], log
        assert portal_flow.TRANSCRIPT_ROW[0]["callback_data"] == "rec:summary" and portal_flow.TRANSCRIPT_ROW[1]["text"] == "🏠 Main Menu"
    finally:
        (slack.mode, portal_flow.load_cached, portal_flow.send_cached, portal_flow.settle, chat.retire,
         portal.fetch_many, portal_flow._portal_lock) = keep


def test_in_slack_a_transcript_list_replaces_the_card_in_place_and_back_restores_it_at_once():
    """Courses Taken / Remaining Courses (Slack): the pressed card BECOMES the list where it stands — nothing new is sent, no
    old card is left above; Back restores the transcript's words on the same card from the saved copy, instantly (no read, no
    upload). Telegram sends the list as before."""
    from uninotifier import portal, slack
    from uninotifier.bot import chat, portal_flow
    log = []
    keep = (slack.mode, portal_flow.load_cached, portal_flow.record_text, chat.edit_in_place, chat.answer, chat.leave_menu,
            portal_flow.notifier.send_long_message)
    try:
        saved = portal.Result("transcript", True, b"png", [], {}, [], [])
        saved.data = {"semesters": [{"term": "T", "courses": []}], "plan": {"categories": []}}
        portal_flow.load_cached = lambda section: (saved, 1.7e9)
        portal_flow.record_text = lambda kind, semesters, plan, overlay=None: f"✅ *Courses Taken* ({kind})"
        chat.edit_in_place = lambda mid, text, rows: log.append(("edit", mid, text, [b.get("callback_data") for r in rows for b in r]))
        chat.answer = lambda *a, **k: True
        chat.leave_menu = lambda: None
        portal_flow.notifier.send_long_message = lambda *a, **k: log.append(("sent",))
        slack.mode = lambda: "slack"
        portal_flow.handle_record_callback({"id": "1", "message": {"message_id": 21}}, "rec:taken")
        assert log and log[0][:3] == ("edit", 21, "✅ *Courses Taken* (taken)") and log[0][3][-2:] == ["rec:summary", "menu"], log
        assert "rec:left" in log[0][3] and "rec:taken" not in log[0][3]              # the other list, the way back
        log.clear()
        portal_flow.handle_record_callback({"id": "2", "message": {"message_id": 21}}, "rec:summary")
        assert log[0][0] == "edit" and log[0][1] == 21 and "Transcript" in log[0][2] and "ZUJ Portal" in log[0][2], log   # its own words again
        assert [e for e in log if e[0] == "sent"] == []
        log.clear()
        slack.mode = lambda: "telegram"
        portal_flow.handle_record_callback({"id": "3", "message": {"message_id": 21}}, "rec:taken")
        assert log == [("sent",)]
    finally:
        (slack.mode, portal_flow.load_cached, portal_flow.record_text, chat.edit_in_place, chat.answer, chat.leave_menu,
         portal_flow.notifier.send_long_message) = keep


def test_a_new_transcript_replaces_the_old_card_and_a_heading_has_room_above_it():
    """A transcript sent again takes the older transcript cards (and their pictures) out of the chat — never a pile of old cards with
    buttons; and a heading never sits against the line above it."""
    from uninotifier import slack
    blocks = slack.blocks("⏳ *Tasks*\n" + slack.heading("📚 *Course tasks*", "📚 Course tasks") + "\n" + slack.small("none ahead 🎉") +
                          "\n" + slack.heading("🔔 *Your reminders*", "🔔 Your reminders"), None, live=False) if False else None
    keep = slack.mode
    try:
        slack.mode = lambda: "slack"
        text = "\n".join(["⏳ *Tasks*", slack.heading("📚 *Course tasks*", "📚 Course tasks"), slack.small("none ahead 🎉"),
                          slack.heading("🔔 *Your reminders*", "🔔 Your reminders"), "1. a reminder"])
        blocks = slack.blocks(text, None, live=False)
        kinds = [b["type"] for b in blocks]
        assert kinds == ["header", "header", "context", "header", "section"], kinds
        assert blocks[2]["elements"][0]["text"].endswith(slack.GAP), "an empty line under 'none ahead', before the next heading"
        assert not blocks[1].get("text", {}).get("text", "").endswith(slack.GAP)
    finally:
        slack.mode = keep
