"""
Online-lecture meeting links (Teams / Zoom / Google Meet).

A course with an "Online" slot in schedule.json gets one meeting link for
the semester. It is found automatically in whatever the instructor posts
(labels, section summaries, descriptions, pages, URL activities, forum
posts), and rides on every lecture reminder as
a "🎥 Join Meeting" button, next to a "🌐 Open Course Page" button and a
screenshot of the post or section the link was published in.

Rule: the newest link wins. Every link ever seen for a course is
remembered, so a link counts as new only the first time it appears; an
old link still sitting in a label can't override a newer one posted later
(or one set by hand with set_link()).

State lives in lecture_links.json (written by monitor.py and bot.py, each
with an atomic replace), separate from saved_items.json so a link change can't
be overwritten by a monitor run that is in progress. The screenshots sit in
lecture_link_shots/ beside it (taken during the monitor run that found the
link, because a reminder is sent later with no browser at hand).
"""

import json
import logging
import re
import time
from pathlib import Path

from uninotifier import archive
from uninotifier import fileio
from uninotifier import media
from uninotifier import moodle
from uninotifier import richtext
from uninotifier import schedule
from uninotifier import paths

log = logging.getLogger("uninotifier")

LINKS_FILE = paths.DATA / "lecture_links.json"
JOIN_BUTTON = "🎥 Join Meeting"
PAGE_BUTTON = "🌐 Open Course Page"

MEETING_RE = re.compile(
    r"https?://(?:[\w-]+\.)*(?:teams\.microsoft\.com/(?:l/meetup-join|meet)/|teams\.live\.com/meet/|"
    r"zoom\.us/(?:j|my|w|s)/|meet\.google\.com/[a-z]{3}-)[^\s<>\"'\])]*", re.I)


def is_meeting_url(url: str) -> bool:
    return bool(url and MEETING_RE.fullmatch(url.strip()))


def meeting_urls(candidates: list[str]) -> list[str]:
    found = []
    for text in candidates:
        found += [m.group(0).rstrip(".,;") for m in MEETING_RE.finditer(text or "")]
    return list(dict.fromkeys(found))


