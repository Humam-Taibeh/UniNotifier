"""
The student's own study plan, as the registration portal lists it («جميع خطة الطالب») — pure
parsing and text, no portal, no Telegram.

The page holds the plan as categories of requirements (university, faculty, major, supporting, …,
compulsory or elective). Each has its courses — code, name, credit hours and a status: مجتازة
passed, قيد الدراسة registered this term, راسب failed, معفاة exempted, or nothing yet — the courses
to pass before it (المتطلبات السابقة, kept as "prereqs": their codes) and the
portal's own totals: hours required, passed and left. Nothing here is fixed to one major or one
plan year: whatever plan the portal shows for the signed-in student is what is counted.

  parse()            the page -> {"categories": [{name, required, passed, remaining, courses}]}
  progress()         the figures of the summary: plan hours, passed, registered this term, failed
                     and not retaken, hours left now and after this term
  progress_lines()   those as message lines
  remaining_lines()  the courses still to take, category by category

Remedial courses (استدراكية) are the placement exams' other side: a new student sits a placement
exam in Arabic, English and computer skills, and is exempted (معفاة) from the remedial course of
each one passed. One that was not passed must be taken as a course ON TOP of the plan's hours. So
they are never part of the degree's figure, as the university counts it, and are shown on their
own (placement(), placement_lines()): each exam's outcome and the hours it adds, if any.
"""

import re

from lxml import html as lxml_html

from uninotifier.notifier import escape_md

LRM, FSI, PDI = "‎", "⁨", "⁩"
STATUSES = {"مجتازة": "passed", "قيد الدراسة": "in_progress", "راسب": "failed", "معفاة": "exempt"}
DONE = ("passed", "exempt")
REMEDIAL = "استدراكي"
_WORDS = (("متطلبات", ""), ("مواد", ""), ("م.", ""), ("جامعية", "University"), ("الجامعة", "University"),
          ("الكلية", "Faculty"), ("كلية", "Faculty"), ("عائلة تخصص", "Major Family"), ("تخصص", "Major"),
          ("التخصص", "Major"), ("مساندة", "Supporting"), ("استدراكية", "Remedial"), ("حرة", "Free"))
_KINDS = (("اجباري", "Compulsory"), ("إجباري", "Compulsory"), ("اختياري", "Elective"))


def _text(element) -> str:
    return re.sub(r"\s+", " ", element.text_content()).strip()


def _number(text: str) -> float | None:
    return float(text) if re.fullmatch(r"\d+(?:\.\d+)?", text or "") else None


def _columns(panel) -> dict[str, int]:
    """Where each column is, by its title; the order seen on the portal otherwise."""
    found = {"status": 0, "code": 1, "name": 2, "hours": 3}
    for index, title in enumerate(_text(th) for th in panel.xpath(".//thead//th")):
        for key, word in (("status", "الحالة"), ("code", "رمز"), ("name", "اسم"), ("hours", "الساعات"),
                          ("prereqs", "السابقة")):
            if word in title:
                found[key] = index
    return found


def _totals(panel) -> dict[str, float]:
    """The category's own footer: عدد الساعات المعتمدة 24 · اجتاز منها 15 · الساعات المتبقة 9."""
    labels = [_text(label) for label in panel.xpath('.//div[contains(@class, "ui-datatable-footer")]//label')]
    totals = {}
    for title, value in zip(labels, labels[1:]):
        number = _number(value)
        if number is None:
            continue
        for key, word in (("required", "المعتمدة"), ("passed", "اجتاز"), ("remaining", "المتبق")):
            if word in title:
                totals.setdefault(key, number)
    return totals


def parse(html: str) -> dict | None:
    """The plan on the page, or None when it does not read as one. The page repeats the categories
    under each of its tabs; every category is taken once, in the page's order."""
    root = lxml_html.fromstring(html)
    for title in root.xpath('//td//span[contains(@class, "ui-column-title")]'):   # the phone layout's copy of the header
        title.drop_tree()
    panels = {panel.get("id"): panel for panel in root.xpath("//div[@id]")}
    categories, seen = [], set()
    for link in root.xpath('//ul[contains(@class, "ui-tabs-nav")]//a[@href]'):
        name, panel = _text(link), panels.get(link.get("href").lstrip("#"))
        if panel is None or not name or name in seen:
            continue
        if panel.xpath('.//ul[contains(@class, "ui-tabs-nav")]'):
            continue                                        # a tab that holds the categories, not one of them
        at = _columns(panel)
        courses = []
        for row in panel.xpath(".//tbody/tr"):
            cells = [_text(cell) for cell in row.xpath("./td")]
            if len(cells) <= max(at[key] for key in ("status", "code", "name", "hours")):
                continue
            hours = _number(cells[at["hours"]])
            if hours is None or not cells[at["code"]]:
                continue
            before = cells[at["prereqs"]] if "prereqs" in at and len(cells) > at["prereqs"] else ""
            courses.append({"code": cells[at["code"]], "name": cells[at["name"]], "hours": hours,
                            "status": STATUSES.get(cells[at["status"]], "open" if not cells[at["status"]] else "other"),
                            "prereqs": prerequisites(before)})
        totals = _totals(panel)
        if not courses or len(totals) < 3:
            continue
        seen.add(name)
        categories.append({"name": name, **totals, "courses": courses})
    return {"categories": categories} if categories else None


