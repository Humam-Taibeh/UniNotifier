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
files are uploaded with Slack's two-step upload, and the message is laid out as Slack's card (a
notification that fits), in a box (anything longer, every screen), or — for a Slack that refuses
those — a coloured frame, then plain text. Without the app's live connection a button that needs a
press (Mark as done) cannot work, so it is left out and a button that opens a page becomes a link;
with it, slackbot.py gives the whole bot real buttons and the Home page.
"""

import html
import itertools
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
_local = threading.local()
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


def _address(url: str) -> str:
    """A link's address as Slack must receive it inside <address|label>: the characters that would end it or start
    another link (< > |) are written as the web writes them (%3C %3E %7C), so an address taken from a Moodle page
    can never relabel a link or slip in a <!here>. (A plain & stays: Slack opens such addresses as they are.)"""
    return url.replace("<", "%3C").replace(">", "%3E").replace("|", "%7C")


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
            out.append(f"<{_address(match.group(2))}|{label}>")
        else:
            out.append(f"<{_address(match.group(0))}>")
        last = match.end()
    out.append(_escape(_unescape_markers((text or "")[last:])))
    return "".join(out)


_PREVIEW_LINK = re.compile(r"<[^|>]+\|([^>]*)>|<([^>|]+)>")


def preview(text: str) -> str:
    """What a notification shows on a lock screen or in the sidebar: the words of the message
    without the marks of the markup (no *stars*, no <address|label>)."""
    plain = _PREVIEW_LINK.sub(lambda m: m.group(1) or m.group(2), text or "")
    plain = html.unescape(plain.replace("*", "").replace("`", ""))
    return "\n".join(line for line in plain.split("\n") if not re.fullmatch(r"\s*[━─]{6,}\s*", line)).strip()


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
                lines.append(f"<{_address(str(button['url']))}|{_escape(str(button.get('text') or 'Open')).replace('|', '¦')}>")
    return lines


def compose(text: str, reply_markup=None) -> str:
    body = mrkdwn(text or "")
    links = link_lines(reply_markup)
    if links:
        body = (body + "\n\n" if body.strip() else "") + "   ".join(links)
    return body[:TEXT_LIMIT]


# ------------------------------------------------------------------ the card (Block Kit)

_TITLE = re.compile(r"^(\S+\s+)?\*([^*\n]+)\*$")                        # "⏰ *Reminder*"
_TITLE_LEAD = re.compile(r"^(\S+\s+)\*([^*\n:]+)\*(\s+[•·—-]\s+.+)$")    # "🎓 *UniNotifier System* • First Semester"
_TITLE_RUN = re.compile(r"^(\S+\s+)\*([^*\n:]+)\*\s+([^\n]+)$")          # "🔧 *The bot stopped* and is back."
_FIELD = re.compile(r"^(\S+\s+)?\*([^*\n]{1,40}?):\*\s*(.+)$")            # "📘 *Course:* Big Data"
HEAD = "⁣"                      # an invisible mark at the start of a line: in Slack a big heading (a header block)
SMALL = "⁢"                     # ... and this one a line of small grey details (a context block); Telegram shows neither
_NOTE = re.compile(r"^_[^_\n].*_$")                                     # "_A note in italics._"
HEADER_LIMIT = 150
TITLE_SHOWN = 38                   # what a box's title shows on one line; a longer line stays inside the box, whole
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


NAV_MARKS = ("🔙", "🏠", "⬅", "↩")                  # the way back: kept apart, at the very bottom
NAV_WORDS = ("Main Menu", "Back")
FIELD_ROWS = 4                                      # up to so many rows of two fields get a section each
ROW_SIZE = 5                                        # buttons side by side; Slack wraps a row that is too wide
ROW_WIDTH = 62                                      # what a row of a message's buttons may hold, so no label is ever cut: the
                                                    # width of the words (an emoji counts double) plus 4 for each button


def _width(element: dict) -> int:
    label = element.get("text", {}).get("text", "")
    return sum(2 if ord(c) > 0x2300 else 1 for c in label) + 4


def _by_width(elements: list[dict], limit: int) -> list[list[dict]]:
    """The buttons in rows that each fit `limit`: a long label stands alone, short ones share a row."""
    rows: list[list[dict]] = []
    for element in elements:
        if rows and len(rows[-1]) < ROW_SIZE and sum(_width(e) for e in rows[-1]) + _width(element) <= limit:
            rows[-1].append(element)
        else:
            rows.append([element])
    return rows


def _is_nav(label: str) -> bool:
    return label.startswith(NAV_MARKS) or any(label.endswith(word) for word in NAV_WORDS)


def _row_lines(rows: list[list[dict]]) -> list[dict]:
    """The rows whose first button carries a "line" as Slack's section blocks, one per row: the line's
    words (mrkdwn, always whole) with, at its right, either a drop-down list ("options": [[label, value]],
    "initial": value — the person picks the number) or the row's first button (a toggle). Telegram and
    any layout that knows no lines just see ordinary rows (the extra key is ignored)."""
    out = []
    for n, row in enumerate(rows):
        info, button = row[0]["line"], row[0]
        block = {"type": "section", "block_id": f"line{n}",
                 "text": {"type": "mrkdwn", "text": str(info.get("text", ""))[:SECTION_LIMIT] or " "}}
        if info.get("multi"):                    # a drop-down to tick several things (Slack allows it only beside a line of words)
            many = info["multi"]
            options = [{"text": {"type": "plain_text", "text": str(t)[:75], "emoji": True}, "value": str(v)[:150]}
                       for t, v in many["options"][:100]]
            block["accessory"] = {"type": "multi_static_select", "action_id": f"msel:{many['prefix']}", "options": options,
                                  "placeholder": {"type": "plain_text", "text": many.get("placeholder", "Choose"), "emoji": True}}
            chosen = [o for o in options if o["value"] in {str(x) for x in many.get("initial", [])}]
            if chosen:
                block["accessory"]["initial_options"] = chosen
        elif info.get("options"):
            options = [{"text": {"type": "plain_text", "text": str(label)[:75], "emoji": True}, "value": str(value)[:150]}
                       for label, value in info["options"][:100]]
            block["accessory"] = {"type": "static_select", "action_id": f"line{n}", "options": options,
                                  "placeholder": {"type": "plain_text", "text": "Choose", "emoji": True}}
            chosen = [o for o in options if o["value"] == str(info.get("initial"))]
            if chosen:
                block["accessory"]["initial_option"] = chosen[0]
        else:
            block["accessory"] = {"type": "button", "action_id": f"line{n}", "value": str(button["callback_data"])[:2000],
                                  "text": {"type": "plain_text", "text": str(button["text"])[:75], "emoji": True}}
            if info.get("style") in ("primary", "danger"):
                block["accessory"]["style"] = info["style"]
        out.append(block)
    return out


def small(line: str) -> str:
    """In Slack a line of details under an item: smaller and grey, inside the item. Elsewhere the line as it is."""
    return SMALL + line if mode() == "slack" else line


def heading(text: str, plain: str) -> str:
    """In Slack a big heading (`plain`: its words without marks); elsewhere `text`, the line as Telegram shows it."""
    return HEAD + plain if mode() == "slack" else text


def grouped(rows: list[list[dict]], name: str, stack: bool = False) -> list[list[dict]]:
    """In Slack: these rows are one group of buttons (a list of courses, a list of tasks), apart from
    the rows around them by a light line; `stack`: each row on a line of its own (a list of courses, one under
    the other). Elsewhere the rows as they are."""
    if mode() != "slack":
        return rows
    return [[{**row[0], "group": name, "stack": stack}, *row[1:]] if row else row for row in rows]


def buttons(reply_markup, prefix: str = "", width: int | None = None) -> list[dict]:
    """Telegram's keyboard as Slack's: the buttons side by side (Telegram stacks one per row, which in
    Slack reads as a long list), the way back apart under a line, a button that opens a page opening
    it, any other carrying its Telegram callback data as its value (slackbot hands the press to the
    bot exactly as Telegram would). ✅ is Slack's green button, 🗑 its red one."""
    groups: list[tuple[tuple, list]] = []            # the buttons, in groups (a row's first button may name its "group")
    nav, cards = [], []
    for r, row in enumerate(_markup(reply_markup)):
        if row and row[0].get("line"):
            cards.append(row)
            continue
        group = (row[0].get("group"), bool(row[0].get("stack"))) if row else (None, False)
        mine: list[dict] = []
        for c, button in enumerate(row[:25]):
            label = str(button.get("text") or "·")
            element = {"type": "button", "text": {"type": "plain_text", "text": label[:75], "emoji": True}}
            if button.get("url"):
                element.update(url=str(button["url"])[:3000], action_id=f"{prefix}u{r}_{c}")
            elif button.get("callback_data"):
                element.update(value=str(button["callback_data"])[:2000], action_id=f"{prefix}b{r}_{c}")
            else:
                continue
            if label.startswith("✅"):
                element["style"] = "primary"
            elif label.startswith("🗑"):
                element["style"] = "danger"
            if _is_nav(label):
                nav.append(element)
                continue
            mine.append(element)
        if mine:
            if not groups or groups[-1][0] != group:
                groups.append((group, []))
            groups[-1][1].append(mine)
    out: list[dict] = []
    numbers = itertools.count()                          # every row has an id of its own (Slack refuses a message that repeats one)
    for (group, stack), rows_ in groups:
        if out:
            out.append({"type": "divider"})              # a group of buttons never runs into the one before it
        flat = [e for row in rows_ for e in row]
        if stack:                                        # a list: each row on a line of its own, one under the other
            rows = [part for row in rows_ for part in (_by_width(row, width or ROW_WIDTH))]
        elif width:                                      # a message's buttons: none of them with its label cut
            rows = _by_width(flat, width)
        else:                                            # the Home page: a row of five
            rows = [flat[n:n + ROW_SIZE] for n in range(0, len(flat), ROW_SIZE)]
        for row in rows:
            out.append({"type": "actions", "block_id": f"{prefix}row{next(numbers)}", "elements": row})
    if cards:
        out[:0] = _row_lines(cards)
    if nav:                                              # the way back — Back and Main Menu — under a line of its own
        if out:
            out.append({"type": "divider"})
        out.append({"type": "actions", "block_id": f"{prefix}nav" if prefix else "nav", "elements": nav[:25]})
    return out


