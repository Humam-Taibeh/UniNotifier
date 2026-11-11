"""The course archive: downloads, folders, versions, what is announced."""

from support import (_package)

import importlib
import json
import os
import tempfile
from pathlib import Path

from uninotifier import archive
from uninotifier import media


def test_archive():
    assert archive.course_folder_name("20261-Big Data-AllSec-3-") == "Big Data"
    assert archive.course_folder_name("20261-Linear Algebra (1)-AllSec-3-F") == "Linear Algebra (1)"
    assert archive.course_folder_name("20261-الاتصال والتواصل باللغة العربية-ش31-3-ك") == "الاتصال والتواصل باللغة العربية"
    # the university renames a course mid-term, with another section tag: still the course's own name
    assert archive.course_folder_name("20261-Technical Writing-Sec12-3-E") == "Technical Writing"
    # a picture of the portal never hides the content it is taken of (it came out blank white)
    import inspect
    from uninotifier.monitor import items as monitor_items
    assert "e.contains(main)" in monitor_items._EXPAND_JS and "_SHOW_JS" in inspect.getsource(monitor_items.capture_screens)
    assert archive.categorize("quiz", "x") == archive.EXAMS
    assert archive.categorize("resource", "Midterm 2025") == archive.EXAMS
    assert archive.categorize("resource", "Course Syllabus") == archive.LECTURES
    assert archive.categorize("resource", "Chapter 3 slides") == archive.LECTURES
    assert archive.categorize("assign", "HW1") == archive.ASSIGNMENTS


def test_archive_layout():
    """English course folders with exactly three numbered subfolders. The semester
    root's 00_Schedules is the student's own: nothing is filed there."""
    assert (archive.GENERAL, archive.LECTURES, archive.ASSIGNMENTS, archive.EXAMS) == (
        "00_Schedules", "01_Lectures", "02_Assignments", "03_Exams")
    assert archive.SUBFOLDERS == (archive.LECTURES, archive.ASSIGNMENTS, archive.EXAMS)

    # portal names -> the English folder, whatever language or spelling the portal uses
    for portal_name, english in (
            ("20261-الاتصال والتواصل باللغة الإنجليزية-ش27-3-و", "English Communication Skills"),
            ("20261-الاتصال والتواصل باللغة العربية-ش31-3-ك", "Arabic Communication Skills"),
            ("20261-Databases-AllSec-3-F", "Database Systems"),
            ("20261-Numerical Analysis(1)-AllSec-3-", "Numerical Analysis (1)"),
            ("20261-Big Data-AllSec-3-", "Big Data"),
            ("20261-Some New Course-AllSec-3-", "Some New Course")):
        assert archive.english_course_name(portal_name) == english, portal_name
    assert archive.english_course_name("الاتصال والتواصل باللغة العربية") == "Arabic Communication Skills", \
        "a folder name already on disk maps the same way"

    # routing: what goes where
    assert archive.categorize("resource", "Exam Schedule") == archive.EXAMS
    assert archive.categorize("resource", "جدول الامتحانات النهائية") == archive.EXAMS
    assert archive.categorize("resource", "Midterm exam dates") == archive.EXAMS
    assert archive.categorize("resource", "Midterm 2025 solutions") == archive.EXAMS
    assert archive.categorize("quiz", "Exam schedule quiz") == archive.EXAMS
    for plan in ("Course Syllabus", "Big Data Course Plan for Bachelor program 0135321 (1)", "خطة المادة",
                 "Course Plan - Projects", "Final exam syllabus"):
        assert archive.categorize("resource", plan) == archive.LECTURES, plan
    assert archive.categorize("resource", "Lecture 3 slides") == archive.LECTURES
    assert archive.categorize("resource", "Homework 2") == archive.ASSIGNMENTS

    root = archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED
    with tempfile.TemporaryDirectory() as tmp:
        archive.ARCHIVE_ROOT = Path(tmp) / "01-First_Semester"
        archive._ARCHIVE_ROOT_RESOLVED = archive.ARCHIVE_ROOT.resolve()
        try:
            arabic = "20261-الاتصال والتواصل باللغة العربية-ش31-3-ك"
            base = archive.ensure_course_tree(arabic)
            assert base.name == "Arabic Communication Skills"
            assert sorted(p.name for p in base.iterdir()) == [
                "01_Lectures", "02_Assignments", "03_Exams"], "strictly three"
            assert sorted(p.name for p in archive.ARCHIVE_ROOT.iterdir()) == ["Arabic Communication Skills"]
            assert not (base / "00_Schedules").exists() and not list(base.glob("*chedules*"))

            assert archive.target(arabic, "resource", "Lecture 1") == base / archive.LECTURES
            assert archive.target(arabic, "resource", "Course Syllabus") == base / archive.LECTURES
            assert archive.target(arabic, "resource", "Exam dates") == base / archive.EXAMS
            assert archive.target("20261-Databases-AllSec-3-F", "assign", "Project 1") == \
                archive.ARCHIVE_ROOT / "Database Systems" / archive.ASSIGNMENTS
            assert not (archive.ARCHIVE_ROOT / archive.GENERAL).exists(), "the personal folder is never created or used"
            try:
                archive.ensure_inside_archive(Path(tmp) / "02-Other_Semester" / "x")
            except archive.ArchiveBoundaryError:
                pass
            else:
                raise AssertionError("the boundary guard must still hold")
        finally:
            archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED = root


