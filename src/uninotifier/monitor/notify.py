"""
Turning what was found into Telegram messages: new items, forum posts, edits,
file updates, lecture links, new courses — each with its full text, its
screenshot or file, and its buttons (Mark as Done, Add to Reminders, …).
"""

from pathlib import Path
import logging

from uninotifier import archive
from uninotifier import failsafe
from uninotifier import lecturelinks
from uninotifier import lectures
from uninotifier import media
from uninotifier import notifier
from uninotifier import paths
from uninotifier import reminders
from uninotifier import richtext
from uninotifier.monitor import alerts, config, items
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")


def deliver(header: str, body: str = "", url: str | None = None, rows: list | None = None,
            shots: list[bytes] = (), shot_name: str = "screenshot.png",
            document: Path | None = None) -> bool:
    """Send one notification in the clearest shape Telegram allows:
      * a file goes as a document, captioned with the header (the full text
        follows separately when it doesn't fit a caption);
      * the screenshot goes as an uncompressed PNG file — captioned with the
        whole message when it fits, otherwise followed by the full text;
      * long text is split into parts; buttons ride on the last message.
    Returns whether the text reached Telegram (that is what gets retried)."""
    full = media.compose(header, body)
    if document is not None:
        if len(full) <= notifier.CAPTION_LIMIT:
            if notifier.send_document(config.BOT_TOKEN, config.CHAT_ID, document, full, url, rows):
                return True
        elif len(header) <= notifier.CAPTION_LIMIT and notifier.send_document(
                config.BOT_TOKEN, config.CHAT_ID, document, header):
            return notifier.send_long_message(
                config.BOT_TOKEN, config.CHAT_ID, media.compose("📝 *Description*", body), url, rows)
    if len(shots) == 1 and len(full) <= notifier.CAPTION_LIMIT:
        if notifier.send_png_file(config.BOT_TOKEN, config.CHAT_ID, shots[0], shot_name, full, url, rows):
            return True
    elif shots:
        notifier.send_png_files(config.BOT_TOKEN, config.CHAT_ID, list(shots), shot_name)  # best effort
    return notifier.send_long_message(config.BOT_TOKEN, config.CHAT_ID, full, url, rows)


def body_text(item: dict) -> str:
    return richtext.to_text(item.get("_body", ""))


def shot_filename(item: dict) -> str:
    """'Arabic Communication Skills - مقدمة المادة.png'"""
    title = f'{media.clean_course_name(item.get("course", ""))} - {item.get("name") or "Untitled"}'
    return archive.safe_name(title, "screenshot")[:120] + ".png"


def screenshot_note(artifact: dict) -> str:
    if artifact.get("truncated"):
        return (f"📸 The screenshot covers the first {config.MAX_SHOT_PARTS} parts of a very long page; "
                "open the portal for the rest.")
    return ""


def notify_file_update(item: dict, artifact: dict) -> bool:
    path = Path(artifact["path"])
    old = Path(artifact["old_path"]).name if artifact.get("old_path") else None
    retired = artifact.get("old_path") if Path(artifact.get("old_path") or "").parent.name == archive.OLD_VERSIONS else None
    text = media.build_update_message(item, artifact.get("old_modified"), artifact.get("modified"),
                                      old_name=old, new_name=path.name, where=archive.destination(path),
                                      retired_to=archive.retired_to(Path(retired)) if retired else None)
    return deliver(text, url=item["url"], rows=reminder_rows(item, "u"),
                   document=path if items.uploadable(path) else None)


def notify_content_update(item: dict, artifact: dict) -> bool:
    header = media.build_content_update_message(item)
    note = screenshot_note(artifact)
    return deliver(f"{header}\n{note}" if note else header, body_text(item), item["url"],
                   media.completion_rows(item) + reminder_rows(item, "e"),
                   artifact.get("shots", []), shot_filename(item))


def reminder_rows(item: dict, prefix: str = "i") -> list[list[dict]]:
    """📌 Add to Reminders for anything but an assignment or a quiz (those are
    in Upcoming Tasks already). prefix keeps a new item, a file update and an
    edit of the same activity apart: i / u / e + cmid."""
    if item["type"] in media.TASK_TYPES:
        return []
    key = f"{prefix}{item.get('cmid') or item.get('sectionid') or ''}"
    return pin_rows(key, item["name"], item["course"], item["url"])


