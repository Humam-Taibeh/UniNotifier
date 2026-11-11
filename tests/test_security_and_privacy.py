"""Who may use the bot, what never leaves the PC, and the test run's own isolation."""

from support import (_as_owner, _fresh_failsafe, GUARD_HITS, _HOME, _legacy_markdown_ok, OUTBOX, OWNER_ID,
                     _package, portal_sections, REPO_PATH, _tg)

import importlib
import json
import logging
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

from uninotifier import access
from uninotifier import failsafe
from uninotifier import notifier
from uninotifier import paths
from uninotifier import schedule


def test_access_lock():
    """Only the owner is served: the id must match on `from` AND on a private chat of that same id,
    anything else is dropped silently (no reply, no button answer, no API call at all), the owner is
    told once per unknown id (never what was typed) and at most a few times a day, and a missing
    or malformed ADMIN_USER_ID fails closed."""
    bot = _package("uninotifier.bot")
    keep_owner = _as_owner(bot)
    calls, handled = [], []
    orig = (bot.handle_command, bot.handle_callback, bot.api)
    bot.api = lambda method, **data: calls.append(method)
    bot.handle_command = lambda text, message_id=None: handled.append(text)
    bot.handle_callback = lambda cb: handled.append(cb["data"])
    _fresh_failsafe()
    access._logged.clear()
    access._handled.clear()
    access._alerts.clear()
    try:
        # the owner, in every shape that is legitimately theirs
        assert access.is_owner(_tg(text="hi")) and access.is_owner(_tg(data="menu"))
        # ... and everything that is not
        strangers = {
            "another user, own private chat": _tg(user_id="777", chat_id="777", text="/start"),
            "another user writing in the owner's chat id": _tg(user_id="777", chat_id=OWNER_ID, text="/start"),
            "the owner's id, but from a group": _tg(chat_type="group", chat_id="-100123", text="/start"),
            "the owner's id in a supergroup": _tg(chat_type="supergroup", text="/start"),
            "a bot with the owner's id": _tg(is_bot=True, text="/start"),
            "the owner's user in someone else's chat": _tg(chat_id="777", text="/start"),
            "a channel post (no sender)": {"update_id": 2, "message": {"chat": {"id": -1001, "type": "channel"}, "text": "x"}},
            "no sender, no chat": {"update_id": 3, "message": {"text": "x"}},
            "an update of another kind": {"update_id": 4, "edited_message": {"from": {"id": int(OWNER_ID)}, "chat": {"id": int(OWNER_ID), "type": "private"}, "text": "x"}},
            "a stranger's button press": _tg(user_id="777", chat_id="777", data="menu"),
            "a button of the owner's chat pressed by someone else": _tg(user_id="777", chat_id=OWNER_ID, data="menu"),
        }
        for name, update in strangers.items():
            assert not access.is_owner(update), name
            bot.handle_update(update)
        assert handled == [] and calls == [], ("nothing is handled and nothing is sent to anyone", handled, calls)
        failsafe.wait_idle()
        # the owner is told, once per id: 777, -100123/owner-group, bot, unknown ... each distinct sender id once
        told = [t for t in OUTBOX if "not you" in t]
        assert told and all("Someone who is not you" in t for t in told)
        assert len(told) == 3, ("one alert per distinct sender id: 777, the owner's id from a group/bot, and 'unknown'", told)
        first = [t for t in told if "id 777" in t]
        assert len(first) == 1, "once per unknown id"
        assert "ignored it" in first[0] and "/start" not in first[0] and "hunter2" not in first[0], "never what was typed"
        assert _legacy_markdown_ok(first[0])

        # a flood of different strangers cannot become a flood of alerts
        OUTBOX.clear()
        failsafe.STATE_FILE.unlink(missing_ok=True)
        access._alerts.clear()
        now = 1_000_000.0
        for n in range(30):
            access.deny(_tg(user_id=str(5000 + n), chat_id=str(5000 + n), text="spam"), now=now + n)
        failsafe.wait_idle()
        assert len([t for t in OUTBOX if "not you" in t]) == access.MAX_ALERTS_PER_DAY
        # a name that tries to break the message does not
        OUTBOX.clear()
        failsafe.STATE_FILE.unlink(missing_ok=True)
        access._alerts.clear()
        access.deny(_tg(user_id="888", chat_id="888", text="x", username="a_b*c`d", first_name="[x](http://evil)"), now=now + 99999)
        failsafe.wait_idle()
        assert len(OUTBOX) == 1 and _legacy_markdown_ok(OUTBOX[0]), OUTBOX

        # fails closed: no / malformed ADMIN_USER_ID means nobody is the owner, and the bot will not start
        for bad in ("", "abc", "-5", "0", "12 34", "1" * 30):
            access.ADMIN_USER_ID = bad if access._ID.fullmatch(bad) else ""
            assert not access.is_owner(_tg(text="hi")) and access.problems(OWNER_ID), bad
        access.ADMIN_USER_ID = OWNER_ID
        assert access.problems(OWNER_ID) == [] and access.problems("555"), "the answering chat must be the owner's"
        assert access.problems("")
    finally:
        bot.handle_command, bot.handle_callback, bot.api = orig
        bot.CHAT_ID, access.ADMIN_USER_ID = keep_owner

    # configuration: ADMIN_USER_ID wins, TELEGRAM_CHAT_ID is the fallback (and is flagged), junk is refused
    saved_env = {k: os.environ.get(k) for k in ("ADMIN_USER_ID", "TELEGRAM_CHAT_ID")}
    try:
        os.environ["ADMIN_USER_ID"], os.environ["TELEGRAM_CHAT_ID"] = " 123456 ", "999"
        assert access._admin_from_env() == "123456" and access.from_chat_id is False
        os.environ["ADMIN_USER_ID"] = ""
        assert access._admin_from_env() == "999" and access.from_chat_id is True
        os.environ["ADMIN_USER_ID"] = "not-a-number"
        assert access._admin_from_env() == "", "junk fails closed instead of falling back"
    finally:
        for k, v in saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        access._admin_from_env()
        access.ADMIN_USER_ID = keep_owner[1]


