"""Staying up and saying so: failures, self-heal, heartbeat, backups, logs, new versions."""

from support import (_as_owner, _failure_policy_checks, _fresh_failsafe, _legacy_markdown_ok, OUTBOX, _package,
                     _tg)

import importlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from uninotifier import access
from uninotifier import failsafe
from uninotifier import gpa
from uninotifier import heartbeat
from uninotifier import lectures
from uninotifier import notifier
from uninotifier import paths
from uninotifier import reminders
from uninotifier import schedule


def test_failure_policy():
    monitor = _package("uninotifier.monitor")
    orig = monitor.send_telegram, monitor.save_data      # restored below: other tests rely on the real ones
    try:
        _failure_policy_checks(monitor)
    finally:
        monitor.send_telegram, monitor.save_data = orig


def test_healthcheck():
    from uninotifier import healthcheck

    calls = []
    orig_post = healthcheck.requests.post

    class Reply:
        def __init__(self, fail=False):
            self.fail = fail

        def raise_for_status(self):
            if self.fail:
                raise healthcheck.requests.HTTPError("500 for https://hc-ping.com/secret-uuid")

    def fake_post(url, data=None, timeout=None):
        calls.append((url, data))
        return Reply(fail=fail["on"])

    fail = {"on": False}
    healthcheck.requests.post = fake_post
    saved_env = os.environ.pop(healthcheck.ENV_NAME, None)
    try:
        assert healthcheck.url() is None and not healthcheck.start() and calls == [], "unset = disabled"
        for bad in ("http://hc-ping.com/x", "not a url", "https://"):
            os.environ[healthcheck.ENV_NAME] = bad
            assert healthcheck.url() is None, bad

        os.environ[healthcheck.ENV_NAME] = " https://hc-ping.com/uuid-1/ "
        assert healthcheck.url() == "https://hc-ping.com/uuid-1"
        healthcheck.start()
        healthcheck.report(0, "6 courses, 8 items, 9.1 s")
        healthcheck.report(1, "exit code 1 — see monitor.log")
        assert calls == [("https://hc-ping.com/uuid-1/start", b""),
                         ("https://hc-ping.com/uuid-1", b"6 courses, 8 items, 9.1 s"),
                         ("https://hc-ping.com/uuid-1/log", "exit code 1 — see monitor.log".encode())], calls

        # an unreachable service never raises, retries, and never writes the secret URL to the log
        fail["on"] = True
        calls.clear()
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        healthcheck.log.addHandler(handler)
        healthcheck.log.propagate = False        # keep the simulated failure out of monitor.log
        logging.disable(logging.NOTSET)
        try:
            assert healthcheck.success("x") is False
        finally:
            healthcheck.log.removeHandler(handler)
            healthcheck.log.propagate = True
            logging.disable(logging.INFO)
        assert len(calls) == healthcheck.RETRIES and records
        assert all("secret-uuid" not in r and "hc-ping" not in r for r in records), records
    finally:
        healthcheck.requests.post = orig_post
        os.environ.pop(healthcheck.ENV_NAME, None)
        if saved_env is not None:
            os.environ[healthcheck.ENV_NAME] = saved_env


def test_new_versions_and_news():
    """A cloned copy is told once about each new version, in the developer's own words (NEWS.md);
    Update Now only fast-forwards a clean copy, and a version that does not start is taken back."""
    import subprocess
    import tempfile
    from uninotifier import updates

    news = "# News\n\n## 2030-02-01 — Registration Plan\nChoose your hours.\n- and courses\n\n## 2030-01-10 — First\nHello.\n"
    entries = updates.parse_news(news)
    assert [e["id"] for e in entries] == ["2030-02-01 — Registration Plan", "2030-01-10 — First"]
    assert entries[0]["text"] == "Choose your hours.\n- and courses"
    fresh = updates.new_entries(news, "## 2030-01-10 — First\nHello.\n")
    assert [e["title"] for e in fresh] == ["2030-02-01 — Registration Plan"], "only what this copy does not have"
    assert updates.visible_subjects(["feat: a new screen", "docs: wording", "fix(bot): a button", "chore: bump"]) == [
        "a new screen", "a button"]
    told = updates.message({"news": fresh, "subjects": ["feat: x"], "behind": 2, "sha": "a" * 40})
    assert "📣 *2030-02-01 — Registration Plan*" in told and "Choose your hours." in told and "What changed" not in told
    plain = updates.message({"news": [], "subjects": ["feat: a new screen", "docs: wording"], "behind": 2, "sha": "a" * 40})
    assert "• a new screen" in plain and "wording" not in plain
    assert "under the hood" in updates.message({"news": [], "subjects": ["docs: wording"], "behind": 1, "sha": "a" * 40})
    for text in (told, plain):
        assert _legacy_markdown_ok(text) and len(text) < notifier.MESSAGE_LIMIT
    assert updates.due(now=100000.0, state={}) and not updates.due(now=100000.0, state={"checked": 99000.0})

    def git(where, *args):
        done = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                               "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
                              cwd=where, capture_output=True, text=True)
        assert done.returncode == 0, (args, done.stderr)
        return done.stdout.strip()

    keep_state = updates.STATE_FILE
    with tempfile.TemporaryDirectory() as tmp:
        origin, copy = Path(tmp) / "origin", Path(tmp) / "copy"
        origin.mkdir()
        git(origin, "init", "--quiet", "--initial-branch=main")
        (origin / "bot.py").write_text("print('one')\n", encoding="utf-8")
        (origin / "requirements.txt").write_text("", encoding="utf-8")
        (origin / "NEWS.md").write_text("## 2030-01-10 — First\nHello.\n", encoding="utf-8")
        git(origin, "add", "-A"); git(origin, "commit", "--quiet", "-m", "feat: the first version")
        git(Path(tmp), "clone", "--quiet", str(origin), "copy")
        updates.STATE_FILE = Path(tmp) / "updates.json"
        try:
            assert updates.is_clone(copy) and not updates.is_clone(Path(tmp))
            assert updates.look(Path(tmp)) is None, "not a clone: not checked"
            assert updates.look(copy)["behind"] == 0
            sent = []
            assert updates.check(lambda text, rows: sent.append(text) or True, root=copy)["behind"] == 0 and not sent

            (origin / "bot.py").write_text("print('two')\n", encoding="utf-8")
            (origin / "NEWS.md").write_text("## 2030-02-01 — Second\nNew things.\n\n## 2030-01-10 — First\nHello.\n",
                                            encoding="utf-8")
            git(origin, "add", "-A"); git(origin, "commit", "--quiet", "-m", "feat: the second version")
            found = updates.check(lambda text, rows: sent.append(text) or True, root=copy)
            assert found["behind"] == 1 and [e["title"] for e in found["news"]] == ["2030-02-01 — Second"]
            assert len(sent) == 1 and "New things." in sent[0]
            updates.check(lambda text, rows: sent.append(text) or True, root=copy)
            assert len(sent) == 1, "one message per new version"
            assert (copy / "bot.py").read_text(encoding="utf-8") == "print('one')\n", "nothing is installed without the press"

            # a file changed by hand stops the update, by name
            (copy / "bot.py").write_text("print('mine')\n", encoding="utf-8")
            try:
                updates.apply(copy)
                raise AssertionError("an edited copy must not be updated")
            except updates.UpdateError as e:
                assert "`bot.py`" in str(e) and "git pull" in str(e)
            git(copy, "checkout", "--quiet", "--", "bot.py")
            assert updates.apply(copy).startswith("Updated to `")
            assert (copy / "bot.py").read_text(encoding="utf-8") == "print('two')\n"
            assert updates.apply(copy) == "Already on the newest version."

            # a version that would not start is taken back
            before = git(copy, "rev-parse", "HEAD")
            (origin / "bot.py").write_text("def broken(:\n", encoding="utf-8")
            git(origin, "add", "-A"); git(origin, "commit", "--quiet", "-m", "fix: broken")
            try:
                updates.apply(copy)
                raise AssertionError("a version that does not compile must be refused")
            except updates.UpdateError as e:
                assert "does not start (bot.py line 1" in str(e) and "back on the version it had" in str(e)
            assert git(copy, "rev-parse", "HEAD") == before and (copy / "bot.py").read_text(encoding="utf-8") == "print('two')\n"
        finally:
            updates.STATE_FILE = keep_state

    # this project's own NEWS.md reads as news, newest first
    own = updates.parse_news((paths.ROOT / updates.NEWS_FILE).read_text(encoding="utf-8"))
    assert own and all(re.match(r"\d{4}-\d{2}-\d{2} — \S", e["id"]) and e["text"] for e in own)
    assert [e["id"][:10] for e in own] == sorted((e["id"][:10] for e in own), reverse=True)


