"""
The whole bot on Slack: its screens, its buttons, and what you type to it — the same bot as on
Telegram, through the same code.

On with NOTIFY_VIA=slack plus the app's own token for a live connection (Socket Mode):

    SLACK_APP_TOKEN=xapp-...     Basic Information -> App-Level Tokens (scope connections:write)
    SLACK_USER=U...              you, in the workspace (the setup finds it)

The bot speaks Telegram's Bot API (bot/chat.py and the flows). This module answers those calls in
Slack's words, and turns what happens in Slack into the updates Telegram would have sent:

  out   sendMessage / editMessageText / editMessageCaption / editMessageReplyMarkup / deleteMessage,
        a file sent or replaced (upload), answerCallbackQuery — a card with real buttons (slack.py),
        rewritten in place by chat.update, removed by chat.delete. Every message gets a number, as on
        Telegram, kept with its Slack timestamp in data/slack_messages.json (the bot and the
        15-minute check both send, so the file is shared, under a lock).
  in    a button pressed -> a callback_query carrying the button's callback data; a message you type
        -> a message ("menu", "tasks", "check" ... work as the Telegram commands: Slack itself takes a
        leading "/"). Both arrive over Socket Mode — a connection the bot opens to Slack, so nothing
        on this PC listens to the internet — and wait in a queue for the bot's own getUpdates loop.

Who: only SLACK_USER is the owner (handed to access.py as ADMIN_USER_ID); anybody else in the
workspace is a stranger to access.py, dropped and reported as on Telegram.
"""

import html
import json
import logging
import queue
import re
import threading
import time

import requests

from uninotifier import envcheck, fileio, paths, slack

log = logging.getLogger("uninotifier")

STORE = paths.DATA / "slack_messages.json"
KEEP = 600                          # messages remembered (a button older than that answers "not found")
_events: queue.Queue = queue.Queue()
_next_update = [int(time.time() * 1000)]      # update ids: always above any offset a previous run saved
_client_lock = threading.Lock()
_client = {"socket": None, "started": False}


# ------------------------------------------------------------------ settings

def app_token() -> str:
    return envcheck.value("SLACK_APP_TOKEN")


def owner() -> str:
    return envcheck.value("SLACK_USER")


def interactive() -> bool:
    """The bot itself lives in Slack: messages go there, and the live connection can be opened."""
    return slack.active() and bool(app_token()) and bool(owner())


def _settings() -> tuple[str, str]:
    found = slack.settings()
    if not found:
        raise slack.SlackError("not_set_up")
    return found


# ------------------------------------------------------------------ message numbers

def _load() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("messages"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"next": 1, "messages": {}}


def _save(data: dict) -> None:
    messages = data["messages"]
    if len(messages) > KEEP:
        for old in sorted(messages, key=int)[:len(messages) - KEEP]:
            del messages[old]
    fileio.write_json_atomic(STORE, data)


def remember(ts: str, text: str = "", markup=None, files: list[str] | None = None,
             names: list[str] | None = None) -> int:
    """A new message's number (as Telegram would give it), kept with what it shows."""
    with fileio.write_lock(STORE):
        data = _load()
        number = int(data.get("next") or 1)
        data["next"] = number + 1
        data["messages"][str(number)] = {"ts": ts, "text": text, "markup": _rows_json(markup), "files": files or [],
                                       "names": names or []}
        _save(data)
    return number


HOME_ID = 999_999_999              # the app's Home page, as the bot sees it: one screen, always there
HOME_TS = "home"


def entry(message_id) -> dict | None:
    try:
        found = _load()["messages"].get(str(int(message_id)))
    except (TypeError, ValueError):
        return None
    if found is None and str(message_id) == str(HOME_ID):
        return {"ts": HOME_TS, "text": "", "markup": "", "files": []}
    return found


def is_home(message_id) -> bool:
    return str(message_id) == str(HOME_ID)


