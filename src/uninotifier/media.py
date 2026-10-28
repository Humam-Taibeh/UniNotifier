"""
Smart media handling: decide how each new item should be treated and build
the Telegram text for it.

  file      resource / folder      -> download, archive, attach
  link      url module             -> never downloaded; send title + direct link
                                      (🎥 for video hosts, 🔗 otherwise)
  text      label, page, section   -> full formatted text + screenshot album
  activity  assign, quiz, forum... -> screenshot of the portal page
  meeting   a Microsoft Teams meeting (teamscheduler) -> details + 🔗 Join Teams Lecture
  attendance  an attendance activity           -> next session + ✍️ Check In
  unknown   unrecognised module type, or a file/link we failed to handle
                                   -> ⚠️ "unclassified item" alert

Every message carries the item's own text (description, label, page body)
in full, and a "Mark as Done" toggle when the activity has one.
"""

import re
from datetime import datetime
from urllib.parse import urlparse

from uninotifier import richtext
from uninotifier import schedule
from uninotifier.archive import course_folder_name
from uninotifier.moodle import quiz_times
from uninotifier.notifier import audit_timestamp, escape_md, format_timestamp

# modname -> (emoji, English type label)
CONTENT_TYPE_LABELS = {
    "assign": ("📝", "Assignment"),
    "quiz": ("📋", "Quiz / Exam"),
    "resource": ("📄", "File"),
    "folder": ("🗂️", "File (Folder)"),
    "url": ("🔗", "Link"),
    "forum": ("📢", "Announcement"),
    "page": ("📃", "Page"),
    "book": ("📖", "Book"),
    "lesson": ("🎓", "Lesson"),
    "choice": ("🗳️", "Poll"),
    "feedback": ("🧾", "Survey"),
    "workshop": ("🛠️", "Workshop"),
    "lti": ("🔌", "External Tool"),
    "data": ("🗃️", "Database"),
    "wiki": ("📚", "Wiki"),
    "glossary": ("📑", "Glossary"),
    "scorm": ("🎮", "Interactive Module"),
    "label": ("🏷️", "Text & Media"),
    "section": ("🗂️", "Section Description"),
    "teamscheduler": ("👥", "Microsoft Teams Meeting"),
    "attendance": ("⏱️", "Attendance Check-in"),
}

# modname -> new-item heading; anything else gets a generic one
NEW_ITEM_HEADINGS = {
    "assign": "📝 *New Assignment*",
    "resource": "📁 *New File*",
    "folder": "📁 *New File*",
    "quiz": "📊 *New Quiz / Exam*",
    "forum": "📢 *New Announcement*",
    "page": "📃 *New Page*",
    "label": "🏷️ *New Course Info*",
    "section": "🗂️ *New Section Description*",
    "teamscheduler": "💻 *New Online Lecture*",
    "attendance": "📝 *Attendance Check-in*",
}
JOIN_TEAMS_BUTTON = "🔗 Join Teams Lecture"
CHECK_IN_BUTTON = "✍️ Check In / تأكيد الحضور"
STATUS_SUBMITTED = "✅ Submitted"
STATUS_PENDING = "⏳ Pending"
DIVIDER = "━━━━━━━━━━━━━━━━━━"

FILE_TYPES = {"resource", "folder"}
LINK_TYPES = {"url"}
TEXT_TYPES = {"label", "page", "section"}
MEETING_TYPES = {"teamscheduler"}     # read by activities.py: details + join link
ATTENDANCE_TYPES = {"attendance"}     # read by activities.py: the sessions

# Completion policy. Only static material is ticked automatically: a file or
# folder once it is saved in the archive, a link once it was resolved. Graded
# work is never touched (Moodle ticks it on submission/attempt), and pages,
# labels, books and forums are left for the student — they may ask for
# something. Everything that isn't ticked gets a one-tap toggle.
AUTO_DONE_TYPES = {"resource", "folder", "url"}
NEVER_DONE_TYPES = {"assign", "quiz"}
# A forum is only a container (every course has an empty "Announcements"):
# it is never announced itself — only the posts published inside it are —
# unless its own description carries text: instructors also write an
# announcement straight into a forum's intro (see silent_container).
CONTAINER_TYPES = {"forum"}
# Assignments and quizzes are tracked in Upcoming Tasks by themselves; every
# other notification carries 📌 Add to Reminders.
TASK_TYPES = {"assign", "quiz"}
DONE_BUTTON = "✅ Mark as Done"
UNDO_BUTTON = "↩️ Undo Done"

