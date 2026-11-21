"""
🎯 Grade Needed as Slack windows — numbers typed in a form with their own fields and their own checks, not as chat
messages:

  🎯 Grade Needed        which average (semester or cumulative) and the target: a pick from 70–90 or your own value
  📘 a course's marks    final grade, coursework out of, and a retake with its previous grade — one window
  🎓 CGPA and hours      the record the calculator starts from

A wrong value is said under its field and the window stays open. Save redraws the screen it was opened from.
"""

import logging
from collections.abc import Callable

from uninotifier import gpa, gpa_ui, media
from uninotifier.bot import chat, winkit
from uninotifier.gpa import fmt

log = logging.getLogger("uninotifier")

TARGET, COURSE, RECORD = "gpa_target", "gpa_course", "gpa_record"
PRESETS = gpa_ui.PRESETS
RETAKE_WORDS = "the new grade replaces the old one; the hours are not counted again"


def course_names() -> list[str]:
    return [media.clean_course_name(c["name"]) for c in chat.load_state().get("courses", {}).values()]


def _option(label: str, value: str) -> dict:
    return {"text": winkit.plain(label), "value": value}


def _select(block_id: str, label: str, options: list[dict], initial: str | None, optional: bool = False) -> dict:
    element = {"type": "static_select", "action_id": block_id, "options": options,
               "placeholder": winkit.plain("Choose")}
    chosen = next((o for o in options if o["value"] == initial), None)
    if chosen:
        element["initial_option"] = chosen
    return {"type": "input", "block_id": block_id, "optional": optional, "label": winkit.plain(label),
            "element": element}


def target_window(message_id: int | None) -> dict:
    scopes = [_option("📊 Semester GPA — this term's average", "s"),
              _option("🎓 Cumulative CGPA — my record after this term", "c")]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text":
                "*What are you aiming for?* Pick the average and the target — I'll show the mark every course still needs."}},
              _select("scope", "Aim for", scopes, "s"),
              _select("preset", "Target", [_option(f"{t}%", str(t)) for t in PRESETS], "80", optional=True),
              winkit.text_field("custom", "…or your own value (0–100)", hint="e.g. 77.5", optional=True),
              {"type": "context", "elements": [{"type": "mrkdwn", "text": "A value typed here is used instead of the pick."}]}]
    return winkit.modal(TARGET, "🎯 Grade Needed", "Show Plan", blocks, {"message_id": message_id})


def course_window(index: int, message_id: int | None) -> dict | None:
    state = gpa.load()
    order = gpa.sync_courses(state, course_names())
    if not 0 <= index < len(order):
        return None
    name = order[index]
    c = state["courses"][order[index]]
    work = gpa.coursework(c)
    retake = winkit.choice("🔁 This is a retake", "yes", RETAKE_WORDS)
    ticks = {"type": "checkboxes", "action_id": "retake", "options": [retake]}
    if c["retake"]:
        ticks["initial_options"] = [retake]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text":
                f"*📘 {name}*\nEmpty a field to clear it. Nothing changes until *Save*."}},
              winkit.text_field("grade", "Final grade (out of 100)", fmt(c["grade"]) if c["grade"] is not None else "",
                                hint="the course's mark, once you know it", optional=True),
              {"type": "divider"},
              winkit.text_field("got", "Coursework marks so far", fmt(work[0]) if work else "",
                                hint="midterm, quizzes, assignments — all together", optional=True),
              winkit.text_field("out_of", "…out of", fmt(work[1]) if work else "", hint="e.g. 60", optional=True),
              {"type": "context", "elements": [{"type": "mrkdwn", "text":
                "The final is the rest of the 100 — with these two I show the best mark you can still reach."}]},
              {"type": "divider"},
              {"type": "input", "block_id": "retake", "optional": True, "label": winkit.plain("Retake"), "element": ticks},
              winkit.text_field("old", "Previous grade (for a retake)",
                                fmt(c["old_grade"]) if c["old_grade"] is not None else "", hint="0–100", optional=True)]
    return winkit.modal(COURSE, "📘 Course Marks", "Save", blocks, {"index": index, "message_id": message_id})


def record_window(message_id: int | None) -> dict:
    state = gpa.load()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text":
                "*🎓 Your record so far.* Read from the portal whenever you open your transcript; what you enter "
                "here lasts until then. Every attempted course counts, failed ones included."}},
              winkit.text_field("cgpa", "Current CGPA (0–100)",
                                fmt(state["cgpa"]) if state.get("cgpa") is not None else "", hint="e.g. 71.26"),
              winkit.text_field("hours", "Hours in your record",
                                fmt(state["passed_hours"]) if state.get("passed_hours") is not None else "", hint="e.g. 72")]
    return winkit.modal(RECORD, "🎓 CGPA & Hours", "Save", blocks, {"message_id": message_id})


