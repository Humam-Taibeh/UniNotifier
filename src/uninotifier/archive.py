"""
Local archive: per-course folder tree and file downloads.

Layout under ARCHIVE_ROOT (the semester folder):
    <Course in English>/01_Lectures         lectures, slides, syllabus / course plan
                        02_Assignments
                        03_Exams            exam papers, solutions, exam dates
                        <any of them>/_Old_Versions   files an instructor replaced (only once one is)
    00_Schedules/                           the student's own timetables and notes:
                                            nothing is ever filed or changed there
(the student's own names since 2026-10-20; before, 01-Lectures_And_Slides,
02-Assignments_And_Projects, 03-Exams_And_Quizzes and Schedules_And_General)

Course folders always carry the English name (schedule.json knows the Arabic
portal names), so a file never lands in a second folder for the same course.

Downloads reuse the authenticated Playwright session (context.request), so
no separate login is needed.
"""

import hashlib
import logging
import os
import re
import zipfile
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import dotenv_values

from uninotifier import paths
from uninotifier.zuj_auth import BASE_URL

log = logging.getLogger("uninotifier")

def _shell_documents() -> str:
    """Explorer's Documents folder as Windows records it (the "Personal" shell folder) - OneDrive's
    when OneDrive backs up Documents; "" when it can't be read."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            return os.path.expandvars(str(winreg.QueryValueEx(key, "Personal")[0])).strip()
    except (ImportError, OSError):
        return ""


def documents_folder() -> Path:
    """The Documents folder the student sees in Explorer: the shell folder, else OneDrive's Documents,
    else Documents in the home folder."""
    onedrive = (os.getenv("OneDrive") or "").strip()
    for candidate in (_shell_documents(), os.path.join(onedrive, "Documents") if onedrive else ""):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return Path.home() / "Documents"


# Where downloaded course files go: ARCHIVE_ROOT in .env (a semester folder of your own;
# %USERPROFILE% and ~ are expanded), or University in your Documents folder when it isn't set.
DEFAULT_ARCHIVE_ROOT = documents_folder() / "University"


def _archive_root() -> Path:
    raw = (os.getenv("ARCHIVE_ROOT") or dotenv_values(paths.ENV_FILE).get("ARCHIVE_ROOT") or "").strip().strip('"')
    return Path(os.path.expandvars(raw)).expanduser() if raw else DEFAULT_ARCHIVE_ROOT


ARCHIVE_ROOT = _archive_root()

_ARCHIVE_ROOT_RESOLVED = ARCHIVE_ROOT.resolve()


class ArchiveBoundaryError(Exception):
    """A write was attempted outside ARCHIVE_ROOT."""


def ensure_inside_archive(path: Path) -> Path:
    """Hard guard: every path this module writes must resolve inside
    ARCHIVE_ROOT (this semester only). Raises instead of writing elsewhere."""
    resolved = Path(path).resolve()
    if resolved != _ARCHIVE_ROOT_RESOLVED and _ARCHIVE_ROOT_RESOLVED not in resolved.parents:
        raise ArchiveBoundaryError(f"{resolved} is outside {_ARCHIVE_ROOT_RESOLVED}")
    return resolved


GENERAL = "00_Schedules"                    # the student's personal folder: never written to
LECTURES = "01_Lectures"
ASSIGNMENTS = "02_Assignments"
EXAMS = "03_Exams"
SUBFOLDERS = (LECTURES, ASSIGNMENTS, EXAMS)  # exactly these three inside every course

# Module types that carry downloadable files.
FILE_TYPES = {"resource", "folder"}

_EXAM_WORDS = re.compile(
    r"\bexams?\b|\bquiz(?:zes)?\b|\bmid(?:term)?\b|\bfinal\b|\btests?\b|امتحان|اختبار|كويز|نصفي|نهائي", re.I)
_ASSIGN_WORDS = re.compile(
    r"assign|homework|\bhw\d*\b|project|\blabs?\b|مشروع|واجب|تكليف|مختبر|"
    # practice questions and problem sheets belong with the assignments, not the lecture notes
    r"\bquestions?\b|\bexercises?\b|\bworksheets?\b|\bpractice\b|\bproblem\s+(?:sets?|sheets?)\b|"
    r"[أاإ]سئلة|[أاإ]سئله|تمارين|تمرين|مسائل|مسألة", re.I)
# a syllabus / course plan stays with the course's lectures, whatever else its title says
_PLAN_WORDS = re.compile(
    r"syllabus|\bcourse\s*(?:plan|outline|description|content)|\boutline\b|\bplan\b|"
    r"خطة|توصيف|مفردات", re.I)

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows device names: a file called "CON" or "nul.pdf" is not a file there (the write goes to
# the device, or fails) — with an extension too on older Windows, so the stem is what counts.
_RESERVED_NAMES = re.compile(r"(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)\s*(\..*)?", re.I)


def safe_name(name: str, fallback: str = "item") -> str:
    cleaned = _INVALID_CHARS.sub("_", name or "").strip(" .")[:120]
    if _RESERVED_NAMES.fullmatch(cleaned):
        cleaned = "_" + cleaned
    return cleaned or fallback


def course_folder_name(full_name: str) -> str:
    """'20261-Big Data-AllSec-3-' -> 'Big Data';
    '20261-الاتصال والتواصل باللغة العربية-ش12-3-ك' -> 'الاتصال والتواصل باللغة العربية'."""
    name = re.sub(r"^\d{4,6}-", "", full_name or "")
    name = re.split(r"-(?:AllSec|ش\d+)", name, maxsplit=1)[0]
    return safe_name(name, fallback=safe_name(full_name, "Course"))


def english_course_name(full_name: str) -> str:
    """The course's English folder name: '20261-الاتصال والتواصل باللغة العربية-ش12-3-ك'
    -> 'Arabic Communication Skills', '20261-Databases-AllSec-3-F' -> 'Database Systems'.
    Names schedule.json doesn't know are kept as the portal has them."""
    from uninotifier.schedule import canonical_course        # schedule imports this module: import late
    return safe_name(canonical_course(course_folder_name(full_name)), fallback=safe_name(full_name, "Course"))