def test_no_personal_values_in_the_code():
    """Anyone can run it: paths, student ID and semester come from .env / schedule.json."""
    from uninotifier import archive
    bot = _package("uninotifier.bot")
    project = Path(__file__).resolve().parents[1]
    code = list((project / "src").rglob("*.py")) + list(project.glob("*.py"))
    # your own IDs, read from .env (so they are never written in this file either); a value still
    # copied from .env.example (what install.bat leaves before you fill it in) is nobody's
    from dotenv import dotenv_values
    from uninotifier import envcheck
    env = dotenv_values(project / ".env") if (project / ".env").is_file() else {}
    own_ids = {v.strip() for v in (env.get("STUDENT_ID"), env.get("PORTAL_USERNAME"),
                                   (env.get("ZUJ_USERNAME") or "").split("@")[0])
               if v and len(v.strip()) >= 6 and not envcheck._PLACEHOLDER.search(v.strip())}
    assert len(code) > 30, code
    for path in code:
        text = path.read_text(encoding="utf-8")
        for personal in ("Humam", "Year_04", *own_ids):
            assert personal not in text, (path.name, personal)
    for path in [*(project / "scripts").glob("*.ps1"), project / ".env.example", project / "schedule.example.json",
                 *project.glob("*.md"), *(project / "docs").glob("*.md")]:      # what a new student reads and runs
        text = path.read_text(encoding="utf-8").replace(REPO_PATH, "")      # the project's own public address is fine
        for personal in ("Humam", "Taibeh", "Year_04", *own_ids):
            assert personal not in text, (path.name, personal)
    for path in (project / "tests").glob("*.py"):           # the tests use a made-up ID, never the real one
        text = path.read_text(encoding="utf-8")
        for personal in own_ids:
            assert personal not in text, (path.name, "the real student ID")
    example = (project / ".env.example").read_text(encoding="utf-8")
    assert "\nARCHIVE_ROOT=\n" in example and "\nSTUDENT_ID=\n" in example, "optional settings are empty: off until filled in"
    assert archive.ARCHIVE_ROOT.is_absolute() and bot.semester() == schedule.load_schedule().get("semester", "")
    assert set(portal_sections()) == {"transcript", "grades", "balance", "home"}, "a new portal page is a decision (home: the timetable)"