def show_home(text: str, markup=None) -> dict:
    """A screen drawn on the Home page (views.publish), kept as the Home "message"."""
    token, _ = _settings()
    from uninotifier import slackhome

    def looks():                                     # best first: the dashboard (main menu only), a box, plain
        if slackhome.is_menu(text):
            yield "dashboard", lambda: slackhome.view(markup)
        yield "box", lambda: slack.home_view(text, markup, box=True)
        yield "plain", lambda: slack.home_view(text, markup, box=False)

    tried = list(looks())
    for n, (name, make) in enumerate(tried):
        try:
            view = make()
            slack.call(token, "views.publish", {"user_id": owner(), "view": json.dumps(view, ensure_ascii=False)})
            break
        except slack.SlackError as e:
            if n == len(tried) - 1 or not slack.refused_layout(e):
                raise
            log.warning("Slack refused the Home page's %s (%s)", name, e)
        except Exception:                            # a dashboard that cannot be built never costs the page
            if n == len(tried) - 1:
                raise
            log.exception("Slack: the Home page's %s could not be built", name)
    with fileio.write_lock(STORE):
        data = _load()
        data["messages"][str(HOME_ID)] = {"ts": HOME_TS, "text": text, "markup": _rows_json(markup), "files": [],
                                          "at": time.time()}
        _save(data)
    return {"message_id": HOME_ID, "chat": {"id": _chat_id()}, "text": text}


def home_menu() -> None:
    """The main menu on the Home page (when the bot starts, and when Home is opened with nothing on it)."""
    from uninotifier.bot import views
    text, rows = views.view_menu()
    show_home(text, {"inline_keyboard": rows})


def _change(message_id, **fields) -> None:
    with fileio.write_lock(STORE):
        data = _load()
        found = data["messages"].get(str(message_id))
        if found is not None:
            found.update(fields)
            _save(data)


def _forget(message_id) -> None:
    with fileio.write_lock(STORE):
        data = _load()
        if data["messages"].pop(str(message_id), None) is not None:
            _save(data)


def number_of(ts: str) -> int | None:
    for number, found in _load()["messages"].items():
        if found.get("ts") == ts:
            return int(number)
    return None


def _rows_json(markup) -> str:
    if not markup:
        return ""
    return markup if isinstance(markup, str) else json.dumps(markup, ensure_ascii=False)


# ------------------------------------------------------------------ out: what the bot sends

def send(text: str, markup=None, files: list[tuple[str, bytes]] | None = None) -> int:
    """A card with its buttons — and, when given, its files right under it, as one thing: if a file
    cannot follow, the card is taken back, so a retry never leaves a card twice. Its number."""
    token, channel = _settings()
    answer = slack.post(token, channel, slack.compose(text, markup), slack.blocks(text, markup, live=True),
                        slack.tone(text))
    ts, file_ids = answer["ts"], []
    if files:
        try:
            file_ids = slack.upload(token, channel, files).get("file_ids", [])
        except (slack.SlackError, requests.RequestException, OSError):
            slack.delete(token, channel, ts)
            raise
        _retire_old_copies(token, [name for name, _ in files], file_ids)
    return remember(ts, text, markup, file_ids, [name for name, _ in files] if files else None)


def _retire_old_copies(token: str, names: list[str], keep: list[str]) -> None:
    """A file sent again (the weekly timetable each time it is asked for) replaces its older copies
    in Slack's Files tab: they only fill the free plan's storage. Best effort — never raises."""
    try:
        with fileio.write_lock(STORE):
            data = _load()
            for found in data["messages"].values():
                if not set(found.get("names") or []) & set(names):
                    continue
                for file_id in [f for f in found.get("files") or [] if f not in keep]:
                    try:
                        slack.call(token, "files.delete", {"file": file_id})
                    except (slack.SlackError, requests.RequestException):
                        pass
                found["files"], found["names"] = [], []
            _save(data)
    except Exception as e:                                    # a tidy-up must never fail a send
        log.warning("Slack: old copies of a file were not removed: %s", e)


