"""Slack: the bot living in Slack — its screens and buttons, the Home page, the private guest form, the timetable
shown from Slack's own copy, and ⚙️ Settings as a window."""

from support import (_as_owner, _package)

import importlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

from uninotifier import access
from uninotifier import notifier
from uninotifier import paths
from uninotifier import telegram


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
        if method in ("chat.delete", "files.delete"):            # Slack is never asked while the message file is locked
            assert not slackbot.STORE.with_name(slackbot.STORE.name + ".lock").exists(), method
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

                # a file sent again retires older copies only when it IS one living copy (a portal answer, the
                # timetable). This week's attendance proof has last week's name, two courses each have a
                # "Syllabus.pdf": different messages, all kept.
                for name, retired in (("Big Data - attendance.png", False), ("Syllabus.pdf", False),
                                      ("ZUJ Portal - Transcript.png", True), ("Weekly_Schedule.png", True)):
                    first = notifier.upload("T", "sendDocument", {"chat_id": OWNER, "caption": "a"}, "document", name, b"x")
                    calls.clear()
                    notifier.upload("T", "sendDocument", {"chat_id": OWNER, "caption": "b"}, "document", name, b"y")
                    assert ("chat.delete" in [m for m, _ in calls]) is retired, (name, calls)
                    assert (slackbot.entry(first["message_id"]) is None) is retired, name

                # a message Slack took is never reported unsent (and so never sent twice) when numbering it fails
                keep_remember = slackbot.remember

                def busy(*a, **k):
                    raise TimeoutError("slack_messages.json.lock is held by another process")
                slackbot.remember = busy
                try:
                    calls.clear()
                    assert notifier.send_message("T", OWNER, "📢 *Sent once*")
                    assert [m for m, _ in calls].count("chat.postMessage") == 1, calls
                finally:
                    slackbot.remember = keep_remember

            _with_env(on, scenario)
            # without the live connection's token, the bot stays on Telegram (messages may still go to Slack)
            assert _with_env({**on, "SLACK_APP_TOKEN": ""}, slackbot.interactive) is False
        finally:
            slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, slack.SHARE_WAIT = keep
            bot.CHAT_ID, access.ADMIN_USER_ID = owner

def test_a_guest_login_in_slack_is_typed_in_a_private_form():
    """Slack never lets an app delete a message you typed, so on Slack a guest's portal login is never typed into
    the chat: /guest shows 🔑 Enter Their Login, the press opens a private form (a Slack modal) at once, and what is
    sent from it goes straight to the sign-in, in memory — never into data/slack_messages.json, a log, or the chat.
    Only the owner can use it; what anyone types to the app is numbered without its words."""
    from support import OWNER_ID as OWNER, _with_env
    from uninotifier import slack, slackbot
    guest_flow = importlib.import_module("uninotifier.bot.guest_flow")
    bot = _package("uninotifier.bot")
    calls, stamps, started = [], iter(f"17100000{n:02d}.0001" for n in range(1, 99)), []

    class Answer:
        status_code, ok, headers = 200, True, {}

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, data=None, timeout=None, headers=None):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, data))
        return Answer({"ok": True, "ts": next(stamps)} if method == "chat.postMessage" else {"ok": True})

    def no_telegram(*a, **k):
        raise AssertionError("Telegram must not be used while the bot lives in Slack")

    keep = (slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, guest_flow.run_guest,
            guest_flow.threading.Thread)
    owner = _as_owner(bot)
    on = {"NOTIFY_VIA": "slack", "SLACK_BOT_TOKEN": "-".join(("xoxb", "1", "2", "made-up")), "SLACK_CHANNEL": "D0123ABCD",
          "SLACK_APP_TOKEN": "-".join(("xapp", "1", "made-up")), "SLACK_USER": "U0OWNER1", "TELEGRAM_CHAT_ID": OWNER}

    class Inline:                                           # the sign-in thread, run at once (and recorded)
        def __init__(self, target=None, args=(), **k):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    with tempfile.TemporaryDirectory() as tmp:
        try:
            slack._post, telegram.request, slack.MIN_INTERVAL = fake_post, no_telegram, 0
            slackbot.STORE, slackbot._connect = Path(tmp) / "slack_messages.json", lambda: None
            guest_flow.run_guest = lambda user, password: started.append((user, password))
            guest_flow.threading.Thread = Inline

            def scenario():
                secret = "Gu3st-S3cret!"
                guest_flow._guest.clear()
                guest_flow.start_guest()
                card = next(d for m, d in calls if m == "chat.postMessage")
                assert "Enter Their Login" in card["blocks"] and "Send their *student ID*" not in card["text"]
                assert guest_flow._guest["step"] == "form"
                # a typed message never asks for the password, and its words are not kept
                slackbot.take("events_api", {"event": {"type": "message", "channel_type": "im", "user": "U0OWNER1",
                                                       "text": "123456", "ts": "1710000099.1"}})
                typed = slackbot.get_updates(None, 0)[0]["message"]
                assert typed["text"] == "123456" and slackbot.entry(typed["message_id"])["text"] == "", \
                    "numbered, but the words are not written to disk"
                # the press opens the form at once (not through the bot's queue: Slack's trigger lasts 3 s)
                calls.clear()
                slackbot.take("interactive", {"type": "block_actions", "user": {"id": "U0OWNER1"}, "trigger_id": "T-1",
                                              "container": {"message_ts": "1710000001.0001"},
                                              "actions": [{"action_id": "b0_0", "value": "guest:form", "action_ts": "2.1"}]})
                opened = [d for m, d in calls if m == "views.open"]
                assert opened and opened[0]["trigger_id"] == "T-1" and slackbot.get_updates(None, 0) == []
                form = json.loads(opened[0]["view"])
                assert form["type"] == "modal" and form["callback_id"] == slackbot.GUEST_VIEW
                assert [b["block_id"] for b in form["blocks"] if b["type"] == "input"] == ["user", "password"]
                # someone else in the workspace: no form, and their press is a stranger's
                calls.clear()
                slackbot.take("interactive", {"type": "block_actions", "user": {"id": "U0STRANGER"}, "trigger_id": "T-2",
                                              "container": {"message_ts": "1710000001.0001"},
                                              "actions": [{"action_id": "b0_0", "value": "guest:form", "action_ts": "2.2"}]})
                assert not [m for m, _ in calls if m == "views.open"]
                assert not access.is_owner(slackbot.get_updates(None, 0)[0])
                # a stranger's form is turned away; the owner's goes straight to the sign-in
                submitted = {"type": "view_submission", "view": {"callback_id": slackbot.GUEST_VIEW, "state": {"values": {
                    "user": {"user": {"value": " 209900002 "}}, "password": {"password": {"value": secret}}}}}}
                slackbot.take("interactive", {**submitted, "user": {"id": "U0STRANGER"}})
                assert started == []
                slackbot.take("interactive", {**submitted, "user": {"id": "U0OWNER1"}})
                assert started == [("209900002", secret)] and guest_flow._guest["step"] == "running"
                assert slackbot.get_updates(None, 0) == [], "the login never joins the chat's updates"
                stored = slackbot.STORE.read_text(encoding="utf-8")
                assert secret not in stored and "209900002" not in stored, "nothing of the login on disk"
                assert not any(secret in json.dumps(d, ensure_ascii=False) for _, d in calls), "... nor sent to the chat"
                # a form sent after the look-up ended is not used
                guest_flow._guest.clear()
                started.clear()
                slackbot.take("interactive", {**submitted, "user": {"id": "U0OWNER1"}})
                assert started == []

            _with_env(on, scenario)
        finally:
            (slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, guest_flow.run_guest,
             guest_flow.threading.Thread) = keep
            bot.CHAT_ID, access.ADMIN_USER_ID = owner
            guest_flow._guest.clear()
            if guest_flow.portal_flow._portal_lock.locked():
                guest_flow.portal_flow._portal_lock.release()


