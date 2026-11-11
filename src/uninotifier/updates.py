"""
New versions of the project, and news from its developer — for a copy that was cloned with git.

Once a day the bot fetches the repository this copy was cloned from (its own `origin`: no address
is written here) and, when it is behind, says so ONCE per new version: what is new — the entries of
NEWS.md that this copy does not have yet, written by the developer for the people who use the
project; the commit subjects when there are none — with ⬇️ Update Now. Nothing is ever installed
without that press.

Update Now: only a fast-forward of a clean copy (a file you changed yourself stops it, with its
name); the requirements are installed again when they changed; the new files must compile, or the
copy goes back to the version it had. Then the bot ends itself and its scheduled task starts it
again on the new code (the monitor reads the new code at its next check by itself).

NEWS.md, newest first:

    ## 2030-01-15 — A short title
    What changed or what people should know, in plain words.

A copy that was downloaded as a ZIP has no git history: it is not checked, and says how to update.
"""

import logging
import re
import subprocess
import sys
import time
from pathlib import Path

from uninotifier import fileio, gitsync, paths, selfheal
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")

STATE_FILE = paths.DATA / "updates.json"
NEWS_FILE = "NEWS.md"
CHECK_EVERY = 24 * 3600
FIRST_CHECK_AFTER = 10 * 60          # after the bot starts: never in the way of its first minutes
GIT_TIMEOUT = 90
INSTALL_TIMEOUT = 15 * 60
NEWS_SHOWN = 4                       # entries in one message
ENTRY_CHARS = 700
SUBJECTS_SHOWN = 8
_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.M)
_VISIBLE = re.compile(r"^(feat|fix|perf)(\(.+?\))?:\s*", re.I)


class UpdateError(Exception):
    """Why an update could not be done, in words for the chat."""


# ---------------------------------------------------------------- git

def _git(*args: str, timeout: float = GIT_TIMEOUT, root: Path | None = None) -> subprocess.CompletedProcess:
    return gitsync.run_bounded(["git", *args], timeout, cwd=root or paths.ROOT)


def _out(*args: str, root: Path | None = None) -> str | None:
    """A git command's output; None when it failed."""
    try:
        res = _git(*args, root=root)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def is_clone(root: Path | None = None) -> bool:
    """A git clone with a branch that follows a remote one."""
    root = root or paths.ROOT
    return (root / ".git").exists() and bool(_out("rev-parse", "--abbrev-ref", "@{u}", root=root))


# ---------------------------------------------------------------- news

def parse_news(text: str) -> list[dict]:
    """NEWS.md -> [{"id" (the heading), "title", "text"}], in the file's order."""
    entries = []
    marks = list(_HEADING.finditer(text or ""))
    for mark, following in zip(marks, [*marks[1:], None]):
        body = text[mark.end():following.start() if following else len(text)].strip()
        entries.append({"id": mark.group(1), "title": mark.group(1), "text": body})
    return entries


def new_entries(remote: str, local: str) -> list[dict]:
    """The entries of the remote NEWS.md that this copy's NEWS.md does not have."""
    have = {entry["id"] for entry in parse_news(local)}
    return [entry for entry in parse_news(remote) if entry["id"] not in have]


def visible_subjects(subjects: list[str]) -> list[str]:
    """Commit subjects a user would care about (feat / fix / perf), without the prefix."""
    return [_VISIBLE.sub("", s).strip() for s in subjects if _VISIBLE.match(s)]


def message(found: dict) -> str:
    """The one message about a new version."""
    lines = ["🆕 *A new version of UniNotifier is ready*"]
    for entry in found["news"][:NEWS_SHOWN]:
        body = entry["text"] if len(entry["text"]) <= ENTRY_CHARS else entry["text"][:ENTRY_CHARS].rstrip() + "…"
        lines += ["", f"📣 *{escape_md(entry['title'])}*"] + ([escape_md(body)] if body else [])
    if len(found["news"]) > NEWS_SHOWN:
        lines.append(f"… and {len(found['news']) - NEWS_SHOWN} more in `{NEWS_FILE}`")
    if not found["news"]:
        subjects = visible_subjects(found["subjects"])
        if subjects:
            lines += ["", "*What changed:*"] + [f"• {escape_md(s)}" for s in subjects[:SUBJECTS_SHOWN]]
            if len(subjects) > SUBJECTS_SHOWN:
                lines.append(f"… and {len(subjects) - SUBJECTS_SHOWN} more")
        else:
            lines += ["", "Small improvements under the hood."]
    lines += ["", "⬇️ *Update Now* installs it and restarts the bot — about a minute. Your settings and data stay as they are."]
    return "\n".join(lines)