def _edit(message_id, text: str | None, markup, keep_markup: bool = False) -> dict | None:
    found = entry(message_id)
    if not found:
        return None
    token, channel = _settings()
    text = found.get("text", "") if text is None else text
    markup = found.get("markup") if keep_markup else markup
    if found.get("ts") == HOME_TS:                   # a screen on the Home page: the page is redrawn
        return show_home(text, markup)
    slack.update(token, channel, found["ts"], slack.compose(text, markup), slack.blocks(text, markup, live=True),
                 slack.tone(text))
    _change(message_id, text=text, markup=_rows_json(markup))
    return {"message_id": int(message_id), "chat": {"id": _chat_id()}, "text": text}


def _delete(message_id) -> bool:
    found = entry(message_id)
    if not found or found.get("ts") == HOME_TS:
        return True                                  # nothing of ours / the Home page stays: nothing to remove
    token, channel = _settings()
    for file_id in found.get("files") or []:
        try:
            slack.call(token, "files.delete", {"file": file_id})
        except (slack.SlackError, requests.RequestException):
            pass
    if not slack.delete(token, channel, found["ts"]):
        return False
    _forget(message_id)
    return True


def _chat_id() -> int:
    raw = envcheck.value("TELEGRAM_CHAT_ID") or envcheck.value("ADMIN_USER_ID") or "0"
    return int(raw) if raw.lstrip("-").isdigit() else 0


def _sent(message_id: int, text: str) -> dict:
    return {"message_id": message_id, "chat": {"id": _chat_id(), "type": "private"}, "date": int(time.time()),
            "text": text}


QUIET = {"sendChatAction", "setMyCommands", "setChatMenuButton", "deleteMyCommands", "setMyDescription"}


def call(method: str, data: dict, timeout: float = 20) -> object:
    """notifier.call_api's Telegram call, answered in Slack: the "result" Telegram would give, or None."""
    try:
        if method == "getUpdates":
            return get_updates(data.get("offset"), float(data.get("timeout") or 0))
        if method == "sendMessage":
            text = data.get("text", "")
            return _sent(send(text, data.get("reply_markup")), text)
        if method in ("editMessageText", "editMessageCaption"):
            text = data.get("text") if method == "editMessageText" else data.get("caption")
            return _edit(data.get("message_id"), text, data.get("reply_markup"))
        if method == "editMessageReplyMarkup":
            return _edit(data.get("message_id"), None, data.get("reply_markup"))
        if method == "deleteMessage":
            return True if _delete(data.get("message_id")) else None
        if method == "answerCallbackQuery":
            _toast(data.get("text"), bool(data.get("show_alert")))
            return True
        if method == "getMe":
            return {"id": 0, "is_bot": True, "first_name": "UniNotifier", "username": "UniNotifier"}
        if method in QUIET:
            return True
        log.warning("Slack: the bot's %s has no Slack form — skipped", method)
        return None
    except slack.SlackError as e:
        if "message_not_found" not in str(e) and "cant_update_message" not in str(e):
            log.warning("Slack refused the bot's %s: %s", method, e)
        return None
    except (requests.RequestException, OSError) as e:
        log.warning("Slack: the bot's %s failed: %s", method, type(e).__name__)
        return None


def upload(method: str, data: dict, field: str, file_name: str, payload: bytes) -> dict | None:
    """notifier.upload's Telegram call (a picture or a file sent, or replaced in its message)."""
    try:
        if method in ("sendPhoto", "sendDocument"):
            caption = data.get("caption", "")
            return _sent(send(caption, data.get("reply_markup"), files=[(file_name, payload)]), caption)
        if method == "editMessageMedia":
            message_id = data.get("message_id")
            found = entry(message_id)
            if not found:
                return None
            media = json.loads(data["media"]) if isinstance(data.get("media"), str) else (data.get("media") or {})
            token, channel = _settings()
            new_files = slack.upload(token, channel, [(file_name, payload)]).get("file_ids", [])
            for file_id in found.get("files") or []:          # the new file in, the old one out
                try:
                    slack.call(token, "files.delete", {"file": file_id})
                except (slack.SlackError, requests.RequestException):
                    pass
            result = _edit(message_id, media.get("caption", found.get("text", "")), data.get("reply_markup"))
            _change(message_id, files=new_files)
            return result
        log.warning("Slack: the bot's %s has no Slack form — skipped", method)
        return None
    except (slack.SlackError, requests.RequestException, OSError) as e:
        log.warning("Slack: the bot's %s failed: %s", method, e)
        return None