def ensure_course_tree(course_name: str) -> Path:
    """The course's folder with its three subfolders, created if missing."""
    base = ARCHIVE_ROOT / english_course_name(course_name)
    ensure_inside_archive(base)
    for sub in SUBFOLDERS:
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def route(course_name: str, modname: str, name: str) -> Path:
    """Where a file belongs: the course's lectures / assignments / exams folder.
    Every downloaded file stays inside its course. Pure: creates nothing."""
    return ARCHIVE_ROOT / english_course_name(course_name) / categorize(modname, name)


def target(course_name: str, modname: str, name: str) -> Path:
    """route(), with the course's folders created if missing."""
    ensure_course_tree(course_name)
    return route(course_name, modname, name)


FOLDER_LABELS = {LECTURES: "Lectures & Slides", ASSIGNMENTS: "Assignments & Projects",
                 EXAMS: "Exams & Quizzes"}


def destination(path: Path) -> str | None:
    """Where a saved file was filed, for the Telegram message:
    '`Big Data/01_Lectures` (Lectures & Slides)'. None for a path
    outside the archive."""
    try:
        parts = Path(path).resolve().relative_to(_ARCHIVE_ROOT_RESOLVED).parts
    except ValueError:
        return None
    if len(parts) < 3:
        return None
    return f"`{parts[0]}/{parts[1]}` ({FOLDER_LABELS.get(parts[1], parts[1])})"


def retired_to(path: Path) -> str | None:
    """Where a retired version went, for the message: '`Big Data/01_Lectures/_Old_Versions/x_v1 (…).pdf`'."""
    try:
        parts = Path(path).resolve().relative_to(_ARCHIVE_ROOT_RESOLVED).parts
    except ValueError:
        return None
    return f"`{'/'.join(parts)}`"


def categorize(modname: str, name: str) -> str:
    if modname != "quiz" and _PLAN_WORDS.search(name):
        return LECTURES
    if modname in ("quiz",) or _EXAM_WORDS.search(name):
        return EXAMS
    if modname == "assign" or _ASSIGN_WORDS.search(name):
        return ASSIGNMENTS
    return LECTURES


