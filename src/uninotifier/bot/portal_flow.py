"""
The registration portal on demand (/grades, /transcript, /balance): one
request at a time, the verification code relayed from the chat, and the
result card — which also syncs the GPA base, retakes and published grades.
"""

import json
import logging
import queue
import re
import threading
import time
from dataclasses import fields
from pathlib import Path

from uninotifier import failsafe
from uninotifier import fileids
from uninotifier import fileio
from uninotifier import finals
from uninotifier import gpa
from uninotifier import maintenance
from uninotifier import notifier
from uninotifier import paths
from uninotifier import portal
from uninotifier import schedule
from uninotifier import transcript
from uninotifier.bot import chat, guest_flow
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")


_portal_lock = threading.Lock()


_portal_code: queue.Queue | None = None        # set while a portal sign-in waits for its code


PORTAL_TITLES = {"grades": ("🎓", "Grades"), "transcript": ("📜", "Transcript"),
                 "balance": ("💰", "Tuition Balance")}


PORTAL_WAIT_FOR_CODE = 5 * 60


CODE_TEXT_RE = re.compile(r"(?=[A-Za-z]*\d)[A-Za-z0-9]{3,10}")   # has a digit: ordinary words are not codes


def portal_setup_text() -> str:
    return "\n".join([
        "🔒 *Portal login not configured*", "",
        "Add these two lines to the `.env` file next to `bot.py` (never send them in chat):", "",
        f"`{portal.ENV_USERNAME}=your portal username`",
        f"`{portal.ENV_PASSWORD}=your portal password`", "",
        "No restart is needed — `.env` is read on every request. The portal is only contacted "
        "when you send /grades, /transcript or /balance."])


def handle_portal_code(text: str) -> bool:
    """While a portal sign-in waits for its verification code, a short reply
    containing a digit ("481516", "481 516") is that code."""
    code = re.sub(r"[\s-]", "", text)
    waiting = _portal_code
    if waiting is None or len(text) > 14 or not CODE_TEXT_RE.fullmatch(code):
        return False
    waiting.put(code)
    chat.send_view("🔑 *Code received* — signing in to the portal…", [])
    return True


def ask_portal_code(attempt: int, rejected: bool, for_guest: bool = False) -> str | None:
    """Called from the portal worker thread: ask in chat, wait for the reply."""
    global _portal_code
    answers: queue.Queue = queue.Queue()
    _portal_code = answers
    whom = "the student a verification code — ask them for it and" if for_guest else "you a verification code."
    try:
        text = (("❌ *The portal didn't accept that code.*\n\n" if rejected else "") +
                f"🔐 *Portal Verification*\n\nThe ZUJ registration portal sent {whom} "
                "Reply here with it.\n⏱️ Waiting up to 5 minutes.")
        (guest_flow.guest_send(text) if for_guest else chat.send_view(text, []))
        return answers.get(timeout=PORTAL_WAIT_FOR_CODE)
    except queue.Empty:
        return None
    finally:
        _portal_code = None


def stamp(at: float | None = None, note: str = "") -> str:
    """🕒 Retrieved: <now> for a fresh read, 🕒 As of: <then> for a cached one."""
    head = "Retrieved" if at is None else "As of"
    return f"🕒 *{head}:* {notifier.audit_timestamp(at)}" + (f" · {note}" if note else "")


def compose(result: portal.Result, when: str) -> tuple[str, list[str], list]:
    """(caption, the text that follows it, buttons) of a portal answer. Pure: nothing is sent or synced."""
    emoji, title = PORTAL_TITLES[result.section]
    portal_row = [{"text": "🏛️ Open Registration Portal ↗️", "url": portal.LOGIN_URL}]
    rows = [portal_row]
    lines = [f"{emoji} *ZUJ Portal — {title}*", when]
    body: list[str] = []

    if not result.found:
        lines += ["", f"⚠️ I couldn't find the {title} page in the portal menu.",
                  "The screenshot shows where I got to; the menu entries I could see are listed below."]
        if result.menu:
            lines += ["", escape_md(" · ".join(result.menu))]
    else:
        body = list(result.lines) or portal.table_lines(result.tables)
        if result.section == "transcript":
            semesters = (result.data or {}).get("semesters") or []
            status = portal.english_status(result.data["status"]) if result.data.get("status") else None
            degree, passed = schedule.load_schedule().get("degree_hours"), result.figures.get("passed_hours")
            remaining = degree - passed if degree and passed is not None else None
            shown = transcript.summary_lines(semesters, result.figures, status, remaining=remaining)
            if shown:
                lines += [""] + shown
            if semesters:
                body = transcript.blocks(semesters)
        elif result.section == "balance" and body:
            total, rest = body[0], body[1:]
            if len("\n".join(lines + ["", total, *rest])) <= notifier.CAPTION_LIMIT:
                lines += ["", total, *rest]           # a balance is short: all of it goes in the one message
                body = []
            else:
                lines += ["", total]
                body = rest
    return "\n".join(lines), body, rows