def _one_liner(blocks_: list[dict]) -> bool:
    """A menu's single short line ("What would you like to see?"): the buttons follow it without a line between."""
    texts = [b for b in blocks_ if b["type"] != "header" and b.get("block_id") != "subtitle"]
    if len(texts) != 1 or texts[0]["type"] != "section" or "text" not in texts[0]:
        return False
    words = texts[0]["text"]["text"]
    return "\n" not in words and len(words) <= 60


GAP = chr(0x2800)                     # a blank character: a line of empty space that Slack does not trim


def _room_before_heading(out: list[dict]) -> None:
    """A heading never sits against the text above it: the block before it ends with an empty line (no block spent)."""
    if not out:
        return
    last = out[-1]
    if last["type"] == "context" and last["elements"] and last["elements"][0].get("text"):
        last["elements"][0]["text"] += chr(10) + GAP
    elif last["type"] == "section" and "text" in last and "accessory" not in last:
        last["text"]["text"] += chr(10) + GAP


def _only_title(blocks_: list[dict]) -> bool:
    """Nothing yet but the card's title (and its small line): the box already draws a line under it."""
    return all(b["type"] == "header" or b.get("block_id") == "subtitle" for b in blocks_)


def blocks(text: str, reply_markup=None, live: bool = False) -> list[dict] | None:
    """The message as a Slack card: its title as a header, its "Label: value" lines side by side in
    two columns, its paragraphs as sections, its italic notes small and grey, the links that open a
    page at the bottom. None when the message is too long for a card (it then goes as text).
    `live`: the bot is connected to Slack (slackbot), so the keyboard becomes real buttons."""
    lines = (text or "").split("\n")
    out: list[dict] = []
    first = lines[0].strip() if lines else ""
    title = _TITLE.match(first) or _TITLE_LEAD.match(first) or _TITLE_RUN.match(first)
    if title and not _FIELD.match(first):
        header = ((title.group(1) or "") + _plain(title.group(2))).strip()
        out.append({"type": "header", "text": {"type": "plain_text", "text": header[:HEADER_LIMIT], "emoji": True}})
        rest = title.group(3).strip(" •·—-") if title.re is _TITLE_LEAD else ""
        if rest:                                       # "🎓 *UniNotifier System* • First Semester": the rest, small
            out.append({"type": "context", "block_id": "subtitle",
                        "elements": [{"type": "mrkdwn", "text": mrkdwn(rest)[:2900]}]})
        lines = lines[1:]
        if title.re is _TITLE_RUN:                     # "🔧 *Title* and the words": the words are the card's text
            lines = [title.group(3)] + lines
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
            size = 2 if len(fields) <= FIELD_ROWS * 2 else 10        # a pair a section: the rows have room between them
            for start in range(0, len(fields), size):                # (Slack shows up to 10 fields per section)
                out.append({"type": "section", "fields": fields[start:start + size]})
        field_lines.clear()
        fields.clear()

    smalls: list[str] = []

    def flush_small():
        if smalls:
            out.append({"type": "context", "block_id": f"small{len(out)}",
                        "elements": [{"type": "mrkdwn", "text": mrkdwn("\n".join(smalls))[:2900]}]})
            smalls.clear()

    for raw in lines:
        line = raw.rstrip()
        if not line.startswith(SMALL):
            flush_small()                                      # the small details end with the first line that is not one
        if line.startswith(HEAD):                              # a heading: bigger than the items under it
            flush_prose()
            if fields:
                flush_fields()
            heading = _plain(line[len(HEAD):])
            _room_before_heading(out)
            out.append({"type": "header", "text": {"type": "plain_text", "text": heading[:HEADER_LIMIT], "emoji": True}})
            continue
        if line.startswith(SMALL):                             # details of the item above: smaller, inside it
            flush_prose()
            if fields:
                flush_fields()
            smalls.append(line[len(SMALL):].strip())
            continue
        field = _FIELD.match(line.strip())
        if field:
            flush_prose()
            label = ((field.group(1) or "") + _plain(field.group(2))).strip()
            fields.append({"type": "mrkdwn", "text": f"*{_escape(label)}*\n{mrkdwn(field.group(3))}"[:1990]})
            field_lines.append(line.strip())
            continue
        if fields:
            flush_fields()
        if _RULE.match(line.strip()):                          # a rule line is a light line, never a row of marks:
            flush_prose()                                      # none under the title (its box has one), one between parts
            if out and out[-1]["type"] != "divider" and not _only_title(out):
                out.append({"type": "divider"})
        elif _NOTE.match(line.strip()):
            flush_prose()
            out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn(line.strip())[:2900]}]})
        elif line.strip():
            prose.append(line)
        else:
            flush_prose()
    flush_prose()
    flush_small()                                              # the last item's small details are an item too
    if fields:
        flush_fields()
    if live:
        keyboard = buttons(reply_markup, width=ROW_WIDTH)
        if keyboard and out and out[-1]["type"] != "divider" and not _only_title(out) and not _one_liner(out):
            out.append({"type": "divider"})                    # the same everywhere: a light line above the buttons
        out.extend(keyboard)
    else:
        links = link_lines(reply_markup)
        if links:
            out.append({"type": "divider"})
            out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "   ".join(links)[:2900]}]})
    if not out or len(out) > MAX_BLOCKS:
        return None
    return out


