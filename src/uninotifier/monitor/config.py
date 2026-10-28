"""
Settings for the scheduled check: credentials from .env, and the timings,
limits and thresholds the other parts of the check use.
"""

import logging
import os
import re

from dotenv import load_dotenv

from uninotifier import paths

log = logging.getLogger("uninotifier")


# Load explicitly from the script's own directory: a scheduled task's
# working directory is not this folder.
load_dotenv(paths.ENV_FILE)


USERNAME = os.getenv("ZUJ_USERNAME")


PASSWORD = os.getenv("ZUJ_PASSWORD")


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


STUDENT_ID = os.getenv("STUDENT_ID") or os.getenv("PORTAL_USERNAME") or ""   # for finding your marks in files


LOCK_STALE_SECONDS = 30 * 60                 # ... or at once, when the PID in it is gone


EXIT_BUSY = 3


ERROR_ALERT_COOLDOWN = 6 * 3600  # seconds between repeats of the same error alert


# A check that fails on the network is retried within the same run, after
# these pauses, before the run counts as failed (each request is also retried,
# see httpctx.py). The scheduler's 15-minute limit leaves room for all three.
SCAN_RETRY_SECONDS = (20, 60)


# Consecutive failed runs (15 min apart) before an error is reported. A
# single network blip or portal hiccup heals itself on the next run.
ALERT_AFTER_FAILURES = {"network": 3, "unexpected": 2, "mfa": 1, "login": 1, "maintenance": 1, "signin": 1,
                        "layout": 2}


# A notification that could not be sent is tried again on the next run. One that still fails
# after this many runs in a row (Telegram refusing it, not a blip) is reported.
UNDELIVERED_ALERT_RUNS = 3


SITE_NAME = "e-learning portal"


MAINTENANCE_FOLLOW_UP = "Checks carry on every 15 minutes — I'll tell you when it's back."


NETWORK_ERROR_RE = re.compile(
    r"net::ERR_|NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|Timeout|timed out|"
    r"Connection(Error|Reset|Refused|Aborted)|getaddrinfo|Max retries exceeded", re.I)


REMINDER_WINDOWS = (("24h", 24 * 3600), ("1h", 1 * 3600))


# Microsoft verification relayed to Telegram: an unanswered attempt is
# retried automatically at most hourly and never at night, so an ignored
# request can't turn into a stream of Authenticator pushes. A manual /check
# (bot.py sets UNINOTIFIER_MANUAL=1) always tries immediately.
MFA_RETRY_SECONDS = 3600


MFA_QUIET_HOURS = (0, 7)   # local time, [start, end)


MANUAL_RUN = os.getenv("UNINOTIFIER_MANUAL") == "1"


TEACHERS_REFRESH_SECONDS = 7 * 86400


# A lecturer re-uploading a file is rare, but every run used to ask the portal about EVERY archived
# file (one HEAD request each: ~10 s of a 29 s run). Now each file is asked about once per
# FILE_PROBE_SECONDS, its turn spread over the runs (scan.probe_due) so no run is the heavy one.
# A manual /check asks about all of them.
SCAN_SLOT_SECONDS = 15 * 60          # the scheduler's period: a "run slot"
FILE_PROBE_SECONDS = 2 * 3600


# Speed: a check reads the portal over plain HTTP, a few requests at a time,
# and starts the browser only when it must. The activity-index "deep check"
# (which needs rendered pages) confirms nothing was missed every few hours
# instead of every run — it has never found anything the activity tree lacked.
HTTP_WORKERS = 4


DEEP_CHECK_SECONDS = 6 * 3600


# Screenshots: one long image per item, rendered at 2x and sent as a file
# (Telegram recompresses photos, never documents), so text stays sharp when
# zoomed. Chromium can't render an image taller than ~16k device pixels, so
# only a taller block is cut — into as few equal parts as possible.
VIEWPORT = {"width": 1100, "height": 900}


SCALE = 2


MAX_SHOT_HEIGHT = 8000   # CSS px per image (x SCALE = 16000 device px)


MAX_SHOT_PARTS = 6
