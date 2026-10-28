"""
Your full academic record, and the semester / cumulative figures computed from
it — instead of trusting the portal's cumulative-average field, which updates
late after results are released.

How ZUJ's record works (checked against the portal: this reproduces its
cumulative average at every semester of the transcript to rounding):

  * every attempted course counts, passed or failed, with its credit hours;
  * a mark below 35 is recorded as 35 — the university's floor (its "zero");
  * a retaken course is replaced: once the new attempt has a mark, the old one
    leaves the record, and the hours are not counted twice;
  * cumulative average = sum(recorded mark x hours) / sum(hours) over the
    counted attempts; semester average is the same over that semester's courses.

Pure functions over portal.parse_transcript()'s semester list (newest first).
"""

import re

from uninotifier import gpa

FLOOR = 35.0


def recorded(mark: float) -> float:
    """The mark as the university records it."""
    return max(mark, FLOOR)


def _name_key(name: str) -> str:
    return re.sub(r"[\W_]+", "", name or "").lower()


def _same_course(a: dict, b: dict) -> bool:
    """A retake keeps the course's code or at least its name (codes change when
    a study plan is revised)."""
    return bool(a["code"] and a["code"] == b["code"]) or (
        bool(_name_key(a["name"])) and _name_key(a["name"]) == _name_key(b["name"]))


def attempts(semesters: list[dict], overlay: dict[str, float] | None = None) -> list[dict]:
    """Every graded attempt, newest semester first. `overlay` ({course code: mark})
    supplies marks the transcript doesn't show yet for the current semester
    (the first block), e.g. read from the results page."""
    out = []
    for index, sem in enumerate(semesters):
        for course in sem["courses"]:
            mark = course["mark"]
            if mark is None and index == 0 and overlay:
                mark = overlay.get(course["code"])
            if mark is None:
                continue
            out.append({"term": sem["term"], "index": index, "code": course["code"], "name": course["name"],
                        "hours": course["hours"], "mark": mark, "grade": recorded(mark)})
    return out


def counted(all_attempts: list[dict]) -> list[dict]:
    """The attempts that count: for each course, only its newest one."""
    kept: list[dict] = []
    for attempt in sorted(all_attempts, key=lambda a: a["index"]):       # newest first
        if not any(_same_course(attempt, k) for k in kept):
            kept.append(attempt)
    return kept


def cumulative(all_attempts: list[dict]) -> tuple[float | None, float]:
    """(cumulative average, hours it covers) over the counted attempts."""
    rows = counted(all_attempts)
    hours = sum(a["hours"] for a in rows)
    return (sum(a["grade"] * a["hours"] for a in rows) / hours if hours else None), hours


def current_roster(semesters: list[dict]) -> list[dict]:
    """The courses of the semester in progress: the newest block."""
    return list(semesters[0]["courses"]) if semesters else []


def season_report(semesters: list[dict], overlay: dict[str, float]) -> dict:
    """Everything the results message needs once the current semester's marks
    are in. {"term", "courses": [{code, name, hours, mark, grade, passed,
    replaces}], "complete", "published", "total", "semester_average",
    "before": (cgpa, hours) of the record without this semester, "after":
    (cgpa, hours) with it, "replaced": [(code, name, old mark, new mark)], "floored"}."""
    roster = current_roster(semesters)
    history = [a for a in attempts(semesters[1:])]
    for a in history:
        a["index"] += 1                       # keep indexes relative to the full list
    current = []
    for course in roster:
        mark = course["mark"] if course["mark"] is not None else overlay.get(course["code"])
        current.append({**course, "mark": mark})

    rows, replaced, floored = [], [], []
    for course in current:
        row = {**course, "grade": recorded(course["mark"]) if course["mark"] is not None else None,
               "passed": None if course["mark"] is None else course["mark"] >= gpa.PASS_MARK, "replaces": None}
        old = next((a for a in sorted(history, key=lambda a: a["index"]) if _same_course(a, course)), None)
        if old is not None:
            row["replaces"] = old["grade"]
            if row["grade"] is not None:
                replaced.append((course["code"], course["name"], old["grade"], row["grade"]))
        if course["mark"] is not None and course["mark"] < FLOOR:
            floored.append((course["name"], course["mark"]))
        rows.append(row)

    published = [r for r in rows if r["grade"] is not None]
    hours = sum(r["hours"] for r in published)
    average = sum(r["grade"] * r["hours"] for r in published) / hours if hours else None

    now_attempts = history + [{"index": 0, "code": r["code"], "name": r["name"], "hours": r["hours"],
                               "mark": r["mark"], "grade": r["grade"], "term": semesters[0]["term"]}
                              for r in published]
    return {"term": semesters[0]["term"] if semesters else "", "courses": rows,
            "complete": bool(rows) and len(published) == len(rows),
            "published": len(published), "total": len(rows), "semester_average": average,
            "before": cumulative(history), "after": cumulative(now_attempts),
            "replaced": replaced, "floored": floored}
