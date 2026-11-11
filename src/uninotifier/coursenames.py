"""
A course keeps its identity when the university renames it on Moodle.

The timetable, the instructor, the archive folder and the calculator all know a course by the
name in schedule.json. Moodle's name for it can change mid-term — another language, other words —
but its Moodle id does not. So each check remembers, per id, the timetable course it was matched
to; when a name comes that matches nothing while its id is already known, the new name becomes an
alias of that course (learn()), and schedule.canonical_course() reads the aliases (alias()).

State: course_names.json — {"ids": {moodle id: timetable course}, "aliases": {moodle name: timetable course}}.
Course names only; nothing personal.
"""

import json
import logging

from uninotifier import fileio, paths

log = logging.getLogger("uninotifier")

STATE_FILE = paths.DATA / "course_names.json"
_cache: dict = {"stamp": None, "state": None}


def load() -> dict:
    """The saved names; read again only when the file changed."""
    try:
        stamp = STATE_FILE.stat().st_mtime_ns
    except OSError:
        return {"ids": {}, "aliases": {}}
    if _cache["stamp"] != stamp:
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state = {"ids": dict(state.get("ids") or {}), "aliases": dict(state.get("aliases") or {})}
        except (OSError, ValueError, AttributeError, TypeError):
            state = {"ids": {}, "aliases": {}}
        _cache.update(stamp=stamp, state=state)
    return _cache["state"]


def alias(name: str) -> str | None:
    """The timetable course a renamed Moodle course is, when it was learnt."""
    return load()["aliases"].get(name)


def learn(courses: dict[str, str], canonical, known: set[str]) -> list[tuple[str, str]]:
    """`courses`: {moodle id: the course's name as Moodle has it now (without term and section)}.
    `canonical(name)` maps a name to its timetable course, `known` is every timetable course.
    Remembers the course of each id that matched; a name that matched nothing under a known id
    becomes an alias. Returns the aliases learnt now, as (new name, timetable course)."""
    state = load()
    ids, aliases = dict(state["ids"]), dict(state["aliases"])
    learnt = []
    for course_id, name in courses.items():
        matched = canonical(name)
        if matched in known:
            ids[course_id] = matched
        elif ids.get(course_id) in known:
            aliases[name] = ids[course_id]
            learnt.append((name, ids[course_id]))
    if (ids, aliases) != (state["ids"], state["aliases"]):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            fileio.write_json_atomic(STATE_FILE, {"ids": ids, "aliases": aliases})
        except OSError as e:
            log.warning("Could not save the course names (%s)", e)
            return []
    for name, course in learnt:
        log.info("Course renamed on Moodle: %s is %s", name, course)
    return learnt
