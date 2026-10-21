"""
Where everything lives. The project root holds your configuration (.env,
schedule.json, assets/ — all three git-ignored, made from the *.example files);
everything the program writes goes to data/ (state, sessions, caches) and
data/logs/, created on first use. Every module takes its paths from here, so
the code can move without anything being written somewhere unexpected.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]       # src/uninotifier/paths.py -> the project folder
# The UNINOTIFIER_* variables put the settings, the timetable and the state folder elsewhere — the
# tests point them at a throw-away folder before anything is imported, and every child process
# they start inherits it, so no test can read your secrets or write your data.
ENV_FILE = Path(os.environ.get("UNINOTIFIER_ENV") or ROOT / ".env")
ENV_EXAMPLE = ROOT / ".env.example"
SCHEDULE_FILE = Path(os.environ.get("UNINOTIFIER_SCHEDULE") or ROOT / "schedule.json")
SCHEDULE_EXAMPLE = ROOT / "schedule.example.json"
ASSETS = Path(os.environ.get("UNINOTIFIER_ASSETS") or ROOT / "assets")   # your timetable as PDF / PNG
SCHEDULE_PDF = ASSETS / "Weekly_Academic_Schedule.pdf"     # sent by the Weekly Timetable button
SCHEDULE_IMAGE = ASSETS / "Weekly_Schedule.png"
DATA = Path(os.environ.get("UNINOTIFIER_DATA") or ROOT / "data")   # tracker state, session, caches, claim files
LOGS = DATA / "logs"                             # created when logging starts (notifier.setup_logging)
MONITOR_SCRIPT = ROOT / "monitor.py"              # the launcher the bot runs for a manual check

DATA.mkdir(parents=True, exist_ok=True)
