"""
Who may use the bot: the owner, and nobody else.

The owner is ADMIN_USER_ID (.env): the Telegram user id of the person the bot belongs to. An update
is served only when ALL of these hold — it came from that user (`from.id`, not merely from a chat
that happens to carry the right number), the sender is not a bot, the conversation is a private
chat with that same id, and the message is not a forward. Anything else — a stranger who found the
bot, a group it was added to, a channel post — is dropped SILENTLY: no reply, no button answer,
nothing that shows the bot exists or what it can do.

A forward is someone else's words: forwarded into the chat, a "123456" would be taken for a
verification code, a "/guest" for a command. So a forwarded message is never acted on, even the
owner's own forward (that one is only logged — the owner is no intruder, so there is no alert).
The same holds for a message sent through an inline bot (via_bot): another bot wrote its text.

What the owner does get is told: the first attempt of each unknown id raises an alert (the id, the
username and name Telegram shows, what kind of update — never what was typed), at most once per
id per day and at most MAX_ALERTS_PER_DAY in all, so a flood cannot become a flood of alerts.

Fails closed: with no valid ADMIN_USER_ID nobody is served and the bot refuses to start (problems()).
Without ADMIN_USER_ID in .env the owner's TELEGRAM_CHAT_ID is used (in a private chat the two are the
same number) and that is logged.
"""

import logging
import os
import re
import time

from dotenv import load_dotenv

from uninotifier import failsafe, paths

log = logging.getLogger("uninotifier")

load_dotenv(paths.ENV_FILE)

ALERT_COOLDOWN = 24 * 3600          # per unknown id
MAX_ALERTS_PER_DAY = 5              # across all ids
LOG_EVERY = 3600                    # one log line per unknown id per hour
MAX_TRACKED = 1000                  # unknown ids remembered in memory (the oldest go first)
PRUNE_EVERY = 600                   # seconds between clean-ups of what is remembered
_ID = re.compile(r"[1-9][0-9]{0,15}")

from_chat_id = False                # True when ADMIN_USER_ID came from TELEGRAM_CHAT_ID


def _admin_from_env() -> str:
    global from_chat_id
    raw = (os.getenv("ADMIN_USER_ID") or "").strip()
    from_chat_id = not raw
    raw = raw or (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return raw if _ID.fullmatch(raw) else ""


ADMIN_USER_ID: str = _admin_from_env()

# All in memory: a stranger's update costs no disk access once its id has been handled. Only the
# first update of an id per ALERT_COOLDOWN reaches failsafe (which reads and writes its file).
_logged: dict[str, float] = {}      # id -> last log line
_handled: dict[str, float] = {}     # id -> when it last went to failsafe (alerted, or held back there)
_alerts: list[float] = []           # alerts sent in the last day (MAX_ALERTS_PER_DAY)
_pruned = {"memory": 0.0, "file": 0.0}


def problems(chat_id: str | None) -> list[str]:
    """What is wrong with the configuration (empty = fine). `chat_id` is where the bot answers
    (TELEGRAM_CHAT_ID): it must be the owner's own private chat."""
    found = []
    if not ADMIN_USER_ID:
        found.append("ADMIN_USER_ID in .env is missing or is not a Telegram user id (digits only)")
    elif str(chat_id or "") != ADMIN_USER_ID:
        found.append("TELEGRAM_CHAT_ID must be your private chat — the same number as ADMIN_USER_ID")
    return found


def sender(update: dict) -> tuple[dict, dict, str]:
    """(from, chat, kind) of a message or a button press; empty dicts when the update has none."""
    cb = update.get("callback_query")
    if cb:
        return cb.get("from") or {}, (cb.get("message") or {}).get("chat") or {}, "button press"
    msg = update.get("message") or {}
    return msg.get("from") or {}, msg.get("chat") or {}, "message"


# Bot API markers of words that are not the sender's own: forward_origin (7.0+), the older
# forward_* fields, a channel post copied into a linked group, and via_bot — a message sent from
# the owner's account through someone's inline bot (@gif, @vote, ...), whose text that bot wrote.
FORWARD_KEYS = ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name", "forward_date",
                "is_automatic_forward", "via_bot")


def is_forwarded(update: dict) -> bool:
    message = update.get("message") or {}
    return any(message.get(key) for key in FORWARD_KEYS)


def _from_owner(update: dict) -> bool:
    user, chat, _ = sender(update)
    return (bool(ADMIN_USER_ID) and not user.get("is_bot")
            and str(user.get("id", "")) == ADMIN_USER_ID
            and chat.get("type") == "private" and str(chat.get("id", "")) == ADMIN_USER_ID)


def is_owner(update: dict) -> bool:
    return _from_owner(update) and not is_forwarded(update)


def _label(user: dict) -> str:
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)[:40]
    parts = [f"id {user.get('id', '?')}"]
    if user.get("username"):
        parts.append(f"@{str(user['username'])[:32]}")
    if name:
        parts.append(f"“{name}”")
    return " · ".join(parts)


def deny(update: dict, now: float | None = None) -> None:
    """An update that is not the owner's: dropped without a trace towards the sender; logged
    (once an hour per id, without any text) and the owner told (rarely). Never raises."""
    try:
        now = time.time() if now is None else now
        if _from_owner(update):                       # the owner's own forward: ignored, no alert
            log.info("Ignored a forwarded message (forwards are never acted on)")
            return
        user, chat, kind = sender(update)
        if is_forwarded(update):
            kind = "forwarded message"
        who = str(user.get("id") or "unknown")
        if now - _logged.get(who, 0) >= LOG_EVERY:
            _logged[who] = now
            log.warning("Dropped a %s from an unauthorised sender (id %s, %s chat)", kind, who, chat.get("type", "?"))
        _prune(now)
        if now - _handled.get(who, float("-inf")) < ALERT_COOLDOWN:
            return                                    # this id was handled already: no disk access at all
        _alerts[:] = [t for t in _alerts if now - t < 86400]
        if len(_alerts) >= MAX_ALERTS_PER_DAY:
            return
        _handled[who] = now
        if failsafe.report(
                f"access:{who}", "Someone who is not you tried to use the bot",
                detail=f"{_label(user)} sent a {kind} in a {chat.get('type', 'unknown')} chat. I ignored it "
                       "and did not answer — they cannot tell the bot exists. Nothing they typed is kept.",
                cooldown=ALERT_COOLDOWN, wait=False):
            _alerts.append(now)
    except Exception:
        log.exception("Could not record an unauthorised update")


def _prune(now: float) -> None:
    """Keep the memory of unknown ids small: entries past their window go every PRUNE_EVERY seconds,
    and beyond MAX_TRACKED ids the oldest go at once (a flood of new ids cannot grow it). Once a day the
    expired access:* entries also leave failsafe.json."""
    for table, window in ((_logged, LOG_EVERY), (_handled, ALERT_COOLDOWN)):
        if now - _pruned["memory"] >= PRUNE_EVERY:
            for key in [k for k, t in table.items() if now - t >= window]:
                del table[key]
        if len(table) > MAX_TRACKED:
            for key in sorted(table, key=table.get)[:len(table) - MAX_TRACKED]:
                del table[key]
    if now - _pruned["memory"] >= PRUNE_EVERY:
        _pruned["memory"] = now
    if now - _pruned["file"] >= 86400:
        _pruned["file"] = now
        failsafe.forget("access:", ALERT_COOLDOWN, now)
