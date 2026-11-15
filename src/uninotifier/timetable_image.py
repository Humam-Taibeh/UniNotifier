"""
The weekly timetable as a picture and a PDF, drawn by the bot itself.

A student used to make this picture by hand. Here it is drawn from the schedule in force
(schedule.load_schedule(): your own schedule.json, or the one the bot built from the portal),
so every student gets one without drawing anything.

  layout(schedule)        pure: which days are columns, which times are rows, where the breaks are
  build_html(schedule..)  pure: the whole page as one HTML string — no script, no outside file
  render(html, png, pdf)  Chromium (the one Playwright already installed) turns it into files
  python -m uninotifier.timetable_image --preview     draw yours now and open it; sends nothing

What it shows is only what the schedule holds. Every text is escaped, and the page is drawn
with the network blocked: a course name can never load or run anything.

The university's logo is not part of the project. A student who wants it puts their own
`logo.png` in the assets folder (git-ignored) and it appears in the header.
"""

import base64
import html
import sys
from datetime import date
from pathlib import Path

from uninotifier import paths

UNIVERSITY = "Al-Zaytoonah University of Jordan"
OUT_DIR = paths.DATA / "timetable"
PNG_FILE = OUT_DIR / "Weekly_Schedule.png"
PDF_FILE = OUT_DIR / "Weekly_Academic_Schedule.pdf"
LOGO_FILE = paths.ASSETS / "logo.png"

WEEK = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
DAY_NAMES = {"sun": "Sunday", "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
             "thu": "Thursday", "fri": "Friday", "sat": "Saturday"}
MODES = {"In-Person": ("inperson", "وجاهي"), "Online": ("online", "إلكتروني"),
         "Blended": ("blended", "مدمج"), "Activity": ("activity", "نشاط")}
CAMPUS_MODES = ("In-Person", "Blended")
DEFAULT_PLACE = {"Online": "Remote / Teams", "Activity": "Asynchronous / Self-study"}
BREAK_MINUTES = 60                  # a gap this long between two rows is drawn as a break
TIME_COLUMN = 125                   # px
DAY_COLUMN = 231                    # px; the page is as wide as its days (never narrower than 3)
SCALE = 2                           # the picture is drawn at twice the size: sharp when zoomed


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def layout(schedule: dict) -> dict:
    """{"days": the days that have a class, in week order,
        "rows": [{"start", "end", "cells": {day: [lecture, ...]}} or {"break": (start, end)}]}.
    A row is one start–end pair, so a lab with its own hours gets its own row and nothing
    is ever squeezed into a time it does not have."""
    per_day = {day: list(lectures) for day, lectures in (schedule.get("days") or {}).items() if lectures}
    days = [day for day in WEEK if day in per_day]
    times = sorted({(lec["start"], lec["end"]) for day in days for lec in per_day[day]},
                   key=lambda t: (_minutes(t[0]), _minutes(t[1])))
    rows, last_end = [], None
    for start, end in times:
        if last_end is not None and _minutes(start) - _minutes(last_end) >= BREAK_MINUTES:
            rows.append({"break": (last_end, start)})
        rows.append({"start": start, "end": end,
                     "cells": {day: [lec for lec in per_day[day] if (lec["start"], lec["end"]) == (start, end)]
                               for day in days}})
        last_end = end if last_end is None or _minutes(end) > _minutes(last_end) else last_end
    return {"days": days, "rows": rows}


def on_campus(schedule: dict, day: str) -> bool:
    return any(lec.get("mode") in CAMPUS_MODES for lec in (schedule.get("days") or {}).get(day, []))


def totals(schedule: dict, hours: dict | None = None) -> tuple[int, float | None]:
    """(courses, credit hours) — the hours only when every course's are known."""
    courses = sorted({lec["course"] for lectures in (schedule.get("days") or {}).values() for lec in lectures})
    known = [(hours or {}).get(course) for course in courses]
    return len(courses), (sum(known) if courses and all(h is not None for h in known) else None)