def _filename_from_headers(resp, fallback_url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        return unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', cd)
    if m:
        raw = m.group(1)
        try:  # servers sometimes send UTF-8 bytes decoded as latin-1
            return raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return raw
    return unquote(Path(urlparse(fallback_url).path).name)


def _unique_path(folder: Path, filename: str, tag: str = "", prefix: str = "") -> Path:
    """A free path in folder; `tag` (e.g. "updated 2026-10-10") is appended to
    the stem so a modified file lands beside its older version; `prefix`
    ("<Course> - ") starts the file name."""
    target = folder / safe_name(prefix + filename, "file")
    stem, suffix, n = target.stem, target.suffix, 1
    if tag:
        stem = f"{stem} ({tag})"
        target = folder / f"{stem}{suffix}"
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return ensure_inside_archive(target)


def _is_html(resp) -> bool:
    return "text/html" in resp.headers.get("content-type", "")


def _file_link(page_html: str) -> str | None:
    """The real file's link on a resource's intermediate page. The page also
    links the site theme's favicon through pluginfile.php (and first), so
    the file is the mod_resource one, else the first link that isn't theme chrome."""
    links = [l.replace("&amp;", "&") for l in re.findall(r'href="([^"]*pluginfile\.php[^"]*)"', page_html)]
    return (next((l for l in links if "/mod_resource/" in l), None)
            or next((l for l in links if "/theme_" not in l and "favicon" not in l.lower()), None))


def _resolve_file_response(context, view_url: str, method: str = "get"):
    """Follow a /mod/resource/view.php link to the actual file response."""
    fetch = getattr(context.request, method)
    resp = fetch(view_url, timeout=120000)
    if resp.ok and _is_html(resp):
        # The resource is displayed on an intermediate page instead of
        # redirecting straight to the file — find the pluginfile link.
        page = context.request.get(view_url, timeout=120000) if method != "get" else resp
        link = _file_link(page.text())
        if not link:
            return None
        resp = fetch(link, timeout=120000)
    if not resp.ok or _is_html(resp):
        return None
    return resp


def _last_modified(resp) -> float | None:
    raw = resp.headers.get("last-modified")
    try:
        return parsedate_to_datetime(raw).timestamp() if raw else None
    except (TypeError, ValueError):
        return None


def probe_resource(context, view_url: str) -> dict | None:
    """Cheap change check (HEAD, no body): {"modified": ts|None, "size": int|None}."""
    try:
        resp = _resolve_file_response(context, view_url, "head")
    except Exception:
        log.exception("Could not probe %s", view_url)
        return None
    if resp is None:
        return None
    length = resp.headers.get("content-length")
    return {"modified": _last_modified(resp), "size": int(length) if length and length.isdigit() else None}


_COPY_SUFFIX = re.compile(r" \(\d+\)(?=\.[^.]*$|$)")


def is_numbered_copy(path: Path) -> bool:
    """'lecture (1).pdf' — a name that only differs from another by " (n)"."""
    return bool(_COPY_SUFFIX.search(path.name))


def find_identical(folder: Path, size: int, digest: str, prefix: str = "") -> Path | None:
    """A file already in `folder` with exactly these bytes (size first, then
    SHA-256), preferring a clean name over a numbered copy. With a `prefix`
    only that course's files count — the shared general folder holds every
    course's."""
    if not folder.is_dir():
        return None
    for path in sorted(folder.iterdir(), key=lambda p: (is_numbered_copy(p), p.name)):
        if (path.is_file() and path.stat().st_size == size and path.name.startswith(prefix)
                and hashlib.sha256(path.read_bytes()).hexdigest() == digest):
            return path.resolve()      # the same spelling _unique_path gives a new file
    return None


def download_resource(context, view_url: str, dest_folder: Path, tag: str = "", prefix: str = "") -> dict | None:
    """Download the file behind a /mod/resource/view.php link.
    Returns {"path", "sha256", "modified"} or None.

    Two Moodle items often point at the very same file. If the folder already
    holds these exact bytes, nothing is written: the result points at the
    existing file and carries "duplicate": True. (Not for a re-upload —
    `tag` set — where the caller compares against the old version itself.)"""
    resp = _resolve_file_response(context, view_url)
    if resp is None:
        return None
    body = resp.body()
    digest = hashlib.sha256(body).hexdigest()
    if not tag:
        existing = find_identical(dest_folder, len(body), digest, prefix)
        if existing is not None:
            return {"path": existing, "sha256": digest, "modified": _last_modified(resp), "duplicate": True}
    filename = _filename_from_headers(resp, resp.url)
    target = _unique_path(dest_folder, filename, tag, prefix)
    target.write_bytes(body)
    return {"path": target, "sha256": digest, "modified": _last_modified(resp), "filename": filename}


# ------------------------------------------------------------------ a re-uploaded file's versions
#
# An instructor's new version takes the file's place in its folder; the version it replaces moves,
# unchanged, into an _Old_Versions folder beside it (created only when that first happens), named
# "<name>_v<n> (<its date>)<ext>". Every step can be repeated: a run whose announcement failed and
# runs again finds the files already in place instead of moving or copying them twice. A file that
# other items still point at (two Moodle items, one file) is never retired: the new version goes
# beside it under its own name.

OLD_VERSIONS = "_Old_Versions"


def shared_paths(files: dict) -> set[str]:
    """The archived files (normcased paths) that more than one saved_items "files" record points at."""
    seen: set[str] = set()
    shared: set[str] = set()
    for record in files.values():
        if isinstance(record, dict) and record.get("path"):
            key = os.path.normcase(str(record["path"]))
            (shared if key in seen else seen).add(key)
    return shared


def is_shared(record: dict, shared: set[str]) -> bool:
    return bool(record.get("path")) and os.path.normcase(str(record["path"])) in shared


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def retire(path: Path, modified: float | None = None) -> Path:
    """Move a superseded file into <its folder>/_Old_Versions as <stem>_v<n> (<date>)<ext>."""
    from datetime import datetime

    folder = Path(path).parent / OLD_VERSIONS
    ensure_inside_archive(folder)
    folder.mkdir(exist_ok=True)                    # on demand: only when a version is actually retired
    stem, suffix = Path(path).stem, Path(path).suffix
    taken = {p.name for p in folder.iterdir()}
    n = 1 + sum(1 for name in taken if name.startswith(f"{stem}_v"))
    day = datetime.fromtimestamp(modified).strftime("%Y-%m-%d") if modified else datetime.now().strftime("%Y-%m-%d")
    while (name := f"{stem}_v{n} ({day}){suffix}") in taken:
        n += 1
    return Path(path).replace(ensure_inside_archive(folder / name))


def find_retired(folder: Path, size: int | None, digest: str | None) -> Path | None:
    """The retired copy of these bytes in <folder>/_Old_Versions, if there is one."""
    if not digest or size is None:
        return None
    return find_identical(Path(folder) / OLD_VERSIONS, size, digest)


def put_new_version(staged: Path, filename: str, dest_folder: Path, old_path: Path | None,
                    old: dict, prefix: str = "", shared: bool = False) -> tuple[Path, Path | None]:
    """The downloaded new version (`staged`, beside the others under a temporary name) takes its
    place; the old file (`old_path`, whose record is `old`: sha256/size/modified) is retired.
    `shared`: other records still point at the old file, so it stays where it is.
    Returns (the new file, the retired old one — None when it was not on disk or is shared)."""
    digest, size = _sha256(staged), staged.stat().st_size
    old_path = Path(old_path) if old_path else None
    if old_path is not None and old_path.is_file() and old_path.resolve() != staged.resolve() \
            and _sha256(old_path) == digest:          # a repeated run: the new version is already in place
        staged.unlink(missing_ok=True)
        return old_path, find_retired(old_path.parent, old.get("size"), old.get("sha256"))
    retired = None
    if shared:
        pass                                          # its other items keep it; the new version goes beside it
    elif old_path is not None and old_path.is_file():
        retired = retire(old_path, old.get("modified"))
    elif old_path is not None:                        # gone from its place: maybe a repeated run retired it
        retired = find_retired(old_path.parent, old.get("size"), old.get("sha256"))
    staged = staged.resolve()
    in_place = next((p.resolve() for p in sorted(Path(dest_folder).iterdir(), key=lambda p: (is_numbered_copy(p), p.name))
                     if p.is_file() and p.resolve() != staged and p.name.startswith(prefix)
                     and p.stat().st_size == size and _sha256(p) == digest), None)
    if in_place is not None:
        staged.unlink(missing_ok=True)                # the new version is already there under its name
        return in_place, retired
    return staged.replace(_unique_path(dest_folder, filename, "", prefix)), retired


def download_folder(context, sesskey: str, cmid: str, folder_name: str,
                    dest_folder: Path) -> Path | None:
    """Download a Moodle folder module as a zip and extract it into a subfolder.
    Returns the extraction directory."""
    resp = context.request.get(
        f"{BASE_URL}/mod/folder/download_folder.php?id={cmid}&sesskey={sesskey}",
        timeout=180000,
    )
    if not resp.ok or _is_html(resp):
        return None
    target_dir = dest_folder / safe_name(folder_name, f"folder-{cmid}")
    n = 1
    while target_dir.exists():
        target_dir = dest_folder / f"{safe_name(folder_name)} ({n})"
        n += 1
    ensure_inside_archive(target_dir)
    target_dir.mkdir(parents=True)
    zip_path = target_dir / "_download.zip"
    zip_path.write_bytes(resp.body())
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                ensure_inside_archive(target_dir / member)  # blocks zip-slip
            zf.extractall(target_dir)
    finally:
        zip_path.unlink(missing_ok=True)
    return target_dir


def path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