HOME_TITLE = "🎓 UniNotifier"
HOME_BLOCKS = 100
BOX_CHILDREN = 10                  # Slack's limit of blocks inside one container


def _foldable(children: list[dict]) -> bool:
    return any(str(b.get("block_id", "")).startswith("small") and i and children[i - 1]["type"] == "section"
               and "text" in children[i - 1] for i, b in enumerate(children))


def _fold_small(children: list[dict]) -> list[dict]:
    """The last item's small details as part of the line above them: one block for that item instead of two (the
    earlier items keep their hierarchy as long as the box can hold it)."""
    for i in range(len(children) - 1, 0, -1):
        block, last = children[i], children[i - 1]
        if str(block.get("block_id", "")).startswith("small") and last["type"] == "section" and "text" in last:
            words = last["text"]["text"] + "\n" + block["elements"][0]["text"]
            joined = {"type": "section", "text": {"type": "mrkdwn", "text": words}}
            return children[:i - 1] + [joined] + children[i + 1:]
    return children


def _fewer(children: list[dict], kind: str) -> list[dict]:
    """Neighbouring button rows as one row (Slack wraps it), or neighbouring paragraphs as one — the
    same card in fewer blocks, so it fits in one box. The way back (block "nav") stays apart."""
    out: list[dict] = []
    for block in children:
        last = out[-1] if out else None
        if kind == "actions" and last and block["type"] == last["type"] == "actions" \
                and "nav" not in (block.get("block_id"), last.get("block_id")) \
                and len(last["elements"]) + len(block["elements"]) <= ROW_SIZE \
                and sum(_width(e) for e in last["elements"] + block["elements"]) <= ROW_WIDTH:
            out[-1] = {**last, "elements": last["elements"] + block["elements"]}
        elif kind == "section" and last and block["type"] == last["type"] == "section" \
                and "text" in block and "text" in last and "accessory" not in block and "accessory" not in last \
                and len(last["text"]["text"]) + len(block["text"]["text"]) + 2 <= SECTION_LIMIT:
            out[-1] = {"type": "section", "text": {"type": "mrkdwn",
                                                   "text": last["text"]["text"] + "\n\n" + block["text"]["text"]}}
        else:
            out.append(block)
    return out


