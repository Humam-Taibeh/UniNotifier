"""
The one door to api.telegram.org. Everything that talks to Telegram goes through
request(), which adds what the old one-shot requests.post() calls lacked:

  keep-alive      one pool of warm connections shared by every thread (a request on a warm
                  connection took 0.28 s and never failed in a 40-call
                  measurement; a new connection took 0.78 s and failed 1 in 40 —
                  the TCP/TLS handshake is the fragile part), with TCP keep-alive
                  probes so a quiet long-poll connection is not dropped on the way
  stale retry     a pooled connection the other side already closed is replaced by
                  a fresh one and the call repeated — only for calls that are safe
                  to repeat (never a send: a duplicate message is worse than a late one)
  rate limit      at most one send/edit per MIN_SEND_INTERVAL per chat (Telegram asks
                  for about one message per second per chat), so a burst of alerts
                  is paced instead of answered with HTTP 429
  429 handling    "retry after N seconds" is honoured once (N up to MAX_RETRY_AFTER)
  statistics      what happened to every call, per hour, in data/telegram_stats.json —
                  so a connection problem is measured, not guessed at
"""

import json
import logging
import random
import socket
import threading
import time

import requests
import urllib3.connection
import urllib3.connectionpool
from requests.adapters import HTTPAdapter

from uninotifier import fileio, paths

log = logging.getLogger("uninotifier")

API = "https://api.telegram.org"
CONNECT_TIMEOUT = 6              # seconds to open a connection (a healthy one takes < 1 s)
# urllib3 applies the connect timeout to WRITING the request too, so with 6 s an upload was aborted
# ("The write operation timed out") whenever one socket write waited 6 s on a busy uplink — seen in
# the logs and in 9 of 20 benchmark uploads. An upload's connection (_UploadConnection) therefore
# still CONNECTS within CONNECT_TIMEOUT — an outage is noticed in 6 s — and only then gets this long
# for writing the file.
UPLOAD_WRITE_TIMEOUT = 30
MIN_SEND_INTERVAL = 0.4          # seconds between two sends/edits to the same chat
MAX_RETRY_AFTER = 30             # a 429 asking for more than this is left to the caller
STATS_FILE = paths.DATA / "telegram_stats.json"
STATS_KEEP_HOURS = 72
STATS_FLUSH_EVERY = 300

# calls that are safe to repeat after a dropped connection (nothing is sent twice)
SAFE_TO_REPEAT = {"getUpdates", "getMe", "answerCallbackQuery", "editMessageText", "editMessageReplyMarkup",
                  "editMessageCaption", "editMessageMedia", "deleteMessage", "sendChatAction", "setMyCommands", "setChatMenuButton"}
# calls the rate limit paces (they put something in the chat)
PACED = {"sendMessage", "sendDocument", "sendPhoto", "sendMediaGroup", "editMessageText",
         "editMessageReplyMarkup", "editMessageCaption", "editMessageMedia", "deleteMessage"}


def _keepalive_options() -> list:
    options = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1), (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (("TCP_KEEPIDLE", 15), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 4)):
        if hasattr(socket, name):
            options.append((socket.IPPROTO_TCP, getattr(socket, name), value))
    return options


class _WriteTimeout:
    """Mixed into an urllib3 connection: connect under the pool's connect timeout, then write the
    request under UPLOAD_WRITE_TIMEOUT. (urllib3 sets `timeout` to the connect timeout before
    connecting and keeps it for the write, and request() hands it to the socket; the read timeout
    is set afterwards by the pool, as usual.)"""

    def request(self, *args, **kwargs):
        if self.sock is None:
            self.connect()                        # still under the connect timeout
        self.timeout = UPLOAD_WRITE_TIMEOUT
        return super().request(*args, **kwargs)


class _UploadConnection(_WriteTimeout, urllib3.connection.HTTPSConnection):
    pass


class _UploadPool(urllib3.connectionpool.HTTPSConnectionPool):
    ConnectionCls = _UploadConnection


class _Adapter(HTTPAdapter):
    """keep-alive probes on every socket; `send_buffer` sets SO_SNDBUF; `upload` gives the
    connections the long write timeout (_UploadConnection)."""

    def __init__(self, *args, send_buffer: int = 0, upload: bool = False, **kwargs):
        self.send_buffer, self.upload = send_buffer, upload       # set before HTTPAdapter builds its pool manager
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        options = _keepalive_options()
        if self.send_buffer:
            options.append((socket.SOL_SOCKET, socket.SO_SNDBUF, self.send_buffer))
        kwargs["socket_options"] = options
        super().init_poolmanager(connections, maxsize, block=block, **kwargs)
        if self.upload:
            self.poolmanager.pool_classes_by_scheme = {**self.poolmanager.pool_classes_by_scheme, "https": _UploadPool}


# Connections kept open for reuse: the long poll holds one for its 20 s, the button-answer
# workers, a chat action, a send or an alert take the others. More calls at once still work —
# the extra connections are simply not kept.
POOL_SIZE = 8

_session_lock = threading.Lock()
_shared: requests.Session | None = None


