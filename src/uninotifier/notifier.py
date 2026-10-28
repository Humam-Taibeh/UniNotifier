"""
Telegram delivery with automatic retry.

Every send goes through `_request`, which retries transient failures
(network errors, 5xx, 429 rate limits) up to MAX_ATTEMPTS times with
exponential backoff. Messages are formatted with Telegram's legacy
"Markdown" parse mode and carry an inline "Open in Portal" button.
"""

import json
import logging
import logging.handlers
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

import requests
from dotenv import dotenv_values

from uninotifier import fileids
from uninotifier import paths
from uninotifier import telegram

log = logging.getLogger("uninotifier")

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2, 5)  # waits after attempt 1 and 2
# The longest "retry after" (HTTP 429) a send waits out. Telegram can ask for minutes; waiting that
# long held the thread — on the bot, buttons and commands — and trying earlier only earns another
# 429. A longer wait gives the send up at once; its caller tries again later (the next check, ...).
MAX_RETRY_WAIT = 60
# Bots may upload up to 50 MB; stay safely under it.
MAX_UPLOAD_BYTES = 45 * 1024 * 1024
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4000      # Telegram's hard limit is 4096
ALBUM_LIMIT = 10
PORTAL_BUTTON_TEXT = "🌐 Open in ZUJ Portal ↗️"

# Runs before every message sent through _request. The entry points of the
# automatic senders set it (monitor.py, the bot's clock threads) to put the
# day separator above the first message of a day — see daily.py.
before_send = None


# ------------------------------------------------------------------ redaction
#
# What must never reach a log line, a traceback or an alert: the secrets that have a shape
# (the bot token, Moodle's session key and cookie, a healthchecks.io ping address) and every
# value in .env, whatever its shape (passwords, usernames, ids, paths) — also URL-encoded, as
# it appears inside a request. .env is read again when it changes. Values shorter than
# MIN_SECRET_LENGTH are left alone: a setting such as BOT_POLL_TIMEOUT=20 is no secret, and
# masking every "20" would make the logs unreadable.

SECRETS_FILE = paths.ENV_FILE
MIN_SECRET_LENGTH = 6
SECRETS_RECHECK_SECONDS = 30

_SHAPES = [
    (re.compile(r"(bot)?\d{6,}:[A-Za-z0-9_-]{30,}"), lambda m: (m.group(1) or "") + "<redacted>"),
    (re.compile(r"""(sesskey["']?\s*[=:]\s*["']?)[A-Za-z0-9]+""", re.I), r"\1<redacted>"),
    (re.compile(r"""(MoodleSession\w*["']?\s*[=:]\s*["']?)[^\s;,"'&)]+""", re.I), r"\1<redacted>"),
    (re.compile(r"""(hc-ping\.com/)[^\s"'<>)]+""", re.I), r"\1<redacted>"),
    # a JWT / bearer token (header.payload[.signature], base64url): Gigya's getJWT, any "Bearer eyJ…"
    (re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*)?"), "<redacted>"),
    # Cambridge One's session cookies (c1_sid, Gigya's glt_… / gig_loginToken_…), the portal's JSESSIONID
    (re.compile(r"""((?:c1_sid|glt_[\w-]+|gig_loginToken_[\w-]+|JSESSIONID)["']?\s*[=:]\s*["']?)[^\s;,"'&)]+"""), r"\1<redacted>"),
]
_secrets: dict = {"checked": None, "mtime": None, "regex": None, "names": {}}


def _env_secrets() -> tuple[re.Pattern | None, dict[str, str]]:
    """(one pattern matching every .env value, value -> its key), rebuilt when .env changes."""
    now = time.monotonic()
    if _secrets["checked"] is not None and now - _secrets["checked"] < SECRETS_RECHECK_SECONDS:
        return _secrets["regex"], _secrets["names"]
    _secrets["checked"] = now
    try:
        mtime = Path(SECRETS_FILE).stat().st_mtime
    except OSError:
        mtime = None
    if mtime == _secrets["mtime"] and _secrets["regex"] is not None:
        return _secrets["regex"], _secrets["names"]
    _secrets["mtime"] = mtime
    names: dict[str, str] = {}
    for key, value in (dotenv_values(SECRETS_FILE) if mtime is not None else {}).items():
        for candidate in {value or "", os.getenv(key) or ""}:          # the file's value, and the one loaded
            candidate = candidate.strip()
            if len(candidate) >= MIN_SECRET_LENGTH:
                for form in {candidate, quote(candidate, safe=""), quote_plus(candidate)}:
                    names.setdefault(form, key)
    _secrets["names"] = names
    _secrets["regex"] = (re.compile("|".join(re.escape(v) for v in sorted(names, key=len, reverse=True)))
                         if names else None)
    return _secrets["regex"], names


