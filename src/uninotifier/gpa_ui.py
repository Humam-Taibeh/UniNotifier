"""
Telegram screens for the academic calculator — all English, all buttons.

Grade Needed is a three-step flow: 🎯 scope (semester GPA or cumulative CGPA)
→ target (presets, or a custom value) → the plan: official grades locked 🔒,
the rest of the target shared equally over the courses still open, with the
final-exam mark each needs when its syllabus is known. ⚙️ Course Adjustments
(the old calculator) is where hours, retakes, grades, syllabus and marks are
tweaked.

Every function returns (text, inline-keyboard rows), or, when the next thing
needed is a typed answer, a Prompt. Courses are addressed by their position in
the semester list (callback data stays tiny); `names` is that list's source —
the courses saved from the portal. bot.py does the sending, and routes the
next plain-text message to on_text() while a Prompt is pending.

Callbacks (all start with "gpa:"):
  gpa:home          the calculator          gpa:c:<i>      a course's panel
  gpa:h:<i>:<n>     set credit hours        gpa:r:<i>      toggle retake
  gpa:g:<i>         enter its grade         gpa:x:<i>      clear its grade
  gpa:s:<i>         set syllabus weights    gpa:m:<i>      enter marks so far
  gpa:cg            current CGPA + hours    gpa:need       Grade Needed: pick a scope
  gpa:t:<s|c>       pick a target           gpa:n:<s|c>:<t> the plan for a target
  gpa:v:<s|c>       type a custom target    gpa:cancel     drop a pending question
  gpa:use:<h>:<c>   take the record hours / CGPA found on the portal
"""

from dataclasses import dataclass

from uninotifier import gpa
from uninotifier.gpa import fmt
from uninotifier.notifier import escape_md

DIVIDER = "━━━━━━━━━━━━━━━━━━"
MENU_BUTTON = {"text": "🔙 Main Menu", "callback_data": "menu"}
CANCEL_ROW = [{"text": "✖️ Cancel", "callback_data": "gpa:cancel"}]


@dataclass
class Prompt:
    """A question to ask in chat; the next plain message is the answer."""
    kind: str                  # grade | old_grade | syllabus | marks | cgpa | target_s | target_c
    index: int | None
    text: str


def _course_mark(course: dict) -> str:
    grade = gpa.course_grade(course)
    if grade is None:
        return "⬜ —"
    lock = "🔒" if course.get("portal_grade") is not None else ""
    return f'{"✅" if grade >= gpa.PASS_MARK else "❌"}{lock} {fmt(grade)}'


def _label(name: str, course: dict) -> str:
    return f'{"🔁 " if course["retake"] else ""}{name} · {fmt(course["hours"])}h · {_course_mark(course)}'


# ------------------------------------------------------------------- views

def view_home(names: list[str]) -> tuple[str, list]:
    state = gpa.load()
    order = gpa.sync_courses(state, names)
    gpa.save(state)
    courses = state["courses"]

    lines = ["⚙️ *Course Adjustments*", DIVIDER]
    for n, name in enumerate(order, 1):
        c = courses[name]
        retake = f' · 🔁 retake, was {fmt(c["old_grade"])}' if c["retake"] else ""
        lines.append(f"{n}. 📘 *{escape_md(name)}* · {fmt(c['hours'])}h · {_course_mark(c)}{retake}")
    if not order:
        lines.append("No courses saved yet — run a check first.")
    lines.append(DIVIDER)

    rows = gpa.graded(courses)
    hours_done = sum(c["hours"] for _, c, _ in rows)
    hours_all = sum(c["hours"] for c in courses.values())
    average = gpa.semester_average(courses)
    if average is None:
        lines.append("📊 *Semester Average:* — (enter a grade to start)")
    else:
        lines.append(f"📊 *Semester Average:* {fmt(average)}%  ({fmt(hours_done)} of {fmt(hours_all)} hours graded)")
        failing = [n for n, c, g in rows if g < gpa.PASS_MARK]
        if failing:
            lines.append(f"❌ *Below the 50% pass mark:* {escape_md(', '.join(failing))}")

    cgpa, passed = state["cgpa"], state["passed_hours"]
    projected = gpa.cumulative(courses, cgpa, passed) if average is not None else None
    if passed is None:
        lines.append("🎓 *Projected CGPA:* set your current CGPA and record hours first")
    elif projected is not None:
        lines.append(f"🎓 *Projected CGPA:* {fmt(projected)}%  (from {fmt(cgpa)}% over {fmt(passed)} record hours)")
    else:
        lines.append(f"🎓 *Current CGPA:* {fmt(cgpa)}% over {fmt(passed)} record hours")
    lines += ["", "Tap a course to change its hours, mark a retake, enter its grade or its syllabus. "
              "Grades marked 🔒 come from the portal."]

    buttons = [[{"text": f"{n}. {_label(name, courses[name])}", "callback_data": f"gpa:c:{n - 1}"}]
               for n, name in enumerate(order, 1)]
    buttons += [[{"text": "⚙️ CGPA & Record Hours", "callback_data": "gpa:cg"}, NEED_BUTTON],
                [MENU_BUTTON]]
    return "\n".join(lines), buttons


