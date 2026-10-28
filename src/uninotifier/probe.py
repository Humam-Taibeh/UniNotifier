"""
The light look at Moodle between the scheduled checks: every PROBE_SECONDS the bot asks Moodle
what changed — ONE request with core_course_get_updates_since for every course and the
deadline calendar (~1.4 KB, ~0.3 s, measured) — and when something did, it starts a check
at once instead of leaving it to the next 15-minute slot.

  what counts   an activity added or changed (configuration), a file uploaded or replaced
                (contentfiles, introfiles), a forum discussion (discussions, posts), a new or
                moved deadline (the calendar). Not "completion": that is our own Mark as done.
  what it is not  a check: it reads nothing else, notifies nothing, downloads nothing. The
                scheduled check stays the full safety net (layout changes, visibility, ...).

It never signs in: without a valid saved session it waits (SESSION_RETRY) and leaves the sign-in
— and any Microsoft verification — to the monitor. The session key is kept between looks (the
dashboard that carries it is ~330 KB); it is fetched again only when Moodle refuses it.
A check it starts is an ordinary one (not /check): the verification policy applies. It starts
none while a check is running (the monitor's lock, /check), at most one per MIN_TRIGGER_GAP,
and never a second while its own is still running. Never raises.
"""

import json
import logging
import os
import subprocess
import sys
import time

import requests

from uninotifier import failsafe, moodle, paths
from uninotifier.httpctx import HttpContext

log = logging.getLogger("uninotifier")

PROBE_SECONDS = 120              # between two looks; 0 switches the probe off
MIN_TRIGGER_GAP = 5 * 60         # at most one check started per this many seconds
SESSION_RETRY = 15 * 60          # without a valid session, look again after this long
OVERLAP = 5 * 60                 # each look asks back this far before the previous one (clock skew)
SEEN_KEEP = 2 * 86400            # changes remembered (so one is acted on once) for this long
IGNORED_AREAS = {"completion", "gradeitems", "outcomes"}     # our own Mark as done; grades are not watched

SESSION_FILE = paths.DATA / "auth_state.json"
STATE_FILE = paths.DATA / "saved_items.json"
MONITOR_LOCK = paths.DATA / "monitor.lock"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Probe:
    """One probe's memory: what it has seen, when it last looked, the cached session key."""

    def __init__(self):
        self.http: HttpContext | None = None       # kept between looks: one warm connection to Moodle
        self.sesskey: str | None = None
        self.last_look = 0.0
        self.since: float | None = None            # None until the first look (the baseline)
        self.seen: dict[str, float] = {}           # change key -> when it was first seen
        self.calendar: str | None = None
        self.last_trigger = float("-inf")
        self.wait_until = 0.0
        self.process: subprocess.Popen | None = None


def course_ids() -> list[str]:
    try:
        return sorted(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("courses", {}), key=int)
    except (OSError, ValueError):
        return []


def signals(results: list[dict], course_list: list[str]) -> tuple[set[str], str | None, bool]:
    """(change keys, the calendar's fingerprint, refused?) from one batched answer: the
    get_updates_since results of `course_list` in order, then the calendar's."""
    found: set[str] = set()
    refused = len(results) < len(course_list) + 1 or any(r.get("error") for r in results)
    for course_id, result in zip(course_list, results):
        for instance in ((result.get("data") or {}).get("instances") or []):
            for update in instance.get("updates") or []:
                if update.get("name") not in IGNORED_AREAS:
                    found.add(f"{course_id}:{instance.get('id')}:{update.get('name')}:{update.get('timeupdated')}")
    calendar = None
    if len(results) > len(course_list) and not results[len(course_list)].get("error"):
        events = (results[len(course_list)].get("data") or {}).get("events") or []
        calendar = json.dumps(sorted((e.get("id"), e.get("timesort"), e.get("name")) for e in events))
    return found, calendar, refused


def look(probe: Probe, now: float) -> bool:
    """One look. True when something new was found (the caller starts a check)."""
    if probe.http is None:
        probe.http = HttpContext.from_state_file(SESSION_FILE)
    http = probe.http
    if http is None:
        probe.wait_until = now + SESSION_RETRY
        return False
    if not probe.sesskey:
        probe.sesskey = http.sesskey()
        if not probe.sesskey:                      # the saved session has expired: the monitor signs in
            log.info("Moodle probe: no valid session — leaving it to the scheduled check")
            probe.http, probe.wait_until = None, now + SESSION_RETRY
            return False
    courses = course_ids()
    if not courses:
        return False
    since = int((probe.since if probe.since is not None else now) - OVERLAP)
    calls = [("core_course_get_updates_since", {"courseid": int(c), "since": since}) for c in courses]
    calls.append(("core_calendar_get_action_events_by_timesort", {"timesortfrom": 0, "limitnum": 50}))
    found, calendar, refused = signals(moodle.ajax_batch(http, probe.sesskey, calls), courses)
    if refused:                                    # an expired key or session: both read afresh next time
        probe.http = probe.sesskey = None
        return False
    baseline = probe.since is None
    new = {key for key in found if key not in probe.seen}
    for key in new:
        probe.seen[key] = now
    for key in [k for k, t in probe.seen.items() if now - t > SEEN_KEEP]:
        del probe.seen[key]
    moved = probe.calendar is not None and calendar is not None and calendar != probe.calendar
    probe.calendar = calendar if calendar is not None else probe.calendar
    probe.since = now
    if baseline:                                   # the first look only learns what is there
        log.info("Moodle probe: watching %d course(s), a look every %d s", len(courses), PROBE_SECONDS)
        return False
    if new or moved:
        log.info("Moodle probe: %d change(s)%s", len(new), " and the deadlines moved" if moved else "")
    return bool(new or moved)


def check_running(probe: Probe) -> bool:
    from uninotifier.bot import checks                  # the bot's /check (bot imports this module)
    own = probe.process is not None and probe.process.poll() is None
    return own or MONITOR_LOCK.exists() or checks._check_lock.locked()


def launch_check() -> subprocess.Popen:
    """An ordinary check, in its own process (the monitor's lock keeps it from overlapping)."""
    return subprocess.Popen([sys.executable, str(paths.MONITOR_SCRIPT)], cwd=paths.ROOT,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={k: v for k, v in os.environ.items() if k != "UNINOTIFIER_MANUAL"},
                            creationflags=NO_WINDOW)


def tick(probe: Probe, now: float | None = None) -> bool:
    """Called often (the probe thread); looks only when due. True when it started a check."""
    now = time.time() if now is None else now
    if not PROBE_SECONDS or now - probe.last_look < PROBE_SECONDS or now < probe.wait_until:
        return False
    probe.last_look = now
    if check_running(probe):
        return False                               # a check is reading the portal right now anyway
    try:
        changed = look(probe, now)
    except (requests.RequestException, ValueError) as e:     # no network, or not JSON (a maintenance page)
        log.info("Moodle probe: %s — trying again at the next look", type(e).__name__)
        probe.http = probe.sesskey = None
        return False
    if not changed or now - probe.last_trigger < MIN_TRIGGER_GAP:
        return False
    probe.last_trigger = now
    probe.process = launch_check()
    log.info("Moodle probe: started a check")
    return True


def loop() -> None:
    """The bot's probe thread."""
    probe = Probe()
    while True:
        try:
            tick(probe)
        except Exception as e:
            log.exception("Moodle probe failed")
            failsafe.bug("bot:probe", "The 2-minute Moodle probe is failing", e,
                         "New posts and files still arrive with the scheduled check (every 15 min).")
        time.sleep(20)
