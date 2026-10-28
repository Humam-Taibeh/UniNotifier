"""
Grade Needed and Course Adjustments in the chat: shows gpa_ui's screens and
routes a typed answer to the question that is waiting for it.
"""

import logging

from uninotifier import gpa_ui
from uninotifier import media
from uninotifier.bot import chat

log = logging.getLogger("uninotifier")


# ------------------------------------------------------------ GPA & portal

_pending_prompt: gpa_ui.Prompt | None = None   # a /gpa question waiting for a typed answer


def course_names() -> list[str]:
    return [media.clean_course_name(c["name"]) for c in chat.load_state().get("courses", {}).values()]


def show_gpa(result, message_id: int | None = None) -> None:
    """A calculator screen — edited into the tapped message when there is one —
    or a question, after which the next plain message is the answer."""
    global _pending_prompt
    if isinstance(result, gpa_ui.Prompt):
        _pending_prompt = result
        chat.send_view(result.text, [gpa_ui.CANCEL_ROW])
    elif message_id:
        chat.edit_view(message_id, *result)
    else:
        chat.send_view(*result)


def handle_gpa_callback(action: str, message_id: int | None) -> None:
    global _pending_prompt
    cancelled = action == "gpa:cancel"
    _pending_prompt = None
    if action.startswith("gpa:use:"):
        message_id = None        # pressed under a screenshot: a document's text can't be edited
    show_gpa(gpa_ui.view_home(course_names()) if cancelled
             else gpa_ui.on_callback(course_names(), action), message_id)


def handle_gpa_answer(text: str) -> None:
    global _pending_prompt
    result = gpa_ui.on_text(course_names(), _pending_prompt, text)
    if isinstance(result, str):
        chat.send_view(result, [gpa_ui.CANCEL_ROW])      # unusable answer: the question stays open
        return
    _pending_prompt = None
    chat.send_view(*result)


def handle_need(arg: str = "") -> None:
    """/need opens the button-driven Grade Needed flow (anything typed after it is ignored)."""
    chat.send_view(*gpa_ui.view_need_picker(course_names()))
