"""
Slack as the way your notifications reach you — for a network that blocks Telegram, or a student
who prefers Slack. Switched on in .env:

    NOTIFY_VIA=slack
    SLACK_BOT_TOKEN=xoxb-...     your Slack app's bot token (docs/slack-app-manifest.json makes the app)
    SLACK_CHANNEL=D...           the private conversation between that app and you (the setup finds it)

Then every message the program sends goes to Slack instead of Telegram: new items on Moodle, the
files, deadline and lecture reminders, your own reminders, alerts, and the answers the bot sends
as messages. Nothing else changes — what could not be sent is tried again exactly as before.

notifier._request hands each Telegram call here (deliver) and this module says the same thing in
Slack's words: Telegram's Markdown becomes Slack's mrkdwn, a [label](address) becomes a Slack link,
a button that opens a page becomes a link under the message, files are uploaded with Slack's
two-step upload. A button that needs a press (Mark as done, Add to Reminders) cannot work in a
message-only app, so it is left out; the Telegram menus stay in Telegram.
"""

import json
import logging
import re
import threading
import time
from pathlib import Path

import requests

from uninotifier import envcheck

log = logging.getLogger("uninotifier")

API = "https://slack.com/api/{method}"
TIMEOUT = (6, 30)                  # connect, read
UPLOAD_TIMEOUT = (6, 120)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2, 5)
MAX_RETRY_WAIT = 60
MIN_INTERVAL = 1.1                 # Slack takes about one message a second in a conversation
TEXT_LIMIT = 39000                 # Slack cuts a message at 40,000 characters
MODES = ("telegram", "slack")

_pace = threading.Lock()
_last_sent = 0.0
_LINK_OR_URL = re.compile(r"(?<!\\)\[((?:[^\]\\]|\\.)*)\]\(([^)\s]+)\)|https?://\S+")


class SlackError(Exception):
    """Slack refused a call for good (a wrong token, a removed app): the reason is Slack's word."""


# ------------------------------------------------------------------ settings

def mode() -> str:
    value = (envcheck.value("NOTIFY_VIA") or "telegram").lower()
    return value if value in MODES else "telegram"


def settings() -> tuple[str, str] | None:
    """(token, conversation) as the program sees them — the process environment, else .env."""
    token, channel = envcheck.value("SLACK_BOT_TOKEN"), envcheck.value("SLACK_CHANNEL")
    return (token, channel) if token and channel else None


def active() -> bool:
    """Messages go to Slack: chosen in .env, and the app and the conversation are set."""
    return mode() == "slack" and settings() is not None


# ------------------------------------------------------------------ text

def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape_markers(text: str) -> str:
    """Telegram's escaped markers (\\_ \\* \\` \\[) as the characters themselves: Slack has no escapes."""
    return re.sub(r"\\([_*`\[\]])", r"\1", text)


def mrkdwn(text: str) -> str:
    """Telegram's legacy Markdown as Slack's mrkdwn. *bold*, _italic_ and `code` are written the same
    in both; links and the characters Slack reserves (& < >) are what changes."""
    out, last = [], 0
    for match in _LINK_OR_URL.finditer(text or ""):
        out.append(_escape(_unescape_markers(text[last:match.start()])))
        if match.group(2):
            label = _escape(_unescape_markers(match.group(1))).replace("|", "¦")
            out.append(f"<{match.group(2)}|{label}>")
        else:
            out.append(f"<{match.group(0)}>")
        last = match.end()
    out.append(_escape(_unescape_markers((text or "")[last:])))
    return "".join(out)


def link_lines(reply_markup) -> list[str]:
    """The buttons of a Telegram message that open a page, as Slack links ("🌐 <url|Open in ZUJ Portal ↗️>").
    A button that needs a press has no meaning in a message-only app and is left out."""
    try:
        markup = json.loads(reply_markup) if isinstance(reply_markup, str) else (reply_markup or {})
    except ValueError:
        return []
    lines = []
    for row in (markup.get("inline_keyboard") or []) if isinstance(markup, dict) else []:
        for button in row or []:
            if isinstance(button, dict) and button.get("url"):
                lines.append(f"<{button['url']}|{_escape(str(button.get('text') or 'Open')).replace('|', '¦')}>")
    return lines