def test_forwarded_messages_are_never_acted_on():
    """A forward carries someone else's words: a forwarded "123456" would pass for a verification
    code, a forwarded "/guest" for a command. No forward is acted on — the owner's own is ignored
    without an alert (the owner is no intruder), a stranger's is dropped and reported as usual."""
    bot = _package("uninotifier.bot")
    keep_owner = _as_owner(bot)
    calls, handled = [], []
    orig = (bot.handle_command, bot.handle_callback, bot.api)
    bot.api = lambda method, **data: calls.append(method)
    bot.handle_command = lambda text, message_id=None: handled.append(text)
    bot.handle_callback = lambda cb: handled.append(cb["data"])
    _fresh_failsafe()
    access._logged.clear()
    access._handled.clear()
    access._alerts.clear()

    def forwarded(update, **marker):
        update["message"].update(marker)
        return update
    try:
        owner_forwards = [
            forwarded(_tg(text="123456"), forward_origin={"type": "user", "date": 1, "sender_user": {"id": 99}}),
            forwarded(_tg(text="/guest"), forward_origin={"type": "hidden_user", "date": 1, "sender_user_name": "X"}),
            forwarded(_tg(text="/start"), forward_from={"id": 99}, forward_date=1),          # older Bot API fields
            forwarded(_tg(text="/start"), forward_date=1),
            forwarded(_tg(text="/start"), is_automatic_forward=True),
            # sent from the owner's account through an inline bot: that bot wrote the text
            forwarded(_tg(text="123456"), via_bot={"id": 777000, "is_bot": True, "username": "somebot"}),
        ]
        for update in owner_forwards:
            assert access.is_forwarded(update) and not access.is_owner(update), update
            bot.handle_update(update)
        assert handled == [] and calls == [], ("a forward is never acted on", handled, calls)
        failsafe.wait_idle()
        assert OUTBOX == [], "the owner's own forward raises no alert"

        stranger = forwarded(_tg(user_id="777", chat_id="777", text="/start"), forward_origin={"type": "user", "date": 1})
        bot.handle_update(stranger)
        failsafe.wait_idle()
        assert handled == [] and len(OUTBOX) == 1 and "forwarded message" in OUTBOX[0], OUTBOX

        # the owner's ordinary messages and buttons are untouched
        assert access.is_owner(_tg(text="/start")) and access.is_owner(_tg(data="menu"))
        bot.handle_update(_tg(text="/start"))
        assert handled == ["/start"]
    finally:
        bot.handle_command, bot.handle_callback, bot.api = orig
        bot.CHAT_ID, access.ADMIN_USER_ID = keep_owner