VIDEO_HOST_RE = re.compile(
    r"(youtube\.com|youtu\.be|vimeo\.com|zoom\.us|teams\.microsoft\.com|"
    r"teams\.live\.com|meet\.google\.com|drive\.google\.com|panopto|"
    r"kaltura|loom\.com|dailymotion\.com|\.mp4($|\?)|\.m3u8($|\?))",
    re.I,
)


def classify(item: dict) -> str:
    modname = item["type"]
    if modname in FILE_TYPES:
        return "file"
    if modname in LINK_TYPES:
        return "link"
    if modname in TEXT_TYPES:
        return "text"
    if modname in MEETING_TYPES:
        return "meeting"
    if modname in ATTENDANCE_TYPES:
        return "attendance"
    if modname in CONTENT_TYPE_LABELS:
        return "activity"
    return "unknown"


def silent_container(item: dict) -> bool:
    """An empty forum: no news by itself (its posts are announced instead)."""
    return item["type"] in CONTAINER_TYPES and not richtext.is_meaningful(item.get("_body", ""))


def completion_row(cmid: str, done: bool) -> list[dict]:
    """The inline toggle: callback 'done:<cmid>:<1|0>' asks the bot to set
    the portal's completion to that value."""
    if done:
        return [{"text": UNDO_BUTTON, "callback_data": f"done:{cmid}:0"}]
    return [{"text": DONE_BUTTON, "callback_data": f"done:{cmid}:1"}]


FAILED_BUTTON = "⚠️ Couldn't update — tap to retry"


def completion_failed_row(cmid: str, done: bool) -> list[dict]:
    """The toggle after an update that failed while the person was already told it was in progress:
    pressing it tries the same change again."""
    return [{"text": FAILED_BUTTON, "callback_data": f"done:{cmid}:{1 if done else 0}"}]


def completion_rows(item: dict) -> list[list[dict]]:
    if not item.get("manual") or not item.get("cmid") or item["type"] in NEVER_DONE_TYPES:
        return []
    return [completion_row(item["cmid"], bool(item.get("done")))]


def is_video_url(url: str) -> bool:
    return bool(url and VIDEO_HOST_RE.search(url))


def resolve_url_target(context, view_url: str) -> str | None:
    """Find where a Moodle URL module points, without following it."""
    try:
        resp = context.request.get(view_url, max_redirects=0, timeout=30000)
        location = resp.headers.get("location")
        if location and not urlparse(location).path.endswith("/login/index.php"):
            return location
        match = re.search(r'class="urlworkaround"[^>]*>.*?href="([^"]+)"', resp.text(), re.S)
        if match:
            return match.group(1).replace("&amp;", "&")
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- messages
# Bilingual layout: field names and types in English, the sentence in formal
# Arabic, and an exact detection time as an audit trail.

def clean_course_name(full_name: str) -> str:
    """'20261-Big Data-AllSec-3-' -> 'Big Data'. Arabic-only portal names are
    mapped to their English name via schedule.json when it knows them."""
    return schedule.canonical_course(course_folder_name(full_name))


def _label(item: dict) -> tuple[str, str]:
    return CONTENT_TYPE_LABELS.get(item["type"], ("📎", "Item"))


def course_line(full_name: str) -> str:
    return f"📘 *Course:* {escape_md(clean_course_name(full_name))}"


def _fields(item: dict, type_label: str, emoji: str, deadline: dict | None,
            status: str | None) -> list[str]:
    lines = [
        course_line(item["course"]),
        f"{emoji} *Type:* {type_label}",
        f'📌 *Title:* {escape_md(item["name"])}',
    ]
    if item.get("section"):
        lines.append(f'🗓️ *Section:* {escape_md(item["section"])}')
    if deadline:
        lines += deadline_lines(deadline)
        status = status or (STATUS_SUBMITTED if deadline.get("submitted") else STATUS_PENDING)
    if status:
        lines.append(f"📊 *Status:* {status}")
    if item.get("manual") and item["type"] not in NEVER_DONE_TYPES:
        lines.append(f'☑️ *Progress:* {"✅ Marked as done" if item.get("done") else "⬜ Not done yet"}')
    return lines


def deadline_lines(deadline: dict) -> list[str]:
    """🕐 Due Date for an assignment; 🕐 Opens and/or Closes for a quiz."""
    if deadline.get("type") != "quiz":
        return [f'🕐 *Due Date:* {format_timestamp(deadline["timestamp"])}']
    opens, closes = quiz_times(deadline)
    return [f"🕐 *{field}:* {format_timestamp(ts)}" for field, ts in (("Opens", opens), ("Closes", closes)) if ts]


def _audit(detected_at: float | None = None) -> str:
    return f"🕒 *Detected:* {audit_timestamp(detected_at)}"


