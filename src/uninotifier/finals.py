"""
Finals-season watcher: tells you when each course's grade is released on the
registration portal, and — once every course of the semester has one — reports
the semester average and your new cumulative GPA.

This is the one automatic use of the portal (everything else there is
on-demand), so it is opt-in and bounded:
  * it runs only while "finals mode" is on — `/finals on`, or automatically
    between the dates of an optional "finals" window in schedule.json:
        "finals": {"from": "2027-01-04", "to": "2027-01-31", "interval_minutes": 30}
  * it checks every `interval_minutes` (default 30), one sign-in per check;
  * once every course has its grade it pauses itself (⏸️ Finals Watch Paused)
    and stops contacting the portal — `/finals on` resumes it, and a new
    schedule.json window (a new "from" date) arms it again next season.

What it reports:
  🎓 Grade Released      one message per course, the moment its mark appears
                         (or ✏️ Grade Updated if the portal changes a mark)
  🏁 Semester Results    when all courses have a mark: semester average and the
                         new cumulative GPA, computed from your full record
                         (gradebook.py) — not from the portal's cumulative field,
                         which updates late. Marks under 35 count as 35, a retaken
                         course replaces its old attempt.

State lives in finals.json (git-ignored). The first check of a term only
reports how many grades are already out; later ones announce what's new.
"""

import json
import logging
import time
from datetime import date, datetime

from uninotifier import fileio
from uninotifier import gpa
from uninotifier import gradebook
from uninotifier import notifier, paths
from uninotifier import portal
from uninotifier import schedule
from uninotifier.gpa import fmt
from uninotifier.notifier import escape_md

log = logging.getLogger("uninotifier")

STATE_FILE = paths.DATA / "finals.json"
DEFAULT_INTERVAL_MINUTES = 30
FAILURE_NOTICE_SECONDS = 6 * 3600
DIVIDER = "━━━━━━━━━━━━━━━━━━"


# ------------------------------------------------------------------- state

def load() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("enabled", False)
    for key, default in (("term", ""), ("seen", {}), ("baselined", False), ("reported", False),
                         ("last_check", 0), ("published", 0), ("total", 0), ("failure_notice", 0), ("code_notice", 0),
                         ("complete", False), ("paused_for", None), ("courses", [])):
        state.setdefault(key, default)
    return state


def save(state: dict) -> None:
    fileio.write_json_atomic(STATE_FILE, state)


def window(sch: dict | None = None) -> dict:
    return (sch or schedule.load_schedule()).get("finals") or {}


def in_window(today: date, sch: dict | None = None) -> bool:
    w = window(sch)
    try:
        return date.fromisoformat(w["from"]) <= today <= date.fromisoformat(w["to"])
    except (KeyError, ValueError):
        return False


def active(now: datetime | None = None, state: dict | None = None, sch: dict | None = None) -> bool:
    """Finals mode on: switched on by hand, or inside the schedule.json window
    — unless this window's grades are all out (paused_for is that window)."""
    now = now or datetime.now()
    state = state or load()
    if state["enabled"]:
        return True
    return in_window(now.date(), sch) and state["paused_for"] != window(sch).get("from")


def disarm(state: dict, sch: dict | None = None) -> None:
    """Stop polling: every grade of the term is published."""
    state["enabled"] = False
    state["paused_for"] = window(sch).get("from")


def interval_seconds(sch: dict | None = None) -> int:
    return int(window(sch).get("interval_minutes", DEFAULT_INTERVAL_MINUTES)) * 60


def due(now: datetime | None = None, state: dict | None = None, sch: dict | None = None) -> bool:
    state = state or load()
    now = now or datetime.now()
    return active(now, state, sch) and now.timestamp() - state["last_check"] >= interval_seconds(sch)


# --------------------------------------------------------------- messages

def display_name(code: str, portal_name: str) -> str:
    """The course's English name (schedule.json "course_codes"), else as the portal has it."""
    return schedule.load_schedule().get("course_codes", {}).get(code) or portal_name


