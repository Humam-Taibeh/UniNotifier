"""
The settings in .env, checked before anything runs.

Every variable the program reads is listed here once: who needs it, what a
valid value looks like, and where to get it. The scheduled check and the bot
call require() first thing, so a missing password or a value still copied
from .env.example ends the run with a plain list of what to fix (in the log
file, on the console, and on Telegram when Telegram itself is set up) instead
of a traceback deep inside a sign-in. `python -m uninotifier.doctor` shows the
same list, and more.
"""

import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date

from dotenv import dotenv_values

from uninotifier import paths

log = logging.getLogger("uninotifier")

EXIT_CONFIG = 2                     # exit code of a run stopped by its settings


@dataclass(frozen=True)
class Var:
    name: str
    needed_by: tuple[str, ...]      # "monitor" / "bot": missing = that part cannot start; () = optional
    hint: str                       # where the value comes from
    pattern: str = ""               # a valid value matches this (when set)
    shape: str = ""                 # ... described in words


VARIABLES = (
    Var("ZUJ_USERNAME", ("monitor",), "your university Microsoft e-mail (the Moodle sign-in)",
        r"[^@\s]+@[^@\s]+\.[^@\s]+", "an e-mail address, e.g. 202312345@std-zuj.edu.jo"),
    Var("ZUJ_PASSWORD", ("monitor",), "the password of that Microsoft account"),
    Var("TELEGRAM_BOT_TOKEN", ("monitor", "bot"), "create a bot with @BotFather on Telegram (/newbot)",
        r"\d+:[\w-]{8,}", "digits, a colon, then a long code, e.g. 123456789:AAH..."),
    Var("TELEGRAM_CHAT_ID", ("monitor", "bot"), "send any message to @userinfobot on Telegram: it replies with your Id",
        r"-?\d+", "a number"),
    Var("ADMIN_USER_ID", (), "the same Id as TELEGRAM_CHAT_ID (the only person the bot answers)",
        r"[1-9]\d{0,15}", "a number"),
    Var("STUDENT_ID", (), "your university number (finds your marks inside result files)"),
    Var("ARCHIVE_ROOT", (), "the folder for downloaded course files (default: Documents\\University)"),
    Var("PORTAL_USERNAME", (), "the registration portal (portal.zuj.edu.jo) user name"),
    Var("PORTAL_PASSWORD", (), "the registration portal password"),
    Var("HEALTHCHECK_URL", (), "a ping URL from healthchecks.io", r"https?://\S+", "a web address"),
    Var("BOT_HEALTHCHECK_URL", (), "a second ping URL from healthchecks.io", r"https?://\S+", "a web address"),
    Var("BOT_POLL_TIMEOUT", (), "seconds of each Telegram long poll (5-50)", r"\d+", "a number"),
    Var("CAMBRIDGE_ASSIGNMENTS_URL", (), "your class's \"My assignments\" page on cambridgeone.org",
        r"https://\S+", "a web address"),
    Var("CAMBRIDGE_UNTIL", (), "the last day to watch Cambridge One",
        r"\d{4}-\d{2}-\d{2}", "a date as YYYY-MM-DD"),
    Var("CAMBRIDGE_EMAIL", (), "your Cambridge One user name or e-mail (the bot signs in again by itself)"),
    Var("CAMBRIDGE_PASSWORD", (), "your Cambridge One password (quote it if it holds # or spaces)"),
)

# Values copied unchanged from .env.example count as "not set".
_PLACEHOLDER = re.compile(r"^(your[_-]|\.\.\.$|changeme$)|<[\w-]+>|your[_-](bot[_-])?check[_-]uuid|\\path\\to\\", re.I)


def value(name: str) -> str:
    """The setting as the program sees it (the process environment wins over .env, as with
    load_dotenv), stripped; "" when unset or still an example value."""
    raw = os.environ.get(name)
    if raw is None:
        raw = _file_values().get(name)
    raw = (raw or "").strip()
    return "" if _PLACEHOLDER.search(raw) else raw


def _file_values() -> dict:
    try:
        return dotenv_values(paths.ENV_FILE) if paths.ENV_FILE.is_file() else {}
    except (OSError, UnicodeDecodeError):
        return {}