def compose(header: str, body: str) -> str:
    """A message header followed by the item's own formatted text
    (description, label, page body, forum post)."""
    return f"{header}\n{DIVIDER}\n{body}" if body else header


def build_item_message(item: dict, deadline: dict | None, note: str = "",
                       grade: dict | None = None, status: str | None = None,
                       detected_at: float | None = None) -> str:
    emoji, label = _label(item)
    if grade:
        head = f'🎉 *Marks Detected:* *{escape_md(grade["summary"])}*'
    else:
        head = NEW_ITEM_HEADINGS.get(item["type"], "🆕 *New Content*")
    lines = [head, "", *_fields(item, label, emoji, deadline, status)]
    if grade:
        for header, value in grade["details"][:8]:
            lines.append(f"   • {escape_md(header)}: {escape_md(value)}")
    if note:
        lines += ["", note]
    lines.append(_audit(detected_at))
    return "\n".join(lines)


def build_content_update_message(item: dict, detected_at: float | None = None) -> str:
    """An instructor edited a label, a page or a description; the current
    version follows as the body."""
    emoji, label = _label(item)
    return "\n".join(["✏️ *Content Updated*", "", *_fields(item, label, emoji, None, None),
                      "", "The instructor edited this content. Current version below.",
                      _audit(detected_at)])


def build_forum_post_message(forum: dict, post: dict, detected_at: float | None = None) -> str:
    """A new topic or reply in a course forum. Topics in the Announcements
    forum are the instructor's announcements."""
    is_news = forum["name"].strip().lower() in ("announcements", "news forum", "الإعلانات", "أخبار")
    if post["is_reply"]:
        head = "💬 *New Forum Reply*"
    else:
        head = "📢 *New Announcement*" if is_news else "🗣️ *New Forum Topic*"
    lines = [
        head, "",
        course_line(forum["course"]),
        f'📂 *Forum:* {escape_md(forum["name"])}',
        f'📌 *Subject:* {escape_md(post["subject"] or "(no subject)")}',
    ]
    if post.get("author"):
        lines.append(f'👤 *By:* {escape_md(post["author"])}')
    if post.get("time"):
        lines.append(f'🕐 *Posted:* {format_timestamp(post["time"])}')
    if post.get("attachments"):
        lines.append(f'📎 *Attachments:* {len(post["attachments"])}')
    lines.append(_audit(detected_at))
    return "\n".join(lines)


