"""
What to register next term — pure rules and text over the study plan (degreeplan.py): no portal,
no Telegram.

The plan says what is left, what each course needs first (prereqs) and how every course stands.
From that, for the term after this one:

  advise()     every course still to take, as open to you or not yet — and why
  options()    the hours you may ask for (the buttons)
  propose()    the courses for that many hours, the most important first
  *_lines()    those as message lines

This term's courses are counted as passed: the advice is for the term after them. A course that
ends up failed shows as failed on the plan, and comes back here by itself as a retake — with
whatever it was the prerequisite of closed again.

The rules (the university's, as its students know them — constants below):
  - a regular term: 12 to 18 hours; 21 in the graduation term, or with an excellent CGPA
  - a summer term: up to 9 hours; 12 only when it is the student's last term at the university
  - the graduation term: everything left fits in the term and nothing is held back by a prerequisite
  - field training and the graduation project: the final year (SENIOR_HOURS passed and no more than
    a year's hours left), with the department head's approval
  - military science: the graduation term
  - a failed course is retaken first; then a course that opens the most courses after it; an
    elective only up to what its category still needs
"""

import re

from uninotifier import degreeplan
from uninotifier.degreeplan import FSI, LRM, PDI
from uninotifier.notifier import escape_md

REGULAR, SUMMER = "regular", "summer"
REGULAR_HOURS = (12, 15, 18)
REGULAR_TOP = 21                    # the graduation term, or an excellent CGPA
SUMMER_HOURS = (3, 6, 9)
SUMMER_TOP = 12                     # only the student's last term
SENIOR_HOURS = 90                   # passed, before training and the graduation project
YEAR_HOURS = 18 + 18 + 9            # what a last year holds: two terms and a summer

_SENIOR = re.compile(r"تدريب|مشروع\s*(?:ال)?تخرج|منهجية.*مشروع")
_MILITARY = re.compile(r"العلوم العسكرية|علوم عسكرية")
_TERMS = ("First semester", "Second semester", "Summer semester")
_ICONS = {"remedial": "🧩", "retake": "🔁", "compulsory": "📘", "elective": "🎯"}
_ORDER = {"remedial": 0, "retake": 1, "compulsory": 2, "elective": 3}


# ---------------------------------------------------------------- the term

def term_key(term: str) -> tuple[int, int]:
    """'Second semester 2025/2026' -> (2025, 1): terms in the order they are studied."""
    year = re.search(r"(\d{4})/\d{4}", term or "")
    return (int(year.group(1)) if year else 0, next((n for n, name in enumerate(_TERMS) if name in (term or "")), 0))


def latest_term(semesters: list[dict]) -> str | None:
    """The newest term of a transcript."""
    names = [s.get("term") or "" for s in semesters or []]
    return max(names, key=term_key) if names else None


def next_term(term: str | None) -> tuple[str, str]:
    """The term after `term` and its kind: First -> Second -> Summer -> First of the next year."""
    year = re.search(r"(\d{4})/(\d{4})", term or "")
    if not year:
        return "Next term", REGULAR
    start, at = int(year.group(1)), term_key(term)[1]
    if at == 2:
        return f"{_TERMS[0]} {start + 1}/{start + 2}", REGULAR
    return f"{_TERMS[at + 1]} {start}/{start + 1}", SUMMER if at == 1 else REGULAR


def other_term(term: str, kind: str) -> tuple[str, str]:
    """The term to plan instead: the summer after a regular term, the regular term after a summer."""
    year = re.search(r"(\d{4})/(\d{4})", term or "")
    if not year:
        return ("Summer term", SUMMER) if kind == REGULAR else ("Next term", REGULAR)
    start = int(year.group(1))
    if kind == SUMMER:
        return f"{_TERMS[0]} {start + 1}/{start + 2}", REGULAR
    return f"{_TERMS[2]} {start}/{start + 1}", SUMMER


# ---------------------------------------------------------------- the advice

def _level(course: dict) -> int:
    """The year a course belongs to, from its code (0142[3]51): lower years first."""
    code = course["code"]
    return int(code[4]) if len(code) > 4 and code[4].isdigit() else 9