def test_migration_helpers():
    from uninotifier.tools import migrate_archive as m

    assert m.FAVICON_NAME.match("favicon.ico") and m.FAVICON_NAME.match("favicon (13).ico")
    assert not m.FAVICON_NAME.match("favicon-lecture.ico") and not m.FAVICON_NAME.match("slides.pdf")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "a.pdf").write_bytes(b"1")
        assert m.free_path(tmp / "b.pdf") == tmp / "b.pdf" and m.free_path(tmp / "a.pdf") == tmp / "a (1).pdf"
        # favicon detection: only identical image copies of recorded resources count
        icon = b"\x89PNG\r\n\x1a\nrest"
        for n in ("favicon.ico", "favicon (1).ico"):
            (tmp / n).write_bytes(icon)
        state = {"files": {"1-1": {"path": str(tmp / "favicon.ico")}, "1-2": {"path": str(tmp / "favicon (1).ico")},
                           "1-3": {"path": str(tmp / "a.pdf")}},
                 "content": {"1-1": {"type": "resource"}, "1-2": {"type": "resource"}, "1-3": {"type": "resource"}}}
        assert sorted(m.find_favicon_junk(state)) == ["1-1", "1-2"]
        (tmp / "favicon (1).ico").write_bytes(icon + b"different")
        assert m.find_favicon_junk(state) == {}, "copies that differ are not touched"
        (tmp / "favicon (1).ico").write_bytes(icon)
        state["content"]["1-2"]["type"] = "assign"
        assert sorted(m.find_favicon_junk(state)) == ["1-1"], "only downloaded resources are considered"


