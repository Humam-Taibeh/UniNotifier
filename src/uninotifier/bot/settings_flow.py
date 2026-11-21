"""
⚙️ Settings: what the bot does by itself, switched on and off with one tap — no file to edit.

  ✍️ Auto Check-In       AUTO_CHECK_IN in .env   checks you in to your online lectures (marks you Present)
  ✅ Auto Mark as Done   AUTO_MARK_DONE in .env  ticks a lecture file the bot saved as done on Moodle

(Midterm Watch is switched on its own screen, 📝 Midterm Watch — not here.)

A switch is written to .env through setup.write_env (the file read back to prove it, kept to your Windows
account) and set in this process at once: the bot's own threads see it immediately, the 15-minute check at
its next run. A value in .env wins over an "auto_check_in" a schedule.json of your own may hold.
🆕 Check for Updates is here too (the same screen as /update).
"""

import logging
import os

from uninotifier import envcheck, media, schedule
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


def is_on(name: str) -> bool:
    if name == "AUTO_CHECK_IN":
        return schedule.auto_check_in()
    return envcheck.value(name).lower() == "true"


def _state(on: bool) -> str:
    return "🟢 On" if on else "⚪ Off"


def view_settings(note: str = "") -> tuple[str, list]:
    lines = ["⚙️ *Settings*", media.DIVIDER]
    for name, label, words in SWITCHES:
        lines += [f"{label} — *{_state(is_on(name))}*", words, ""]
    if note:
        lines += [note]
    rows = [[{"text": f"{label}: Turn {'Off' if is_on(name) else 'On'}", "callback_data": f"set:{name}"}]
            for name, label, _ in SWITCHES]
    rows.append([{"text": "🆕 Check for Updates", "callback_data": "set:update"}])
    return "\n".join(lines).rstrip(), rows + [chat.BACK_ROW]


def switch(name: str, on: bool) -> str:
    """Turn one switch on or off. "" when done, else why not (in words for the screen)."""
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


# ------------------------------------------------------------------ Slack: the Settings window

VIEW_ID = "settings"                # the window's callback_id
# what each switch does, said under its tick box (Slack shows at most 75 characters there)
TICK_WORDS = {"AUTO_CHECK_IN": "Checks you in to online lectures — marks you Present even if absent.",
              "AUTO_MARK_DONE": "Ticks saved lecture files as done — your instructor sees it."}


def _plain(text: str) -> dict:
    return {"type": "plain_text", "text": text, "emoji": True}


def modal(panel: bool = False) -> dict:
    """⚙️ Settings as a Slack window over the chat: a tick box per switch (with what it does), Save / Cancel, and
    🆕 Check for Updates. Nothing changes until Save. Opened from the 🎛️ Control Panel (`panel`), Save closes the
    panel with it."""
    options = [{"text": {"type": "mrkdwn", "text": f"*{label}*"},
                "description": {"type": "mrkdwn", "text": TICK_WORDS[name]}, "value": name}
               for name, label, _ in SWITCHES]
    ticks = {"type": "checkboxes", "action_id": "auto", "options": options}
    on = [option for option in options if is_on(option["value"])]
    if on:
        ticks["initial_options"] = on
    from uninotifier import updates
    version = updates._out("rev-parse", "--short", "HEAD") if updates.is_clone() else None
    return {"type": "modal", "callback_id": VIEW_ID, "title": _plain("⚙️ Settings"),
            "submit": _plain("Save"), "close": _plain("Cancel"),
            "private_metadata": '{"panel": true}' if panel else "{}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    "*What UniNotifier does for you on its own*\nTick what you want — *Save* applies it at once."}},
                {"type": "divider"},
                {"type": "input", "block_id": "auto", "optional": True, "label": _plain("Automatic actions"),
                 "element": ticks},
                {"type": "context", "elements": [{"type": "mrkdwn", "text":
                    "Assignments, quizzes and forums are never touched. Your attendance is always your instructor's."}]},
                {"type": "divider"},
                {"type": "section", "block_id": "updates",
                 "text": {"type": "mrkdwn", "text": "*🆕 Updates*\nIs a new version of UniNotifier ready?"},
                 "accessory": {"type": "button", "action_id": "set_update", "value": "set:update",
                               "text": _plain("Check for Updates")}},
                *([{"type": "context", "elements": [{"type": "mrkdwn", "text": f"This copy: `{version}`"}]}]
                  if version else []),
            ]}


def save(chosen: set[str]) -> str:
    """The window's Save: every switch as ticked. "" when saved, else what to tell (shown under the tick boxes,
    nothing changed)."""
    if "AUTO_CHECK_IN" in chosen and not is_on("AUTO_CHECK_IN") and not (
            envcheck.value("PORTAL_USERNAME") and envcheck.value("PORTAL_PASSWORD")):
        return "Auto Check-In needs the portal login (PORTAL_USERNAME, PORTAL_PASSWORD) — run the setup to add it."
    for name, _, _ in SWITCHES:
        want = name in chosen
        if want != is_on(name):
            problem = switch(name, want)
            if problem.startswith("⚠️ The settings file"):
                return "The settings file could not be saved — nothing was changed."
    return ""


def saved_line() -> str:
    """The quiet confirmation after Save (shown to you alone, for a moment)."""
    return "✅ *Settings saved* — " + " · ".join(f"{label}: *{'On' if is_on(name) else 'Off'}*"
                                                for name, label, _ in SWITCHES)


def updates_window(text: str, rows: list) -> dict:
    """🆕 Updates as a second window over Settings: what is new and ⬇️ Update Now / ⏰ Later (no way back buttons —
    the window's own Back closes it)."""
    from uninotifier import slack
    rows = [row for row in rows if not any(button.get("callback_data") == "menu" for button in row)]
    blocks = slack.blocks(text, {"inline_keyboard": rows} if rows else None, live=True) or [
        {"type": "section", "text": {"type": "mrkdwn", "text": slack.mrkdwn(text)}}]
    return {"type": "modal", "title": _plain("🆕 Updates"), "close": _plain("Back"), "blocks": blocks[:100]}


def waiting_window(words: str) -> dict:
    return {"type": "modal", "title": _plain("🆕 Updates"), "close": _plain("Back"),
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": words}}]}


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
