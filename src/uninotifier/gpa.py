"""
ZUJ academic calculator: semester average, cumulative GPA, "what do I need
on the final", with the university's rules in one place.

ZUJ grades on a 100-point percentage scale (not 4.0):
  * a course is passed with at least PASS_MARK (50);
  * a course is 3 credit hours unless changed.

The hours in the CGPA are every attempted course, failed ones included (recorded at
35, the university's floor) — see gradebook.py, which reproduces the portal's figure.

  Semester average  = sum(grade * hours) / sum(hours)
  Cumulative GPA    = (CGPA * record hours + semester average * semester hours)
                      / (record hours + semester hours)
  Retake            = the new grade replaces the old one in the points and its
                      hours are not counted twice:
                      new points = old points - old grade * hours + new grade * hours

Pure functions plus a small JSON store (gpa.json, kept out of git: it holds
grades). Each course record:
  {"hours": 3, "grade": 85.0 | None, "retake": False, "old_grade": None,
   "syllabus": [["Midterm", 30.0], ["Final", 40.0], ...] | None,
   "marks": {"Midterm": 24.0}, "portal_grade": 81.0 | None}

portal_grade is the official mark once the portal publishes it (/grades,
Finals Watch): it is locked — it wins over anything typed — and plan()
spreads the rest of a target over the courses still without a grade.
"""

import re

from uninotifier import paths
from uninotifier import userdata

PASS_MARK = 50.0
DEFAULT_HOURS = 3
HOUR_CHOICES = (1, 2, 3)
STATE_FILE = paths.DATA / "gpa.json"
FINAL_RE = re.compile(r"final|نهائي|نهائى", re.I)


class InputError(ValueError):
    """A user-entered value that can't be used; the message is shown to them."""


def fmt(value: float | None, digits: int = 2) -> str:
    """85 -> '85', 81.5 -> '81.5', 74.2143 -> '74.21'."""
    if value is None:
        return "—"
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


# ------------------------------------------------------------------- store

def new_course() -> dict:
    return {"hours": DEFAULT_HOURS, "grade": None, "retake": False, "old_grade": None,
            "syllabus": None, "marks": {}, "portal_grade": None}


def load() -> dict:
    state = userdata.load(STATE_FILE, "your Grade Needed plan")
    state.setdefault("cgpa", None)
    state.setdefault("passed_hours", None)     # the hours behind cgpa: every attempted course
    state.setdefault("base_source", None)      # "portal" once synced from the transcript
    state.setdefault("base_synced", None)
    state.setdefault("courses", {})
    return state


def set_base(cgpa: float, hours: float, source: str = "portal", at: float | None = None) -> bool:
    """The CGPA base the targets build on: the record *before* this semester —
    every attempted course, failures at 35, a retake counted once (gradebook).
    Synced from the portal's transcript whenever it is read. Returns whether
    it changed."""
    import time
    state = load()
    changed = (state["cgpa"], state["passed_hours"], state["base_source"]) != (round(cgpa, 4), hours, source)
    state.update(cgpa=round(cgpa, 4), passed_hours=hours, base_source=source, base_synced=at or time.time())
    save(state)
    return changed


def save(state: dict) -> None:
    userdata.save(STATE_FILE, state)


def sync_courses(state: dict, names: list[str]) -> list[str]:
    """Make the semester's courses match the portal's (sorted by name), keeping
    what was entered for the ones that remain. With no names known yet, the
    stored list is kept. Returns the course names in display order."""
    if names:
        state["courses"] = {n: state["courses"].get(n) or new_course() for n in sorted(set(names))}
    return list(state["courses"])


# ------------------------------------------------------------ calculations

def course_grade(course: dict) -> float | None:
    """The grade to count: the portal's official mark, else the one entered by
    hand, else the sum of the marks once every syllabus component (final
    included) has one."""
    if course.get("portal_grade") is not None:
        return course["portal_grade"]
    if course.get("grade") is not None:
        return course["grade"]
    syllabus, marks = course.get("syllabus"), course.get("marks") or {}
    if syllabus and all(name in marks for name, _ in syllabus):
        return round(sum(marks[name] for name, _ in syllabus), 2)
    return None


def graded(courses: dict) -> list[tuple[str, dict, float]]:
    return [(n, c, g) for n, c in courses.items() if (g := course_grade(c)) is not None]


def semester_average(courses: dict) -> float | None:
    """Weighted average over the courses that have a grade."""
    rows = graded(courses)
    hours = sum(c["hours"] for _, c, _ in rows)
    return sum(g * c["hours"] for _, c, g in rows) / hours if hours else None


def cumulative(courses: dict, cgpa: float | None, passed_hours: float | None) -> float | None:
    """Projected CGPA. A retake takes its old grade out of the old points and
    adds no hours; every other graded course adds its points and hours.
    None when the current CGPA / passed hours aren't known (a student with 0
    passed hours needs no CGPA)."""
    if passed_hours is None or (cgpa is None and passed_hours):
        return None
    points = (cgpa or 0) * passed_hours
    hours = passed_hours
    for _, c, g in graded(courses):
        points += g * c["hours"]
        if c.get("retake") and c.get("old_grade") is not None:
            points -= c["old_grade"] * c["hours"]
        else:
            hours += c["hours"]
    return points / hours if hours else None