def _session() -> requests.Session:
    """The one session every thread shares. Its connection pool (urllib3) is thread-safe: a call
    borrows an idle warm connection, or opens one when all are busy, and gives it back. So a
    thread started for a single call — a chat action, a portal or guest run, a failsafe alert —
    reuses a warm connection instead of paying a TLS handshake of its own (each thread used to
    have its own session: measured ~0.5 s and the occasional 6 s handshake stall per thread)."""
    global _shared
    session = _shared
    if session is None:
        with _session_lock:
            if _shared is None:
                _shared = requests.Session()
                _shared.mount("https://", _Adapter(pool_connections=1, pool_maxsize=POOL_SIZE, max_retries=0))
            session = _shared
    return session


# Uploads (documents, screenshots, albums) go through a session of their own: its connections write
# under UPLOAD_WRITE_TIMEOUT (_UploadConnection), and its sockets may get a larger send buffer —
# Windows' default one keeps few bytes in flight on a long path. UPLOAD_SEND_BUFFER 0 = the system's default. Measure before changing it: python -m
# uninotifier.tools.upload_benchmark (setting it switches off Windows' own send-buffer tuning for
# that socket). OFF because the measurement did not show a speed-up. 2026-10-20 00:04, 3 MB x 10
# per variant, alternated, same fresh connection and 30 s write timeout for both:
#   default  median 8.0 s, p90 12.0 s, 3 of 10 failed (writes stalled 30 s), best 2.1 s (~1.4 MB/s)
#   1 MB     median 6.5 s, p90  7.0 s, 0 of 10 failed, never faster than ~0.46 MB/s
# 1 MB traded peak speed for steadiness on a congested uplink (13 of 28 vs 5 of 28 failures over
# three runs). Worth measuring again at a quieter hour, and with 2 MB, before deciding.
UPLOAD_SEND_BUFFER = 0
_uploads: dict[int, requests.Session] = {}


def _upload_session(send_buffer: int | None = None) -> requests.Session:
    """The session for calls that carry files (connect in CONNECT_TIMEOUT, write in UPLOAD_WRITE_TIMEOUT),
    one per send-buffer size — 0 is the system's default (the benchmark compares two)."""
    size = UPLOAD_SEND_BUFFER if send_buffer is None else send_buffer
    with _session_lock:
        session = _uploads.get(size)
        if session is None:
            session = _uploads[size] = requests.Session()
            session.mount("https://", _Adapter(pool_connections=1, pool_maxsize=2, max_retries=0,
                                               send_buffer=size, upload=True))
        return session


def reset_session() -> None:
    """A call failed on its connection: the next calls start on a fresh pool, so no other idle
    connection that died the same way is reused. Closing the old pool closes only its idle
    connections — a call another thread is making right now finishes normally."""
    global _shared
    with _session_lock:
        old = [s for s in (_shared, *_uploads.values()) if s is not None]
        _shared = None
        _uploads.clear()
    for session in old:
        try:
            session.close()
        except Exception:
            pass


def post(url: str, **kwargs) -> requests.Response:
    """requests.post on the shared keep-alive session — the lowest door (tests replace it). A call
    that carries files goes through the upload session (UPLOAD_SEND_BUFFER)."""
    return (_upload_session() if kwargs.get("files") else _session()).post(url, **kwargs)


# ------------------------------------------------------------------ statistics

_stats_lock = threading.Lock()
_stats: dict = {}
_last_flush = 0.0