def _toast(text: str | None, alert: bool) -> None:
    """Telegram's toast after a press. Slack has none: a dialog's words (alert) are shown to you alone
    in the conversation; a passing "Updating…" is left out."""
    if not (text and alert):
        return
    token, channel = _settings()
    slack.call(token, "chat.postEphemeral", {"channel": channel, "user": owner(), "text": slack.mrkdwn(text)})


# ------------------------------------------------------------------ in: what happens in Slack

def _update_id() -> int:
    with _client_lock:
        _next_update[0] += 1
        return _next_update[0]


def _person(user: str) -> tuple[dict, dict]:
    """(from, chat) as Telegram shapes them: you are the owner access.py knows; anyone else is not."""
    if user and user == owner():
        me = _chat_id()
        return {"id": me, "is_bot": False, "first_name": "You"}, {"id": me, "type": "private"}
    return {"id": f"slack:{user}", "is_bot": False, "first_name": "someone in the workspace"}, \
           {"id": f"slack:{user}", "type": "private"}


COMMAND_WORD = re.compile(r"^/?([A-Za-z_]+)(\s.*)?$", re.S)
ALIASES = {"menu": "start", "home": "start", "main": "start"}


def command(text: str) -> str:
    """What you typed, as the bot reads it: a command word ("tasks", "menu", "need 85") with its "/" —
    Slack keeps a leading "/" for its own commands, so it is typed without one. Anything else (an
    answer the bot asked for, a reminder's words) is left exactly as typed."""
    from uninotifier.bot import app, gpa_flow, guest_flow, reminder_flow
    words = {name for name, _ in app.COMMANDS} | {"check", "help"}
    found = COMMAND_WORD.match(text.strip())
    asking = bool(reminder_flow._reminder_wait or gpa_flow._pending_prompt
                  or guest_flow._guest.get("step") in ("user", "password", "target"))
    if found and asking and found.group(2):
        return text                                  # the answer to the bot's question, words and all
    if found:
        word = found.group(1).lower()
        word = ALIASES.get(word, word)
        if word in words:
            return f"/{word}{found.group(2) or ''}"
    return text


def from_press(payload: dict) -> list[dict]:
    """A Socket Mode interactive payload (block_actions) as Telegram callback_query updates."""
    if payload.get("type") != "block_actions":
        return []
    user = (payload.get("user") or {}).get("id", "")
    container = payload.get("container") or {}
    if container.get("type") == "view" or (payload.get("view") or {}).get("type") == "home":
        ts, number = HOME_TS, HOME_ID                 # pressed on the Home page
    else:
        ts = container.get("message_ts") or (payload.get("message") or {}).get("ts")
        number = number_of(ts) if ts else None
    found = entry(number) if number else None
    sender, chat = _person(user)
    out = []
    for action in payload.get("actions") or []:
        if action.get("url") or not action.get("value"):
            continue                                      # a button that opens a page: Slack opens it
        message = {"message_id": number or 0, "chat": chat,
                   "date": int(time.time()) if ts == HOME_TS else int(float(ts or 0)),
                   "text": (found or {}).get("text", "")}
        if found and found.get("markup"):
            message["reply_markup"] = json.loads(found["markup"])
        out.append({"update_id": _update_id(),
                    "callback_query": {"id": f"slack-{action.get('action_ts') or time.time()}", "from": sender,
                                       "message": message, "chat_instance": "slack", "data": action["value"]}})
    return out


