"""
UniNotifier — the scheduled check.

Windows Task Scheduler (task "UniNotifier") runs this file every 15 minutes, and the bot runs it for /check. The code lives in src/uninotifier/monitor; this file only
puts src/ on the import path and starts it, so the scheduled task never needs
to change.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

try:
    from uninotifier.monitor import main  # noqa: E402
except Exception as exc:                  # a broken edit must not make every check fail unseen
    from uninotifier import failsafe
    failsafe.report("monitor:import", "The scheduled check could not start",
                    detail="The code has an error that stops it loading; no checks run until it is fixed.", exc=exc)
    raise

if __name__ == "__main__":
    main()
