"""Talking to Telegram: sending, uploading, polling, slowing down, restarts."""

from support import (_fresh_failsafe, OUTBOX, _package, _REAL_POST)

import json
import logging
import os
import socket
import tempfile
import time
from pathlib import Path

from uninotifier import failsafe
from uninotifier import fileids
from uninotifier import heartbeat
from uninotifier import notifier
from uninotifier import paths
from uninotifier import telegram


def test_notifier_helpers():
    token = "1234567890:FAKEfakeFAKEfakeFAKEfakeFAKEfake123"
    line = f"HTTPSConnectionPool url: /bot{token}/sendMessage"
    assert token not in notifier.redact(line) and "/bot<redacted>/sendMessage" in notifier.redact(line)
    chunks = notifier.split_text("\n".join(f"line {n} " + "x" * 50 for n in range(300)), 1000)
    assert all(len(c) <= 1000 for c in chunks) and len(chunks) > 10
    assert "\n".join(chunks).count("line ") == 300
    assert notifier.split_text("y" * 2500, 1000) == ["y" * 1000, "y" * 1000, "y" * 500]
    kb = notifier._keyboard("https://p", [[{"text": "✅ Mark as Done", "callback_data": "done:1:1"}]])
    assert '"done:1:1"' in kb and kb.index("done:1:1") < kb.index("https://p")


