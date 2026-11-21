"""
⚙️ Settings: what the bot does by itself, switched on and off with one tap — no file to edit.

  ✍️ Auto Check-In       AUTO_CHECK_IN in .env   checks you in to your online lectures (marks you Present)
  ✅ Auto Mark as Done   AUTO_MARK_DONE in .env  ticks a lecture file the bot saved as done on Moodle
  📝 Midterm Watch       data/exams_watch.json   reads the exams site for your midterm marks

A switch is written to .env through setup.write_env (the file read back to prove it, kept to your Windows
account) and set in this process at once: the bot's own threads see it immediately, the 15-minute check at
its next run. A value in .env wins over an "auto_check_in" a schedule.json of your own may hold.
🆕 Check for Updates is here too (the same screen as /update).
"""

import logging
import os

from uninotifier import envcheck, exams_watch, media, schedule
from uninotifier.bot import chat

log = logging.getLogger("uninotifier")

# (.env name, button label, what it does — said on the screen)
SWITCHES = (
    ("AUTO_CHECK_IN", "✍️ Auto Check-In",
     "checks you in to your *online* lectures by itself. It marks you Present whether or not you attend — "
     "turn it off for a lecture you will miss."),
    ("AUTO_MARK_DONE", "✅ Auto Mark as Done",
     "ticks *Mark as done* on Moodle for a lecture file it saved. Your instructor's completion report shows it."),
)
MIDTERMS = "MIDTERMS"


def is_on(name: str) -> bool:
    if name == MIDTERMS:
        return exams_watch.enabled()
    if name == "AUTO_CHECK_IN":
        return schedule.auto_check_in()
    return envcheck.value(name).lower() == "true"


def _state(on: bool) -> str:
    return "🟢 On" if on else "⚪ Off"


def view_settings(note: str = "") -> tuple[str, list]:
    lines = ["⚙️ *Settings*", media.DIVIDER]
    for name, label, words in SWITCHES:
        lines += [f"{label} — *{_state(is_on(name))}*", words, ""]
    lines += [f"📝 Midterm Watch — *{_state(is_on(MIDTERMS))}*",
              "reads the exams site every 15 minutes and tells you each midterm mark as it appears."]
    if note:
        lines += ["", note]
    rows = [[{"text": f"{label}: Turn {'Off' if is_on(name) else 'On'}", "callback_data": f"set:{name}"}]
            for name, label, _ in SWITCHES]
    rows.append([{"text": f"📝 Midterm Watch: Turn {'Off' if is_on(MIDTERMS) else 'On'}", "callback_data": f"set:{MIDTERMS}"}])
    rows.append([{"text": "🆕 Check for Updates", "callback_data": "set:update"}])
    return "\n".join(lines).rstrip(), rows + [chat.BACK_ROW]


def switch(name: str, on: bool) -> str:
    """Turn one switch on or off. "" when done, else why not (in words for the screen)."""
    if name == MIDTERMS:
        exams_watch.set_enabled(on)
        return ""
    if name not in {n for n, _, _ in SWITCHES}:
        return "⚠️ That setting is not known."
    from uninotifier import setup
    value = "true" if on else "false"
    try:
        setup.write_env({name: value})
    except (setup.SetupError, OSError) as e:
        log.warning("Settings: %s could not be saved (%s)", name, type(e).__name__)
        return "⚠️ The settings file could not be saved — nothing was changed."
    os.environ[name] = value                       # this process sees it at once (.env is read at the start only)
    log.info("Settings: %s turned %s", name, "on" if on else "off")
    if name == "AUTO_CHECK_IN" and on and not (envcheck.value("PORTAL_USERNAME") and envcheck.value("PORTAL_PASSWORD")):
        return "⚠️ Auto Check-In needs the portal login (`PORTAL_USERNAME`, `PORTAL_PASSWORD`) to know your online days."
    return ""


def handle_callback(cb: dict, action: str, message_id: int | None) -> None:
    """set:<NAME> flips one switch · set:update opens 🆕 Updates."""
    name = action.split(":", 1)[1]
    if name == "update":
        chat.answer(cb["id"], "🔎 Looking for a new version…")
        from uninotifier.bot import update_flow
        screen = update_flow.view_status()
    else:
        chat.answer(cb["id"])
        screen = view_settings(switch(name, not is_on(name)))
    if message_id:
        chat.edit_view(message_id, *screen)
    else:
        chat.send_view(*screen)
