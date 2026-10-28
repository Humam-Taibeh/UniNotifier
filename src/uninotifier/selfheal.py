"""
Self-healing for the always-on bot. When a scheduled check finds the bot's
heartbeat stale (heartbeat.py), it does what you would do by hand: end the stuck
process (only if it really is this project's bot.py — a recycled process id must
never cost an innocent program its life) and start the "UniNotifier_BotListener"
task again. It starts the task; it never edits its definition.

Policy (monitor/alerts.py: check_bot): at most MAX_ATTEMPTS restarts per outage,
spaced RETRY_AFTER apart, then a plain "isn't responding" alert to a human.
Never raises.
"""

import logging
import os
import time

from uninotifier import gitsync, paths

log = logging.getLogger("uninotifier")

TASK_NAME = "UniNotifier_BotListener"
MAX_ATTEMPTS = 3
RETRY_AFTER = 10 * 60


def _run(cmd: list[str], timeout: float):
    return gitsync.run_bounded(cmd, timeout)


def is_our_bot(pid) -> bool:
    """Is `pid` a process started from THIS project's bot.py?"""
    if os.name != "nt" or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    res = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"], 30)
    line = res.stdout.lower()
    return "bot.py" in line and str(paths.ROOT).lower() in line


def restart_bot(pid) -> bool:
    """End the stuck bot (if `pid` is really it) and start its task. True when the
    task was started."""
    try:
        if is_our_bot(pid):
            log.warning("Ending the unresponsive bot (pid %s)", pid)
            _run(["taskkill", "/PID", str(pid), "/T", "/F"], 20)
            time.sleep(2)
        res = _run(["schtasks", "/Run", "/TN", TASK_NAME], 20)
        if res.returncode != 0:
            log.error("Could not start the %s task: %s", TASK_NAME, (res.stderr or res.stdout).strip()[:200])
        return res.returncode == 0
    except Exception:
        log.exception("Restarting the bot failed")
        return False