def test_the_timetable_is_uploaded_once_then_shown_from_slacks_own_copy():
    """Every 📅 Weekly Timetable press once uploaded both files again (about 1.5 MB), waited for Slack to share them and
    then sent the menu: seconds per press. Now the first press uploads and remembers Slack's file ids; every later press
    is ONE message — the picture from Slack's copy and a link to the PDF — and the previous quick copy goes. A changed
    timetable is uploaded again (the old files go); a copy Slack refuses to show (deleted by hand) is uploaded afresh."""
    from support import OWNER_ID as OWNER, _with_env
    from uninotifier import slack, slackbot, slackfiles
    views = importlib.import_module("uninotifier.bot.views")
    bot = _package("uninotifier.bot")
    calls, stamps, ids = [], iter(f"17200000{n:02d}.0001" for n in range(1, 99)), iter(f"F{n:03d}" for n in range(1, 99))
    refuse = []

    class Answer:
        status_code, ok, headers = 200, True, {}

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    uploaded = {}

    def fake_post(url, data=None, timeout=None, headers=None):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, data))
        if method == "files.getUploadURLExternal":
            file_id = next(ids)
            uploaded[file_id] = data["filename"]
            return Answer({"ok": True, "upload_url": f"https://files.slack.com/upload/v1/{file_id}", "file_id": file_id})
        if method == "completeUploadExternal" or method == "files.completeUploadExternal":
            shared = json.loads(data["files"])
            fake_post.last = [f["id"] for f in shared]
            return Answer({"ok": True, "files": shared})
        if method == "conversations.history":
            return Answer({"ok": True, "messages": [{"ts": "1720000099.0001", "files": [
                {"id": i, "permalink": f"https://myuni.slack.com/files/UBOT/{i}/{uploaded[i]}"} for i in fake_post.last]}]})
        if method == "chat.postMessage":
            if refuse and "slack_file" in (data.get("blocks") or ""):
                return Answer({"ok": False, "error": refuse.pop(0)})
            return Answer({"ok": True, "ts": next(stamps)})
        return Answer({"ok": True})
    fake_post.last = []

    def no_telegram(*a, **k):
        raise AssertionError("Telegram must not be used while the bot lives in Slack")

    keep = (slack._post, telegram.request, slack.MIN_INTERVAL, slack.SHARE_WAIT, slackbot.STORE, slackbot._connect,
            slackfiles.STATE_FILE, views.SCHEDULE_PDF, views.SCHEDULE_IMAGE)
    owner = _as_owner(bot)
    on = {"NOTIFY_VIA": "slack", "SLACK_BOT_TOKEN": "-".join(("xoxb", "1", "2", "made-up")), "SLACK_CHANNEL": "D0123ABCD",
          "SLACK_APP_TOKEN": "-".join(("xapp", "1", "made-up")), "SLACK_USER": "U0OWNER1", "TELEGRAM_CHAT_ID": OWNER}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pdf, png = tmp / "Weekly_Academic_Schedule.pdf", tmp / "Weekly_Schedule.png"
        pdf.write_bytes(b"%PDF-1.7 timetable")
        png.write_bytes(b"\x89PNG timetable")
        try:
            slack._post, telegram.request, slack.MIN_INTERVAL, slack.SHARE_WAIT = fake_post, no_telegram, 0, 1
            slackbot.STORE, slackbot._connect = tmp / "slack_messages.json", lambda: None
            slackfiles.STATE_FILE, views.SCHEDULE_PDF, views.SCHEDULE_IMAGE = tmp / "slack_files.json", pdf, png

            def press():
                calls.clear()
                views.send_schedule_files()
                return [m for m, _ in calls]

            def scenario():
                first = press()
                assert first.count("files.getUploadURLExternal") == 2 and "files.completeUploadExternal" in first, first
                saved = json.loads(slackfiles.STATE_FILE.read_text(encoding="utf-8"))["timetable"]
                assert (saved["pdf_id"], saved["png_id"], saved["upload_ts"]) == ("F001", "F002", "1720000099.0001")
                assert saved["pdf_link"].endswith("/F001/Weekly_Academic_Schedule.pdf")
                # the second press: no upload, no share wait — one message from Slack's copy (then the menu)
                second = press()
                assert "files.getUploadURLExternal" not in second and "conversations.history" not in second, second
                quick = next(d for m, d in calls if m == "chat.postMessage" and "slack_file" in (d.get("blocks") or ""))
                blocks = json.loads(quick["blocks"])
                assert blocks[0] == {"type": "image", "slack_file": {"id": "F002"}, "alt_text": "Weekly timetable"}
                assert saved["pdf_link"] in blocks[1]["elements"][0]["text"]
                quick_ts = json.loads(slackfiles.STATE_FILE.read_text(encoding="utf-8"))["timetable"]["quick_ts"]
                # the third: the previous quick copy goes, the upload stays
                press()
                deleted = [d["ts"] for m, d in calls if m == "chat.delete"]
                assert quick_ts in deleted and "1720000099.0001" not in deleted, deleted
                # a new timetable: uploaded again, the old files and their message removed
                png.write_bytes(b"\x89PNG a new timetable")
                fourth = press()
                assert fourth.count("files.getUploadURLExternal") == 2, fourth
                assert {d["file"] for m, d in calls if m == "files.delete"} == {"F001", "F002"}
                assert "1720000099.0001" in [d["ts"] for m, d in calls if m == "chat.delete"]
                # a copy Slack will not show again (a file deleted by hand): uploaded afresh, never lost
                refuse.append("invalid_blocks")
                fifth = press()
                assert fifth.count("files.getUploadURLExternal") == 2, fifth
                assert json.loads(slackfiles.STATE_FILE.read_text(encoding="utf-8"))["timetable"]["png_id"] == "F006"

            _with_env(on, scenario)
        finally:
            (slack._post, telegram.request, slack.MIN_INTERVAL, slack.SHARE_WAIT, slackbot.STORE, slackbot._connect,
             slackfiles.STATE_FILE, views.SCHEDULE_PDF, views.SCHEDULE_IMAGE) = keep
            bot.CHAT_ID, access.ADMIN_USER_ID = owner