def elapsed(seconds: float) -> str:
    """3 days 4 hours / 2 hours 5 minutes / 12 minutes / under a minute."""
    minutes = int(seconds // 60)
    days, hours, mins = minutes // 1440, minutes % 1440 // 60, minutes % 60
    plural = lambda n, unit: f"{n} {unit}{'' if n == 1 else 's'}"
    if days:
        return plural(days, "day") + (f" {plural(hours, 'hour')}" if hours else "")
    if hours:
        return plural(hours, "hour") + (f" {plural(mins, 'minute')}" if mins else "")
    return plural(mins, "minute") if mins else "under a minute"


def _file_name(name: str) -> str:
    return f"`{name.replace('`', chr(39))}`"


def build_update_message(item: dict, old_modified: float | None, new_modified: float | None,
                         old_name: str | None = None, new_name: str | None = None,
                         where: str | None = None, detected_at: float | None = None,
                         retired_to: str | None = None) -> str:
    """A re-uploaded document: the previous file, the new file and where it was
    saved, where the previous one went (retired_to: its _Old_Versions folder),
    and how long after the previous version the change was made."""
    emoji, label = _label(item)
    lines = ["🔄 *File Updated*", "",
             *_fields(item, label, emoji, None, None), ""]
    previous = f"⏮️ *Previous file:* {_file_name(old_name)}" if old_name else "⏮️ *Previous version*"
    lines.append(previous + (f" — {audit_timestamp(old_modified)}" if old_modified else ""))
    if new_name:
        lines.append(f"⏭️ *New file:* {_file_name(new_name)}"
                     + (f" — {audit_timestamp(new_modified)}" if new_modified else ""))
    elif new_modified:
        lines.append(f"⏭️ *Modified at:* {audit_timestamp(new_modified)}")
    if where:
        lines.append(f"📂 *Saved to:* {where}")
    if retired_to:
        lines.append(f"🗄️ *Previous version moved to:* {retired_to}")
    if old_modified and new_modified and new_modified >= old_modified:
        lines.append(f"⏱️ *Changed:* {elapsed(new_modified - old_modified)} after the previous version")
    lines += ["", "The instructor uploaded a new version. It takes the previous file's place; the previous "
                  "file was moved, unchanged, into `_Old_Versions`." if retired_to else
                  "The instructor uploaded a new version. The previous file was no longer in the folder, "
                  "so nothing was moved."]
    lines.append(_audit(detected_at))
    return "\n".join(lines)


def build_link_message(item: dict, target: str, deadline: dict | None = None,
                       detected_at: float | None = None) -> str:
    video = is_video_url(target)
    if video:
        head, emoji, label = "🎥 *New Video*", "🎥", "Video / Lecture Recording"
    else:
        head, emoji, label = "🔗 *New Link*", "🔗", "Link"
    lines = [head, "", *_fields(item, label, emoji, deadline, None)]
    lines.append(f"{'▶️ *Watch:*' if video else '🌐 *URL:*'} "
                 f"[{escape_md(urlparse(target).netloc or 'open')}]({target})")
    lines.append(_audit(detected_at))
    return "\n".join(lines)


def build_meeting_message(item: dict, meeting: dict | None, detected_at: float | None = None) -> str:
    """A Teams meeting activity: its details from the portal; the join link
    is the button under the message, never raw text."""
    from uninotifier import activities
    meeting = meeting or {}
    lines = [NEW_ITEM_HEADINGS["teamscheduler"], "", *_fields(item, "Microsoft Teams Meeting / Online Lecture", "👥", None, None)]
    if meeting.get("details"):
        lines.append("")
        lines += [f"{activities.meeting_icon(k)} *{escape_md(k)}:* {escape_md(v)}" for k, v in meeting["details"]]
    lines += ["", f"Tap *{JOIN_TEAMS_BUTTON[2:]}* below to open the meeting in Teams."
              if meeting.get("url") else "The join link opens from the meeting's portal page (button below)."]
    lines.append(_audit(detected_at))
    return "\n".join(lines)


def meeting_rows(item: dict, meeting: dict | None) -> list[list[dict]]:
    return [[{"text": JOIN_TEAMS_BUTTON, "url": (meeting or {}).get("url") or item["url"]}]]


def build_attendance_message(item: dict, sessions: list[dict] | None, now: datetime | None = None,
                             detected_at: float | None = None) -> str:
    """An attendance activity: the next session and what checking in means.
    The instructor's own note (e.g. "الرجاء تأكيد الحضور …") follows as the body."""
    from uninotifier import activities
    lines = [NEW_ITEM_HEADINGS["attendance"], "", *_fields(item, "Attendance Check-in", "⏱️", None, None)]
    upcoming = activities.next_session(sessions or [], now)
    if upcoming:
        lines.append(f'🗓️ *Next session:* {escape_md(upcoming["date"])}'
                     + (f' · {escape_md(upcoming["time"])}' if upcoming["time"] else ""))
        if upcoming["points"]:
            lines.append(f'🎯 *Points:* {escape_md(upcoming["points"].replace("?", "–"))}')
    if sessions:
        lines.append(f"📋 *Sessions listed:* {len(sessions)}")
    lines += ["", "Check-in opens on the portal during each session — tap *Check In* below then."]
    lines.append(_audit(detected_at))
    return "\n".join(lines)


def attendance_rows(item: dict) -> list[list[dict]]:
    return [[{"text": CHECK_IN_BUTTON, "url": item["url"]}]]


def build_reminder_message(deadline: dict, flag: str, detected_at: float | None = None) -> str:
    """flag is '24h' or '1h'. Never sent for a submitted assignment."""
    is_quiz = deadline.get("type") == "quiz"
    emoji, label = ("📋", "Quiz / Exam") if is_quiz else ("📝", "Assignment")
    head = "🚨 *Final Reminder*" if flag == "1h" else "⏰ *Reminder*"
    hours = "1 hour" if flag == "1h" else "24 hours"
    action = ("Closes" if deadline.get("event") == "close" else "Opens") if is_quiz else "Due"
    return "\n".join([
        head, "",
        course_line(deadline.get("course", "")),
        f"{emoji} *Type:* {label}",
        f'📌 *Title:* {escape_md(deadline.get("name", ""))}',
        *deadline_lines(deadline),
        f"📊 *Status:* {STATUS_PENDING}",
        "",
        f"{action} in {hours}.",
        _audit(detected_at),
    ])


def build_unclassified_message(item: dict) -> str:
    return "\n".join([
        "⚠️ *Unclassified Item*", "",
        course_line(item["course"]),
        f'📌 *Title:* {escape_md(item["name"])}',
        f'🏷️ *Type:* `{item["type"]}`',
        item["url"],
        _audit(),
    ])