def _is_choice(category: dict) -> bool:
    return sum(c["hours"] for c in category["courses"]) > category["required"]


def _prerequisites(course: dict, by_code: dict) -> tuple[str, list[dict], list[str]]:
    """("open" | "if" | "blocked", the courses it waits for, codes the plan does not list)."""
    waits, pending, unknown = [], [], []
    for code in course.get("prereqs") or []:
        before = by_code.get(code)
        if before is None:
            unknown.append(code)
        elif before["status"] in degreeplan.DONE:
            continue
        elif before["status"] == "in_progress":
            pending.append(before)
        else:
            waits.append(before)
    if waits:
        return "blocked", waits, unknown
    return ("if", pending, unknown) if pending else ("open", [], unknown)


def _unlocks(plan: dict) -> dict[str, int]:
    """How many courses still to take wait, directly or through others, for each course."""
    left = [c for cat in plan["categories"] for c in cat["courses"] if c["status"] not in degreeplan.DONE]
    waits_for = {c["code"]: set(c.get("prereqs") or []) for c in left}
    counts: dict[str, int] = {}
    for course in left:
        seen, queue = set(), list(waits_for[course["code"]])
        while queue:
            code = queue.pop()
            if code in seen:
                continue
            seen.add(code)
            queue += list(waits_for.get(code, ()))
        for code in seen:
            counts[code] = counts.get(code, 0) + 1
    return counts


def advise(plan: dict, kind: str = REGULAR) -> dict:
    """Where the student stands for the term after this one, and every course still to take:

      {"left": hours left once this term is passed (remedial courses owed included),
       "passed": the plan's hours passed by then, "term_courses": this term's courses (counted as passed),
       "graduating": the term can be the last one, "senior": the final year,
       "entries": [{"course", "category", "kind": remedial | retake | compulsory | elective,
                    "state": open | if | blocked, "waits": [courses], "why": text when blocked,
                    "approval": bool, "unlocks": n, "unknown": [codes]}]  — the most important first,
       "needs": {category name: elective hours it still needs}, "open_hours": hours open to register}
    """
    by_code = {c["code"]: c for cat in plan["categories"] for c in cat["courses"]}
    unlocks = _unlocks(plan)
    entries, needs = [], {}
    left = passed = 0.0
    for category in plan["categories"]:
        remedial = degreeplan.is_remedial(category)
        if remedial:
            need = sum(c["hours"] for c in category["courses"]
                       if c["status"] not in degreeplan.DONE and c["status"] != "in_progress")
        else:
            need = max(degreeplan.after_term(category), 0)
            passed += category["required"] - need
        left += need
        if need <= 0:
            continue
        choice = not remedial and _is_choice(category)
        if choice:
            needs[category["name"]] = need
        for course in category["courses"]:
            if course["status"] in degreeplan.DONE or course["status"] == "in_progress":
                continue
            state, waits, unknown = _prerequisites(course, by_code)
            entries.append({"course": course, "category": category["name"],
                            "kind": ("remedial" if remedial else "retake" if course["status"] == "failed"
                                     else "elective" if choice else "compulsory"),
                            "state": state, "waits": waits, "why": "", "approval": False,
                            "unlocks": unlocks.get(course["code"], 0), "unknown": unknown})
    senior = passed >= SENIOR_HOURS and left <= YEAR_HOURS
    for entry in entries:
        if _SENIOR.search(entry["course"]["name"]):
            entry["approval"] = True
            if not senior:
                entry["state"], entry["why"] = "blocked", (
                    f"final year only ({SENIOR_HOURS}+ hrs passed), with the department head's approval")
    # the last term: all that is left fits in it, with nothing held back (military science waits for this very term)
    top = REGULAR_TOP if kind == REGULAR else SUMMER_TOP
    graduating = 0 < left <= top and _reachable(entries, needs) >= left
    for entry in entries:
        if _MILITARY.search(entry["course"]["name"]) and not graduating:
            entry["state"], entry["why"] = "blocked", "graduation term only"
        if entry["state"] == "blocked" and not entry["why"]:
            entry["why"] = "after " + " and ".join(_name(c) for c in entry["waits"])
    entries.sort(key=lambda e: (_ORDER[e["kind"]], -e["unlocks"], _level(e["course"]), e["course"]["code"]))
    term_courses = sum(1 for c in by_code.values() if c["status"] == "in_progress")
    return {"left": left, "passed": passed, "term_courses": term_courses, "graduating": graduating,
            "senior": senior, "entries": entries, "needs": needs, "kind": kind,
            "open_hours": _reachable([e for e in entries if e["state"] != "blocked"], needs)}


