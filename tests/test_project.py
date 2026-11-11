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
                 "'.env.example', '.env'", "'schedule.example.json', 'schedule.json'", "-m uninotifier.doctor"):
        assert step in installer, f"the installer lost: {step}"
    ci = (project / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "installer:" in ci and "scripts\\install.ps1 -NoEditor" in ci and "pip install --no-deps -e ." in ci
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