def test_secrets_never_reach_logs_or_alerts():
    """Every .env value (also URL-encoded, as inside a request), the bot token, Moodle's session key
    and cookie and a healthchecks.io address are masked in log lines, tracebacks and alerts. .env is
    read again when it changes; short settings (BOT_POLL_TIMEOUT=20) are not secrets."""
    from uninotifier import maintenance

    password = "Hunter2!x y_z"
    token = "123456789:AAH" + "x" * 32
    keep = (notifier.SECRETS_FILE, notifier.SECRETS_RECHECK_SECONDS)
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_text(f'ZUJ_PASSWORD="{password}"\nTELEGRAM_BOT_TOKEN={token}\nPORTAL_USERNAME=student4711\n'
                       "BOT_POLL_TIMEOUT=20\nHEALTHCHECK_URL=https://hc-ping.com/0f1e2d3c-aaaa-bbbb\n", encoding="utf-8")
        notifier.SECRETS_FILE, notifier.SECRETS_RECHECK_SECONDS = env, 0
        notifier.reset_secrets()
        _fresh_failsafe()
        try:
            leaky = (f"POST /login password={password} user=student4711 encoded=Hunter2%21x%20y_z plus=Hunter2%21x+y_z "
                     f"https://api.telegram.org/bot{token}/sendMessage "
                     "HTTPSConnectionPool(host='elearning.zuj.edu.jo'): Max retries exceeded with url: "
                     "/lib/ajax/service.php?sesskey=AbC123xyz&info=core_course "
                     '{"sesskey":"Zz9Yy8"} Cookie: MoodleSession=k3y5ecret; MoodleSessionzuj=0ther '
                     "{'MoodleSession': 'pyrepr'} ping https://hc-ping.com/5a5a5a5a-1111-2222/fail "
                     "timeout 20 s")
            clean = notifier.redact(leaky)
            for secret in (password, "Hunter2%21x%20y_z", "Hunter2%21x+y_z", "student4711", token, "AbC123xyz",
                           "Zz9Yy8", "k3y5ecret", "0ther", "pyrepr", "5a5a5a5a", "0f1e2d3c"):
                assert secret not in clean, (secret, clean)
            assert "<ZUJ_PASSWORD>" in clean and "<PORTAL_USERNAME>" in clean and "timeout 20 s" in clean, clean
            assert "sesskey=<redacted>" in clean and "hc-ping.com/<redacted>" in clean

            # a traceback in the log file
            try:
                raise ConnectionError(f"login as student4711 with {password} refused")
            except ConnectionError:
                record = logging.LogRecord("uninotifier", logging.ERROR, __file__, 1, "Sign-in failed for %s",
                                           ("student4711",), sys.exc_info())
            line = notifier.RedactingFormatter("%(message)s").format(record)
            assert "Traceback" in line and password not in line and "student4711" not in line, line

            # an alert: headline, detail and the exception line
            failsafe.report("test:secret", "Sign-in as student4711 failed", detail=f"used {password}",
                            exc=RuntimeError(f"bot{token} sesskey=AbC123xyz"))
            assert len(OUTBOX) == 1 and all(s not in OUTBOX[0] for s in ("student4711", password, token, "AbC123xyz"))
            # the portal's "can't sign in" reason shown on Telegram
            error = maintenance.unavailable(None, "portal", TimeoutError("page.goto timed out: ...?u=student4711"))
            assert "student4711" not in error.detail

            # .env changed: the new value is masked from then on
            env.write_text(env.read_text(encoding="utf-8") + "PORTAL_PASSWORD=Brand-New-Secret\n", encoding="utf-8")
            os.utime(env, (time.time() + 5, time.time() + 5))
            assert "Brand-New-Secret" not in notifier.redact("pw Brand-New-Secret")
        finally:
            notifier.SECRETS_FILE, notifier.SECRETS_RECHECK_SECONDS = keep
            notifier.reset_secrets()


def test_guest_password_never_stays_in_the_chat():
    """The guest's password message is deleted — tried again when Telegram does not confirm it, and
    the owner asked to delete it by hand when it still stands. A reply after the password prompt
    expired is taken for the password: deleted, not read as anything else (a reminder note)."""
    bot = _package("uninotifier.bot")
    guest_flow = importlib.import_module("uninotifier.bot.guest_flow")
    reminder_flow = importlib.import_module("uninotifier.bot.reminder_flow")
    calls, views, outcomes = [], [], []

    def api(method, **data):
        calls.append((method, data))
        return outcomes.pop(0) if method == "deleteMessage" and outcomes else {"message_id": 99}

    keep = (bot.api, bot.send_view, guest_flow.DELETE_RETRY_SECONDS, reminder_flow._reminder_wait)
    bot.api, bot.send_view = api, lambda text, rows: views.append(text)
    guest_flow.DELETE_RETRY_SECONDS = (0, 0)
    try:
        # Telegram confirms only the third try: three calls, no warning
        outcomes[:] = [None, None, True]
        assert guest_flow.delete_password_message(41)
        assert [m for m, _ in calls] == ["deleteMessage"] * 3
        # never confirmed: the owner is asked to delete it, in a reply to that very message
        calls.clear()
        outcomes[:] = [None, None, None]
        assert not guest_flow.delete_password_message(42)
        warning = calls[-1]
        assert warning[0] == "sendMessage" and "delete it yourself" in warning[1]["text"]
        assert warning[1]["reply_parameters"]["message_id"] == 42

        # the password prompt expired, and a reminder note is being asked for: the reply is deleted, not used
        calls.clear()
        outcomes[:] = []
        noted = []
        keep_text = reminder_flow.handle_reminder_text
        reminder_flow.handle_reminder_text = noted.append
        reminder_flow._reminder_wait = "note"
        guest_flow._guest.clear()
        guest_flow._guest.update(step="password", user="20230001", at=time.time() - guest_flow.GUEST_TIMEOUT - 1, messages=[])
        try:
            bot.handle_command("S3cret-pass", 55)
        finally:
            reminder_flow.handle_reminder_text = keep_text
        assert ("deleteMessage", {"chat_id": bot.CHAT_ID, "message_id": 55}) in calls
        assert noted == [] and "timed out" in views[-1] and guest_flow._guest == {}

        # ... but a command after the expiry is a command, and a late student ID is not deleted
        calls.clear()
        guest_flow._guest.update(step="password", at=0, messages=[])
        bot.handle_command("/help", 56)
        assert not any(m == "deleteMessage" for m, _ in calls) and guest_flow._guest == {}
        calls.clear()
        guest_flow._guest.update(step="user", at=0, messages=[])
        reminder_flow._reminder_wait = None
        bot.handle_command("20230001", 57)
        assert not any(m == "deleteMessage" for m, _ in calls)
    finally:
        bot.api, bot.send_view, guest_flow.DELETE_RETRY_SECONDS, reminder_flow._reminder_wait = keep
        guest_flow._guest.clear()


