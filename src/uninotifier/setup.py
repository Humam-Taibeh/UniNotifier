"""
The first-time setup as a short conversation in the console — no file to edit.

  python -m uninotifier.setup                  ask, check each answer, write .env
  python -m uninotifier.setup --offline        ask and write; check nothing over the network
  python -m uninotifier.setup --answers FILE   take the answers from a JSON file {"NAME": "value"}
                                               (automated installs; nothing is asked)
  python -m uninotifier.setup --demo           try it with made-up answers: the same questions, Telegram
                                               played by the setup itself, nothing contacted, nothing
                                               written — at the end it shows what WOULD be in .env

install.bat runs it for a new student. It asks for five things:

  1. the university e-mail            (the Moodle sign-in)
  2. its password                     (typed without being shown)
  3. the Telegram bot's token         (checked with Telegram at once: the bot's name is shown)
  4. who you are on Telegram          (found by itself: you press Start in your bot — no Id to
                                       look up; typed by hand only when that cannot be done)
  5. the registration portal login    (optional: your timetable, transcript and balance — and
                                       whether to check you in to online lectures)

What it does with the answers: writes them into .env — made from .env.example, so every
explanation stays in the file, and an .env that already exists keeps everything else it holds —
then reads the file back with the same reader the program uses, to prove that every value
survived (a password with # or a quote in it), and limits the file to your Windows account.
Run it again at any time: Enter keeps what is saved.

Nothing typed here is logged or shown again, and nothing is sent anywhere — except the bot's
token to Telegram itself, which is the only way to check it.
"""

import getpass
import json
import os
import re
import subprocess
import sys

import requests
from dotenv import dotenv_values

from uninotifier import envcheck, fileio, paths

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 15
HELLO = "✅ UniNotifier is connected to this chat. Setup goes on in the window on your PC."
_PLAIN = re.compile(r"[\w@.:/+\-%~]+")          # a value that needs no quotes in .env
_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=.*$")


class SetupError(Exception):
    """Something the student has to know about; the message is the whole story."""


# ------------------------------------------------------------------ the .env file

def quoted(value: str) -> str:
    """The value as .env must hold it: as it is when that is safe, in single quotes otherwise
    (a space, #, a quote or a backslash would be read as something else)."""
    if value == "" or _PLAIN.fullmatch(value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render(template: str, values: dict[str, str]) -> str:
    """`template` (.env.example, or the .env already there) with each NAME= line of `values`
    rewritten; a name the text does not hold is added at the end. Everything else is kept."""
    left = dict(values)
    lines = []
    for line in template.splitlines():
        found = _LINE.match(line)
        if found and found.group(1) in left:
            name = found.group(1)
            line = f"{name}={quoted(left.pop(name))}"
        lines.append(line)
    if left:
        lines += ["", "# Added by the setup"] + [f"{name}={quoted(value)}" for name, value in left.items()]
    return "\n".join(lines) + "\n"


def account_sid() -> str | None:
    """The security Id of the Windows account running this (S-1-5-21-...) — exact, where a name can
    be ambiguous (a domain account, a renamed account)."""
    try:
        done = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r'"(S-1-[\d-]+)"', done.stdout or "")
    return found.group(1) if done.returncode == 0 and found else None


def restrict(path) -> bool:
    """Only your own Windows account may open the file (it holds your passwords). False when
    that could not be done — the file is still written, and still git-ignored.
    In two steps, so the file can never end up open to nobody: your account is given full access
    first, and only when that worked is everyone else's inherited access taken away."""
    if os.name != "nt":
        return False
    sid = account_sid()
    if not sid:
        return False
    try:
        granted = subprocess.run(["icacls", str(path), "/grant:r", f"*{sid}:F"], capture_output=True, timeout=30)
        if granted.returncode != 0:
            return False
        done = subprocess.run(["icacls", str(path), "/inheritance:r"], capture_output=True, timeout=30)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def write_env(values: dict[str, str], path=None, example=None) -> bool:
    """Keep `values` in .env. The file is read back the way the program reads it; if any value
    did not come back exactly, the file is put back as it was and SetupError says which.
    Returns whether the file could be limited to your own Windows account (restrict)."""
    path = paths.ENV_FILE if path is None else path
    example = paths.ENV_EXAMPLE if example is None else example
    try:
        before = path.read_text(encoding="utf-8-sig") if path.is_file() else None
        template = before if before is not None else example.read_text(encoding="utf-8")
    except OSError as e:
        raise SetupError(f"The settings file could not be read ({type(e).__name__}).") from e
    fileio.write_text_atomic(path, render(template, values))
    read_back = dotenv_values(path)
    lost = [name for name, value in values.items() if (read_back.get(name) or "") != value]
    if lost:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            fileio.write_text_atomic(path, before)
        raise SetupError(f"{', '.join(lost)} cannot be kept in .env as typed (it holds a combination of symbols "
                         "the file format changes, such as ${...}). Nothing was changed.")
    return restrict(path)