def compose(text: str, reply_markup=None) -> str:
    body = mrkdwn(text or "")
    links = link_lines(reply_markup)
    if links:
        body = (body + "\n\n" if body.strip() else "") + "   ".join(links)
    return body[:TEXT_LIMIT]


# ------------------------------------------------------------------ the card (Block Kit)

_TITLE = re.compile(r"^(\S+\s+)?\*([^*\n]+)\*$")                        # "⏰ *Reminder*"
_FIELD = re.compile(r"^(\S+\s+)?\*([^*\n]{1,40}?):\*\s*(.+)$")            # "📘 *Course:* Big Data"
_NOTE = re.compile(r"^_[^_\n].*_$")                                     # "_A note in italics._"
HEADER_LIMIT = 150
SECTION_LIMIT = 2900
MAX_BLOCKS = 50


def _plain(text: str) -> str:
    """mrkdwn markers gone, for Slack's plain-text header."""
    return re.sub(r"(?<!\\)[*_`]", "", _unescape_markers(text)).strip()


def _sections(lines: list[str]) -> list[dict]:
    blocks, chunk = [], ""
    for line in lines:
        if chunk and len(chunk) + len(line) + 1 > SECTION_LIMIT:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk[:SECTION_LIMIT]}})
    return blocks


def _markup(reply_markup) -> list[list[dict]]:
    try:
        markup = json.loads(reply_markup) if isinstance(reply_markup, str) else (reply_markup or {})
    except ValueError:
        return []
    rows = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    return [[b for b in row if isinstance(b, dict)] for row in rows or [] if isinstance(row, list)]


def buttons(reply_markup) -> list[dict]:
    """Telegram's keyboard as Slack's: one row of buttons per row, a button that opens a page opening
    it, any other carrying its Telegram callback data as its value (slackbot hands the press to the
    bot exactly as Telegram would). ✅ is Slack's green button, 🗑 its red one."""
    out = []
    for r, row in enumerate(_markup(reply_markup)):
        elements = []
        for c, button in enumerate(row[:25]):
            label = str(button.get("text") or "·")
            element = {"type": "button", "text": {"type": "plain_text", "text": label[:75], "emoji": True}}
            if button.get("url"):
                element.update(url=str(button["url"])[:3000], action_id=f"u{r}_{c}")
            elif button.get("callback_data"):
                element.update(value=str(button["callback_data"])[:2000], action_id=f"b{r}_{c}")
            else:
                continue
            if label.startswith("✅"):
                element["style"] = "primary"
            elif label.startswith("🗑"):
                element["style"] = "danger"
            elements.append(element)
        if elements:
            out.append({"type": "actions", "block_id": f"row{r}", "elements": elements})
    return out