def test_stranger_floods_cost_no_disk_access():
    """A stranger hammering the bot: the first update of an id goes to failsafe (which reads and
    writes its file); every further one is turned away in memory. Many ids at once: at most
    MAX_ALERTS_PER_DAY alerts, then nothing touches the disk; the memory stays bounded; stale
    entries are pruned, and expired access:* records leave failsafe.json once a day."""
    disk = {"load": 0, "save": 0}
    real_load, real_save = failsafe._load, failsafe._save

    def counted_load():
        disk["load"] += 1
        return real_load()

    def counted_save(state):
        disk["save"] += 1
        return real_save(state)

    keep = (access.ADMIN_USER_ID, access.MAX_TRACKED)
    access.ADMIN_USER_ID, access.MAX_TRACKED = OWNER_ID, 50
    failsafe._load, failsafe._save = counted_load, counted_save
    for table in (access._logged, access._handled, access._alerts):
        table.clear()
    access._pruned.update(memory=0.0, file=0.0)
    _fresh_failsafe()
    t0 = 1_000_000.0
    try:
        stranger = _tg(user_id="777", chat_id="777", text="/start")
        access.deny(stranger, now=t0)
        failsafe.wait_idle()
        after_first = dict(disk)
        for n in range(1, 500):
            access.deny(stranger, now=t0 + n)
        assert disk == after_first, ("repeats of a known id never touch the disk", after_first, disk)
        assert len(OUTBOX) == 1

        # a flood of different ids: a few alerts, then nothing reaches the disk; memory stays bounded
        for n in range(3000):
            access.deny(_tg(user_id=str(10_000 + n), chat_id=str(10_000 + n), text="x"), now=t0 + 600 + n * 0.01)
        failsafe.wait_idle()
        assert len(OUTBOX) == access.MAX_ALERTS_PER_DAY, len(OUTBOX)
        settled = dict(disk)
        for n in range(3000, 3500):
            access.deny(_tg(user_id=str(10_000 + n), chat_id=str(10_000 + n), text="x"), now=t0 + 700 + n * 0.01)
        assert disk == settled, "past the daily cap, a flood costs no disk access"
        assert len(access._handled) <= access.MAX_TRACKED and len(access._logged) <= access.MAX_TRACKED

        # a day later: stale entries are pruned, the id may alert again, and failsafe.json is cleaned up
        later = t0 + access.ALERT_COOLDOWN + 3600
        record = real_load()                       # failsafe holds back on the wall clock: a day passes there too
        record["access:777"]["at"] = time.time() - access.ALERT_COOLDOWN - 1
        real_save(record)
        access.deny(stranger, now=later)
        failsafe.wait_idle()
        assert all(later - t < access.ALERT_COOLDOWN for t in access._handled.values())
        assert len(OUTBOX) == access.MAX_ALERTS_PER_DAY + 1, "after its window, the id is told about again"

        # failsafe.forget drops only the expired records of the given prefix
        real_save({"access:1": {"at": 0}, "access:2": {"at": later}, "delivery": {"at": 0, "active": True}})
        assert failsafe.forget("access:", access.ALERT_COOLDOWN, now=later) == 1
        assert set(real_load()) == {"access:2", "delivery"}
    finally:
        failsafe._load, failsafe._save = real_load, real_save
        access.ADMIN_USER_ID, access.MAX_TRACKED = keep
        for table in (access._logged, access._handled, access._alerts):
            table.clear()