def _reachable(entries: list[dict], needs: dict) -> float:
    """The hours these courses can bring towards the plan: an elective category only up to its need."""
    total, chosen = 0.0, dict.fromkeys(needs, 0.0)
    for entry in entries:
        if entry["state"] == "blocked":
            continue
        hours = entry["course"]["hours"]
        if entry["category"] in needs:
            room = needs[entry["category"]] - chosen[entry["category"]]
            if room <= 0:
                continue
            hours = min(hours, room)
            chosen[entry["category"]] += hours
        total += hours
    return total


def options(advice: dict) -> list[tuple[int, str]]:
    """The hours to choose from, as (hours, button text). A regular term: 12 / 15 / 18, and 21 ⭐
    (the graduation term or an excellent CGPA); a summer term: 3 / 6 / 9, and 12 in the last term.
    In the graduation term the last choice is everything that is left."""
    left = int(advice["left"]) if float(advice["left"]).is_integer() else advice["left"]
    if advice["kind"] == SUMMER:
        base = [(h, f"{h} hrs") for h in SUMMER_HOURS]
    else:
        base = [(h, f"{h} hrs") for h in REGULAR_HOURS] + ([] if advice["graduating"] else [(REGULAR_TOP, f"{REGULAR_TOP} hrs ⭐")])
    if advice["graduating"]:
        return [(h, text) for h, text in base if h < left] + [(left, f"{left:g} hrs · all that is left")]
    return [(h, text) for h, text in base if h <= max(left, base[0][0])]


def propose(advice: dict, hours: float) -> dict:
    """The courses for `hours`: taken in the advice's order, an elective category only up to what it
    needs, never past the hours asked for. {"entries", "total", "short": the hours could not be filled}."""
    picked, total, chosen = [], 0.0, dict.fromkeys(advice["needs"], 0.0)
    for entry in advice["entries"]:
        size = entry["course"]["hours"]
        if entry["state"] == "blocked" or total + size > hours:
            continue
        if entry["category"] in chosen:
            if chosen[entry["category"]] >= advice["needs"][entry["category"]]:
                continue
            chosen[entry["category"]] += size
        picked.append(entry)
        total += size
    return {"entries": picked, "total": total, "short": total < hours}


# ---------------------------------------------------------------- the text

def _n(value: float) -> str:
    return f"{value:g}"


def _name(course: dict) -> str:
    return f"{FSI}{escape_md(course['name'] or course['code'])}{PDI}"


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _notes(entry: dict) -> str:
    """What to know about a course that is open: a retake, what it opens, what it still depends on."""
    notes = []
    if entry["kind"] == "retake":
        notes.append("retake")
    elif entry["kind"] == "remedial":
        notes.append("remedial, on top of the plan")
    elif entry["kind"] == "elective":
        notes.append(f"elective · {escape_md(degreeplan.english(entry['category']))}")
    if entry["unlocks"]:
        notes.append(f"opens {_count(entry['unlocks'], 'course')}")
    if entry["state"] == "if":
        notes.append("if you pass " + " and ".join(_name(c) for c in entry["waits"]) + " this term")
    if entry["approval"]:
        notes.append("🖊️ department head's approval")
    if entry["unknown"]:
        notes.append("⚠️ needs " + ", ".join(entry["unknown"]) + " — not on your plan, check it")
    return " — " + " · ".join(notes) if notes else ""


def _line(entry: dict, number: int | None = None) -> str:
    head = f"{number}. " if number else ""
    course = entry["course"]
    return f"{LRM}{head}{_ICONS[entry['kind']]} {_name(course)} · {_n(course['hours'])}h{_notes(entry)}"


