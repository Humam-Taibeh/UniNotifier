"""The repository itself: settings, the doctor, the READMEs, the license, what a new student gets."""

from support import (_fresh_failsafe, _HOME, OUTBOX, PUBLIC_REPO, REPO_PATH, _with_env)

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from uninotifier import archive
from uninotifier import failsafe
from uninotifier import paths


def test_every_test_file_is_run():
    """A topic file that the runner does not list would never run: every tests/test_*.py but the
    runner itself is one of its TOPICS, and no test function is defined twice."""
    import ast
    tests_dir = Path(__file__).resolve().parent
    runner = (tests_dir / "test_architecture.py").read_text(encoding="utf-8")
    topics = {path.stem for path in tests_dir.glob("test_*.py")} - {"test_architecture"}
    assert topics and all(f'"{topic}"' in runner for topic in topics), sorted(topics)
    names = [node.name for topic in sorted(topics)
             for node in ast.parse((tests_dir / f"{topic}.py").read_text(encoding="utf-8")).body
             if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
    assert len(names) == len(set(names)) and len(names) > 100, len(names)
    lengths = {topic: len((tests_dir / f"{topic}.py").read_text(encoding="utf-8").splitlines()) for topic in topics}
    assert max(lengths.values()) < 2000, f"split the longest one: {lengths}"


def test_the_license_is_noncommercial():
    """The license file is the PolyForm Noncommercial text with the author's notice, and both READMEs say so."""
    text = (paths.ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("# PolyForm Noncommercial License 1.0.0") and "Required Notice: Copyright (c)" in text
    assert "MIT License" not in text
    for name in ("README.md", "README.ar.md"):
        readme = (paths.ROOT / name).read_text(encoding="utf-8")
        assert "license-PolyForm%20Noncommercial" in readme and "License: MIT" not in readme, name


def test_modules_import_quietly_and_names_resolve():
    """Importing any module is side-effect free (a tool once sent a Telegram message on
    import), and every `module.name` the code uses exists — the checks a split or a
    rename can silently break (the showcase tool lost monitor.deliver that way)."""
    import ast
    import importlib
    import subprocess

    probe = (
        "import importlib, pkgutil, socket, sys\n"
        "def blocked(*a, **k): raise AssertionError('network used while importing')\n"
        "socket.socket.connect = blocked\n"
        "socket.create_connection = blocked\n"
        "import uninotifier\n"
        "for m in pkgutil.walk_packages(uninotifier.__path__, 'uninotifier.'):\n"
        "    importlib.import_module(m.name)\n"
    )
    # ... and touches no file: the child imports everything into an empty state folder, which must
    # stay empty (views.py once read schedule.json at import and wrote schedule.last_good.json)
    with tempfile.TemporaryDirectory() as empty:
        done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120,
                              env={**os.environ, "UNINOTIFIER_DATA": empty})
        assert done.returncode == 0 and not done.stdout.strip(), (done.stdout + done.stderr)[-600:]
        written = sorted(p.name for p in Path(empty).rglob("*"))
        assert written == [], ("importing wrote into the state folder", written)

    root = Path(importlib.import_module("uninotifier").__file__).parent
    missing = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").startswith("uninotifier"):
                for a in node.names:
                    try:
                        mod = importlib.import_module(f"{node.module}.{a.name}")
                    except ImportError:
                        continue                       # a plain name, not a submodule
                    aliases[a.asname or a.name] = mod
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in aliases and not hasattr(aliases[node.value.id], node.attr)):
                missing.append(f"{path.relative_to(root)}:{node.lineno} {node.value.id}.{node.attr}")
    assert not missing, missing