SCHEME = r"(?:https?://|mailto:|tel:)"
SLACK_LINK = re.compile(rf"<{SCHEME}(?:[^|>]*\|([^>]*)|([^>|]*))>")


def unlink(text: str) -> str:
    """What was typed, without Slack's auto-links: a bare number or address a person types arrives as
    "<http://202311988|202311988>" — the words they typed are the label (or the link, without its scheme)."""
    return html.unescape(SLACK_LINK.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(2), text))


def from_event(event: dict) -> list[dict]:
    """A Socket Mode event as Telegram updates: a message you typed to the app."""
    if event.get("type") != "message" or event.get("channel_type") != "im":
        return []
    if event.get("bot_id") or event.get("subtype"):
        return []                                         # the app's own messages, edits, joins
    text = unlink(event.get("text") or "")
    sender, chat = _person(event.get("user", ""))
    number = remember(event.get("ts", ""), text) if sender["id"] == _chat_id() else 0
    return [{"update_id": _update_id(),
             "message": {"message_id": number, "from": sender, "chat": chat,
                         "date": int(float(event.get("ts") or time.time())), "text": command(text)}}]


def take(kind: str, payload: dict) -> None:
    """What Socket Mode delivered, queued for the bot's loop. Never raises (it runs on Slack's thread)."""
    try:
        event = payload.get("event") or {}
        if kind == "events_api" and event.get("type") == "app_home_opened":
            if event.get("tab") == "home" and event.get("user") == owner() and _home_stale():
                threading.Thread(target=_home_menu_quietly, name="slack-home", daemon=True).start()
            return
        if kind == "interactive":
            updates = from_press(payload)
        elif kind == "events_api":
            updates = from_event(event)
        else:
            updates = []
        for update in updates:
            _events.put(update)
    except Exception:
        log.exception("Slack: could not read what Slack sent")


def _connect() -> None:
    """Open the live connection (once; it reconnects by itself)."""
    with _client_lock:
        if _client["started"]:
            return
        _client["started"] = True
    from slack_sdk.socket_mode.builtin import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web import WebClient
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    token, _ = _settings()

    def listener(client: SocketModeClient, req: SocketModeRequest) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))   # within 3 s, always
        take(req.type, req.payload or {})

    socket = SocketModeClient(app_token=app_token(), web_client=WebClient(token=token), auto_reconnect_enabled=True)
    socket.socket_mode_request_listeners.append(listener)
    socket.connect()
    _client["socket"] = socket
    log.info("Slack: connected — buttons and messages reach the bot")
    threading.Thread(target=_home_menu_quietly, name="slack-home", daemon=True).start()   # Home: fresh at start


HOME_FRESH = 60            # seconds a dashboard stays as drawn when Home is opened again


def _home_stale() -> bool:
    """Home is empty, or shows the main menu (the dashboard) drawn a while ago: draw it again, so
    opening Home always shows today as it is. A screen you were on (tasks, GPA) is left as it is."""
    from uninotifier import slackhome
    shown = _load()["messages"].get(str(HOME_ID)) or {}
    if not shown.get("text"):
        return True
    return slackhome.is_menu(shown["text"]) and time.time() - float(shown.get("at") or 0) > HOME_FRESH


def _home_menu_quietly() -> None:
    try:
        home_menu()
    except Exception:
        log.exception("Slack: the Home page could not be drawn")


def get_updates(offset: int | None, timeout: float) -> list[dict] | None:
    """Telegram's getUpdates, from Slack: what is waiting (or what arrives within `timeout` seconds)."""
    try:
        _connect()
    except Exception as e:
        with _client_lock:
            _client["started"] = False
        log.warning("Slack: the live connection could not be opened (%s)", type(e).__name__)
        time.sleep(min(timeout, 5))
        return None
    found = []
    try:
        found.append(_events.get(timeout=max(timeout, 0.05)))
        while True:
            found.append(_events.get_nowait())
    except queue.Empty:
        pass
    return [u for u in found if offset is None or u["update_id"] >= offset]