def view_course(names: list[str], i: int) -> tuple[str, list]:
    state = gpa.load()
    order = gpa.sync_courses(state, names)
    if not 0 <= i < len(order):
        return view_home(names)
    name = order[i]
    c = state["courses"][name]
    grade = gpa.course_grade(c)

    lines = [f"📘 *{escape_md(name)}*", DIVIDER, f"⏱️ *Credit hours:* {fmt(c['hours'])}"]
    source = "" if c["grade"] is not None or grade is None else " (sum of your marks)"
    lines.append(f"🎯 *Grade:* {fmt(grade)}{source}" if grade is not None else "🎯 *Grade:* not entered")
    if c["retake"]:
        lines.append(f"🔁 *Retake:* yes — previous grade {fmt(c['old_grade'])}; the new grade replaces it "
                     "and the hours are not counted again")
        if c["old_grade"] < gpa.PASS_MARK:
            lines.append("ℹ️ The previous attempt was a fail. If its hours were never part of your record hours, "
                         "switch Retake off so these hours are added instead.")
    else:
        lines.append("🔁 *Retake:* no (counted as a new course)")
    if c["syllabus"]:
        marks = c["marks"]
        parts = [f"{escape_md(n)} {fmt(marks[n]) + '/' if n in marks else ''}{fmt(w)}" for n, w in c["syllabus"]]
        lines.append(f"📊 *Syllabus:* {' · '.join(parts)}")
    else:
        lines.append("📊 *Syllabus:* not set (needed for the “Grade Needed” calculator)")

    rows = [[{"text": f'{"✅ " if c["hours"] == h else ""}{h}h', "callback_data": f"gpa:h:{i}:{h}"}
             for h in gpa.HOUR_CHOICES],
            [{"text": f'🔁 Retake: {"On ✅" if c["retake"] else "Off"}', "callback_data": f"gpa:r:{i}"}],
            [{"text": "✏️ Enter Grade", "callback_data": f"gpa:g:{i}"},
             {"text": "🧹 Clear Grade", "callback_data": f"gpa:x:{i}"}],
            [{"text": "📊 Set Syllabus", "callback_data": f"gpa:s:{i}"},
             {"text": "📝 Enter Marks", "callback_data": f"gpa:m:{i}"}],
            [{"text": "🔙 Back", "callback_data": "gpa:home"}]]
    return "\n".join(lines), rows


SCOPES = {"s": ("📊", "Target Semester GPA", "semester average"),
          "c": ("🎓", "Target Cumulative CGPA", "cumulative GPA")}
PRESETS = (50, 60, 70, 75, 80, 85, 90)
ADJUST_BUTTON = {"text": "⚙️ Course Adjustments", "callback_data": "gpa:home"}
NEED_BUTTON = {"text": "🎯 Grade Needed", "callback_data": "gpa:need"}


def _base_line(state: dict) -> str:
    courses = state["courses"]
    hours = sum(c["hours"] for c in courses.values())
    term = f"this term: {len(courses)} courses, {fmt(hours)} credit hours"
    if state["passed_hours"] is None:
        return f"📚 {term} · CGPA base not set yet (read /transcript once)"
    return f"📚 {term}\n🎓 Cumulative GPA now: {fmt(state['cgpa'])}%"


def view_need_picker(names: list[str] | None = None) -> tuple[str, list]:
    """Step 1 of Grade Needed: semester target or cumulative target."""
    state = gpa.load()
    gpa.sync_courses(state, names or [])
    text = "\n".join([
        "🎯 *Grade Needed*", DIVIDER,
        _base_line(state), "",
        "What are you aiming for?",
        "• *Semester GPA* — the average of this term's courses",
        "• *Cumulative CGPA* — your record after this term", "",
        "Official grades from the portal are locked in 🔒; the target is shared out over the "
        "courses that don't have one yet."])
    return text, [[{"text": f"{SCOPES['s'][0]} {SCOPES['s'][1]}", "callback_data": "gpa:t:s"}],
                  [{"text": f"{SCOPES['c'][0]} {SCOPES['c'][1]}", "callback_data": "gpa:t:c"}],
                  [ADJUST_BUTTON], [MENU_BUTTON]]