def test_telegram_transport():
    """The one door to Telegram: connections are reused, a dead pooled connection is replaced
    (but a send is never repeated), sends are paced, a 429 is honoured once, and every call
    is counted so a connection problem can be measured."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import requests

    class Reply:
        def __init__(self, status=200, body=None):
            self.status_code, self._body = status, body or {"ok": True}
            self.ok = status < 400

        def json(self):
            return self._body

    def reset_stats():
        telegram._stats.clear()
        telegram.STATS_FILE.unlink(missing_ok=True)

    sleeps, calls = [], []
    keep = (telegram.post, telegram.time.sleep, telegram.MIN_SEND_INTERVAL)
    telegram.time.sleep = sleeps.append
    try:
        # ok / bookkeeping: polls and other calls are counted apart
        reset_stats()
        telegram.post = lambda url, **kw: calls.append((url, kw)) or Reply()
        assert telegram.request("T", "getMe").ok and telegram.request("T", "getUpdates", json={"timeout": 5}, timeout=15).ok
        assert calls[0][0].endswith("/botT/getMe") and calls[1][1]["timeout"] == (telegram.CONNECT_TIMEOUT, 15)
        hour = time.strftime("%Y-%m-%dT%H")
        assert telegram._stats[hour] == {"api:ok": 1, "poll:ok": 1}, telegram._stats

        # a pooled connection that died: a call that is safe to repeat is repeated once, on a fresh connection
        calls.clear()
        outcomes = [requests.ConnectionError("Connection aborted: ConnectionResetError(10054, 'forcibly closed')"), Reply()]

        def flaky(url, **kw):
            calls.append(url)
            out = outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        telegram.post = flaky
        assert telegram.request("T", "answerCallbackQuery", json={"callback_query_id": "1"}).ok and len(calls) == 2
        assert telegram._stats[hour]["api:ConnectionReset"] == 1

        # ... but a send is never repeated by the transport (a duplicate message is worse than a late one)
        calls.clear()
        outcomes[:] = [requests.ConnectionError("boom"), Reply()]
        try:
            telegram.request("T", "sendMessage", json={"chat_id": 1, "text": "x"})
            raise AssertionError("must raise")
        except requests.ConnectionError:
            pass
        assert len(calls) == 1, "sendMessage was repeated"
        # a read timeout is not repeated either (the call may have been processed)
        calls.clear()
        outcomes[:] = [requests.ReadTimeout("slow"), Reply()]
        try:
            telegram.request("T", "getUpdates", json={})
            raise AssertionError("must raise")
        except requests.ReadTimeout:
            pass
        assert len(calls) == 1 and telegram._stats[hour]["poll:ReadTimeout"] == 1
        # ... while a timeout on a call that is harmless to repeat (often the handshake of a new connection) is repeated once
        calls.clear()
        outcomes[:] = [requests.ReadTimeout("handshake"), Reply()]
        assert telegram.request("T", "setMyCommands", json={"commands": []}).ok and len(calls) == 2
        calls.clear()
        outcomes[:] = [requests.ReadTimeout("a"), requests.ReadTimeout("b")]
        try:
            telegram.request("T", "setMyCommands", json={})
            raise AssertionError("must raise")
        except requests.ReadTimeout:
            pass
        assert len(calls) == 2, "once more, not forever"
        calls.clear()
        outcomes[:] = [requests.ReadTimeout("slow"), Reply()]
        try:
            telegram.request("T", "sendMessage", json={"chat_id": 1})
            raise AssertionError("must raise")
        except requests.ReadTimeout:
            pass
        assert len(calls) == 1, "a send is never repeated"
        # ... unless it never left: a stalled handshake (a ReadTimeout whose limit is the CONNECT timeout, not
        # the call's own) or a connect timeout means nothing was sent, so even a send goes again - once
        calls.clear()
        hs = requests.ReadTimeout(f"HTTPSConnectionPool(host='api.telegram.org', port=443): Read timed out. (read timeout={telegram.CONNECT_TIMEOUT})")
        outcomes[:] = [hs, Reply()]
        assert telegram.request("T", "sendMessage", json={"chat_id": 1}, timeout=20).ok and len(calls) == 2
        assert telegram._stats[hour]["api:HandshakeTimeout"] == 1
        calls.clear()
        outcomes[:] = [requests.ConnectTimeout("no route"), Reply()]
        assert telegram.request("T", "sendMessage", json={"chat_id": 1}).ok and len(calls) == 2
        calls.clear()
        outcomes[:] = [hs, hs, Reply()]
        try:
            telegram.request("T", "sendMessage", json={"chat_id": 1}, timeout=20)
            raise AssertionError("must raise")
        except requests.ReadTimeout:
            pass
        assert len(calls) == 2, "once more, not forever"
        calls.clear()
        outcomes[:] = [requests.ReadTimeout("Read timed out. (read timeout=20)"), Reply()]
        try:
            telegram.request("T", "sendMessage", json={"chat_id": 1}, timeout=20)
            raise AssertionError("must raise")
        except requests.ReadTimeout:
            pass
        assert len(calls) == 1, "a read timeout of the call itself may mean it was processed: never repeated"
        assert telegram.stalled_before_sending(hs, 20) and not telegram.stalled_before_sending(hs, telegram.CONNECT_TIMEOUT)

        # 429: "retry after N" is honoured once (and not for an upload, whose file is already read)
        sleeps.clear()
        outcomes[:] = [Reply(429, {"ok": False, "parameters": {"retry_after": 3}}), Reply()]
        telegram.post = flaky
        assert telegram.request("T", "sendMessage", json={"chat_id": 1}).ok and sleeps == [3.5]
        sleeps.clear()
        outcomes[:] = [Reply(429, {"ok": False, "parameters": {"retry_after": 99}})]
        assert telegram.request("T", "sendMessage", json={"chat_id": 1}).status_code == 429 and sleeps == [], "too long: left to the caller"
        outcomes[:] = [Reply(429, {"ok": False, "parameters": {"retry_after": 2}}), Reply()]
        assert telegram.request("T", "sendDocument", data={"chat_id": 1}, files={"f": b"x"}).status_code == 429

        # pacing: back-to-back sends to one chat are spaced; another chat, or a call that sends nothing, is not
        sleeps.clear()
        telegram.MIN_SEND_INTERVAL = 0.4
        telegram._next_slot.clear()
        telegram.post = lambda url, **kw: Reply()
        telegram.request("T", "sendMessage", json={"chat_id": 7, "text": "a"})
        telegram.request("T", "sendMessage", json={"chat_id": 7, "text": "b"})
        telegram.request("T", "sendMessage", json={"chat_id": 8, "text": "c"})
        telegram.request("T", "answerCallbackQuery", json={"callback_query_id": "1"})
        # Windows' monotonic clock ticks every ~16 ms: two sends in one tick wait the full 0.4 s, give or take a float's last bit
        assert len(sleeps) == 1 and 0.2 < sleeps[0] <= 0.4 + 1e-6, sleeps
    finally:
        telegram.post, telegram.time.sleep, telegram.MIN_SEND_INTERVAL = keep

    # backoff: 0.5, 1, 2, 4, then 8 s (with jitter), never more
    waits = [telegram.backoff_seconds(n) for n in (1, 2, 3, 4, 5, 20)]
    assert waits == sorted(waits) or waits[4] <= waits[5] * 1.5
    assert 0.39 <= waits[0] <= 0.61 and all(w <= 9.7 for w in waits) and waits[5] >= 6.3

    # statistics: summed over the window, old hours dropped
    reset_stats()
    telegram._stats[time.strftime("%Y-%m-%dT%H")] = {"poll:ok": 5, "poll:ReadTimeout": 1}
    telegram.STATS_FILE.write_text(json.dumps({"2020-01-01T00": {"poll:ok": 99}}), encoding="utf-8")
    assert telegram.summary(24) == {"poll:ok": 5, "poll:ReadTimeout": 1}
    assert "2020-01-01T00" not in json.loads(telegram.STATS_FILE.read_text(encoding="utf-8"))
    reset_stats()

    # the real session: ONE shared by every thread, keep-alive probes on, and ONE TCP connection
    # for many requests — also when each request comes from a thread of its own
    assert (socket_opts := dict(((lvl, opt), val) for lvl, opt, val in telegram._keepalive_options()))
    import socket
    assert socket_opts[(socket.SOL_SOCKET, socket.SO_KEEPALIVE)] == 1
    seen = {}
    t = threading.Thread(target=lambda: seen.update(other=telegram._session()))
    t.start()
    t.join()
    mine = telegram._session()
    assert mine is telegram._session() and seen["other"] is mine, "every thread shares the one session"
    telegram.reset_session()
    assert telegram._session() is not mine

    connections, slow = [], threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            connections.append(1)
            super().setup()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path.endswith("/slow"):
                slow.wait(5)                          # held open until the test has reset the pool
            body = b'{"ok": true, "result": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/botT/getMe"
        telegram.reset_session()
        for _ in range(5):
            assert _REAL_POST(url, json={}, timeout=5).ok
        assert len(connections) == 1, f"5 requests opened {len(connections)} connections"
        # five short-lived threads (chat actions, workers, alerts): still that one warm connection
        for _ in range(5):
            worker = threading.Thread(target=lambda: _REAL_POST(url, json={}, timeout=5))
            worker.start()
            worker.join()
        assert len(connections) == 1, f"5 threads opened {len(connections) - 1} new connection(s)"
        connections.clear()
        for _ in range(3):
            requests.post(url, json={}, timeout=5)          # what the old code did
        assert len(connections) == 3
        # a reset while another thread's call is in flight: that call still finishes normally
        outcome = {}
        busy = threading.Thread(target=lambda: outcome.update(ok=_REAL_POST(url[:-len("getMe")] + "slow", json={}, timeout=10).ok))
        busy.start()
        time.sleep(0.3)
        telegram.reset_session()
        slow.set()
        busy.join(10)
        assert outcome == {"ok": True}, outcome
    finally:
        server.shutdown()
        telegram.reset_session()


def test_files_telegram_already_holds_are_not_uploaded_again():
    """A document is uploaded once; afterwards its file_id (remembered by content) sends it with no
    upload at all. A changed file is a new file, a file_id Telegram refuses is dropped and the file
    uploaded again, a network failure neither uploads nor forgets, and the timetable album is covered."""
    import requests

    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-fileids-"))
    pdf, png = tmp / "plan.pdf", tmp / "plan.png"
    pdf.write_bytes(b"%PDF-" + b"a" * 5000)
    png.write_bytes(b"\x89PNG" + b"b" * 9000)

    class Reply:
        def __init__(self, status=200, body=None, text=""):
            self.status_code, self._body, self.text = status, body or {"ok": True, "result": True}, text
            self.ok = status < 400

        def json(self):
            return self._body

    sent, state = [], {"mode": "ok", "ids": iter(range(1, 10 ** 6))}

    def post(url, json=None, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        uploaded = sum(len(v[1]) if isinstance(v[1], (bytes, bytearray)) else os.fstat(v[1].fileno()).st_size
                       for v in (files or {}).values())          # an album holds bytes, a single file an open handle
        sent.append({"method": method, "data": data, "files": files, "bytes": uploaded})
        if state["mode"] == "down":
            raise requests.ConnectionError("network down")
        if state["mode"] == "stale":
            refs = [m["media"] for m in __import__("json").loads(data["media"])] if method == "sendMediaGroup" else [data.get("document", "attach")]
            if any(not str(r).startswith("attach") and r != "attach" for r in refs):
                return Reply(400, {"ok": False}, '{"ok":false,"description":"Bad Request: wrong file identifier/HTTP URL specified"}')
        if method == "sendMediaGroup":
            n = len(__import__("json").loads(data["media"]))
            return Reply(body={"ok": True, "result": [{"document": {"file_id": f"ID{next(state['ids'])}"}} for _ in range(n)]})
        return Reply(body={"ok": True, "result": {"document": {"file_id": f"ID{next(state['ids'])}"}}})

    keep = (telegram.post, notifier.time.sleep)
    telegram.post, notifier.time.sleep = post, lambda s: None
    fileids.STORE.unlink(missing_ok=True)
    try:
        def album(items=None):
            sent.clear()
            return notifier.send_document_group("T", "1", items or [(pdf, "📎 PDF"), (png, "🖼️ PNG")])

        # first time: both are uploaded and remembered by content
        assert album() and sent[0]["bytes"] == 5005 + 9004 and set(sent[0]["files"]) == {"f0", "f1"}
        assert len(json.loads(fileids.STORE.read_text(encoding="utf-8"))) == 2
        # second time: not a byte uploaded, the album refers to the file_ids
        assert album() and sent[0]["bytes"] == 0 and sent[0]["files"] is None
        media = json.loads(sent[0]["data"]["media"])
        assert [m["media"] for m in media] == ["ID1", "ID2"] and [m["caption"] for m in media] == ["📎 PDF", "🖼️ PNG"]
        # the same content under another name and path is the same file
        copy = tmp / "other-name.pdf"
        copy.write_bytes(pdf.read_bytes())
        assert fileids.fingerprint(copy) == fileids.fingerprint(pdf) and fileids.fingerprint(tmp / "nope") is None
        # a changed file is uploaded again, the unchanged one still is not
        png.write_bytes(b"\x89PNG" + b"c" * 9000)
        assert album() and sent[0]["bytes"] == 9004 and set(sent[0]["files"]) == {"f1"}
        assert [m["media"] for m in json.loads(sent[0]["data"]["media"])] == ["ID1", "attach://f1"]
        assert album() and sent[0]["bytes"] == 0, "and its new file_id is remembered too"

        # Telegram refuses a file_id: they are forgotten and uploaded afresh, once
        state["mode"] = "stale"
        assert album() and [s["bytes"] for s in sent] == [0, 5005 + 9004], [s["bytes"] for s in sent]
        state["mode"] = "ok"
        assert album() and sent[0]["bytes"] == 0, "the fresh ids work"

        # the network is down: nothing is uploaded and nothing is forgotten
        state["mode"] = "down"
        assert not album() and all(s["bytes"] == 0 for s in sent) and len(sent) == notifier.MAX_ATTEMPTS
        state["mode"] = "ok"
        assert album() and sent[0]["bytes"] == 0

        # one document
        sent.clear()
        assert notifier.send_document("T", "1", pdf, "cap", rows=[[{"text": "x", "callback_data": "y"}]])
        assert sent[0]["data"]["document"] == fileids.lookup(fileids.fingerprint(pdf)) and sent[0]["bytes"] == 0
        assert "reply_markup" in sent[0]["data"]
        fileids.STORE.unlink()
        sent.clear()
        assert notifier.send_document("T", "1", pdf, "cap") and sent[0]["bytes"] == 5005 and "document" not in sent[0]["data"]
        assert notifier.send_document("T", "1", pdf, "cap") and sent[1]["bytes"] == 0
        state["mode"] = "stale"
        sent.clear()
        assert notifier.send_document("T", "1", pdf, "cap") and [s["bytes"] for s in sent] == [0, 5005]
        state["mode"] = "ok"

        # the timetable: the second request uploads nothing
        bot = _package("uninotifier.bot")
        keep_api = bot.api
        bot.api = lambda method, **d: {"message_id": 1} if method == "sendMessage" else True
        fileids.STORE.unlink(missing_ok=True)
        try:
            sent.clear()
            bot.send_schedule_files()
            first = sum(s["bytes"] for s in sent if s["method"] == "sendMediaGroup")
            sent.clear()
            bot.send_schedule_files()
            again = sum(s["bytes"] for s in sent if s["method"] == "sendMediaGroup")
        finally:
            bot.api = keep_api
        assert first == paths.SCHEDULE_PDF.stat().st_size + paths.SCHEDULE_IMAGE.stat().st_size and again == 0, (first, again)

        # the memory itself: bounded, tolerant of damage
        fileids.MAX_ENTRIES = 3
        for n in range(6):
            fileids.remember(f"fp{n}", f"id{n}", "x")
        assert len(json.loads(fileids.STORE.read_text(encoding="utf-8"))) == 3
        fileids.STORE.write_text("{ not json", encoding="utf-8")
        assert fileids.lookup("fp5") is None and fileids.ids_from_reply(None) == [] and fileids.ids_from_reply({"result": True}) == []
        assert fileids.ids_from_reply({"result": [{"document": {"file_id": "a"}}, {"photo": []}]}) == ["a", None]
    finally:
        telegram.post, notifier.time.sleep = keep
        fileids.MAX_ENTRIES = 300
        fileids.STORE.unlink(missing_ok=True)


def test_poll_outage_tracking_and_quiet_logs():
    """One failed long poll is routine and silent; an outage is logged once when it starts and
    once when it ends, with a pause that grows; a failed poll is not a WARNING line each time."""
    import requests
    bot = _package("uninotifier.bot")
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append((record.levelno, record.getMessage()))
    logger = logging.getLogger("uninotifier")
    logger.addHandler(handler)
    try:
        tracker = bot.OutageTracker()
        pauses = [tracker.failed(now=100.0 + n) for n in range(3)]
        assert records == [] and pauses[0] < pauses[2] + 1
        tracker.failed(now=103.0)
        assert [m for lvl, m in records if lvl == logging.WARNING] == ["Telegram is unreachable: 4 polls in a row failed"]
        tracker.failed(now=104.0)
        assert len(records) == 1, "an outage is reported once"
        tracker.ok(now=160.0)
        assert tracker.failures == 0 and not tracker.reported
        tracker.failed(now=200.0)
        assert tracker.failures == 1 and tracker.since == 200.0

        # call_api: a failed getUpdates is a debug note, any other failed call still a warning
        records.clear()
        keep = telegram.post
        telegram.post = lambda url, **kw: (_ for _ in ()).throw(requests.ReadTimeout("slow"))
        try:
            assert notifier.call_api("T", "getUpdates", {}, timeout=1) is None
            assert [m for lvl, m in records if lvl >= logging.WARNING] == []
            assert notifier.call_api("T", "sendMessage", {"chat_id": 1}, timeout=1) is None
            assert len([m for lvl, m in records if lvl == logging.WARNING]) == 1
        finally:
            telegram.post = keep
    finally:
        logger.removeHandler(handler)
    assert 5 <= bot.POLL_TIMEOUT <= 50 and bot.POLL_SLACK >= 5


def test_upload_tried_again_sends_the_whole_file():
    """A send whose TLS handshake stalled is tried again (nothing was sent), and the second try
    must carry the whole file: requests reads a file object while it builds the request, so the
    second try used to send it empty. The fake door reads the body exactly as requests does."""
    import requests

    class Reply:
        status_code, ok = 200, True

        def json(self):
            return {"ok": True, "result": {"message_id": 5, "document": {"file_id": "F", "file_unique_id": "U"}}}

    stall = requests.ReadTimeout("HTTPSConnectionPool(host='api.telegram.org', port=443): Read timed out. "
                                 f"(read timeout={telegram.CONNECT_TIMEOUT})")
    seen = []

    def door(url, files=None, **kw):
        body = {field: value[1] if isinstance(value[1], bytes) else value[1].read()
                for field, value in (files or {}).items()}
        seen.append({field: len(data) for field, data in body.items()})
        if len(seen) == 1:
            raise stall
        return Reply()

    keep = telegram.post
    telegram.post = door
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Lecture 1.pdf"
            path.write_bytes(b"%PDF" + b"x" * 5000)
            fileids.forget(fileids.fingerprint(path))
            assert notifier.send_document("T", "42", path, "📎 Lecture 1")
            assert seen == [{"document": 5004}, {"document": 5004}], seen
    finally:
        telegram.post = keep


def test_refused_markdown_goes_out_as_plain_text():
    """Telegram refusing a message's Markdown is final — the same message would be refused every
    run. It goes out once more as plain text (links and addresses whole), the owner is told once,
    and a notification that still cannot be sent for several checks in a row is reported."""
    import importlib

    assert notifier.plain_text("*Lecture 3* — [Open](https://x.edu/mod_resource/a_b.pdf) snake\\_case "
                               "https://y.edu/z_1 `code`") == \
        "Lecture 3 — Open (https://x.edu/mod_resource/a_b.pdf) snake_case https://y.edu/z_1 code"
    assert notifier.without_markdown({"chat_id": 1, "text": "x"}) is None, "nothing to drop: not tried again"

    class Reply:
        def __init__(self, status, body):
            self.status_code, self.ok, self._body = status, status < 400, body
            self.text = json.dumps(body)

        def json(self):
            return self._body

    refused = {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities: "
                                                              "Can't find end of the entity starting at byte offset 7"}
    sent = []

    def door(url, json=None, data=None, files=None, **kw):
        body = dict(json or data or {})
        sent.append((url.rsplit("/", 1)[1], body))
        if "parse_mode" in body or "parse_mode" in str(body.get("media", "")):
            return Reply(400, refused)
        return Reply(200, {"ok": True, "result": {"message_id": 9}})

    keep = telegram.post
    telegram.post = door
    _fresh_failsafe()
    try:
        assert notifier.send_message("T", "42", "*Big_Data* lecture")
        assert [m for m, _ in sent] == ["sendMessage", "sendMessage"]
        assert "parse_mode" not in sent[1][1] and sent[1][1]["text"] == "BigData lecture", sent[1]
        # an album's captions too
        sent.clear()
        media = json.dumps([{"type": "document", "media": "F1", "caption": "*x_y*", "parse_mode": "Markdown"}])
        assert notifier._request("T", "sendMediaGroup", {"chat_id": "42", "media": media})
        assert json.loads(sent[1][1]["media"]) == [{"type": "document", "media": "F1", "caption": "xy"}]
        # ... and the bot's own screens (call_api)
        sent.clear()
        assert notifier.call_api("T", "editMessageText", {"chat_id": 42, "message_id": 3, "text": "*a_b*",
                                                          "parse_mode": "Markdown"}) == {"message_id": 9}
        assert sent[1][1]["text"] == "ab" and "parse_mode" not in sent[1][1]
        # a refusal that is not about Markdown is not retried
        sent.clear()
        telegram.post = lambda url, **kw: sent.append(1) or Reply(400, {"ok": False, "description": "Bad Request: chat not found"})
        assert not notifier.send_message("T", "42", "*x*") and len(sent) == 1
        failsafe.wait_idle()
        told = [t for t in OUTBOX if "without its formatting" in t]
        assert len(told) == 1, ("told once, not once per message", OUTBOX)
    finally:
        telegram.post = keep

    # the monitor: a notification that keeps failing is reported after UNDELIVERED_ALERT_RUNS checks
    alerts = importlib.import_module("uninotifier.monitor.alerts")
    mstate = importlib.import_module("uninotifier.monitor.state")
    mconfig = importlib.import_module("uninotifier.monitor.config")
    _fresh_failsafe()
    with tempfile.TemporaryDirectory() as tmp:
        keep = (mstate.DATA_FILE, mstate.LOCK_FILE)
        mstate.DATA_FILE, mstate.LOCK_FILE = Path(tmp) / "saved_items.json", Path(tmp) / "monitor.lock"
        try:
            saved = {"undelivered": {}}
            stuck = {"item:1-5": "Lecture 3 — Big Data"}
            for run in range(1, mconfig.UNDELIVERED_ALERT_RUNS):
                alerts.track_undelivered(saved, stuck, now=run)
            assert not OUTBOX and saved["undelivered"]["item:1-5"]["runs"] == mconfig.UNDELIVERED_ALERT_RUNS - 1
            alerts.track_undelivered(saved, stuck, now=10)
            assert len(OUTBOX) == 1 and "Lecture 3" in OUTBOX[0] and "could not be sent" in OUTBOX[0], OUTBOX
            alerts.track_undelivered(saved, stuck, now=11)
            assert len(OUTBOX) == 1, "the same stuck set is not reported every run"
            alerts.track_undelivered(saved, {}, now=12)                    # it went out
            assert saved["undelivered"] == {} and len(OUTBOX) == 2 and "have gone out" in OUTBOX[1], OUTBOX
            alerts.track_undelivered(saved, {"item:1-6": "x"}, now=13)     # a blip: one run, never reported
            alerts.track_undelivered(saved, {}, now=14)
            assert len(OUTBOX) == 2 and saved["undelivered"] == {}
        finally:
            mstate.DATA_FILE, mstate.LOCK_FILE = keep


def test_uploads_use_their_own_send_buffer():
    """Calls that carry files go through a session of their own: its connections connect within
    CONNECT_TIMEOUT and write within UPLOAD_WRITE_TIMEOUT, and its sockets may get a larger send buffer
    (UPLOAD_SEND_BUFFER, 0 = default). Everything else uses the shared pool; a reset starts both afresh."""
    keep = telegram.UPLOAD_SEND_BUFFER
    try:
        telegram.reset_session()
        telegram.UPLOAD_SEND_BUFFER = 0
        plain = telegram._upload_session()
        assert plain is not telegram._session(), "uploads always have their own pool"
        plain_adapter = plain.get_adapter("https://api.telegram.org")
        assert not any(opt == socket.SO_SNDBUF for _, opt, _ in plain_adapter.poolmanager.connection_pool_kw["socket_options"])
        assert plain_adapter.poolmanager.pool_classes_by_scheme["https"] is telegram._UploadPool
        shared_pools = telegram._session().get_adapter("https://api.telegram.org").poolmanager.pool_classes_by_scheme
        assert shared_pools["https"] is not telegram._UploadPool, "only uploads get the long write timeout"
        telegram.UPLOAD_SEND_BUFFER = 1 << 20
        upload = telegram._upload_session()
        assert upload is not telegram._session() and upload is telegram._upload_session()
        options = upload.get_adapter("https://api.telegram.org").poolmanager.connection_pool_kw["socket_options"]
        assert (socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20) in options
        assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in options, "keep-alive probes stay on"
        shared = telegram._session().get_adapter("https://api.telegram.org").poolmanager.connection_pool_kw["socket_options"]
        assert not any(opt == socket.SO_SNDBUF for _, opt, _ in shared), "the long poll and buttons keep the default"

        # the door picks the session by whether the call carries files
        picked = []
        real = (telegram._session, telegram._upload_session)

        class Probe:
            def __init__(self, name):
                self.name = name

            def post(self, url, **kw):
                picked.append(self.name)

        telegram._session, telegram._upload_session = (lambda: Probe("shared")), (lambda size=None: Probe("upload"))
        try:
            _REAL_POST("https://api.telegram.org/botT/sendDocument", data={}, files={"document": ("a", b"x")})
            _REAL_POST("https://api.telegram.org/botT/sendMessage", json={})
        finally:
            telegram._session, telegram._upload_session = real
        assert picked == ["upload", "shared"], picked

        telegram.reset_session()
        assert telegram._upload_session() is not upload, "a reset starts the upload pool afresh too"

        # every call connects within CONNECT_TIMEOUT — an outage is noticed in 6 s, uploads included
        given, keep_post = [], telegram.post

        class Ok:
            status_code, ok = 200, True

        telegram.post = lambda url, **kw: given.append(kw["timeout"]) or Ok()
        try:
            telegram.request("T", "sendDocument", data={"chat_id": 1}, files={"document": ("a", b"x")}, timeout=60)
            telegram.request("T", "sendMessage", json={"chat_id": 1}, timeout=20)
        finally:
            telegram.post = keep_post
        assert given == [(telegram.CONNECT_TIMEOUT, 60), (telegram.CONNECT_TIMEOUT, 20)], given
        assert telegram.UPLOAD_WRITE_TIMEOUT >= 30

        # ... and an upload connection really connects under that, writes under UPLOAD_WRITE_TIMEOUT and
        # reads under the read timeout (urllib3 would write under the connect timeout: 6 s)
        import urllib3
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        seen = []

        class WriteProbe(telegram._WriteTimeout, urllib3.connection.HTTPConnection):
            def connect(self):
                seen.append(("connect", self.timeout))
                super().connect()

            def send(self, data):
                seen.append(("write", self.sock.gettimeout()))
                super().send(data)

        class Pool(urllib3.HTTPConnectionPool):
            ConnectionCls = WriteProbe

        class Sink(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            pool = Pool("127.0.0.1", server.server_port)
            reply = pool.urlopen("POST", "/botT/sendDocument", body=b"x" * 50000,
                                 timeout=urllib3.Timeout(connect=telegram.CONNECT_TIMEOUT, read=60))
            assert reply.status == 200
            connection = pool.pool.queue[-1] or pool.pool.queue[0]
            read_timeout = connection.sock.gettimeout() if connection and connection.sock else 60
        finally:
            server.shutdown()
        assert seen[0] == ("connect", telegram.CONNECT_TIMEOUT), seen
        writes = {t for kind, t in seen if kind == "write"}
        assert writes == {telegram.UPLOAD_WRITE_TIMEOUT}, seen
        assert read_timeout == 60, read_timeout
    finally:
        telegram.UPLOAD_SEND_BUFFER = keep
        telegram.reset_session()


def test_a_restart_never_replays_an_update():
    """The offset is saved before each update is handled: a bot killed while handling one (the
    monitor restarts a stuck bot) does not get the same update again — and cannot get stuck on it
    again. A saved offset of another bot (another token) is ignored."""
    bot = _package("uninotifier.bot")

    class Killed(BaseException):
        pass

    asked, handled, sleeps = [], [], []
    queue = [{"update_id": 10}, {"update_id": 11}, {"update_id": 12}]

    def get_updates(token, method, params=None, timeout=20, **kw):
        asked.append(params.get("offset"))
        return [u for u in queue if params.get("offset") is None or u["update_id"] >= params["offset"]]

    def dispatch(update):
        handled.append((update["update_id"], bot.load_offset()))
        if update["update_id"] == 11:
            raise Killed()                      # the process dies while handling update 11

    keep = (notifier.call_api, bot.dispatch, bot.OFFSET_FILE, bot.BOT_TOKEN, bot.time.sleep)
    with tempfile.TemporaryDirectory() as tmp:
        notifier.call_api, bot.dispatch, bot.OFFSET_FILE = get_updates, dispatch, Path(tmp) / "offset.json"
        try:
            assert bot.load_offset() is None
            try:
                bot.poll_once(bot.load_offset(), bot.OutageTracker())
            except Killed:
                pass
            assert handled == [(10, 11), (11, 12)], ("each offset is on disk before its update is handled", handled)
            # ... the restart: it asks past update 11, so 11 is not handled again; 12 is
            assert bot.poll_once(bot.load_offset(), bot.OutageTracker()) == 13
            assert asked == [None, 12] and [u for u, _ in handled] == [10, 11, 12]
            # another bot's offset is not used
            bot.BOT_TOKEN = "999999:other-token"
            assert bot.load_offset() is None
            bot.BOT_TOKEN = keep[3]
            # Telegram unreachable: the offset stays, the loop backs off
            notifier.call_api = lambda *a, **k: None
            bot.time.sleep = sleeps.append
            assert bot.poll_once(13, bot.OutageTracker()) == 13 and len(sleeps) == 1
        finally:
            notifier.call_api, bot.dispatch, bot.OFFSET_FILE, bot.BOT_TOKEN, bot.time.sleep = keep


def test_a_long_retry_after_is_not_waited_out():
    """HTTP 429: a "retry after" up to MAX_RETRY_WAIT is waited out once; a longer one gives the send
    up at once instead of holding the thread (on the bot: every button) for minutes. A malformed
    one is treated as no delay given."""
    class Reply:
        def __init__(self, status, body):
            self.status_code, self.ok, self._body = status, status < 400, body
            self.text = json.dumps(body)

        def json(self):
            return self._body

    def throttled(seconds):
        return Reply(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": seconds}})

    replies, calls, sleeps = [], [], []

    def door(url, **kw):
        calls.append(url)
        return replies.pop(0)

    keep = (telegram.post, time.sleep)
    telegram.post, time.sleep = door, sleeps.append
    try:
        replies[:] = [throttled(300)]
        started = time.monotonic()
        assert not notifier.send_message("T", "42", "hello")
        assert len(calls) == 1 and sleeps == [] and time.monotonic() - started < 1, (calls, sleeps)

        calls.clear()
        replies[:] = [throttled(45), Reply(200, {"ok": True, "result": {}})]
        assert notifier.send_message("T", "42", "hello")
        assert sleeps == [45.0] and len(calls) == 2, (sleeps, calls)

        calls.clear()
        sleeps.clear()
        replies[:] = [throttled("soon"), Reply(200, {"ok": True, "result": {}})]
        assert notifier.send_message("T", "42", "hello") and sleeps == [notifier.BACKOFF_SECONDS[0]], sleeps
    finally:
        telegram.post, time.sleep = keep


def test_the_poll_slows_down_when_telegram_asks():
    """A 429 on getUpdates: the loop waits what Telegram asked for (it used to try again within 8 s),
    but never longer than MAX_POLL_WAIT, so its heartbeat never looks stuck."""
    bot = _package("uninotifier.bot")

    class Reply:
        status_code, ok = 429, False

        def __init__(self, seconds):
            self._body = {"ok": False, "error_code": 429, "description": "Too Many Requests",
                          "parameters": {"retry_after": seconds}}

        def json(self):
            return self._body

    # call_api tells the caller how long Telegram asked to wait
    keep = (telegram.post, time.sleep, notifier.call_api)
    sleeps = []
    telegram.post, time.sleep = (lambda url, **kw: Reply(45)), sleeps.append
    try:
        told = {}
        assert notifier.call_api("T", "getUpdates", {}, outcome=told) is None and told == {"retry_after": 45.0}
        # ... and the loop waits it out — up to MAX_POLL_WAIT
        for asked, waited in ((45, 45), (900, bot.MAX_POLL_WAIT), (None, None)):
            sleeps.clear()

            def refused(token, method, data=None, timeout=20, outcome=None, asked=asked):
                if asked and outcome is not None:
                    outcome["retry_after"] = asked
                return None
            notifier.call_api = refused
            assert bot.poll_once(7, bot.OutageTracker()) == 7
            if waited:
                assert sleeps == [waited], (asked, sleeps)
            else:
                assert len(sleeps) == 1 and sleeps[0] <= 1, ("no 429: the usual short back-off", sleeps)
        assert bot.MAX_POLL_WAIT < heartbeat.STALE_SECONDS / 2
    finally:
        telegram.post, time.sleep, notifier.call_api = keep


def test_an_odd_getupdates_answer_never_stops_the_loop():
    """getUpdates answering something that is not a list of updates (call_api gives True for an
    "ok" without a result) or a list with malformed entries: skipped and logged — the polling
    loop never crashes on it."""
    bot = _package("uninotifier.bot")
    handled, sleeps = [], []
    keep = (notifier.call_api, bot.dispatch, bot.OFFSET_FILE, time.sleep)
    with tempfile.TemporaryDirectory() as tmp:
        bot.dispatch, bot.OFFSET_FILE, time.sleep = (lambda u: handled.append(u["update_id"])), Path(tmp) / "o.json", sleeps.append
        try:
            for odd in (True, {"result": []}, "x"):
                notifier.call_api = lambda *a, odd=odd, **k: odd
                assert bot.poll_once(4, bot.OutageTracker()) == 4
            assert handled == [] and len(sleeps) == 3, "each odd answer counts as a failed poll"
            notifier.call_api = lambda *a, **k: [None, {"message": {}}, {"update_id": "7"}, {"update_id": True},
                                                 {"update_id": 5}]
            assert bot.poll_once(4, bot.OutageTracker()) == 6 and handled == [5]
        finally:
            notifier.call_api, bot.dispatch, bot.OFFSET_FILE, time.sleep = keep


def test_the_statistics_do_not_grow_forever():
    """The always-on bot counts its Telegram calls per hour in memory; hours past STATS_KEEP_HOURS
    leave memory at the next flush, as they leave the file."""
    keep = dict(telegram._stats)
    try:
        telegram._stats.clear()
        old = time.strftime("%Y-%m-%dT%H", time.localtime(time.time() - (telegram.STATS_KEEP_HOURS + 5) * 3600))
        now = time.strftime("%Y-%m-%dT%H")
        telegram._stats.update({"2020-01-01T00": {"api:ok": 1}, old: {"poll:ok": 3}, now: {"api:ok": 2}})
        telegram.flush_stats()
        assert set(telegram._stats) == {now}, telegram._stats
        assert set(json.loads(telegram.STATS_FILE.read_text(encoding="utf-8"))) >= {now}
    finally:
        telegram._stats.clear()
        telegram._stats.update(keep)
        telegram.STATS_FILE.unlink(missing_ok=True)