def problems(role: str | None = None) -> list[str]:
    """What stops `role` ("monitor" / "bot"; None = both) from running. Empty = fine."""
    found = []
    roles = (role,) if role else ("monitor", "bot")
    for var in VARIABLES:
        val = value(var.name)
        if not val:
            if set(var.needed_by) & set(roles):
                found.append(f"{var.name} is not set - {var.hint}.")
        elif var.pattern and not re.fullmatch(var.pattern, val):
            found.append(f"{var.name} doesn't look right - expected {var.shape}.")
    admin, chat = value("ADMIN_USER_ID"), value("TELEGRAM_CHAT_ID")
    if admin and chat and admin != chat and re.fullmatch(r"-?\d+", chat):
        found.append("TELEGRAM_CHAT_ID must be your private chat with the bot - the same number as ADMIN_USER_ID.")
    return found


def warnings() -> list[str]:
    """Settings that are allowed but probably not what you meant."""
    found = []
    if bool(value("PORTAL_USERNAME")) != bool(value("PORTAL_PASSWORD")):
        found.append("Only one of PORTAL_USERNAME / PORTAL_PASSWORD is set - /transcript, /balance and Finals Watch need both.")
    if bool(value("CAMBRIDGE_ASSIGNMENTS_URL")) != bool(value("CAMBRIDGE_UNTIL")):
        found.append("Cambridge One needs both CAMBRIDGE_ASSIGNMENTS_URL and CAMBRIDGE_UNTIL - it stays off.")
    if bool(value("CAMBRIDGE_EMAIL")) != bool(value("CAMBRIDGE_PASSWORD")):
        found.append("Only one of CAMBRIDGE_EMAIL / CAMBRIDGE_PASSWORD is set - the bot cannot sign in to "
                     "Cambridge One by itself without both.")
    until = value("CAMBRIDGE_UNTIL")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
        try:
            if date.fromisoformat(until) < date.today():
                found.append(f"CAMBRIDGE_UNTIL ({until}) is in the past - Cambridge One is no longer watched.")
        except ValueError:
            found.append(f"CAMBRIDGE_UNTIL ({until}) is not a real date.")
    archive = value("ARCHIVE_ROOT")
    if archive and not os.path.isabs(os.path.expanduser(os.path.expandvars(archive.strip('"')))):
        found.append("ARCHIVE_ROOT should be a full path, e.g. C:\\Users\\you\\Documents\\University.")
    if not value("ADMIN_USER_ID") and value("TELEGRAM_CHAT_ID"):
        found.append("ADMIN_USER_ID is not set - TELEGRAM_CHAT_ID is used as the owner's id.")
    return found


def report(found: list[str]) -> str:
    """The message shown when a run cannot start."""
    where = (f"Settings file: {paths.ENV_FILE}" if paths.ENV_FILE.is_file() else
             f"There is no settings file yet. Copy {paths.ENV_EXAMPLE.name} to {paths.ENV_FILE.name} "
             f"(in {paths.ENV_FILE.parent}) and fill it in.")
    return "\n".join(["UniNotifier can't start - some settings in .env need your attention:", "",
                      *(f"  - {p}" for p in found), "", where,
                      "Check everything at once with:  .venv\\Scripts\\python -m uninotifier.doctor"])


def require(role: str) -> None:
    """Stop the run (exit code EXIT_CONFIG) with a friendly list when `role` can't work.
    The list goes to the log, to the console when there is one, and to Telegram when the
    Telegram settings themselves are fine (once per 6 h, through failsafe)."""
    found = problems(role)
    if not found:
        return
    text = report(found)
    log.error("%s", text)
    if sys.stderr is not None and not any(isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stderr
                                          for h in logging.getLogger().handlers):
        print(text, file=sys.stderr)
    if not any(p.startswith(("TELEGRAM_", "ADMIN_USER_ID")) for p in found):
        from uninotifier import failsafe
        failsafe.report(f"config:{role}", "UniNotifier's settings need your attention",
                        detail="\n".join(f"• {p}" for p in found) + f"\n\nEdit {paths.ENV_FILE.name} on the PC.")
    sys.exit(EXIT_CONFIG)
