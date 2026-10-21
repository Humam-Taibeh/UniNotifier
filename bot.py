"""
UniNotifier — the always-on Telegram bot.

Windows Task Scheduler (task "UniNotifier_BotListener") starts this file at logon. The code lives in src/uninotifier/bot; this file only
puts src/ on the import path and starts it, so the scheduled task never needs
to change.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

try:
    from uninotifier.bot import main  # noqa: E402
except Exception as exc:                # a broken edit must not make the bot die unseen
    from uninotifier import failsafe
    failsafe.report("bot:import", "The bot could not start", detail="The code has an error that stops it loading.", exc=exc)
    raise

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