def blocks(text: str, reply_markup=None, live: bool = False) -> list[dict] | None:
    """The message as a Slack card: its title as a header, its "Label: value" lines side by side in
    two columns, its paragraphs as sections, its italic notes small and grey, the links that open a
    page at the bottom. None when the message is too long for a card (it then goes as text).
    `live`: the bot is connected to Slack (slackbot), so the keyboard becomes real buttons."""
    lines = (text or "").split("\n")
    out: list[dict] = []
    first = lines[0].strip() if lines else ""
    title = _TITLE.match(first)
    if title and not _FIELD.match(first):
        header = ((title.group(1) or "") + _plain(title.group(2))).strip()
        out.append({"type": "header", "text": {"type": "plain_text", "text": header[:HEADER_LIMIT], "emoji": True}})
        lines = lines[1:]
    prose: list[str] = []
    fields: list[dict] = []
    field_lines: list[str] = []

    def flush_prose():
        out.extend(_sections([mrkdwn(l) for l in prose]))
        prose.clear()

    def flush_fields():
        if len(fields) == 1:                                   # one alone reads better as the line it was
            out.append({"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn(field_lines[0])}})
        else:
            for start in range(0, len(fields), 10):           # Slack shows up to 10 fields per section
                out.append({"type": "section", "fields": fields[start:start + 10]})
        field_lines.clear()
        fields.clear()

    for raw in lines:
        line = raw.rstrip()
        field = _FIELD.match(line.strip())
        if field:
            flush_prose()
            label = ((field.group(1) or "") + _plain(field.group(2))).strip()
            fields.append({"type": "mrkdwn", "text": f"*{_escape(label)}*\n{mrkdwn(field.group(3))}"[:1990]})
            field_lines.append(line.strip())
            continue
        if fields:
            flush_fields()
        if _NOTE.match(line.strip()):
            flush_prose()
            out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn(line.strip())[:2900]}]})
        elif line.strip():
            prose.append(line)
        else:
            flush_prose()
    flush_prose()
    if fields:
        flush_fields()
    if live:
        out.extend(buttons(reply_markup))
    else:
        links = link_lines(reply_markup)
        if links:
            out.append({"type": "divider"})
            out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "   ".join(links)[:2900]}]})
    if not out or len(out) > MAX_BLOCKS:
        return None
    return out


# ------------------------------------------------------------------ the API

def _wait_turn() -> None:
    global _last_sent
    with _pace:
        wait = MIN_INTERVAL - (time.monotonic() - _last_sent)
        if wait > 0:
            time.sleep(wait)
        _last_sent = time.monotonic()


def call(token: str, method: str, data: dict | None = None, timeout=TIMEOUT) -> dict:
    """One Slack Web API call, tried again on a network failure, a server error or "slow down".
    Returns Slack's answer; raises SlackError when Slack refused it for good, OSError-like
    requests.RequestException when Slack could not be reached at all."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        delay = None
        try:
            resp = requests.post(API.format(method=method), data=data or {}, timeout=timeout,
                                 headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 429:
                delay = float(resp.headers.get("Retry-After") or 0) or None
                if delay and delay > MAX_RETRY_WAIT:
                    raise SlackError(f"ratelimited (asked to wait {delay:.0f} s)")
                reason = "HTTP 429"
            elif resp.status_code >= 500:
                reason = f"HTTP {resp.status_code}"
            else:
                body = resp.json()
                if body.get("ok"):
                    return body
                raise SlackError(str(body.get("error") or f"HTTP {resp.status_code}"))
        except (requests.RequestException, ValueError) as e:
            if attempt == MAX_ATTEMPTS:
                raise requests.ConnectionError(f"Slack {method}: {type(e).__name__}") from None
            reason = type(e).__name__
        if attempt == MAX_ATTEMPTS:
            raise requests.ConnectionError(f"Slack {method} failed after {attempt} attempts ({reason})")
        wait = delay or BACKOFF_SECONDS[attempt - 1]
        log.warning("Slack %s attempt %d failed (%s) — retrying in %ss", method, attempt, reason, wait)
        time.sleep(wait)
    raise requests.ConnectionError(f"Slack {method} failed")       # not reached


TONES = (("#d93f3f", ("🚨", "❌", "⚠️", "⛔", "🔴", "🆘")),          # needs you now
         ("#e0a020", ("⏰", "⏳", "⌛", "📅", "🗓", "🔔", "📌")),           # time: deadlines, reminders, lectures
         ("#3b6fd8", ("🎉", "📊", "🎓", "🏁", "💯", "📈")))               # marks and results
DEFAULT_TONE = "#0f5f37"                                                # everything else: new on Moodle, done, info


def tone(text: str) -> str:
    """The colour of a message's frame, from the mark its first line starts with."""
    first = (text or "").lstrip().split("\n", 1)[0]
    return next((colour for colour, marks in TONES if first.startswith(marks)), DEFAULT_TONE)


PIECE_LINES = 6           # Slack folds a frame taller than about ten lines under "Show more": stay well under


