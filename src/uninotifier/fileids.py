"""
Telegram remembers every file it has received: once a document has been uploaded, the `file_id` in
Telegram's reply sends the same file again without uploading a byte. This module is that memory —
a file's SHA-256 and size mapped to its file_id (data/file_ids.json) — so the timetable album,
which used to be read from the disk and uploaded again on every request (1.4 MB at ~0.35 MB/s: 5-9 s),
is delivered in a fraction of a second after the first time.

The key is the file's CONTENT, not its name or path: a changed file is a different key and is uploaded
once more; an unchanged one is never uploaded twice. A file_id Telegram no longer accepts is
forgotten by the sender (notifier.py) and the file uploaded afresh. Nothing here raises.
"""

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

from uninotifier import fileio, paths

log = logging.getLogger("uninotifier")

STORE = paths.DATA / "file_ids.json"
MAX_ENTRIES = 300

_lock = threading.Lock()
_hashes: dict[tuple, str] = {}              # (path, mtime_ns, size) -> sha256: a big file is hashed once, not per send


def fingerprint(path: Path) -> str | None:
    """'<sha256>:<size>' of the file's content, or None if it cannot be read."""
    try:
        stat = Path(path).stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        with _lock:
            digest = _hashes.get(key)
        if digest is None:
            sha = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    sha.update(chunk)
            digest = sha.hexdigest()
            with _lock:
                _hashes[key] = digest
        return f"{digest}:{stat.st_size}"
    except OSError:
        return None


def _load() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def lookup(fp: str | None) -> str | None:
    if not fp:
        return None
    with _lock:
        entry = _load().get(fp)
    return entry.get("file_id") if isinstance(entry, dict) else None


def remember(fp: str | None, file_id: str | None, name: str = "") -> None:
    if not fp or not file_id:
        return
    try:
        with _lock:
            data = _load()
            data[fp] = {"file_id": file_id, "name": name[:80], "at": int(time.time())}
            if len(data) > MAX_ENTRIES:
                for old in sorted(data, key=lambda k: data[k].get("at", 0))[:len(data) - MAX_ENTRIES]:
                    del data[old]
            fileio.write_json_atomic(STORE, data)
    except OSError:
        log.warning("Could not write %s", STORE.name)


def forget(fp: str | None) -> None:
    if not fp:
        return
    try:
        with _lock:
            data = _load()
            if data.pop(fp, None) is not None:
                fileio.write_json_atomic(STORE, data)
    except OSError:
        log.warning("Could not write %s", STORE.name)


def ids_from_reply(body: dict | None) -> list[str | None]:
    """The file_id of each document in a sendDocument reply (one message) or a sendMediaGroup reply
    (a list of messages), in order; None where a message carries no document."""
    result = (body or {}).get("result")
    messages = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
    return [((m or {}).get("document") or {}).get("file_id") for m in messages]