def apply_syncs(result: portal.Result) -> None:
    """Under the hood of a fresh read: the Grade Needed base, this term's retakes, published grades."""
    if result.section == "transcript" and result.found:
        f = result.figures
        if "base_cgpa" in f and "base_hours" in f:
            sync_base(f["base_cgpa"], f["base_hours"])
        if "retakes" in result.data:              # only a record that was read: never clear on a failed parse
            sync_retakes(result.data["retakes"])
    if result.section == "grades":
        lock_grades({r["code"]: r["mark"] for r in result.data.get("results", []) if r.get("mark") is not None})


def send_portal_result(result: portal.Result) -> None:
    """The answer to a portal request as ONE message: the page's screenshot with everything that fits
    in its caption (summary and details). Only when there is more than a caption holds — a transcript's
    semesters — the rest follows as a single text message. The menu comes back under it (run_portal)."""
    apply_syncs(result)
    header, body, rows = compose(result, stamp())
    name = f"ZUJ Portal - {PORTAL_TITLES[result.section][1]}.png"
    if not (result.png and notifier.send_png_file(chat.BOT_TOKEN, chat.CHAT_ID, result.png, name, header, rows=rows)):
        notifier.send_message(chat.BOT_TOKEN, chat.CHAT_ID, header, rows=rows)
    if any(line.strip() for line in body):
        notifier.send_long_message(chat.BOT_TOKEN, chat.CHAT_ID, "\n".join(body).strip())


# ---------------------------------------------------------------- the cached answer (stale-while-revalidate)
#
# Transcript and Balance are answered at once from the last read, stamped "As of HH:MM". When that
# read is FRESH_SECONDS old or more, the portal is read again right behind it and the same messages
# are updated in place: the stamp only (nothing changed), or the screenshot and text too. A refresh
# that fails leaves the cached answer, saying so. Grades (the Finals Watch check) are always read fresh.

SWR_SECTIONS = ("transcript", "balance")
FRESH_SECONDS = 15 * 60
CACHE_DIR = paths.DATA / "portal_cache"
REFRESHING = "checking for changes…"
REFRESH_FAILED = "⚠️ couldn't refresh just now — this is the saved copy"
UPDATED = "🔄 updated"


def _cache_files(section: str) -> tuple[Path, Path]:
    return CACHE_DIR / f"{section}.json", CACHE_DIR / f"ZUJ Portal - {PORTAL_TITLES[section][1]}.png"


def _content(result: portal.Result) -> str:
    """What the answer says (not the screenshot's pixels): equal = nothing to update."""
    return json.dumps({f.name: getattr(result, f.name) for f in fields(result) if f.name != "png"},
                      ensure_ascii=False, sort_keys=True, default=str)


def save_cached(result: portal.Result) -> None:
    if result.section not in SWR_SECTIONS or not result.found or not result.png:
        return
    meta, png = _cache_files(result.section)
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        png.write_bytes(result.png)
        fileio.write_json_atomic(meta, {"at": time.time(), "result": json.loads(_content(result))})
    except OSError as e:
        log.warning("Portal: could not keep the %s answer (%s)", result.section, e)