def _plain(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def summary_line(schedule: dict, hours: dict | None = None) -> str:
    """'6 Courses · 18 Credit Hours'."""
    count, total = totals(schedule, hours)
    parts = [f"{count} Course{'s' if count != 1 else ''}"]
    if total is not None:
        parts.append(f"{_plain(total)} Credit Hours")
    return " · ".join(parts)


def _span(days: list[str]) -> str:
    """'Sun–Wed', or the one day's name."""
    if not days:
        return ""
    return DAY_NAMES[days[0]] if len(days) == 1 else f"{DAY_NAMES[days[0]][:3]}–{DAY_NAMES[days[-1]][:3]}"


# ------------------------------------------------------------------ the page

_ICONS = {
    "inperson": '<svg viewBox="0 0 12 12"><circle cx="6" cy="6" r="5" fill="currentColor"/></svg>',
    "blended": '<svg viewBox="0 0 12 12"><circle cx="6" cy="6" r="5" fill="currentColor"/></svg>',
    "activity": '<svg viewBox="0 0 12 12"><circle cx="6" cy="6" r="4.2" fill="none" stroke="currentColor" '
                'stroke-width="1.6" stroke-dasharray="2.2 1.8"/></svg>',
    "online": '<svg viewBox="0 0 12 12"><path d="M10 6a4 4 0 1 1-1.2-2.85" fill="none" stroke="currentColor" '
              'stroke-width="1.7" stroke-linecap="round"/><path d="M9.6 1.2v2.6H7" fill="none" '
              'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
}

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:#fcfbf7}
body{width:__WIDTH__px;font-family:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;color:#1c2420;
  -webkit-font-smoothing:antialiased}
.mono{font-family:"Cascadia Mono","Consolas","Courier New",monospace}
.head{background:linear-gradient(100deg,#0b5632 0%,#0f6039 55%,#14703f 100%);color:#fff;
  padding:46px 48px 48px;display:flex;align-items:center;gap:30px}
.head img{width:66px;height:66px;border-radius:50%;background:#fff;object-fit:contain;flex:none}
.head .who{flex:1;min-width:0}
.head .uni{font-size:11px;letter-spacing:.3em;text-transform:uppercase;opacity:.82}
.head h1{font-size:33px;font-weight:700;letter-spacing:-.01em;margin:8px 0 9px;line-height:1.1}
.head .sub{font-size:15px;opacity:.92}
.head .issued{text-align:right;flex:none}
.head .issued .term{font-size:11px;letter-spacing:.24em;text-transform:uppercase;opacity:.8}
.head .issued .date{font-size:13px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;margin-top:12px}
.legend{display:flex;align-items:center;gap:34px;padding:22px 46px;border-bottom:1px solid #e6e4dc;font-size:15px}
.legend .cap{font-size:11px;letter-spacing:.3em;color:#8a8f8a;text-transform:uppercase}
.legend .ar{color:#7c827d;margin-left:9px}
.key{display:inline-flex;align-items:center;gap:9px;white-space:nowrap}
.key svg,.mode svg{width:12px;height:12px;flex:none}
.row{display:grid;grid-template-columns:__TIME__px repeat(__DAYS__,minmax(0,1fr));border-bottom:1px solid #e6e4dc}
.days{border-bottom:2px solid #0f5f37}
.days>div{padding:20px 14px 18px}
.days .t{font-size:11px;color:#8a8f8a;padding-left:26px;align-self:center}
.days .name{font-size:18px;font-weight:600;color:#0f5f37}
.days .where{font-size:10px;letter-spacing:.24em;color:#8a8f8a;text-transform:uppercase;margin-top:8px}
.time{padding:0 0 0 26px;border-right:1px solid #e6e4dc;display:flex;flex-direction:column;justify-content:center;
  font-size:14px;line-height:1.75}
.time b{font-weight:700;color:#141a17}.time span{color:#7c827d}
.cell{display:flex;flex-direction:column;min-height:132px}
.card{flex:1;padding:21px 18px 20px 17px;border-left:4px solid var(--bar);background:var(--bg);min-width:0}
.card+.card{border-top:1px solid #fcfbf7}
.card .en{font-size:17.5px;font-weight:600;line-height:1.22;color:#17201b;overflow-wrap:anywhere}
.card .ar{font-size:14.5px;color:#7c827d;margin-top:7px;unicode-bidi:plaintext;text-align:left;overflow-wrap:anywhere}
.card .meta{display:flex;align-items:center;gap:11px;margin-top:11px;flex-wrap:wrap}
.card .sec{font-size:10.5px;letter-spacing:.16em;background:rgba(20,30,25,.075);border-radius:4px;padding:5px 8px;
  color:#4e5651;white-space:nowrap}
.mode{display:inline-flex;align-items:center;gap:7px;font-size:13.5px;font-weight:600;color:var(--ink);white-space:nowrap}
.card .place{font-size:13px;color:#5d655f;margin-top:11px;overflow-wrap:anywhere;unicode-bidi:plaintext;text-align:left}
.inperson{--bg:#e7f0eb;--bar:#0f5f37;--ink:#0f5f37;color:#0f5f37}
.blended{--bg:#e9edf3;--bar:#0f5f37;--ink:#0f5f37;color:#0f5f37}
.activity{--bg:#f8eed9;--bar:#c98a1b;--ink:#a8660a;color:#c98a1b}
.online{--bg:#e9ecf3;--bar:#3b4a73;--ink:#3b4a73;color:#3b4a73}
.card.inperson,.card.blended,.card.activity,.card.online{color:#1c2420}
.break .time{padding-top:17px;padding-bottom:17px}
.break .gap{grid-column:2/-1;display:flex;align-items:center;padding-left:36px;font-size:10.5px;letter-spacing:.3em;
  color:#8a8f8a;text-transform:uppercase}
.foot{background:#0f5f37;color:#fff;padding:62px 46px 70px;display:flex;align-items:baseline;gap:40px;
  justify-content:space-between;font-size:14.5px;font-weight:600;flex-wrap:wrap}
.foot .cap{font-size:10.5px;letter-spacing:.26em;font-weight:400;opacity:.8;margin-right:11px;text-transform:uppercase}
.empty{padding:70px 46px;color:#7c827d;font-size:16px}
"""


def _e(text) -> str:
    return html.escape(str(text or ""), quote=True)


def _mode_mark(mode: str) -> str:
    css, _ = MODES.get(mode, MODES["In-Person"])
    return f'<span class="mode {css}">{_ICONS[css]}{_e(mode)}</span>'


def _card(lec: dict) -> str:
    mode = lec.get("mode", "In-Person")
    css, _ = MODES.get(mode, MODES["In-Person"])
    place = lec.get("place") or DEFAULT_PLACE.get(mode, "")
    section = str(lec.get("section") or "").strip()
    parts = [f'<div class="en">{_e(lec.get("course"))}</div>']
    if lec.get("course_ar") and lec["course_ar"] != lec.get("course"):
        parts.append(f'<div class="ar">{_e(lec["course_ar"])}</div>')
    sec = f'<span class="sec mono">SEC {_e(section.zfill(2))}</span>' if section else ""
    parts.append(f'<div class="meta">{sec}{_mode_mark(mode)}</div>')
    if place:
        parts.append(f'<div class="place">{_e(place)}</div>')
    return f'<div class="card {css}">{"".join(parts)}</div>'


def _row(row: dict, days: list[str]) -> str:
    if "break" in row:
        start, end = row["break"]
        return (f'<div class="row break"><div class="time mono"><span>{_e(start)}</span><span>{_e(end)}</span></div>'
                '<div class="gap mono">— Break / Transition — No scheduled classes —</div></div>')
    cells = "".join(f'<div class="cell">{"".join(_card(lec) for lec in row["cells"][day])}</div>' for day in days)
    return (f'<div class="row"><div class="time mono"><b>{_e(row["start"])}</b><span>{_e(row["end"])}</span></div>'
            f'{cells}</div>')


def page_width(day_count: int) -> int:
    return TIME_COLUMN + DAY_COLUMN * max(day_count, 3)


def build_html(schedule: dict, hours: dict | None = None, issued: date | None = None,
               university: str = UNIVERSITY, logo: bytes | None = None) -> str:
    """The whole timetable page. `hours` maps a course's name to its credit hours (shown only
    when every course's are known); `logo` is a PNG's bytes, or None for no logo."""
    plan = layout(schedule)
    days = plan["days"]
    issued = issued or date.today()
    summary = summary_line(schedule, hours)
    css = (_CSS.replace("__WIDTH__", str(page_width(len(days)))).replace("__TIME__", str(TIME_COLUMN))
           .replace("__DAYS__", str(max(len(days), 1))))
    picture = (f'<img alt="" src="data:image/png;base64,{base64.b64encode(logo).decode("ascii")}">'
               if logo else "")
    subtitle = " · ".join(part for part in (summary, _span(days)) if part)
    head = (f'<div class="head">{picture}<div class="who"><div class="uni mono">{_e(university)}</div>'
            f'<h1>Weekly Class Schedule</h1><div class="sub">{_e(subtitle)}</div></div>'
            f'<div class="issued mono"><div class="term">{_e(schedule.get("semester") or "Term schedule")}</div>'
            f'<div class="date">Issued {_e(issued.strftime("%b"))} {issued.day}, {issued.year}</div></div></div>')
    used = [mode for mode in MODES
            if any(lec.get("mode") == mode for day in days for lec in schedule["days"][day])]
    legend = ('<div class="legend"><span class="cap mono">Legend</span>'
              + "".join(f'<span class="key {MODES[m][0]}">{_ICONS[MODES[m][0]]}'
                        f'<span style="color:#1c2420">{_e(m)}<span class="ar">{_e(MODES[m][1])}</span></span></span>'
                        for m in used) + "</div>")
    if not days:
        body = '<div class="empty">No classes in this timetable yet.</div>'
    else:
        columns = "".join(f'<div><div class="name">{DAY_NAMES[day]}</div><div class="where mono">'
                          f'{"On campus" if on_campus(schedule, day) else "Remote"}</div></div>' for day in days)
        body = (f'<div class="row days"><div class="t mono">Time</div>{columns}</div>'
                + "".join(_row(row, days) for row in plan["rows"]))
    campus = [DAY_NAMES[day] for day in days if on_campus(schedule, day)]
    remote = [DAY_NAMES[day] for day in days if not on_campus(schedule, day)]
    foot = ('<div class="foot">'
            + "".join(f'<div><span class="cap mono">{cap}</span>{_e(" • ".join(names))}</div>'
                      for cap, names in (("On-campus", campus), ("Remote", remote)) if names)
            + f"<div>{_e(summary)}</div></div>")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Weekly Class Schedule</title>'
            f"<style>{css}</style></head><body>{head}{legend if used else ''}{body}{foot}</body></html>")


# ------------------------------------------------------------------ files

def render(page_html: str, png: Path | None = PNG_FILE, pdf: Path | None = PDF_FILE, width: int | None = None) -> None:
    """Draw the page into a PNG (twice the size, one long image) and a one-page PDF.
    The page is given as text and every request it could make is refused."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": width or page_width(4), "height": 900},
                                          device_scale_factor=SCALE, java_script_enabled=False)
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            page.set_content(page_html, wait_until="load")
            box = page.locator("body").bounding_box()
            if png is not None:
                png.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(png), type="png", full_page=True)
            if pdf is not None:
                pdf.parent.mkdir(parents=True, exist_ok=True)
                page.emulate_media(media="screen")
                page.pdf(path=str(pdf), width=f"{int(box['width'])}px", height=f"{int(box['height']) + 1}px",
                         print_background=True, page_ranges="1")
        finally:
            browser.close()


def logo_bytes() -> bytes | None:
    try:
        return LOGO_FILE.read_bytes()
    except OSError:
        return None


def draw(schedule: dict, hours: dict | None = None, png: Path | None = PNG_FILE, pdf: Path | None = PDF_FILE) -> None:
    """Draw `schedule` into the two files."""
    page_html = build_html(schedule, hours, logo=logo_bytes())
    render(page_html, png, pdf, width=page_width(len(layout(schedule)["days"])))


if __name__ == "__main__":      # python -m uninotifier.timetable_image --preview
    import os

    from uninotifier import schedule as schedule_module
    current = schedule_module.load_schedule()
    if not layout(current)["days"]:
        sys.exit("There is no timetable to draw yet.")
    draw(current)
    print(f"Drawn: {PNG_FILE}\n       {PDF_FILE}")
    if "--preview" in sys.argv and hasattr(os, "startfile"):
        os.startfile(PNG_FILE)      # opens it in your picture viewer; nothing is sent anywhere