def test_settings_switch_what_the_bot_does_by_itself():
    """⚙️ Settings (main menu, `settings`): Auto Check-In and Auto Mark as Done, each on or off with one
    tap — written to .env (read back, nothing else in it touched) and seen by the bot at once. A value in .env wins
    over a schedule.json's "auto_check_in". The screen ends with Back + Main Menu; 🆕 Check for Updates is there too."""
    import shutil
    from uninotifier import exams_watch, schedule, setup
    settings_flow = importlib.import_module("uninotifier.bot.settings_flow")
    bot = _package("uninotifier.bot")
    assert any(b.get("callback_data") == "settings" for row in bot.view_menu()[1] for b in row), "⚙️ Settings on the menu"
    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-settings-"))
    env = tmp / ".env"
    env.write_text("ZUJ_USERNAME=a@b.c\n# my own note\nAUTO_MARK_DONE=\nAUTO_CHECK_IN=\n", encoding="utf-8")
    keep = (paths.ENV_FILE, setup.restrict, exams_watch.STATE_FILE, schedule.load_schedule, bot.edit_view, bot.api)
    keep_env = {k: os.environ.get(k) for k in ("AUTO_MARK_DONE", "AUTO_CHECK_IN")}
    screens = []
    try:
        paths.ENV_FILE, setup.restrict = env, (lambda path: True)
        exams_watch.STATE_FILE = tmp / "exams_watch.json"
        schedule.load_schedule = lambda: {"auto_check_in": True}           # a schedule.json of the owner's that says on
        for k in keep_env:
            os.environ.pop(k, None)
        text, rows = settings_flow.view_settings()
        assert text.startswith("⚙️ *Settings*") and rows[-1] == bot.BACK_ROW, rows
        assert [b["callback_data"] for row in rows[:-1] for b in row] == [
            "set:AUTO_CHECK_IN", "set:AUTO_MARK_DONE", "set:update"]
        assert "Auto Check-In — *🟢 On*" in text and "Auto Mark as Done — *⚪ Off*" in text, text
        bot.edit_view = lambda message_id, text, rows: screens.append((message_id, text, rows))
        bot.api = lambda method, **data: True
        bot.handle_callback({"id": "s1", "data": "set:AUTO_MARK_DONE", "message": {"message_id": 7}})
        assert "AUTO_MARK_DONE=true" in env.read_text(encoding="utf-8") and os.environ["AUTO_MARK_DONE"] == "true"
        assert "# my own note" in env.read_text(encoding="utf-8"), "nothing else in .env is touched"
        assert screens[-1][0] == 7 and "Auto Mark as Done — *🟢 On*" in screens[-1][1], "redrawn in place"
        # off from the bot although schedule.json says on: .env wins, at once
        bot.handle_callback({"id": "s2", "data": "set:AUTO_CHECK_IN", "message": {"message_id": 7}})
        assert "AUTO_CHECK_IN=false" in env.read_text(encoding="utf-8") and schedule.auto_check_in() is False
        assert "Auto Check-In — *⚪ Off*" in screens[-1][1]
        os.environ["AUTO_CHECK_IN"] = ""
        assert schedule.auto_check_in() is True, "empty in .env: the schedule.json's choice"
        assert schedule.auto_check_in({}) is False, "neither says: off"
        os.environ["AUTO_CHECK_IN"] = "true"
        assert schedule.auto_check_in({"auto_check_in": False}) is True
        # Midterm Watch is switched on its own screen, not here
        assert "Midterm" not in settings_flow.view_settings()[0] and exams_watch.enabled()
        assert settings_flow.switch("MIDTERMS", False).startswith("⚠️") and exams_watch.enabled()
        assert settings_flow.switch("SOMETHING_ELSE", True).startswith("⚠️"), "only the listed settings are written"
    finally:
        paths.ENV_FILE, setup.restrict, exams_watch.STATE_FILE, schedule.load_schedule, bot.edit_view, bot.api = keep
        for k, v in keep_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_open_as_a_window_in_slack():
    """In Slack ⚙️ Settings opens as a window over the chat (a modal): a tick box per automatic action, with what it
    does, ticked as it is now; Save applies it at once (and a quiet "Settings saved" only you see), Cancel changes
    nothing; a problem is said inside the window under the boxes. 🆕 Check for Updates opens a second window that says
    it is looking, then what it found, with ⬇️ Update Now / ⏰ Later. Nobody else can open or save it."""
    import shutil
    from support import OWNER_ID as OWNER, _with_env
    from uninotifier import schedule, setup, slack, slackbot, updates
    update_flow = importlib.import_module("uninotifier.bot.update_flow")
    bot = _package("uninotifier.bot")
    calls = []

    class Answer:
        status_code, ok, headers = 200, True, {}

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, data=None, timeout=None, headers=None):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, data))
        return Answer({"ok": True, "view": {"id": "V-UPDATES"}} if method == "views.push" else {"ok": True, "ts": "1.1"})

    def windows(method):
        return [json.loads(d["view"]) for m, d in calls if m == method]

    def no_telegram(*a, **k):
        raise AssertionError("Telegram must not be used while the bot lives in Slack")

    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-window-"))
    env = tmp / ".env"
    env.write_text("AUTO_MARK_DONE=\nAUTO_CHECK_IN=\n", encoding="utf-8")
    keep = (slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, paths.ENV_FILE,
            setup.restrict, schedule.load_schedule, update_flow.view_status, update_flow.start, updates.is_clone)
    keep_env = {k: os.environ.get(k) for k in ("AUTO_MARK_DONE", "AUTO_CHECK_IN", "PORTAL_USERNAME", "PORTAL_PASSWORD")}
    owner = _as_owner(bot)
    on = {"NOTIFY_VIA": "slack", "SLACK_BOT_TOKEN": "-".join(("xoxb", "1", "2", "made-up")), "SLACK_CHANNEL": "D0123ABCD",
          "SLACK_APP_TOKEN": "-".join(("xapp", "1", "made-up")), "SLACK_USER": "U0OWNER1", "TELEGRAM_CHAT_ID": OWNER}
    try:
        slack._post, telegram.request, slack.MIN_INTERVAL = fake_post, no_telegram, 0
        slackbot.STORE, slackbot._connect = tmp / "slack_messages.json", lambda: None
        paths.ENV_FILE, setup.restrict = env, (lambda path: True)
        schedule.load_schedule = lambda: {"auto_check_in": True}             # check-in on (from schedule.json)
        updates.is_clone = lambda root=None: False
        for k in keep_env:
            os.environ.pop(k, None)

        def submit(user, ticked):
            return slackbot.respond("interactive", {"type": "view_submission", "user": {"id": user}, "view": {
                "callback_id": "settings", "state": {"values": {"auto": {"auto": {
                    "selected_options": [{"value": v} for v in ticked]}}}}}})

        def press(value, view=None, trigger="T-1"):
            calls.clear()
            payload = {"type": "block_actions", "user": {"id": "U0OWNER1"}, "trigger_id": trigger,
                       "container": {"type": "view" if view else "message", "message_ts": "1.1"},
                       "actions": [{"action_id": "b0_0", "value": value, "action_ts": "3.1"}]}
            if view:
                payload["view"] = view
            slackbot.take("interactive", payload)

        def scenario():
            # ⚙️ Settings opens the window at once — not a screen in the chat
            press("settings")
            window = windows("views.open")[0]
            assert window["type"] == "modal" and window["callback_id"] == "settings" and slackbot.get_updates(None, 0) == []
            ticks = next(b for b in window["blocks"] if b.get("block_id") == "auto")["element"]
            assert [o["value"] for o in ticks["options"]] == ["AUTO_CHECK_IN", "AUTO_MARK_DONE"]
            assert [o["value"] for o in ticks.get("initial_options", [])] == ["AUTO_CHECK_IN"], "ticked as it is now"
            assert all(len(o["description"]["text"]) <= 75 for o in ticks["options"]), "Slack shows 75 characters"
            assert (window["submit"]["text"], window["close"]["text"]) == ("Save", "Cancel")
            # Save: Mark as Done on, Check-In off — applied at once; a quiet confirmation only you see
            assert submit("U0OWNER1", ["AUTO_MARK_DONE"]) == {}
            saved = env.read_text(encoding="utf-8")
            assert "AUTO_MARK_DONE=true" in saved and "AUTO_CHECK_IN=false" in saved and schedule.auto_check_in() is False
            for thread in [t for t in threading.enumerate() if t.name == "settings-saved"]:
                thread.join(5)
            toast = [d for m, d in calls if m == "chat.postEphemeral"][-1]
            assert toast["user"] == "U0OWNER1" and "Settings saved" in toast["text"], toast
            # Check-In needs the portal login: said inside the window, nothing written
            answer = submit("U0OWNER1", ["AUTO_CHECK_IN", "AUTO_MARK_DONE"])
            assert answer["response_action"] == "errors" and "portal login" in answer["errors"]["auto"]
            assert env.read_text(encoding="utf-8") == saved
            # someone else: the window closes, nothing changes
            assert submit("U0STRANGER", []) == {} and env.read_text(encoding="utf-8") == saved
            assert slackbot.respond("interactive", {"type": "block_actions"}) is None, "an ordinary press: take() reads it"
            # 🆕 Check for Updates: a second window, "looking…" at once, then what was found
            update_flow.view_status = lambda: (updates.message({"news": [], "subjects": ["feat: a faster timetable"],
                                                                "behind": 1, "sha": "a" * 40}),
                                               updates.ROWS + [bot.BACK_ROW])
            press("set:update", {"type": "modal", "id": "V-SETTINGS"})
            assert "Looking" in json.dumps(windows("views.push")[0], ensure_ascii=False)
            for thread in [t for t in threading.enumerate() if t.name == "settings-updates"]:
                thread.join(5)
            shown = [d for m, d in calls if m == "views.update"][-1]
            found = json.loads(shown["view"])
            buttons = [e["value"] for b in found["blocks"] if b["type"] == "actions" for e in b["elements"] if e.get("value")]
            assert shown["view_id"] == "V-UPDATES" and buttons == ["update:now", "update:later"], buttons
            assert "a faster timetable" in json.dumps(found, ensure_ascii=False)
            # ⬇️ Update Now / ⏰ Later in that window
            started = []
            update_flow.start = lambda message_id=None: started.append(1) or True
            press("update:now", {"type": "modal", "id": "V-UPDATES"})
            assert started and "Updating" in [d for m, d in calls if m == "views.update"][-1]["view"]
            press("update:later", {"type": "modal", "id": "V-UPDATES"})
            assert "postponed" in [d for m, d in calls if m == "views.update"][-1]["view"]
            # a stranger's ⚙️ press opens nothing
            calls.clear()
            slackbot.take("interactive", {"type": "block_actions", "user": {"id": "U0STRANGER"}, "trigger_id": "T-9",
                                          "container": {"message_ts": "1.1"},
                                          "actions": [{"action_id": "b0_0", "value": "settings", "action_ts": "3.9"}]})
            assert not windows("views.open") and not access.is_owner(slackbot.get_updates(None, 0)[0])

        _with_env(on, scenario)
    finally:
        (slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, paths.ENV_FILE,
         setup.restrict, schedule.load_schedule, update_flow.view_status, update_flow.start, updates.is_clone) = keep
        for k, v in keep_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        bot.CHAT_ID, access.ADMIN_USER_ID = owner
        shutil.rmtree(tmp, ignore_errors=True)


