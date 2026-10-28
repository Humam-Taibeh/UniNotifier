"""
Tidy the semester archive: remove duplicate copies and put files where the
current routing rules say they belong. A dry run unless --apply.

  1. Duplicates — within one folder, files with the same base name ("lecture.pdf",
     "lecture (1).pdf", "lecture (2).pdf") and byte-identical content (size, then
     SHA-256) are collapsed into one, keeping the clean name. A file whose content
     differs is never touched, whatever its name.
  2. Misfiled — every downloaded resource is checked against archive.route(): a
     "suggested questions" sheet saved among the lecture notes moves to the
     assignments folder, and so on. A file the bot once put in 00_Schedules
     goes back to its course and loses its "<Course> - " prefix.
  3. saved_items.json follows: records of deleted copies point at the surviving
     file, records of moved files at their new place.

00_Schedules is the student's own folder: its files are never
de-duplicated, and only files the bot itself recorded are moved out of it.
Nothing leaves the semester folder (archive.ensure_inside_archive), nothing is
overwritten (a clash gets "(1)"), and it refuses to run while a monitor check
holds monitor.lock.

    .venv\\Scripts\\python -m uninotifier.tools.tidy_archive            # show what it would do
    .venv\\Scripts\\python -m uninotifier.tools.tidy_archive --apply    # do it
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from uninotifier import archive
from uninotifier import fileio
from uninotifier.tools.migrate_archive import LOCK_FILE, STATE_FILE, acquire_lock, free_path, rel

_COPY = re.compile(r" \(\d+\)(?=\.[^.]*$|$)")


def base_name(path: Path) -> str:
    """'lecture (2).pdf' -> 'lecture.pdf'."""
    return _COPY.sub("", path.name)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_duplicates(root: Path) -> list[tuple[Path, list[Path]]]:
    """[(file to keep, [redundant copies])] — per folder, same base name, same bytes.
    The keeper is the clean name if there is one, else the oldest copy."""
    found = []
    personal = root / archive.GENERAL
    for folder in [root, *[p for p in root.rglob("*") if p.is_dir()]]:
        if folder == personal or personal in folder.parents:
            continue
        if archive.OLD_VERSIONS in folder.relative_to(root).parts:
            continue                                    # replaced versions are history: kept as they are
        groups: dict[tuple[str, int], list[Path]] = {}
        for path in folder.iterdir():
            if path.is_file():
                groups.setdefault((base_name(path), path.stat().st_size), []).append(path)
        for files in groups.values():
            if len(files) < 2:
                continue
            by_content: dict[str, list[Path]] = {}
            for path in files:
                by_content.setdefault(sha256(path), []).append(path)
            for same in by_content.values():
                if len(same) > 1:
                    same.sort(key=lambda p: (archive.is_numbered_copy(p), p.stat().st_mtime, p.name))
                    found.append((same[0], same[1:]))
    return found


def misfiled(state: dict, redirect: dict[Path, Path]) -> list[tuple[Path, Path]]:
    """[(current path, where it belongs)] for downloaded resources in the wrong folder."""
    moves = []
    seen = set()
    for item_id, record in state.get("files", {}).items():
        item = state.get("content", {}).get(item_id, {})
        current = redirect.get(Path(record.get("path", "")), Path(record.get("path", "")))
        if item.get("type") != "resource" or not current.is_file() or current in seen:
            continue
        folder = archive.route(item.get("course", ""), item["type"], item.get("name", ""))
        if current.parent == folder:
            continue
        name = current.name
        prefix = archive.english_course_name(item.get("course", "")) + " - "
        if current.parent.name == archive.GENERAL and name.startswith(prefix):
            name = name[len(prefix):]                    # back from the old shared folder
        seen.add(current)
        moves.append((current, folder / name))
    return moves


def tidy(apply: bool) -> int:
    root = archive.ARCHIVE_ROOT
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    print(f"{'APPLYING' if apply else 'DRY RUN (nothing is changed)'} — {root}\n")

    redirect: dict[Path, Path] = {}                      # deleted copy -> surviving file
    print("1. Duplicate copies (same name, identical content):")
    duplicates = find_duplicates(root)
    for keeper, copies in duplicates:
        for copy in copies:
            print(f"   delete  {rel(copy)}   (identical to {rel(keeper)})")
            redirect[copy] = keeper
            if apply:
                archive.ensure_inside_archive(copy).unlink()
    if not duplicates:
        print("   none")

    print("\n2. Files in the wrong folder:")
    moves = misfiled(state, redirect)
    moved: dict[Path, Path] = {}
    for src, dst in moves:
        dst = free_path(archive.ensure_inside_archive(dst))
        print(f"   move    {rel(src)}  ->  {rel(dst)}")
        if apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
        moved[src] = dst
    if not moves:
        print("   none")

    changed = 0
    for record in state.get("files", {}).values():
        path = Path(record.get("path", ""))
        new = moved.get(redirect.get(path, path), redirect.get(path, path))
        if new != path:
            record["path"], changed = str(new), changed + 1
    print(f"\n3. Saved state: {changed} recorded path(s) updated.")
    if apply and changed:
        with fileio.write_lock(STATE_FILE):          # the bot changes this file too (Mark as done)
            fileio.write_json_atomic(STATE_FILE, state)
    print("\nDone." if apply else "\nThat was a dry run. Run again with --apply to do it.")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="do it (default: only show)")
    args = parser.parse_args()
    if args.apply and not acquire_lock():
        print("A monitor check is running (monitor.lock is held). Try again in a minute.")
        return 3
    try:
        return tidy(args.apply)
    finally:
        if args.apply:
            LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