def needed_average(courses: dict, target: float) -> float | None:
    """The average the still-ungraded courses must reach for the whole
    semester to average `target`. None when nothing is left to grade."""
    done = graded(courses)
    rest = sum(c["hours"] for n, c in courses.items() if course_grade(c) is None)
    if not rest:
        return None
    total = sum(c["hours"] for c in courses.values())
    return (target * total - sum(g * c["hours"] for _, c, g in done)) / rest


# ------------------------------------------------------------ text parsing

NUMBER = r"-?\d+(?:\.\d+)?"


def parse_number(text: str, low: float = 0, high: float = 100, what: str = "value") -> float:
    match = re.fullmatch(rf"\s*({NUMBER})\s*%?\s*", text or "")
    if not match:
        raise InputError("Send just a number, e.g. 85.")
    value = float(match.group(1))
    if not low <= value <= high:
        raise InputError(f"The {what} must be between {fmt(low)} and {fmt(high)}.")
    return value


def parse_cgpa(text: str) -> tuple[float, float]:
    """'69.86 66' -> (CGPA 69.86, hours in the record 66)."""
    nums = re.findall(NUMBER, text or "")
    if len(nums) != 2:
        raise InputError("Send two numbers: your current CGPA, then the hours in your record — e.g. 69.86 66")
    cgpa, hours = float(nums[0]), float(nums[1])
    if not 0 <= cgpa <= 100:
        raise InputError("The CGPA must be between 0 and 100.")
    if not 0 <= hours <= 300:
        raise InputError("The record hours must be between 0 and 300.")
    return cgpa, hours


def _items(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;،\n]+", text or "") if part.strip()]


def parse_syllabus(text: str) -> list[list]:
    """'Midterm 30, Assignments 10, Final 40' -> [["Midterm", 30.0], ...].
    The weights are out of 100 and must add up to it, with exactly one Final."""
    components: list[list] = []
    for item in _items(text):
        match = re.fullmatch(rf"(.+?)\s*[:=\-]?\s*({NUMBER})\s*%?", item)
        if not match:
            raise InputError(f"Couldn't read “{item}”. Use: name weight, e.g. Midterm 30")
        name, weight = match.group(1).strip(" :=-"), float(match.group(2))
        if not name or weight <= 0:
            raise InputError(f"Couldn't read “{item}”. Use: name weight, e.g. Midterm 30")
        if any(n.lower() == name.lower() for n, _ in components):
            raise InputError(f"“{name}” is listed twice.")
        components.append([name, weight])
    if len(components) < 2:
        raise InputError("List at least two parts, e.g. Midterm 30, Final 40, Assignments 30")
    total = sum(w for _, w in components)
    if abs(total - 100) > 0.01:
        raise InputError(f"The weights add up to {fmt(total)}, but must add up to 100.")
    if sum(1 for n, _ in components if FINAL_RE.search(n)) != 1:
        raise InputError("Exactly one part must be the final exam — name it “Final”.")
    return components


def final_part(syllabus: list[list]) -> list | None:
    return next((c for c in syllabus if FINAL_RE.search(c[0])), None)


def parse_marks(text: str, syllabus: list[list]) -> dict[str, float]:
    """'Midterm 24, Assignments 8' (or '24/30') -> {"Midterm": 24.0, ...}.
    A part is matched by name (a prefix is enough); marks are points out of
    that part's weight."""
    marks: dict[str, float] = {}
    for item in _items(text):
        match = re.fullmatch(rf"(.+?)\s*[:=\-]?\s*({NUMBER})\s*(?:/\s*({NUMBER}))?", item)
        if not match:
            raise InputError(f"Couldn't read “{item}”. Use: name mark, e.g. Midterm 24")
        key = re.sub(r"\W", "", match.group(1)).lower()
        hits = [(n, w) for n, w in syllabus
                if key and (re.sub(r"\W", "", n).lower().startswith(key)
                            or key.startswith(re.sub(r"\W", "", n).lower()))]
        if len(hits) != 1:
            names = ", ".join(n for n, _ in syllabus)
            raise InputError(f"“{match.group(1).strip()}” doesn't match one part of the syllabus ({names}).")
        name, weight = hits[0]
        value = float(match.group(2))
        if match.group(3):                       # "12/15" on a part worth 30
            out_of = float(match.group(3))
            if out_of <= 0:
                raise InputError(f"“{item}” is out of zero.")
            value = value / out_of * weight
        if not 0 <= value <= weight + 1e-9:
            raise InputError(f"{name} is worth {fmt(weight)}, so the mark can't be {fmt(value)}.")
        marks[name] = round(value, 2)
    if not marks:
        raise InputError("Send at least one mark, e.g. Midterm 24")
    return marks


