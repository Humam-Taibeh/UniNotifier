"""
Telling the user about problems — but only problems that persist: consecutive
failures are counted per kind, alerts are throttled, and "back to normal"
follows. Also the layout watchdog's report and the Microsoft-verification
policy.
"""

from datetime import datetime
import logging
import time

from uninotifier import failsafe
from uninotifier import heartbeat
from uninotifier import maintenance
from uninotifier import notifier
from uninotifier import selfheal
from uninotifier import watchdog
from uninotifier.monitor import config, state

log = logging.getLogger("uninotifier")


def send_telegram(text: str, url: str | None = None) -> bool:
    return notifier.send_message(config.BOT_TOKEN, config.CHAT_ID, text, url)


def alert_error(saved: dict, kind: str, text: str, png: bytes | None = None) -> None:
    """Error alerts are throttled per kind so a persistent problem (e.g. an
    expired session) doesn't message every 15 minutes. `png`: a screenshot of
    the page, sent as a file with the alert as its caption."""
    now = time.time()
    if now - saved["alerts"].get(kind, 0) < config.ERROR_ALERT_COOLDOWN:
        log.info("Suppressing repeated '%s' alert (cooldown)", kind)
        return
    if (png and notifier.send_png_file(config.BOT_TOKEN, config.CHAT_ID, png, maintenance.SNAPSHOT.name, text)) \
            or send_telegram(text):
        saved["alerts"][kind] = now
        saved["failures"]["alerted"] = True
        state.save_data(saved)


def record_failure(saved: dict, kind: str, text: str, notify: bool = True,
                   png: bytes | None = None) -> None:
    """Count consecutive failed runs and alert only once a failure has
    persisted for ALERT_AFTER_FAILURES[kind] runs in a row. notify=False
    when the user was already told some other way."""
    failures = saved["failures"]
    failures["count"] = failures.get("count", 0) + 1
    failures.setdefault("since", time.time())
    failures["kind"] = kind
    needed = config.ALERT_AFTER_FAILURES.get(kind, 1)
    if not notify:
        state.save_data(saved)
        return
    if failures["count"] < needed:
        log.info("Failure %d/%d (%s) — not alerting yet, likely transient",
                 failures["count"], needed, kind)
        state.save_data(saved)
        return
    if needed > 1:
        text += (f"\n\n_Failing since {notifier.audit_timestamp(failures['since'])} "
                 f"({failures['count']} consecutive checks)._")
    alert_error(saved, kind, text, png)
    state.save_data(saved)


def clear_failures(saved: dict) -> None:
    """A successful run: if the user was told about a problem, say it's over."""
    failures = saved["failures"]
    if failures.get("alerted"):
        send_telegram(f"✅ *Back to normal*\n\nPortal checks are working again "
                      f"(problem started {notifier.audit_timestamp(failures.get('since'))}).")
    saved["failures"] = {}
    saved["alerts"] = {}  # a successful run clears the error cooldowns


def report_layout(saved: dict, anomalies: list[str], accepted: list[str], now: float | None = None) -> None:
    """🧩 when the pages read differently (at most every 6 hours while it lasts),
    ✅ once they read normally again. Never fails the run."""
    now = now or time.time()
    layout = saved["layout"]
    if accepted:
        send_telegram(watchdog.accepted_text(accepted))
    if anomalies:
        if now - layout.get("alerted_at", 0) >= watchdog.REPEAT_SECONDS and send_telegram(watchdog.alert_text(anomalies)):
            layout["alerted_at"] = now
    elif layout.get("alerted_at"):
        if send_telegram(watchdog.RECOVERED_TEXT):
            layout.pop("alerted_at", None)
    state.save_data(saved)


