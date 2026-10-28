"""
The fail-loud layer: a crash, a bug or a broken settings file ends in a
Telegram message to you, not in one line of a log nobody reads.

  report()               tell you about a problem — each kind of problem at most
                         once per COOLDOWN (a bug that fires every 20 s must not
                         become 4000 messages), and again at once if it turns into
                         a different problem (a new `fingerprint`)
  resolve()              the "it's fine again" follow-up
  install_excepthooks()  anything that escapes — the main thread or a background
                         thread — is reported before the process dies

Everything here is best effort: reporting a problem must never cause one, so
no function raises. The holding-back record lives in data/failsafe.json, so a
crash loop (the scheduler restarting a dying bot) is also rate-limited across
restarts.
"""

import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv

from uninotifier import fileio, notifier, paths
from uninotifier.notifier import escape_md, redact

log = logging.getLogger("uninotifier")

load_dotenv(paths.ENV_FILE)

STATE_FILE = paths.DATA / "failsafe.json"
COOLDOWN = 6 * 3600               # seconds before the same kind of problem is reported again
DETAIL_LIMIT = 400

send = notifier.send_message      # the only door out (tests replace it)

_lock = threading.Lock()
_inflight: set[str] = set()       # kinds being delivered right now


def _credentials() -> tuple[str, str]:
    return os.getenv("TELEGRAM_BOT_TOKEN") or "", str(os.getenv("TELEGRAM_CHAT_ID") or "")


def _load() -> dict:
    try:
        import json
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    try:
        fileio.write_json_atomic(STATE_FILE, state)
    except OSError:
        log.warning("Could not write %s", STATE_FILE.name)


def _frame(exc: BaseException) -> traceback.FrameSummary | None:
    """The innermost frame inside this project (else the innermost one)."""
    frames = traceback.extract_tb(exc.__traceback__) if exc and exc.__traceback__ else []
    mine = [f for f in frames if "uninotifier" in f.filename.replace("\\", "/")]
    return (mine or frames or [None])[-1]


def signature(exc: BaseException) -> str:
    """Identifies a bug: 'KeyError@app.py:196' — the same bug keeps one signature."""
    frame = _frame(exc)
    where = f"@{Path(frame.filename).name}:{frame.lineno}" if frame else ""
    return f"{type(exc).__name__}{where}"


def describe(exc: BaseException) -> str:
    """'KeyError: 'id' — app.py:196 in handle_update'"""
    frame = _frame(exc)
    where = f" — {Path(frame.filename).name}:{frame.lineno} in {frame.name}" if frame else ""
    return f"{type(exc).__name__}: {str(exc)[:200]}{where}"


def _code(text: str) -> str:
    """Text for a Markdown `code` span: no backticks, no token, bounded."""
    return "`" + redact(text).replace("`", "'")[:DETAIL_LIMIT] + "`"


def _text(headline: str, detail: str, exc: BaseException | None, held_back: int) -> str:
    lines = ["🚨 *UniNotifier needs attention*", "", f"*{escape_md(redact(headline))}*"]
    if detail:
        lines += ["", escape_md(redact(detail)[:DETAIL_LIMIT * 2])]
    if exc is not None:
        lines += ["", _code(describe(exc))]
    lines += ["", "_The full details are in the log files in_ `data/logs/`."]
    if held_back:
        lines += [f"_{held_back} similar problem(s) were held back since the last alert._"]
    return "\n".join(lines)


def _deliver(kind: str, text: str, fingerprint: str) -> bool:
    try:
        token, chat = _credentials()
        if not (token and chat):
            log.warning("failsafe: no Telegram credentials — could not report %s", kind)
            return False
        ok = bool(send(token, chat, text))
        if ok:
            with _lock:
                state = _load()
                state[kind] = {"at": time.time(), "fp": fingerprint, "suppressed": 0, "active": True}
                _save(state)
        return ok
    except Exception:
        log.exception("failsafe: delivery of %s failed", kind)
        return False
    finally:
        with _lock:
            _inflight.discard(kind)


