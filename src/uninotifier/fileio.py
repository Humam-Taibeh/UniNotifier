"""
Writing a state file so that a power cut or a forced restart cannot leave it
half-written: the data goes to a temporary file next to it, is flushed to the
disk, and only then replaces the real file in one step. (Without the flush,
Windows may replace the file with one the disk has not filled in yet, and a
power loss leaves it empty.) Windows also refuses the replace while another
process has the file open — the bot reading it, an antivirus scan — so the
replace is tried again for a few seconds before it gives up.
"""

import contextlib
import json
import os
import time
from pathlib import Path

LOCK_STALE_SECONDS = 10      # a write lock this old belongs to a process that died holding it
LOCK_WAIT_SECONDS = 15       # longer than LOCK_STALE_SECONDS: a dead holder's lock is always taken over in time
REPLACE_TRIES = 6            # a replace Windows refuses (the file is open elsewhere) is tried this often,
REPLACE_WAIT = 0.25          # waiting 0.25, 0.5, … s in between (about 4 s in all; a reader holds it for ms)


def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_WAIT * (attempt + 1))


def write_json_atomic(path: Path, data) -> None:
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


@contextlib.contextmanager
def write_lock(path: Path, wait: float = LOCK_WAIT_SECONDS):
    """`with write_lock(file):` — one writer at a time across processes (the monitor saving
    its state, the bot changing one field of it). Held for milliseconds: it guards a single
    read-modify-write, never a whole run. Raises TimeoutError when it cannot be had in time."""
    lock = Path(path).with_name(Path(path).name + ".lock")
    deadline = time.monotonic() + wait
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock.unlink(missing_ok=True)          # its holder died mid-write
                    continue
            except OSError:
                continue                                  # released in the meantime
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{lock.name} is held by another process")
            time.sleep(0.02)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)