def test_the_run_is_isolated_from_your_files():
    """The run (and every process it starts) reads the made-up .env and writes the throw-away data
    folder — never your .env or data/. UNINOTIFIER_ENV / UNINOTIFIER_DATA are what make it so."""
    import subprocess

    bot = _package("uninotifier.bot")
    real_root = Path(__file__).resolve().parents[1]
    assert paths.ENV_FILE == _HOME / ".env" and paths.DATA == _HOME / "data"
    assert not str(paths.DATA).startswith(str(real_root)), "state lives outside the project"
    assert bot.BOT_TOKEN == "123456:test-token" and bot.CHAT_ID == "42", "only the made-up credentials are loaded"
    assert notifier.SECRETS_FILE == paths.ENV_FILE
    from uninotifier import gitsync
    assert not gitsync.enabled() and gitsync.STATE_REPO == paths.DATA, "a throw-away state folder is never a backup repository"
    child = subprocess.run(
        [sys.executable, "-c", "from uninotifier import paths; print(paths.DATA); print(paths.ENV_FILE)"],
        capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(real_root / "src")})
    assert child.stdout.split() == [str(_HOME / "data"), str(_HOME / ".env")], (child.stdout, child.stderr[-300:])


def test_the_guards_block_the_network_and_the_browser():
    """The suite-wide guards: a real request to any outside host is refused and recorded (even when
    the code would have swallowed the error), so is starting a browser; loopback keeps working for
    the tests' own servers. The project's own paths are guarded too — the monitor's Browser class."""
    import requests
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    scan = importlib.import_module("uninotifier.monitor.scan")
    try:
        for attempt in (lambda: requests.get("https://elearning.zuj.edu.jo/my/", timeout=5),
                        lambda: socket.create_connection(("93.184.216.34", 443), timeout=5)):
            try:
                attempt()
                raise AssertionError("an outside connection got through")
            except (requests.ConnectionError, OSError):
                pass
        assert any("elearning.zuj.edu.jo" in h for h in GUARD_HITS) and any("93.184.216.34" in h for h in GUARD_HITS), GUARD_HITS
        GUARD_HITS.clear()
        try:
            scan.Browser().page                                  # what the monitor does to start Chromium
            raise AssertionError("a browser got started")
        except RuntimeError:
            pass
        assert GUARD_HITS and "browser" in GUARD_HITS[0]
    finally:
        GUARD_HITS.clear()

    class Hello(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hi")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hello)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert requests.get(f"http://127.0.0.1:{server.server_port}/", timeout=5).text == "hi"
        assert not GUARD_HITS, "loopback is allowed"
    finally:
        server.shutdown()


def test_no_personal_files_are_tracked():
    """A clone gets the code and the examples — never someone's settings, tracker, timetable or session."""
    import subprocess
    project = Path(__file__).resolve().parents[1]
    if not (project / ".git").exists():
        print("  (skipped: not a git checkout)")
        return
    files = subprocess.run(["git", "ls-files"], cwd=project, capture_output=True, text=True).stdout.splitlines()
    assert files, "git ls-files returned nothing"
    personal = [f for f in files if f in (".env", "schedule.json") or f.startswith(("data/", "logs/", "assets/Weekly_"))]
    assert personal == [], personal
    for example in (".env.example", "schedule.example.json", "scripts/setup_tasks.ps1", "scripts/remove_tasks.ps1"):
        assert (project / example).is_file(), example
    ignored = subprocess.run(["git", "check-ignore", ".env", "schedule.json", "data/saved_items.json",
                              "assets/Weekly_Schedule.png"], cwd=project, capture_output=True, text=True).stdout.split()
    assert len(ignored) == 4, ignored
    assert schedule.validate(json.loads((project / "schedule.example.json").read_text(encoding="utf-8"))) == []
