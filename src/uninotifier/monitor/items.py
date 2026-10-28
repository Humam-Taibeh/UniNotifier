"""
Handling one Moodle item: download and file it, resolve a link, read a Teams
meeting or attendance page, take its screenshot, tick it done on the portal,
and notice a re-uploaded file.
"""

from datetime import datetime
from pathlib import Path
import logging
import math

from uninotifier import activities
from uninotifier import archive
from uninotifier import grades
from uninotifier import media
from uninotifier import moodle
from uninotifier import notifier
from uninotifier.monitor import config

log = logging.getLogger("uninotifier")


# ------------------------------------------------------- item handling

_EXPAND_JS = """() => {
    document.querySelectorAll('.course-content-item-content.collapse')
        .forEach(e => e.classList.add('show'));
    document.querySelectorAll('.section-collapsed')
        .forEach(e => e.classList.remove('section-collapsed'));
    document.querySelectorAll('img[loading="lazy"]').forEach(i => { i.loading = 'eager'; });
    // floating widgets (accessibility, back-to-top, sticky bars) would sit on top of the content
    document.querySelectorAll('body *').forEach(e => {
        const pos = getComputedStyle(e).position;
        if (pos === 'fixed' || pos === 'sticky') e.style.visibility = 'hidden';
    });
}"""


_BOX_JS = """(el) => { const r = el.getBoundingClientRect();
    return {x: r.left + window.scrollX, y: r.top + window.scrollY, w: r.width, h: r.height}; }"""


def capture_screens(page, url: str, selector: str) -> tuple[list[bytes], bool]:
    """Screenshot one element as a single long image (or, past Chromium's
    height limit, the fewest equal parts). Returns (pngs, truncated);
    truncated means the element was taller than MAX_SHOT_PARTS images."""
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.evaluate(_EXPAND_JS)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # lazy images still loading: shoot what's there
        target = page.locator(selector).first
        if target.count() == 0:
            target = page.locator("#region-main").first
        box = target.evaluate(_BOX_JS) if target.count() else None
        if not box or box["w"] < 10 or box["h"] < 10:
            box = page.evaluate("() => ({x: 0, y: 0, w: document.documentElement.scrollWidth,"
                                " h: document.documentElement.scrollHeight})")
        pad = 8
        x, y = max(box["x"] - pad, 0), max(box["y"] - pad, 0)
        w, h = box["w"] + 2 * pad, box["h"] + 2 * pad
        count = math.ceil(h / config.MAX_SHOT_HEIGHT)
        truncated = count > config.MAX_SHOT_PARTS
        count = min(count, config.MAX_SHOT_PARTS)
        tile_h = config.MAX_SHOT_HEIGHT if truncated else math.ceil(h / count)
        return [page.screenshot(type="png", full_page=True,
                                clip={"x": x, "y": y + n * tile_h, "width": w,
                                      "height": min(tile_h, y + h - (y + n * tile_h))})
                for n in range(count)], truncated
    except Exception:
        log.exception("Could not screenshot %s", url)
        return [], False


# Files this process saved, by the item they were saved for: when a retried
# scan downloads such an item again, the identical bytes it finds are its own
# copy from the failed attempt — not a second item sharing a file.
_SAVED_FOR: dict[str, str] = {}


def process_item(context, sesskey: str, item: dict) -> dict:
    """Handle one item according to its kind (download / resolve). Returns
    an artifact dict: kind, and as produced: path, size, target, grade."""
    kind = media.classify(item)
    artifact = {"kind": kind}

    if kind == "file":
        dest = archive.target(item["course"], item["type"], item["name"])
        try:
            if item["type"] == "resource":
                got = archive.download_resource(context, item["url"], dest)
                path = got["path"] if got else None
                if got:
                    artifact["sha256"], artifact["modified"] = got["sha256"], got["modified"]
                    own_copy = _SAVED_FOR.get(str(got["path"])) == item["url"]
                    artifact["duplicate"] = bool(got.get("duplicate")) and not own_copy
                    if artifact["duplicate"]:
                        log.info("    identical to %s — not saved again", got["path"].name)
                    elif not got.get("duplicate"):
                        _SAVED_FOR[str(got["path"])] = item["url"]
            else:
                path = archive.download_folder(
                    context, sesskey, moodle.extract_cmid(item["url"]), item["name"], dest)
        except Exception:
            log.exception("    download failed for %s", item["name"])
            path = None
        if path:
            artifact["path"] = str(path)
            artifact["size"] = archive.path_size(path)
            log.info("    saved %s (%d bytes)", path, artifact["size"])
            artifact["grade"] = grades.scan_paths(path, config.STUDENT_ID) if config.STUDENT_ID else None
        else:
            log.warning("    could not download %s", item["name"])
            artifact["kind"] = "unknown"

    elif kind == "link":
        target = media.resolve_url_target(context, item["url"])
        if target:
            artifact["target"] = target
        else:
            artifact["kind"] = "unknown"

    elif kind in ("meeting", "attendance"):
        try:
            resp = context.request.get(item["url"], timeout=30000)
            page = resp.text() if resp.ok else ""
        except Exception:
            log.exception("    could not read %s", item["name"])
            page = ""
        if kind == "meeting":
            artifact["meeting"] = activities.parse_meeting(page)
            if artifact["meeting"]["url"]:
                artifact["target"] = artifact["meeting"]["url"]   # feeds the lecture reminders (lecturelinks)
        else:
            artifact["sessions"] = activities.parse_sessions(page)

    return artifact


