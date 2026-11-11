"""
🗓️ Registration Plan in the chat (under 📜 Academic Record & Balance, and /register): what to
register next term, from the study plan the last transcript read brought (registration.py).

Nothing is read from the portal here and nothing is registered: the screens are worked out from
the saved plan. Sections, days and registering itself belong to the portal's registration page,
which only exists while the university has registration open.
"""

import logging

from uninotifier import media
from uninotifier import notifier
from uninotifier import registration
from uninotifier.bot import chat, portal_flow

log = logging.getLogger("uninotifier")

_kind: str | None = None            # the term kind chosen with 🔁; None = the term after this one

HOME_ROW = [{"text": "🔙 Registration Plan", "callback_data": "reg:home"},
            {"text": "🏠 Main Menu", "callback_data": "menu"}]
SCREEN_LIMIT = 3900                 # a longer list goes out as a message of its own


def saved_plan() -> tuple[dict, list[dict], float] | None:
    """(plan, semesters, when it was read) from the last transcript; None when there is no plan yet
    or it was read before prerequisites were kept."""
    cached = portal_flow.load_cached("transcript")
    if not cached:
        return None
    data = cached[0].data or {}
    plan = data.get("plan")
    if not plan or any("prereqs" not in c for cat in plan["categories"] for c in cat["courses"]):
        return None
    return plan, data.get("semesters") or [], cached[1]


def term_for(semesters: list[dict]) -> tuple[str, str]:
    """The term being planned and its kind: the one after the transcript's newest, or the one chosen."""
    term, kind = registration.next_term(registration.latest_term(semesters))
    if _kind and _kind != kind:
        return registration.other_term(term, kind)
    return term, kind


def view_missing() -> tuple[str, list]:
    return ("🗓️ *Registration Plan*\n\nI need your study plan with its prerequisites first — open "
            "📜 Transcript once, then come back here.",
            [[{"text": "📜 Transcript", "callback_data": "portal:transcript"}], chat.RECORD_ROW])


def view_home() -> tuple[str, list]:
    saved = saved_plan()
    if not saved:
        return view_missing()
    plan, semesters, at = saved
    term, kind = term_for(semesters)
    advice = registration.advise(plan, kind)
    lines = registration.overview_lines(advice, term)
    lines.insert(1, media.DIVIDER)
    rows = []
    if advice["left"]:
        lines += ["", "*How many hours do you want to register?*"]
        rows.append([{"text": text, "callback_data": f"reg:h:{hours:g}"} for hours, text in registration.options(advice)])
        rows.append([{"text": "✅ What I Can Take", "callback_data": "reg:open"},
                     {"text": "⛔ Not Yet — Why", "callback_data": "reg:held"}])
    other = registration.SUMMER if kind == registration.REGULAR else registration.REGULAR
    rows.append([{"text": "🔁 Plan the Summer Term Instead" if other == registration.SUMMER
                  else "🔁 Plan the Regular Term Instead", "callback_data": f"reg:k:{other}"}])
    lines += ["", "🔒 Sections, days and registering from here open with the portal's registration period.",
              portal_flow.stamp(at, "open 📜 Transcript to refresh")]
    return "\n".join(lines), rows + [chat.RECORD_ROW]


def view_screen(what: str) -> tuple[str, list]:
    """A screen behind the home one: "h:<hours>", "open" or "held"."""
    saved = saved_plan()
    if not saved:
        return view_missing()
    plan, semesters, _ = saved
    term, kind = term_for(semesters)
    advice = registration.advise(plan, kind)
    if what.startswith("h:"):
        try:
            hours = float(what[2:])
        except ValueError:
            return view_home()
        lines = registration.proposal_lines(advice, hours, term)
    elif what == "open":
        lines = registration.open_lines(advice)
    else:
        lines = registration.blocked_lines(advice)
    return "\n".join(lines), [HOME_ROW]


def show(view: tuple[str, list], message_id: int | None) -> None:
    text, rows = view
    if len(text) > SCREEN_LIMIT:                    # a long plan: the list is its own message, with the way back
        notifier.send_long_message(chat.BOT_TOKEN, chat.CHAT_ID, text, rows=rows)
        chat.leave_menu()
    elif message_id:
        chat.edit_view(message_id, text, rows)
    else:
        chat.send_view(text, rows)


def handle_callback(cb: dict, action: str, message_id: int | None) -> None:
    """reg:home · reg:k:<regular|summer> · reg:h:<hours> · reg:open · reg:held"""
    global _kind
    chat.answer(cb["id"])
    what = action.partition(":")[2]
    if chat.is_file_message(cb.get("message")):     # pressed under a file: the screen opens below it
        message_id = None
    if what.startswith("k:"):
        _kind = what[2:] if what[2:] in (registration.REGULAR, registration.SUMMER) else None
        what = "home"
    show(view_home() if what == "home" else view_screen(what), message_id)


def start() -> None:
    """/register"""
    chat.send_view(*view_home())