def allowed_line(advice: dict) -> str:
    if advice["kind"] == SUMMER:
        return (f"📏 *Summer term:* up to {SUMMER_HOURS[-1]} hrs"
                + (f" · {SUMMER_TOP} now, as it is your last term" if advice["graduating"]
                   else f" · {SUMMER_TOP} only in your last term at the university"))
    if advice["graduating"]:
        return f"📏 *Graduation term:* up to {REGULAR_TOP} hrs, and fewer than {REGULAR_HOURS[0]} is allowed"
    return (f"📏 *Regular term:* {REGULAR_HOURS[0]}–{REGULAR_HOURS[-1]} hrs · ⭐ {REGULAR_TOP} only with an "
            "excellent CGPA or in your graduation term")


def overview_lines(advice: dict, term: str) -> list[str]:
    """The Registration screen: where you stand for `term`, before the hours are chosen."""
    entries = advice["entries"]
    can = [e for e in entries if e["state"] != "blocked"]
    lines = [f"🗓️ *Registration Plan* — {escape_md(term)}"]
    if not advice["left"]:
        return lines + ["", "🎓 *Nothing is left on your plan* once this term is passed."]
    left = f"🏁 *Left to take:* {_n(advice['left'])} hrs"
    if advice["term_courses"]:
        left += f" — this term's {_count(advice['term_courses'], 'course')} counted as passed"
    lines += [left, f"✅ *Open to you:* {_n(advice['open_hours'])} hrs · {_count(len(can), 'course')}",
              f"⛔ *Not yet:* {_count(len(entries) - len(can), 'course')}", allowed_line(advice)]
    if advice["graduating"]:
        lines.append("🎓 *This can be your graduation term:* everything left fits in it.")
    return lines


def proposal_lines(advice: dict, hours: float, term: str) -> list[str]:
    """The courses proposed for `hours`."""
    proposal = propose(advice, hours)
    lines = [f"🗓️ *{_n(hours)} hrs* — {escape_md(term)}", ""]
    lines += [_line(entry, n) for n, entry in enumerate(proposal["entries"], 1)]
    lines += ["", f"*Total:* {_n(proposal['total'])} hrs · {_count(len(proposal['entries']), 'course')}"]
    if proposal["short"]:
        lines.append(f"⚠️ Only {_n(proposal['total'])} hrs could be filled: the rest of your plan is not open to you yet.")
    low = REGULAR_HOURS[0]
    if advice["kind"] == REGULAR and proposal["total"] < low and not advice["graduating"]:
        lines.append(f"⚠️ A regular term needs at least {low} hrs.")
    if advice["kind"] == REGULAR and hours >= REGULAR_TOP and not advice["graduating"]:
        lines.append(f"⭐ {REGULAR_TOP} hrs is allowed only with an excellent CGPA — it is not your graduation term.")
    if any(e["state"] == "if" for e in proposal["entries"]):
        lines.append("_A course marked “if you pass” opens only when this term's course is passed._")
    return lines


def open_lines(advice: dict) -> list[str]:
    """Everything that can be registered, the most important first."""
    can = [e for e in advice["entries"] if e["state"] != "blocked"]
    lines = ["✅ *What You Can Take*", f"{_n(advice['open_hours'])} hrs towards your plan · the most important first", ""]
    lines += [_line(entry) for entry in can] or ["Nothing is open yet."]
    for name, need in advice["needs"].items():
        offered = sum(e["course"]["hours"] for e in can if e["category"] == name)
        if offered > need:
            lines += ["", f"_🎯 {escape_md(degreeplan.english(name))}: choose {_n(need)} hrs of these electives._"]
    return lines


def blocked_lines(advice: dict) -> list[str]:
    """What cannot be registered yet, each with its reason."""
    held = [e for e in advice["entries"] if e["state"] == "blocked"]
    lines = ["⛔ *Not Yet — and Why*", ""]
    lines += [f"{LRM}▫️ {_name(e['course'])} · {_n(e['course']['hours'])}h — {e['why']}" for e in held]
    return lines if held else lines + ["Nothing is held back: every course left is open to you."]
