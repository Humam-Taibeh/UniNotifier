"""
Guest look-up: another student's record, once — their ID, their password, their
verification code, then their record in the same layout as your own transcript
(with their major); 🎯 Target GPA shows the average they need for a goal; 🚪 Sign Out
clears the session. Nothing is stored or synced.

Where the login is typed: on Telegram as two messages (the password one deleted at once).
On Slack an app can never delete your message, so the login is typed into a private
Slack form instead (slackbot opens it): it never becomes a chat message, is never written
to disk, and lives in memory only for the sign-in.
"""

import json
import logging
import re
import threading
import time

from uninotifier import failsafe
from uninotifier import finals
from uninotifier import gpa
from uninotifier import guest
from uninotifier import maintenance
from uninotifier import notifier
from uninotifier import portal
from uninotifier import portal_pages
from uninotifier import registration
from uninotifier import schedule
from uninotifier import slack
from uninotifier import slackbot
from uninotifier import transcript
from uninotifier.bot import chat, portal_flow
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")


# ---------------------------------------------------------- guest look-up
# Another student's record, once: their portal login is typed here (the
# password message is deleted at once and kept in memory only for the
# sign-in), their verification code read from their phone, and their
# record and this term's grades sent one after another. Nothing is stored or synced — none of your own data (GPA
# base, retakes, grades, portal_debug/) is touched. 🚪 Sign Out clears it from
# the chat.

GUEST_SECTIONS = ["transcript", "grades", "balance"]   # grades fill in this term's marks; the financial page is read ONLY for the major


GUEST_TIMEOUT = 10 * 60                  # an unanswered student ID / password prompt expires


_guest: dict = {}                        # step (user | password | running | done), user, at, messages


GUEST_CANCEL_ROW = chat.cancel_row("record")


def guest_send(text: str, rows: list | None = None) -> dict | None:
    """A guest-session message; its id is kept so 🚪 Sign Out can remove it."""
    sent = chat.api("sendMessage", chat_id=chat.CHAT_ID, text=text, parse_mode="Markdown",
               disable_web_page_preview=True, reply_markup={"inline_keyboard": rows or []})
    if isinstance(sent, dict) and "message_id" in sent:
        _guest.setdefault("messages", []).append(sent["message_id"])
    return sent