def view_targets(scope: str) -> tuple[str, list]:
    """Step 2: the target value."""
    emoji, title, what = SCOPES[scope]
    text = "\n".join([f"{emoji} *{title}*", DIVIDER, f"Pick the {what} you want to reach:",
                       "", "50 = pass mark"])
    buttons = [{"text": f"{t}", "callback_data": f"gpa:n:{scope}:{t}"} for t in PRESETS]
    buttons.append({"text": "✏️ Custom Value", "callback_data": f"gpa:v:{scope}"})
    return text, [buttons[:3], buttons[3:6], buttons[6:],
                  [{"text": "🔙 Back", "callback_data": "gpa:need"}, MENU_BUTTON]]


def view_plan(names: list[str], scope: str, target: float) -> tuple[str, list]:
    """Step 3: what every course still needs."""
    state = gpa.load()
    order = gpa.sync_courses(state, names)
    gpa.save(state)
    courses = {n: state["courses"][n] for n in order}
    emoji, title, what = SCOPES[scope]
    result = gpa.plan(courses, target, "semester" if scope == "s" else "cgpa",
                      state["cgpa"], state["passed_hours"])
    lines = [f"{emoji} *{title}: {fmt(target)}%*", DIVIDER, _base_line(state), ""]
    if result["status"] == "no_base":
        lines += ["⚠️ Your current CGPA and record hours aren't set yet.",
                  "Open ⚙️ Course Adjustments → *CGPA & Record Hours* (or tap *Use as my CGPA* under /transcript)."]
        return "\n".join(lines), [[ADJUST_BUTTON], [{"text": "🔙 Back", "callback_data": f"gpa:t:{scope}"}, MENU_BUTTON]]

    needed = result["needed"]
    for name, c, grade in result["locked"]:
        source = "portal" if c.get("portal_grade") is not None else "entered"
        lines.append(f"🔒 *{escape_md(name)}* · {fmt(c['hours'])}h · {fmt(grade)} ({source})")
    for name, c in result["open"]:
        if result["status"] == "secured":
            goal = f"anything — already secured (pass needs {fmt(gpa.PASS_MARK)})"
        elif result["status"] == "out_of_reach":
            goal = "100 isn't enough"
        else:
            goal = f"*{fmt(max(needed, 0))}*" + (f" (and {fmt(gpa.PASS_MARK)} to pass)" if needed < gpa.PASS_MARK else "")
        lines.append(f"📘 *{escape_md(name)}* · {fmt(c['hours'])}h → needs {goal}")
        need = gpa.need_for(c, max(needed, gpa.PASS_MARK)) if result["status"] == "reachable" else None
        if need:
            lines.append(f"    📝 {_requirement(need)}")
    lines.append(DIVIDER)

    status = result["status"]
    if status == "done":
        lines.append(f"✅ Every course has its grade — your {what} is final.")
    elif status == "secured":
        lines.append(f"🎉 *Already secured* — the locked grades alone reach {fmt(target)}%.")
    elif status == "out_of_reach":
        lines.append(f"⛔ *Out of reach* — even 100 in every remaining course falls short "
                     f"(it would need {fmt(needed)}).")
    else:
        lines.append(f"🎯 *Needed on each remaining course:* {fmt(needed)}%")
    if result["semester"] is not None:
        lines.append(f"📊 *Semester average{'' if status == 'done' else ' at that pace'}:* {fmt(result['semester'])}%")
    if result["cgpa"] is not None:
        lines.append(f"🎓 *CGPA{'' if status == 'done' else ' at that pace'}:* {fmt(result['cgpa'])}%")
    if any(gpa.need_for(c, 0) is None for _, c in result["open"]) and status == "reachable":
        lines += ["", "ℹ️ Add a course's syllabus in ⚙️ Course Adjustments to see the final-exam mark it needs."]

    other = "c" if scope == "s" else "s"
    return "\n".join(lines), [
        [{"text": "🔁 Another Target", "callback_data": f"gpa:t:{scope}"},
         {"text": f"{SCOPES[other][0]} As {'CGPA' if other == 'c' else 'Semester'} Target",
          "callback_data": f"gpa:n:{other}:{fmt(target)}"}],
        [ADJUST_BUTTON], [MENU_BUTTON]]


def _requirement(need: dict) -> str:
    required, weight = need["required"], need["weight"]
    if required <= 0:
        return "*Already secured* — any final score reaches the target"
    if required > weight + 1e-9:
        return (f"*Out of reach* — even full marks on the final gives "
                f"{fmt(need['earned'] + weight)}")
    return f"Final needed: *{fmt(required)} / {fmt(weight)}*  ({fmt(required / weight * 100)}%)"


# --------------------------------------------------------------- callbacks