# ------------------------------------------------------------------ Telegram

def _call(token: str, method: str, **params) -> dict | None:
    """Telegram's answer, or None when Telegram could not be reached at all."""
    try:
        return requests.get(API.format(token=token, method=method), params=params, timeout=TIMEOUT).json()
    except (requests.RequestException, ValueError):
        return None


def telegram_bot(token: str) -> tuple[str, str]:
    """("ok", the bot's username) / ("refused", "") / ("unreachable", "")."""
    answer = _call(token, "getMe")
    if answer is None:
        return "unreachable", ""
    return ("ok", answer["result"].get("username", "")) if answer.get("ok") else ("refused", "")


def telegram_people(token: str) -> list[dict] | None:
    """Who wrote to the bot lately, in a private chat, newest first: [{"id", "name"}] — how the
    setup finds your Id after you press Start. None when that cannot be told (Telegram not
    reached, or the bot is already running and holds the updates)."""
    answer = _call(token, "getUpdates", timeout=0)
    if answer is None or not answer.get("ok"):
        return None
    people: dict[str, dict] = {}
    for update in reversed(answer.get("result") or []):
        message = update.get("message") or {}
        chat, sender = message.get("chat") or {}, message.get("from") or {}
        if chat.get("type") != "private" or sender.get("is_bot") or "id" not in chat:
            continue
        name = " ".join(part for part in (sender.get("first_name"), sender.get("last_name")) if part)
        if sender.get("username"):
            name = f"{name} (@{sender['username']})".strip()
        people.setdefault(str(chat["id"]), {"id": str(chat["id"]), "name": name or "someone"})
    return list(people.values())


def telegram_hello(token: str, chat_id: str) -> str:
    """"ok" when the bot's first message reached the chat, "blocked" when Telegram refused it
    (Start was not pressed in the bot, or the Id is wrong), "unreachable"."""
    answer = _call(token, "sendMessage", chat_id=chat_id, text=HELLO)
    if answer is None:
        return "unreachable"
    return "ok" if answer.get("ok") else "blocked"


# ------------------------------------------------------------------ the conversation

class Console:
    """The window the student types in."""

    def say(self, text: str = "") -> None:
        print(text)

    def ask(self, prompt: str, secret: bool = False) -> str:
        return (getpass.getpass(prompt) if secret else input(prompt)).strip()


def shape_problem(name: str, value: str) -> str:
    """"" when `value` looks like what `name` holds, else what it should look like."""
    var = next(v for v in envcheck.VARIABLES if v.name == name)
    return "" if not var.pattern or re.fullmatch(var.pattern, value) else var.shape


def _ask(console: Console, name: str, prompt: str, current: str = "", secret: bool = False,
         optional: bool = False) -> str:
    """One answer, asked again until it has the right shape. Enter keeps `current`."""
    kept = " [Enter keeps the saved one]" if current and secret else f" [{current}]" if current else \
           " [Enter to skip]" if optional else ""
    while True:
        answer = console.ask(f"   {prompt}{kept}: ", secret) or current
        if not answer:
            if optional:
                return ""
            console.say("   This one is needed.")
            continue
        problem = shape_problem(name, answer)
        if not problem:
            return answer
        console.say(f"   That does not look right: it should be {problem}.")


