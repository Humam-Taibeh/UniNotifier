"""
What a portal is showing when signing in to it fails.

Both sign-ins — the e-learning site (monitor.py, through Microsoft) and the
registration portal (portal.py) — hand any failure that isn't a wrong
password, an expired Microsoft session or a missing code to `unavailable()`.
It reads the page the browser is on, looks for a maintenance notice (Arabic
or English), keeps a screenshot as maintenance_snapshot.png and returns a
PortalUnavailable carrying both, so the caller can send the picture with the
alert instead of a bare "login failed":

  🛠️ maintenance   the page says so ("قيد الصيانة", "Under Maintenance",
                   "Service Unavailable", ...) — not a crash: the monitor ends
                   the run cleanly and says when the site is back
  ⚠️ unexpected    anything else — the screenshot shows what went wrong
"""

import logging
import re

from uninotifier import paths
from uninotifier.notifier import escape_md, redact

log = logging.getLogger("uninotifier")

SNAPSHOT = paths.DATA / "maintenance_snapshot.png"
DETAIL_LIMIT = 300

MAINTENANCE_RE = re.compile(
    r"قيد\s*الصيانة|تحت\s*الصيانة|أعمال\s*(?:ال)?صيانة|(?:في\s*)?وضع\s*الصيانة|"
    r"الخدمة\s*غير\s*متاحة|الموقع\s*غير\s*متاح(?:\s*حالي[اًا])?|"
    r"under\s+maintenance|undergoing\s+maintenance|maintenance\s+mode|(?:scheduled|planned)\s+maintenance|"
    r"down\s+for\s+maintenance|service\s+(?:is\s+)?(?:temporarily\s+)?unavailable|temporarily\s+unavailable|"
    r"bad\s+gateway|gateway\s+time-?out",
    re.I)


class PortalUnavailable(Exception):
    """Signing in failed for a reason other than the credentials."""

    def __init__(self, site: str, detail: str, phrase: str | None = None, png: bytes | None = None):
        self.site, self.detail, self.phrase, self.png = site, detail, phrase, png
        state = f"under maintenance (“{phrase}”)" if phrase else "unexpected sign-in failure"
        super().__init__(f"{site}: {state} — {detail}")


def detect(text: str | None) -> str | None:
    """The maintenance phrase a page shows, if any."""
    match = MAINTENANCE_RE.search(text or "")
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else None


def page_text(page) -> str:
    """The page's title and visible text; "" when it can't be read."""
    try:
        return f"{page.title()}\n{page.inner_text('body', timeout=5000)}"
    except Exception:
        log.warning("Could not read the page text for the maintenance check")
        return ""


def capture(page) -> tuple[str | None, bytes | None]:
    """(maintenance phrase or None, screenshot) of the page as it is now.
    Never raises: a page that can't be read gives (None, None)."""
    text = page_text(page)
    png = None
    try:
        png = page.screenshot(type="png", timeout=15000)
        SNAPSHOT.write_bytes(png)
    except Exception:
        log.warning("Could not take the portal snapshot")
    return detect(text), png


def unavailable(page, site: str, cause: BaseException | str) -> PortalUnavailable:
    """The exception to raise for a failed sign-in, with what the page shows."""
    detail = re.sub(r"\s+", " ", redact(str(cause))).strip()[:DETAIL_LIMIT] or type(cause).__name__
    phrase, png = capture(page) if page is not None else (None, None)
    error = PortalUnavailable(site, detail, phrase or detect(detail), png)
    log.error("%s", error)
    return error


# The headline is in Arabic, as the user asked for this alert; the details stay English.
HEADLINE_WITH_SHOT = "⚠️ *تعذر الدخول للبوابة، مرفق لقطة للشاشة الحالية للتحقق من الحالة*"
HEADLINE = "⚠️ *تعذر الدخول للبوابة*"


def alert_text(error: PortalUnavailable, follow_up: str = "") -> str:
    lines = [HEADLINE_WITH_SHOT if error.png else HEADLINE, "",
             f"🌐 *Portal:* {error.site[:1].upper()}{error.site[1:]}"]
    if error.phrase:
        lines.append(f"🛠️ *Status:* Under maintenance — the page says “{escape_md(error.phrase)}”")
    else:
        lines.append(f"❓ *Reason:* {escape_md(error.detail)} (not a wrong password)")
    if follow_up:
        lines += ["", follow_up]
    return "\n".join(lines)