def on_callback(names: list[str], action: str):
    """The screen (text, rows) or Prompt for a "gpa:…" callback."""
    state = gpa.load()
    order = gpa.sync_courses(state, names)
    parts = action.split(":")[1:]
    kind = parts[0] if parts else "home"

    def course(index: str) -> tuple[int, dict] | tuple[None, None]:
        i = int(index)
        return (i, state["courses"][order[i]]) if 0 <= i < len(order) else (None, None)

    if kind == "home":
        return view_home(names)
    if kind == "need":
        return view_need_picker(names)
    if kind == "t" and len(parts) > 1 and parts[1] in SCOPES:
        return view_targets(parts[1])
    if kind == "n":
        # gpa:n:<scope>:<target>; an older button carries only the target (a semester one)
        scope, value = (parts[1], parts[2]) if len(parts) > 2 else ("s", parts[1])
        return view_plan(names, scope if scope in SCOPES else "s", float(value))
    if kind == "v" and len(parts) > 1 and parts[1] in SCOPES:
        return Prompt(f"target_{parts[1]}", None, f"✏️ *{SCOPES[parts[1]][1]}*\n\n"
                      "Send the target as a number from 0 to 100, e.g. 77.5")
    if kind == "cg":
        return Prompt("cgpa", None,
                      "⚙️ *Current CGPA and record hours*\n\nSend both numbers, e.g. `71.26 72`\n"
                      "(CGPA on the 0–100 scale, then the credit hours in your record — every attempted "
                      "course counts, failed ones included.) This is synced automatically whenever "
                      "/transcript or Finals Watch reads the portal; an entry here lasts until then.")
    if kind == "use":                                # a button on older messages
        state["passed_hours"], state["cgpa"], state["base_source"] = float(parts[1]), float(parts[2]), "entered"
        gpa.save(state)
        return view_home(names)

    i, c = course(parts[1]) if len(parts) > 1 else (None, None)
    if c is None:
        return view_home(names)
    name = order[i]
    if kind == "c":
        return view_course(names, i)
    if kind == "h":
        c["hours"] = int(parts[2])
    elif kind == "x":
        c["grade"] = None
    elif kind == "r":
        if not c["retake"]:
            return Prompt("old_grade", i, f"🔁 *Retake — {escape_md(name)}*\n\n"
                          "What was your *previous grade* in this course? (0–100)")
        c["retake"], c["old_grade"] = False, None
    elif kind == "g":
        return Prompt("grade", i, f"✏️ *Grade — {escape_md(name)}*\n\nSend the grade (0–100).")
    elif kind == "s":
        return Prompt("syllabus", i, f"📊 *Syllabus — {escape_md(name)}*\n\n"
                      "Send each part of the grade with its weight (out of 100), e.g.\n"
                      "`Midterm 30, Assignments 10, Project 20, Final 40`\n"
                      "The weights must add up to 100 and one part must be the *Final*.")
    elif kind == "m":
        if not c["syllabus"]:
            return (f"⚠️ Set the syllabus of *{escape_md(name)}* first.",
                    [[{"text": "📊 Set Syllabus", "callback_data": f"gpa:s:{i}"}],
                     [{"text": "🔙 Back", "callback_data": f"gpa:c:{i}"}]])
        parts_text = ", ".join(f"{n} ({fmt(w)})" for n, w in c["syllabus"])
        return Prompt("marks", i, f"📝 *Marks — {escape_md(name)}*\n\n"
                      f"Send the marks you have so far, as points out of each part's weight:\n{escape_md(parts_text)}\n"
                      "e.g. `Midterm 24, Assignments 8` — send a part again to correct it.")
    gpa.save(state)
    return view_course(names, i)


def on_text(names: list[str], pending: Prompt, text: str) -> tuple[str, list] | str:
    """Apply a typed answer. Returns the screen to show next, or an error
    message (str) when the answer was unusable and the question stays open."""
    state = gpa.load()
    order = gpa.sync_courses(state, names)
    try:
        if pending.kind.startswith("target_"):
            return view_plan(names, pending.kind[-1], gpa.parse_number(text, what="target"))
        if pending.kind == "cgpa":
            state["cgpa"], state["passed_hours"] = gpa.parse_cgpa(text)
            state["base_source"] = "entered"
            gpa.save(state)
            return view_home(names)
        c = state["courses"][order[pending.index]]
        if pending.kind == "grade":
            c["grade"] = gpa.parse_number(text, what="grade")
        elif pending.kind == "old_grade":
            c["old_grade"], c["retake"] = gpa.parse_number(text, what="grade"), True
        elif pending.kind == "syllabus":
            c["syllabus"], c["marks"] = gpa.parse_syllabus(text), {}
        elif pending.kind == "marks":
            c["marks"].update(gpa.parse_marks(text, c["syllabus"]))
    except gpa.InputError as e:
        return f"⚠️ {escape_md(str(e))}\n\nTry again, or tap Cancel."
    except (IndexError, KeyError, TypeError):
        return view_home(names)          # the course list changed under the question
    gpa.save(state)
    return view_course(names, pending.index)