def boxed(card: list[dict], title: str = HOME_TITLE, flat: bool = False) -> list[dict]:
    """The card inside a box (Slack's container block; emoji:False in its title — Slack turns an emoji
    into its :name: and the title cannot draw that back, a lone emoji showed as ":hourglass_flowing_sand:"): its title as the box's title, a line under
    it, everything else inside — the words on the box, not on Slack's background, and nothing ever
    folded under "Show more". A card with nothing under its title stays as it is."""
    children = list(card)
    subtitle = ""
    if children and children[0]["type"] == "header":
        title = children.pop(0)["text"]["text"]
        if children and children[0].get("block_id") == "subtitle":
            subtitle = children[0]["elements"][0]["text"]
            if len(subtitle) <= HEADER_LIMIT:
                children.pop(0)
            else:
                subtitle = ""
    elif children and children[0]["type"] == "section" and "text" in children[0]:
        first = children[0]["text"]["text"]
        if "\n" not in first and "<" not in first and len(_plain(first)) <= TITLE_SHOWN:
            title = _plain(children.pop(0)["text"]["text"])    # a one-line message: the line is the title
    if not children:
        return card
    if flat:                                                # a Slack whose boxes refuse headers: a bold line instead
        children = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{_escape(b['text']['text'])}*"}}
                    if b["type"] == "header" else b for b in children]
    parts = [children]
    if len(children) > BOX_CHILDREN:                      # more than a box holds: split at the headings — each heading its own box
        parts = _split_at_headings(children)
    chunks: list[list[dict]] = []
    for part in parts:
        chunks += _pack(part)                              # always boxes: what a box cannot hold goes on in the next one
    boxes = []
    for n, part in enumerate(chunks):
        name, small = (title, subtitle) if n == 0 else (f"{title} (continued)", "")
        if n and part and part[0]["type"] == "header":    # a later box is named by the heading it starts with
            name, part = part[0]["text"]["text"], part[1:]
        box = {"type": "container", "title": {"type": "plain_text", "text": (name or " ")[:HEADER_LIMIT], "emoji": False},
               "has_header_divider": True, "child_blocks": part}
        if small:
            box["subtitle"] = {"type": "mrkdwn", "text": small}
        boxes.append(box)
    return boxes


