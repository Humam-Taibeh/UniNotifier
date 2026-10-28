"""
Guest look-up: read SOMEONE ELSE's academic record from the ZUJ registration
portal and print a summary — the courses they passed, the ones they failed
and still owe, their CGPA, passed hours and status, this term's courses and
retakes, and what this term must average for a few CGPA targets.

Nothing of yours is touched: no Telegram message, no gpa.json, no saved
state, no portal_debug/ dump. The password is typed hidden and never stored;
the verification code the portal sends goes to THEIR phone / e-mail, so they
read it out when asked.

Run (on this PC):
    .venv\\Scripts\\python -m uninotifier.guest               # asks for username + password
    .venv\\Scripts\\python -m uninotifier.guest --save DIR    # also writes report.txt + the screenshot
"""

import argparse
import getpass
import sys
from pathlib import Path

from uninotifier import gpa
from uninotifier import gradebook
from uninotifier import portal

TARGETS = (60, 65, 70, 75, 80, 85)


def _mark(course: dict) -> str:
    return gpa.fmt(course["mark"]) if course["mark"] is not None else "—"


def _owed(history: list[dict]) -> list[dict]:
    """Failed courses with no later pass of the same course."""
    passed = [c for sem in history for c in sem["courses"] if c["passed"]]
    owed = []
    for sem in history:
        for c in sem["courses"]:
            if c["passed"] is False and not any(gradebook._same_course(c, p) for p in passed) \
                    and not any(gradebook._same_course(c, o) for o in owed):
                owed.append({**c, "term": sem["term"]})
    return owed


def targets(semesters: list[dict]) -> list[tuple[float, dict]]:
    """For each CGPA target: gpa.plan over this term's courses, with the record
    before this term as the base and retakes replacing their old attempt."""
    if not semesters or not semesters[0]["courses"]:
        return []
    base_cgpa, base_hours = gradebook.cumulative(gradebook.attempts(semesters[1:]))
    report = gradebook.season_report(semesters, {})
    courses = {}
    for c in report["courses"]:
        courses[f"{c['code']} {c['name']}"] = {
            "hours": c["hours"] or gpa.DEFAULT_HOURS, "grade": None, "portal_grade": c["grade"],
            "retake": c["replaces"] is not None, "old_grade": c["replaces"], "syllabus": None, "marks": {}}
    return [(t, gpa.plan(courses, t, "cgpa", base_cgpa, base_hours)) for t in TARGETS]


DEFAULT_TERM_HOURS = 18            # a full load, when no term is in progress to measure one by
TRAJECTORY_TERMS = (1, 2, 3)


def trajectory(semesters: list[dict], target: float, terms: tuple[int, ...] = TRAJECTORY_TERMS) -> dict:
    """What a student must average to reach a cumulative `target`, if they keep that average for 1, 2 or 3
    terms of the same load as the one in progress (a full load when none is). The university's own rules:
    every attempt counts, marks under 35 count as 35, a course being retaken this term leaves the record
    when it is replaced. -> {"in_progress", "term_hours", "base": (cgpa, hours), "rows": [(terms, hours,
    needed, verdict)]}; verdict: "secured" | "pass" (passing every course is enough) | "ok" | "out" (over 100)."""
    in_progress = bool(semesters) and any(c["passed"] is None for c in semesters[0]["courses"])
    current = semesters[0]["courses"] if in_progress else []
    history = semesters[1:] if in_progress else semesters
    kept = [a for a in gradebook.attempts(history)
            if not any(gradebook._same_course(a, c) for c in current)]        # a retake replaces its old attempt
    base, hours = gradebook.cumulative(kept)
    term_hours = sum(c["hours"] for c in current) or DEFAULT_TERM_HOURS
    rows = []
    for n in terms:
        added = n * term_hours
        needed = (target * (hours + added) - (base or 0.0) * hours) / added
        verdict = ("secured" if needed <= 0 else "pass" if needed <= gpa.PASS_MARK
                   else "out" if needed > 100 else "ok")
        rows.append((n, added, needed, verdict))
    return {"in_progress": in_progress, "term_hours": term_hours, "base": (base, hours), "rows": rows}


