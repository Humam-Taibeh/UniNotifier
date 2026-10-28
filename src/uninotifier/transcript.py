"""
The academic record as a message — pure text, no portal, no Telegram.

  summary_lines()  the executive summary: cumulative GPA, passed hours (and how many are in
                   progress or still to repeat), attempted hours, status — and, for a guest, the
                   major and the hours left
  blocks()         one block per semester, the one in progress first (⏳), then the others:
                   ✅ passed · ❌ failed · 🔁 replaced by a later retake · ⏳ in progress, each
                   with its mark and credit hours

Works on portal.parse_transcript()'s semester list (newest first). Course names are shown as the
portal publishes them (often Arabic); each is wrapped in a Unicode isolate and every line starts
with a left-to-right mark, so an Arabic name cannot reorder the mark and hours around it.
"""

from uninotifier import gpa, gradebook
from uninotifier.notifier import escape_md

LRM, FSI, PDI = "\u200e", "\u2068", "\u2069"
LEGEND = "_✅ passed · ❌ failed · 🔁 replaced by a later retake · ⏳ in progress_"


def isolated(text: str) -> str:
    """Text of its own direction that cannot reorder its neighbours."""
    return f"{FSI}{text}{PDI}" if text else text


def _hours(value: float) -> str:
    return f"{gpa.fmt(value)}h"


def standing(semesters: list[dict]) -> dict:
    """Where every course of the record stands, each counted once at its newest attempt: hours passed,
    in progress, failed and not retaken yet ("to repeat"), and how many attempts a later retake replaced."""
    kept: list[dict] = []
    retaken = 0
    for sem in semesters:                                   # newest first
        for course in sem["courses"]:
            if any(gradebook._same_course(course, k) for k in kept):
                retaken += 1                                # a newer attempt replaced this one
            else:
                retaken += 1 if course.get("repeated") else 0   # the portal says it was repeated
                kept.append(course)
    hours = lambda rows: sum(c["hours"] for c in rows)      # noqa: E731
    passed = [c for c in kept if c["passed"] is True]
    progress = [c for c in kept if c["passed"] is None]
    repeat = [c for c in kept if c["passed"] is False]
    return {"passed": hours(passed), "in_progress": hours(progress), "to_repeat": hours(repeat),
            "retaken": retaken, "courses_passed": len(passed)}


def summary_lines(semesters: list[dict], figures: dict, status: str | None = None,
                  major: str | None = None, remaining: float | None = None) -> list[str]:
    """The executive summary. `figures` is the transcript Result's (cgpa, record_cgpa, passed_hours,
    record_hours); `status` the English academic status; `major` / `remaining` only for a guest."""
    lines = []
    if major:
        lines.append(f"📘 *Major:* {isolated(escape_md(major))}")
    cgpa = figures.get("record_cgpa", figures.get("cgpa"))
    if cgpa is not None:
        shown = (f" (Portal display: {gpa.fmt(figures['cgpa'])}%)"
                 if "cgpa" in figures and "record_cgpa" in figures and figures["cgpa"] != figures["record_cgpa"] else "")
        lines.append(f"🎓 *Cumulative GPA:* {cgpa:.2f}%{shown}")
    state = standing(semesters) if semesters else None
    passed = figures.get("passed_hours", state["passed"] if state else None)
    if passed is not None:
        lines.append(f"⏱️ *Passed Hours:* {gpa.fmt(passed)} hrs")
        if state:
            parts = []
            if state["in_progress"]:
                parts.append(f"⏳ {_hours(state['in_progress'])} in progress")
            if state["to_repeat"]:
                parts.append(f"❌ {_hours(state['to_repeat'])} to repeat")
            if state["retaken"]:
                parts.append(f"🔁 {state['retaken']} retaken")
            if parts:
                lines.append("      └ " + " · ".join(parts))
    if "record_hours" in figures:
        lines.append(f"📚 *Total Attempted Hours:* {gpa.fmt(figures['record_hours'])} hrs")
    if remaining is not None:
        lines.append(f"🏁 *Remaining Hours:* {gpa.fmt(max(0.0, remaining))} hrs")
    if status:
        lines.append(f"📌 *Academic Status:* {escape_md(status)}")
    return lines


def course_line(course: dict, overlay: dict[str, float] | None = None) -> str:
    """One bullet: status, mark, name, hours."""
    mark = course["mark"]
    if mark is None and overlay:
        mark = overlay.get(course["code"])           # a mark the results page already shows for this term
    name = isolated(escape_md(course["name"] or course["code"]))
    if course["passed"] is None and mark is None:
        return f"{LRM}⏳ {name} · {_hours(course['hours'])}"
    shown = gpa.fmt(mark) if mark is not None else "—"
    if course.get("repeated"):
        emoji = "🔁"
    elif course["passed"] is None:
        emoji = "✅" if mark >= gpa.PASS_MARK else "❌"
    else:
        emoji = "✅" if course["passed"] else "❌"
    return f"{LRM}{emoji} *{shown}* · {name} · {_hours(course['hours'])}"


def _replaced(semesters: list[dict], index: int, course: dict) -> dict:
    """The course, marked as replaced when a NEWER semester holds another attempt of it (the portal's own
    "repeated" column is honoured too)."""
    later = any(gradebook._same_course(course, other) for sem in semesters[:index] for other in sem["courses"])
    return {**course, "repeated": bool(course.get("repeated")) or later}


def blocks(semesters: list[dict], overlay: dict[str, float] | None = None) -> list[str]:
    """Lines of the semester blocks (blank lines between them); empty semesters are skipped."""
    lines = []
    for index, sem in enumerate(semesters):
        if not sem["courses"]:
            continue
        if index == 0 and any(c["passed"] is None for c in sem["courses"]):
            lines.append(f"⏳ *{escape_md(sem['term'])}* · In Progress")
            marks = overlay
        else:
            stats = " · ".join(x for x in (
                f"average {gpa.fmt(sem['average'])}" if sem["average"] is not None else "",
                f"cumulative {gpa.fmt(sem['cumulative'])}" if sem["cumulative"] is not None else "") if x)
            lines.append(f"📅 *{escape_md(sem['term'])}*" + (f" · {stats}" if stats else ""))
            marks = None
        lines += [course_line(_replaced(semesters, index, c), marks) for c in sem["courses"]]
        lines.append("")
    if lines:
        lines.append(LEGEND)
    return lines