def notify_new_item(item: dict, deadline: dict | None, artifact: dict,
                    submitted: bool | None = None) -> bool:
    kind = artifact.get("kind") or media.classify(item)
    url = item["url"]
    body = body_text(item)
    rows = media.completion_rows(item) + reminder_rows(item)
    shots, shot_name = artifact.get("shots", []), shot_filename(item)

    if kind == "link" and artifact.get("target"):
        return deliver(media.build_link_message(item, artifact["target"], deadline), body, url, rows)

    if kind == "unknown":
        return deliver(media.build_unclassified_message(item), body, url, rows, shots, shot_name)

    if kind == "meeting":
        return deliver(media.build_meeting_message(item, artifact.get("meeting")), body, url,
                       media.meeting_rows(item, artifact.get("meeting")) + rows, shots, shot_name)

    if kind == "attendance":
        return deliver(media.build_attendance_message(item, artifact.get("sessions")), body, url,
                       media.attendance_rows(item) + rows, shots, shot_name)

    grade = artifact.get("grade")
    status = None
    if item["type"] == "assign":
        done = submitted or (deadline or {}).get("submitted")
        status = media.STATUS_SUBMITTED if done else media.STATUS_PENDING
    saved_path = Path(artifact["path"]) if artifact.get("path") else None
    # the same file under a second Moodle item: it was already sent, say where it is
    duplicate = bool(artifact.get("duplicate"))
    document = saved_path if items.uploadable(saved_path) and not duplicate else None
    where = archive.destination(saved_path) if saved_path else None
    notes = ["📎 Same file as an item already sent — not attached again." if duplicate else
             "💾 Saved locally (file is large or a folder)." if saved_path and not document else "",
             f"📂 *Saved to:* {where}" if where else "",
             screenshot_note(artifact)]
    header = media.build_item_message(item, deadline, "\n".join(n for n in notes if n), grade, status)
    return deliver(header, body, url, rows, shots, shot_name, document)


def pin_rows(key: str, note: str, course: str, url: str) -> list[list[dict]]:
    """📌 Add to Reminders under an announcement or course page; the bot looks
    the key up in reminder_sources.json when it is tapped. Never fails a send."""
    try:
        reminders.remember_source(key, note, media.clean_course_name(course), url)
    except Exception as e:
        log.exception("Could not file the reminder source %s", key)
        failsafe.bug("monitor:pin", "📌 Add to Reminders could not be attached", e,
                     "Notifications go out without the 📌 button until it is fixed.")
        return []
    return [reminders.pin_row(key)]


def notify_forum_post(forum: dict, post: dict) -> bool:
    """The post itself, then its attachments as files (best effort: the
    message already says how many there are)."""
    rows = pin_rows(f"p{post['id']}", post["subject"] or forum["name"], forum["course"], post["url"])
    if not deliver(media.build_forum_post_message(forum, post), richtext.to_text(post["html"]),
                   post["url"], rows):
        return False
    for filename, data in post.get("_files", []):
        tmp = paths.DATA / f"_attachment_{post['id']}_{archive.safe_name(filename, 'file')}"
        try:
            tmp.write_bytes(data)
            notifier.send_document(config.BOT_TOKEN, config.CHAT_ID, tmp, f"📎 {escape_md(filename)}", post["url"])
        except OSError:
            log.exception("Could not forward attachment %s", filename)
        finally:
            tmp.unlink(missing_ok=True)
    return True


def notify_lecture_link(change: dict) -> bool:
    slots = lecturelinks.online_slots(change["course"])
    lines = ["📌 *Lecture Link Saved*", "",
             f'📘 *Course:* {escape_md(change["course"])}']
    if slots:
        lines.append(f"🗓️ *Online lecture:* {slots}")
    lines += [f'🔎 *Found in:* {escape_md(change["origin"])}', "",
              "Every reminder for this lecture now carries the 🎥 *Join Meeting* and "
              "🌐 *Open Course Page* buttons."]
    return lectures.send_with_link(config.BOT_TOKEN, config.CHAT_ID, "\n".join(lines), change["course"], hint=False)


def download_attachments(context, post: dict) -> None:
    post["_files"] = []
    for filename, url in post.get("attachments", []):
        try:
            resp = context.request.get(url, timeout=60000)
            data = resp.body() if resp.ok else b""
            if data and len(data) <= notifier.MAX_UPLOAD_BYTES:
                post["_files"].append((filename, data))
        except Exception:
            log.exception("Could not download forum attachment %s", filename)


def notify_new_course(course: dict) -> bool:
    return alerts.send_telegram(
        f'📚 *New Course*\n\n{media.course_line(course["name"])}', course["url"])