def build_report(username: str, result: portal.Result) -> str:
    semesters = (result.data or {}).get("semesters") or []
    f = result.figures
    out = [f"ZUJ academic record — {username}", "=" * 44]
    if not semesters:
        out.append("The transcript page was reached, but no courses could be read from it.")
        return "\n".join(out)

    cgpa = f.get("record_cgpa", f.get("cgpa"))
    if cgpa is not None:
        shown = f"  (the portal shows {gpa.fmt(f['cgpa'])}%)" if "cgpa" in f and "record_cgpa" in f else ""
        out.append(f"Cumulative GPA:       {cgpa:.2f}%{shown}")
    if "passed_hours" in f:
        out.append(f"Passed credit hours:  {gpa.fmt(f['passed_hours'])}")
    if "record_hours" in f:
        out.append(f"Hours in the GPA:     {gpa.fmt(f['record_hours'])}  (every attempt; failures count as 35)")
    if result.data.get("status"):
        out.append(f"Academic status:      {portal.english_status(result.data['status'])}")

    current, history = semesters[0], semesters[1:]
    in_progress = any(c["passed"] is None for c in current["courses"])
    done_terms = history if in_progress else semesters
    passed = [c for sem in done_terms for c in sem["courses"] if c["passed"]]
    out += ["", f"COURSES TAKEN — {len(passed)} passed"]
    for sem in reversed(done_terms):
        rows = [c for c in sem["courses"] if c["passed"] is not None]
        if not rows:
            continue
        out.append(f"  {sem['term']}" + (f"  — term average {gpa.fmt(sem['average'])}" if sem.get("average") else ""))
        for c in rows:
            out.append(f"    {'✓' if c['passed'] else '✗'} {c['code']}  {c['name']}  · {gpa.fmt(c['hours'])}h · {_mark(c)}")

    owed = _owed(done_terms)
    if owed:
        out += ["", "FAILED AND NOT YET PASSED"]
        retaking = current["courses"] if in_progress else []
        for c in owed:                           # same code or same name: a renumbered course counts
            now = "  → being retaken this term" if any(gradebook._same_course(c, r) for r in retaking) else ""
            out.append(f"    ✗ {c['code']}  {c['name']}  · {_mark(c)} ({c['term']}){now}")

    if in_progress:
        hours = sum(c["hours"] for c in current["courses"])
        out += ["", f"THIS TERM — {current['term']}: {len(current['courses'])} courses, {gpa.fmt(hours)} hours"]
        report = gradebook.season_report(semesters, {})
        for c in report["courses"]:
            extra = f"  🔁 retake — replaces {gpa.fmt(c['replaces'])}" if c["replaces"] is not None else ""
            out.append(f"    • {c['code']}  {c['name']}  · {gpa.fmt(c['hours'])}h{extra}")
        plans = targets(semesters)
        if plans:
            out += ["", "TO REACH A CUMULATIVE GPA OF … THIS TERM MUST AVERAGE"]
            for target, plan in plans:
                status = plan["status"]
                text = ("already secured" if status == "secured" else
                        f"out of reach (would need {gpa.fmt(plan['needed'])})" if status == "out_of_reach" else
                        "final" if status == "done" else
                        f"{gpa.fmt(plan['needed'])}% — passing every course ({gpa.fmt(gpa.PASS_MARK)}) is enough"
                        if plan["needed"] < gpa.PASS_MARK else f"{gpa.fmt(plan['needed'])}%")
                out.append(f"    {target}%  →  {text}")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read someone else's ZUJ transcript — nothing of yours is touched.")
    parser.add_argument("--save", metavar="DIR", help="also write report.txt and transcript.png there")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    username = input("Guest's portal username (student ID): ").strip()
    password = getpass.getpass("Guest's portal password (hidden, never stored): ")
    if not username or not password:
        sys.exit("Both are needed.")

    def ask_code(attempt: int, rejected: bool) -> str | None:
        if rejected:
            print("The portal didn't accept that code.")
        return input("Verification code the portal sent to the guest (Enter to give up): ").strip() or None

    print("Signing in to the registration portal…")
    try:
        result = portal.fetch_many(["transcript"], ask_code, creds=(username, password),
                                   keep_debug=False)["transcript"]
    except portal.PortalError as e:
        sys.exit(f"Stopped: {e}")
    except Exception as e:                       # maintenance, network, … — say what, no traceback
        sys.exit(f"Couldn't read the portal: {e}")

    text = build_report(username, result)
    print("\n" + text)
    if args.save:
        folder = Path(args.save)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "report.txt").write_text(text, encoding="utf-8")
        if result.png:
            (folder / "transcript.png").write_bytes(result.png)
        print(f"\nSaved to {folder.resolve()}")


if __name__ == "__main__":
    main()
