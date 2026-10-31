"""
One check from start to finish (run) and the process entry point (main):
lock, scan, deliver and record what is new, the day's record, then report to
healthchecks.io and back the state up to Git.
"""

import logging
import sys
import time

from uninotifier import paths
from uninotifier import checkin
from uninotifier import daily
from uninotifier import envcheck
from uninotifier import failsafe
from uninotifier import gitsync
from uninotifier import healthcheck
from uninotifier import maintenance
from uninotifier import media
from uninotifier import mfa
from uninotifier import moodle
from uninotifier import notifier
from uninotifier import tasks
from uninotifier import watchdog
from uninotifier.monitor import alerts, config, notify, scan, state, timed
from uninotifier.notifier import escape_md
from uninotifier.zuj_auth import LoginError, MFARequiredError

log = logging.getLogger("uninotifier")


def run(announce: bool = False) -> None:
    """One check. `announce` (monitor.py --announce) makes a first run with an
    empty state report everything it finds, instead of saving it silently as
    the baseline."""
    if not all([config.USERNAME, config.PASSWORD, config.BOT_TOKEN, config.CHAT_ID]):
        log.error("Missing credentials — fill in your .env file (see .env.example).")
        sys.exit(1)

    saved = state.load_saved_data()
    is_first_run = not saved["courses"] and not saved["content"] and not announce

    try:
        timed.send_schedule_messages(saved)
    except Exception as e:
        log.exception("Schedule reminders failed")
        failsafe.bug("monitor:schedule", "The check could not send the lecture reminders", e,
                     "The morning summary and the lecture fallback (when the bot is down) may be missing.")
    state.save_data(saved)
    alerts.check_bot(saved)
    alerts.check_code(saved)

    handler = mfa.TelegramMFA(config.BOT_TOKEN, config.CHAT_ID) if alerts.mfa_allowed(saved) else None
    try:
        result = scan.scan_with_retries(saved, notify_new=not is_first_run, mfa_handler=handler)
    except watchdog.LayoutChanged as e:
        log.error("Layout check: %s", e)
        alerts.record_failure(saved, "layout", watchdog.alert_text([str(e)]))
        sys.exit(1)
    except MFARequiredError as e:
        log.error("Session needs renewing: %s", e)
        if handler and handler.started:
            saved["mfa"]["last_attempt"] = handler.started
        alerts.record_failure(
            saved, "mfa",
            "⚠️ *Session needs renewal*\n\n"
            "Microsoft is asking for verification (Authenticator / SMS). Send /check and "
            "I'll relay it here — the Authenticator number, or a request for the SMS code.",
            notify=not e.notified,
        )
        sys.exit(1)
    except maintenance.PortalUnavailable as e:
        if e.phrase:
            # the portal's own state, not a failure of the monitor: reported once (and
            # "back to normal" later), and the run ends cleanly — no exit code 1
            state.SUMMARY["maintenance"] = e.phrase
            alerts.record_failure(saved, "maintenance", maintenance.alert_text(e, config.MAINTENANCE_FOLLOW_UP), png=e.png)
            sys.exit(0)
        alerts.record_failure(saved, "signin", maintenance.alert_text(e), png=e.png)
        sys.exit(1)
    except LoginError as e:
        log.error("Login failed: %s", e)
        alerts.record_failure(saved, "login", f"⚠️ *Portal login failed*\n{escape_md(notifier.redact(str(e)))}")
        sys.exit(1)
    except Exception as e:
        kind = alerts.failure_kind(e)
        retried = f" after {len(config.SCAN_RETRY_SECONDS) + 1} attempts" if kind == "network" else ""
        log.exception("%s failure while talking to the portal%s", kind.capitalize(), retried)
        alerts.record_failure(saved, kind,
                       "📡 *Cannot reach the portal*\n\nThe university site or the internet connection "
                       "is down. Checks continue automatically." if kind == "network" else
                       "⚠️ *Unexpected error while checking the portal* — see `data/logs/monitor.log`")
        sys.exit(1)
    finally:
        if handler:
            handler.close()

    alerts.clear_failures(saved)
    saved["mfa"] = {}
    alerts.report_layout(saved, result.get("anomalies", []), result.get("accepted", []))
    courses, content, deadlines = result["courses"], result["content"], result["deadlines"]
    artifacts, updates, submissions = result["artifacts"], result["updates"], result["submissions"]
    state.SUMMARY.update(courses=len(courses), items=len(content), seconds=result["seconds"],
                   browser_used=result["browser_used"])

    # Record every archived file (id + size) so it is never downloaded again.
    for item_id, art in artifacts.items():
        if art.get("path"):
            saved["files"][item_id] = {"path": art["path"], "size": art.get("size", 0),
                                       "sha256": art.get("sha256"), "modified": art.get("modified")}

    new_courses = {cid: c for cid, c in courses.items() if cid not in saved["courses"]}
    new_content = {iid: it for iid, it in content.items() if iid not in saved["content"]}

    # From here on, whatever is delivered is written down at once (save_data after each
    # message), not at the end of the run: a run that dies half-way — killed by /check's
    # time limit, the PC going to sleep, a second run starting on a stale lock — must not
    # make the next cycle announce the same things again.
    saved["deadlines"] = deadlines
    state.save_data(saved)

    # Items whose Telegram notification failed are left out of the saved
    # state so the next run reports them again.
    undelivered_content: set[str] = set()
    undelivered_courses: set[str] = set()
    failed: dict[str, str] = {}          # everything that could not be sent this run (alerts.track_undelivered)

    if is_first_run:
        log.info("First run — saving baseline silently (%d courses, %d items, %d deadlines).",
                 len(courses), len(content), len(deadlines))
    else:
        log.info("New: %d course(s), %d content item(s)", len(new_courses), len(new_content))
        for cid, course in new_courses.items():
            if notify.notify_new_course(course):
                saved["courses"][cid] = course
                state.save_data(saved)
            else:
                undelivered_courses.add(cid)
                failed[f"course:{cid}"] = f"New course: {media.clean_course_name(course['name'])}"
        for iid, item in new_content.items():
            if media.silent_container(item):               # an empty forum: its posts are reported below
                saved["content"][iid] = state.public(item)
                state.save_data(saved)
                continue
            deadline = deadlines.get(f'{item["type"]}-{moodle.extract_cmid(item["url"])}')
            submitted = submissions.get(f'assign-{moodle.extract_cmid(item["url"])}')
            if notify.notify_new_item(item, deadline, artifacts.get(iid, {}), submitted):
                saved["content"][iid] = state.public(item)
                state.save_data(saved)
            else:
                undelivered_content.add(iid)
                failed[f"item:{iid}"] = f"{item['name']} — {media.clean_course_name(item['course'])}"
        if not new_courses and not new_content:
            log.info("No new content.")

    now = time.time()
    state.SUMMARY.update(
        ok=True,
        new_courses=len(new_courses),
        new_items=[{"type": it["type"], "name": it["name"],
                    "course": media.clean_course_name(it["course"])} for it in new_content.values()
                   if not media.silent_container(it)],
        updated_files=len(updates),
        pending=sum(1 for d in deadlines.values()
                    if d["timestamp"] > now and not d.get("submitted")),
        submitted=[d["name"] for k, d in deadlines.items() if submissions.get(k)],
    )

    for iid, art in updates.items():
        log.info("File updated: %s", content[iid]["name"])
        if notify.notify_file_update(content[iid], art):
            saved["files"][iid] = {"path": art["path"], "size": art["size"],
                                   "sha256": art["sha256"], "modified": art["modified"],
                                   **({"versions": art["versions"]} if art.get("versions") else {})}
            state.save_data(saved)
        # on failure the old record stays, so the next run detects it again (the files are already in
        # place: archive.put_new_version moves and copies nothing twice)

    # An edit that failed to deliver keeps its old fingerprint: retried next run.
    for iid, art in result["edits"].items():
        if notify.notify_content_update(content[iid], art):
            saved["content"][iid] = state.public(content[iid])        # the new fingerprint: announced once
            state.save_data(saved)
        else:
            content[iid] = {**content[iid], "hash": saved["content"][iid]["hash"]}

    # Forum posts: remember every post seen, except new ones that failed to send.
    undelivered_posts: set[str] = set()
    for iid, fresh in result["posts"].items():
        for post in fresh:
            if notify.notify_forum_post(content[iid], post):
                known = saved["forums"].setdefault(iid, [])
                if post["id"] not in known:
                    known.append(post["id"])
                state.save_data(saved)                                # seen the moment it is delivered
            else:
                undelivered_posts.add(post["id"])
                failed[f"post:{post['id']}"] = f"A post in {content[iid]['name']} — {media.clean_course_name(content[iid]['course'])}"
    forums = {}
    for iid, posts in result["forum_posts"].items():
        known = set(saved["forums"].get(iid, []))
        if posts is not None:
            known |= {p["id"] for p in posts if p["id"] not in undelivered_posts}
        if posts is not None or iid in saved["forums"]:
            forums[iid] = sorted(known, key=int)
    state.SUMMARY.update(new_posts=sum(len(p) for p in result["posts"].values()),
                   edited=len(result["edits"]))

    for change in result["link_changes"]:
        notify.notify_lecture_link(change)
    if not is_first_run:
        alerts.track_undelivered(saved, failed)

    # The day's record for the 11:59 PM recap (daily.py): anything the portal
    # published since the last check counts, delivered or not.
    found_new = not is_first_run and bool(
        new_courses or any(not media.silent_container(it) for it in new_content.values())
        or any(result["posts"].values()) or updates or result["edits"])
    daily.note_check(saved["days"], found_new)
    state.save_data(saved)
    try:
        daily.catch_up(config.BOT_TOKEN, config.CHAT_ID, saved["days"], found_new)
        daily.send_recap(config.BOT_TOKEN, config.CHAT_ID, saved["days"])
    except Exception as e:
        log.exception("Daily recap failed")
        failsafe.bug("monitor:recap", "The 11:59 PM recap failed", e)

    try:
        timed.send_deadline_reminders(deadlines, on_sent=lambda: state.save_data(saved))
    except Exception as e:
        log.exception("Reminder pass failed")
        failsafe.bug("monitor:deadlines", "The deadline reminders failed", e,
                     "The 24 h / 1 h reminders before assignments and quizzes may not arrive.")

    saved.update({
        "courses": {k: v for k, v in courses.items() if k not in undelivered_courses},
        "content": {k: state.public(v) for k, v in content.items() if k not in undelivered_content},
        "deadlines": deadlines,
        "forums": forums,
    })
    state.save_data(saved)
    state.backup_state()
    try:
        tasks.send_digest(config.BOT_TOKEN, config.CHAT_ID, saved)    # the noon digest, if the bot missed it
    except Exception as e:
        log.exception("Open-tasks digest failed")
        failsafe.bug("monitor:tasks", "The daily open-tasks message failed", e,
                     "The noon list of open assignments and quizzes may not arrive.")
    try:
        checkin.tick(config.BOT_TOKEN, config.CHAT_ID, checkin.Watch())   # attendance, if the bot is down
    except Exception as e:
        log.exception("Automatic check-in failed")
        failsafe.bug("monitor:checkin", "Automatic attendance check-in is failing", e,
                     "Check in yourself until it is fixed: the Check In button in the lecture alert.")