def _slack_windows_harness(bot, tmp):
    """A fake Slack for the form windows: what was opened, updated and said, and the helpers to press and save."""
    from uninotifier import slackbot
    calls = []

    class Answer:
        status_code, ok, headers = 200, True, {}

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, data=None, timeout=None, headers=None):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, data))
        return Answer({"ok": True, "ts": "1.1", "view": {"id": "V-OPEN"}})

    def press(value, view=None, trigger="T-1", user="U0OWNER1", ts="1700000001.0001"):
        calls.clear()
        while not slackbot._events.empty():               # what an earlier test left in the queue is not this press's
            slackbot._events.get_nowait()
        payload = {"type": "block_actions", "user": {"id": user}, "trigger_id": trigger,
                   "container": {"type": "message", "message_ts": ts},
                   "actions": [{"action_id": "b0_0", "value": value, "action_ts": "3.1"}]}
        if view:
            payload["view"] = view
        slackbot.take("interactive", payload)

    def opened():
        return [json.loads(d["view"]) for m, d in calls if m == "views.open"]

    def save(window, fields, user="U0OWNER1"):
        """Fill the window's fields as Slack would send them, and send Save: Slack's answer."""
        state = {}
        for block in window["blocks"]:
            if block["type"] != "input":
                continue
            kind, name = block["element"]["type"], block["block_id"]
            given = fields.get(name)
            if kind == "plain_text_input":
                state[name] = {name: {"type": kind, "value": given if name in fields else block["element"].get("initial_value")}}
            elif kind == "datepicker":
                state[name] = {name: {"type": kind, "selected_date": given or block["element"].get("initial_date")}}
            elif kind == "timepicker":
                state[name] = {name: {"type": kind, "selected_time": given or block["element"].get("initial_time")}}
            elif kind == "static_select":
                chosen = given or (block["element"].get("initial_option") or {}).get("value")
                state[name] = {name: {"type": kind, "selected_option": {"value": chosen} if chosen else None}}
            elif kind == "checkboxes":
                ticked = given if name in fields else [o["value"] for o in block["element"].get("initial_options", [])]
                state[name] = {name: {"type": kind, "selected_options": [{"value": v} for v in ticked]}}
        view = {"callback_id": window["callback_id"], "private_metadata": window["private_metadata"],
                "state": {"values": state}}
        answer = slackbot.respond("interactive", {"type": "view_submission", "user": {"id": user}, "view": view})
        for thread in [t for t in threading.enumerate() if t.name == "window-saved"]:
            thread.join(5)
        return answer

    def toasts():
        return [d["text"] for m, d in calls if m == "chat.postEphemeral"]

    return calls, press, opened, save, toasts, fake_post