def test_downloader_dedup_and_routing():
    class Resp:
        def __init__(self, body, url="https://x/pluginfile.php/1/mod_resource/content/1/Lecture.pdf", ctype="application/pdf"):
            self._body, self.url, self.ok = body, url, True
            self.headers = {"content-type": ctype, "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"}

        def body(self):
            return self._body

        def text(self):
            return ""

    class Ctx:
        def __init__(self, body):
            self.body = body
            self.request = self

        def get(self, url, timeout=0):
            return Resp(self.body)

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "Big Data" / "01_Lectures"
        folder.mkdir(parents=True)
        old_root = archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED
        archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED = Path(tmp), Path(tmp).resolve()
        try:
            first = archive.download_resource(Ctx(b"PDF-A" * 100), "https://x/mod/resource/view.php?id=1", folder)
            assert first["path"].name == "Lecture.pdf" and not first.get("duplicate")
            # a second Moodle item with the very same bytes: nothing is written
            again = archive.download_resource(Ctx(b"PDF-A" * 100), "https://x/mod/resource/view.php?id=2", folder)
            assert again["duplicate"] and again["path"] == first["path"] and again["sha256"] == first["sha256"]
            assert [p.name for p in folder.iterdir()] == ["Lecture.pdf"], "no 'Lecture (1).pdf'"
            # different bytes under the same name: a real new file, numbered
            other = archive.download_resource(Ctx(b"PDF-B" * 100), "https://x/mod/resource/view.php?id=3", folder)
            assert not other.get("duplicate") and other["path"].name == "Lecture (1).pdf"
            # ...and its exact copy is recognised too, preferring the clean name when both match
            assert archive.download_resource(Ctx(b"PDF-B" * 100), "u", folder)["path"].name == "Lecture (1).pdf"
            # an instructor's re-upload (tag set) is never swallowed: the caller compares versions itself
            updated = archive.download_resource(Ctx(b"PDF-A" * 100), "u", folder, tag="updated 2026-10-12")
            assert not updated.get("duplicate") and "updated" in updated["path"].name

            # the shared general folder: only the same course's files count
            general = Path(tmp) / archive.GENERAL
            general.mkdir()
            a = archive.download_resource(Ctx(b"SYL"), "u", general, prefix="Big Data - ")
            b = archive.download_resource(Ctx(b"SYL"), "u", general, prefix="Linear Algebra (1) - ")
            c = archive.download_resource(Ctx(b"SYL"), "u", general, prefix="Big Data - ")
            assert not a.get("duplicate") and not b.get("duplicate") and c["duplicate"] and c["path"] == a["path"]
        finally:
            archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED = old_root

    # practice questions / problem sheets are assignment material, not lecture notes
    for name in ("اسئلة مقترحة", "أسئلة مقترحة", "Practice questions", "Suggested Questions", "Exercises 3",
                 "Problem sheet 2", "Worksheet", "تمارين الفصل الأول"):
        assert archive.categorize("resource", name) == archive.ASSIGNMENTS, name
    assert archive.categorize("resource", "Midterm questions") == archive.EXAMS, "exam words still win"
    assert archive.categorize("resource", "Chapter 2 lecture notes") == archive.LECTURES
    assert archive.categorize("resource", "Review") == archive.LECTURES and archive.categorize("resource", "ملخص المادة") == archive.LECTURES
    assert archive.categorize("resource", "Course Syllabus") == archive.LECTURES


def test_tidy_archive():
    from uninotifier.tools import tidy_archive as t

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "01-First_Semester"
        old = archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED
        archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED = root, root.resolve()
        try:
            lectures = root / "Big Data" / archive.LECTURES
            general = root / archive.GENERAL
            lectures.mkdir(parents=True)
            general.mkdir()
            (lectures / "Notes.pdf").write_bytes(b"same")
            (lectures / "Notes (1).pdf").write_bytes(b"same")
            (lectures / "Notes (2).pdf").write_bytes(b"same")
            (lectures / "Derivative.docx").write_bytes(b"version one")
            (lectures / "Derivative (1).docx").write_bytes(b"a different document")   # NOT a duplicate
            (lectures / "Sheet.docx").write_bytes(b"questions")
            (general / "Big Data - Syllabus.docx").write_bytes(b"syl")
            (general / "Big Data - Syllabus (1).docx").write_bytes(b"syl")
            (general / "My timetable.pdf").write_bytes(b"mine")
            (general / "My timetable (1).pdf").write_bytes(b"mine")

            assert (t.base_name(Path("a (2).pdf")), t.base_name(Path("a.pdf")), t.base_name(Path("b (c).pdf"))) == \
                ("a.pdf", "a.pdf", "b (c).pdf")
            dupes = {k.name: sorted(c.name for c in cs) for k, cs in t.find_duplicates(root)}
            # the personal folder is never de-duplicated, whatever it holds
            assert dupes == {"Notes.pdf": ["Notes (1).pdf", "Notes (2).pdf"]}, dupes
            # the keeper is the clean name even when a numbered copy is older
            os.utime(lectures / "Notes.pdf", (2e9, 2e9))
            assert [k.name for k, _ in t.find_duplicates(root) if k.parent == lectures] == ["Notes.pdf"]

            course = "20261-Big Data-AllSec-3-"
            state = {"files": {"1": {"path": str(lectures / "Sheet.docx")},
                               "2": {"path": str(general / "Big Data - Syllabus (1).docx")},
                               "3": {"path": str(lectures / "Notes.pdf")}},
                     "content": {"1": {"type": "resource", "name": "اسئلة مقترحة", "course": course},
                                 "2": {"type": "resource", "name": "Course Syllabus", "course": course},
                                 "3": {"type": "resource", "name": "Lecture notes", "course": course}}}
            moves = t.misfiled(state, {})
            # a syllabus the bot once put in the shared folder goes back to its course, without the prefix;
            # the student's own files there (not in the state) are not touched
            assert moves == [(lectures / "Sheet.docx", root / "Big Data" / archive.ASSIGNMENTS / "Sheet.docx"),
                             (general / "Big Data - Syllabus (1).docx", lectures / "Syllabus (1).docx")], moves
        finally:
            archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED = old