def test_settings_are_checked_before_anything_runs():
    """A missing or still-example setting stops a run with a plain list of what to fix (exit code 2):
    in the log, and on Telegram when Telegram itself is set up — never a traceback."""
    from uninotifier import envcheck
    assert envcheck.problems() == [], envcheck.problems()        # the made-up .env is complete

    # values copied unchanged from .env.example count as not set
    example = {k: v for k, v in __import__("dotenv").dotenv_values(paths.ENV_EXAMPLE).items() if v}
    for name in ("ZUJ_USERNAME", "ZUJ_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        assert name not in example or _with_env({name: example[name]}, lambda n=name: envcheck.value(n)) == "", name
    for raw in ("your_password", "https://hc-ping.com/your-check-uuid", "https://x.org/class/<class-id>/a",
                "C:\\path\\to\\your\\semester\\folder", "..."):
        assert _with_env({"HEALTHCHECK_URL": raw}, lambda: envcheck.value("HEALTHCHECK_URL")) == "", raw
    assert _with_env({"ZUJ_PASSWORD": " s3cret "}, lambda: envcheck.value("ZUJ_PASSWORD")) == "s3cret"

    # who needs what: the bot runs without the Moodle password, the check doesn't
    missing = _with_env({"ZUJ_PASSWORD": ""}, lambda: (envcheck.problems("bot"), envcheck.problems("monitor")))
    assert missing[0] == [] and len(missing[1]) == 1 and missing[1][0].startswith("ZUJ_PASSWORD is not set"), missing
    shape = _with_env({"TELEGRAM_BOT_TOKEN": "not a token", "ZUJ_USERNAME": "no-at-sign"}, envcheck.problems)
    assert any(p.startswith("TELEGRAM_BOT_TOKEN doesn't look right") for p in shape)
    assert any(p.startswith("ZUJ_USERNAME doesn't look right") for p in shape), shape
    other = _with_env({"ADMIN_USER_ID": "7"}, envcheck.problems)
    assert other == ["TELEGRAM_CHAT_ID must be your private chat with the bot - the same number as ADMIN_USER_ID."], other
    warned = _with_env({"PORTAL_USERNAME": "u1234567", "PORTAL_PASSWORD": "", "CAMBRIDGE_UNTIL": "2020-01-31",
                        "CAMBRIDGE_ASSIGNMENTS_URL": "", "ARCHIVE_ROOT": "relative\\folder"}, envcheck.warnings)
    assert len(warned) == 4 and envcheck.problems() == [], warned

    # require(): the run ends with exit code 2 and the list; Telegram hears about it once
    _fresh_failsafe()

    def stopped(role):
        import contextlib
        import io
        try:
            with contextlib.redirect_stderr(io.StringIO()):      # the console copy of the list
                envcheck.require(role)
        except SystemExit as e:
            return e.code
        return None
    assert stopped("monitor") is None
    assert _with_env({"ZUJ_PASSWORD": ""}, lambda: stopped("monitor")) == envcheck.EXIT_CONFIG
    failsafe.wait_idle()
    assert len(OUTBOX) == 1 and "ZUJ\\_PASSWORD is not set" in OUTBOX[0], OUTBOX
    assert _with_env({"ZUJ_PASSWORD": ""}, lambda: stopped("monitor")) == envcheck.EXIT_CONFIG
    failsafe.wait_idle()
    assert len(OUTBOX) == 1, "repeated runs with the same mistake are held back"
    # without Telegram there is no one to tell: the log and the console only
    _fresh_failsafe()
    assert _with_env({"TELEGRAM_BOT_TOKEN": ""}, lambda: stopped("bot")) == envcheck.EXIT_CONFIG
    failsafe.wait_idle()
    assert OUTBOX == []
    text = envcheck.report(["X is not set - y."])
    assert "  - X is not set - y." in text and str(paths.ENV_FILE) in text and "uninotifier.doctor" in text


def test_the_setup_asks_five_things_and_writes_the_settings_file():
    """setup.py: a new student answers in the console and never edits a file. Each answer is asked again
    until it has the right shape, the token is checked with Telegram, the Telegram Id is found from the
    Start pressed in the bot, a password is never shown, and what is written to .env is read back to prove
    every value survived."""
    from dotenv import dotenv_values
    from uninotifier import setup

    class Script(setup.Console):
        def __init__(self, answers):
            self.answers, self.said, self.asked = list(answers), [], []

        def say(self, text=""):
            self.said.append(text)

        def ask(self, prompt, secret=False):
            self.asked.append((prompt, secret))
            if not self.answers:
                raise EOFError                  # what input() raises when the window is closed half-way
            return self.answers.pop(0)

    nothing = lambda name: ""                                   # noqa: E731 — no .env yet
    tmp = Path(tempfile.mkdtemp(prefix="uninotifier-setup-"))
    keep = (setup.restrict, setup.telegram_bot, setup.telegram_people, setup.telegram_hello, setup.requests.get,
            paths.ENV_FILE)
    setup.restrict = lambda path: True
    try:
        # --- what is written: every explanation of .env.example stays, only the values change
        env = tmp / ".env"
        tricky = {"ZUJ_USERNAME": "209900001@std-zuj.edu.jo", "ZUJ_PASSWORD": "p#ss w'ord \"x\" back\\slash",
                  "TELEGRAM_BOT_TOKEN": "111111111:AAAAAAAA-bbbb_cc", "TELEGRAM_CHAT_ID": "555", "ADMIN_USER_ID": "555",
                  "PORTAL_USERNAME": "209900001", "PORTAL_PASSWORD": "كلمة $ign=100%", "AUTO_CHECK_IN": "true"}
        assert setup.write_env(tricky, env) is True
        assert {k: dotenv_values(env)[k] for k in tricky} == tricky, "read back exactly, by the reader the program uses"
        text = env.read_text(encoding="utf-8")
        example = paths.ENV_EXAMPLE.read_text(encoding="utf-8")
        assert [l for l in text.splitlines() if l.startswith("#")] == [l for l in example.splitlines() if l.startswith("#")]
        assert "ZUJ_USERNAME=209900001@std-zuj.edu.jo\n" in text and "your_password" not in text
        assert setup.quoted("plain-1.2") == "plain-1.2" and setup.quoted("a b") == "'a b'" and setup.quoted("") == ""
        # an .env that is already there keeps everything else it holds
        env.write_text(text.replace("HEALTHCHECK_URL=", "HEALTHCHECK_URL=https://hc-ping.com/made-up", 1)
                       + "# my own note\nMY_OWN=1\n", encoding="utf-8")
        setup.write_env({"ZUJ_PASSWORD": "another one", "BOT_POLL_TIMEOUT": "30"}, env)
        again = dotenv_values(env)
        assert again["ZUJ_PASSWORD"] == "another one" and again["HEALTHCHECK_URL"] == "https://hc-ping.com/made-up"
        assert again["MY_OWN"] == "1" and again["BOT_POLL_TIMEOUT"] == "30" and "# my own note" in env.read_text(encoding="utf-8")
        # a value the file format would change is refused, and the file is put back as it was
        before = env.read_text(encoding="utf-8")
        try:
            setup.write_env({"ZUJ_PASSWORD": "${HOME}x"}, env)
            raise AssertionError("a value that does not survive must be refused")
        except setup.SetupError as e:
            assert "ZUJ_PASSWORD" in str(e) and "Nothing was changed" in str(e)
        assert env.read_text(encoding="utf-8") == before
        assert setup.render("A=1\nB=2\n", {"B": "x y", "C": "3"}) == "A=1\nB='x y'\n\n# Added by the setup\nC=3\n"

        # --- the conversation, with Telegram answering
        bots = [("refused", ""), ("ok", "my_uni_bot")]
        people = [[], [{"id": "555", "name": "Sam (@sam)"}]]
        hello = []
        setup.telegram_bot = lambda token: bots.pop(0)
        setup.telegram_people = lambda token: people.pop(0)
        setup.telegram_hello = lambda token, chat: hello.append(chat) or "ok"
        console = Script(["not-an-email", "209900001@std-zuj.edu.jo",       # 1: asked again until it is an e-mail
                          "", "pa ss#1",                                    # 2: needed
                          "123", "111111111:AAAAAAAAAAAA", "222222222:BBBBBBBBBBBB",   # 3: a shape, a refusal, the real one
                          "", "", "",                                       # 4: Enter (nobody yet), Enter, "is that you?" -> yes
                          "209900001", "portal pw", "y"])                   # 5: the portal and automatic check-in
        values = setup.converse(console, saved=nothing)
        assert values == {"ZUJ_USERNAME": "209900001@std-zuj.edu.jo", "ZUJ_PASSWORD": "pa ss#1",
                          "TELEGRAM_BOT_TOKEN": "222222222:BBBBBBBBBBBB", "TELEGRAM_CHAT_ID": "555", "ADMIN_USER_ID": "555",
                          "PORTAL_USERNAME": "209900001", "PORTAL_PASSWORD": "portal pw", "AUTO_CHECK_IN": "true"}, values
        assert console.answers == [] and hello == ["555"], "the bot's first message goes to the chat that was found"
        said = "\n".join(console.said)
        for line in ("it should be an e-mail address", "This one is needed.", "Telegram does not know this token",
                     "[ OK ] Your bot: @my_uni_bot", "t.me/my_uni_bot", "No message has reached the bot yet",
                     "[ OK ] Look at Telegram"):
            assert line in said, line
        assert any("I see Sam (@sam). Is that you?" in prompt for prompt, _ in console.asked)
        hidden_prompts = [prompt.strip() for prompt, hidden in console.asked if hidden]
        assert {p.split(":")[0].split(" [")[0] for p in hidden_prompts} == {"Password", "Token", "Portal password"}, hidden_prompts
        assert all(hidden for prompt, hidden in console.asked if "assword" in prompt or "Token" in prompt), "never echoed"
        assert not any("userinfobot" in line for line in console.said), "no Id to look up when the bot can be asked"

        # --- offline (or Telegram not reachable): nothing is checked, the Id is typed; the portal is skipped
        def never(*a, **k):
            raise AssertionError("no network in an offline setup")
        setup.telegram_bot = setup.telegram_people = setup.telegram_hello = never
        console = Script(["209900001@std-zuj.edu.jo", "pw", "222222222:BBBBBBBBBBBB", "abc", "555", ""])
        values = setup.converse(console, offline=True, saved=nothing)
        assert values["TELEGRAM_CHAT_ID"] == "555" and values["PORTAL_USERNAME"] == "" and values["AUTO_CHECK_IN"] == ""
        assert any("userinfobot" in line for line in console.said) and any("should be a number" in line for line in console.said)
        setup.telegram_bot = lambda token: ("unreachable", "")
        console = Script(["209900001@std-zuj.edu.jo", "pw", "222222222:BBBBBBBBBBBB", "555", ""])
        assert setup.converse(console, saved=nothing)["TELEGRAM_BOT_TOKEN"] == "222222222:BBBBBBBBBBBB"
        assert any("cannot be reached" in line for line in console.said), "saved as typed; the doctor checks it later"

        # --- the bot already runs (its updates cannot be read), or several people wrote to it
        setup.telegram_bot = lambda token: ("ok", "my_uni_bot")
        setup.telegram_hello = lambda token, chat: "blocked"
        setup.telegram_people = lambda token: None
        console = Script(["209900001@std-zuj.edu.jo", "pw", "222222222:BBBBBBBBBBBB", "", "777", ""])
        assert setup.converse(console, saved=nothing)["ADMIN_USER_ID"] == "777"
        assert any("press START in t.me/my_uni_bot" in line for line in console.said), "told when the bot could not write yet"
        setup.telegram_people = lambda token: [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]
        console = Script(["209900001@std-zuj.edu.jo", "pw", "222222222:BBBBBBBBBBBB", "", "2", ""])
        assert setup.converse(console, saved=nothing)["TELEGRAM_CHAT_ID"] == "2"

        # --- run again: Enter keeps what is saved, and a saved password is never shown
        kept = dict(values, ZUJ_PASSWORD="the saved secret", PORTAL_USERNAME="209900001", PORTAL_PASSWORD="portal secret",
                    AUTO_CHECK_IN="true", TELEGRAM_CHAT_ID="555")
        console = Script(["", "", "", "", "", "", ""])
        again = setup.converse(console, offline=True, saved=lambda name: kept.get(name, ""))
        assert again["ZUJ_PASSWORD"] == "the saved secret" and again["PORTAL_PASSWORD"] == "portal secret"
        assert again["AUTO_CHECK_IN"] == "true", "Enter keeps the choice that was made"
        shown = "\n".join(console.said + [prompt for prompt, _ in console.asked])
        assert "the saved secret" not in shown and "portal secret" not in shown and "Enter keeps the saved one" in shown

        # --- Telegram's answers, read
        class Answer:
            def __init__(self, data):
                self.data = data

            def json(self):
                return self.data
        updates = {"ok": True, "result": [
            {"message": {"chat": {"id": 555, "type": "private"}, "from": {"first_name": "Sam", "username": "sam"}}},
            {"message": {"chat": {"id": -100, "type": "group"}, "from": {"first_name": "Group"}}},
            {"message": {"chat": {"id": 9, "type": "private"}, "from": {"first_name": "Bot", "is_bot": True}}},
            {"message": {"chat": {"id": 777, "type": "private"}, "from": {"first_name": "Lina", "last_name": "K"}}},
            {"message": {"chat": {"id": 555, "type": "private"}, "from": {"first_name": "Sam", "username": "sam"}}},
            {"edited_message": {}}]}
        asked = []
        setup.requests.get = lambda url, params=None, timeout=None: asked.append((url, params)) or Answer(updates)
        assert keep[2]("111:tok") == [{"id": "555", "name": "Sam (@sam)"}, {"id": "777", "name": "Lina K"}], "newest first, once each"
        assert asked[0][0].endswith("/bot111:tok/getUpdates")
        setup.requests.get = lambda url, params=None, timeout=None: Answer({"ok": True, "result": {"username": "my_uni_bot"}})
        assert keep[1]("t") == ("ok", "my_uni_bot") and keep[3]("t", "555") == "ok"
        setup.requests.get = lambda url, params=None, timeout=None: Answer({"ok": False, "error_code": 409})
        assert keep[1]("t") == ("refused", "") and keep[2]("t") is None and keep[3]("t", "555") == "blocked"

        def down(url, params=None, timeout=None):
            raise setup.requests.ConnectionError("no route")
        setup.requests.get = down
        assert keep[1]("t") == ("unreachable", "") and keep[2]("t") is None and keep[3]("t", "555") == "unreachable"

        # --- python -m uninotifier.setup --demo: the whole conversation with made-up answers — Telegram played
        # by the setup itself, nothing contacted (requests.get still raises "no route" here), nothing written
        paths.ENV_FILE = tmp / "demo" / ".env"
        setup.telegram_bot, setup.telegram_people, setup.telegram_hello = keep[1], keep[2], keep[3]
        console = Script(["fake@std-zuj.edu.jo", "fake password", "123456789:FAKEFAKEFAKE",
                          "", "",                                           # Enter after Start; "is that you?" -> yes
                          "209900001", "fake portal", "y"])
        assert setup.main(["--demo"], console) == 0 and console.answers == []
        said = "\n".join(console.said)
        assert not paths.ENV_FILE.exists() and not paths.ENV_FILE.parent.exists(), "a try-out writes nothing"
        assert "[ OK ] Your bot: @your_uni_bot" in said and "Look at Telegram" in said and "nothing was written" in said
        assert "ZUJ_USERNAME=fake@std-zuj.edu.jo" in said and "ZUJ_PASSWORD=<13 characters, hidden>" in said
        assert "AUTO_CHECK_IN=true" in said and "TELEGRAM_CHAT_ID=123456789" in said
        assert "fake password" not in said and "FAKEFAKEFAKE" not in said and "fake portal" not in said, \
            "a password or token is never shown again"
        assert (setup.telegram_bot, setup.telegram_people, setup.telegram_hello) == keep[1:4], "Telegram is real again after"
        assert setup.main(["--demo"], Script([])) == 0, "stopped half-way (no more answers): nothing changed, no error"

        # --- python -m uninotifier.setup --answers FILE: an automated install; a wrong answer changes nothing
        paths.ENV_FILE = tmp / "auto" / ".env"
        paths.ENV_FILE.parent.mkdir()
        answers = tmp / "answers.json"
        answers.write_text(json.dumps({"ZUJ_USERNAME": "209900001@std-zuj.edu.jo", "ZUJ_PASSWORD": "pw",
                                       "TELEGRAM_BOT_TOKEN": "222222222:BBBBBBBBBBBB", "TELEGRAM_CHAT_ID": "555"}), encoding="utf-8")
        console = Script([])
        assert setup.main(["--answers", str(answers)], console) == 0 and "[ OK ] Saved to .env" in console.said[-1]
        assert dotenv_values(paths.ENV_FILE)["TELEGRAM_CHAT_ID"] == "555"
        answers.write_text(json.dumps({"TELEGRAM_CHAT_ID": "not a number"}), encoding="utf-8")
        console = Script([])
        assert setup.main(["--answers", str(answers)], console) == 1 and console.said[-1].startswith("[FAIL] TELEGRAM_CHAT_ID")
        answers.write_text(json.dumps({"NO_SUCH_SETTING": "1"}), encoding="utf-8")
        assert setup.main(["--answers", str(answers)], Script([])) == 1
        assert dotenv_values(paths.ENV_FILE)["TELEGRAM_CHAT_ID"] == "555", "a refused file changes nothing"
    finally:
        (setup.restrict, setup.telegram_bot, setup.telegram_people, setup.telegram_hello, setup.requests.get,
         paths.ENV_FILE) = keep
        shutil.rmtree(tmp, ignore_errors=True)


def test_archive_root_defaults_to_documents_university():
    """ARCHIVE_ROOT is optional: unset, files go to University in the Documents folder Explorer shows
    (OneDrive's when OneDrive backs up Documents); set, %VARS% and ~ are expanded, so one .env line
    works on any PC."""
    empty = _HOME / "empty.env"
    empty.write_text("", encoding="utf-8")
    keep = paths.ENV_FILE, archive._shell_documents
    paths.ENV_FILE = empty
    try:
        assert archive.DEFAULT_ARCHIVE_ROOT == archive.documents_folder() / "University"
        assert _with_env({"ARCHIVE_ROOT": None}, archive._archive_root) == archive.DEFAULT_ARCHIVE_ROOT
        assert _with_env({"ARCHIVE_ROOT": "  "}, archive._archive_root) == archive.DEFAULT_ARCHIVE_ROOT
        # which Documents: Windows' shell folder first, then OneDrive's, then the home folder's
        shell, onedrive = _HOME / "ShellDocs", _HOME / "OneDrive - ZUJ"
        shell.mkdir(exist_ok=True)
        (onedrive / "Documents").mkdir(parents=True, exist_ok=True)
        archive._shell_documents = lambda: str(shell)
        assert _with_env({"OneDrive": str(onedrive)}, archive.documents_folder) == shell
        archive._shell_documents = lambda: ""
        assert _with_env({"OneDrive": str(onedrive)}, archive.documents_folder) == onedrive / "Documents"
        archive._shell_documents = lambda: str(_HOME / "gone")       # a shell folder that doesn't exist is skipped
        assert _with_env({"OneDrive": None}, archive.documents_folder) == Path.home() / "Documents"
        got = _with_env({"ARCHIVE_ROOT": "%UNINOTIFIER_T%\\Semester_1", "UNINOTIFIER_T": str(_HOME)}, archive._archive_root)
        assert got == _HOME / "Semester_1", got
        assert _with_env({"ARCHIVE_ROOT": "~/Uni"}, archive._archive_root) == Path.home() / "Uni"
    finally:
        paths.ENV_FILE, archive._shell_documents = keep
    assert archive.ARCHIVE_ROOT == _HOME / "archive", "the tests never touch the real archive"


def test_doctor_says_what_to_fix():
    """python -m uninotifier.doctor: every check in plain words, exit 0 when nothing blocks a run."""
    import contextlib
    import io
    from uninotifier import doctor
    keep = doctor.check_browser, doctor.check_tasks
    doctor.check_browser = lambda r: r.line(doctor.OK, "Chromium (stubbed in the tests)")
    doctor.check_tasks = lambda r: None

    def run(*argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.main(list(argv))
        return code, out.getvalue()
    try:
        code, text = run("--offline")
        assert code == 0 and "[ OK ] Settings (.env)" in text and "[ OK ] Timetable: First Semester" in text, text
        assert f"[ OK ] Course files are saved to {_HOME / 'archive'}" in text and "All set." in text, text
        assert "[ -- ] Cambridge One: off" in text, "nothing set: the feature is off and says so"

        # Cambridge One is for the English course: on only when asked, with or without its own sign-in,
        # and a warning when no course of yours looks like an English one
        on = {"CAMBRIDGE_ASSIGNMENTS_URL": "https://www.cambridgeone.org/class/learner/o/class/c/assignments",
              "CAMBRIDGE_UNTIL": "2099-12-31"}
        code, text = _with_env(on, lambda: run("--offline"))
        assert code == 0 and "[ OK ] Cambridge One: watching until 2099-12-31 (the session ends every ~3 days" in text, text
        assert "Cambridge One is on, but" not in text, "the tracker lists an English course"
        code, text = _with_env({**on, "CAMBRIDGE_EMAIL": "me@example.invalid", "CAMBRIDGE_PASSWORD": "pw"},
                               lambda: run("--offline"))
        assert "watching until 2099-12-31, and it signs in again by itself" in text, text
        keep_data = paths.DATA
        paths.DATA = Path(tempfile.mkdtemp(prefix="uninotifier-doctor-data-"))
        try:
            (paths.DATA / "saved_items.json").write_text(json.dumps({"courses": {"1": {"name": "20261-Big Data-AllSec-3-"}}}),
                                                         encoding="utf-8")
            code, text = _with_env(on, lambda: run("--offline"))
            assert code == 0 and "[WARN] Cambridge One is on, but none of your Moodle courses looks like an English course" in text, text
            (paths.DATA / "saved_items.json").write_text(json.dumps({"courses": {"1": "20261-English Communication-1"}}),
                                                         encoding="utf-8")
            assert "Cambridge One is on, but" not in _with_env(on, lambda: run("--offline"))[1]
            (paths.DATA / "saved_items.json").unlink()
            assert "Cambridge One is on, but" not in _with_env(on, lambda: run("--offline"))[1], "no tracker yet: no guess"
        finally:
            shutil.rmtree(paths.DATA, ignore_errors=True)
            paths.DATA = keep_data

        code, text = _with_env({"ZUJ_PASSWORD": ""}, lambda: run("--offline"))
        assert code == 1 and "[FAIL] ZUJ_PASSWORD is not set" in text and "1 problem(s) to fix" in text, text
        keep_schedule = paths.SCHEDULE_FILE
        paths.SCHEDULE_FILE = _HOME / "no-such-schedule.json"
        try:
            code, text = run("--offline")
        finally:
            paths.SCHEDULE_FILE = keep_schedule
        assert code == 1 and "copy schedule.example.json to no-such-schedule.json" in text, text

        # the example left as it was installed (and saved by Notepad with a BOM): fails, so no fake lecture alerts
        tmp = Path(tempfile.mkdtemp(prefix="uninotifier-doctor-"))
        example = json.loads(paths.SCHEDULE_EXAMPLE.read_text(encoding="utf-8"))
        own = json.loads(keep_schedule.read_text(encoding="utf-8"))
        paths.SCHEDULE_FILE = tmp / "schedule.json"
        try:
            paths.SCHEDULE_FILE.write_text("﻿" + json.dumps(example, ensure_ascii=False), encoding="utf-8")
            code, text = run("--offline")
            assert code == 1 and "[FAIL] schedule.json still holds the example classes" in text, text
            assert "not valid JSON" not in text and "[ OK ] Timetable" not in text, text
            own["days"].setdefault("mon", []).append(example["days"]["mon"][0])     # one example class left behind
            paths.SCHEDULE_FILE.write_text(json.dumps(own), encoding="utf-8")
            code, text = run("--offline")
            assert code == 0 and "[ OK ] Timetable" in text and "[WARN] 1 class(es) in schedule.json are still the example's" in text, text
        finally:
            paths.SCHEDULE_FILE = keep_schedule
            shutil.rmtree(tmp, ignore_errors=True)
        assert all(ord(ch) < 128 for line in text.splitlines() if line.startswith("[") for ch in line[:6])

        # Telegram not reached: a network that blocks Telegram is told apart from no internet at all
        import requests
        keep_net = (requests.get, requests.head)

        def no_telegram(url, **kw):
            raise requests.ConnectionError("cut")
        try:
            requests.get = no_telegram
            requests.head = lambda url, **kw: object()                       # the university's site answers
            report = doctor.Report()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                _with_env({"TELEGRAM_BOT_TOKEN": "123456789:AAAAAAAAAA", "TELEGRAM_CHAT_ID": "1"},
                          lambda: doctor.check_telegram(report, send=False))
            assert report.failed == 1 and "this network blocks Telegram" in out.getvalue(), out.getvalue()
            assert "VPN" in out.getvalue() and "Nothing is lost" in out.getvalue()
            requests.head = no_telegram                                      # nothing answers: the internet is down
            report = doctor.Report()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                _with_env({"TELEGRAM_BOT_TOKEN": "123456789:AAAAAAAAAA", "TELEGRAM_CHAT_ID": "1"},
                          lambda: doctor.check_telegram(report, send=False))
            assert report.failed == 0 and "check the internet connection" in out.getvalue(), out.getvalue()
        finally:
            requests.get, requests.head = keep_net
    finally:
        doctor.check_browser, doctor.check_tasks = keep


def test_the_repository_is_ready_for_students():
    """What a student and a contributor rely on stays in place: exact pins that match pyproject.toml,
    Dependabot for packages and actions, the double-click installer (with Windows line endings), CI that
    runs that installer, issue forms that refuse to be sent without the privacy promise, and the
    security and contributing guides."""
    import subprocess
    project = Path(__file__).resolve().parents[1]

    def lines(name):
        return [l.strip() for l in (project / name).read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.strip().startswith("#")]
    pins = lines("requirements.txt")
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][\w.]*", p) for p in pins), pins
    assert lines("requirements-dev.txt")[0] == "-r requirements.txt"
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][\w.]*", p) for p in lines("requirements-dev.txt")[1:])
    declared = re.search(r"^dependencies = \[(.*?)\]", (project / "pyproject.toml").read_text(encoding="utf-8"), re.S | re.M)
    names = {re.split(r"[<>=!~ ]", d.strip().strip('",'))[0].lower() for d in declared.group(1).split(",") if d.strip()}
    assert names == {p.split("==")[0].lower() for p in pins}, ("pyproject.toml and requirements.txt list different packages", names)

    dependabot = (project / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "package-ecosystem: pip" in dependabot and "package-ecosystem: github-actions" in dependabot
    assert dependabot.count("interval: weekly") == 2

    bat = (project / "install.bat").read_bytes()
    assert b"-ExecutionPolicy Bypass" in bat and b'scripts\\install.ps1"' in bat and b"pause" in bat
    attributes = (project / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in attributes and "*.ps1 text eol=crlf" in attributes
    if (project / ".git").exists():
        checked = subprocess.run(["git", "check-attr", "eol", "install.bat", "scripts/install.ps1"], cwd=project,
                                 capture_output=True, text=True).stdout
        assert checked.count("eol: crlf") == 2, checked
    installer = (project / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert all(ord(ch) < 128 for ch in installer), "Windows PowerShell 5.1 misreads a BOM-less non-ASCII script"
    for step in ("'3.11'", "'-r', 'requirements.txt'", "'--no-deps', '-e', '.'", "'playwright', 'install', 'chromium'",
                 "'.env.example'", "-m uninotifier.setup", "-m uninotifier.doctor",
                 # the finish it offers: sign in, the first check, the scheduled tasks
                 "-m uninotifier.tools.login_refresh", "'monitor.py'", "'setup_tasks.ps1'"):
        assert step in installer, f"the installer lost: {step}"
    assert "notepad" not in installer.lower() and "schedule.example.json" not in installer, \
        "a student edits no file: the settings are asked for, the timetable is read from the portal"
    ci = (project / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "installer:" in ci and "scripts\\install.ps1 -NoEditor" in ci and "pip install --no-deps -e ." in ci
    assert "-m uninotifier.setup --answers" in ci and "-m uninotifier.timetable_image" in ci, \
        "CI answers the setup and draws a timetable on a clean Windows"
    for workflow in sorted((project / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        assert re.search(r"^permissions:\n  contents: read$", text, re.M), (workflow.name, "read-only token by default")
        for action in re.findall(r"uses: (\S+)", text):
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action), (workflow.name, "pin to a commit", action)
    hook = (project / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "unset $(git rev-parse --local-env-vars)" in hook, "the checks must run without git's own variables"
    assert 'nogit "$py" tests/test_architecture.py' in hook, "or a test would write into this repository"
    for step in ("-m pyflakes src tests monitor.py bot.py", "tests/test_architecture.py", "gitleaks git --pre-commit --staged"):
        assert step in hook and (step.startswith("gitleaks") or step.split()[-1] in ci), f"the pre-commit hook lost: {step}"
    if (project / ".git").exists():
        checked = subprocess.run(["git", "check-attr", "eol", ".githooks/pre-commit"], cwd=project,
                                 capture_output=True, text=True).stdout
        assert "eol: lf" in checked, "a CRLF checkout would break the hook's #!/bin/sh line"

    promise = "I have verified that no passwords, student IDs, or private tokens are included."
    for form in ("bug_report.yml", "feature_request.yml"):
        text = (project / ".github" / "ISSUE_TEMPLATE" / form).read_text(encoding="utf-8")
        assert re.search(rf"- label: {re.escape(promise)}\n\s+required: true", text), form
    config = (project / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8")
    assert "blank_issues_enabled: false" in config and "/security/advisories/new" in config
    security = (project / "SECURITY.md").read_text(encoding="utf-8")
    assert "/security/advisories/new" in security and "Don't open a public issue" in security
    contributing = (project / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for rule in ("Tests are offline", "No personal data", "Both READMEs change together", "The bot speaks English"):
        assert rule in contributing, rule


def test_the_readme_translations_say_the_same():
    """README.md and README.ar.md link to each other and stay in step: the same badges, commands,
    diagram and sample output (comments may be translated), every relative link pointing at a file —
    and the tests badge is the live status of the CI workflow that runs this suite."""
    project = Path(__file__).resolve().parents[1]
    en = (project / "README.md").read_text(encoding="utf-8")
    ar = (project / "README.ar.md").read_text(encoding="utf-8")
    assert en.splitlines()[0] == "[العربية](README.ar.md) | **English**", en.splitlines()[0]
    assert ar.splitlines()[0] == "**العربية** | [English](README.md)", ar.splitlines()[0]

    def badges(text):
        head = text.split("\n# UniNotifier\n", 1)[0].splitlines()
        assert head[1:4] == ["", "---", ""] and head[4] == '<div align="center">' and head[-1] == "</div>", head
        return [line for line in head if line.startswith("[![")]
    assert badges(en) == badges(ar) and len(badges(en)) == 5, "the same five badges on both pages"
    labels = " ".join(badges(en))
    for label in ("Python 3.11+", "Platform: Windows", "Code style: pyflakes clean", "License: PolyForm Noncommercial"):
        assert f"[![{label}](" in labels, label
    workflow = f"https://{REPO_PATH}/actions/workflows/tests.yml"
    assert f"[![Tests]({workflow}/badge.svg)]({workflow})" in labels, "the tests badge is the CI workflow's own"
    ci = (project / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    for step in ("runs-on: windows-latest", '"3.11"', "python tests/test_architecture.py",
                 "python -m pyflakes src tests monitor.py bot.py", "python -m playwright install chromium"):
        assert step in ci, f"the CI workflow lost: {step}"

    def blocks(text):
        found = []
        for body in re.findall(r"^```\w*\n(.*?)^```", text, re.S | re.M):
            lines = [re.sub(r"\s+#\s.*$", "", line).rstrip() for line in body.splitlines()]
            found.append([line for line in lines if line and not line.lstrip().startswith("# ")])
        return found
    assert len(blocks(en)) >= 8 and blocks(en) == blocks(ar), "a command, the diagram or a sample differs"

    for name, text in (("README.md", en), ("README.ar.md", ar)):
        for target in re.findall(r"\]\(([^)#]+?)\)", text):
            if not target.startswith(("http://", "https://")):
                assert (project / target).exists(), (name, target)
        assert f"git clone {PUBLIC_REPO}\n" in text and "%USERPROFILE%" in text, (name, "clone address and placeholder")
    assert [line.count("|") for line in en.splitlines() if line.startswith("|")] == \
           [line.count("|") for line in ar.splitlines() if line.startswith("|")], "the tables have the same shape"