def _yes(console: Console, question: str, default: bool) -> bool:
    answer = console.ask(f"   {question} [{'Y/n' if default else 'y/N'}]: ").lower()
    return default if not answer else answer.startswith("y")


def find_chat(console: Console, token: str, bot: str, current: str = "") -> str:
    """Your Telegram Id: found from the Start you press in your bot; typed only as a last resort."""
    console.say(f"   Open your bot in Telegram - t.me/{bot} - and press START (or send it any message).")
    for _ in range(5):
        console.ask("   Then press Enter here: ")
        people = telegram_people(token)
        if people is None:
            console.say("   I cannot read the bot's messages just now (it may already be running).")
            break
        if len(people) == 1 and _yes(console, f"I see {people[0]['name']}. Is that you?", True):
            return people[0]["id"]
        if len(people) > 1:
            for number, person in enumerate(people, 1):
                console.say(f"     {number}. {person['name']}")
            pick = console.ask("   Which one are you? Type the number: ")
            if pick.isdigit() and 1 <= int(pick) <= len(people):
                return people[int(pick) - 1]["id"]
        if not people:
            console.say("   No message has reached the bot yet. Press START in the bot, then try again.")
    console.say("   Then type your Telegram Id instead: send any message to @userinfobot and it replies with it.")
    return _ask(console, "TELEGRAM_CHAT_ID", "Your Telegram Id", current)


def converse(console: Console, offline: bool = False, saved=envcheck.value) -> dict[str, str]:
    """Ask everything and return {NAME: value} for .env. `saved(name)` gives what is already kept."""
    values: dict[str, str] = {}
    console.say("UniNotifier setup - five short questions. Passwords are not shown while you type.")
    console.say("")
    console.say("1/5  Your university e-mail - the one you open Moodle (e-learning) with")
    values["ZUJ_USERNAME"] = _ask(console, "ZUJ_USERNAME", "E-mail", saved("ZUJ_USERNAME"))
    console.say("2/5  The password of that e-mail")
    values["ZUJ_PASSWORD"] = _ask(console, "ZUJ_PASSWORD", "Password", saved("ZUJ_PASSWORD"), secret=True)

    console.say("3/5  Your Telegram bot. In Telegram open @BotFather, send /newbot, choose a name,")
    console.say("     and copy the token it gives you (it looks like 123456789:AAH...).")
    bot, reached = "", False
    while True:
        token = _ask(console, "TELEGRAM_BOT_TOKEN", "Token", saved("TELEGRAM_BOT_TOKEN"), secret=True)
        if offline:
            break
        state, bot = telegram_bot(token)
        if state == "ok":
            console.say(f"   [ OK ] Your bot: @{bot}")
            reached = True
            break
        if state == "unreachable":
            console.say("   [WARN] Telegram cannot be reached from this PC right now - the token is saved as typed;")
            console.say("          the doctor checks it later.")
            break
        console.say("   Telegram does not know this token. Copy it again from @BotFather (/mybots - API Token).")
    values["TELEGRAM_BOT_TOKEN"] = token

    console.say("4/5  Who you are on Telegram - the bot answers you and nobody else")
    if reached:
        chat = find_chat(console, token, bot, saved("TELEGRAM_CHAT_ID"))
        state = telegram_hello(token, chat)
        if state == "ok":
            console.say("   [ OK ] Look at Telegram: your bot just wrote to you.")
        elif state == "blocked":
            console.say(f"   [WARN] The bot could not write to you yet: press START in t.me/{bot}. The doctor checks again.")
    else:
        console.say("   Send any message to @userinfobot on Telegram: it replies with your Id (a number).")
        chat = _ask(console, "TELEGRAM_CHAT_ID", "Your Telegram Id", saved("TELEGRAM_CHAT_ID"))
    values["TELEGRAM_CHAT_ID"] = values["ADMIN_USER_ID"] = chat

    console.say("5/5  The registration portal (portal.zuj.edu.jo) - optional. With it the bot reads your")
    console.say("     timetable by itself and draws it, and shows your transcript and balance when you ask.")
    values["PORTAL_USERNAME"] = _ask(console, "PORTAL_USERNAME", "Portal user name (usually your university number)",
                                     saved("PORTAL_USERNAME"), optional=True)
    if values["PORTAL_USERNAME"]:
        values["PORTAL_PASSWORD"] = _ask(console, "PORTAL_PASSWORD", "Portal password", saved("PORTAL_PASSWORD"), secret=True)
        on = _yes(console, "Check you in to your ONLINE lectures automatically (with a screenshot as proof)?",
                  saved("AUTO_CHECK_IN").lower() == "true")
        values["AUTO_CHECK_IN"] = "true" if on else "false"
    else:
        values["PORTAL_PASSWORD"], values["AUTO_CHECK_IN"] = "", ""
        console.say("   Skipped: you can run this setup again later to add it.")
    return values


