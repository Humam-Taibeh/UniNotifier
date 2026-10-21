"""
One-time migration of the semester archive to the current layout (see archive.py):

    Schedules_And_General/                 (new, shared, at the semester root)
    <Course in English>/01-Lectures_And_Slides
                        02-Assignments_And_Projects
                        03-Exams_And_Quizzes

  * course folders are renamed to their English names (Arabic -> English,
    "Databases" -> "Database Systems", "Numerical Analysis(1)" -> "Numerical Analysis (1)");
  * each course's old 01-Schedules_And_General is emptied into the shared
    folder, files renamed "<Course> - <file>";
  * 02-/03-/04- subfolders become 01-/02-/03-;
  * the favicon.ico files an earlier version of the downloader saved in place
    of real lecture files (identical copies of the portal's icon) are removed and
    forgotten, so the next check downloads the real files.

Safe by construction: a dry run unless --apply; everything stays inside the
semester folder (archive.ensure_inside_archive); nothing is overwritten (a name
clash gets "(1)"); only folders that are empty after the moves are removed; it
refuses to run while a monitor check holds monitor.lock.

    .venv\\Scripts\\python -m uninotifier.tools.migrate_archive            # show the plan
    .venv\\Scripts\\python -m uninotifier.tools.migrate_archive --apply    # do it
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from uninotifier import archive
from uninotifier import fileio
from uninotifier import paths

ROOT_DIR = Path(__file__).parent
STATE_FILE = paths.DATA / "saved_items.json"
LOCK_FILE = paths.DATA / "monitor.lock"
LOCK_STALE_SECONDS = 30 * 60

OLD_GENERAL = "01-Schedules_And_General"
OLD_TO_NEW = {"02-Lectures_And_Slides": archive.LECTURES,
              "03-Assignments_And_Projects": archive.ASSIGNMENTS,
              "04-Exams_And_Quizzes": archive.EXAMS}
FAVICON_NAME = re.compile(r"^favicon(?: \(\d+\))?\.ico$", re.I)
IMAGE_MAGIC = (b"\x00\x00\x01\x00", b"\x89PNG")   # an .ico, or a PNG named .ico (the portal's is one)


def rel(path: Path) -> str:
    try:
        return str(Path(path).relative_to(archive.ARCHIVE_ROOT))
    except ValueError:
        return str(path)


def free_path(path: Path) -> Path:
    """`path`, or `path` with " (n)" before the extension if something is there."""
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def acquire_lock() -> bool:
    """The monitor's run lock, so a check can't start while files are moving."""
    try:
        if time.time() - LOCK_FILE.stat().st_mtime < LOCK_STALE_SECONDS:
            return False
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, b"migrate_archive")
    os.close(fd)
    return True


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_favicon_junk(state: dict) -> dict[str, Path]:
    """Recorded downloads that are really the portal's favicon: {item id: path}.
    Only when every candidate is the very same small image — identical content
    in unrelated lecture items can't be their content."""
    candidates = {}
    for item_id, record in state.get("files", {}).items():
        path = Path(record.get("path", ""))
        item = state.get("content", {}).get(item_id, {})
        if item.get("type") == "resource" and FAVICON_NAME.match(path.name) and path.is_file():
            candidates[item_id] = path
    if not candidates:
        return {}
    if len({sha256(p) for p in candidates.values()}) != 1:
        print("   the favicon files differ from one another — leaving them alone")
        return {}
    if not all(p.read_bytes().startswith(IMAGE_MAGIC) for p in candidates.values()):
        print("   the favicon files aren't images — leaving them alone")
        return {}
    return candidates


def plan_course(course_dir: Path, general: Path) -> dict:
    """What to do for one course folder, using the *final* names throughout:
      rename   (course_dir, final_dir) or None
      moves    [(old path, final path, is_dir)] — old paths are under course_dir
      empties  old folders to remove once their contents are gone
      creates  folders to create
      notes    things left alone"""
    english = archive.english_course_name(course_dir.name)
    final = archive.ARCHIVE_ROOT / english
    plan = {"english": english, "final": final, "rename": None, "moves": [], "empties": [],
            "creates": [], "notes": []}
    if course_dir.name != english:
        if final.exists() and final.resolve() != course_dir.resolve():
            plan["notes"].append(f"{english} already exists next to {course_dir.name} — left unmerged")
            return plan
        plan["rename"] = (course_dir, final)

    old_general = course_dir / OLD_GENERAL
    if old_general.is_dir():
        for entry in sorted(old_general.iterdir()):
            plan["moves"].append((entry, general / f"{english} - {entry.name}", entry.is_dir()))
        plan["empties"].append(old_general)
    for old, new in OLD_TO_NEW.items():
        if (course_dir / old).is_dir():
            plan["moves"].append((course_dir / old, final / new, True))
    for sub in archive.SUBFOLDERS:
        coming = any((course_dir / old).is_dir() for old, new in OLD_TO_NEW.items() if new == sub)
        if not coming and not (course_dir / sub).exists():
            plan["creates"].append(final / sub)
    known = {OLD_GENERAL, *OLD_TO_NEW, *archive.SUBFOLDERS}
    extra = [p.name for p in course_dir.iterdir() if p.name not in known]
    if extra:
        plan["notes"].append(f"{english}: left in place (not part of the layout): {', '.join(extra)}")
    return plan