def test_alerts_are_recorded_as_they_are_sent():
    """A run that dies half-way must not make the next one repeat what was already
    delivered: every ID is written down the moment its message goes out."""
    monitor = _package("uninotifier.monitor")

    course = "20261-Big Data-AllSec-3-"

    def item(n, kind="label"):
        return {"name": f"Item {n}", "type": kind, "course": course, "course_id": "1", "cmid": str(n),
                "url": f"https://x/mod/{kind}/view.php?id={n}"}

    content = {f"1-{n}": item(n) for n in (10, 11, 12)}
    content["1-20"] = item(20, "forum")
    posts = [{"id": str(n), "discussion": "1", "subject": f"S{n}", "author": "Dr", "time": n, "html": "x",
              "is_reply": False, "url": f"https://x/d#p{n}", "attachments": []} for n in (101, 102, 103)]

    def result():
        return {"courses": {"1": {"name": course, "url": "https://x/c"}}, "content": dict(content),
                "deadlines": {}, "artifacts": {}, "updates": {}, "edits": {},
                "posts": {"1-20": list(posts)}, "forum_posts": {"1-20": list(posts)},
                "submissions": {}, "link_changes": [], "seconds": 1.0, "browser_used": False}

    class Crash(BaseException):
        pass

    with tempfile.TemporaryDirectory() as tmp:
        keep = {k: getattr(monitor, k) for k in ("DATA_FILE", "USERNAME", "PASSWORD", "BOT_TOKEN", "CHAT_ID",
                                                 "run_scan", "notify_new_item", "notify_forum_post",
                                                 "send_schedule_messages", "mfa_allowed", "notify_new_course")}
        monitor.DATA_FILE = Path(tmp) / "saved_items.json"
        monitor.USERNAME = monitor.PASSWORD = monitor.BOT_TOKEN = monitor.CHAT_ID = "x"
        monitor.send_schedule_messages = lambda saved: None
        monitor.mfa_allowed = lambda saved: False
        monitor.notify_new_course = lambda course: True
        def fake_scan(saved, notify_new, mfa_handler=None):
            r = result()          # the portal's view; "posts" is filtered against what was already seen, as run_scan does
            r["posts"] = monitor.new_forum_posts(saved, r["forum_posts"], notify_new)
            return r
        monitor.run_scan = fake_scan
        # a state that already knows the course and the forum (so this is not a "first run")
        monitor.save_data({**monitor.load_saved_data(), "courses": {"1": {"name": course, "url": "https://x/c"}},
                           "content": {"1-0": item(0)}, "forums": {"1-20": ["100"]}})
        sent_items, sent_posts = [], []
        try:
            # run 1: dies while announcing the third new item
            def flaky_item(it, deadline, art, submitted=None):
                if it["name"] == "Item 12":
                    raise Crash("process killed")
                sent_items.append(it["name"])
                return True
            monitor.notify_new_item = flaky_item
            monitor.notify_forum_post = lambda forum, post: sent_posts.append(post["id"]) or True
            try:
                monitor.run()
            except Crash:
                pass
            assert sent_items == ["Item 10", "Item 11"], sent_items
            on_disk = json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))
            assert {"1-10", "1-11"} <= set(on_disk["content"]) and "1-12" not in on_disk["content"], \
                "what was delivered is already on disk, what wasn't is not"

            # run 2: only what was never delivered goes out — nothing is announced twice
            monitor.notify_new_item = lambda it, deadline, art, submitted=None: sent_items.append(it["name"]) or True
            monitor.run()
            # the new forum itself is no news (it is only a container) — its posts are
            assert sent_items == ["Item 10", "Item 11", "Item 12"], sent_items
            assert sent_posts == ["101", "102", "103"]
            assert "1-20" in json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))["content"]

            # run 3 finds nothing new at all
            monitor.run()
            assert sent_items == ["Item 10", "Item 11", "Item 12"] and sent_posts == ["101", "102", "103"], (sent_items, sent_posts)

            # forum posts: dying after the first post, the other two are still owed — and only those
            monitor.save_data({**monitor.load_saved_data(), "forums": {"1-20": ["100"]}})
            sent_posts.clear()

            def flaky_post(forum, post):
                if post["id"] == "102":
                    raise Crash("killed")
                sent_posts.append(post["id"])
                return True
            monitor.notify_forum_post = flaky_post
            try:
                monitor.run()
            except Crash:
                pass
            assert sent_posts == ["101"]
            assert "101" in json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))["forums"]["1-20"]
            monitor.notify_forum_post = lambda forum, post: sent_posts.append(post["id"]) or True
            monitor.run()
            assert sent_posts == ["101", "102", "103"], sent_posts

            # Telegram refusing one item run after run: it is owed every run, and reported once it is stuck
            _fresh_failsafe()
            monitor.save_data({**monitor.load_saved_data(),
                               "content": {k: v for k, v in monitor.load_saved_data()["content"].items() if k != "1-12"},
                               "announced": {k: v for k, v in monitor.load_saved_data()["announced"].items() if k != "1-12"}})
            monitor.notify_new_item = lambda it, deadline, art, submitted=None: False
            for _ in range(monitor.UNDELIVERED_ALERT_RUNS):
                monitor.run()
            failsafe.wait_idle()
            owed = json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))["undelivered"]
            assert owed["item:1-12"]["runs"] == monitor.UNDELIVERED_ALERT_RUNS, owed
            assert len([t for t in OUTBOX if "could not be sent" in t and "Item 12" in t]) == 1, OUTBOX
            monitor.notify_new_item = lambda it, deadline, art, submitted=None: sent_items.append(it["name"]) or True
            monitor.run()
            assert json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))["undelivered"] == {}
            assert any("have gone out" in t for t in OUTBOX), OUTBOX
        finally:
            for k, v in keep.items():
                setattr(monitor, k, v)

    # a reminder is saved the moment it is sent, and writing state keeps the run lock alive
    flags = []
    orig = monitor.send_telegram
    monitor.send_telegram = lambda text, url=None: True
    try:
        due = {"assign-1": {"type": "assign", "course": course, "name": "HW", "url": "u",
                            "timestamp": time.time() + 1800}}
        monitor.send_deadline_reminders(due, on_sent=lambda: flags.append(dict(due["assign-1"])))
    finally:
        monitor.send_telegram = orig
    assert len(flags) == 1 and flags[0].get("reminded_24h") and flags[0].get("reminded_1h"), flags
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "monitor.lock"
        lock.write_text("1")
        os.utime(lock, (1, 1))
        orig = monitor.LOCK_FILE, monitor.DATA_FILE
        monitor.LOCK_FILE, monitor.DATA_FILE = lock, Path(tmp) / "s.json"
        try:
            monitor.save_data({"a": 1})
            assert lock.stat().st_mtime > 1e9, "saving state refreshes the lock"
        finally:
            monitor.LOCK_FILE, monitor.DATA_FILE = orig


def test_a_crashed_check_is_told_to_be_over():
    """A check that crashed on a bug tells you; the first run that goes through says it works
    again — once, and not before — however many different crashes were open."""
    _fresh_failsafe()
    mrun = importlib.import_module("uninotifier.monitor.run")
    state_mod = importlib.import_module("uninotifier.monitor.state")
    outcome = {"raise": None}

    def fake_run(announce=False):
        if outcome["raise"]:
            raise outcome["raise"]
    swaps = [(mrun.notifier, "setup_logging", lambda *a, **k: None), (mrun.failsafe, "install_excepthooks", lambda *a: None),
             (mrun.envcheck, "require", lambda *a: None), (state_mod, "acquire_lock", lambda: True),
             (state_mod, "release_lock", lambda: None), (state_mod, "write_last_run", lambda *a: None),
             (mrun.healthcheck, "start_bg", lambda: None), (mrun.healthcheck, "report_bg", lambda *a: None),
             (mrun.healthcheck, "wait", lambda: None), (mrun.gitsync, "sync_state", lambda: None),
             (mrun, "run", fake_run)]
    kept = [(obj, name, getattr(obj, name)) for obj, name, _ in swaps]
    for obj, name, new in swaps:
        setattr(obj, name, new)
    try:
        before = len(OUTBOX)
        mrun.main()
        assert len(OUTBOX) == before, "a good run with nothing open says nothing"
        outcome["raise"] = ValueError("a bug")
        try:
            mrun.main()
            raise AssertionError("a crash ends the run with exit 1")
        except SystemExit as e:
            assert e.code == 1
        assert "crashed on a bug" in OUTBOX[-1] and len(OUTBOX) == before + 1
        try:
            mrun.main()                                   # the same bug again: held back, still not over
        except SystemExit:
            pass
        assert len(OUTBOX) == before + 1
        outcome["raise"] = None
        mrun.main()
        assert len(OUTBOX) == before + 2 and OUTBOX[-1].startswith("✅ *The scheduled check works again*"), OUTBOX[-1]
        assert _legacy_markdown_ok(OUTBOX[-1])
        mrun.main()
        assert len(OUTBOX) == before + 2, "told once"
    finally:
        for obj, name, old in kept:
            setattr(obj, name, old)


