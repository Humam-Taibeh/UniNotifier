"""
The weekly timetable on Slack: uploaded ONCE, then shown again from Slack's own copy — instantly.

Uploading the timetable on every press (a PDF and a PNG, about 1.5 MB with the files in assets/) meant two
uploads, Slack's share step and a wait for the files to appear: several seconds, more on a slow line. Slack keeps
a file it was given, so a press shows that copy instead:

  first press, or the timetable changed   upload both files (one message), and remember their Slack file ids and
                                          links under the files' content (sha256) in data/slack_files.json
  every later press                       ONE message: the picture from Slack's copy (an image block) and a link
                                          that opens the PDF in Slack — no upload, no waiting; the previous quick
                                          copy is taken away, so the chat keeps one
  a new timetable                         uploaded again; the old files and their messages are removed

Anything Slack refuses on the quick path (a file deleted by hand, a block it does not take) drops the memory and the
files are uploaded afresh — the timetable always arrives. Telegram has its own equivalent (fileids.py).
"""

import json
import logging
import time
from pathlib import Path

import requests

from uninotifier import fileids, fileio, paths, slack

log = logging.getLogger("uninotifier")

STATE_FILE = paths.DATA / "slack_files.json"
KEY = "timetable"


def _load() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    fileio.write_json_atomic(STATE_FILE, data)


def _remember(entry: dict | None) -> None:
    data = _load()
    if entry:
        data[KEY] = entry
    else:
        data.pop(KEY, None)
    _save(data)


def quick_blocks(entry: dict) -> list[dict]:
    """The quick copy: the picture from Slack's own file, the PDF one tap away."""
    return [{"type": "image", "slack_file": {"id": entry["png_id"]}, "alt_text": "Weekly timetable"},
            {"type": "context", "elements": [{"type": "mrkdwn", "text":
                f"📎 <{entry['pdf_link']}|{slack._escape(entry['pdf_name'])}> — the high-resolution PDF"}]}]


def _delete(token: str, channel: str, ts: str | None, file_ids: list[str] = ()) -> None:
    """Best effort: an old copy that will not go is no reason to fail a press."""
    for file_id in file_ids:
        try:
            slack.call(token, "files.delete", {"file": file_id})
        except (slack.SlackError, requests.RequestException):
            pass
    if ts:
        slack.delete(token, channel, ts)


def send_timetable(pdf: Path, png: Path) -> str | None:
    """Show the timetable in the Slack conversation: "cached" (Slack's copy), "uploaded", or None when it could not
    be sent (the caller then falls back to the ordinary way)."""
    found = slack.settings()
    if not found:
        return None
    token, channel = found
    shas = [fileids.fingerprint(pdf), fileids.fingerprint(png)]
    entry = _load().get(KEY) or {}
    if entry.get("shas") == shas and entry.get("png_id") and entry.get("pdf_link"):
        try:
            slack._wait_turn()
            posted = slack.call(token, "chat.postMessage", {
                "channel": channel, "text": "📅 Weekly timetable", "unfurl_links": "false", "unfurl_media": "false",
                "blocks": json.dumps(quick_blocks(entry), ensure_ascii=False)})
            _delete(token, channel, entry.get("quick_ts"))                 # the chat keeps one quick copy
            _remember({**entry, "quick_ts": posted.get("ts"), "shown": time.time()})
            return "cached"
        except slack.SlackError as e:                                     # a file deleted by hand, a block refused
            log.info("Slack: the saved timetable could not be shown again (%s) — uploading it", e)
        except requests.RequestException as e:
            log.warning("Slack: the timetable could not be sent (%s)", type(e).__name__)
            return None
    try:
        answer = slack.upload(token, channel, [(pdf.name, pdf.read_bytes()), (png.name, png.read_bytes())])
    except (slack.SlackError, requests.RequestException, OSError) as e:
        log.warning("Slack: the timetable could not be uploaded (%s)", e)
        return None
    pdf_id, png_id = (answer.get("file_ids") or [None, None])[:2]
    shared = answer.get("shared") or {}
    if entry:                                    # the copy before this one (an older timetable, or one Slack refused to
        _delete(token, channel, entry.get("quick_ts"))                    # show again): its files and messages go
        _delete(token, channel, entry.get("upload_ts"), [i for i in (entry.get("pdf_id"), entry.get("png_id")) if i])
    link = (shared.get("links") or {}).get(pdf_id) or ""
    _remember({"shas": shas, "pdf_id": pdf_id, "png_id": png_id, "pdf_name": pdf.name, "pdf_link": link,
               "upload_ts": shared.get("ts") or "", "quick_ts": None, "shown": time.time()} if pdf_id and png_id else None)
    return "uploaded"