def test_auto_done_only_for_saved_files_and_links():
    """"Mark as done" is ticked automatically only on static material that really
    landed; graded work, forums, pages and labels are never ticked."""
    monitor = _package("uninotifier.monitor")
    ticked = []
    orig = monitor.moodle.set_completion
    monitor.moodle.set_completion = lambda ctx, key, cmid, done: ticked.append(cmid) or True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            saved = Path(tmp) / "Lecture 1.pdf"
            saved.write_bytes(b"%PDF")

            def tick(kind, artifact, n):
                item = {"type": kind, "cmid": str(n), "manual": True, "done": False}
                monitor.auto_complete(None, "k", item, artifact)
                return item["done"]

            assert tick("resource", {"kind": "file", "path": str(saved)}, 1)
            assert tick("folder", {"kind": "file", "path": tmp}, 2)
            assert tick("url", {"kind": "link", "target": "https://youtu.be/x"}, 3)
            assert not tick("resource", {"kind": "file", "path": str(Path(tmp) / "gone.pdf")}, 4)
            assert not tick("resource", {"kind": "unknown"}, 5)          # download failed
            assert not tick("url", {"kind": "unknown"}, 6)               # link not resolved
            for n, kind in enumerate(("assign", "quiz", "forum", "page", "label", "book"), 10):
                assert not tick(kind, {"kind": "file", "path": str(saved)}, n), kind
            assert ticked == ["1", "2", "3"], ticked
    finally:
        monitor.moodle.set_completion = orig
    assert media.CONTAINER_TYPES == {"forum"}


def test_announce_first_run_and_duplicate_files():
    """monitor.py --announce reports an empty state's first scan; an empty forum
    stays silent, a second Moodle item for an already saved file is not attached again."""
    monitor = _package("uninotifier.monitor")
    course = "20261-Big Data-AllSec-3-"
    items = {"1-1": {"name": "Lecture 1", "type": "resource", "course": course, "cmid": "1", "url": "https://x/mod/resource/view.php?id=1"},
             "1-2": {"name": "Lecture 1 (again)", "type": "resource", "course": course, "cmid": "2", "url": "https://x/mod/resource/view.php?id=2"},
             "1-3": {"name": "Announcements", "type": "forum", "course": course, "cmid": "3", "url": "https://x/mod/forum/view.php?id=3"}}
    seen = {}

    def fake_scan(saved, notify_new, mfa_handler=None):
        seen["notify_new"] = notify_new
        return {"courses": {"1": {"name": course, "url": "https://x/c"}}, "content": dict(items),
                "deadlines": {}, "artifacts": {}, "updates": {}, "edits": {}, "posts": {},
                "forum_posts": {"1-3": []}, "submissions": {}, "link_changes": [], "seconds": 1.0,
                "browser_used": False}

    with tempfile.TemporaryDirectory() as tmp:
        keep = {k: getattr(monitor, k) for k in ("DATA_FILE", "USERNAME", "PASSWORD", "BOT_TOKEN", "CHAT_ID",
                                                 "run_scan", "notify_new_item", "send_schedule_messages",
                                                 "mfa_allowed", "notify_new_course")}
        monitor.DATA_FILE = Path(tmp) / "saved_items.json"
        monitor.USERNAME = monitor.PASSWORD = monitor.BOT_TOKEN = monitor.CHAT_ID = "x"
        monitor.send_schedule_messages = lambda saved: None
        monitor.mfa_allowed = lambda saved: False
        monitor.notify_new_course = lambda course: True
        monitor.run_scan = fake_scan
        sent = []
        monitor.notify_new_item = lambda it, deadline, art, submitted=None: sent.append(it["name"]) or True
        try:
            monitor.run()                                   # plain first run: silent baseline
            assert seen["notify_new"] is False and sent == []
            monitor.DATA_FILE.unlink()
            monitor.run(announce=True)                      # --announce: everything is reported
            assert seen["notify_new"] is True and sent == ["Lecture 1", "Lecture 1 (again)"], sent
            state = json.loads(monitor.DATA_FILE.read_text(encoding="utf-8"))
            assert set(state["content"]) == set(items)      # the forum is known, just not announced
        finally:
            for k, v in keep.items():
                setattr(monitor, k, v)

    docs = []
    orig = monitor.deliver
    monitor.deliver = lambda header, body="", url=None, rows=None, shots=(), shot_name="", document=None: \
        docs.append((header, document)) or True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "Lecture 1.pdf"
            pdf.write_bytes(b"%PDF")
            item = items["1-1"]
            monitor.notify_new_item(item, None, {"kind": "file", "path": str(pdf), "size": 4})
            monitor.notify_new_item(item, None, {"kind": "file", "path": str(pdf), "size": 4, "duplicate": True})
    finally:
        monitor.deliver = orig
    assert docs[0][1] == pdf and docs[1][1] is None and "not attached again" in docs[1][0]