def check_bot(saved: dict, now: float | None = None, uptime: float | None = None) -> None:
    """Look after the always-on bot. When a loop of it has not turned for
    heartbeat.STALE_SECONDS, restart it (selfheal.py: up to MAX_ATTEMPTS times,
    RETRY_AFTER apart — judged again on the next check); only when that cannot be
    done, or did not help, tell the owner (at most every ERROR_ALERT_COOLDOWN). When
    it is back, say so — and that it was restarted automatically, if it was. No pulse
    on disk = the bot never ran here. Right after Windows starts the bot has had no
    time to beat yet, so the first heartbeat.STALE_SECONDS of uptime are skipped.
    A check the bot itself started (/check) never restarts it. Never raises."""
    try:
        beat = heartbeat.read()
        if not beat:
            return
        now = time.time() if now is None else now
        uptime = heartbeat.uptime_seconds() if uptime is None else uptime
        watch = saved.setdefault("watch", {})
        stale = heartbeat.stale_parts(beat, now)
        if stale:
            if uptime < heartbeat.STALE_SECONDS:
                return
            watch.setdefault("bot_down_since", now)
            if not config.MANUAL_RUN and watch.get("bot_heals", 0) < selfheal.MAX_ATTEMPTS:
                if now - watch.get("bot_heal_at", float("-inf")) < selfheal.RETRY_AFTER:
                    return                       # the last restart is still being given its chance
                watch["bot_heals"] = watch.get("bot_heals", 0) + 1
                watch["bot_heal_at"] = now
                log.warning("The bot's heartbeat is stale (%s) — restarting it (attempt %d/%d)",
                            ", ".join(stale), watch["bot_heals"], selfheal.MAX_ATTEMPTS)
                if selfheal.restart_bot(beat.get("pid")):
                    state.save_data(saved)
                    return                       # judged on the next check
            told = watch.get("bot_alert_at")
            if told is None or now - told >= config.ERROR_ALERT_COOLDOWN:
                last = max((float(beat.get(p) or 0) for p in heartbeat.PARTS), default=0)
                tried = watch.get("bot_heals", 0)
                cause = selfheal.code_problem()
                if send_telegram(
                        "⚠️ *The bot isn't responding*\n\n"
                        f"Its last sign of life was {notifier.audit_timestamp(last)} "
                        f"({'listening for commands' if 'poll' in stale else 'lecture clock'} stopped). "
                        "Commands, buttons and lecture alerts are down; scheduled checks and the lecture "
                        "fallback still run.\n\n"
                        + (f"I restarted it {tried} time(s) and it did not recover. " if tried else "")
                        + (f"🔎 *Cause:* {_code_span(cause)} — fix it (or `git restore` that file), then restart the "
                           "`UniNotifier_BotListener` task." if cause else
                           "Restart the `UniNotifier_BotListener` task, or sign out and back in.")):
                    watch["bot_alert_at"] = now
        elif watch.get("bot_down_since"):
            if watch.get("bot_alert_at"):
                send_telegram("✅ *The bot is back* — commands and lecture alerts are working again.")
            elif watch.get("bot_heals"):
                send_telegram("🔧 *The bot had stopped responding — I restarted it* and it is working again.")
            for key in ("bot_down_since", "bot_alert_at", "bot_heals", "bot_heal_at"):
                watch.pop(key, None)
        state.save_data(saved)
    except Exception as e:
        log.exception("Bot heartbeat check failed")
        failsafe.bug("monitor:botwatch", "The check could not look after the bot", e,
                     "A stuck bot is not restarted automatically until it is fixed.")


def _code_span(text: str) -> str:
    return "`" + text.replace("`", "'") + "`"


def check_code(saved: dict) -> None:
    """A broken project file is told the moment it appears, while the bot that is running
    still works: it stops the next start (after Windows restarts), which is when it would
    otherwise be found — silently, hours later. Told once per mistake (again if it
    changes), and once when it is fixed. Never raises."""
    try:
        problem = selfheal.code_problem()
        watch = saved.setdefault("watch", {})
        known = watch.get("code_problem")
        if problem and problem != known:
            file = problem.split(" line ")[0].split(":")[0]
            if send_telegram(
                    "⚠️ *A project file is broken*\n\n"
                    f"{_code_span(problem)}\n\n"
                    "What is running now still works, but the bot will *not start again* after Windows "
                    "restarts or the bot is restarted, until this is fixed. Fix that line, or put the file "
                    f"back as it was: {_code_span('git restore ' + file)}."):
                watch["code_problem"] = problem
                state.save_data(saved)
        elif not problem and known:
            if send_telegram("✅ *The project files are fine again* — the bot will start normally."):
                watch.pop("code_problem", None)
                state.save_data(saved)
    except Exception as e:
        log.exception("Project file check failed")
        failsafe.bug("monitor:codecheck", "The check could not look at the project files", e,
                     "A broken file would only be noticed when the bot fails to start.")


def track_undelivered(saved: dict, failed: dict[str, str], now: float | None = None) -> None:
    """`failed`: key -> what it is, for every notification this run could not send (each is tried
    again next run). One that keeps failing — UNDELIVERED_ALERT_RUNS runs in a row — is no blip:
    the owner is told (failsafe: again when the stuck set changes, else at most every 6 h), and
    told again once everything has gone out. Never raises."""
    try:
        now = time.time() if now is None else now
        book = saved["undelivered"]
        for key in [k for k in book if k not in failed]:
            del book[key]
        for key, label in failed.items():
            entry = book.setdefault(key, {"runs": 0, "since": now})
            entry.update(runs=entry["runs"] + 1, label=label)
        stuck = sorted(k for k, e in book.items() if e["runs"] >= config.UNDELIVERED_ALERT_RUNS)
        if stuck:
            names = "\n".join(f"• {book[k]['label']}" for k in stuck[:8])
            more = f"\n… and {len(stuck) - 8} more" if len(stuck) > 8 else ""
            failsafe.report(
                "delivery", f"{len(stuck)} notification(s) could not be sent for {config.UNDELIVERED_ALERT_RUNS}+ checks",
                detail=f"{names}{more}\n\nEach check tries them again. The reason is in data/logs/monitor.log "
                       "(look for \"Telegram rejected\").",
                fingerprint=",".join(stuck))
        elif not book:
            failsafe.resolve("delivery", "✅ *The notifications that could not be sent have gone out.*")
        state.save_data(saved)
    except Exception as e:
        log.exception("Could not keep track of undelivered notifications")
        failsafe.bug("monitor:undelivered", "Keeping count of unsent notifications failed", e)


def mfa_allowed(saved: dict, now: datetime | None = None) -> bool:
    """May this run relay a Microsoft verification to Telegram?"""
    if config.MANUAL_RUN:
        return True
    now = now or datetime.now()
    start, end = config.MFA_QUIET_HOURS
    if start <= now.hour < end:
        return False
    return now.timestamp() - saved["mfa"].get("last_attempt", 0) >= config.MFA_RETRY_SECONDS


def failure_kind(exc: BaseException) -> str:
    return "network" if config.NETWORK_ERROR_RE.search(f"{type(exc).__name__}: {exc}") else "unexpected"
