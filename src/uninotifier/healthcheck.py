"""
Dead-man's-switch heartbeat for the scheduled check (healthchecks.io).

The monitor pings after every *successful* run. If no ping arrives for longer
than the check's period + grace time, healthchecks.io alerts you by its own
channels (e-mail, Telegram, ...). That catches what the bot can't report by
itself: the PC being off or asleep, the scheduled task stopped or broken, the
monitor hanging, or the session failing for hours.

  start    run begun       (lets healthchecks.io time the run and spot a hang)
  success  run finished OK (resets the countdown)
  log      run failed      (recorded in the check's log; the countdown is NOT
                            reset, so a persistent failure alerts after the
                            grace time while a single network blip stays quiet)

Set HEALTHCHECK_URL in .env to the check's ping URL (https://hc-ping.com/<uuid>).
Unset = disabled. The bot has its own, independent check (BOT_HEALTHCHECK_URL):
its pulse thread pings it while the bot's loops are turning, so a crashed or
hung bot is noticed the same way. Pings never raise and never block a run for more than a few
seconds. The URL is a secret: it is never logged.
"""

import logging
import os
import queue
import threading
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger("uninotifier")

ENV_NAME = "HEALTHCHECK_URL"
BOT_ENV_NAME = "BOT_HEALTHCHECK_URL"     # the always-on bot's own check (see ping_bot)
TIMEOUT = 10
RETRIES = 2


def url(env_name: str = ENV_NAME) -> str | None:
    """The ping URL if it's a plausible https address, else None."""
    raw = (os.getenv(env_name) or "").strip().rstrip("/")
    parsed = urlparse(raw)
    return raw if parsed.scheme == "https" and parsed.netloc else None


def _ping(suffix: str, body: str = "", env_name: str = ENV_NAME) -> bool:
    base = url(env_name)
    if not base:
        return False
    for attempt in range(1, RETRIES + 1):
        try:
            requests.post(f"{base}{suffix}", data=body.encode("utf-8")[:10000], timeout=TIMEOUT).raise_for_status()
            return True
        except requests.RequestException as e:
            # the exception text would contain the secret URL: log only its type
            log.warning("Healthcheck ping %s failed (%s), attempt %d/%d",
                        suffix or "/", type(e).__name__, attempt, RETRIES)
    return False


def start() -> bool:
    return _ping("/start")


def success(note: str = "") -> bool:
    return _ping("", note)


def ping_bot(note: str = "") -> bool:
    """The bot's loops are turning (its own check, BOT_HEALTHCHECK_URL; unset = disabled).
    Suggested on healthchecks.io: period 5 min, grace 15 min."""
    return _ping("", note, BOT_ENV_NAME)


# The two pings of a run (/start, then the result) used to be sent in line: ~3 s of every run spent
# waiting for healthchecks.io. A single standing worker sends them beside the real work and keeps their
# order (start always before the result); wait() lets the process end only after the last one is out.
_queue: queue.Queue = queue.Queue()
_worker_started = False
_Thread = threading.Thread


def _worker() -> None:
    while True:
        job = _queue.get()
        try:
            job()
        except Exception:
            log.exception("Healthcheck ping failed")
        finally:
            _queue.task_done()


def _later(job) -> None:
    global _worker_started
    if not _worker_started:
        _worker_started = True
        _Thread(target=_worker, name="healthcheck", daemon=True).start()
    _queue.put(job)


def start_bg() -> None:
    _later(start)


def report_bg(exit_code: int, note: str = "") -> None:
    _later(lambda: report(exit_code, note))


def wait(timeout: float = 20.0) -> bool:
    """Block until every queued ping has been sent, or `timeout` seconds. True when none is left."""
    end = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.02)
    return not _queue.unfinished_tasks


def log_failure(message: str) -> bool:
    return _ping("/log", message)


def report(exit_code: int, note: str = "") -> None:
    """Ping the outcome of a run: success on exit 0, otherwise a log entry."""
    if exit_code == 0:
        success(note)
    else:
        log_failure(note or f"run failed (exit code {exit_code})")
