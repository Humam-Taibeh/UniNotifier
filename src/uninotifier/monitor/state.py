"""
What the check remembers between runs (saved_items.json), the one-run-at-a-
time lock, and the summary of the last run that the bot reads (/check).
"""

import json
import logging
import os
import time

from uninotifier import failsafe, fileio, notifier, paths
from uninotifier.monitor import config

log = logging.getLogger("uninotifier")


DATA_FILE = paths.DATA / "saved_items.json"


LAST_RUN_FILE = paths.DATA / "last_run.json"   # read by bot.py for the /check summary


LOCK_FILE = paths.DATA / "monitor.lock"        # one run at a time (scheduler vs. /check)


SESSION_FILE = paths.DATA / "auth_state.json"


STATE_KEYS = ("courses", "content", "deadlines", "files", "alerts", "schedule_sent", "teachers",
              "forums", "failures", "mfa", "deep", "days", "layout", "watch", "undelivered")


SUMMARY: dict = {"ok": False}


def pid_alive(pid: int) -> bool:
    if os.name == "nt":       # os.kill(pid, 0) would *terminate* the process on Windows
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259                # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def lock_holder_gone() -> bool:
    """The lock's PID has exited — the run was killed (the scheduler's time
    limit, a shutdown) before it could release the lock."""
    try:
        pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False          # unreadable or still being written: trust its age instead
    return pid != os.getpid() and not pid_alive(pid)


def acquire_lock() -> bool:
    """Exclusive run lock, so the scheduled task and a manual /check never
    overlap. A lock whose process is gone, or older than LOCK_STALE_SECONDS,
    belongs to a dead run."""
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                fresh = time.time() - LOCK_FILE.stat().st_mtime < config.LOCK_STALE_SECONDS
            except OSError:
                continue
            if fresh and not lock_holder_gone():
                return False
            log.warning("Taking over the run lock of a run that is no longer running")
            LOCK_FILE.unlink(missing_ok=True)
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    return False


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def write_last_run(error: str | None = None) -> None:
    try:
        SUMMARY.update(finished=time.time(), error=error)
        fileio.write_json_atomic(LAST_RUN_FILE, SUMMARY)
    except Exception as e:
        log.exception("Could not write last_run.json")
        failsafe.bug("monitor:last-run", "The check could not record its result", e,
                     "The bot's status line and /check's summary show an older run until it is fixed.")


# ------------------------------------------------------------------- state

BACKUP_KEEP_DAYS = 14          # one backup per day for this many days (plus the latest)
CORRUPT_KEEP = 5               # unreadable state files kept for inspection


def backup_dir():
    return DATA_FILE.parent / "backups"


def read_state(path) -> dict | None:
    """A state file's content, or None when it is missing, unreadable or not a JSON object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _daily_backups() -> list:
    return sorted(backup_dir().glob("saved_items.????-??-??.json"), reverse=True)     # newest first


def backup_state() -> None:
    """After a good run, keep the state twice: as saved_items.latest.json and as
    the day's backup (the day's last one is kept, BACKUP_KEEP_DAYS days back). An
    unreadable file, or an empty baseline, never replaces a good backup. Never raises."""
    try:
        data = read_state(DATA_FILE)
        if not data or not data.get("courses"):
            return
        folder = backup_dir()
        folder.mkdir(exist_ok=True)
        text = DATA_FILE.read_text(encoding="utf-8")
        fileio.write_text_atomic(folder / "saved_items.latest.json", text)
        fileio.write_text_atomic(folder / f"saved_items.{time.strftime('%Y-%m-%d')}.json", text)
        for old in _daily_backups()[BACKUP_KEEP_DAYS:]:
            old.unlink(missing_ok=True)
    except Exception as e:
        log.exception("Could not back up saved_items.json")
        failsafe.bug("monitor:backup", "saved_items.json could not be backed up", e,
                     "A damaged state file could not be restored until this is fixed.")


def recover_state() -> dict:
    """saved_items.json is unreadable (a power cut, a bad edit): keep it aside for
    inspection, put the newest good backup in its place and tell the owner. With no
    backup the run starts a fresh baseline — and says so, because anything published
    in the meantime would go unannounced."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log.error("saved_items.json is unreadable — looking for a backup")
    folder = backup_dir()
    try:
        folder.mkdir(exist_ok=True)
        DATA_FILE.replace(folder / f"saved_items.corrupt-{stamp}.json")
        for old in sorted(folder.glob("saved_items.corrupt-*.json"), reverse=True)[CORRUPT_KEEP:]:
            old.unlink(missing_ok=True)
    except OSError:
        log.exception("Could not set the unreadable state file aside")
    for candidate in [folder / "saved_items.latest.json", *_daily_backups()]:
        data = read_state(candidate)
        if data and data.get("courses"):
            try:
                fileio.write_text_atomic(DATA_FILE, candidate.read_text(encoding="utf-8"))
            except OSError:
                log.exception("Could not put the backup back")
                break
            failsafe.report(
                "state", "saved_items.json was unreadable — restored from a backup",
                detail=f"Restored the copy from {notifier.audit_timestamp(candidate.stat().st_mtime)}. Anything published "
                       "since then may be announced a second time; nothing is lost. The damaged file is kept in data/backups/.",
                fingerprint=stamp)
            return data
    failsafe.report(
        "state", "saved_items.json was unreadable and there is no backup",
        detail="Starting a fresh baseline: what is on the portal now is taken as already known, so anything "
               "published in the last few hours may go unannounced. The damaged file is kept in data/backups/.",
        fingerprint=stamp)
    return {}


def load_saved_data() -> dict:
    data = {}
    if DATA_FILE.exists():
        data = read_state(DATA_FILE)
        if data is None:
            data = recover_state()
    for key in STATE_KEYS:
        data.setdefault(key, {})
    return data


def save_data(data: dict) -> None:
    """Atomic write, flushed to the disk (fileio). Also proof of life for the run
    lock: a run that keeps delivering (and saving) is never mistaken for a dead
    one, however long the Telegram timeouts make it. Under the file's write lock: the bot
    changes a field of it too (app.set_item_done)."""
    with fileio.write_lock(DATA_FILE):
        fileio.write_json_atomic(DATA_FILE, data)
    try:
        os.utime(LOCK_FILE)
    except OSError:
        pass  # no lock held (tests, tools)


def public(item: dict) -> dict:
    """An item without its run-only fields (bodies, flags prefixed "_")."""
    return {k: v for k, v in item.items() if not k.startswith("_")}