def reset_secrets() -> None:
    """Read .env again on the next redact() (tests; a changed SECRETS_FILE)."""
    _secrets.update(checked=None, mtime=None, regex=None, names={})


def redact(text: str) -> str:
    """`text` without any secret: shaped ones become "<redacted>", .env values "<KEY>"."""
    text = text or ""
    for pattern, replacement in _SHAPES:
        text = pattern.sub(replacement, text)
    try:
        regex, names = _env_secrets()
    except Exception:            # redaction must never break the logging or the alert that called it
        return text
    return regex.sub(lambda m: f"<{names[m.group(0)]}>", text) if regex else text


class RedactingFormatter(logging.Formatter):
    """Masks the bot token wherever it appears in a log line or traceback
    (request URLs inside network exceptions carry it)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


LOG_MAX_BYTES = 2 * 1024 * 1024        # a log file starts a new one at this size ...
LOG_BACKUPS = 4                        # ... and the last four are kept (monitor.log.1 is the newest): ~10 MB per log


class RotatingLogFile(logging.handlers.RotatingFileHandler):
    """A log file that stays small. Windows will not rename a file another process has open (the bot
    while `cambridge --check` writes to the same log, two monitor runs side by side): the roll then
    waits for the next record instead of failing the one being logged."""

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()


def setup_logging(log_file: Path) -> logging.Logger:
    formatter = RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(),
                RotatingLogFile(log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")]
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    return logging.getLogger("uninotifier")


def escape_md(text: str) -> str:
    """Escape the characters that are special in legacy Markdown."""
    return re.sub(r"([_*`\[])", r"\\\1", text or "")


# --------------------------------------------- when Telegram refuses the Markdown
#
# A message whose Markdown Telegram cannot parse (an unescaped "_" in a course name, ...) is
# refused for good - retrying it unchanged would fail every run. It is sent once more as plain
# text instead (still readable: links and addresses stay whole), and the owner is told, so the
# text that broke it gets fixed.

_LINK_OR_URL = re.compile(r"(?<!\\)\[((?:[^\]\\]|\\.)*)\]\(([^)\s]+)\)|https?://\S+")


def _unmark(text: str) -> str:
    """Legacy Markdown -> plain text: the *, _ and ` markers go, escaped characters stay."""
    return re.sub(r"\\([_*`\[])", r"\1", re.sub(r"(?<!\\)[*_`]", "", text))


def plain_text(text: str) -> str:
    """What a Markdown message says, as plain text: [label](url) becomes "label (url)", addresses
    are kept exactly as they are (an "_" in a URL is not a marker)."""
    out, last = [], 0
    for match in _LINK_OR_URL.finditer(text or ""):
        out.append(_unmark(text[last:match.start()]))
        out.append(f"{_unmark(match.group(1))} ({match.group(2)})" if match.group(2) else match.group(0))
        last = match.end()
    out.append(_unmark((text or "")[last:]))
    return "".join(out)


def markdown_refused(description) -> bool:
    return "can't parse entities" in str(description or "").lower()


def without_markdown(data: dict) -> dict | None:
    """The same call with every Markdown part as plain text (text, caption, an album's captions).
    None when the call had no Markdown to drop."""
    plain, changed = dict(data), False
    if plain.pop("parse_mode", None):
        changed = True
        for key in ("text", "caption"):
            if isinstance(plain.get(key), str):
                plain[key] = plain_text(plain[key])
    media = plain.get("media")
    if media:
        items = json.loads(media) if isinstance(media, str) else media
        for item in items:
            if isinstance(item, dict) and item.pop("parse_mode", None):
                changed = True
                if isinstance(item.get("caption"), str):
                    item["caption"] = plain_text(item["caption"])
        plain["media"] = json.dumps(items) if isinstance(media, str) else items
    return plain if changed else None


def _report_markdown(method: str, description: str) -> None:
    try:
        from uninotifier import failsafe          # failsafe sends through this module
        failsafe.report("telegram:markdown", "A message went out without its formatting",
                        detail=f"Telegram refused the Markdown of a {method} ({description[:120]}), so it was "
                               "sent as plain text. Some text in it is not escaped — see the logs.",
                        wait=False)
    except Exception:
        log.exception("Could not report the refused Markdown")


def _keyboard(url: str | None, rows: list | None = None) -> str | None:
    """Inline keyboard: optional extra button rows above the portal button."""
    keyboard = list(rows or [])
    if url:
        keyboard.append([{"text": PORTAL_BUTTON_TEXT, "url": url}])
    return json.dumps({"inline_keyboard": keyboard}) if keyboard else None


def _retry_after(resp) -> float | None:
    """The seconds a 429 asks to wait, or None when the reply does not say (or says it oddly)."""
    try:
        return float(resp.json().get("parameters", {}).get("retry_after") or 0) or None
    except (ValueError, TypeError, AttributeError):
        return None


def _request(token: str, method: str, data: dict, file_field: str | None = None,
             file_path: Path | None = None, file_bytes: bytes | None = None,
             file_name: str = "file", attachments: dict[str, tuple[str, bytes]] | None = None,
             outcome: dict | None = None) -> bool:
    """`outcome`, when given, is filled in: {"body": Telegram's JSON reply} on success, or
    {"status": 4xx, "description": ...} when Telegram refused the request for good."""
    if before_send:
        try:
            before_send()
        except Exception:
            log.exception("before_send hook failed")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            files = None
            if file_field and file_path is not None:
                files = {file_field: (file_name, open(file_path, "rb"))}
            elif file_field and file_bytes is not None:
                files = {file_field: (file_name, file_bytes)}
            elif attachments:
                files = dict(attachments)
            try:
                resp = telegram.request(token, method, data=data, files=files, timeout=60)
            finally:
                if files and file_field and hasattr(files[file_field][1], "close"):
                    files[file_field][1].close()

            if resp.ok:
                if outcome is not None:
                    try:
                        outcome["body"] = resp.json()
                    except ValueError:
                        outcome["body"] = None
                return True
            # 4xx other than 429 won't be fixed by retrying.
            if resp.status_code != 429 and resp.status_code < 500:
                plain = without_markdown(data) if markdown_refused(resp.text) else None
                if plain is not None:
                    log.error("Telegram refused the Markdown of %s (%s) — sending it as plain text",
                              method, resp.text[:200])
                    _report_markdown(method, resp.text)
                    return _request(token, method, plain, file_field, file_path, file_bytes, file_name,
                                    attachments, outcome)
                if outcome is not None:
                    outcome.update(status=resp.status_code, description=resp.text[:300])
                log.error("Telegram rejected %s: %s", method, resp.text[:300])
                return False
            delay = None
            if resp.status_code == 429:
                delay = _retry_after(resp)
                if delay and delay > MAX_RETRY_WAIT:
                    log.error("Telegram asks to wait %.0f s before the next %s — giving this one up "
                              "(it is tried again later)", delay, method)
                    if outcome is not None:
                        outcome.update(status=429, description=resp.text[:300])
                    return False
            reason = f"HTTP {resp.status_code}"
        except (requests.RequestException, OSError, ValueError) as e:
            delay = None
            reason = repr(e)

        if attempt == MAX_ATTEMPTS:
            log.error("Telegram %s failed after %d attempts (%s)", method, attempt, reason)
            return False
        wait = delay or BACKOFF_SECONDS[attempt - 1]
        log.warning("Telegram %s attempt %d failed (%s) — retrying in %ss",
                    method, attempt, reason, wait)
        time.sleep(wait)
    return False


def send_message(token: str, chat_id: str, text: str, url: str | None = None,
                 rows: list | None = None, silent: bool = False, reply_to: int | None = None,
                 reply: dict | None = None) -> bool:
    """reply_to: a message id to quote — tapping the quote jumps to it. Sent
    without the quote if that message no longer exists. `reply`, when given, receives
    Telegram's answer ({"body": …}: the message id, to edit it later)."""
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
            "disable_web_page_preview": True}
    if silent:
        data["disable_notification"] = True
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
    kb = _keyboard(url, rows)
    if kb:
        data["reply_markup"] = kb
    return _request(token, "sendMessage", data, outcome=reply)


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split on line boundaries so no Markdown span is cut in half (each line
    carries its own formatting). A single over-long line is hard-wrapped."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks or [""]


def send_long_message(token: str, chat_id: str, text: str, url: str | None = None,
                      rows: list | None = None) -> bool:
    """A message of any length, as consecutive parts; the buttons go on the
    last part. Delivered only if every part was."""
    chunks = split_text(text)
    for n, chunk in enumerate(chunks):
        last = n == len(chunks) - 1
        body = chunk if len(chunks) == 1 else f"{chunk}\n\n_(part {n + 1}/{len(chunks)})_"
        if not send_message(token, chat_id, body, url if last else None, rows if last else None):
            return False
    return True


def call_api(token: str, method: str, data: dict | None = None, timeout: int = 20,
             outcome: dict | None = None) -> dict | None:
    """Single-shot Bot API call for the interactive bot; returns the 'result'
    field, or None on any failure. No retry loop: the polling loop retries.
    `outcome`, when given, receives {"retry_after": seconds} when Telegram asked to slow down."""
    try:
        resp = telegram.request(token, method, json=data or {}, timeout=timeout)
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        # a failed long poll is routine (the polling loop retries and reports a real outage)
        (log.debug if method == "getUpdates" else log.warning)("Telegram %s failed: %r", method, e)
        return None
    if not body.get("ok"):
        plain = without_markdown(data or {}) if markdown_refused(body.get("description")) else None
        if plain is not None:
            log.error("Telegram refused the Markdown of %s (%s) — sending it as plain text",
                      method, str(body.get("description"))[:200])
            _report_markdown(method, str(body.get("description")))
            return call_api(token, method, plain, timeout, outcome)
        if outcome is not None and isinstance(body.get("parameters"), dict) and body["parameters"].get("retry_after"):
            outcome["retry_after"] = _retry_after(resp)
        # "message is not modified" is expected when a button is pressed twice.
        if "not modified" not in str(body.get("description", "")):
            log.warning("Telegram %s rejected: %s", method, str(body)[:200])
        return None
    return body.get("result", True)


def _file_refused(outcome: dict) -> bool:
    """Telegram turned down a file_id (it expired or was never valid): upload the file instead."""
    return outcome.get("status") == 400 and "file" in str(outcome.get("description", "")).lower()


def send_document(token: str, chat_id: str, path: Path, caption: str, url: str | None = None,
                  rows: list | None = None, reply: dict | None = None) -> bool:
    """A document. One Telegram already holds (same content, see fileids.py) is sent by its file_id
    without uploading; the first time, or after Telegram forgot it, the file is uploaded and its
    file_id remembered. `reply`, when given, receives Telegram's answer (as send_message)."""
    data = {"chat_id": chat_id, "caption": caption[:CAPTION_LIMIT], "parse_mode": "Markdown"}
    kb = _keyboard(url, rows)
    if kb:
        data["reply_markup"] = kb
    fp = fileids.fingerprint(path)
    cached = fileids.lookup(fp)
    if cached:
        outcome: dict = {}
        if _request(token, "sendDocument", {**data, "document": cached}, outcome=outcome):
            if reply is not None:
                reply.update(outcome)
            return True
        if not _file_refused(outcome):
            return False                      # a network problem: uploading would not help
        fileids.forget(fp)
    outcome = {}
    if not _request(token, "sendDocument", data, "document", file_path=path, file_name=path.name, outcome=outcome):
        return False
    fileids.remember(fp, (fileids.ids_from_reply(outcome.get("body")) or [None])[0], path.name)
    if reply is not None:
        reply.update(outcome)
    return True


def send_document_group(token: str, chat_id: str, items: list[tuple[Path, str]]) -> bool:
    """One album of documents (Telegram cannot mix documents with photos in an
    album). `items` is [(path, caption)]; each file keeps its own caption. Files Telegram
    already holds go by file_id, only the others are uploaded (fileids.py)."""
    prints = [fileids.fingerprint(path) for path, _ in items]
    cached = [fileids.lookup(fp) for fp in prints]
    for use_cache in ((True, False) if any(cached) else (False,)):
        media, attachments = [], {}
        for n, ((path, caption), fp, file_id) in enumerate(zip(items, prints, cached)):
            if use_cache and file_id:
                reference = file_id
            else:
                reference = f"attach://f{n}"
                attachments[f"f{n}"] = (path.name, path.read_bytes())
            media.append({"type": "document", "media": reference,
                          "caption": caption[:CAPTION_LIMIT], "parse_mode": "Markdown"})
        outcome: dict = {}
        if _request(token, "sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media)},
                    attachments=attachments or None, outcome=outcome):
            ids = fileids.ids_from_reply(outcome.get("body"))
            for (path, _), fp, file_id, new_id in zip(items, prints, cached, ids + [None] * len(items)):
                if not (use_cache and file_id):
                    fileids.remember(fp, new_id, path.name)
            return True
        if not (use_cache and any(cached) and _file_refused(outcome)):
            return False                      # not about a file_id: a retry with the same files would fail the same way
        for fp in prints:
            fileids.forget(fp)                # one of them is stale: upload them all afresh
    return False


def send_png_file(token: str, chat_id: str, png: bytes, filename: str, caption: str = "",
                  url: str | None = None, rows: list | None = None) -> bool:
    """A screenshot sent as a document: Telegram keeps a file byte-for-byte,
    so text stays sharp when zoomed (a photo would be recompressed)."""
    data = {"chat_id": chat_id, "caption": caption[:CAPTION_LIMIT], "parse_mode": "Markdown"}
    kb = _keyboard(url, rows)
    if kb:
        data["reply_markup"] = kb
    return _request(token, "sendDocument", data, "document", file_bytes=png, file_name=filename)


def send_png_files(token: str, chat_id: str, pngs: list[bytes], filename: str) -> bool:
    """Several screenshot files as one album ("name (1 of 3).png", …)."""
    if len(pngs) == 1:
        return send_png_file(token, chat_id, pngs[0], filename)
    stem = filename.removesuffix(".png")
    ok = True
    for start in range(0, len(pngs), ALBUM_LIMIT):
        media, attachments = [], {}
        for n, png in enumerate(pngs[start:start + ALBUM_LIMIT], start):
            media.append({"type": "document", "media": f"attach://d{n}"})
            attachments[f"d{n}"] = (f"{stem} ({n + 1} of {len(pngs)}).png", png)
        if len(media) == 1:
            ok &= send_png_file(token, chat_id, pngs[start], attachments[f"d{start}"][0])
            continue
        ok &= _request(token, "sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media)},
                       attachments=attachments)
    return ok


def upload(token: str, method: str, data: dict, field: str, file_name: str, payload: bytes) -> dict | None:
    """Single-shot upload that returns Telegram's result (e.g. the message id,
    needed to edit the message later). None on any failure."""
    try:
        resp = telegram.request(token, method, data=data, files={field: (file_name, payload)}, timeout=60)
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("Telegram %s failed: %r", method, e)
        return None
    if not body.get("ok"):
        log.warning("Telegram %s rejected: %s", method, str(body)[:200])
        return None
    return body.get("result")


def clock12(hour: int, minute: int, second: int | None = None) -> str:
    """12-hour clock with am/pm: (14, 5) -> '2:05 PM'."""
    secs = f":{second:02d}" if second is not None else ""
    return f"{hour % 12 or 12}:{minute:02d}{secs} {'AM' if hour < 12 else 'PM'}"


def hhmm12(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return clock12(h, m)


def format_timestamp(ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(ts)
        return f"{dt:%Y-%m-%d} {clock12(dt.hour, dt.minute)}"
    except (OverflowError, OSError, ValueError):
        return "?"


def audit_timestamp(ts: float | None = None) -> str:
    """Second-precision local time, used as the audit-trail stamp on messages."""
    try:
        dt = datetime.fromtimestamp(ts if ts is not None else time.time())
        return f"{dt:%Y-%m-%d} {clock12(dt.hour, dt.minute, dt.second)}"
    except (OverflowError, OSError, ValueError):
        return "?"