def load_cached(section: str) -> tuple[portal.Result, float] | None:
    if section not in SWR_SECTIONS:
        return None
    meta, png = _cache_files(section)
    try:
        saved = json.loads(meta.read_text(encoding="utf-8"))
        return portal.Result(**saved["result"], png=png.read_bytes()), float(saved["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _message_id(reply: dict) -> int | None:
    return ((reply.get("body") or {}).get("result") or {}).get("message_id")


def _parts(body: list[str]) -> list[str]:
    """The text after the caption as send_long_message would send it."""
    text = "\n".join(body).strip()
    if not text:
        return []
    chunks = notifier.split_text(text)
    return [c if len(chunks) == 1 else f"{c}\n\n_(part {n + 1}/{len(chunks)})_" for n, c in enumerate(chunks)]


def send_cached(result: portal.Result, when: str) -> dict:
    """The cached answer, shaped like send_portal_result's (the screenshot goes by its file_id: nothing
    is uploaded twice). Returns the message ids, to update them: {"doc", "body": [...]}."""
    header, body, rows = compose(result, when)
    sent: dict = {"doc": None, "body": []}
    reply: dict = {}
    if notifier.send_document(chat.BOT_TOKEN, chat.CHAT_ID, _cache_files(result.section)[1], header,
                              rows=rows, reply=reply):
        sent["doc"] = _message_id(reply)
    else:
        notifier.send_message(chat.BOT_TOKEN, chat.CHAT_ID, header, rows=rows)
    for part in _parts(body):
        reply = {}
        notifier.send_message(chat.BOT_TOKEN, chat.CHAT_ID, part, reply=reply)
        sent["body"].append(_message_id(reply))
    return sent


def _edit_caption(message_id: int, header: str, rows: list) -> None:
    chat.api("editMessageCaption", chat_id=chat.CHAT_ID, message_id=message_id,
             caption=header[:notifier.CAPTION_LIMIT], parse_mode="Markdown", reply_markup={"inline_keyboard": rows})


def _edit_media(message_id: int, header: str, rows: list, result: portal.Result) -> None:
    """The new screenshot (and caption) into the same message; its file_id is remembered."""
    path = _cache_files(result.section)[1]
    media = {"type": "document", "media": "attach://document", "caption": header[:notifier.CAPTION_LIMIT],
             "parse_mode": "Markdown"}
    answer = notifier.upload(chat.BOT_TOKEN, "editMessageMedia",
                             {"chat_id": chat.CHAT_ID, "message_id": message_id, "media": json.dumps(media),
                              "reply_markup": json.dumps({"inline_keyboard": rows})},
                             "document", path.name, result.png)
    if isinstance(answer, dict):
        fileids.remember(fileids.fingerprint(path), (fileids.ids_from_reply({"result": answer}) or [None])[0], path.name)


def refresh(section: str, old: portal.Result, at: float, sent: dict) -> None:
    """The read behind a cached answer, and the in-place update."""
    failed = None
    try:
        new = portal.fetch_many([section], ask_portal_code)[section]
        if not new.found:
            failed = "the page was not found"
    except (maintenance.PortalUnavailable, portal.PortalError) as e:
        failed = str(e)
    except Exception as e:
        log.exception("Portal refresh failed")
        failsafe.bug("bot:portal-refresh", "Refreshing a cached portal answer failed", e,
                     "The cached answer stays as it was; Transcript and Balance still answer.")
        failed = type(e).__name__
    if failed:
        log.info("Portal: the %s refresh did not work (%s) — the saved copy stays", section, notifier.redact(failed))
        if sent["doc"]:
            header, _, rows = compose(old, stamp(at, REFRESH_FAILED))
            _edit_caption(sent["doc"], header, rows)
        return
    apply_syncs(new)
    save_cached(new)
    changed = _content(new) != _content(old)
    header, body, rows = compose(new, stamp(note=UPDATED if changed else ""))
    log.info("Portal: the cached %s was %s", section, "updated" if changed else "still current")
    if not sent["doc"]:                           # the cached copy never arrived as a file: answer afresh
        send_portal_result(new)
        return
    if not changed:
        _edit_caption(sent["doc"], header, rows)
        return
    _edit_media(sent["doc"], header, rows, new)
    parts = _parts(body)
    if len(parts) == len(sent["body"]) and all(sent["body"]):
        for message_id, part in zip(sent["body"], parts):
            chat.api("editMessageText", chat_id=chat.CHAT_ID, message_id=message_id, text=part, parse_mode="Markdown")
        return
    for message_id in filter(None, sent["body"]):  # a different number of parts: they are sent again
        chat.api("deleteMessage", chat_id=chat.CHAT_ID, message_id=message_id)
    for part in parts:
        notifier.send_message(chat.BOT_TOKEN, chat.CHAT_ID, part)
    chat.send_view(*return_view(section))         # the menu back under the new text


def answer_from_cache(section: str, result: portal.Result, at: float) -> None:
    """At once: the saved answer and the menu under it; then, if it is old, the refresh behind it."""
    stale = time.time() - at >= FRESH_SECONDS
    log.info("Portal request: %s (answered from the %s copy)", section, "saved" if stale else "fresh")
    sent = send_cached(result, stamp(at, REFRESHING if stale else ""))
    chat.send_view(*return_view(section))
    if stale:
        refresh(section, result, at, sent)


def sync_base(cgpa: float, hours: float) -> None:
    """The portal's record (before this term) becomes the Grade Needed base."""
    try:
        if gpa.set_base(cgpa, hours, "portal"):
            log.info("CGPA base synced from the portal: %.2f over %g hours", cgpa, hours)
    except Exception as e:
        log.exception("Could not sync the CGPA base")
        failsafe.bug("bot:sync-base", "The CGPA base could not be updated from the transcript", e,
                     "Grade Needed works from the previous base until it is fixed.")


def sync_retakes(old_by_code: dict[str, float]) -> None:
    """Retaken courses (from the portal record) become retakes in Grade Needed."""
    try:
        changed = gpa.sync_retakes({finals.display_name(code, code): old for code, old in old_by_code.items()})
        if changed:
            log.info("Retakes synced from the portal: %s", ", ".join(changed))
    except Exception as e:
        log.exception("Could not sync the retakes")
        failsafe.bug("bot:sync-retakes", "This term's retakes could not be updated from the transcript", e)


def lock_grades(marks_by_code: dict[str, float]) -> None:
    """Official marks from the portal become locked grades in Grade Needed."""
    try:
        changed = gpa.record_portal_grades({finals.display_name(code, code): mark
                                            for code, mark in marks_by_code.items()})
        if changed:
            log.info("Locked portal grades for: %s", ", ".join(changed))
    except Exception as e:
        log.exception("Could not lock the portal grades")
        failsafe.bug("bot:lock-grades", "Published grades could not be locked into Grade Needed", e)


def send_unavailable(error: maintenance.PortalUnavailable, follow_up: str, rows: list | None = None) -> None:
    """A failed portal sign-in, with the screenshot of what the portal shows. `rows`: the buttons of the
    message (None = a way back to the menu; [] = none, the caller puts the menu under it)."""
    text = maintenance.alert_text(error, follow_up)
    rows = [chat.BACK_ROW] if rows is None else rows
    if not (error.png and notifier.send_png_file(chat.BOT_TOKEN, chat.CHAT_ID, error.png, maintenance.SNAPSHOT.name,
                                                 text, rows=rows)):
        chat.send_view(text, rows)


def return_view(section: str) -> tuple[str, list]:
    """The screen the person came from, put back under the answer: the hub the button lives in."""
    from uninotifier.bot import views
    return views.view_academic() if section == "grades" else views.view_record()


@chat.with_action("typing")
def read_and_answer(section: str, message_id: int | None) -> None:
    """A fresh read, then the answer — one message — and the menu under it. No status message: the
    chat shows "typing…", and a button's own message shows the progress."""
    emoji, title = PORTAL_TITLES[section]
    log.info("Portal request: %s", section)
    if message_id:
        chat.edit_view(message_id, f"{emoji} *{title}* — signing in to the ZUJ registration portal…", [])
    failure = None
    try:
        results = portal.fetch_many([section], ask_portal_code)
    except maintenance.PortalUnavailable as e:
        send_unavailable(e, "Try again a little later.", rows=[])         # the screenshot says why
    except portal.PortalError as e:
        failure = f"❌ *{title}* — {escape_md(notifier.redact(str(e)))}"
    except Exception:
        log.exception("Portal request failed")
        failure = f"❌ *{title}* — the portal couldn't be read. See `data/logs/bot.log` for the reason."
    else:
        for result in results.values():
            send_portal_result(result)
            save_cached(result)
    if failure:
        rows = [[{"text": "🔄 Try Again", "callback_data": f"portal:{section}"}], chat.BACK_ROW]
        chat.edit_view(message_id, failure, rows) if message_id else chat.send_view(failure, rows)
    else:
        chat.send_view(*return_view(section))


def run_portal(section: str, message_id: int | None) -> None:
    """Worker thread: one on-demand portal request — from the saved answer when there is one
    (Transcript, Balance), else read now."""
    try:
        cached = load_cached(section)
        if cached:
            answer_from_cache(section, *cached)
        else:
            read_and_answer(section, message_id)
    except Exception as e:
        log.exception("Portal worker failed")
        failsafe.bug("bot:portal", "A portal request crashed", e)
    finally:
        _portal_lock.release()


def start_portal(section: str, message_id: int | None = None) -> bool:
    """False when nothing was started (already busy); a missing login is
    answered in chat."""
    if portal.credentials() is None:
        chat.send_view(portal_setup_text(), [chat.BACK_ROW])
        return True
    if not _portal_lock.acquire(blocking=False):
        return False
    threading.Thread(target=run_portal, args=(section, message_id), daemon=True).start()
    return True
