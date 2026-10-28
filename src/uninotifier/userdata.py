"""
The files that hold what YOU entered — your reminders (reminders.json) and the Grade
Needed plan (gpa.json). Nothing on the portal can rebuild them, so:

  save()   writes atomically (fileio) and keeps a copy in data/backups/ — the newest
           (<name>.latest.json) and one per day for BACKUP_KEEP_DAYS days
  load()   a missing file is simply empty; an UNREADABLE one is never taken as empty
           (the next save would wipe everything in it): it is kept aside for inspection,
           the newest good backup is put back and you are told. Only with no backup at
           all does it start empty — and says so.

A file that cannot be opened at all (locked by another program for a moment) raises
instead of reading as empty, for the same reason.
"""

import json
import logging
import time
from pathlib import Path

from uninotifier import failsafe, fileio, notifier

log = logging.getLogger("uninotifier")

BACKUP_KEEP_DAYS = 14
CORRUPT_KEEP = 5


def backup_dir(path: Path) -> Path:
    return Path(path).parent / "backups"


def _read(path: Path) -> dict | None:
    """The file's content, or None when it is not a readable JSON object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _daily(path: Path) -> list[Path]:
    stem = Path(path).stem
    return sorted(backup_dir(path).glob(f"{stem}.????-??-??.json"), reverse=True)      # newest first


def save(path: Path, data: dict) -> None:
    """Write the file, then back it up. A failed backup never fails the save."""
    path = Path(path)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fileio.write_text_atomic(path, text)
    try:
        folder = backup_dir(path)
        folder.mkdir(exist_ok=True)
        fileio.write_text_atomic(folder / f"{path.stem}.latest.json", text)
        fileio.write_text_atomic(folder / f"{path.stem}.{time.strftime('%Y-%m-%d')}.json", text)
        for old in _daily(path)[BACKUP_KEEP_DAYS:]:
            old.unlink(missing_ok=True)
    except OSError as e:
        log.exception("Could not back up %s", path.name)
        failsafe.bug(f"userdata:backup:{path.stem}", f"{path.name} could not be backed up", e)


def load(path: Path, what: str) -> dict:
    """The file's content ({} when it does not exist yet). `what` names it for the owner
    ("your reminders"). Raises OSError when the file exists but cannot be opened."""
    path = Path(path)
    try:
        data = _read(path)
    except FileNotFoundError:
        return {}
    if data is not None:
        return data
    return _recover(path, what)


def _recover(path: Path, what: str) -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = backup_dir(path)
    log.error("%s is unreadable — looking for a backup", path.name)
    try:
        folder.mkdir(exist_ok=True)
        path.replace(folder / f"{path.stem}.corrupt-{stamp}.json")
        for old in sorted(folder.glob(f"{path.stem}.corrupt-*.json"), reverse=True)[CORRUPT_KEEP:]:
            old.unlink(missing_ok=True)
    except OSError:
        log.exception("Could not set the unreadable %s aside", path.name)
    for candidate in [folder / f"{path.stem}.latest.json", *_daily(path)]:
        try:
            data = _read(candidate)
        except OSError:
            continue
        if data is None:
            continue
        try:
            fileio.write_json_atomic(path, data)
        except OSError:
            log.exception("Could not put the backup of %s back", path.name)
        failsafe.report(
            f"userdata:{path.stem}", f"{path.name} was unreadable — {what} restored from a backup",
            detail=f"Restored the copy from {notifier.audit_timestamp(candidate.stat().st_mtime)}; a change made "
                   "after it may be missing. The damaged file is kept in data/backups/.",
            fingerprint=stamp, wait=False)
        return data
    failsafe.report(
        f"userdata:{path.stem}", f"{path.name} was unreadable and there is no backup",
        detail=f"{what[0].upper() + what[1:]} start empty. The damaged file is kept in data/backups/ — "
               "what was in it can be copied back by hand.",
        fingerprint=stamp, wait=False)
    return {}