# -------------------------------------------------------------------- /need

def need_for(course: dict, target: float) -> dict | None:
    """What one course needs on its final to end on `target`.
    None without a syllabus. The result has: weight (of the final), earned
    (points from the other parts), required (points on the final, may be <= 0
    or above the weight), missing (parts with no mark yet, counted as 0)."""
    syllabus = course.get("syllabus")
    final = final_part(syllabus) if syllabus else None
    if not final:
        return None
    marks = course.get("marks") or {}
    others = [(n, w) for n, w in syllabus if n != final[0]]
    earned = sum(marks.get(n, 0) for n, _ in others)
    return {"weight": final[1], "earned": earned, "out_of": 100 - final[1],
            "required": target - earned,
            "missing": [n for n, _ in others if n not in marks]}


def record_portal_grades(marks: dict[str, float]) -> list[str]:
    """Lock the official marks the portal published ({course name: mark});
    courses not in the calculator are ignored. Returns the names that changed."""
    state = load()
    changed = [name for name, mark in marks.items()
               if name in state["courses"] and state["courses"][name].get("portal_grade") != mark]
    for name in changed:
        state["courses"][name]["portal_grade"] = marks[name]
    if changed:
        save(state)
    return changed


def sync_retakes(old_grades: dict[str, float]) -> list[str]:
    """This term's courses that repeat an earlier attempt, from the portal record
    ({course name: the old attempt as recorded, floor 35 included}): flagged as
    retakes, so the new mark replaces the old one in the weighted sum and the
    hours count once. A retake set by hand is left alone. Returns what changed."""
    state = load()
    changed = []
    for name, c in state["courses"].items():
        if name in old_grades:
            if (c.get("retake"), c.get("old_grade"), c.get("retake_source")) != (True, old_grades[name], "portal"):
                c.update(retake=True, old_grade=old_grades[name], retake_source="portal")
                changed.append(name)
        elif c.get("retake_source") == "portal":       # no longer a retake on the portal
            c.update(retake=False, old_grade=None, retake_source=None)
            changed.append(name)
    if changed:
        save(state)
    return changed


def plan(courses: dict, target: float, scope: str, cgpa: float | None = None,
         record_hours: float | None = None) -> dict:
    """What the courses still without a grade must average for the semester
    average (scope "semester") or the cumulative GPA (scope "cgpa") to reach
    `target`. Graded courses are locked; the rest share the target equally.
    Returns {"needed", "status", "locked", "open", "semester", "cgpa"}:
      needed    the average each open course needs (None: nothing left open)
      status    secured | reachable | out_of_reach | done | no_base
      semester / cgpa   what they come to when every open course scores `needed`
                        (clamped to 0–100), or the final figures when all are graded"""
    locked = [(n, c, g) for n, c, g in graded(courses)]
    open_ = [(n, c) for n, c in courses.items() if course_grade(c) is None]
    open_hours = sum(c["hours"] for _, c in open_)
    result = {"needed": None, "status": "done", "locked": locked, "open": open_,
              "semester": None, "cgpa": None}
    if scope == "cgpa" and (record_hours is None or (cgpa is None and record_hours)):
        result["status"] = "no_base"
        return result

    if open_:
        locked_points = sum(g * c["hours"] for _, c, g in locked)
        if scope == "semester":
            needed = (target * sum(c["hours"] for c in courses.values()) - locked_points) / open_hours
        else:
            # cumulative points: the record, minus every retaken course's old attempt,
            # plus the new grades; a retake adds no hours (see cumulative())
            total_hours = record_hours + sum(c["hours"] for c in courses.values() if not c.get("retake"))
            old = sum((c.get("old_grade") or 0) * c["hours"] for c in courses.values() if c.get("retake"))
            needed = (target * total_hours - (cgpa or 0) * record_hours + old - locked_points) / open_hours
        result["needed"] = needed
        result["status"] = "secured" if needed <= 0 else "out_of_reach" if needed > 100 else "reachable"
        pace = min(max(needed, 0.0), 100.0)
        filled = {n: dict(c, grade=pace, portal_grade=None) for n, c in open_}
        projected = {**courses, **filled}
    else:
        projected = courses
    result["semester"] = semester_average(projected)
    result["cgpa"] = cumulative(projected, cgpa, record_hours)
    return result


def balanced_final_percent(courses: dict, target: float) -> float | None:
    """The one final-exam percentage that, scored in every course that has a
    syllabus, brings the semester average to `target` (the average is linear
    in it). Over the courses with a syllabus only."""
    rows = [(c["hours"], need_for(c, target)) for c in courses.values()]
    rows = [(h, n) for h, n in rows if n]
    if not rows:
        return None
    hours = sum(h for h, _ in rows)
    gap = target * hours - sum(h * n["earned"] for h, n in rows)
    scale = sum(h * n["weight"] / 100 for h, n in rows)
    return gap / scale
