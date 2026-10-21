"""
Cloud backup of the tracker state — opt-in, into a private repository of its own.

The code repository never holds your state: data/ is git-ignored. To keep an
off-PC copy, make data/ a git repository of its own (once):

    python -m uninotifier.gitsync --init https://github.com/<you>/<private-repo>.git

From then on, when one of the backed-up files (data/.gitignore lists them:
saved_items.json, gpa.json, reminders.json — never sessions or secrets) has
changed, each check commits and pushes it. Never raises and never blocks long:
with no network (or no remote yet) the commit simply stays local and is pushed
by the first run that has a connection. Without data/.git, nothing happens.

"Never blocks long" needs care on Windows: a stalled HTTPS push leaves git's
helpers (git-remote-https, the credential manager) holding the output pipes,
and subprocess.run's timeout then waits for them forever — a 60 s timeout
once held the run lock for 13 minutes and made the scheduler skip the next
checks. So git writes to temporary files instead of pipes, a timeout kills
the whole process tree, git itself gives up on a transfer that stalls, and
the credential manager may never open a window.
"""

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from uninotifier import failsafe, paths

log = logging.getLogger("uninotifier")

ROOT_DIR = paths.ROOT
STATE_REPO = paths.DATA        # the backup repository: data/ itself, once --init has made it one
# What the backup repository commits: everything else in data/ is a cache, a session or a secret.
BACKED_UP = ("saved_items.json", "gpa.json", "reminders.json")
STATE_GITIGNORE = ("# UniNotifier state backup: only the files listed with ! are committed;\n"
                   "# sessions, cookies, caches and logs never leave this PC.\n*\n!.gitignore\n"
                   + "".join(f"!{name}\n" for name in BACKED_UP))
COMMIT_MESSAGE = "sync: update tracker state"
GIT_TIMEOUT = 45
# abort a transfer slower than 1 KB/s for 20 s, instead of waiting on a dead connection
STALL_OPTIONS = ("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=20")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                       creationflags=NO_WINDOW)
    else:
        proc.kill()


def run_bounded(cmd: list[str], timeout: float, env: dict | None = None,
                cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run cmd; past `timeout` kill it and everything it started, then raise
    subprocess.TimeoutExpired. Output goes through temporary files, so a
    child left behind can't keep the wait open."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, cwd=cwd or ROOT_DIR, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, env=env, creationflags=NO_WINDOW)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(cmd, code, out.read().decode("utf-8", "replace"),
                                           err.read().decode("utf-8", "replace"))


def _git(*args: str, timeout: int = GIT_TIMEOUT, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """git in the backup repository, with a hard time limit. Raises subprocess.TimeoutExpired."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",   # fail instead of hanging on a credential prompt
           "GCM_INTERACTIVE": "never"}                 # ... or on a credential-manager window
    return run_bounded(["git", *STALL_OPTIONS, *args], timeout, env, cwd or STATE_REPO)


def enabled() -> bool:
    """The backup is on when data/ is a git repository of its own (see --init)."""
    return (STATE_REPO / ".git").is_dir()


def sync_state() -> None:
    try:
        if not enabled():
            return

        if _git("status", "--porcelain").stdout.strip():
            _git("add", "-A")                          # data/.gitignore keeps it to the BACKED_UP files
            res = _git("commit", "-m", COMMIT_MESSAGE)
            if res.returncode != 0:
                log.warning("git commit failed: %s", (res.stderr or res.stdout).strip()[:200])
                return
            log.info("State committed (%s)", COMMIT_MESSAGE)

        if not _git("remote").stdout.strip():
            log.info("No git remote configured — backup stays local")
            return

        upstream = _git("rev-list", "--count", "@{u}..HEAD")
        if upstream.returncode == 0:
            if upstream.stdout.strip() == "0":
                return
            push = _git("push")
        else:  # branch has no upstream yet
            push = _git("push", "-u", "origin", "HEAD")

        if push.returncode == 0:
            log.info("State pushed to remote")
        else:
            log.warning("git push failed (offline?) — will retry next run: %s",
                        (push.stderr or "").strip()[:200])
    except subprocess.TimeoutExpired as e:
        if {"add", "commit"} & set(e.cmd):
            # the killed git can't remove its own index lock; left behind, it would block every later commit
            (STATE_REPO / ".git" / "index.lock").unlink(missing_ok=True)
        log.warning("git %s timed out after %g s (slow or no connection to GitHub) — "
                    "the state stays committed locally and is pushed by a later run",
                    " ".join(a for a in e.cmd[1:] if a not in STALL_OPTIONS), e.timeout)
    except Exception as e:
        log.exception("Git state sync failed — continuing")
        failsafe.bug("gitsync", "The cloud backup to GitHub failed", e,
                     "Your tracker state stays on this PC only until it is fixed.")


def init(remote: str | None = None) -> int:
    """Make data/ the backup repository (idempotent): its own .gitignore, a first commit,
    and — given a remote — a push. A credential-account pin of the code repository is
    copied, so a credential manager holding several GitHub accounts never shows a picker."""
    def ok(res: subprocess.CompletedProcess, what: str) -> bool:
        if res.returncode != 0:
            print(f"{what} failed: {(res.stderr or res.stdout).strip()[:300]}")
        return res.returncode == 0

    STATE_REPO.mkdir(parents=True, exist_ok=True)
    if not enabled() and not ok(_git("init", "-b", "main"), "git init"):
        return 1
    (STATE_REPO / ".gitignore").write_text(STATE_GITIGNORE, encoding="utf-8")
    pin = _git("config", "--get", "credential.https://github.com.username", cwd=ROOT_DIR).stdout.strip()
    if pin:
        _git("config", "--local", "credential.https://github.com.username", pin)
    for key, fallback in (("user.name", "UniNotifier"), ("user.email", "uninotifier@localhost")):
        if not _git("config", "--get", key).stdout.strip():      # a PC where git was never given a name
            _git("config", "--local", key, fallback)
    _git("add", "-A")
    if _git("status", "--porcelain").stdout.strip() and not ok(_git("commit", "-m", "backup: first state snapshot"), "git commit"):
        return 1
    if remote:
        has_origin = _git("remote", "get-url", "origin").returncode == 0
        if not ok(_git("remote", "set-url" if has_origin else "add", "origin", remote), "git remote"):
            return 1
        if not ok(_git("push", "-u", "origin", "HEAD", timeout=120), "git push"):
            print("The backup repository is set up; the push is retried by every check.")
            return 1
    print(f"State backup is on: {STATE_REPO}" + (f" -> {remote}" if remote else " (local only: add a remote with --init <url>)"))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] != "--init" or len(args) > 2:
        sys.exit("Usage: python -m uninotifier.gitsync --init [https://github.com/<you>/<private-repo>.git]")
    sys.exit(init(args[1] if len(args) == 2 else None))
