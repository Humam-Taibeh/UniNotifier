"""
Is everything set up? One command that checks the whole installation and says
what to do next, in plain words:

    .venv\\Scripts\\python -m uninotifier.doctor            all checks (asks Telegram who your bot is)
    .venv\\Scripts\\python -m uninotifier.doctor --offline  no network
    .venv\\Scripts\\python -m uninotifier.doctor --send     ... and send yourself a test message

Exit code 0 when nothing blocks the scheduled check and the bot, 1 otherwise.
scripts/setup_tasks.ps1 runs it before registering the scheduled tasks.
Output is plain ASCII, so it reads the same in any console.
"""

import json
import subprocess
import sys

from uninotifier import envcheck, paths

OK, FAIL, WARN, INFO = "[ OK ]", "[FAIL]", "[WARN]", "[ -- ]"
TASKS = ("UniNotifier", "UniNotifier_BotListener")


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def line(self, mark: str, text: str, fix: str = "") -> None:
        self.failed += mark == FAIL
        print(f"{mark} {text}")
        if fix:
            print(f"       -> {fix}")


def check_python(r: Report) -> None:
    v = sys.version_info
    if v >= (3, 11):
        r.line(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        r.line(FAIL, f"Python {v.major}.{v.minor} is too old", "install Python 3.11 or newer and re-create .venv")


def check_env(r: Report) -> None:
    if not paths.ENV_FILE.is_file():
        r.line(FAIL, f"No settings file ({paths.ENV_FILE})",
               f"copy {paths.ENV_EXAMPLE.name} to .env and fill it in:  copy .env.example .env")
        return
    found = envcheck.problems()
    for problem in found:
        r.line(FAIL, problem)
    for warning in envcheck.warnings():
        r.line(WARN, warning)
    if not found:
        r.line(OK, "Settings (.env): everything required is filled in")


def example_classes(days: dict) -> int:
    """How many of these classes are still copied unchanged from schedule.example.json."""
    try:
        example = json.loads(paths.SCHEDULE_EXAMPLE.read_text(encoding="utf-8-sig")).get("days") or {}
    except (OSError, ValueError):
        return 0
    return sum(lecture in example.get(day, []) for day, lectures in days.items() for lecture in lectures)


def check_schedule(r: Report) -> None:
    from uninotifier import schedule
    name = paths.SCHEDULE_FILE.name
    if not paths.SCHEDULE_FILE.is_file():
        r.line(FAIL, f"No timetable ({name})",
               f"copy {paths.SCHEDULE_EXAMPLE.name} to {name} and enter your own classes")
        return
    try:
        data = json.loads(paths.SCHEDULE_FILE.read_text(encoding="utf-8-sig"))    # Notepad may add a BOM
    except (OSError, ValueError) as e:
        r.line(FAIL, f"{name} is not valid JSON: {e}")
        return
    found = schedule.validate(data)
    for problem in found[:10]:
        r.line(FAIL, f"{name}: {problem}")
    if not found:
        days = data.get("days") or {}
        lectures = sum(len(v) for v in days.values())
        copied = example_classes(days)
        if copied and copied == lectures:
            # left as installed, it would announce lectures the student doesn't have
            r.line(FAIL, f"{name} still holds the example classes",
                   f"open it (notepad {name}) and replace them with your own week - README, step 5")
        else:
            r.line(OK, f"Timetable: {data.get('semester') or '(no semester name)'}, {lectures} weekly classes")
            if copied:
                r.line(WARN, f"{copied} class(es) in {name} are still the example's",
                       f"replace or delete them (notepad {name})")
    if not (paths.SCHEDULE_PDF.is_file() or paths.SCHEDULE_IMAGE.is_file()):
        r.line(INFO, f"No timetable picture in assets/ (optional: {paths.SCHEDULE_PDF.name} / {paths.SCHEDULE_IMAGE.name} "
                     "are what the bot's Weekly Timetable button sends)")


def check_archive(r: Report) -> None:
    from uninotifier import archive
    root = archive.ARCHIVE_ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".uninotifier-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        r.line(OK, f"Course files are saved to {root}")
    except OSError as e:
        r.line(FAIL, f"Can't write to the archive folder {root} ({e.__class__.__name__})",
               "set ARCHIVE_ROOT in .env to a folder you own")


def check_browser(r: Report) -> None:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
        from pathlib import Path
        if Path(exe).is_file():
            r.line(OK, "Chromium for Playwright is installed")
        else:
            r.line(FAIL, "Chromium for Playwright is missing", ".venv\\Scripts\\python -m playwright install chromium")
    except Exception as e:                 # the doctor must report, never crash
        r.line(FAIL, f"Playwright doesn't work ({e.__class__.__name__}: {str(e)[:120]})",
               ".venv\\Scripts\\pip install -r requirements.txt, then: .venv\\Scripts\\python -m playwright install chromium")


def check_telegram(r: Report, send: bool) -> None:
    import requests
    token, chat = envcheck.value("TELEGRAM_BOT_TOKEN"), envcheck.value("TELEGRAM_CHAT_ID")
    if not (token and chat):
        r.line(INFO, "Telegram not checked (token or chat id missing above)")
        return
    api = f"https://api.telegram.org/bot{token}"
    try:
        me = requests.get(f"{api}/getMe", timeout=15).json()
        if not me.get("ok"):
            r.line(FAIL, "Telegram refused the bot token", "copy the token again from @BotFather (/mybots -> API Token)")
            return
        r.line(OK, f"Telegram bot: @{me['result'].get('username')}")
        chat_info = requests.get(f"{api}/getChat", params={"chat_id": chat}, timeout=15).json()
        if not chat_info.get("ok"):
            r.line(FAIL, "The bot can't reach your chat yet",
                   f"open t.me/{me['result'].get('username')} on Telegram and press Start, then run this again")
            return
        r.line(OK, "The bot can message you")
        if send:
            sent = requests.post(f"{api}/sendMessage", timeout=15, data={
                "chat_id": chat, "text": "✅ UniNotifier is set up on this PC - this is a test message from the doctor."}).json()
            r.line(OK if sent.get("ok") else FAIL, "Test message sent" if sent.get("ok") else
                   f"Test message refused: {sent.get('description', '?')}")
    except requests.RequestException as e:
        r.line(WARN, f"Couldn't reach Telegram ({e.__class__.__name__}) - check the internet connection")


def check_state(r: Report) -> None:
    from uninotifier import gitsync
    if (paths.DATA / "auth_state.json").is_file():
        r.line(OK, "Moodle session saved")
    else:
        r.line(INFO, "No Moodle session yet - sign in once:  .venv\\Scripts\\python -m uninotifier.tools.login_refresh")
    if not (paths.DATA / "saved_items.json").is_file():
        r.line(INFO, "No baseline yet - the first check records what is on Moodle now, quietly:  .venv\\Scripts\\python monitor.py")
    r.line(INFO, "State backup: " + ("on (data/ is its own git repository)" if gitsync.enabled() else
                                     "off (optional: python -m uninotifier.gitsync --init <private repo URL>)"))


def check_tasks(r: Report) -> None:
    if sys.platform != "win32":
        return
    fix = "powershell -ExecutionPolicy Bypass -File scripts\\setup_tasks.ps1"
    # PowerShell, not schtasks: schtasks prints in the console code page, which garbles a non-ASCII path
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; foreach ($n in '" + "','".join(TASKS) + "') {"
              " $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue;"
              " if ($t) { $n + [char]9 + $t.Actions[0].Arguments } else { $n + [char]9 } }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                             timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        r.line(INFO, "Couldn't read the scheduled tasks")
        return
    arguments = dict((line.split("\t", 1) + [""])[:2] for line in out.splitlines() if "\t" in line)
    launchers = {TASKS[0]: paths.MONITOR_SCRIPT, TASKS[1]: paths.ROOT / "bot.py"}
    missing = [name for name in TASKS if not arguments.get(name)]
    elsewhere = [name for name in TASKS if arguments.get(name)
                 and arguments[name].strip().strip('"').lower() != str(launchers[name]).lower()]
    if missing:
        r.line(INFO, f"Scheduled tasks not registered: {', '.join(missing)}", fix)
    elif elsewhere:
        r.line(WARN, f"The scheduled tasks run another copy: {arguments[elsewhere[0]].strip()}", fix + "  (points them here)")
    else:
        r.line(OK, "Scheduled tasks are registered: " + ", ".join(TASKS))


def main(argv: list[str]) -> int:
    offline, send = "--offline" in argv, "--send" in argv
    print(f"UniNotifier doctor - {paths.ROOT}\n")
    r = Report()
    check_python(r)
    check_env(r)
    check_schedule(r)
    check_archive(r)
    check_browser(r)
    if not offline:
        check_telegram(r, send)
    check_state(r)
    check_tasks(r)
    print("\n" + ("All set." if not r.failed else f"{r.failed} problem(s) to fix - see the arrows above."))
    return 1 if r.failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")      # a console that can't show a character gets "?"
    sys.exit(main(sys.argv[1:]))