def test_an_announced_item_is_never_announced_again():
    """A run that reads a course incompletely (the browser fallback has no section
    descriptions) makes items vanish for one run. When they come back they are not new."""
    monitor = _package("uninotifier.monitor")
    course = "20261-Linear Algebra (1)-AllSec-3-F"
    full = {f"1-{n}": {"name": f"Item {n}", "type": "label", "course": course, "course_id": "1", "cmid": str(n),
                       "url": f"https://x/mod/label/view.php?id={n}"} for n in (1, 2)}
    reads = [full, {"1-1": full["1-1"]}, full]          # known, partial read, known again

    def fake_scan(saved, notify_new, mfa_handler=None):
        return {"courses": {"1": {"name": course, "url": "https://x/c"}}, "content": dict(reads.pop(0)),
                "deadlines": {}, "artifacts": {}, "updates": {}, "edits": {}, "posts": {}, "forum_posts": {},
                "submissions": {}, "link_changes": [], "seconds": 1.0, "browser_used": False}

    with tempfile.TemporaryDirectory() as tmp:
        keep = {k: getattr(monitor, k) for k in ("DATA_FILE", "USERNAME", "PASSWORD", "BOT_TOKEN", "CHAT_ID",
                                                 "run_scan", "notify_new_item", "send_schedule_messages",
                                                 "mfa_allowed", "notify_new_course")}
        monitor.DATA_FILE = Path(tmp) / "saved_items.json"
        monitor.USERNAME = monitor.PASSWORD = monitor.BOT_TOKEN = monitor.CHAT_ID = "x"
        monitor.send_schedule_messages = lambda saved: None
        monitor.mfa_allowed = lambda saved: False
        monitor.notify_new_course = lambda course: True
        monitor.run_scan = fake_scan
        sent = []
        monitor.notify_new_item = lambda it, deadline, art, submitted=None: sent.append(it["name"]) or True
        try:
            monitor.run()                                   # baseline
            monitor.run()                                   # Item 2 missing this time
            monitor.run()                                   # and back
            assert sent == [], sent
        finally:
            for k, v in keep.items():
                setattr(monitor, k, v)


def test_archive_boundary():
    root = archive.ARCHIVE_ROOT
    assert archive.ensure_inside_archive(root / "Big Data" / "x.pdf")
    for bad in (root.parent / "Other_Semester", root / ".." / "02-Second_Semester",
                Path("C:/Windows/x.txt")):
        try:
            archive.ensure_inside_archive(bad)
        except archive.ArchiveBoundaryError:
            continue
        raise AssertionError(f"{bad} should be rejected")
    assert archive._unique_path(root / "Big Data", "a.pdf", "updated 2026-10-10").name ==         "a (updated 2026-10-10).pdf"