def _split_at_headings(children: list[dict]) -> list[list[dict]]:
    """The blocks as whole parts, each starting at a heading, packed into as few boxes as hold them. A part never ends or starts
    with a line (the box edge is the line)."""
    segments: list[list[dict]] = []
    for block in children:
        if block["type"] == "header" and segments and segments[-1]:
            segments.append([block])
        elif segments:
            segments[-1].append(block)
        else:
            segments.append([block])
    parts: list[list[dict]] = []
    for segment in segments:
        if parts and len(parts[-1]) + len(segment) <= BOX_CHILDREN:
            parts[-1] = parts[-1] + segment
        else:
            parts.append(list(segment))
    for part in parts:
        while part and part[-1]["type"] == "divider":
            part.pop()
        while part and part[0]["type"] == "divider":
            part.pop(0)
    return [part for part in parts if part]


def _pack(children: list[dict]) -> list[list[dict]]:
    """The blocks as one box when they fit (losing the least), else as boxes one after another: the words in order, the controls and the
    way back — under their lines — in the last one. Never blocks outside a box, never a message without one."""
    fitted = _fit(children)
    if fitted is not None:
        return [fitted]
    at = len(children)
    while at and children[at - 1]["type"] in ("actions", "divider"):
        at -= 1
    body, tail = children[:at], children[at:]
    chunks = [body[n:n + BOX_CHILDREN] for n in range(0, len(body), BOX_CHILDREN)] or [[]]
    if len(chunks[-1]) + len(tail) <= BOX_CHILDREN:
        chunks[-1] = chunks[-1] + tail
    else:
        chunks.append(tail)
    return [fit if (fit := _fit(chunk)) is not None else chunk[:BOX_CHILDREN] for chunk in chunks if chunk]