def guest_file(png: bytes, name: str, caption: str, rows: list | None = None) -> None:
    fields = {"chat_id": chat.CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
    if rows:
        fields["reply_markup"] = json.dumps({"inline_keyboard": rows})
    sent = notifier.upload(chat.BOT_TOKEN, "sendDocument", fields, "document", name, png)
    if isinstance(sent, dict) and "message_id" in sent:
        _guest.setdefault("messages", []).append(sent["message_id"])


DELETE_RETRY_SECONDS = (0.5, 1.5)        # waits before the 2nd and 3rd try to delete a password message


def delete_password_message(message_id: int | None) -> bool:
    """The message holding a guest's password leaves the chat: deleteMessage, tried again twice
    if Telegram did not confirm it. If it still stands, the owner is asked to delete it by hand —
    a password must never just stay there unnoticed. True when it is gone."""
    if not message_id:
        return True
    for wait in (0, *DELETE_RETRY_SECONDS):
        if wait:
            time.sleep(wait)
        if chat.api("deleteMessage", chat_id=chat.CHAT_ID, message_id=message_id) is not None:
            return True
    log.error("Could not delete the message with a guest's password (message %s)", message_id)
    if slack.mode() == "slack":
        return False                                        # Slack never lets an app delete your message: nothing to ask
    chat.api("sendMessage", chat_id=chat.CHAT_ID, parse_mode="Markdown", reply_parameters={
        "message_id": message_id, "allow_sending_without_reply": True},
        text="⚠️ *I couldn't delete the message with the password* — please delete it yourself "
             "(long-press it → Delete).")
    return False


def drop_pending() -> None:
    """A guest look-up still asking for the ID or the password is dropped, what was typed taken out of the chat
    (a finished look-up stays until Sign Out)."""
    if _guest.get("step") in ("user", "password", "target", "form"):
        for message_id in _guest.get("messages", []):
            chat.api("deleteMessage", chat_id=chat.CHAT_ID, message_id=message_id)
        _guest.clear()


def handle_expired_prompt(text: str, message_id: int | None) -> bool:
    """A reply that arrives after the student ID / password prompt expired. The prompt is dropped;
    a reply to the PASSWORD prompt is most likely the password, so it is deleted, not read as
    anything else (a reminder note, a code). True when the text was consumed here."""
    step = _guest.get("step")
    _guest.clear()
    if step != "password" or text.strip().startswith("/"):
        return False
    delete_password_message(message_id)
    chat.send_view("⌛ *The guest look-up had timed out* — I deleted that message in case it was the "
                   "password. Start again with /guest.", [chat.BACK_ROW])
    return True


FORM_ACTION = "guest:form"                # Slack: the button that opens the private login form
FORM_ROW = [{"text": "🔑 Enter Their Login", "callback_data": FORM_ACTION}]


def start_guest() -> None:
    if _guest.get("step") == "running":
        chat.send_view("⏳ *A guest look-up is already running.*", [chat.BACK_ROW])
        return
    _guest.clear()
    if slackbot.interactive():                # Slack cannot delete a typed password: a private form instead
        _guest.update(step="form", at=time.time(), messages=[])
        guest_send("👤 *Guest Look-up*\n\nAnother student's record from the registration portal — once, "
                   "nothing stored, no alerts, nothing of yours touched.\n\nPress *🔑 Enter Their Login*: "
                   "a private form opens. What is typed there never becomes a chat message and is never "
                   "saved.", [FORM_ROW, GUEST_CANCEL_ROW])
        return
    _guest.update(step="user", at=time.time(), messages=[])
    guest_send("👤 *Guest Look-up*\n\nAnother student's record from the registration portal — once, "
               "nothing stored, no alerts, nothing of yours touched.\n\nSend their *student ID* "
               "(portal username).", [GUEST_CANCEL_ROW])


def login_from_form(user: str, password: str) -> None:
    """Slack: the login typed in the private form (slackbot hands it over; it is never stored)."""
    if _guest.get("step") != "form" or time.time() - _guest.get("at", 0) > GUEST_TIMEOUT:
        _guest.clear()
        chat.send_view("⌛ *That guest look-up had ended* — start again with /guest.", [chat.BACK_ROW])
        return
    user = (user or "").strip()
    if not re.fullmatch(r"\w{4,20}", user) or not password:
        guest_send("⚠️ That doesn't look like a student ID and password — press the button and try again.",
                   [FORM_ROW, GUEST_CANCEL_ROW])
        return
    if not portal_flow._portal_lock.acquire(blocking=False):
        guest_send("⏳ A portal request is already running — try again in a minute.", [FORM_ROW, GUEST_CANCEL_ROW])
        return
    _guest.update(step="running", user=user)
    threading.Thread(target=run_guest, args=(user, password), daemon=True).start()


def handle_guest_text(text: str, message_id: int | None) -> None:
    if _guest["step"] == "target":
        if message_id:
            _guest["messages"].append(message_id)
        try:
            value = float(text.strip().replace("%", "").replace(",", "."))
        except ValueError:
            value = None
        if value is None or not 40 <= value <= 100:
            guest_send("⚠️ Send a number between 40 and 100 — for example 78.",
                       [[{"text": "🔙 Back", "callback_data": "guest:target"}, {"text": "🏠 Main Menu", "callback_data": "menu"}]])
            return
        _guest["step"] = "done"
        show_target(_guest.get("panel"), value)
        return
    if _guest["step"] == "user":
        user = text.strip()
        if message_id:
            _guest["messages"].append(message_id)           # removed with the rest on 🚪 Sign Out
        if not re.fullmatch(r"\w{4,20}", user):
            guest_send("⚠️ That doesn't look like a student ID — send just the number.", [GUEST_CANCEL_ROW])
            return
        _guest.update(step="password", user=user, at=time.time())
        guest_send("🔑 Now send their *password*.\nI delete your message at once; it is kept in memory "
                   "only for this sign-in.", [GUEST_CANCEL_ROW])
        return
    delete_password_message(message_id)                     # the password leaves the chat right away
    if not portal_flow._portal_lock.acquire(blocking=False):
        guest_send("⏳ A portal request is already running — try the password again in a minute.",
                   [GUEST_CANCEL_ROW])
        return
    _guest["step"] = "running"
    threading.Thread(target=run_guest, args=(_guest["user"], text), daemon=True).start()


FAILED_ROWS = [[{"text": "🔁 Try Again", "callback_data": "guest:start"}], chat.RECORD_ROW]


def _failed(text: str, png: bytes | None, signing: dict | None) -> None:
    """The look-up did not get in (a wrong ID or password, the portal down): ONE message says so — with the portal's
    own page when there is one, so you see what it said — and 🔁 Try Again. There was no session, so no session panel
    follows, and the "Signing in…" line goes."""
    if isinstance(signing, dict) and signing.get("message_id"):
        chat.api("deleteMessage", chat_id=chat.CHAT_ID, message_id=signing["message_id"])
    _guest.clear()
    if not (png and notifier.send_png_file(chat.BOT_TOKEN, chat.CHAT_ID, png, "Guest - portal.png", text,
                                           rows=FAILED_ROWS)):
        chat.send_view(text, FAILED_ROWS)


@chat.with_action("typing")
def run_guest(user: str, password: str) -> None:
    """Worker thread: sign in as the guest, send what was read. The caller holds _portal_lock."""
    shown = False
    try:
        signing = guest_send(f"👤 Signing in as `{escape_md(user)}`…")
        try:
            results = portal.fetch_many(GUEST_SECTIONS, lambda a, r: portal_flow.ask_portal_code(a, r, for_guest=True),
                                        creds=(user, password), keep_debug=False)
        except maintenance.PortalUnavailable as e:
            _failed(maintenance.alert_text(e, "Try the guest look-up again later."), e.png, signing)
        except portal.PortalError as e:
            _failed(f"❌ *Guest look-up stopped* — {escape_md(notifier.redact(str(e)))}", e.png, signing)
        except Exception as e:
            log.exception("Guest look-up failed")
            failsafe.bug("bot:guest", "A guest look-up crashed", e)
            _failed("❌ *Guest look-up failed* — the portal couldn't be read.", None, signing)
        else:
            shown = send_guest_results(user, results)
            if not shown:
                record = results.get("transcript")
                _failed("❌ *Guest look-up stopped* — their transcript could not be read.",
                        record.png if record else None, signing)
    finally:
        portal_flow._portal_lock.release()
        if shown:                                   # signed in: the session's panel (🎯 Target GPA, 🚪 Sign Out)
            _guest["step"] = "done"
            sent = guest_send(panel_text(), panel_rows(with_target=bool(_guest.get("record"))))
            if isinstance(sent, dict) and "message_id" in sent:
                _guest["panel"] = sent["message_id"]


# ------------------------------------------------------- the panel and the target calculator

TARGET_PRESETS = (70, 75, 80, 85, 90)


def panel_text() -> str:
    return ("👤 *Guest session finished* — nothing of this account is stored.\n\n"
            "🎯 *Target GPA* shows the average they need to reach a goal.\n"
            "Sign out to remove the session from the chat too.")


def panel_rows(with_target: bool = True) -> list:
    rows = [[{"text": "🎯 Target GPA", "callback_data": "guest:target"}]] if with_target else []
    return rows + [[{"text": "🚪 Sign Out & Delete Messages", "callback_data": "guest:wipe"}],
                   [{"text": "🚪 Sign Out, Keep Messages", "callback_data": "guest:out"}]]


def target_picker() -> tuple[str, list]:
    buttons = [{"text": f"{t}%", "callback_data": f"guest:t:{t}"} for t in TARGET_PRESETS]
    rows = [buttons + [{"text": "✏️ Custom Value", "callback_data": "guest:tcustom"}],
            [{"text": "🔙 Back", "callback_data": "guest:back"}, {"text": "🏠 Main Menu", "callback_data": "menu"}]]
    return "🎯 *Target GPA*\n\nWhich cumulative GPA do they want to reach?", rows


def target_text(user: str, record: dict, target: float) -> str:
    """What they need to average for `target`, over this term and the next ones (guest.trajectory)."""
    plan = guest.trajectory(record["semesters"], target)
    figures = record["figures"]
    cgpa = figures.get("record_cgpa", figures.get("cgpa"))
    hours = figures.get("record_hours")
    lines = [f"🎯 *Target GPA — Guest {escape_md(user)}*", ""]
    if cgpa is not None:
        lines.append(f"🎓 *Now:* {cgpa:.2f}%" + (f" over {gpa.fmt(hours)} attempted hours" if hours else ""))
        lines.append("")
    lines.append(f"To reach *{gpa.fmt(target)}%* cumulative, the average they need:")
    first = "This term" if plan["in_progress"] else "Next term"
    labels = {1: first, 2: f"{first} + the next one", 3: f"{first} + the next two"}
    for n, added, needed, verdict in plan["rows"]:
        answer = {"secured": "already secured",
                  "pass": "passing every course is enough",
                  "out": f"out of reach — it would take {gpa.fmt(needed)}%"}.get(verdict, f"*{gpa.fmt(needed)}%*")
        lines.append(f"• 📅 {labels[n]} ({gpa.fmt(added)}h): {answer}")
    lines += ["", "_The university's own rules: a mark under 35 counts as 35, and a retake replaces the old attempt._"]
    return "\n".join(lines)


def edit_panel(message_id: int | None, text: str, rows: list) -> None:
    """Change the guest's control message in place (a guest message, not a menu: it is edited where it is)."""
    if message_id:
        chat.api("editMessageText", chat_id=chat.CHAT_ID, message_id=message_id, text=text, parse_mode="Markdown",
                 disable_web_page_preview=True, reply_markup={"inline_keyboard": rows})
    else:
        guest_send(text, rows)


def show_target(message_id: int | None, target: float) -> None:
    record = _guest.get("record")
    if not record:
        edit_panel(message_id, "🚪 This guest session has ended.", [])
        return
    rows = [[{"text": "🔁 Another Target", "callback_data": "guest:target"}]] + panel_rows(with_target=False)
    edit_panel(message_id, target_text(_guest.get("user", ""), record, target), rows)


def _target_callback(cb: dict, action: str) -> None:
    message_id = (cb.get("message") or {}).get("message_id")
    if not _guest.get("record"):
        edit_panel(message_id, "🚪 This guest session has ended.", [])
        return
    parts = action.split(":")
    kind = parts[1]
    _guest["panel"] = message_id
    if _guest.get("step") == "target":
        _guest["step"] = "done"                                  # a button drops an open "type a number" prompt
    if kind == "target":
        edit_panel(message_id, *target_picker())
    elif kind == "back":
        edit_panel(message_id, panel_text(), panel_rows())
    elif kind == "tcustom":
        _guest.update(step="target", at=time.time())
        edit_panel(message_id, "✏️ *Custom target*\n\nSend it as a number between 40 and 100 — for example 78.",
                   [[{"text": "🔙 Back", "callback_data": "guest:target"}, {"text": "🏠 Main Menu", "callback_data": "menu"}]])
    elif kind == "t" and len(parts) > 2:
        try:
            show_target(message_id, float(parts[2]))
        except ValueError:
            edit_panel(message_id, *target_picker())


def send_guest_results(user: str, results: dict) -> bool:
    """The guest's record, in the layout of your own transcript: the screenshot with the executive summary
    (major, passed hours, hours left when `degree_hours` is set, GPA, status), then the semesters — one text
    message. This term's published marks (the results page) fill in the term in progress. The financial page
    was read only for the major; none of it is shown or kept. False when there was no transcript to show."""
    head = f"👤 *Guest {escape_md(user)}*"
    record = results.get("transcript")
    if not record:
        return False
    semesters = (record.data or {}).get("semesters") or []
    if not semesters:
        guest_send(f"{head} — the transcript page was reached, but no courses could be read from it.")
        if record.png:
            guest_file(record.png, f"Guest {user} - Transcript.png", f"{head} — 📜 Transcript")
        return True
    major = None
    finance = results.get("balance")
    if finance and finance.found:
        major = portal_pages.parse_major(finance.tables)
    passed = record.figures.get("passed_hours")
    degree = schedule.load_schedule().get("degree_hours")
    remaining = degree - passed if degree and passed is not None else None
    status = portal_pages.english_status(record.data["status"]) if record.data.get("status") else None
    plan = record.data.get("plan")
    summary = transcript.summary_lines(semesters, record.figures, status, major, remaining, plan=plan,
                                       retakes=len(record.data.get("retakes") or {}))
    caption = "\n".join([f"{head} — 📜 Transcript", "", *summary])
    rows = [portal_flow.record_buttons("guest", bool(plan))]       # the courses are a button away
    if plan:
        rows.append([{"text": "🗓️ Registration Plan", "callback_data": "guest:reg"}])
    if record.png:
        guest_file(record.png, f"Guest {user} - Transcript.png", caption, rows)
    else:
        guest_send(caption, rows)
    _guest["record"] = {"semesters": semesters, "figures": record.figures, "plan": plan,     # in memory only,
                        "overlay": finals.marks_overlay(results.get("grades"), semesters)}     # gone at sign-out
    return True


def _registration_text(record: dict) -> str:
    """The guest's next term in one message: where they stand, what is open, what is not yet."""
    plan = record.get("plan")
    if not plan:
        return ""
    term, kind = registration.next_term(registration.latest_term(record.get("semesters") or []))
    advice = registration.advise(plan, kind)
    lines = [*registration.overview_lines(advice, term), "", *registration.open_lines(advice), "",
             *registration.blocked_lines(advice)]
    return "\n".join(lines)


def handle_guest_callback(cb: dict, action: str) -> None:
    chat.answer(cb["id"])
    kind = action.split(":")[1]
    if kind == "start":
        start_guest()
        return
    if kind == "form":
        return                                              # Slack opens the form itself, at the press (slackbot)
    if _guest.get("step") == "running":
        return                                              # finishes by itself, then offers 🚪
    if kind in ("target", "t", "tcustom", "back"):
        _target_callback(cb, action)
        return
    if kind == "reg":                                       # what the guest can register next term
        record = _guest.get("record") or {}
        for chunk in notifier.split_text(_registration_text(record) or "🚪 This guest session has ended."):
            guest_send(chunk)
        return
    if kind in ("taken", "left"):                           # the lists under the guest's transcript
        record = _guest.get("record") or {}
        text = portal_flow.record_text(kind, record.get("semesters") or [], record.get("plan"), record.get("overlay"))
        for chunk in notifier.split_text(text or "🚪 This guest session has ended."):
            guest_send(chunk)
        return
    if kind in ("wipe", "cancel"):
        for message_id in _guest.get("messages", []):
            chat.api("deleteMessage", chat_id=chat.CHAT_ID, message_id=message_id)
    _guest.clear()
    if kind == "out":
        _strip_guest_buttons(cb)
    chat.send_view("✅ *Signed out of the guest account* — nothing of it is kept." if kind != "cancel"
              else "✖️ *Guest look-up cancelled.*", [chat.BACK_ROW])


def _strip_guest_buttons(cb: dict) -> None:
    message = cb.get("message") or {}
    if message.get("message_id"):
        chat.api("editMessageReplyMarkup", chat_id=chat.CHAT_ID, message_id=message["message_id"],
            reply_markup={"inline_keyboard": []})