def test_reminders_are_set_in_a_slack_window_with_a_date_and_time_picker():
    """➕ New Reminder, 📌 Add to Reminders and a reminder's Edit buttons open ONE window in Slack: the note, a date
    picker, a time picker and quick picks that fill them in (keeping what you typed). A time that has passed is said under
    the time; a saved reminder is confirmed to you alone. An edit opens with the reminder's own text, day and time.
    Typing `remind` offers the form on a button. Nobody else can save one."""
    from datetime import datetime, timedelta
    from support import OWNER_ID as OWNER, _with_env
    from uninotifier import reminders, slack, slackbot
    reminder_flow = importlib.import_module("uninotifier.bot.reminder_flow")
    bot = _package("uninotifier.bot")
    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-reminder-window-"))
    calls, press, opened, save, toasts, fake_post = _slack_windows_harness(bot, tmp)

    def no_telegram(*a, **k):
        raise AssertionError("Telegram must not be used while the bot lives in Slack")

    keep = (slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, reminders.STATE_FILE,
            reminders.SOURCES_FILE, reminder_flow.with_words, bot.send_view, bot.edit_view)
    owner = _as_owner(bot)
    on = {"NOTIFY_VIA": "slack", "SLACK_BOT_TOKEN": "-".join(("xoxb", "1", "2", "made-up")), "SLACK_CHANNEL": "D0123ABCD",
          "SLACK_APP_TOKEN": "-".join(("xapp", "1", "made-up")), "SLACK_USER": "U0OWNER1", "TELEGRAM_CHAT_ID": OWNER}
    try:
        slack._post, telegram.request, slack.MIN_INTERVAL = fake_post, no_telegram, 0
        slackbot.STORE, slackbot._connect = tmp / "slack_messages.json", lambda: None
        reminders.STATE_FILE, reminders.SOURCES_FILE = tmp / "reminders.json", tmp / "sources.json"
        reminder_flow.with_words = lambda note, url: note + "\n(the post's own words)"
        sent_views, edited = [], []
        bot.send_view = lambda text, rows: sent_views.append((text, rows)) or {"message_id": 1}
        bot.edit_view = lambda message_id, text, rows: edited.append((message_id, text))

        def scenario():
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            # ➕ opens the window at once — not a chat screen
            press("rem:new")
            window = opened()[0]
            assert window["type"] == "modal" and window["callback_id"] == "reminder" and slackbot.get_updates(None, 0) == []
            kinds = {b["block_id"]: b["element"]["type"] for b in window["blocks"] if b["type"] == "input"}
            assert kinds == {"note": "plain_text_input", "day": "datepicker", "time": "timepicker"}, kinds
            quick = next(b for b in window["blocks"] if b.get("block_id") == "quick")["elements"]
            assert {"rem:q:hour", "rem:q:morning"} <= {e["value"] for e in quick} and all(len(e["text"]["text"]) <= 75 for e in quick)
            # a quick pick fills the day and time in and keeps what was typed
            redrawn = None
            slack_view = {"type": "modal", "id": "V-1", "callback_id": "reminder", "private_metadata": window["private_metadata"],
                          "state": {"values": {"note": {"note": {"type": "plain_text_input", "value": "Buy the textbook"}}}}}
            press("rem:q:morning", slack_view)
            redrawn = json.loads([d for m, d in calls if m == "views.update"][-1]["view"])
            assert [d["view_id"] for m, d in calls if m == "views.update"] == ["V-1"]
            fields = {b["block_id"]: b["element"] for b in redrawn["blocks"] if b["type"] == "input"}
            assert fields["note"]["initial_value"] == "Buy the textbook", "what was typed stays"
            assert (fields["day"]["initial_date"], fields["time"]["initial_time"]) == (tomorrow, "08:00")
            # a time that has passed is said under the time, nothing saved
            answer = save(window, {"note": "Buy the textbook", "day": "2020-01-01", "time": "09:00"})
            assert answer["response_action"] == "errors" and "already passed" in answer["errors"]["time"]
            assert reminders.pending() == []
            # saved: the reminder is there, confirmed to you alone
            calls.clear()
            assert save(window, {"note": "Buy the textbook", "day": tomorrow, "time": "18:30"}) == {}
            [item] = reminders.pending()
            assert item["note"] == "Buy the textbook" and datetime.fromtimestamp(item["due"]).strftime("%Y-%m-%d %H:%M") == f"{tomorrow} 18:30"
            assert "Reminder set" in toasts()[-1] and [d["user"] for m, d in calls if m == "chat.postEphemeral"] == ["U0OWNER1"]
            # an empty note is refused under the note
            assert save(window, {"note": "  ", "day": tomorrow, "time": "18:30"})["errors"]["note"]
            # ✏️ Change Time of that reminder: the window has its own text, day and time; Save changes it
            press(f"rem:eh:{item['id']}")
            edit = opened()[0]
            assert edit["title"]["text"] == "✏️ Edit Reminder" and json.loads(edit["private_metadata"])["edit"] == item["id"]
            fields = {b["block_id"]: b["element"] for b in edit["blocks"] if b["type"] == "input"}
            assert fields["note"]["initial_value"] == "Buy the textbook" and fields["time"]["initial_time"] == "18:30"
            assert save(edit, {"time": "20:15"}) == {}
            assert datetime.fromtimestamp(reminders.get(item["id"])["due"]).strftime("%H:%M") == "20:15"
            assert "Reminder changed" in toasts()[-1] and reminders.get(item["id"])["note"] == "Buy the textbook"
            # 📌 Add to Reminders under a notification: its words in the window; saved unchanged it keeps the post's own words
            reminders.remember_source("k1", "Announcement: bring your ID", "Big Data", "https://x.example/p")
            press("rem:s:k1")
            pinned = opened()[0]
            assert pinned["title"]["text"] == "📌 Add to Reminders"
            assert json.loads(pinned["private_metadata"])["course"] == "Big Data"
            assert save(pinned, {"day": tomorrow, "time": "07:00"}) == {}
            added = next(r for r in reminders.pending() if r["note"].startswith("Announcement"))
            assert added["note"].endswith("(the post's own words)") and added["course"] == "Big Data" and added["url"] == "https://x.example/p"
            # a pin too old to be found: the ordinary press answers it, no window
            press("rem:s:gone")
            assert opened() == []
            # someone else cannot save one
            before = len(reminders.pending())
            assert save(window, {"note": "x", "day": tomorrow, "time": "09:00"}, user="U0STRANGER") == {}
            assert len(reminders.pending()) == before
            # `remind` typed: the form is offered on a button
            reminder_flow.start_reminder()
            text, rows = sent_views[-1]
            assert rows[0][0]["callback_data"] == "rem:new" and reminder_flow._reminder_wait is None

        _with_env(on, scenario)
    finally:
        (slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, reminders.STATE_FILE,
         reminders.SOURCES_FILE, reminder_flow.with_words, bot.send_view, bot.edit_view) = keep
        bot.CHAT_ID, access.ADMIN_USER_ID = owner
        reminder_flow._reminder_draft.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_grade_needed_is_asked_in_slack_windows_not_typed_as_messages():
    """🎯 Grade Needed opens a window (which average, a pick from 70–90 or your own value); a course's final grade,
    coursework out of and retake with its previous grade are ONE window; the CGPA and record hours another. A wrong value
    is said under its own field and the window stays; Save applies it and redraws the screen it came from."""
    from support import OWNER_ID as OWNER, _with_env
    from uninotifier import gpa, slack, slackbot
    bot = _package("uninotifier.bot")
    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-gpa-window-"))
    calls, press, opened, save, toasts, fake_post = _slack_windows_harness(bot, tmp)

    def no_telegram(*a, **k):
        raise AssertionError("Telegram must not be used while the bot lives in Slack")

    keep = (slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, gpa.STATE_FILE,
            bot.edit_view, bot.send_view)
    owner = _as_owner(bot)
    on = {"NOTIFY_VIA": "slack", "SLACK_BOT_TOKEN": "-".join(("xoxb", "1", "2", "made-up")), "SLACK_CHANNEL": "D0123ABCD",
          "SLACK_APP_TOKEN": "-".join(("xapp", "1", "made-up")), "SLACK_USER": "U0OWNER1", "TELEGRAM_CHAT_ID": OWNER}
    try:
        slack._post, telegram.request, slack.MIN_INTERVAL = fake_post, no_telegram, 0
        slackbot.STORE, slackbot._connect = tmp / "slack_messages.json", lambda: None
        gpa.STATE_FILE = tmp / "gpa.json"
        edited, sent = [], []
        bot.edit_view = lambda message_id, text, rows: edited.append((message_id, text))
        bot.send_view = lambda text, rows: sent.append(text) or {"message_id": 1}

        def scenario():
            screen = slackbot.remember("1700000001.0001", "🎯 Grade Needed")      # the screen the buttons are on
            # 🎯 Grade Needed
            press("gpa:need")
            window = opened()[0]
            assert window["callback_id"] == "gpa_target", window["callback_id"]
            assert slackbot.get_updates(None, 0) == [], "the press went to the chat"
            presets = next(b for b in window["blocks"] if b.get("block_id") == "preset")["element"]
            assert [o["value"] for o in presets["options"]] == ["70", "75", "80", "85", "90"] and presets["initial_option"]["value"] == "80"
            bad = save(window, {"custom": "abc"})
            assert bad["response_action"] == "errors" and "custom" in bad["errors"], bad
            assert "between 0 and 100" in save(window, {"custom": "150"})["errors"]["custom"]
            assert save(window, {"scope": "c", "custom": "77.5"}) == {}
            assert edited and edited[-1][0] == screen and "77.5" in edited[-1][1] and "CGPA" in edited[-1][1], edited
            assert save(window, {"preset": "85"}) == {} and "85" in edited[-1][1]
            # a course's marks: one window
            press("gpa:g:0", ts="1700000001.0001")
            course = opened()[0]
            assert course["callback_id"] == "gpa_course" and json.loads(course["private_metadata"])["index"] == 0
            assert save(course, {"grade": "150"})["errors"]["grade"]
            assert "out of" in save(course, {"got": "70", "out_of": "60"})["errors"]["out_of"]
            assert save(course, {"got": "42"})["errors"]
            assert "previous grade" in save(course, {"retake": ["yes"]})["errors"]["old"]
            assert save(course, {"grade": "88", "got": "42", "out_of": "60"}) == {}
            names = [c for c in gpa.load()["courses"]]
            saved = gpa.load()["courses"][names[0]]
            assert saved["grade"] == 88 and gpa.coursework(saved) == (42, 60) and not saved["retake"]
            assert "saved" in toasts()[-1]
            # reopened, the window holds what was saved; a retake with its previous grade
            press("gpa:w:0")
            again = opened()[0]
            fields = {b["block_id"]: b["element"] for b in again["blocks"] if b["type"] == "input"}
            assert (fields["grade"]["initial_value"], fields["got"]["initial_value"], fields["out_of"]["initial_value"]) == ("88", "42", "60")
            assert save(again, {"grade": "", "got": "", "out_of": "", "retake": ["yes"], "old": "35"}) == {}
            saved = gpa.load()["courses"][names[0]]
            assert saved["retake"] and saved["old_grade"] == 35 and saved["grade"] is None and gpa.coursework(saved) is None
            # turning a retake ON opens the window; turning it OFF is the ordinary toggle (nothing to type)
            press("gpa:r:1")
            assert opened() and opened()[0]["callback_id"] == "gpa_course"
            press("gpa:r:0")
            assert opened() == [], "already a retake: the ordinary toggle"
            # the record
            press("gpa:cg")
            record = opened()[0]
            assert record["callback_id"] == "gpa_record"
            assert "CGPA" in save(record, {"cgpa": "150", "hours": "72"})["errors"]["cgpa"]
            assert save(record, {"cgpa": "71.26", "hours": "72"}) == {}
            assert (gpa.load()["cgpa"], gpa.load()["passed_hours"]) == (71.26, 72)
            # someone else's save changes nothing
            assert save(record, {"cgpa": "10", "hours": "1"}, user="U0STRANGER") == {} and gpa.load()["cgpa"] == 71.26

        _with_env(on, scenario)
    finally:
        (slack._post, telegram.request, slack.MIN_INTERVAL, slackbot.STORE, slackbot._connect, gpa.STATE_FILE,
         bot.edit_view, bot.send_view) = keep
        bot.CHAT_ID, access.ADMIN_USER_ID = owner
        shutil.rmtree(tmp, ignore_errors=True)


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
        "courses", "tasks", "academic", "record", "settings", "check", "rem:new"]
    assert "Updated" in page["blocks"][-1]["elements"][0]["text"]


def test_slack_autolinks_are_unwrapped():
    """A student ID typed in Slack arrives as a link ("<http://202400001|202400001>"): the bot reads what was typed."""
    from uninotifier import slackbot
    assert slackbot.unlink("<http://202400001|202400001>") == "202400001"
    assert slackbot.unlink("<http://202400001>") == "202400001"
    assert slackbot.unlink("id <mailto:a@b.com|a@b.com> &amp; more") == "id a@b.com & more"
    assert slackbot.unlink("plain 202400001") == "plain 202400001"
    update = slackbot.from_event({"type": "message", "channel_type": "im", "user": "U1", "ts": "1.0",
                                  "text": "<http://202400001|202400001>"})
    assert update[0]["message"]["text"] == "202400001"
