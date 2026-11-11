"""
🗓️ Registration Plan in the chat (under 📜 Academic Record & Balance, and /register): what to
register next term, from the study plan the last transcript read brought (registration.py).

Two ways to a term's courses: ✨ the suggestion for the hours you choose, and ✋ your own choice
among the courses open to you (it starts from the suggestion when there is one).

Nothing is read from the portal here and nothing is registered: the screens are worked out from
the saved plan. Sections, days and registering itself belong to the portal's registration page,
which only exists while the university has registration open.
"""

import logging
import time

from uninotifier import media
from uninotifier import notifier
from uninotifier import registration
from uninotifier.bot import chat, portal_flow

log = logging.getLogger("uninotifier")

_kind: str | None = None            # the term kind chosen with 🔁; None = the term after this one
_chosen: list[str] = []             # ✋ the course codes of your own choice (in memory: gone on a restart)

HOME_ROW = [{"text": "🔙 Registration Plan", "callback_data": "reg:home"},
            {"text": "🏠 Main Menu", "callback_data": "menu"}]
SCREEN_LIMIT = 3900                 # a longer list goes out as a message of its own
OLD_PLAN_SECONDS = 24 * 3600        # a plan read longer ago than this is said to be worth refreshing
BUTTON_NAME = 34                    # characters of a course name on its button


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


def read_line(at: float, now: float | None = None) -> str:
    """When the plan these screens are worked out from was read from the portal."""
    line = f"🕒 *Your plan as read from the portal:* {notifier.audit_timestamp(at)}"
    if (time.time() if now is None else now) - at >= OLD_PLAN_SECONDS:
        line += "\n_A grade came out since? Tap 🔄 Update My Plan._"
    return line


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
        lines += ["", "✨ *Suggest my courses* — choose the hours:"]
        rows.append([{"text": text, "callback_data": f"reg:h:{hours:g}"} for hours, text in registration.options(advice)])
        rows.append([{"text": "✋ Choose Courses Myself", "callback_data": "reg:pick"}])
        rows.append([{"text": "✅ What I Can Take", "callback_data": "reg:open"},
                     {"text": "⛔ Not Yet — Why", "callback_data": "reg:held"}])
    other = registration.SUMMER if kind == registration.REGULAR else registration.REGULAR
    rows.append([{"text": "🔁 Plan the Summer Term Instead" if other == registration.SUMMER
                  else "🔁 Plan the Regular Term Instead", "callback_data": f"reg:k:{other}"},
                 {"text": "🔄 Update My Plan", "callback_data": "portal:transcript"}])
    lines += ["", "🔒 Sections, days and registering from here open with the portal's registration period.",
              read_line(at)]
    return "\n".join(lines), rows + [chat.RECORD_ROW]


def _short(name: str) -> str:
    return name if len(name) <= BUTTON_NAME else name[:BUTTON_NAME - 1].rstrip() + "…"


def view_pick(advice: dict, term: str) -> tuple[str, list]:
    """✋ Your own choice: every open course is a button that adds it or takes it out."""
    open_entries = [e for e in advice["entries"] if e["state"] != "blocked"]
    codes = {e["course"]["code"] for e in open_entries}
    _chosen[:] = [code for code in _chosen if code in codes]          # a plan that changed: only what is still open
    rows = [[{"text": f"{'✅' if e['course']['code'] in _chosen else '▫️'} {_short(e['course']['name'])} · "
                      f"{e['course']['hours']:g}h", "callback_data": f"reg:t:{e['course']['code']}"}]
            for e in open_entries]
    if _chosen:
        rows.append([{"text": "🧹 Clear My Choice", "callback_data": "reg:clear"}])
    return "\n".join(registration.choice_lines(advice, _chosen, term)), rows + [HOME_ROW]


def view_screen(what: str) -> tuple[str, list]:
    """A screen behind the home one: "h:<hours>" (the suggestion), "pick" / "t:<code>" / "clear"
    (your own choice), "open" or "held"."""
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
        _chosen[:] = [e["course"]["code"] for e in registration.propose(advice, hours)["entries"]]
        return ("\n".join(registration.proposal_lines(advice, hours, term)),
                [[{"text": "✋ Change These Courses", "callback_data": "reg:pick"}], HOME_ROW])
    if what.startswith("t:"):
        code = what[2:]
        _chosen.remove(code) if code in _chosen else _chosen.append(code)
        return view_pick(advice, term)
    if what == "clear":
        _chosen.clear()
        return view_pick(advice, term)
    if what == "pick":
        return view_pick(advice, term)
    lines = registration.open_lines(advice) if what == "open" else registration.blocked_lines(advice)
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
    """reg:home · reg:k:<regular|summer> · reg:h:<hours> · reg:pick · reg:t:<code> · reg:clear ·
    reg:open · reg:held"""
    global _kind
    chat.answer(cb["id"])
    what = action.partition(":")[2]
    if chat.is_file_message(cb.get("message")):     # pressed under a file: the screen opens below it
        message_id = None
    if what.startswith("k:"):
        _kind = what[2:] if what[2:] in (registration.REGULAR, registration.SUMMER) else None
        _chosen.clear()
        what = "home"
    show(view_home() if what == "home" else view_screen(what), message_id)


def start() -> None:
    """/register"""
    chat.send_view(*view_home())