def prerequisites(cell: str) -> list[str]:
    """'(0112120 مبادئ البرمجة)' -> ['0112120']: the codes of the courses to pass first (المتطلبات
    السابقة), each written as (code name) — a name may hold brackets of its own, 'تفاضل وتكامل (1)'."""
    return list(dict.fromkeys(re.findall(r"[(]\s*([0-9]{5,})", cell or "")))


def english(name: str) -> str:
    """'متطلبات تخصص (اجبارية)' -> 'Major · Compulsory'. A name with an unknown word stays as published."""
    base, _, kind = name.partition("(")
    kind = next((en for ar, en in _KINDS if ar in kind), "")
    rest = base.strip()
    words = []
    for ar, en in _WORDS:
        if ar in rest:
            rest = rest.replace(ar, " ", 1)
            if en:
                words.append(en)
    if rest.strip() or not words:
        return name
    return " · ".join([" ".join(dict.fromkeys(words)), kind] if kind else [" ".join(dict.fromkeys(words))])


def is_remedial(category: dict) -> bool:
    return REMEDIAL in category["name"]


def _counted(plan: dict) -> list[dict]:
    return [c for c in plan["categories"] if not is_remedial(c)]


def _term_hours(category: dict) -> float:
    """This term's hours that count towards the category: an elective taken beyond what the
    category still needs adds nothing to the degree."""
    registered = sum(c["hours"] for c in category["courses"] if c["status"] == "in_progress")
    return min(registered, category["remaining"])


def after_term(category: dict) -> float:
    """The category's hours still to take once this term's courses are passed."""
    return category["remaining"] - _term_hours(category)


def progress(plan: dict) -> dict:
    """The summary's figures (hours): degree (the whole plan), passed, remaining (left now), term
    (registered this term) with term_courses, after (left once this term is passed), failed (failed
    and not registered again) with failed_courses."""
    counted = _counted(plan)
    current = [c for cat in counted for c in cat["courses"] if c["status"] == "in_progress"]
    failed = [c for cat in counted for c in cat["courses"] if c["status"] == "failed"]
    remaining = sum(cat["remaining"] for cat in counted)
    return {"degree": sum(cat["required"] for cat in counted),
            "passed": sum(cat["passed"] for cat in counted),
            "remaining": remaining,
            "term": sum(c["hours"] for c in current), "term_courses": len(current),
            "after": remaining - sum(_term_hours(cat) for cat in counted),
            "failed": sum(c["hours"] for c in failed), "failed_courses": len(failed)}


def placement(plan: dict) -> dict | None:
    """The placement exams, from the plan's remedial courses: {"courses": [...], "extra" (hours they
    add on top of the plan: every one not exempted), "left" (of those, still to pass), "term"
    (registered this term)}. None when the plan lists none."""
    courses = [c for cat in plan["categories"] if is_remedial(cat) for c in cat["courses"]]
    if not courses:
        return None
    owed = [c for c in courses if c["status"] != "exempt"]
    return {"courses": courses,
            "extra": sum(c["hours"] for c in owed),
            "left": sum(c["hours"] for c in owed if c["status"] != "passed"),
            "term": sum(c["hours"] for c in owed if c["status"] == "in_progress")}


_SUBJECTS = (("عربي", "Arabic"), ("نجليزي", "English"), ("حاسوب", "Computer Skills"))
_OUTCOMES = {"exempt": ("✅", "exempt"), "passed": ("✅", "taken as a course and passed"),
             "in_progress": ("⏳", "being taken this term"), "failed": ("❌", "failed — to retake"),
             "open": ("▫️", "to take"), "other": ("▫️", "to take")}


def _subject(course: dict) -> str:
    """'اللغة العربية الاستدراكية' -> 'Arabic'; a subject not known here keeps its published name."""
    for word, english_name in _SUBJECTS:
        if word in course["name"]:
            return english_name
    return f"{FSI}{escape_md(course['name'] or course['code'])}{PDI}"