def test_failsafe_reports_once_and_resolves():
    """A problem reaches you once, not once per occurrence; a different problem, or
    the same one after the cool-down, reaches you again; "fine again" follows once;
    nothing in it can raise, leak the token or break Markdown."""
    _fresh_failsafe()
    try:
        raise ValueError("a_b *c* `d` 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    except ValueError as e:
        exc = e
    assert failsafe.report("k", "Something *broke*", detail="Fix _this_", exc=exc) is True
    assert len(OUTBOX) == 1 and "needs attention" in OUTBOX[0] and "ValueError" in OUTBOX[0]
    assert "AAAAAAAAAAAAAAAAAAAA" not in OUTBOX[0] and _legacy_markdown_ok(OUTBOX[0]), OUTBOX[0]
    assert not failsafe.report("k", "Something broke", exc=exc) and not failsafe.report("k", "Something broke")
    assert len(OUTBOX) == 1, "held back during the cool-down"
    assert failsafe.report("k", "Something broke", fingerprint="other") and len(OUTBOX) == 2, "a different instance"
    state = json.loads(failsafe.STATE_FILE.read_text(encoding="utf-8"))
    state["k"]["at"] = 0
    state["k"]["suppressed"] = 2
    failsafe.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    assert failsafe.report("k", "Again", fingerprint="other") and "2 similar" in OUTBOX[-1], "after the cool-down"

    assert failsafe.resolve("k", "✅ fine") and OUTBOX[-1] == "✅ fine"
    assert not failsafe.resolve("k", "✅ fine") and not failsafe.resolve("never-reported", "x"), "once, and only if open"

    # a kind that carries what went wrong (a crash's signature): ONE follow-up for whichever were open
    assert failsafe.report("crash:A@x.py:1", "A check crashed") and failsafe.report("crash:B@y.py:2", "A check crashed")
    assert failsafe.report("other", "Unrelated")
    sent_so_far = len(OUTBOX)
    assert failsafe.resolve_matching("crash:", "✅ crash over") and OUTBOX[-1] == "✅ crash over"
    assert len(OUTBOX) == sent_so_far + 1, "one message, not one per signature"
    assert not failsafe.resolve_matching("crash:", "✅ crash over") and len(OUTBOX) == sent_so_far + 1, "once"
    assert failsafe.resolve("other", "✅ other") and OUTBOX[-1] == "✅ other", "an unrelated kind stays open"

    # background delivery, and a failing door out
    assert failsafe.report("bg", "In the background", wait=False)
    failsafe.wait_idle()
    assert OUTBOX[-1].count("In the background") == 1
    orig = failsafe.send
    failsafe.send = lambda *a, **k: (_ for _ in ()).throw(OSError("network down"))
    try:
        assert failsafe.report("down", "Network is down") is False, "never raises"
        assert "down" not in json.loads(failsafe.STATE_FILE.read_text(encoding="utf-8")), "a failed send is retried next time"
    finally:
        failsafe.send = orig
    assert failsafe.report("down", "Network is back") is True

    # bug signatures: the same bug, the same signature
    assert failsafe.signature(exc).startswith(f"ValueError@{Path(__file__).name}:")

    # whatever escapes a thread is reported (and then still printed by the default hook)
    import threading
    hooks = (sys.excepthook, threading.excepthook)
    err = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        failsafe.install_excepthooks("bot")
        t = threading.Thread(target=lambda: 1 / 0, name="worker")
        t.start()
        t.join()
        failsafe.wait_idle()
    finally:
        sys.stderr.close()
        sys.stderr = err
        sys.excepthook, threading.excepthook = hooks
    assert "background thread" in OUTBOX[-1] and "worker" in OUTBOX[-1] and "ZeroDivisionError" in OUTBOX[-1]


def test_bot_contains_and_reports_failures():
    """One failing button or message neither stops the bot nor goes unseen: the
    person is answered, the bug is reported (once), and what they typed is not logged."""
    _fresh_failsafe()
    bot = _package("uninotifier.bot")
    keep_owner = _as_owner(bot)
    api_calls, records = [], []
    orig = (bot.handle_command, bot.handle_callback, bot.api)
    bot.api = lambda method, **data: api_calls.append((method, data))
    bot.handle_command = lambda text, message_id=None: 1 / 0
    bot.handle_callback = lambda cb: {}["missing"]
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logging.getLogger("uninotifier").addHandler(handler)
    try:
        bot.dispatch({**_tg(text="hunter2-secret"), "update_id": 7})
        bot.dispatch({**_tg(data="x"), "update_id": 8, "callback_query": {**_tg(data="x")["callback_query"], "id": "cb1"}})
        bot.dispatch({**_tg(user_id="999", chat_id="999", text="hi"), "update_id": 9})   # a stranger: dropped before any handler
        failsafe.wait_idle()
    finally:
        logging.getLogger("uninotifier").removeHandler(handler)
        bot.handle_command, bot.handle_callback, bot.api = orig
        bot.CHAT_ID, access.ADMIN_USER_ID = keep_owner
    methods = [m for m, _ in api_calls]
    assert methods == ["sendMessage", "answerCallbackQuery"], methods
    assert "Something went wrong" in api_calls[0][1]["text"] and api_calls[1][1]["callback_query_id"] == "cb1"
    failures = [t for t in OUTBOX if "failed to handle" in t]
    assert len(failures) == 2 and "message" in failures[0] and "button press" in failures[1], OUTBOX
    assert len([t for t in OUTBOX if "not you" in t]) == 1, "the stranger was dropped, and the owner told once"
    assert not any("hunter2" in r for r in records), "the update's text must not be logged"
    assert all(_legacy_markdown_ok(t) for t in OUTBOX)


def test_bot_heartbeat_and_monitor_watch():
    """The bot's pulse is written as its loops turn; the scheduled monitor notices when
    one stops (not right after Windows starts), says so at most once, and says when it is back.
    The bot also has its own healthchecks.io check."""
    from uninotifier import healthcheck
    from uninotifier.monitor import alerts, state as mstate

    heartbeat.FILE.unlink(missing_ok=True)
    heartbeat._state.pop("poll", None)
    heartbeat._state.pop("ticker", None)
    heartbeat._last_write = 0
    assert heartbeat.read() == {} and not heartbeat.fresh_in_memory()
    heartbeat.beat("poll", now=1000)
    heartbeat.beat("ticker", now=1001)                 # inside the write throttle: memory only
    assert heartbeat.read()["poll"] == 1000 and "ticker" not in heartbeat.read()
    heartbeat._last_write = 0
    heartbeat.beat("ticker", now=1002)
    assert heartbeat.stale_parts(heartbeat.read(), now=1100) == []
    assert heartbeat.stale_parts(heartbeat.read(), now=1000 + heartbeat.STALE_SECONDS + 5) == ["poll", "ticker"]

    sent = []
    keep = (alerts.send_telegram, mstate.DATA_FILE, mstate.LOCK_FILE, alerts.selfheal.restart_bot)
    alerts.send_telegram = lambda text, url=None: sent.append(text) or True
    alerts.selfheal.restart_bot = lambda pid: False          # this test is about the alerts: a restart that cannot happen
    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-watch-"))
    mstate.DATA_FILE, mstate.LOCK_FILE = tmp / "saved_items.json", tmp / "monitor.lock"
    saved = mstate.load_saved_data()
    long_up = 99999
    try:
        now = 5000.0
        heartbeat.FILE.unlink()
        alerts.check_bot(saved, now, long_up)
        assert sent == [], "no pulse on disk: the bot never ran here"
        heartbeat.FILE.write_text(json.dumps({"poll": now - 30, "ticker": now - 20}), encoding="utf-8")
        alerts.check_bot(saved, now, long_up)
        assert sent == [] and not saved["watch"], "fresh"
        heartbeat.FILE.write_text(json.dumps({"poll": now - 30, "ticker": now - 3600}), encoding="utf-8")
        alerts.check_bot(saved, now, uptime=120)
        assert sent == [], "just after Windows started: give the bot time"
        alerts.check_bot(saved, now, long_up)
        assert len(sent) == 1 and "isn't responding" in sent[0] and "lecture clock" in sent[0] and _legacy_markdown_ok(sent[0])
        alerts.check_bot(saved, now + 900, long_up)
        assert len(sent) == 1, "once per cool-down"
        heartbeat.FILE.write_text(json.dumps({"poll": now + 1000, "ticker": now + 1000}), encoding="utf-8")
        alerts.check_bot(saved, now + 1010, long_up)
        assert len(sent) == 2 and "bot is back" in sent[1] and not saved["watch"]
        alerts.check_bot(saved, now + 1020, long_up)
        assert len(sent) == 2, "back is said once"
    finally:
        alerts.send_telegram, mstate.DATA_FILE, mstate.LOCK_FILE, alerts.selfheal.restart_bot = keep
        heartbeat.FILE.unlink(missing_ok=True)

    # the bot's own healthchecks.io check, independent of the monitor's
    calls = []
    orig_post = healthcheck.requests.post
    healthcheck.requests.post = lambda url, data=None, timeout=None: calls.append(url) or type(
        "R", (), {"raise_for_status": lambda self: None})()
    saved_env = {k: os.environ.pop(k, None) for k in (healthcheck.ENV_NAME, healthcheck.BOT_ENV_NAME)}
    try:
        assert healthcheck.ping_bot() is False and calls == [], "unset = disabled"
        os.environ[healthcheck.ENV_NAME] = "https://hc-ping.com/monitor-uuid"
        assert healthcheck.ping_bot() is False and calls == [], "the monitor's URL is not the bot's"
        os.environ[healthcheck.BOT_ENV_NAME] = "https://hc-ping.com/bot-uuid/"
        assert healthcheck.ping_bot() is True and calls == ["https://hc-ping.com/bot-uuid"]
    finally:
        healthcheck.requests.post = orig_post
        for k, v in saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_bot_self_heal():
    """A bot that stops beating is restarted by the next scheduled check (a few times,
    spaced out, only when it is really this project's bot.py), you hear about it only if
    that does not help, and a recovery says it was done for you."""
    from uninotifier import selfheal
    from uninotifier.monitor import alerts, state as mstate

    # the restart itself: only a process that really is this project's bot.py is ended
    commands, listing = [], {"out": ""}

    class Done:
        def __init__(self, out="", code=0):
            self.stdout, self.stderr, self.returncode = out, "", code

    def fake_run(cmd, timeout):
        commands.append(cmd[0].lower())
        return Done(listing["out"]) if cmd[0].lower().startswith("powershell") else Done()

    keep_run, keep_sleep = selfheal._run, selfheal.time.sleep
    selfheal._run, selfheal.time.sleep = fake_run, lambda s: None
    try:
        root = str(selfheal.paths.ROOT)
        ours = f'"C:\\py\\pythonw.exe" "{root}\\bot.py"'
        listing["out"] = ours
        assert selfheal.is_our_bot(4242) and not selfheal.is_our_bot(0) and not selfheal.is_our_bot(None)
        assert not selfheal.is_our_bot(True) and not selfheal.is_our_bot("12")
        listing["out"] = '"C:\\Windows\\notepad.exe" bot.py'
        assert not selfheal.is_our_bot(4242), "a recycled pid: same name is not enough"
        listing["out"] = ""
        assert not selfheal.is_our_bot(4242), "no such process"

        commands.clear()
        listing["out"] = ours
        assert selfheal.restart_bot(4242) is True
        assert commands == ["powershell.exe", "taskkill", "schtasks"], commands
        commands.clear()
        listing["out"] = ""
        assert selfheal.restart_bot(4242) is True and "taskkill" not in commands, "nothing to end: only start the task"
    finally:
        selfheal._run, selfheal.time.sleep = keep_run, keep_sleep

    # the policy in the scheduled check
    sent, restarts = [], []
    keep = (alerts.send_telegram, mstate.DATA_FILE, mstate.LOCK_FILE, alerts.selfheal.restart_bot)
    alerts.send_telegram = lambda text, url=None: sent.append(text) or True
    alerts.selfheal.restart_bot = lambda pid: restarts.append(pid) or True
    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-heal-"))
    mstate.DATA_FILE, mstate.LOCK_FILE = tmp / "saved_items.json", tmp / "monitor.lock"
    saved = mstate.load_saved_data()
    long_up, t0 = 99999, 10000.0
    stuck = {"poll": t0 - 3600, "ticker": t0 - 3600, "pid": 4242}
    try:
        heartbeat.FILE.write_text(json.dumps(stuck), encoding="utf-8")
        alerts.check_bot(saved, t0, uptime=60)
        assert restarts == [], "just after Windows started"
        alerts.check_bot(saved, t0, long_up)
        assert restarts == [4242] and sent == [], "restarted quietly, judged on the next check"
        alerts.check_bot(saved, t0 + 300, long_up)
        assert restarts == [4242] and sent == [], "the restart is given its chance"
        heartbeat.FILE.write_text(json.dumps({"poll": t0 + 890, "ticker": t0 + 890, "pid": 5}), encoding="utf-8")
        alerts.check_bot(saved, t0 + 900, long_up)
        assert len(sent) == 1 and "restarted it" in sent[0] and "bot is back" not in sent[0] and not saved["watch"], sent
        assert _legacy_markdown_ok(sent[0])

        # it keeps failing: three restarts, spaced out, then a human is told
        sent.clear(); restarts.clear()
        heartbeat.FILE.write_text(json.dumps(stuck), encoding="utf-8")
        for step in range(3):
            alerts.check_bot(saved, t0 + 2000 + step * 900, long_up)
        assert restarts == [4242] * 3 and sent == []
        alerts.check_bot(saved, t0 + 2000 + 3 * 900, long_up)
        assert len(sent) == 1 and "restarted it 3 time(s)" in sent[0] and _legacy_markdown_ok(sent[0]), sent
        alerts.check_bot(saved, t0 + 2000 + 4 * 900, long_up)
        assert len(sent) == 1 and len(restarts) == 3, "no more restarts, no repeat of the alert"
        heartbeat.FILE.write_text(json.dumps({"poll": t0 + 9000, "ticker": t0 + 9000, "pid": 5}), encoding="utf-8")
        alerts.check_bot(saved, t0 + 9010, long_up)
        assert "bot is back" in sent[-1] and not saved["watch"]

        # a check the bot started itself never restarts the bot
        sent.clear(); restarts.clear()
        heartbeat.FILE.write_text(json.dumps(stuck), encoding="utf-8")
        from uninotifier.monitor import config as mconfig
        mconfig.MANUAL_RUN = True
        try:
            alerts.check_bot(saved, t0 + 20000, long_up)
        finally:
            mconfig.MANUAL_RUN = False
        assert restarts == [] and len(sent) == 1 and "isn't responding" in sent[0]
    finally:
        alerts.send_telegram, mstate.DATA_FILE, mstate.LOCK_FILE, alerts.selfheal.restart_bot = keep
        heartbeat.FILE.unlink(missing_ok=True)


def test_a_broken_file_is_told_before_it_stops_the_bot():
    """An edit that breaks a file does not stop the running bot — it stops the next start (after
    Windows restarts), silently. The scheduled check compiles the project's files and tells you
    at once which line, once per mistake, and again when it is fixed; the "not responding"
    alert names the cause instead of advising to sign out and back in."""
    from uninotifier import selfheal
    from uninotifier.monitor import alerts, state as mstate

    assert selfheal.code_problem() is None, "this project's own files compile"
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "src" / "uninotifier" / "bot").mkdir(parents=True)
        (root / "monitor.py").write_text("import sys\n", encoding="utf-8")
        (root / "src" / "uninotifier" / "bot" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        assert selfheal.code_problem(root) is None
        (root / "bot.py").write_text('    """\ndoc\n"""\nimport sys\n', encoding="utf-8")      # the accident: an indented first line
        assert selfheal.code_problem(root) == "bot.py line 1: IndentationError: unexpected indent", selfheal.code_problem(root)
        (root / "bot.py").write_text('"""\ndoc\n"""\nimport sys\n', encoding="utf-8")
        (root / "src" / "uninotifier" / "bot" / "app.py").write_text("def main(:\n    pass\n", encoding="utf-8")
        found = selfheal.code_problem(root)
        assert found.startswith("src/uninotifier/bot/app.py line 1: SyntaxError"), found
        (root / "src" / "uninotifier" / "bot" / "app.py").write_bytes(b"\xff\xfe not text")
        assert selfheal.code_problem(root).startswith("src/uninotifier/bot/app.py: "), "unreadable is a problem too"
        assert not list(root.rglob("*.pyc")) and not list(root.rglob("__pycache__")), "nothing is written"

    sent, problems = [], {"now": None}
    keep = (alerts.send_telegram, mstate.DATA_FILE, mstate.LOCK_FILE, selfheal.code_problem)
    alerts.send_telegram = lambda text, url=None: sent.append(text) or True
    selfheal.code_problem = lambda root=None: problems["now"]
    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-code-"))
    mstate.DATA_FILE, mstate.LOCK_FILE = tmp / "saved_items.json", tmp / "monitor.lock"
    saved = mstate.load_saved_data()
    try:
        alerts.check_code(saved)
        assert sent == [] and "code_problem" not in saved["watch"], "nothing wrong: silence"
        problems["now"] = "bot.py line 1: IndentationError: unexpected indent"
        alerts.check_code(saved)
        assert len(sent) == 1 and sent[0].startswith("⚠️ *A project file is broken*") and _legacy_markdown_ok(sent[0]), sent
        assert "`bot.py line 1: IndentationError: unexpected indent`" in sent[0] and "git restore bot.py" in sent[0], sent[0]
        assert "will *not start again*" in sent[0]
        alerts.check_code(saved)
        assert len(sent) == 1, "once per mistake, not every 15 minutes"
        problems["now"] = "src/uninotifier/moodle.py line 9: SyntaxError: invalid syntax"
        alerts.check_code(saved)
        assert len(sent) == 2 and "git restore src/uninotifier/moodle.py" in sent[1], "a different mistake is told again"
        problems["now"] = None
        alerts.check_code(saved)
        assert len(sent) == 3 and sent[2].startswith("✅ *The project files are fine again*") and not saved["watch"], sent
        alerts.check_code(saved)
        assert len(sent) == 3, "and that is told once"

        # the "not responding" alert names the cause
        sent.clear()
        t0, stuck = 10000.0, {"poll": 6400.0, "ticker": 6400.0, "pid": 4242}
        heartbeat.FILE.write_text(json.dumps(stuck), encoding="utf-8")
        saved["watch"].update(bot_heals=selfheal.MAX_ATTEMPTS, bot_heal_at=t0 - 5000)
        problems["now"] = "bot.py line 1: IndentationError: unexpected indent"
        alerts.check_bot(saved, t0, uptime=99999)
        assert len(sent) == 1 and "🔎 *Cause:* `bot.py line 1: IndentationError: unexpected indent`" in sent[0], sent
        assert "sign out and back in" not in sent[0] and _legacy_markdown_ok(sent[0]), sent[0]
        sent.clear()
        saved["watch"].pop("bot_alert_at", None)
        problems["now"] = None
        alerts.check_bot(saved, t0 + 100, uptime=99999)
        assert len(sent) == 1 and "sign out and back in" in sent[0], "no cause found: the general advice"
    finally:
        alerts.send_telegram, mstate.DATA_FILE, mstate.LOCK_FILE, selfheal.code_problem = keep
        heartbeat.FILE.unlink(missing_ok=True)


def test_schedule_validation_and_last_good():
    """schedule.json is checked as strictly as it is used: every mistake is named,
    and a broken file never switches the lecture reminders off — the last good copy
    stays in force until it is fixed, and you are told once per version of the mistake."""
    import copy
    good = json.loads(paths.SCHEDULE_FILE.read_text(encoding="utf-8"))
    assert schedule.validate(good) == [], schedule.validate(good)

    def broken(mutate):
        data = copy.deepcopy(good)
        mutate(data)
        return schedule.validate(data)

    cases = {
        "unknown setting": lambda d: d.update(lecture_reminder_minute=30),
        "unknown day": lambda d: d["days"].update(mond=[]),
        "missing end": lambda d: d["days"]["wed"][0].pop("end"),
        "bad time": lambda d: d["days"]["wed"][0].update(start="7:00 PM"),
        "ends before it starts": lambda d: d["days"]["wed"][0].update(start="21:00"),
        "bad mode": lambda d: d["days"]["wed"][0].update(mode="Hybrid"),
        "bad minutes": lambda d: d.update(lecture_reminder_minutes=0),
        "removed setting": lambda d: d.update(morning_summary_hour=7),
        "bad remind_modes": lambda d: d.update(remind_modes=["Zoom"]),
        "teachers": lambda d: d.update(teachers={"Big Data": 5}),
        "finals dates": lambda d: d.update(finals={"from": "2027-01-04"}),
        "finals order": lambda d: d.update(finals={"from": "2027-02-01", "to": "2027-01-04"}),
        "days not an object": lambda d: d.update(days=[]),
    }
    for name, mutate in cases.items():
        assert broken(mutate), f"should be rejected: {name}"
    assert schedule.validate([1]) and schedule.validate("x")
    ok = copy.deepcopy(good)
    ok["finals"] = {"from": "2027-01-04", "to": "2027-01-31", "interval_minutes": 30}
    ok["_note"] = "comments are fine"
    assert schedule.validate(ok) == []

    _fresh_failsafe()
    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-schedule-"))
    keep = (schedule.SCHEDULE_FILE, schedule.LAST_GOOD_FILE, schedule._good, schedule._broken, dict(schedule._read))
    schedule.SCHEDULE_FILE, schedule.LAST_GOOD_FILE = tmp / "schedule.json", tmp / "last_good.json"
    schedule._good, schedule._broken = None, False
    schedule._read.update(path=None, text=None, data=None)
    try:
        # no file and no earlier copy: empty, and told
        assert schedule.load_schedule() == {"days": {}}
        failsafe.wait_idle()
        assert len(OUTBOX) == 1 and "OFF" in OUTBOX[0] and "copy schedule.example.json to schedule.json" in OUTBOX[0], OUTBOX
        assert schedule.load_schedule() == {"days": {}}
        failsafe.wait_idle()
        assert len(OUTBOX) == 1, "a file that stays missing is reported once"

        schedule.SCHEDULE_FILE.write_text(json.dumps(good), encoding="utf-8")
        assert schedule.load_schedule() == good and schedule.LAST_GOOD_FILE.exists()
        failsafe.wait_idle()
        assert "valid again" in OUTBOX[-1] and len(OUTBOX) == 2

        trailing = json.dumps(good)[:-1] + ",}"                 # the classic comma mistake
        schedule.SCHEDULE_FILE.write_text(trailing, encoding="utf-8")
        assert schedule.load_schedule() == good, "the last good copy stays in force"
        failsafe.wait_idle()
        assert len(OUTBOX) == 3 and "needs fixing" in OUTBOX[-1] and "not valid JSON" in OUTBOX[-1]
        assert "keep using the last good copy" in OUTBOX[-1] and _legacy_markdown_ok(OUTBOX[-1])
        assert schedule.load_schedule() == good
        failsafe.wait_idle()
        assert len(OUTBOX) == 3, "the same mistake is not repeated"

        worse = copy.deepcopy(good)
        worse["days"]["wed"][0]["mode"] = "Hybrid"
        schedule.SCHEDULE_FILE.write_text(json.dumps(worse), encoding="utf-8")
        assert schedule.load_schedule() == good
        failsafe.wait_idle()
        assert len(OUTBOX) == 4 and "Hybrid" in OUTBOX[-1], "another mistake is a new report"

        # a restart while it is still broken still falls back to the copy on disk
        schedule._good, schedule._broken = None, False
        schedule._read.update(path=None, text=None, data=None)
        assert schedule.load_schedule() == good

        schedule.SCHEDULE_FILE.write_text(json.dumps(ok), encoding="utf-8")
        assert schedule.load_schedule() == ok
        failsafe.wait_idle()
        assert "valid again" in OUTBOX[-1]
        edited = copy.deepcopy(ok)
        edited["lecture_reminder_minutes"] = 15
        schedule.SCHEDULE_FILE.write_text(json.dumps(edited), encoding="utf-8")
        assert schedule.load_schedule()["lecture_reminder_minutes"] == 15, "an edit takes effect on the next call"
        sent = len(OUTBOX)
        edited["lecture_reminder_minutes"] = 20                   # saved by Notepad as "UTF-8 with BOM"
        schedule.SCHEDULE_FILE.write_text("﻿" + json.dumps(edited), encoding="utf-8")
        assert schedule.load_schedule()["lecture_reminder_minutes"] == 20, "a byte-order mark is not a mistake"
        failsafe.wait_idle()
        assert len(OUTBOX) == sent, OUTBOX[sent:]
    finally:
        schedule.SCHEDULE_FILE, schedule.LAST_GOOD_FILE, schedule._good, schedule._broken = keep[:4]
        schedule._read.update(keep[4])
        schedule._arabic_to_english.cache_clear()


def test_state_backup_and_recovery():
    """saved_items.json: backed up after a good run, kept to 14 days, and an unreadable
    file is replaced by the newest good backup (and reported) instead of silently
    starting over. An empty file is NOT corruption (it is how --announce is used)."""
    from uninotifier import fileio
    from uninotifier.monitor import state as mstate

    _fresh_failsafe()
    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-state-"))
    keep = (mstate.DATA_FILE, mstate.LOCK_FILE)
    mstate.DATA_FILE, mstate.LOCK_FILE = tmp / "saved_items.json", tmp / "monitor.lock"
    try:
        content = {"courses": {"1": {"name": "Big Data"}}, "content": {"1-1": {"name": "Slides"}}}
        fileio.write_json_atomic(mstate.DATA_FILE, content)
        assert not list(tmp.glob("*.tmp")), "the temporary file is gone"
        mstate.backup_state()
        folder = mstate.backup_dir()
        today = time.strftime("%Y-%m-%d")
        assert json.loads((folder / "saved_items.latest.json").read_text(encoding="utf-8")) == content
        assert (folder / f"saved_items.{today}.json").exists()

        for n in range(20):                                   # old days: only the newest 14 days stay
            (folder / f"saved_items.2020-01-{n + 1:02d}.json").write_text("{}", encoding="utf-8")
        mstate.backup_state()
        days = sorted(p.name for p in folder.glob("saved_items.????-??-??.json"))
        assert len(days) == mstate.BACKUP_KEEP_DAYS and f"saved_items.{today}.json" in days, days

        # an empty baseline or an unreadable file never replaces a good backup
        mstate.DATA_FILE.write_text("{}", encoding="utf-8")
        mstate.backup_state()
        mstate.DATA_FILE.write_text("{ not json", encoding="utf-8")
        mstate.backup_state()
        assert json.loads((folder / "saved_items.latest.json").read_text(encoding="utf-8")) == content

        # empty is a valid state (--announce): loaded as it is, nobody alarmed
        mstate.DATA_FILE.write_text("{}", encoding="utf-8")
        loaded = mstate.load_saved_data()
        assert loaded["courses"] == {} and set(mstate.STATE_KEYS) <= set(loaded) and OUTBOX == []

        # unreadable (also: valid JSON that is not an object) -> restored from the backup, reported
        for damage in ("{ not json", "[1, 2]", ""):
            OUTBOX.clear()
            failsafe.STATE_FILE.unlink(missing_ok=True)
            mstate.DATA_FILE.write_text(damage, encoding="utf-8")
            loaded = mstate.load_saved_data()
            assert loaded["courses"] == content["courses"] and loaded["content"] == content["content"], damage
            assert json.loads(mstate.DATA_FILE.read_text(encoding="utf-8")) == content, "the file itself is repaired"
            assert len(OUTBOX) == 1 and "restored from a backup" in OUTBOX[0] and _legacy_markdown_ok(OUTBOX[0]), OUTBOX
            assert list(folder.glob("saved_items.corrupt-*.json")), "the damaged file is kept"

        # no backup at all: a fresh baseline, said out loud
        for p in folder.glob("saved_items.*.json"):
            if "corrupt" not in p.name:
                p.unlink()
        OUTBOX.clear()
        failsafe.STATE_FILE.unlink(missing_ok=True)
        mstate.DATA_FILE.write_text("{ not json", encoding="utf-8")
        loaded = mstate.load_saved_data()
        assert loaded["courses"] == {} and len(OUTBOX) == 1 and "no backup" in OUTBOX[0]
        assert len(list(folder.glob("saved_items.corrupt-*.json"))) <= mstate.CORRUPT_KEEP
    finally:
        mstate.DATA_FILE, mstate.LOCK_FILE = keep


def test_monitor_pruning_and_background_pings():
    """A run no longer asks the portal about every archived file (each is asked once per two hours,
    spread evenly over the runs; /check asks about all of them), and its two healthchecks.io pings
    go out beside the work, in order, without the process ending before the last one is out."""
    import threading
    from uninotifier import healthcheck
    from uninotifier.monitor import config as mconfig, scan as mscan

    # --- each file's turn
    ids = [f"{course}-{n}" for course in (101, 102, 103, 104) for n in range(8)]
    runs = round(mconfig.FILE_PROBE_SECONDS / mconfig.SCAN_SLOT_SECONDS)
    assert runs == 8
    base = 1_800_000_000 // mconfig.SCAN_SLOT_SECONDS * mconfig.SCAN_SLOT_SECONDS
    per_slot = []
    for slot in range(runs):
        now = base + slot * mconfig.SCAN_SLOT_SECONDS + 44          # a run lands anywhere inside its slot
        per_slot.append({i for i in ids if mscan.probe_due(i, now)})
    for i in ids:
        assert sum(i in due for due in per_slot) == 1, f"{i}: exactly one turn per {runs} runs"
    assert sum(len(d) for d in per_slot) == len(ids) and max(len(d) for d in per_slot) <= 3 * len(ids) // runs, [len(d) for d in per_slot]
    assert all(mscan.probe_due(i, base + 7, manual=True) for i in ids), "/check asks about every file"
    again = {i for i in ids if mscan.probe_due(i, base + 3 * mconfig.SCAN_SLOT_SECONDS + 9 * runs * mconfig.SCAN_SLOT_SECONDS)}
    assert again == per_slot[3], "the same file has the same turn every cycle"
    keep_every = mconfig.FILE_PROBE_SECONDS
    mconfig.FILE_PROBE_SECONDS = mconfig.SCAN_SLOT_SECONDS
    try:
        assert all(mscan.probe_due(i, base + 5) for i in ids), "probing every run is a setting away"
    finally:
        mconfig.FILE_PROBE_SECONDS = keep_every

    # --- the pings
    calls, gate = [], threading.Event()

    def fake_post(url, data=None, timeout=None):
        gate.wait(5)
        calls.append(url)
        if url.endswith("/boom"):
            raise ValueError("not a requests error")
        return type("R", (), {"raise_for_status": lambda self: None})()

    keep = (healthcheck.requests.post, os.environ.get(healthcheck.ENV_NAME))
    healthcheck.requests.post = fake_post
    os.environ[healthcheck.ENV_NAME] = "https://hc-ping.com/uuid-9"
    try:
        started = time.monotonic()
        healthcheck.start_bg()
        healthcheck.report_bg(0, "6 courses")
        assert time.monotonic() - started < 0.3, "the pings must not hold the run"
        assert healthcheck.wait(0.05) is False and calls == [], "still being sent"
        gate.set()
        assert healthcheck.wait(5) is True
        assert calls == ["https://hc-ping.com/uuid-9/start", "https://hc-ping.com/uuid-9"], ("start before the result", calls)
        # a ping that blows up does not stop the next ones
        calls.clear()
        os.environ[healthcheck.ENV_NAME] = "https://hc-ping.com/boom"
        healthcheck.report_bg(1, "x")
        os.environ[healthcheck.ENV_NAME] = "https://hc-ping.com/uuid-9"
        healthcheck.report_bg(0, "y")
        assert healthcheck.wait(5) and calls[-1] == "https://hc-ping.com/uuid-9", calls
    finally:
        gate.set()
        healthcheck.requests.post = keep[0]
        os.environ.pop(healthcheck.ENV_NAME, None)
        if keep[1] is not None:
            os.environ[healthcheck.ENV_NAME] = keep[1]


def test_your_own_data_survives_a_damaged_file():
    """reminders.json and gpa.json hold what you typed in — nothing can rebuild them. A damaged
    file used to read as empty and the next save wiped everything; now it is kept aside, the
    newest backup is put back, and you are told. Every save leaves a backup."""
    from uninotifier import userdata

    keep = (reminders.STATE_FILE, gpa.STATE_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        reminders.STATE_FILE, gpa.STATE_FILE = Path(tmp) / "reminders.json", Path(tmp) / "gpa.json"
        backups = Path(tmp) / "backups"
        _fresh_failsafe()
        try:
            assert reminders.load()["items"] == [] and not OUTBOX, "a file that does not exist yet is simply empty"
            first = reminders.add("Bring the lab report", time.time() + 3600)
            reminders.add("Office hours", time.time() + 7200)
            assert (backups / "reminders.latest.json").is_file()
            assert len(list(backups.glob("reminders.????-??-??.json"))) == 1, "one backup per day"

            # a damaged file (a power cut, a bad edit): restored from the backup, never read as empty
            reminders.STATE_FILE.write_text('{"next_id": 3, "items": [{"id": 1, "no', encoding="utf-8")
            restored = reminders.load()
            assert [r["note"] for r in restored["items"]] == ["Bring the lab report", "Office hours"], restored
            assert reminders.get(first["id"])["note"] == "Bring the lab report", "the file itself is repaired"
            assert len(list(backups.glob("reminders.corrupt-*.json"))) == 1, "the damaged file is kept to look at"
            failsafe.wait_idle()
            assert len(OUTBOX) == 1 and "restored from a backup" in OUTBOX[0] and "reminders" in OUTBOX[0], OUTBOX
            # adding after the repair keeps what was there
            reminders.add("Third", time.time() + 9000)
            assert len(reminders.load()["items"]) == 3

            # not a JSON object at all, and no backup left: empty — and said so
            for old in backups.glob("reminders.*"):
                old.unlink()
            _fresh_failsafe()
            reminders.STATE_FILE.write_text("[1, 2]", encoding="utf-8")
            assert reminders.load()["items"] == []
            failsafe.wait_idle()
            assert len(OUTBOX) == 1 and "no backup" in OUTBOX[0], OUTBOX

            # a file that exists but cannot be opened is not "empty" either: it raises, nothing is saved over it
            assert not reminders.STATE_FILE.exists(), "the damaged file was moved aside"
            reminders.STATE_FILE.mkdir()
            try:
                reminders.load()
                raise AssertionError("an unopenable file must not read as empty")
            except OSError:
                pass
            reminders.STATE_FILE.rmdir()

            # the Grade Needed plan the same way
            _fresh_failsafe()
            state = gpa.load()
            state["courses"] = {"Database Systems": {"hours": 3, "items": []}}
            gpa.save(state)
            gpa.STATE_FILE.write_text("\x00\x00", encoding="utf-8")
            assert "Database Systems" in gpa.load()["courses"]
            failsafe.wait_idle()
            assert len(OUTBOX) == 1 and "Grade Needed plan" in OUTBOX[0], OUTBOX
            assert userdata.backup_dir(gpa.STATE_FILE) == backups
        finally:
            reminders.STATE_FILE, gpa.STATE_FILE = keep


def test_catch_alls_tell_the_owner():
    """A catch-all keeps the bot and the check running — but a bug it swallows must not leave a
    feature switched off with only a log line to show for it: the owner is told (failsafe.bug,
    once per bug). /check never stays on "Checking the portal…". And no new silent catch-all can
    slip into the modules that run unattended."""
    import ast
    import subprocess

    bot = _package("uninotifier.bot")
    checks = importlib.import_module("uninotifier.bot.checks")

    def boom(*a, **k):
        raise KeyError("text")

    keep = (schedule.due_messages, notifier.send_message, subprocess.run, bot.edit_view)
    _fresh_failsafe()
    try:
        # a lecture alert whose sending crashes
        schedule.due_messages = lambda sched, now: [("2099-01-01-test-before", "before", "⏰ text", "Big Data")]
        notifier.send_message = boom
        assert lectures.send_due("T", "42") == 0
        lectures.release("2099-01-01-test-before")
        # a reminder whose sending crashes
        reminders.add("Lab report", time.time() - 60)
        assert reminders.send_due("T", "42") == 0
        failsafe.wait_idle()
        assert any("lecture alert could not be sent" in t for t in OUTBOX), OUTBOX
        assert any("reminder could not be sent" in t for t in OUTBOX), OUTBOX

        # /check crashing: reported, and the screen says so instead of "Checking…" forever
        screens = []
        bot.edit_view = lambda message_id, text, rows: screens.append(text)
        subprocess.run = boom
        checks.run_check(7)
        failsafe.wait_idle()
        assert screens[0].startswith("🔄") and "Check Failed" in screens[-1], screens
        assert any("/check failed" in t for t in OUTBOX), OUTBOX
        assert not checks._check_lock.locked()
    finally:
        schedule.due_messages, notifier.send_message, subprocess.run, bot.edit_view = keep
        reminders.STATE_FILE.unlink(missing_ok=True)

    # the guard: in the unattended code, a handler that logs an exception also tells the owner —
    # or is listed here with the reason it need not
    told_elsewhere = {
        ("bot/checks.py", "Could not show that the check failed"),            # the failure itself is reported
        ("bot/finals_flow.py", "Finals check failed"),                       # shown on the Finals screen / notice
        ("bot/portal_flow.py", "Portal request failed"),                     # shown as the answer, Try Again
        ("monitor/run.py", "%s failure while talking to the portal%s"),      # alerts.record_failure
        ("monitor/scan.py", "Failed scanning course %s — skipping it this run"),   # read in the browser; watchdog
        ("monitor/scan.py", "Could not keep the screenshot of the link source"),   # the link itself is kept
        ("monitor/scan.py", "Could not picture the grade of %s"),                  # the grade is told without it
        ("monitor/notify.py", "Could not forward attachment %s"),            # one file; the post still goes out
        ("monitor/notify.py", "Could not download forum attachment %s"),     # one file; the post still goes out
        ("monitor/state.py", "Could not set the unreadable state file aside"),     # the recovery reports right after
        ("monitor/state.py", "Could not put the backup back"),                     # ... likewise
        ("userdata.py", "Could not set the unreadable %s aside"),            # the recovery reports right after
        ("userdata.py", "Could not put the backup of %s back"),              # ... likewise
    }
    root = Path(lectures.__file__).parent
    guarded = ["monitor/run.py", "monitor/scan.py", "monitor/notify.py", "monitor/alerts.py", "monitor/state.py",
               "lectures.py", "reminders.py", "userdata.py", "gitsync.py", "bot/checks.py", "bot/finals_flow.py",
               "bot/portal_flow.py", "bot/guest_flow.py", "probe.py", "cambridge.py"]
    silent = []
    for name in guarded:
        for handler in ast.walk(ast.parse((root / name).read_text(encoding="utf-8"))):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            body = "\n".join(ast.unparse(statement) for statement in handler.body)
            logged = [n.args[0].value for statement in handler.body for n in ast.walk(statement)
                      if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "exception"
                      and n.args and isinstance(n.args[0], ast.Constant)]
            if logged and "failsafe." not in body and (name, logged[0]) not in told_elsewhere:
                silent.append(f"{name}: {logged[0]}")
    assert not silent, ("catch-alls that only log — report with failsafe.bug, or list the reason", silent)


def test_state_files_are_written_through_fileio():
    """Every state file is replaced in one step after its data is flushed to the disk (fileio), so a
    power cut cannot leave one empty. A hand-made "write a .tmp, then rename" skips the flush — none
    may come back."""
    root = Path(lectures.__file__).parent
    pattern = re.compile(r"""with_suffix\(\s*["'][^"']*tmp["']\s*\)|\btmp\.replace\(""")
    found = [f"{path.relative_to(root).as_posix()}:{n}"
             for path in sorted(root.rglob("*.py")) if path.name != "fileio.py"
             for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if pattern.search(line)]
    assert not found, ("write state files with fileio.write_json_atomic / write_text_atomic", found)


def test_two_writers_of_one_file_do_not_share_a_temporary_file():
    """The bot and a check write some of the same files (the alert record, the Telegram statistics,
    the check-in cache). With one shared "<name>.tmp" each could cut short the other's half-written
    copy, or find it already moved away. Every writer has a temporary file of its own, and a write
    that fails leaves none behind."""
    import threading
    from uninotifier import fileio

    tmp = Path(tempfile.mkdtemp())
    target = tmp / "state.json"
    used, real = [], os.replace

    def record(src, dst):
        used.append(Path(src).name)
        real(src, dst)

    def refuse(src, dst):
        raise OSError("the disk is full")

    try:
        os.replace = record
        workers = [threading.Thread(target=fileio.write_json_atomic, args=(target, {"n": n})) for n in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        fileio.write_json_atomic(target, {"n": "main"})
        assert len(set(used)) == 3 and all(name.endswith(".tmp") for name in used), used
        assert json.loads(target.read_text(encoding="utf-8")) == {"n": "main"} and not list(tmp.glob("*.tmp"))
        os.replace = refuse
        try:
            fileio.write_json_atomic(target, {"n": "lost"})
            raise AssertionError("the failure must reach the caller")
        except OSError as e:
            assert "disk is full" in str(e)
        assert not list(tmp.glob("*.tmp")), "a failed write leaves its temporary file behind"
        assert json.loads(target.read_text(encoding="utf-8")) == {"n": "main"}, "the file itself is untouched"
    finally:
        os.replace = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_log_files_stay_small():
    """A log starts a new file at LOG_MAX_BYTES and keeps the last LOG_BACKUPS, newest first
    (monitor.log.1), so a year of running cannot fill the disk. Windows refuses to rename a log
    another process has open: the record being logged still lands, nothing is raised, and the
    roll happens at the next record once the file is free. Both entry points use it."""
    import inspect
    import logging.handlers

    assert "RotatingLogFile" in inspect.getsource(notifier.setup_logging)
    assert notifier.LOG_BACKUPS >= 2 and notifier.LOG_MAX_BYTES >= 1024 * 1024
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "monitor.log"
        handler = notifier.RotatingLogFile(path, maxBytes=400, backupCount=2, encoding="utf-8")
        logger = logging.Logger("rotation-test")
        logger.addHandler(handler)
        real_rename = os.rename
        try:
            for n in range(30):
                logger.warning("line %02d %s", n, "x" * 40)
            names = sorted(p.name for p in Path(tmp).iterdir())
            assert names == ["monitor.log", "monitor.log.1", "monitor.log.2"], names
            assert "line 29" in path.read_text(encoding="utf-8"), "the newest records are in the live file"
            assert "line 00" not in "".join(p.read_text(encoding="utf-8") for p in Path(tmp).iterdir()), "the oldest are gone"

            def busy(src, dst):
                raise PermissionError(13, "The process cannot access the file because it is being used by another process")
            os.rename = busy
            for n in range(30, 40):                      # every roll fails: still no exception, nothing lost
                logger.warning("line %02d %s", n, "x" * 40)
            assert "line 39" in path.read_text(encoding="utf-8")
            os.rename = real_rename
            logger.warning("line 40 %s", "x" * 40)
            assert "line 40" in "".join(p.read_text(encoding="utf-8") for p in Path(tmp).iterdir())
            assert path.stat().st_size < 400 + 100, "rolled again once the file was free"
        finally:
            os.rename = real_rename
            handler.close()


def test_state_backup_lives_in_its_own_repository():
    """The code repository never holds your state. gitsync --init makes data/ a git repository of its
    own that commits only the tracker and your own data — never sessions or caches — and each check
    commits a change (kept locally while there is no remote)."""
    import subprocess
    from uninotifier import gitsync
    repo = Path(tempfile.mkdtemp(prefix="uninotifier-backup-"))
    (repo / "saved_items.json").write_text('{"courses": {}}', encoding="utf-8")
    (repo / "gpa.json").write_text("{}", encoding="utf-8")
    (repo / "auth_state.json").write_text('{"cookies": "secret"}', encoding="utf-8")
    (repo / "logs").mkdir()
    (repo / "logs" / "monitor.log").write_text("x", encoding="utf-8")
    keep = gitsync.STATE_REPO
    gitsync.STATE_REPO = repo
    try:
        assert not gitsync.enabled()
        gitsync.sync_state()                                   # off: does nothing, creates nothing
        assert not (repo / ".git").exists()
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            assert gitsync.init() == 0 and gitsync.enabled()

        def tracked():
            return sorted(subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout.split())
        assert tracked() == [".gitignore", "gpa.json", "saved_items.json"], tracked()
        with contextlib.redirect_stdout(io.StringIO()):
            assert gitsync.init() == 0, "running --init again is harmless"
        (repo / "saved_items.json").write_text('{"courses": {"1": "x"}}', encoding="utf-8")
        (repo / "reminders.json").write_text("[]", encoding="utf-8")
        gitsync.sync_state()
        log_lines = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout.split("\n")
        assert log_lines[0] == gitsync.COMMIT_MESSAGE and "reminders.json" in tracked() and "auth_state.json" not in tracked()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
        assert status == "", status
    finally:
        gitsync.STATE_REPO = keep
        shutil.rmtree(repo, ignore_errors=True)


def test_a_state_file_that_is_busy_is_not_taken_for_a_damaged_one():
    """Windows refuses to open a file for a moment while another process replaces it (the bot
    writing a "done" mark) or scans it. That is not a damaged file: reading it as one set the good
    state aside as "corrupt" and went back to an older backup, with an alarm. A file that cannot
    be OPENED is tried again, and then fails the check (the next one reads it); only a file whose
    CONTENT is unreadable is recovered from a backup."""
    from uninotifier.monitor import state

    class File:
        def __init__(self, failures: int, text: str = '{"courses": {"1": {}}}'):
            self.failures, self.text, self.reads = failures, text, 0

        def exists(self):
            return True

        def read_text(self, encoding=None):
            self.reads += 1
            if self.reads <= self.failures:
                raise PermissionError(13, "The process cannot access the file because it is being used")
            return self.text

    def never():
        raise AssertionError("a busy file is not a damaged one")

    keep = state.DATA_FILE, state.recover_state, getattr(state, "READ_WAIT", None)
    recovered = []
    try:
        state.READ_WAIT, state.recover_state = 0, never
        state.DATA_FILE = File(failures=2)
        assert state.load_saved_data()["courses"] == {"1": {}} and state.DATA_FILE.reads == 3
        state.DATA_FILE = File(failures=99)
        try:
            state.load_saved_data()
            raise AssertionError("a file that stays locked must fail the check, not start a new baseline")
        except PermissionError:
            pass
        state.recover_state = lambda: recovered.append(1) or {}
        state.DATA_FILE = File(failures=0, text='{"courses": {"1"')
        assert state.load_saved_data()["courses"] == {} and recovered == [1], "damaged content is still recovered"
    finally:
        state.DATA_FILE, state.recover_state, state.READ_WAIT = keep


def test_a_damaged_session_file_does_not_stop_the_browser():
    """Chromium refuses to start a context from a session file that is not JSON (measured: an empty
    or cut-short file raises JSONDecodeError), and Playwright writes that file in place — so a power
    cut while it was being written made every check fail until the file was deleted by hand. A
    file that cannot be read is left out (the check signs in again), and the session is saved
    through fileio, which never leaves half a file."""
    from uninotifier import fileio
    from uninotifier.monitor import scan, state

    started = []

    class Context:
        def new_page(self):
            return object()

        def storage_state(self, path=None):
            assert path is None, "the session must be written by fileio, not in place by the browser"
            return {"cookies": [{"name": "MoodleSession", "value": "fresh"}], "origins": []}

    class Chromium:
        def launch(self, **kwargs):
            return self

        def new_context(self, storage_state=None, **kwargs):
            if storage_state is not None:
                json.loads(Path(storage_state).read_text(encoding="utf-8"))     # what Playwright does
            started.append(storage_state)
            return Context()

    class Playwright:
        chromium = Chromium()

        def start(self):
            return self

    keep = scan.sync_playwright, scan.install_virtual_authenticator, state.SESSION_FILE, fileio.write_json_atomic
    tmp = Path(tempfile.mkdtemp())
    written = []
    try:
        scan.sync_playwright, scan.install_virtual_authenticator = Playwright, lambda context, page: None
        state.SESSION_FILE = tmp / "auth_state.json"
        for damaged in ("", '{"cookies": [{"name": "Moodle'):
            state.SESSION_FILE.write_text(damaged, encoding="utf-8")
            browser = scan.Browser()
            assert browser.page is not None and started[-1] is None, f"{damaged!r} must not be handed to the browser"
        state.SESSION_FILE.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        browser = scan.Browser()
        assert browser.page is not None and started[-1] == str(state.SESSION_FILE), "a good file is used"
        fileio.write_json_atomic = lambda path, data: (written.append(path), keep[3](path, data))[1]
        browser.save_state()
        assert written == [state.SESSION_FILE], written
        assert json.loads(state.SESSION_FILE.read_text(encoding="utf-8"))["cookies"][0]["value"] == "fresh"
    finally:
        scan.sync_playwright, scan.install_virtual_authenticator, state.SESSION_FILE, fileio.write_json_atomic = keep
        shutil.rmtree(tmp, ignore_errors=True)