def load() -> dict:
    """{course: {"url", "source": "portal"|"manual", "origin", "portal_url", "shot",
    "set_at", "seen": [...]}}"""
    try:
        return json.loads(LINKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(links: dict) -> None:
    fileio.write_json_atomic(LINKS_FILE, links)


def get(course: str) -> str | None:
    return load().get(course, {}).get("url")


def entry(course: str) -> dict:
    return load().get(course, {})


def _shots_dir() -> Path:
    return LINKS_FILE.with_name("lecture_link_shots")


def save_shot(course: str, png: bytes) -> None:
    """Keep the screenshot of the post that published the course's current link."""
    links = load()
    if not links.get(course, {}).get("url"):
        return
    _shots_dir().mkdir(exist_ok=True)
    name = archive.safe_name(course, "course") + ".png"
    (_shots_dir() / name).write_bytes(png)
    links[course]["shot"] = name
    _save(links)


def shot_bytes(course: str) -> bytes | None:
    name = entry(course).get("shot")
    try:
        return (_shots_dir() / name).read_bytes() if name else None
    except OSError:
        return None


def set_link(course: str, url: str | None, source: str, origin: str = "") -> None:
    """Store (or, with url=None, forget) a course's lecture link."""
    links = load()
    entry = links.setdefault(course, {"seen": []})
    if url:
        entry.update(url=url, source=source, origin=origin, set_at=time.time())
        for key in ("portal_url", "shot"):   # a hand-set link has no post to show
            entry.pop(key, None)
        if url not in entry["seen"]:
            entry["seen"].append(url)
    else:
        for key in ("url", "source", "origin", "set_at", "portal_url", "shot"):
            entry.pop(key, None)
    _save(links)


def online_courses(sched: dict | None = None) -> list[str]:
    """Courses with at least one online slot, in week order."""
    sched = sched or schedule.load_schedule()
    modes = set(sched.get("link_modes", ["Online"]))
    found = [lec["course"] for day in schedule.DAY_KEYS[6:] + schedule.DAY_KEYS[:6]
             for lec in sched.get("days", {}).get(day, []) if lec["mode"] in modes]
    return list(dict.fromkeys(found))


def online_slots(course: str) -> str:
    """'Wed 7:00 PM–8:30 PM' for the course's online slots."""
    from uninotifier.notifier import hhmm12
    return ", ".join(f'{schedule.DAY_ABBR[day]} {hhmm12(lec["start"])}–{hhmm12(lec["end"])}'
                     for day, lec in schedule.lectures_for(course) if lec["mode"] == "Online")


def _course_anchor(item: dict) -> str:
    """Where "Open Course Page" should land for a content item. A URL activity's
    own page redirects straight to the meeting, so it points at its card on
    the course page instead."""
    if item.get("type") == "url" and item.get("course_id") and item.get("cmid"):
        return f'{moodle.course_url(item["course_id"])}#module-{item["cmid"]}'
    return item["url"]


def candidates(content: dict, forum_posts: dict) -> dict[str, list[dict]]:
    """course -> [{"url", "origin", "portal_url", "shot": (page url, selector)}],
    newest sources first: forum posts (latest first), then course content."""
    out: dict[str, list[dict]] = {}
    for forum_id, posts in forum_posts.items():
        forum = content.get(forum_id)
        if not forum or not posts:
            continue
        course = media.clean_course_name(forum["course"])
        for post in sorted(posts, key=lambda p: p["time"], reverse=True):
            for url in meeting_urls(richtext.links(post["html"])):
                out.setdefault(course, []).append({
                    "url": url, "origin": f'forum post "{post["subject"] or forum["name"]}"',
                    "portal_url": post["url"],
                    "shot": (post["url"].split("#", 1)[0], f'#p{post["id"]}')})
    for item in content.values():
        sources = richtext.links(item.get("_body", "")) + [item.get("target") or ""]
        for url in meeting_urls(sources):
            out.setdefault(media.clean_course_name(item["course"]), []).append({
                "url": url, "origin": f'"{item["name"]}"', "portal_url": _course_anchor(item),
                "shot": moodle.screenshot_target(item)})
    return out


def update_from_portal(content: dict, forum_posts: dict) -> list[dict]:
    """Record meeting links that appeared on the portal for the first time.
    Returns the changes as [{"course", "url", "origin", "portal_url", "shot"}]
    for the caller to screenshot and announce."""
    online = set(online_courses())
    links, changes, dirty = load(), [], False
    for course, found in candidates(content, forum_posts).items():
        if course not in online:
            continue
        entry = links.setdefault(course, {"seen": []})
        fresh = [f for f in found if f["url"] not in entry["seen"]]
        if not fresh:
            continue
        dirty = True
        entry["seen"] += list(dict.fromkeys(f["url"] for f in fresh))
        best = fresh[0]
        if best["url"] != entry.get("url"):
            entry.pop("shot", None)   # the old screenshot belongs to the old link
            entry.update(url=best["url"], source="portal", origin=best["origin"],
                         portal_url=best["portal_url"], set_at=time.time())
            changes.append({"course": course, **best})
            log.info("Lecture link for %s found in %s", course, best["origin"])
    if dirty:
        _save(links)
    return changes


def join_rows(course_entry: dict) -> list[list[dict]]:
    """The reminder's buttons: join the meeting, and open the portal page
    where the link was published (when that is known)."""
    rows = [[{"text": JOIN_BUTTON, "url": course_entry["url"]}]]
    if course_entry.get("portal_url"):
        rows.append([{"text": PAGE_BUTTON, "url": course_entry["portal_url"]}])
    return rows
