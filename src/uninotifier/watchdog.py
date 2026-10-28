"""
Layout watchdog: notices when the university's pages stop reading the way
they used to — a theme update, a renamed field — instead of failing silently.

A changed page rarely raises an error; it just reads as *less*: a course that
had 46 items suddenly has 2, the course page's text is gone, no forum can be
read, the course list comes back empty. Left alone, that is the worst kind of
failure: new assignments go unannounced, and once the parser is fixed every
"vanished" item would be announced again as new.

So after each scan the monitor compares what it read with what it already
knew, course by course:

  collapsed course   far fewer items than known (below COLLAPSE_RATIO of at
                     least COLLAPSE_MIN_KNOWN) — the course is treated as
                     unreadable this run: its known items are kept, nothing is
                     re-announced later
  lost page text     items that used to carry text now carry none
  forums unreadable  every forum failed to read
  no courses         the enrolled-course list came back empty

Anything found is reported once (🧩 Moodle looks different, repeated at most
every 6 hours) and followed by ✅ when the pages read normally again.
"""

class LayoutChanged(Exception):
    """The scan read nothing where something was expected (no courses at all)."""


COLLAPSE_MIN_KNOWN = 4        # a course must have had this many items to count as collapsed
COLLAPSE_RATIO = 0.5          # ... and now read fewer than this share of them
TEXT_MIN_KNOWN = 3            # items with text a course must have had before "lost text" counts
REPEAT_SECONDS = 6 * 3600     # the alert is repeated at most this often while it lasts
ACCEPT_SECONDS = 3 * 86400    # a course read short for this long is the new normal (items really removed)


def course_id(item_id: str) -> str:
    return item_id.split("-", 1)[0]


def counts(content: dict, cid: str) -> tuple[int, int]:
    """(items, items with text) of one course — section summaries left out."""
    rows = [it for iid, it in content.items() if course_id(iid) == cid and it.get("type") != "section"]
    return len(rows), sum(1 for it in rows if it.get("hash"))


def collapsed(found: int, known: int) -> bool:
    return known >= COLLAPSE_MIN_KNOWN and found < known * COLLAPSE_RATIO


def check_courses(courses: dict, found: dict, known: dict) -> tuple[list[str], list[str]]:
    """Compare one scan with the saved state. Returns (collapsed course ids,
    human-readable findings)."""
    from uninotifier.media import clean_course_name
    gone, findings = [], []
    for cid, course in courses.items():
        n_found, text_found = counts(found, cid)
        n_known, text_known = counts(known, cid)
        name = clean_course_name(course.get("name", cid))
        if collapsed(n_found, n_known):
            gone.append(cid)
            findings.append(f"{name}: {n_found} item(s) read, {n_known} known")
        elif text_known >= TEXT_MIN_KNOWN and text_found == 0 and n_found:
            findings.append(f"{name}: the course page's text could not be read")
    return gone, findings


def check_forums(forum_posts: dict) -> list[str]:
    """Every forum unreadable (None) in one run."""
    if forum_posts and all(posts is None for posts in forum_posts.values()):
        return [f"none of the {len(forum_posts)} forums could be read"]
    return []


def alert_text(findings: list[str]) -> str:
    from uninotifier.notifier import escape_md
    lines = ["🧩 *Moodle looks different*", "",
             "Some pages didn't read the way they used to — the university may have changed the site:"]
    lines += [f"• {escape_md(f)}" for f in findings]
    lines += ["", "To be safe, the items already known are kept (nothing will be announced twice), "
              "but new items in these places may be missed until this is fixed. "
              "Details are in `data/logs/monitor.log`."]
    return "\n".join(lines)


RECOVERED_TEXT = "✅ *Moodle reads normally again* — every course came back as expected."


def accepted_text(names: list[str]) -> str:
    from uninotifier.notifier import escape_md
    return ("ℹ️ *Accepted as the new normal:* " + escape_md(", ".join(names)) +
            " has read the same way for 3 days, so its removed items are taken as really removed.")


def review(state: dict, gone: list[str], findings: list[str], courses: dict, now: float) -> tuple[list[str], list[str]]:
    """Track how long each course has been read short (state is saved["layout"]).
    Returns (course ids still to protect, names accepted as the new normal now)."""
    since = state.setdefault("short_since", {})
    for cid in list(since):
        if cid not in gone:
            del since[cid]                                  # reads normally again
    protect, accepted = [], []
    for cid in gone:
        first = since.setdefault(cid, now)
        if now - first >= ACCEPT_SECONDS:
            from uninotifier.media import clean_course_name
            accepted.append(clean_course_name(courses.get(cid, {}).get("name", cid)))
        else:
            protect.append(cid)
    for cid in [c for c in gone if c not in protect]:
        since.pop(cid, None)
    findings[:] = [f for f in findings if not any(f.startswith(f"{name}:") for name in accepted)]
    return protect, accepted