def _released(course: dict, report: dict, previous: float | None) -> str:
    mark = course["mark"]
    passed = mark >= gpa.PASS_MARK
    lines = [
        "🎓 *Grade Updated*" if previous is not None else "🎓 *Grade Released*", "",
        f"📘 *Course:* {escape_md(display_name(course['code'], course['name']))}",
        f"🔢 *Code:* {escape_md(course['code'])}",
        f"🎯 *Mark:* {fmt(mark)}" + (f"  (was {fmt(previous)})" if previous is not None else "")
        + ("  ✅ Passed" if passed else f"  ❌ Below the {fmt(gpa.PASS_MARK)} pass mark"),
    ]
    if mark < gradebook.FLOOR:
        lines.append(f"ℹ️ Recorded as {fmt(gradebook.FLOOR)}, the university's minimum.")
    if course.get("replaces") is not None:
        lines.append(f"🔁 *Retake:* replaces your earlier {fmt(course['replaces'])}")
    lines += [f"📊 *Progress:* {report['published']} of {report['total']} grades published",
              f"🕒 *Detected:* {notifier.audit_timestamp()}"]
    return "\n".join(lines)


def results_message(report: dict, portal_cgpa: float | None) -> str:
    """The semester average and the new cumulative GPA, from the full record."""
    lines = [f"🏁 *Semester Results — {escape_md(report['term'])}*", DIVIDER]
    for n, c in enumerate(sorted(report["courses"], key=lambda c: c["code"]), 1):
        name = escape_md(display_name(c["code"], c["name"]))
        lines.append(f"{n}. 📘 *{name}* · {fmt(c['hours'])}h · {fmt(c['mark'])} "
                     + ("✅" if c["passed"] else "❌") + (f" · 🔁 was {fmt(c['replaces'])}" if c["replaces"] is not None else ""))
    lines.append(DIVIDER)

    average = report["semester_average"]
    after, hours_after = report["after"]
    before, hours_before = report["before"]
    lines.append(f"📊 *Semester Average:* {fmt(average)}%")
    if after is not None:
        change = f"  ({'+' if after >= before else '−'}{fmt(abs(after - before))})" if before is not None else ""
        lines.append(f"🎓 *New Cumulative GPA:* {fmt(after)}%{change}")
        lines.append(f"📚 *Hours in your record:* {fmt(hours_after)}"
                     + (f"  (was {fmt(hours_before)} — retakes aren't counted twice)" if hours_after != hours_before else ""))
    failed = [display_name(c["code"], c["name"]) for c in report["courses"] if not c["passed"]]
    if failed:
        lines.append(f"❌ *Below the pass mark:* {escape_md(', '.join(failed))}")
    if report["replaced"]:
        lines.append("🔁 *Retakes replaced:* " + escape_md("; ".join(
            f"{display_name(code, name)} {fmt(old)} → {fmt(new)}" for code, name, old, new in report["replaced"])))
    if report["floored"]:
        lines.append("ℹ️ Marks under 35 are recorded as 35, the university's minimum.")
    note = "Computed from your full course record"
    if portal_cgpa is not None:
        note += f"; the portal's own field still shows {fmt(portal_cgpa)} and updates later"
    lines += ["", f"_{note}._"]
    return "\n".join(lines)


def paused_message(state: dict) -> dict:
    return {"text": "\n".join([
        "⏸️ *Finals Watch Paused*", "",
        f"All {state['total']} grades for {escape_md(state['term'])} are published, so I've stopped "
        "checking the portal.",
        "A late correction won't be noticed while paused — tap Resume (or send /finals on) to keep watching."]),
        "rows": [[{"text": "▶️ Resume Finals Watch", "callback_data": "finals:on"}]]}


def started_message(report: dict) -> str:
    return "\n".join([
        "🏁 *Finals Watch*", "",
        f"📅 *Term:* {escape_md(report['term'])}",
        f"📊 *Grades published:* {report['published']} of {report['total']}",
        "", "I'll message you the moment each grade appears, and once all are out I'll report your "
        "semester average and new cumulative GPA."])


# ------------------------------------------------------------------ logic

def marks_overlay(grades: portal.Result | None, semesters: list[dict]) -> dict[str, float]:
    """{course code: mark} for the semester in progress: the results page is
    the primary source; the transcript's current block fills in what it lacks."""
    marks = {c["code"]: c["mark"] for c in (semesters[0]["courses"] if semesters else [])
             if c["mark"] is not None}
    for row in (grades.data.get("results", []) if grades else []):
        if row["mark"] is not None:
            marks[row["code"]] = row["mark"]
    return marks