ROWS = [[{"text": "⬇️ Update Now", "callback_data": "update:now"},
         {"text": "⏰ Later", "callback_data": "update:later"}]]


# ---------------------------------------------------------------- the check

def load() -> dict:
    try:
        import json
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def look(root: Path | None = None) -> dict | None:
    """Fetch, and say what is waiting: {"sha", "behind", "news", "subjects"} — None when this copy
    is not a clone or the remote could not be reached; "behind" 0 when it is up to date."""
    root = root or paths.ROOT
    if not is_clone(root):
        return None
    try:
        if _git("fetch", "--quiet", root=root).returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    behind, sha = _out("rev-list", "--count", "HEAD..@{u}", root=root), _out("rev-parse", "@{u}", root=root)
    if behind is None or not sha or not behind.isdigit():
        return None
    try:
        local = (root / NEWS_FILE).read_text(encoding="utf-8")
    except OSError:
        local = ""
    subjects = (_out("log", "--format=%s", "HEAD..@{u}", root=root) or "").splitlines()
    return {"sha": sha, "behind": int(behind), "subjects": subjects,
            "news": new_entries(_out("show", f"@{{u}}:{NEWS_FILE}", root=root) or "", local)}


def due(now: float | None = None, state: dict | None = None) -> bool:
    now = time.time() if now is None else now
    return now - (load() if state is None else state).get("checked", 0) >= CHECK_EVERY


def check(send, now: float | None = None, root: Path | None = None) -> dict | None:
    """The daily look. `send(text, rows)` is called once per new version. Returns what was found."""
    now = time.time() if now is None else now
    state = load()
    found = look(root)
    state["checked"] = now
    if found and found["behind"] and state.get("told") != found["sha"]:
        if send(message(found), ROWS):
            state["told"] = found["sha"]
            log.info("Update: told about %s (%d commits behind)", found["sha"][:7], found["behind"])
    try:
        fileio.write_json_atomic(STATE_FILE, state)
    except OSError:
        log.warning("Update: could not save the check's state")
    return found


# ---------------------------------------------------------------- the update

def apply(root: Path | None = None, python: str | None = None) -> str:
    """Bring this copy to the remote version. Returns one line about what was done; raises
    UpdateError with the reason when nothing was changed (the copy is as it was)."""
    root = root or paths.ROOT
    if not is_clone(root):
        raise UpdateError("this copy was not cloned with git — download the new ZIP as the README's *Updating* says")
    changed = _out("status", "--porcelain", "--untracked-files=no", root=root)
    if changed is None:
        raise UpdateError("git could not read this copy")
    if changed:
        names = ", ".join(f"`{line.split(maxsplit=1)[-1]}`" for line in changed.splitlines()[:5])
        raise UpdateError(f"you changed project files yourself ({names}) — update by hand with `git pull`")
    old = _out("rev-parse", "HEAD", root=root)
    if not old or _out("fetch", "--quiet", root=root) is None:
        raise UpdateError("GitHub could not be reached — try again later")
    if _out("rev-list", "--count", "HEAD..@{u}", root=root) == "0":
        return "Already on the newest version."
    if _out("merge", "--ff-only", "--quiet", "@{u}", root=root) is None:
        raise UpdateError("this copy has commits of its own — update by hand with `git pull`")

    def back(reason: str) -> UpdateError:
        _out("reset", "--hard", "--quiet", old, root=root)          # the copy was clean: nothing of yours is lost
        return UpdateError(reason + " — the copy is back on the version it had")

    if _out("diff", "--name-only", old, "HEAD", "--", "requirements.txt", root=root):
        try:
            installed = gitsync.run_bounded([python or sys.executable, "-m", "pip", "install", "--quiet",
                                             "--disable-pip-version-check", "-r", "requirements.txt"],
                                            INSTALL_TIMEOUT, cwd=root)
        except (OSError, subprocess.TimeoutExpired):
            raise back("the new requirements could not be installed") from None
        if installed.returncode != 0:
            log.error("Update: pip failed: %s", installed.stderr.strip()[-400:])
            raise back("the new requirements could not be installed")
        try:                                # a new Playwright needs its own Chromium; nothing to do when it is there
            gitsync.run_bounded([python or sys.executable, "-m", "playwright", "install", "chromium"],
                                INSTALL_TIMEOUT, cwd=root)
        except (OSError, subprocess.TimeoutExpired):
            log.warning("Update: the browser could not be refreshed — run `python -m playwright install chromium`")
    broken = selfheal.code_problem(root)
    if broken:
        raise back(f"the new version does not start ({broken})")
    new = _out("rev-parse", "--short", "HEAD", root=root) or "the newest version"
    log.info("Update: %s -> %s", old[:7], new)
    return f"Updated to `{new}`."
