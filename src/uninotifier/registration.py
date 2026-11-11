"""
What to register next term — pure rules and text over the study plan (degreeplan.py): no portal,
no Telegram.

The plan says what is left, what each course needs first (prereqs) and how every course stands.
From that, for the term after this one:

  advise()     every course still to take, as open to you or not yet — and why
  options()    the hours you may ask for (the buttons)
  propose()    the courses for that many hours, the most important first
  check()      a choice of your own: its hours, and what the rules say about it
  *_lines()    those as message lines

This term's courses are counted as passed: the advice is for the term after them. A course that
ends up failed shows as failed on the plan, and comes back here by itself as a retake — with
whatever it was the prerequisite of closed again.

The rules (the university's, as its students know them — constants below):
  - a regular term: 12 to 18 hours; 21 in the graduation term, or with an excellent CGPA
  - a summer term: up to 9 hours; 12 only when it is the student's last term at the university
  - the graduation term: everything left fits in the term and nothing is held back by a prerequisite
  - field training and the graduation project: once SENIOR_HOURS of the plan are passed, with the
    department head's approval
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
                            "state": state, "waits": waits, "why": "", "reason": "prereq", "approval": False,
                            "unlocks": unlocks.get(course["code"], 0), "unknown": unknown})
    senior = passed >= SENIOR_HOURS
    for entry in entries:
        if _SENIOR.search(entry["course"]["name"]):
            entry["approval"] = True
            if not senior:
                entry["state"], entry["why"], entry["reason"] = "blocked", (
                    f"after {SENIOR_HOURS} hrs passed, with the department head's approval"), "senior"
    # the last term: all that is left fits in it, with nothing held back (military science waits for this very term)
    top = REGULAR_TOP if kind == REGULAR else SUMMER_TOP
    graduating = 0 < left <= top and _reachable(entries, needs) >= left
    for entry in entries:
        if _MILITARY.search(entry["course"]["name"]) and not graduating:
            entry["state"], entry["why"], entry["reason"] = "blocked", "graduation term only", "military"
        if entry["state"] == "blocked" and not entry["why"]:
            entry["why"] = "after " + " and ".join(_name(c) for c in entry["waits"])
    entries.sort(key=lambda e: (_ORDER[e["kind"]], -e["unlocks"], _level(e["course"]), e["course"]["code"]))
    term_courses = sum(1 for c in by_code.values() if c["status"] == "in_progress")
    return {"left": left, "passed": passed, "term_courses": term_courses, "graduating": graduating,
            "held_hours": left - _reachable([e for e in entries if e["state"] != "blocked"], needs),
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


def check(advice: dict, codes) -> dict:
    """A choice of your own (course codes, among the open ones): {"entries", "total" (hours
    registered), "counted" (of those, towards the plan), "notes": what the rules say about it}."""
    wanted = set(codes)
    entries = [e for e in advice["entries"] if e["state"] != "blocked" and e["course"]["code"] in wanted]
    total = sum(e["course"]["hours"] for e in entries)
    counted = _reachable(entries, advice["needs"])
    notes = []
    for name, need in advice["needs"].items():
        taken = sum(e["course"]["hours"] for e in entries if e["category"] == name)
        if taken > need:
            notes.append(f"⚠️ {escape_md(degreeplan.english(name))} needs only {_n(need)} more hrs: "
                         f"{_n(taken - need)} of these add nothing to your plan.")
    if advice["kind"] == SUMMER:
        if total > SUMMER_TOP:
            notes.append(f"⛔ A summer term takes {SUMMER_TOP} hrs at the very most.")
        elif total > SUMMER_HOURS[-1] and not advice["graduating"]:
            notes.append(f"⛔ More than {SUMMER_HOURS[-1]} hrs in a summer term is only for your last term at the university.")
    else:
        if total > REGULAR_TOP:
            notes.append(f"⛔ A term takes {REGULAR_TOP} hrs at the very most.")
        elif total > REGULAR_HOURS[-1] and not advice["graduating"]:
            notes.append(f"⭐ More than {REGULAR_HOURS[-1]} hrs is allowed only with an excellent CGPA — "
                         "it is not your graduation term.")
        if 0 < total < REGULAR_HOURS[0] and not advice["graduating"]:
            notes.append(f"⚠️ A regular term needs at least {REGULAR_HOURS[0]} hrs.")
    if any(e["state"] == "if" for e in entries):
        notes.append("_A course marked “if you pass” opens only when this term's course is passed._")
    return {"entries": entries, "total": total, "counted": counted, "notes": notes}


# ---------------------------------------------------------------- the text

def _n(value: float) -> str:
    return f"{value:g}"


def _name(course: dict) -> str:
    return f"{FSI}{escape_md(course['name'] or course['code'])}{PDI}"


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _notes(entry: dict, kind: bool = True) -> str:
    """What to know about a course that is open: a retake, what it opens, what it still depends on.
    `kind` off under a heading that already says it."""
    notes = []
    if kind and entry["kind"] in ("retake", "elective"):
        notes.append(entry["kind"])
    elif kind and entry["kind"] == "remedial":
        notes.append("remedial, on top of the plan")
    if entry["unlocks"]:
        notes.append(f"opens {_count(entry['unlocks'], 'course')}")
    if entry["state"] == "if":
        notes.append("if you pass " + " and ".join(_name(c) for c in entry["waits"]) + " this term")
    if entry["approval"]:
        notes.append("🖊️ department head's approval")
    if entry["unknown"]:
        notes.append("⚠️ needs " + ", ".join(entry["unknown"]) + " — not on your plan, check it")
    return " — " + " · ".join(notes) if notes else ""


def _line(entry: dict, number: int | None = None, kind: bool = True) -> str:
    course = entry["course"]
    head = f"{number}. {_ICONS[entry['kind']]}" if number else "▫️"
    return f"{LRM}{head} {_name(course)} · {_n(course['hours'])}h{_notes(entry, kind)}"


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
              f"⛔ *Not yet:* {_n(advice['held_hours'])} hrs · {_count(len(entries) - len(can), 'course')}",
              allowed_line(advice)]
    if advice["graduating"]:
        lines.append("🎓 *This can be your graduation term:* everything left fits in it.")
    return lines


def proposal_lines(advice: dict, hours: float, term: str) -> list[str]:
    """The courses suggested for `hours`."""
    proposal = propose(advice, hours)
    lines = [f"✨ *Suggested: {_n(hours)} hrs* — {escape_md(term)}",
             "_Failed courses first, then the courses that open the most after them._", ""]
    lines += [_line(entry, n) for n, entry in enumerate(proposal["entries"], 1)]
    lines += ["", f"*Total:* {_n(proposal['total'])} hrs · {_count(len(proposal['entries']), 'course')}"]
    if proposal["short"]:
        lines.append(f"⚠️ Only {_n(proposal['total'])} hrs could be filled: the rest of your plan is not open to you yet.")
    return lines + check(advice, [e["course"]["code"] for e in proposal["entries"]])["notes"]


def choice_lines(advice: dict, codes, term: str) -> list[str]:
    """Your own choice among the open courses: what is chosen, its hours, and what the rules say."""
    chosen = check(advice, codes)
    lines = [f"✋ *Your Choice* — {escape_md(term)}", "_Tap a course to add it or take it out._", ""]
    lines += [_line(entry, n) for n, entry in enumerate(chosen["entries"], 1)] or ["Nothing chosen yet."]
    total = f"*Total:* {_n(chosen['total'])} hrs · {_count(len(chosen['entries']), 'course')}"
    if chosen["counted"] != chosen["total"]:
        total += f" · {_n(chosen['counted'])} towards your plan"
    return lines + ["", total] + chosen["notes"]


_HEADINGS = (("remedial", "🧩 *Remedial* — on top of the plan's hours"), ("retake", "🔁 *Retake first*"),
             ("compulsory", "📘 *Compulsory* — the most important first"))


def open_lines(advice: dict) -> list[str]:
    """Everything that can be registered, under what it is: retakes, compulsory courses, then each
    group of electives with the hours it still needs."""
    can = [e for e in advice["entries"] if e["state"] != "blocked"]
    lines = ["✅ *What You Can Take*", f"{_n(advice['open_hours'])} hrs towards your plan"]
    if not can:
        return lines + ["", "Nothing is open yet."]
    for kind, title in _HEADINGS:
        group = [e for e in can if e["kind"] == kind]
        if group:
            lines += ["", title] + [_line(e, kind=False) for e in group]
    for name, need in advice["needs"].items():
        group = [e for e in can if e["category"] == name]
        if group:
            lines += ["", f"🎯 *{escape_md(degreeplan.english(name))}* — choose {_n(need)} hrs"]
            lines += [_line(e, kind=False) for e in group]
    return lines


def blocked_lines(advice: dict) -> list[str]:
    """What cannot be registered yet, under its reason: a course to pass first (named under each
    one), the hours the final-year courses wait for, the graduation term."""
    held = [e for e in advice["entries"] if e["state"] == "blocked"]
    lines = ["⛔ *Not Yet — and Why*", f"{_n(advice['held_hours'])} hrs towards your plan · {_count(len(held), 'course')}"]
    if not held:
        return lines + ["", "Nothing is held back: every course left is open to you."]

    def course(entry: dict) -> str:
        return f"{LRM}▫️ *{_name(entry['course'])}* · {_n(entry['course']['hours'])}h"

    waiting = [e for e in held if e["reason"] == "prereq"]
    if waiting:
        lines += ["", "🔗 *Another course comes first*"]
        for entry in waiting:
            lines += [course(entry), f"{LRM}      └ first: " + " and ".join(_name(c) for c in entry["waits"])]
    senior = [e for e in held if e["reason"] == "senior"]
    if senior:
        lines += ["", f"🎓 *After {SENIOR_HOURS} hrs passed* — you have {_n(advice['passed'])} after this term",
                  "_Each needs the department head's approval._"] + [course(e) for e in senior]
    military = [e for e in held if e["reason"] == "military"]
    if military:
        lines += ["", "🏁 *Graduation term only*"] + [course(e) for e in military]
    return lines