def evaluate(state: dict, grades: portal.Result | None, transcript: portal.Result) -> list[dict]:
    """What to tell the user after one reading of the portal: a list of
    {"text", "rows"} messages. Updates `state` (the caller saves it)."""
    semesters = (transcript.data or {}).get("semesters") or []
    if not semesters or not semesters[0]["courses"]:
        raise portal.PortalError("The transcript didn't list this semester's courses.")
    overlay = marks_overlay(grades, semesters)
    report = gradebook.season_report(semesters, overlay)
    current = {c["code"]: c["mark"] for c in report["courses"] if c["mark"] is not None}

    if state["term"] != report["term"]:                    # a new term: start over
        state.update(term=report["term"], seen={}, baselined=False, reported=False)
    state["courses"] = [{"code": c["code"], "name": c["name"], "hours": c["hours"], "mark": c["mark"]}
                        for c in sorted(report["courses"], key=lambda c: c["code"])]
    state.update(published=report["published"], total=report["total"], complete=report["complete"],
                 base=list(report["before"]),           # the CGPA base without this term (bot syncs it)
                 retakes={c["code"]: c["replaces"] for c in report["courses"] if c["replaces"] is not None})
    messages = []

    first = not state["baselined"]
    if first:
        state.update(baselined=True, seen=current, reported=report["complete"])
        messages.append({"text": started_message(report), "rows": []})
        if not report["complete"]:
            return messages                                # nothing more to say until a grade appears

    for course in sorted(report["courses"], key=lambda c: c["code"]):
        mark = course["mark"]
        if mark is None or state["seen"].get(course["code"]) == mark:
            continue
        previous = state["seen"].get(course["code"])
        messages.append({"text": _released(course, report, previous), "rows": []})
        state["seen"][course["code"]] = mark
        state["reported"] = False                          # a new or changed mark: summarise again

    if report["complete"] and (first or not state["reported"]):
        state["reported"] = True
        rows = [[{"text": "📊 GPA & Grade Hub", "callback_data": "academic"}]]
        messages.append({"text": results_message(report, transcript.figures.get("cgpa")), "rows": rows})
    return messages


def check(fetch, state: dict | None = None, sch: dict | None = None) -> tuple[list[dict], dict]:
    """One reading of the portal. `fetch(sections)` returns {section: Result}
    (portal.fetch_many with a code handler). Returns (messages, state); the
    state is saved here, so a failed reading leaves it as it was. Grades are
    compared with the last reading kept in finals.json ("seen"); when the
    term is complete, polling pauses itself."""
    state = state or load()
    results = fetch(["grades", "transcript"])
    messages = evaluate(state, results.get("grades"), results["transcript"])
    if state["complete"] and active(state=state, sch=sch):
        disarm(state, sch)
        messages.append(paused_message(state))
    state["last_check"] = time.time()
    state["failure_notice"] = 0
    save(state)
    return messages, state


def grades_lines(state: dict) -> list[str]:
    """This term's grades as the last reading found them: each course with its mark, or ⏳ while
    it is not published."""
    courses = state.get("courses") or []
    if not state["term"] or not courses:
        return ["📊 *This term's grades* — not read yet. Tap 🔄 Check Grades Now."]
    lines = [f"📊 *This term's grades* — {state['published']} of {state['total']} published"]
    for c in courses:
        name = escape_md(display_name(c["code"], c["name"]))
        if c["mark"] is None:
            lines.append(f"• ⏳ {name} — not published yet")
        else:
            lines.append(f"• {'✅' if c['mark'] >= gpa.PASS_MARK else '❌'} *{fmt(c['mark'])}* · {name}")
    return lines


def status_text(state: dict | None = None, sch: dict | None = None) -> str:
    state = state or load()
    on = active(state=state, sch=sch)
    lines = ["🏁 *Finals Watch*", DIVIDER,
             f"{'✅ *On*' if on else '⏸️ *Off*'} — " + (
                 f"checks the portal every {interval_seconds(sch) // 60} min" if on else
                 "the portal is only contacted when you ask")]
    if not on and state["complete"] and state["term"]:
        lines.append("✅ Every grade of the term is published — paused automatically")
    w = window(sch)
    if w.get("from") and w.get("to"):
        lines.append(f"🗓️ Switches on by itself from {w['from']} to {w['to']}")
    if state["term"]:
        lines.append(f"📅 *Term:* {escape_md(state['term'])}")
    lines += ["", *grades_lines(state)]
    if state["last_check"]:
        lines += ["", f"🕒 *Last check:* {notifier.audit_timestamp(state['last_check'])}"]
    lines += ["", "While it's on: each released grade is announced, and when every course has one you get "
              "your semester average and new cumulative GPA."]
    return "\n".join(lines)