def _lines(block: dict) -> int:
    """About how many lines a block takes in Slack (a line wraps at about 70 characters)."""
    def wrapped(text: str) -> int:
        return sum(1 + len(part) // 70 for part in text.split("\n"))
    if block.get("type") == "section" and block.get("fields"):
        rows = [block["fields"][i:i + 2] for i in range(0, len(block["fields"]), 2)]
        return sum(max(wrapped(f["text"]) for f in row) for row in rows)
    if block.get("type") == "section":
        return wrapped(block["text"]["text"])
    if block.get("type") == "context":
        return sum(wrapped(e.get("text", "")) for e in block.get("elements", []))
    return 1


def pieces(blocks: list[dict]) -> list[list[dict]]:
    """The frame's blocks in short pieces — each well under the height Slack folds — joining small
    neighbours, so a message is one, two or three pieces and nothing is ever hidden."""
    out, piece, height = [], [], 0
    for block in blocks:
        if block.get("type") == "divider":
            continue                                   # the gap between pieces already separates
        lines = _lines(block)
        if piece and height + lines > PIECE_LINES:
            out.append(piece)
            piece, height = [], 0
        piece.append(block)
        height += lines
    if piece:
        out.append(piece)
    return out


def framed(card: list[dict], colour: str) -> dict:
    """The card as Slack shows it: its first block (the title) on top, the rest inside a frame with a
    coloured edge — each message stands on its own, its colour saying what it is."""
    data = {"blocks": json.dumps(card[:1], ensure_ascii=False)}
    if card[1:]:
        data["attachments"] = json.dumps([{"color": colour, "blocks": piece} for piece in pieces(card[1:])],
                                         ensure_ascii=False)
    return data


def post(token: str, channel: str, text: str, card: list[dict] | None = None, colour: str = DEFAULT_TONE) -> dict:
    """A message: `card` (blocks, framed) when given, `text` as what a notification shows — and the
    message itself when the card is refused, so a layout Slack does not take never costs the message."""
    data = {"channel": channel, "text": text, "mrkdwn": "true", "unfurl_links": "false", "unfurl_media": "false"}
    _wait_turn()
    if card:
        try:
            return call(token, "chat.postMessage", {**data, **framed(card, colour)})
        except SlackError as e:
            if "block" not in str(e) and "invalid_arguments" not in str(e):
                raise
            log.warning("Slack refused the card (%s) — sent as text", e)
            _wait_turn()
    return call(token, "chat.postMessage", data)


def update(token: str, channel: str, ts: str, text: str, card: list[dict] | None, colour: str = DEFAULT_TONE) -> dict:
    """A message already sent, rewritten in place (a screen that changes, buttons that go)."""
    data = {"channel": channel, "ts": ts, "text": text}
    _wait_turn()
    if card:
        try:
            return call(token, "chat.update", {**data, **framed(card, colour)})
        except SlackError as e:
            if "block" not in str(e) and "invalid_arguments" not in str(e):
                raise
            log.warning("Slack refused the card of an edit (%s) — written as text", e)
    return call(token, "chat.update", {**data, "blocks": "[]", "attachments": "[]"})


def delete(token: str, channel: str, ts: str) -> bool:
    try:
        call(token, "chat.delete", {"channel": channel, "ts": ts})
        return True
    except SlackError as e:
        return "message_not_found" in str(e)                 # gone already: what was wanted
    except requests.RequestException:
        return False


def upload(token: str, channel: str, files: list[tuple[str, bytes]], comment: str = "") -> dict:
    """Files into the conversation, as ONE message with `comment` above them (Slack's upload in two
    steps: an address for each file, then one call that shares them all)."""
    shared = []
    for name, payload in files:
        slot = call(token, "files.getUploadURLExternal", {"filename": name, "length": str(len(payload))})
        sent = requests.post(slot["upload_url"], data=payload, timeout=UPLOAD_TIMEOUT)
        if not sent.ok:
            raise requests.ConnectionError(f"Slack upload of a file failed (HTTP {sent.status_code})")
        shared.append({"id": slot["file_id"], "title": name})
    _wait_turn()
    data = {"files": json.dumps(shared), "channel_id": channel}
    if comment.strip():
        data["initial_comment"] = comment
    answer = call(token, "files.completeUploadExternal", data, timeout=UPLOAD_TIMEOUT)
    answer["file_ids"] = [f["id"] for f in shared]
    return answer


# ------------------------------------------------------------------ Telegram's calls, said in Slack

def _file(name: str, path: Path | None, payload: bytes | None) -> tuple[str, bytes] | None:
    if payload is not None:
        return name, payload
    if path is not None:
        return name or Path(path).name, Path(path).read_bytes()
    return None


def deliver(method: str, data: dict, file_name: str = "file", file_path: Path | None = None,
            file_bytes: bytes | None = None, attachments: dict | None = None,
            outcome: dict | None = None) -> bool:
    """notifier._request's Telegram call, delivered to Slack. True when it arrived (the same
    meaning as for Telegram: False means "try again later")."""
    found = settings()
    if not found:
        return False
    token, channel = found
    from uninotifier import slackbot                  # the bot side; it imports this module
    live, message_id = slackbot.interactive(), None
    try:
        if method == "sendMessage":
            if live:                                  # real buttons, and a number to press and edit them by
                message_id = slackbot.send(data.get("text", ""), data.get("reply_markup"))
            else:
                post(token, channel, compose(data.get("text", ""), data.get("reply_markup")),
                     blocks(data.get("text", ""), data.get("reply_markup")), tone(data.get("text", "")))
        elif method in ("sendDocument", "sendPhoto"):
            one = _file(file_name, file_path, file_bytes)
            if one is None:                           # a Telegram file_id: Slack cannot use it
                if outcome is not None:
                    outcome.update(status=400, description="file: Slack needs the file itself")
                return False
            if live:                                  # the card with its buttons, the file right under it
                message_id = slackbot.send(data.get("caption", ""), data.get("reply_markup"), files=[one])
            else:
                upload(token, channel, [one], compose(data.get("caption", ""), data.get("reply_markup")))
        elif method == "sendMediaGroup":
            media = json.loads(data["media"]) if isinstance(data.get("media"), str) else (data.get("media") or [])
            files, captions = [], []
            for item in media:
                reference = str(item.get("media") or "")
                if not reference.startswith("attach://") or reference[9:] not in (attachments or {}):
                    if outcome is not None:
                        outcome.update(status=400, description="file: Slack needs the file itself")
                    return False
                files.append(tuple(attachments[reference[9:]]))
                if item.get("caption"):
                    captions.append(compose(item["caption"]))
            upload(token, channel, files, "\n".join(captions))
        else:
            log.warning("Slack: %s has no Slack form — not sent", method)
            return False
    except SlackError as e:
        log.error("Slack refused %s: %s", method, e)
        if outcome is not None:
            outcome.update(status=400, description=str(e))
        return False
    except (requests.RequestException, OSError) as e:
        log.error("Slack %s failed: %s", method, e)
        return False
    if outcome is not None:                           # as Telegram's answer: the message's number, to edit it
        outcome["body"] = {"ok": True, "result": {"message_id": message_id}} if message_id else None
    return True


# ------------------------------------------------------------------ the setup's questions

def who(token: str) -> dict:
    """{"team": the workspace's name, "bot": the app's name} — raises SlackError for a wrong token."""
    answer = call(token, "auth.test")
    return {"team": answer.get("team", ""), "bot": answer.get("user", "")}


def people(token: str) -> list[dict]:
    """The people of the workspace (no apps, no Slackbot, nobody removed): [{"id", "name"}]."""
    answer = call(token, "users.list", {"limit": "200"})
    found = []
    for member in answer.get("members") or []:
        if member.get("is_bot") or member.get("deleted") or member.get("id") == "USLACKBOT":
            continue
        profile = member.get("profile") or {}
        name = profile.get("real_name") or profile.get("display_name") or member.get("name") or "someone"
        found.append({"id": member["id"], "name": name})
    return found


def conversation(token: str, user: str) -> str:
    """The private conversation between the app and `user`: its channel Id (D...)."""
    return call(token, "conversations.open", {"users": user})["channel"]["id"]