def _fit(children: list[dict]) -> list[dict] | None:
    """One box's blocks brought down to what a box holds, losing the least: the lines between parts first, then the line over
    the controls, then the last items' small details, then neighbours merged. None when it still does not fit."""
    children = list(children)

    def over() -> bool:
        return len(children) > BOX_CHILDREN

    def above_buttons(i: int) -> bool:                     # the line over the buttons: the one that always stays
        return i + 1 < len(children) and children[i + 1]["type"] == "actions" and (
            children[i + 1].get("block_id") == "nav" or i == 0 or children[i - 1]["type"] != "actions")

    if over():                                             # the lines between parts go first,
        children = [b for i, b in enumerate(children) if b["type"] != "divider" or above_buttons(i)]
    if over() and _foldable(children):                     # then the line over the controls (the one over the way back stays):
        children = [b for i, b in enumerate(children)      # an item's small details matter more than that line
                    if not (b["type"] == "divider" and above_buttons(i) and children[i + 1].get("block_id") != "nav")]
    while over() and _foldable(children):                  # then the LAST items' small details join their lines, one at a time
        children = _fold_small(children)
    for kind in ("actions", "section"):
        if over():
            children = _fewer(children, kind)
    return None if over() else children


CARD_TEXT = 200                    # Slack's limit for a card's body, and for its small print
CARD_BUTTONS = 3
_RULE = re.compile(r"^[━─]{6,}$")
_DETECTED = ("Detected", "Detected:")


def _small(line: str) -> str:
    return line.replace("*", "")