def report(kind: str, headline: str, detail: str = "", exc: BaseException | None = None,
           fingerprint: str = "", wait: bool = True, cooldown: float | None = None) -> bool:
    """Tell the owner about a problem. `kind` names the problem (the hold-back
    key); `fingerprint` changes when it becomes a different instance of the same
    kind (another broken file content). wait=False sends from a background thread,
    for callers that must not stop (the bot's polling loop). Returns True when a
    message went out (or is on its way). `cooldown` overrides COOLDOWN for this kind."""
    try:
        with _lock:
            state = _load()
            entry = state.get(kind) or {}
            if kind in _inflight or (time.time() - entry.get("at", 0) < (COOLDOWN if cooldown is None else cooldown)
                                     and entry.get("fp", "") == fingerprint):
                entry["suppressed"] = entry.get("suppressed", 0) + 1
                state[kind] = entry
                _save(state)
                return False
            _inflight.add(kind)
            held_back = entry.get("suppressed", 0)
        text = _text(headline, detail, exc, held_back)
        if wait:
            return _deliver(kind, text, fingerprint)
        threading.Thread(target=_deliver, args=(kind, text, fingerprint), name="failsafe", daemon=True).start()
        return True
    except Exception:
        log.exception("failsafe: could not report %s", kind)
        with _lock:
            _inflight.discard(kind)
        return False


def bug(area: str, headline: str, exc: BaseException, detail: str = "") -> bool:
    """report() for an exception a catch-all swallows so the program keeps going: the log line
    alone would leave a feature switched off without anyone knowing. One kind per bug (area +
    signature, so two different bugs in one place are both told), sent from a background thread
    (safe on the bot's polling and clock threads; the monitor waits for it before it exits)."""
    return report(f"{area}:{signature(exc)}", headline, detail=detail, exc=exc, wait=False)


def resolve(kind: str, text: str, wait: bool = True) -> bool:
    """The follow-up to report(): if `kind` was reported and still counts as open,
    say it is over (once)."""
    try:
        with _lock:
            state = _load()
            entry = state.get(kind) or {}
            if not entry.get("active"):
                return False
            entry["active"] = False
            entry["suppressed"] = 0
            state[kind] = entry
            _save(state)
        token, chat = _credentials()
        if not (token and chat):
            return False

        def job():
            try:
                send(token, chat, text)
            except Exception:
                log.exception("failsafe: could not send the follow-up for %s", kind)

        if wait:
            job()
        else:
            threading.Thread(target=job, name="failsafe", daemon=True).start()
        return True
    except Exception:
        log.exception("failsafe: could not resolve %s", kind)
        return False


def forget(prefix: str, older_than: float, now: float | None = None) -> int:
    """Drop the hold-back records of kinds starting with `prefix` whose last alert is older than
    `older_than` seconds (e.g. one per stranger's id, which would otherwise pile up). Returns how
    many went. Never raises."""
    try:
        now = time.time() if now is None else now
        with _lock:
            state = _load()
            old = [k for k, e in state.items() if k.startswith(prefix) and now - (e or {}).get("at", 0) >= older_than]
            for kind in old:
                del state[kind]
            if old:
                _save(state)
        return len(old)
    except Exception:
        log.exception("failsafe: could not forget %s*", prefix)
        return 0


def wait_idle(timeout: float = 10.0) -> None:
    """Block until the background deliveries have finished (tests; a clean exit)."""
    for thread in threading.enumerate():
        if thread.name == "failsafe" and thread is not threading.current_thread():
            thread.join(timeout)


def install_excepthooks(source: str) -> None:
    """Report whatever escapes: an uncaught exception that ends the process
    (before it ends), and one that kills a background thread."""
    previous = sys.excepthook

    def on_main(exc_type, exc, tb):
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            report(f"{source}:crash:{signature(exc)}", f"The {source} crashed and stopped", exc=exc)
        previous(exc_type, exc, tb)

    def on_thread(args):
        if args.exc_type is not SystemExit and args.exc_value is not None:
            name = args.thread.name if args.thread else "?"
            report(f"{source}:thread:{name}:{signature(args.exc_value)}",
                   f"A background thread of the {source} ({name}) died", exc=args.exc_value, wait=False)
        threading.__excepthook__(args)

    sys.excepthook = on_main
    threading.excepthook = on_thread