def stalled_before_sending(exc: BaseException, read_timeout: float) -> bool:
    """The call never left: it timed out opening the connection, or in the TLS handshake of a new one.
    The handshake shows as a ReadTimeout whose limit is the CONNECT timeout, not the call's read timeout
    ("read timeout=6" for a call that was given 20) — nothing was sent, so repeating even a send is safe."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    return (isinstance(exc, requests.ReadTimeout) and read_timeout != CONNECT_TIMEOUT
            and f"read timeout={CONNECT_TIMEOUT})" in str(exc))


def label(exc: BaseException) -> str:
    """A short name for what went wrong: 'ReadTimeout', 'ConnectTimeout', 'ConnectionReset', ..."""
    text = repr(exc)
    if isinstance(exc, requests.ConnectTimeout):
        return "ConnectTimeout"
    if isinstance(exc, requests.ReadTimeout):
        return "HandshakeTimeout" if f"read timeout={CONNECT_TIMEOUT})" in text else "ReadTimeout"
    if "10054" in text or "ConnectionResetError" in text:
        return "ConnectionReset"
    if "RemoteDisconnected" in text:
        return "RemoteDisconnected"
    return type(exc).__name__


def _record(method: str, outcome: str) -> None:
    global _last_flush
    hour = time.strftime("%Y-%m-%dT%H")
    key = f"{'poll' if method == 'getUpdates' else 'api'}:{outcome}"
    with _stats_lock:
        bucket = _stats.setdefault(hour, {})
        bucket[key] = bucket.get(key, 0) + 1
        if time.time() - _last_flush >= STATS_FLUSH_EVERY:
            _last_flush = time.time()
            _flush_locked()


def _flush_locked() -> None:
    try:
        try:
            disk = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            disk = {}
        cutoff = time.strftime("%Y-%m-%dT%H", time.localtime(time.time() - STATS_KEEP_HOURS * 3600))
        for hour in [h for h in _stats if h < cutoff]:
            del _stats[hour]                  # the always-on bot would otherwise keep every hour it ever ran
        merged = {}
        for hour in set(disk) | set(_stats):
            if hour < cutoff:
                continue
            merged[hour] = {k: max(disk.get(hour, {}).get(k, 0), _stats.get(hour, {}).get(k, 0))
                            for k in set(disk.get(hour, {})) | set(_stats.get(hour, {}))}
        fileio.write_json_atomic(STATS_FILE, merged)
    except OSError:
        log.warning("Could not write %s", STATS_FILE.name)


def flush_stats() -> None:
    with _stats_lock:
        _flush_locked()


def summary(hours: int = 24) -> dict[str, int]:
    """Totals per outcome over the last `hours` hours, e.g. {'poll:ok': 3100, 'poll:ReadTimeout': 40}."""
    flush_stats()
    try:
        disk = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cutoff = time.strftime("%Y-%m-%dT%H", time.localtime(time.time() - hours * 3600))
    totals: dict[str, int] = {}
    for hour, bucket in disk.items():
        if hour >= cutoff:
            for key, n in bucket.items():
                totals[key] = totals.get(key, 0) + n
    return totals


# ------------------------------------------------------------------ pacing

_pace_lock = threading.Lock()
_next_slot: dict[str, float] = {}


def _wait_for_slot(method: str, chat_id) -> None:
    """Reserve this chat's next sending slot and sleep until it comes (at most MIN_SEND_INTERVAL
    per queued call; a quiet chat never waits)."""
    if method not in PACED or not MIN_SEND_INTERVAL or chat_id is None:
        return
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(str(chat_id), 0.0))
        _next_slot[str(chat_id)] = slot + MIN_SEND_INTERVAL
    if slot > now:
        time.sleep(slot - now)


def backoff_seconds(failures: int) -> float:
    """Pause after `failures` failed attempts in a row: 0.5, 1, 2, 4, then 8 s, with jitter."""
    return min(8.0, 0.5 * 2 ** max(0, failures - 1)) * random.uniform(0.8, 1.2)


# ------------------------------------------------------------------ the door

def _in_memory(files: dict | None) -> dict | None:
    """requests reads a file object while it builds the request, so a call that is tried again
    would send it empty (Telegram: "file must be non-empty"). The contents are read once, here,
    and every try sends the same bytes."""
    if not files:
        return files
    return {field: (value[0], value[1].read(), *value[2:])
            if isinstance(value, tuple) and len(value) > 1 and hasattr(value[1], "read") else value
            for field, value in files.items()}


def request(token: str, method: str, *, json=None, data=None, files=None, timeout=20) -> requests.Response:
    """One Bot API call. `timeout` is the read timeout in seconds (or a (connect, read)
    pair). Raises requests exceptions exactly as requests.post did."""
    url = f"{API}/bot{token}/{method}"
    timeouts = timeout if isinstance(timeout, tuple) else (CONNECT_TIMEOUT, timeout)
    _wait_for_slot(method, (json or data or {}).get("chat_id"))
    files = _in_memory(files)
    tries_allowed = 2 if method in SAFE_TO_REPEAT and not files else 1
    tries, slowed_down, reconnected = 0, False, False
    while True:
        tries += 1
        try:
            resp = post(url, json=json, data=data, files=files, timeout=timeouts)
        except requests.ConnectionError as e:
            reset_session()                                   # never reuse a connection that just failed
            _record(method, label(e))
            if tries < tries_allowed:
                continue                                      # once more, on a fresh connection
            if stalled_before_sending(e, timeouts[1]) and not reconnected:
                reconnected = True                            # nothing was sent: any call may go again, once
                continue
            raise
        except requests.RequestException as e:
            reset_session()
            _record(method, label(e))
            # a timeout (often the TLS handshake of a new connection) on a call that is harmless to
            # repeat is repeated once - but never a long poll, whose retry is the polling loop's job
            if isinstance(e, requests.Timeout) and tries < tries_allowed and method != "getUpdates":
                continue
            if stalled_before_sending(e, timeouts[1]) and not reconnected and method != "getUpdates":
                reconnected = True                            # a stalled handshake: nothing was sent, so even a send may go again
                continue
            raise
        if resp.status_code == 429 and not files and not slowed_down:
            try:
                wait = float(resp.json().get("parameters", {}).get("retry_after", 0))
            except (ValueError, AttributeError):
                wait = 0
            if 0 < wait <= MAX_RETRY_AFTER:
                slowed_down = True
                _record(method, "http_429")
                log.warning("Telegram asked to slow down (%s) — waiting %.0f s", method, wait)
                time.sleep(wait + 0.5)
                continue
        _record(method, "ok" if resp.ok else f"http_{resp.status_code}")
        return resp
