"""
The small pieces every Slack window (modal) is made of: its fields, and reading back what was filled in.

A window is a form over the chat: a press opens it at once (Slack's trigger lasts 3 seconds), Save sends what was
filled in WITH Slack's acknowledgement — so a wrong value is said inside the window, under the field, and nothing is
lost — and the screen behind it is redrawn. reminder_window.py and gpa_window.py are the windows; slackbot.py routes.
"""

import json


def plain(text: str) -> dict:
    return {"type": "plain_text", "text": text, "emoji": True}


def text_field(block_id: str, label: str, initial: str = "", *, hint: str = "", multiline: bool = False,
               optional: bool = False, limit: int | None = None) -> dict:
    """A box to type in (the block and its element share one id: the value is read back by it)."""
    element = {"type": "plain_text_input", "action_id": block_id, "multiline": multiline}
    if initial:
        element["initial_value"] = str(initial)[:limit or 3000]
    if hint:
        element["placeholder"] = plain(hint)
    if limit:
        element["max_length"] = limit
    return {"type": "input", "block_id": block_id, "optional": optional, "label": plain(label), "element": element}


def date_field(block_id: str, label: str, initial: str) -> dict:
    return {"type": "input", "block_id": block_id, "label": plain(label),
            "element": {"type": "datepicker", "action_id": block_id, "initial_date": initial}}


def time_field(block_id: str, label: str, initial: str) -> dict:
    return {"type": "input", "block_id": block_id, "label": plain(label),
            "element": {"type": "timepicker", "action_id": block_id, "initial_time": initial,
                        "placeholder": plain("Pick a time")}}


def choice(label: str, value: str, words: str = "") -> dict:
    option = {"text": {"type": "mrkdwn", "text": label}, "value": value}
    if words:
        option["description"] = {"type": "mrkdwn", "text": words}
    return option


def modal(callback_id: str, title: str, submit: str, blocks: list[dict], meta: dict | None = None) -> dict:
    """Slack shows a title of up to 24 characters and a button of up to 24."""
    return {"type": "modal", "callback_id": callback_id, "title": plain(title[:24]), "submit": plain(submit[:24]),
            "close": plain("Cancel"), "blocks": blocks, "private_metadata": json.dumps(meta or {}, ensure_ascii=False)}


def meta(view: dict) -> dict:
    try:
        found = json.loads(view.get("private_metadata") or "{}")
        return found if isinstance(found, dict) else {}
    except ValueError:
        return {}


def values(view: dict) -> dict:
    """What was filled in: {field id: its value} — text as typed, a date "YYYY-MM-DD", a time "HH:MM", a pick's value,
    the values of ticked boxes as a list; an empty field is "" (or [])."""
    out = {}
    for block, actions in ((view.get("state") or {}).get("values") or {}).items():
        for action in actions.values():
            kind = action.get("type")
            if kind == "plain_text_input":
                out[block] = (action.get("value") or "").strip()
            elif kind == "datepicker":
                out[block] = action.get("selected_date") or ""
            elif kind == "timepicker":
                out[block] = action.get("selected_time") or ""
            elif kind in ("static_select", "radio_buttons"):
                out[block] = (action.get("selected_option") or {}).get("value") or ""
            elif kind == "checkboxes":
                out[block] = [o.get("value") for o in action.get("selected_options") or []]
    return out


def problem(block_id: str, words: str) -> dict:
    """Slack's answer that keeps the window open with `words` under the field."""
    return {"response_action": "errors", "errors": {block_id: words}}