def needs_screenshot(artifact: dict) -> bool:
    """Files and links speak for themselves; everything else (and a file
    that couldn't be attached) is shown as it looks on the portal."""
    return artifact["kind"] in ("activity", "text", "unknown") or (
        artifact["kind"] == "file" and not Path(artifact["path"]).is_file()) or (
        artifact["kind"] == "meeting" and not artifact["meeting"]["details"]) or (
        artifact["kind"] == "attendance" and not artifact["sessions"])


def landed(artifact: dict) -> bool:
    """A file now on disk in the archive, or a link that was resolved."""
    if artifact.get("kind") == "file":
        return bool(artifact.get("path")) and Path(artifact["path"]).exists()
    return artifact.get("kind") == "link" and bool(artifact.get("target"))


def auto_complete(context, sesskey: str, item: dict, artifact: dict) -> None:
    """Tick "Mark as done" on static material that really landed: a downloaded
    file or folder, a resolved link. Assignments, quizzes, forums, pages and
    labels are never ticked."""
    if (item["type"] in media.AUTO_DONE_TYPES and item.get("manual") and not item.get("done")
            and landed(artifact)
            and moodle.set_completion(context, sesskey, item["cmid"], True)):
        item["done"] = True
        log.info("    marked as done on the portal")


def check_file_update(context, item: dict, record: dict, shared: bool = False) -> dict | None:
    """Detect a lecturer re-uploading/modifying an already archived resource.
    A cheap HEAD compares Last-Modified (and size); on a change the new version
    is downloaded and confirmed by SHA-256, takes the file's place, and the old
    one moves to _Old_Versions beside it (archive.put_new_version) — unless it is
    `shared` with other items, which keep it. Returns the
    update artifact — with "versions", every retired copy, so the record keeps
    both — or None when nothing changed. Records saved before tracking existed
    are baselined silently instead of reported."""
    probe = archive.probe_resource(context, item["url"])
    if not probe:
        return None
    old_mod, new_mod = record.get("modified"), probe["modified"]
    if old_mod is None and "sha256" not in record:
        record["modified"] = new_mod  # baseline, no notification
        return None
    changed = (new_mod is not None and old_mod is not None and new_mod > old_mod) or (
        new_mod is None and probe["size"] is not None and probe["size"] != record.get("size"))
    if not changed:
        return None

    dest = archive.target(item["course"], item["type"], item["name"])
    got = archive.download_resource(context, item["url"], dest, tag=f"incoming {datetime.now():%Y-%m-%d}")
    if not got:
        return None
    if got["sha256"] == record.get("sha256"):
        got["path"].unlink(missing_ok=True)  # same bytes: only the timestamp moved
        record["modified"] = new_mod
        return None
    old_path = Path(record["path"]) if record.get("path") else None
    new_path, retired = archive.put_new_version(got["path"], got.get("filename") or got["path"].name, dest,
                                                old_path, record, shared=shared)
    versions = list(record.get("versions") or [])
    if retired is not None and str(retired) not in {v.get("path") for v in versions}:
        versions.append({"path": str(retired), "size": record.get("size"), "sha256": record.get("sha256"),
                         "modified": old_mod})
    return {"kind": "file", "updated": True, "path": str(new_path),
            "size": archive.path_size(new_path), "sha256": got["sha256"],
            "modified": got["modified"] or new_mod, "old_modified": old_mod,
            "old_path": str(retired) if retired else record.get("path"), "versions": versions}


def uploadable(path: Path | None) -> bool:
    return bool(path) and path.is_file() and path.stat().st_size <= notifier.MAX_UPLOAD_BYTES