def migrate(apply: bool) -> int:
    root = archive.ARCHIVE_ROOT
    if not root.is_dir():
        print(f"The semester folder doesn't exist: {root}")
        return 1
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    print(f"{'APPLYING' if apply else 'DRY RUN (nothing is changed)'} — {root}\n")

    junk = find_favicon_junk(state)
    if junk:
        print(f"1. Remove {len(junk)} favicon.ico file(s) saved instead of real lecture files, and forget them:")
        for item_id, path in junk.items():
            print(f"   delete  {rel(path)}   ({state['content'][item_id].get('name', '')[:40]})")
            if apply:
                path.unlink()
                del state["files"][item_id]
    else:
        print("1. No misdownloaded favicon files.")

    general = root / archive.GENERAL
    print("\n2. Course folders -> English names, three subfolders each:")
    if not general.exists():
        print(f"   create  {archive.GENERAL}")
        if apply:
            general.mkdir()

    path_map: list[tuple[Path, Path]] = []       # old path prefix -> final path prefix
    notes: list[str] = []

    def move(src: Path, dst: Path) -> bool:
        """Rename on disk. A folder another program has open (an Explorer window,
        a viewer) can't be renamed: say so and carry on — re-run after closing it."""
        if not apply:
            return True
        try:
            os.rename(src, dst)
            return True
        except OSError as e:
            notes.append(f"could not move {rel(src)} ({e.strerror}) — close whatever has it open and run again")
            print(f"   !! skipped: {e.strerror}")
            return False
    for course_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name != archive.GENERAL):
        plan = plan_course(course_dir, general)
        notes += plan["notes"]
        print(f"\n  {course_dir.name}" + (f"  =>  {plan['english']}" if plan["rename"] else ""))
        if plan["rename"]:
            src, dst = plan["rename"]
            archive.ensure_inside_archive(dst)
            print(f"   rename  {rel(src)}  ->  {rel(dst)}")
            renamed = move(src, dst)
        else:
            renamed = False
        if plan["rename"] and apply and not renamed:
            continue            # the course folder couldn't be renamed: leave its contents as they are for now
        current = plan["final"] if (apply and plan["rename"] and renamed) else course_dir   # where things are *now*
        for src, dst, is_dir in plan["moves"]:
            dst = free_path(archive.ensure_inside_archive(dst))
            actual = current / src.relative_to(course_dir)
            print(f"   {'folder' if is_dir else 'file  '}  {rel(src)}  ->  {rel(dst)}")
            if move(actual, dst):
                path_map.append((plan["final"] / src.relative_to(course_dir), dst))   # keyed by the final course name
        for old in plan["empties"]:
            actual = current / old.relative_to(course_dir)
            if apply and (not actual.exists() or any(actual.iterdir())):
                if actual.exists():
                    notes.append(f"kept {rel(old)} — it still holds files")
                continue
            print(f"   remove empty folder  {rel(old)}")
            if apply:
                actual.rmdir()
        for folder in plan["creates"]:
            print(f"   create  {rel(folder)}")
            if apply:
                archive.ensure_inside_archive(folder).mkdir(parents=True, exist_ok=True)

    def remap(path_text: str) -> str:
        """Where a recorded file lives after the migration: first the course
        folder's English name, then the subfolder / general-folder moves."""
        path = Path(path_text)
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            return path_text
        if len(parts) > 1 and parts[0] != archive.GENERAL:
            path = root / archive.english_course_name(parts[0]) / Path(*parts[1:])
        for old, new in sorted(path_map, key=lambda m: -len(str(m[0]))):
            try:
                return str(new / path.relative_to(old))
            except ValueError:
                continue
        return str(path)

    changed = 0
    for record in state.get("files", {}).values():
        new_path = remap(record.get("path", ""))
        if new_path != record.get("path"):
            changed += 1
            record["path"] = new_path
    print(f"\n3. Saved state: {changed} recorded file path(s) follow the moves.")
    if apply and (changed or junk):
        with fileio.write_lock(STATE_FILE):          # the bot changes this file too (Mark as done)
            fileio.write_json_atomic(STATE_FILE, state)

    if notes:
        print("\nNotes:")
        for note in notes:
            print("  -", note)
    print("\nDone." if apply else "\nThat was a dry run. Run again with --apply to do it.")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")        # Arabic folder names on a Windows console
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="do the moves (default: only show them)")
    args = parser.parse_args()
    if args.apply and not acquire_lock():
        print("A monitor check is running (monitor.lock is held). Try again in a minute.")
        return 3
    try:
        return migrate(args.apply)
    finally:
        if args.apply:
            LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
