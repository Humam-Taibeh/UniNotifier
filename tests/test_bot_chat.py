"""The chat: menus, buttons, screens and how messages are worded."""

from support import (_as_owner, _fresh_failsafe, _legacy_markdown_ok, OUTBOX, _package, _tg)

import importlib
import json
import os
import tempfile
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
    assert len(text.splitlines()) == 2 and text.startswith("🎓 *UniNotifier System* • First Semester 2026/2027")
    assert text.splitlines()[1].endswith("• 6 Courses Tracked"), text
    # My Courses & Schedule is a menu, not a wall of text; Tracked Items and Campus Guide are gone
    text, rows = bot.view_courses()
    assert [[b["text"] for b in row] for row in rows] == [
        ["📋 Course List & Details"], ["📅 Weekly Timetable"], ["🔙 Main Menu"]]
    assert [rows[0][0]["callback_data"], rows[1][0]["callback_data"]] == ["courselist", "schedule"]
    assert "6 courses · 18 credit hours" in text and len(text.splitlines()) <= 5
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