def notice(card: list[dict]) -> dict | None:
    """A notification as Slack's card — its title, the course under it, the details, the time small,
    up to three buttons on the right — when ALL of it fits one (Slack keeps a card short); None when
    something would not fit, or when it is a screen (it has the way back), so nothing is ever cut."""
    if not card or card[0]["type"] != "header":
        return None
    title, subtitle, groups, small, buttons = card[0]["text"]["text"], "", [], [], []
    for block in card[1:]:
        body: list[str] = []
        groups.append(body)
        kind = block["type"]
        if kind == "divider":
            continue
        if kind == "context":
            words = " ".join(e.get("text", "") for e in block.get("elements") or [])
            if block.get("block_id") == "subtitle":
                subtitle = words
            else:
                small.append(words)
        elif kind == "section" and block.get("fields"):
            for field in block["fields"]:
                label, _, value = field["text"].partition("\n")
                label = label.strip("*")
                if not subtitle and label.endswith("Course"):
                    subtitle = f"{label[:-len('Course')].strip()} {value}".strip()     # "📘 Big Data"
                elif label.endswith(_DETECTED):
                    small.append(f"{label}: {value}")
                else:
                    body.append(f"*{label}:* {value}")
        elif kind == "section" and "text" in block:
            for line in block["text"]["text"].split("\n"):
                if _RULE.match(line.strip()):
                    continue
                if "Detected:*" in line:
                    small.append(_small(line))
                else:
                    body.append(line)
        elif kind == "actions" and block.get("block_id") != "nav":
            buttons += block["elements"]
        else:
            return None                                  # the way back, a picture, anything else: a screen
    groups = ["\n".join(g) for g in groups if g]
    body_text = "\n\n".join(groups).strip()             # a blank line between the details and the words...
    if len(body_text) > CARD_TEXT:
        body_text = "\n".join(groups).strip()           # ...when there is room for it
    small_text = "\n".join(small).strip()
    if len(buttons) > CARD_BUTTONS or len(body_text) > CARD_TEXT or len(small_text) > CARD_TEXT \
            or len(title) > HEADER_LIMIT or len(subtitle) > HEADER_LIMIT:
        return None
    out = {"type": "card", "title": {"type": "plain_text", "text": title, "emoji": False}}
    if subtitle:
        out["subtitle"] = {"type": "mrkdwn", "text": subtitle}
    if body_text:
        out["body"] = {"type": "mrkdwn", "text": body_text}
    if small_text:
        out["subtext"] = {"type": "mrkdwn", "text": small_text}
    if buttons:
        out["actions"] = buttons
    return out


def footer() -> dict:
    stamp = int(time.time())
    return {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"Updated <!date^{stamp}^{{date_short_pretty}} at {{time}}|just now> · type *menu* in Messages "
            "for the same screens in the conversation"}]}