def test_windows_device_names_are_saved_as_files():
    """A lecturer's file called "CON", "nul.pdf" or "Com1.txt" would be written to a Windows device
    (or fail) instead of the archive: such names get a "_" in front; ordinary names are untouched,
    and no name can still climb out of its folder."""
    for reserved in ("CON", "nul.pdf", "Com1.txt", "LPT9", "aux .doc", "PRN.tar.gz", "CONOUT$"):
        assert archive.safe_name(reserved) == "_" + reserved, reserved
    for ordinary in ("console.pdf", "Lecture CON.pdf", "auxiliary.docx", "com10.txt", "nullable.py", "Big Data"):
        assert archive.safe_name(ordinary) == ordinary, ordinary
    assert archive.safe_name("../../x") == "_.._x" and archive.safe_name("..") == "item"
    assert archive.safe_name("a" * 300) == "a" * 120
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / archive.safe_name("CON")
        path.write_bytes(b"lecture")
        assert path.read_bytes() == b"lecture" and [p.name for p in Path(tmp).iterdir()] == ["_CON"]


def test_replaced_files_move_to_old_versions():
    """An instructor's new version takes the file's place in its folder; the old one moves, unchanged,
    into _Old_Versions beside it (created only then) as <name>_v<n> (<its date>). The record keeps the
    latest file and every retired one, so neither is downloaded again. A run that repeats (its
    announcement failed) moves and copies nothing twice; the same bytes with a new date change no
    file; a new version under another name retires the old name. The tidy tool leaves the old
    versions alone, and the message says where the previous file went. A file two items share is
    never retired: the updated item's new version goes beside it."""
    from datetime import datetime
    from uninotifier.tools import tidy_archive
    items = importlib.import_module("uninotifier.monitor.items")

    class Resp:
        def __init__(self, body, name):
            self._body, self.ok = body, True
            self.url = f"https://x/pluginfile.php/1/mod_resource/content/1/{name}"
            self.headers = {"content-type": "application/pdf", "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"}

        def body(self):
            return self._body

        def text(self):
            return ""

    class Ctx:
        def __init__(self, body, name="Lecture.pdf"):
            self._resp = Resp(body, name)
            self.request = self

        def get(self, url, timeout=0):
            return self._resp

    def day(ts):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    item = {"course": "20261-Big Data-AllSec-3-F", "type": "resource", "name": "Chapter 1", "url": "https://x/mod/resource/view.php?id=7"}
    t1, t2, t3 = 1_790_000_000, 1_790_500_000, 1_791_000_000
    with tempfile.TemporaryDirectory() as tmp:
        keep = (archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED, archive.probe_resource)
        archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED = Path(tmp), Path(tmp).resolve()
        probe = {"modified": t2, "size": None}
        archive.probe_resource = lambda context, url: dict(probe)
        try:
            folder = archive.target(item["course"], "resource", item["name"])
            first = archive.download_resource(Ctx(b"V1" * 200), item["url"], folder)
            record = {"path": str(first["path"]), "size": 400, "sha256": first["sha256"], "modified": t1}
            old_versions = folder / archive.OLD_VERSIONS
            assert not old_versions.exists(), "no _Old_Versions until a file is actually replaced"

            # version 2: in the file's place; version 1 retired, unchanged
            art = items.check_file_update(Ctx(b"V2" * 200), item, record)
            main = folder / "Lecture.pdf"
            retired_1 = old_versions / f"Lecture_v1 ({day(t1)}).pdf"
            assert main.read_bytes() == b"V2" * 200 and retired_1.read_bytes() == b"V1" * 200
            assert sorted(p.name for p in folder.iterdir()) == sorted([archive.OLD_VERSIONS, "Lecture.pdf"]), "no stray copies"
            assert art["path"] == str(main.resolve()) and art["old_path"] == str(retired_1.resolve())
            assert art["versions"] == [{"path": str(retired_1.resolve()), "size": 400, "sha256": record["sha256"], "modified": t1}]

            # the announcement failed, the record stayed: the next run finds everything in place
            again = items.check_file_update(Ctx(b"V2" * 200), item, record)
            assert again["path"] == art["path"] and again["versions"] == art["versions"]
            assert sorted(p.name for p in folder.iterdir()) == sorted([archive.OLD_VERSIONS, "Lecture.pdf"])
            assert [p.name for p in old_versions.iterdir()] == [retired_1.name], "nothing moved or copied twice"

            # recorded as run.py does; version 3 retires version 2 beside version 1
            record = {"path": art["path"], "size": art["size"], "sha256": art["sha256"], "modified": art["modified"],
                      "versions": art["versions"]}
            probe["modified"] = t3
            art = items.check_file_update(Ctx(b"V3" * 200), item, record)
            assert main.read_bytes() == b"V3" * 200
            assert sorted(p.name for p in old_versions.iterdir()) == sorted([retired_1.name, f"Lecture_v2 ({day(record['modified'])}).pdf"]), "named after the replaced file's own date"
            assert [v["sha256"] for v in art["versions"]] == [first["sha256"], record["sha256"]], "both old versions tracked"

            # the same bytes with a newer date: no file changes, only the record's date
            record = {**record, "path": art["path"], "sha256": art["sha256"], "modified": t3, "versions": art["versions"]}
            probe["modified"] = t3 + 60
            assert items.check_file_update(Ctx(b"V3" * 200), item, record) is None and record["modified"] == t3 + 60
            assert len(list(folder.iterdir())) == 2 and len(list(old_versions.iterdir())) == 2

            # a new version under another name: the old name is retired, the new one takes the folder
            probe["modified"] = t3 + 3600
            art = items.check_file_update(Ctx(b"V4" * 200, "Lecture-revised.pdf"), item, record)
            assert sorted(p.name for p in folder.iterdir()) == sorted([archive.OLD_VERSIONS, "Lecture-revised.pdf"])
            assert (old_versions / f"Lecture_v3 ({day(record['modified'])}).pdf").read_bytes() == b"V3" * 200
            again = items.check_file_update(Ctx(b"V4" * 200, "Lecture-revised.pdf"), item, record)
            assert again["path"] == art["path"] and sorted(p.name for p in folder.iterdir()) == \
                sorted([archive.OLD_VERSIONS, "Lecture-revised.pdf"]), "repeated: no 'Lecture-revised (1).pdf'"

            # the message: where the previous file went
            text = media.build_update_message(item, t3, t3 + 3600, old_name=Path(art["old_path"]).name,
                                              new_name="Lecture-revised.pdf", where=archive.destination(Path(art["path"])),
                                              retired_to=archive.retired_to(Path(art["old_path"])))
            assert f"`Big Data/01_Lectures/{archive.OLD_VERSIONS}/{Path(art['old_path']).name}`" in text
            assert "takes the previous file's place" in text and "beside the previous file" not in text
            assert "nothing was moved" in media.build_update_message(item, t1, t2, new_name="x.pdf")

            # the tidy tool never "de-duplicates" history
            (old_versions / "Lecture_v1 (copy).pdf").write_bytes(b"V1" * 200)
            assert not [k for k, _ in tidy_archive.find_duplicates(Path(tmp)) if archive.OLD_VERSIONS in k.parts]

            # one file, two items: a new version of one goes beside it; the file stays for the other
            got = archive.download_resource(Ctx(b"S1" * 200, "Shared.pdf"), item["url"], folder)
            shared_file = Path(got["path"])
            rec_a = {"path": str(shared_file), "size": 400, "sha256": got["sha256"], "modified": t1}
            files = {"a": rec_a, "b": dict(rec_a), "c": {"path": art["path"]}}
            shared = archive.shared_paths(files)
            assert shared == {os.path.normcase(str(shared_file))}
            assert archive.is_shared(rec_a, shared) and not archive.is_shared(files["c"], shared)
            probe["modified"] = t3 + 7200
            moved = items.check_file_update(Ctx(b"S2" * 200, "Shared.pdf"), item, rec_a, shared=True)
            assert shared_file.read_bytes() == b"S1" * 200, "the other item's file is untouched"
            new_file = Path(moved["path"])
            assert new_file.parent == folder.resolve() and new_file != shared_file.resolve()
            assert new_file.read_bytes() == b"S2" * 200
            assert moved["versions"] == [] and moved["old_path"] == rec_a["path"]
            assert not [p for p in old_versions.iterdir() if p.name.startswith("Shared")], "nothing retired"
            again = items.check_file_update(Ctx(b"S2" * 200, "Shared.pdf"), item, rec_a, shared=True)
            assert again["path"] == moved["path"]
            assert len([p for p in folder.iterdir() if p.name.startswith("Shared")]) == 2, "repeated: no third copy"
        finally:
            archive.ARCHIVE_ROOT, archive._ARCHIVE_ROOT_RESOLVED, archive.probe_resource = keep