def main() -> None:
    """One scheduled (or /check) run: lock, check, report, back up. Exits with
    0 (done), 1 (failed) or EXIT_BUSY (another run holds the lock)."""
    notifier.setup_logging(paths.LOGS / "monitor.log")
    failsafe.install_excepthooks("monitor")
    envcheck.require("monitor")       # a missing password ends here, with a list of what to fix
    exit_code = 0
    if not state.acquire_lock():
        log.info("Another run is in progress — skipping this one")
        sys.exit(config.EXIT_BUSY)
    healthcheck.start_bg()
    notifier.before_send = lambda: daily.ensure_separator(config.BOT_TOKEN, config.CHAT_ID)   # every message here is automatic
    try:
        run(announce="--announce" in sys.argv[1:])
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
        raise
    except Exception as e:
        exit_code = 1
        log.exception("Fatal error — monitor run aborted")
        # a bug in the code (not the portal): every run would fail the same way, unseen
        failsafe.report(f"monitor:fatal:{failsafe.signature(e)}", "A check crashed on a bug", exc=e)
        sys.exit(1)
    else:                             # a run that finishes: if a crash was told, say it is over
        failsafe.resolve_matching("monitor:fatal:", "✅ *The scheduled check works again* — it ran through "
                                                    "without the bug that stopped it.")
    finally:
        failsafe.wait_idle()
        state.write_last_run(None if exit_code == 0 else f"exit code {exit_code}")
        attempts = state.SUMMARY.get("attempts", 1)
        healthcheck.report_bg(exit_code, (
            f'portal under maintenance ({state.SUMMARY["maintenance"]})' if state.SUMMARY.get("maintenance") else
            f'{state.SUMMARY.get("courses", 0)} courses, {state.SUMMARY.get("items", 0)} items, '
            f'{state.SUMMARY.get("seconds", "?")} s' + (f", attempt {attempts}" if attempts > 1 else ""))
            if exit_code == 0 else f"exit code {exit_code} — see data/logs/monitor.log")
        gitsync.sync_state()  # runs on every exit path, never raises
        state.release_lock()
        healthcheck.wait()   # the process ends after the last ping is out (they were sent beside the work)