def home_view(text: str, reply_markup=None, box: bool = True) -> dict:
    """A screen of the bot as the app's Home page: the whole screen on one page, inside a box —
    nothing folded under "Show more", nothing piling up in the conversation — rewritten in place at
    every press. `box` False: the same screen without the box (for a Slack that refuses it)."""
    card = blocks(text, reply_markup, live=True) or [
        {"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn(text)[:SECTION_LIMIT] or " "}}]
    if box:
        card = boxed(card)
    elif card[0]["type"] != "header":
        card.insert(0, {"type": "header", "text": {"type": "plain_text", "text": HOME_TITLE, "emoji": True}})
    return {"type": "home", "blocks": card[:HOME_BLOCKS - 1] + [footer()]}


# ------------------------------------------------------------------ the API

def _wait_turn() -> None:
    global _last_sent
    with _pace:
        wait = MIN_INTERVAL - (time.monotonic() - _last_sent)
        if wait > 0:
            time.sleep(wait)
        _last_sent = time.monotonic()


def _post(url: str, **kwargs) -> requests.Response:
    """One HTTP POST over a connection kept open (one per thread): a fresh connection to Slack costs
    about a second before the call even starts — kept open, a press is answered in about half."""
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
    return session.post(url, **kwargs)


def call(token: str, method: str, data: dict | None = None, timeout=TIMEOUT) -> dict:
    """One Slack Web API call, tried again on a network failure, a server error or "slow down".
    Returns Slack's answer; raises SlackError when Slack refused it for good, OSError-like
    requests.RequestException when Slack could not be reached at all."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        delay = None
        try:
            resp = _post(API.format(method=method), data=data or {}, timeout=timeout,
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


def refused_layout(error: Exception) -> bool:
    """Slack refused the look of a message (its blocks), not the message."""
    return any(word in str(error) for word in ("block", "attachment", "invalid_arguments"))


_BOX_HEADERS = True                 # does this Slack take a header block inside a box? (found out the first time)


def _layouts(card: list[dict], colour: str) -> list[tuple[str, dict]]:
    """The card's looks, best first: in a box (every message — a notification too: Slack's compact card cut its
    buttons and its words); in a coloured frame (a Slack without boxes)."""
    looks = []
    headings = any(b["type"] == "header" for b in card[1:])
    flat = ("box-flat", {"blocks": json.dumps(boxed(card, flat=True), ensure_ascii=False), "attachments": "[]"})
    if headings and not _BOX_HEADERS:
        looks.append(flat)                                   # this Slack refused headers inside a box before: no second try
    else:
        looks.append(("box", {"blocks": json.dumps(boxed(card), ensure_ascii=False), "attachments": "[]"}))
        if headings:
            looks.append(flat)
    looks.append(("frame", framed(card, colour)))
    return looks


def _send_card(token: str, method: str, data: dict, card: list[dict] | None, colour: str) -> dict:
    """chat.postMessage / chat.update with the card in its best look Slack takes — and as text when
    it takes none, so a layout Slack does not take never costs the message."""
    global _BOX_HEADERS
    refused_box = False
    for name, layout in _layouts(card, colour) if card else []:
        try:
            sent = call(token, method, {**data, "text": preview(data["text"]), **layout})
            if name == "box-flat" and refused_box:
                _BOX_HEADERS = False                         # the box took it only without its headers: remembered
            return sent
        except SlackError as e:
            if not refused_layout(e):
                raise
            refused_box = refused_box or name == "box"
            log.warning("Slack refused the card's %s (%s)", name, e)
    if method == "chat.update":
        data = {**data, "blocks": "[]", "attachments": "[]"}
    return call(token, method, data)


def post(token: str, channel: str, text: str, card: list[dict] | None = None, colour: str = DEFAULT_TONE) -> dict:
    """A message: `card` (blocks, in a box) when given, `text` as what a notification shows."""
    _wait_turn()                                       # Slack's pace is for NEW messages only
    return _send_card(token, "chat.postMessage", {"channel": channel, "text": text, "mrkdwn": "true",
                                                  "unfurl_links": "false", "unfurl_media": "false"}, card, colour)


def update(token: str, channel: str, ts: str, text: str, card: list[dict] | None, colour: str = DEFAULT_TONE) -> dict:
    """A message already sent, rewritten in place (a screen that changes, buttons that go) — at once."""
    return _send_card(token, "chat.update", {"channel": channel, "ts": ts, "text": text}, card, colour)


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
        sent = _post(slot["upload_url"], data=payload, timeout=UPLOAD_TIMEOUT)
        if not sent.ok:
            raise requests.ConnectionError(f"Slack upload of a file failed (HTTP {sent.status_code})")
        shared.append({"id": slot["file_id"], "title": name})
    _wait_turn()
    data = {"files": json.dumps(shared), "channel_id": channel}
    if comment.strip():
        data["initial_comment"] = comment
    answer = call(token, "files.completeUploadExternal", data, timeout=UPLOAD_TIMEOUT)
    answer["file_ids"] = [f["id"] for f in shared]
    _await_share(token, channel, answer["file_ids"])
    return answer


SHARE_WAIT = 12.0          # seconds a shared file may take to appear in the conversation


def _await_share(token: str, channel: str, file_ids: list[str]) -> None:
    """Slack shares an uploaded file a moment AFTER confirming it: a message sent at once would land
    above the file (a menu above the timetable it belongs under). Wait until the file shows in the
    conversation — read with im:history; without it, a short pause does the same."""
    deadline = time.monotonic() + SHARE_WAIT
    while time.monotonic() < deadline:
        try:
            recent = call(token, "conversations.history", {"channel": channel, "limit": "5"})
        except SlackError:                             # no im:history (an app made before the buttons)
            time.sleep(2.5)
            return
        except requests.RequestException:
            return
        shown = {f.get("id") for m in recent.get("messages") or [] for f in m.get("files") or []}
        if set(file_ids) <= shown:
            return
        time.sleep(0.6)


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
