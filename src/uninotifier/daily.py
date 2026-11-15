"""
Claim files: one marker file per message or attempt that must happen once, however many
processes (the bot's clock, the scheduled monitor) look at the same minute.

A claim is an exclusively-created file (data/daily_sent/ here; lectures.py and reminders.py keep
theirs in folders of their own, through the same code): `claim` is True for exactly one caller
per key, `release` gives it back when the message could not be delivered, and `prune` forgets
markers older than a week.

A claim taken right before a send is taken *pending* (claim(key, pending=True)) and made final
with `confirm` once the message is out. A process killed between the two — the bot restarted,
an update, a power cut — leaves a pending claim whose process is gone: it is ABANDONED, and the
next caller takes it over, so the message is sent instead of being held back for a week. A
final claim (and every claim of an older version: an empty file) is never taken over.
"""

import json
import os
import time
from pathlib import Path

from uninotifier import paths

CLAIM_DIR = paths.DATA / "daily_sent"
KEEP_DAYS = 7
PENDING_LIMIT = 15 * 60            # no send takes this long: an older pending claim is abandoned in any case

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


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


def marker(directory: Path, key: str) -> Path:
    return directory / f"{key}.sent"


def _pending(path: Path) -> dict | None:
    """The pending claim in `path` ({"pid", "at"}), or None: a final claim, an older version's empty
    marker, or one being written this instant — all of them real claims."""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "null")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None


def abandoned(path: Path, now: float | None = None) -> bool:
    found = _pending(path)
    if found is None:
        return False
    now = time.time() if now is None else now
    return (found["pid"] != os.getpid() and not pid_alive(found["pid"])) or now - found.get("at", 0) > PENDING_LIMIT


def _take_over(path: Path) -> bool:
    """Move an abandoned claim out of the way. Only one process can rename a given file, and the moved
    file is checked to still be the abandoned one — a fresh claim moved by mistake is put back."""
    seen = _pending(path)
    aside = path.with_name(f"{path.name}.{os.getpid()}.abandoned")
    try:
        os.rename(path, aside)
    except OSError:
        return False                                  # someone else took it over first
    if _pending(aside) != seen:                       # a fresh claim replaced it in between: give it back
        try:
            os.rename(aside, path)
        except OSError:
            pass
        return False
    aside.unlink(missing_ok=True)
    return True


def claim_in(directory: Path, key: str, pending: bool = False) -> bool:
    """True for exactly one caller per key. `pending`: a send follows — confirm() it once it is out."""
    directory.mkdir(parents=True, exist_ok=True)
    path = marker(directory, key)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if abandoned(path) and _take_over(path):
                continue
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if pending:
                f.write(json.dumps({"pid": os.getpid(), "at": time.time()}))
        return True
    return False


def confirm_in(directory: Path, key: str) -> None:
    """The send went out: the claim becomes final, never taken over."""
    try:
        marker(directory, key).write_text("", encoding="utf-8")
    except OSError:
        pass


def release_in(directory: Path, key: str) -> None:
    marker(directory, key).unlink(missing_ok=True)


def prune_in(directory: Path) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in directory.glob("*.sent") if directory.is_dir() else []:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def claim(key: str, pending: bool = False) -> bool:
    return claim_in(CLAIM_DIR, key, pending)


def confirm(key: str) -> None:
    confirm_in(CLAIM_DIR, key)


def release(key: str) -> None:
    release_in(CLAIM_DIR, key)


def claimed(key: str) -> bool:
    return marker(CLAIM_DIR, key).exists()


def prune() -> None:
    prune_in(CLAIM_DIR)