def placement_line(plan: dict) -> str | None:
    """One line for the summary: the exams and, when any was not passed, the hours on top of the plan."""
    p = placement(plan)
    if not p:
        return None
    if not p["extra"]:
        return f"📝 *Placement Exams:* all {len(p['courses'])} exempt — nothing added to the plan"
    marks = " · ".join(f"{_subject(c)} {_OUTCOMES[c['status']][0]}" for c in p["courses"])
    left = f"{_n(p['left'])} still to pass" if p["left"] else "all passed"
    return f"📝 *Placement Exams:* {marks} — +{_n(p['extra'])} hrs on top of the plan ({left})"


def placement_lines(plan: dict) -> list[str]:
    """The exams one by one, for the list of what is left."""
    p = placement(plan)
    if not p:
        return []
    head = "nothing added to the plan" if not p["extra"] else (
        f"+{_n(p['extra'])} hrs on top of the plan" + (f", {_n(p['left'])} still to pass" if p["left"] else ", all passed"))
    lines = ["", f"*Placement Exams* — {head}"]
    for course in p["courses"]:
        icon, outcome = _OUTCOMES[course["status"]]
        hours = "" if course["status"] == "exempt" else f" · {_n(course['hours'])}h"
        lines.append(f"{LRM}{icon} {_subject(course)}{hours} — {outcome}")
    return lines


def _n(value: float) -> str:
    return f"{value:g}"


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def progress_lines(plan: dict, retakes: int = 0, repeated: int = 0) -> list[str]:
    """The degree progress for the summary. `retakes`: how many of this term's courses are retakes;
    `repeated`: how many courses of the record were ever taken again (both from the transcript)."""
    p = progress(plan)
    lines = [f"📊 *Degree Plan:* {_n(p['degree'])} hrs", f"✅ *Passed:* {_n(p['passed'])} hrs"]
    if p["term_courses"]:
        again = f" ({_count(retakes, 'retake')})" if retakes else ""
        lines.append(f"⏳ *This Term:* {_n(p['term'])} hrs · {_count(p['term_courses'], 'course')}{again}")
    if p["failed_courses"]:
        lines.append(f"❌ *Failed, Not Retaken Yet:* {_n(p['failed'])} hrs · {_count(p['failed_courses'], 'course')}")
    if repeated:
        passed_again = max(repeated - retakes, 0)
        detail = " · ".join(part for part in (f"{passed_again} done" if passed_again else "",
                                              f"{retakes} this term" if retakes else "") if part)
        lines.append(f"🔁 *Repeated Courses:* {repeated}" + (f" ({detail})" if detail else ""))
    left = f"🏁 *Remaining:* {_n(max(p['remaining'], 0))} hrs"
    if p["term_courses"] and p["after"] != p["remaining"]:
        left += f" · {_n(max(p['after'], 0))} after this term"
    exams = placement(plan)
    if exams and exams["left"]:
        left += f" · +{_n(exams['left'])} remedial"
    return lines + [left] + [line for line in (placement_line(plan),) if line]


def _course(course: dict) -> str:
    icon, note = {"in_progress": ("⏳", " — this term"), "failed": ("❌", " — failed, retake")}.get(
        course["status"], ("▫️", ""))
    return f"{LRM}{icon} {FSI}{escape_md(course['name'] or course['code'])}{PDI} · {_n(course['hours'])}h{note}"


def remaining_lines(plan: dict) -> list[str]:
    """What is still to take, category by category: this term's courses first, then the failed ones,
    then the rest. A category that offers more than it requires is a choice ("choose from"); one
    with nothing left is only named at the end."""
    p = progress(plan)
    head = f"🏁 *{_n(max(p['remaining'], 0))} hrs* left of {_n(p['degree'])}"
    if p["term_courses"] and p["after"] != p["remaining"]:
        head += f" · *{_n(max(p['after'], 0))} hrs* after this term"
    lines = ["📚 *Remaining Courses*", head]
    complete = []
    order = {"in_progress": 0, "failed": 1}
    for category in _counted(plan):
        title = escape_md(english(category["name"]))
        if category["remaining"] <= 0:
            complete.append(title)
            continue
        open_courses = sorted((c for c in category["courses"] if c["status"] not in DONE),
                              key=lambda c: order.get(c["status"], 2))
        offered = sum(c["hours"] for c in category["courses"])
        choice = " · choose from:" if offered > category["required"] else ""
        lines += ["", f"*{title}* — {_n(category['remaining'])} of {_n(category['required'])} hrs left{choice}"]
        lines += [_course(c) for c in open_courses]
    lines += placement_lines(plan)
    if complete:
        lines += ["", "✅ *Complete:* " + ", ".join(complete)]
    lines += ["", "_⏳ this term · ❌ failed, retake · ▫️ not taken yet_"]
    return lines