def from_answers(answers: dict) -> dict[str, str]:
    """A JSON file's answers as .env values: only names the program knows, each of the right shape."""
    known = {var.name for var in envcheck.VARIABLES}
    values = {}
    for name, value in answers.items():
        if name not in known or not isinstance(value, str):
            raise SetupError(f"{name}: not a setting UniNotifier has (or its value is not text).")
        problem = shape_problem(name, value) if value else ""
        if problem:
            raise SetupError(f"{name} does not look right: it should be {problem}.")
        values[name] = value
    return values


class Demo:
    """--demo: Telegram as the setup would find it, played here — any token of the right shape is
    'your bot', and the Start you are asked to press is 'seen' — so the whole conversation can be
    tried with made-up answers. Nothing leaves the PC."""
    BOT = "your_uni_bot"

    def __enter__(self):
        global telegram_bot, telegram_people, telegram_hello
        self.kept = (telegram_bot, telegram_people, telegram_hello)
        telegram_bot = lambda token: ("ok", self.BOT)                               # noqa: E731
        telegram_people = lambda token: [{"id": "123456789", "name": "You (a made-up account)"}]  # noqa: E731
        telegram_hello = lambda token, chat: "ok"                                   # noqa: E731
        return self

    def __exit__(self, *exc):
        global telegram_bot, telegram_people, telegram_hello
        telegram_bot, telegram_people, telegram_hello = self.kept
        return False


def preview(values: dict[str, str]) -> list[str]:
    """What .env would hold, a password or a token shown only as its length."""
    hidden = ("ZUJ_PASSWORD", "PORTAL_PASSWORD", "TELEGRAM_BOT_TOKEN")
    return [f"   {name}=" + (f"<{len(value)} characters, hidden>" if name in hidden and value else value)
            for name, value in values.items()]


def main(argv: list[str], console: Console | None = None) -> int:
    console = console or Console()
    if "--demo" in argv:
        console.say("TRY-OUT - answer with anything made up. Nothing is checked online, nothing is saved,")
        console.say("and your real settings are not touched.")
        console.say("")
        try:
            with Demo():
                values = converse(console, saved=lambda name: "")
        except (KeyboardInterrupt, EOFError):
            console.say("\nStopped. This was only a try-out: nothing was changed.")
            return 0
        console.say("")
        console.say("This is what a real setup would write to .env:")
        for line in preview(values):
            console.say(line)
        console.say("")
        console.say("[ OK ] Try-out finished - nothing was written. For real: .venv\\Scripts\\python -m uninotifier.setup")
        return 0
    try:
        if "--answers" in argv:
            source = argv[argv.index("--answers") + 1:][:1]
            if not source:
                raise SetupError("--answers needs the path of a JSON file.")
            with open(source[0], encoding="utf-8-sig") as f:
                values = from_answers(json.load(f))
        else:
            values = converse(console, offline="--offline" in argv)
        private = write_env(values)
    except SetupError as e:
        console.say(f"[FAIL] {e}")
        return 1
    except (OSError, ValueError) as e:
        console.say(f"[FAIL] The answers could not be read or saved ({type(e).__name__}: {e}).")
        return 1
    except (KeyboardInterrupt, EOFError):
        console.say("\nStopped. Nothing was changed.")
        return 1
    console.say("")
    console.say(f"[ OK ] Saved to {paths.ENV_FILE.name}"
                + (" - only your Windows account can open it, and" if private else " -") + " it never leaves this PC.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