def opening(values: list[str], payload: dict, message_number: int | None) -> dict | None:
    """The window a Grade Needed press asks for, else None."""
    for value in values:
        parts = value.split(":")
        if parts[0] != "gpa" or len(parts) < 2:
            continue
        kind = parts[1]
        if kind == "need":
            return target_window(message_number)
        if kind == "cg":
            return record_window(message_number)
        if kind in ("g", "w", "m") and len(parts) > 2 and parts[2].isdigit():
            return course_window(int(parts[2]), message_number)
        if kind == "r" and len(parts) > 2 and parts[2].isdigit():
            index = int(parts[2])
            state = gpa.load()
            order = gpa.sync_courses(state, course_names())
            if 0 <= index < len(order) and not state["courses"][order[index]]["retake"]:
                return course_window(index, message_number)     # turning it ON asks for the previous grade: the window
    return None


def submit(payload: dict) -> tuple[dict, Callable[[], str | None] | None]:
    view = payload.get("view") or {}
    values, meta = winkit.values(view), winkit.meta(view)
    message_id = meta.get("message_id")
    kind = view.get("callback_id")
    try:
        if kind == TARGET:
            target = gpa.parse_number(values.get("custom") or values.get("preset") or "", what="target")
            scope = values.get("scope") or "s"
            return {}, lambda: show(message_id, gpa_ui.view_plan(course_names(), scope, target))
        if kind == RECORD:
            cgpa, hours = gpa.parse_cgpa(f"{values.get('cgpa', '')} {values.get('hours', '')}")
            return {}, lambda: record(cgpa, hours, message_id)
        return course_submit(values, meta)
    except gpa.InputError as e:
        return winkit.problem("custom" if values.get("custom") else "preset" if kind == TARGET else "cgpa", str(e)), None


def course_submit(values: dict, meta: dict) -> tuple[dict, Callable[[], str | None] | None]:
    """The course window's Save: checked first (so a wrong field is said under itself), applied after."""
    checked: dict = {}
    for field, what in (("grade", "grade"), ("old", "previous grade")):
        if values.get(field):
            try:
                checked[field] = gpa.parse_number(values[field], what=what)
            except gpa.InputError as e:
                return winkit.problem(field, str(e)), None
    if values.get("got") or values.get("out_of"):
        try:
            checked["work"] = gpa.parse_coursework(f"{values.get('got', '')}/{values.get('out_of', '')}")
        except gpa.InputError as e:
            return winkit.problem("out_of" if values.get("got") else "got", str(e)), None
    retake = "yes" in (values.get("retake") or [])
    if retake and "old" not in checked:
        return winkit.problem("old", "A retake needs the previous grade."), None
    return {}, lambda: apply_course(meta, checked, retake)


def apply_course(meta: dict, checked: dict, retake: bool) -> str:
    state = gpa.load()
    order = gpa.sync_courses(state, course_names())
    index = meta.get("index", -1)
    if not 0 <= index < len(order):
        show(meta.get("message_id"), gpa_ui.view_home(course_names()))
        return "The course list changed — nothing was saved."
    c = state["courses"][order[index]]
    c["grade"] = checked.get("grade")
    if "work" in checked:
        gpa.set_coursework(c, *checked["work"])
    else:
        gpa.clear_coursework(c)
    c["retake"], c["old_grade"] = retake, (checked.get("old") if retake else None)
    gpa.save(state)
    show(meta.get("message_id"), gpa_ui.view_course(course_names(), index))
    return f"✅ *{order[index]}* saved"


def record(cgpa: float, hours: float, message_id: int | None) -> str:
    state = gpa.load()
    state["cgpa"], state["passed_hours"], state["base_source"] = cgpa, hours, "entered"
    gpa.save(state)
    show(message_id, gpa_ui.view_home(course_names()))
    return f"✅ Record saved — CGPA {fmt(cgpa)} over {fmt(hours)} hours"


def show(message_id: int | None, screen: tuple[str, list]) -> None:
    """The result goes where the window was opened from: that screen redrawn, or a new one."""
    if message_id:
        chat.edit_view(message_id, *screen)
    else:
        chat.send_view(*screen)
