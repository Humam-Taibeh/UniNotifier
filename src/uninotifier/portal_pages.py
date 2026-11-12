"""
The registration portal's pages, read: pure functions from what a page shows (its tables, its
text, its HTML) to data and to the lines of a message — the transcript's semesters, the balance,
this term's results, the home page's timetable, the figures a page states. No browser, no
network, no files: portal.py opens the pages and hands them here.

Written against the real pages (dumps taken 2026-10-11). The transcript is one collapsible block
per semester; the financial record is one table plus a "total balance" bar; the results page is a
table with an "empty" message; the home page's timetable is a table whose cells carry their
column title.
"""

import re

from uninotifier import transcript
from uninotifier.notifier import escape_md

_NUM = r"(-?\d[\d,]*(?:\.\d+)?)"


_GAP = r"[^\d-]{0,25}"   # label, colon, spaces — up to the number (a minus sign is part of it)


FIGURES = {
    "passed_hours": [
        rf"(?:الساعات|ساعات)\s*(?:المنجزة|المجتازة|الناجحة|المقطوعة|المكتسبة){_GAP}{_NUM}",
        rf"(?:passed|completed|earned)\s*(?:credit\s*)?hours{_GAP}{_NUM}"],
    "cgpa": [
        rf"المعدل\s*التراكمي{_GAP}{_NUM}",
        rf"(?:cumulative|overall)\s*(?:gpa|average){_GAP}{_NUM}"],
    "balance": [
        rf"(?:الرصيد|المبلغ\s*المستحق|الذمم){_GAP}{_NUM}",
        rf"(?:balance|amount\s*due){_GAP}{_NUM}"],
}


STATUS_NAMES = {"منتظم": "Regular / Good Standing", "إنذار": "Academic Warning", "انذار": "Academic Warning",
                "مراقب": "Academic Probation", "مفصول": "Dismissed", "متخرج": "Graduated", "مؤجل": "Deferred"}


STATUS_RE = re.compile(r"الحالة\s*:?\s*([^\s\d:]+)")


def english_status(raw: str) -> str:
    """The academic status the transcript shows (منتظم …) in English."""
    return next((name for word, name in STATUS_NAMES.items() if word in raw), raw)


# The home page's table of sections: a title span and a label with the value in every cell.
TIMETABLE_COLUMNS = {"رمز المقرر": "code", "اسم المقرر": "name", "الشعبة": "section",
                     "النشاط": "activity", "قاعة": "room", "الوقت": "time"}


MAX_TABLE_ROWS = 80          # rows sent as text; the screenshot has the rest


def clean_tables(raw: list[list[list[str]]]) -> list[list[list[str]]]:
    """Keep real data tables: at least two rows and two columns of content,
    no row entirely empty."""
    out = []
    for table in raw:
        rows = [[re.sub(r"\s+", " ", c or "").strip() for c in row] for row in table]
        rows = [r for r in rows if any(r)]
        if len(rows) >= 2 and max(len([c for c in r if c]) for r in rows) >= 2:
            out.append(rows)
    return out


def find_figures(text: str) -> dict[str, float]:
    """Best-effort numbers from the page text: passed hours, CGPA, balance."""
    found = {}
    for key, patterns in FIGURES.items():
        for pattern in patterns:
            match = re.search(pattern, text or "", re.I)
            if match:
                try:
                    found[key] = float(match.group(1).replace(",", ""))
                except ValueError:
                    continue
                break
    return found


def table_lines(tables: list[list[list[str]]], limit: int = MAX_TABLE_ROWS,
                translate: bool = False) -> list[str]:
    """Plain 'cell | cell | cell' lines (Markdown-escaped); the first row of
    each table is its header, translated to English with translate=True. A
    blank line separates tables; output stops after `limit` rows."""
    lines, used = [], 0
    for table in tables:
        for n, row in enumerate(table):
            if used >= limit:
                lines.append(f"… {sum(len(t) for t in tables) - used} more rows — see the screenshot")
                return lines
            cells = [HEADER_NAMES.get(c, c) if translate and n == 0 else c for c in row if c]
            text = escape_md(" | ".join(cells))
            lines.append(f"*{text}*" if n == 0 else text)
            used += 1
        lines.append("")
    return lines


TERM_NAMES = {"الأول": "First semester", "الثاني": "Second semester", "الصيفي": "Summer semester"}


HEADER_NAMES = {"رمز المقرر": "Code", "اسم المقرر": "Course", "النشاط": "Activity", "الحالة": "Status",
                "العلامة": "Mark", "ناجح؟": "Passed", "ناجح": "Passed", "عدد الساعات المعتمدة": "Hours",
                "الفصل الدراسي": "Semester", "الرصيد": "Balance", "التخصص": "Major"}


NO_RESULTS = re.compile(r"لا\s*يوجد\s*(?:نتائج|بيانات)")


def english_term(legend: str) -> str:
    """'الفصل الثاني 2025/2026 20252' -> 'Second semester 2025/2026'."""
    year = re.search(r"\d{4}/\d{4}", legend or "")
    name = next((v for k, v in TERM_NAMES.items() if k in (legend or "")), "Semester")
    return f"{name} {year.group(0)}" if year else name


def _number(text: str) -> float | None:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text or "")
    return float(match.group(0).replace(",", "")) if match else None


def parse_transcript(raw: list[dict]) -> list[dict]:
    """The semester blocks, newest first as the portal lists them:
    {"term", "average", "cumulative", "courses": [{"code", "name", "hours",
    "mark", "passed" (True / False / None while in progress), "repeated"}]}."""
    semesters = []
    for block in raw:
        heads = block.get("heads", [])
        col = {key: next((i for i, h in enumerate(heads) if word in h), None)
               for key, word in (("code", "رمز"), ("name", "اسم"), ("hours", "الساعات"),
                                 ("mark", "العلامة"), ("passed", "ناجح"), ("repeated", "معادة"))}
        courses = []
        for row in block.get("rows", []):
            if len(row) < len(heads) or col["code"] is None:
                continue                                   # the "no data" row
            passed = row[col["passed"]] if col["passed"] is not None else ""
            courses.append({
                "code": row[col["code"]], "name": row[col["name"]] if col["name"] is not None else "",
                "hours": _number(row[col["hours"]]) or 0 if col["hours"] is not None else 0,
                "mark": _number(row[col["mark"]]) if col["mark"] is not None else None,
                "passed": True if passed == "✓" else False if passed == "✗" else None,
                "repeated": col["repeated"] is not None and row[col["repeated"]] == "✓"})
        graded = {c["code"] for c in courses if c["passed"] is not None}
        courses = [c for c in courses if c["passed"] is not None or c["code"] not in graded]  # stale duplicate rows
        text = block.get("text", "")
        average =re.search(r"المعدل\s*الفصلي\s*(\d+(?:\.\d+)?)", text)
        cumulative = re.search(r"المعدل\s*التراكمي\s*(\d+(?:\.\d+)?)", text)
        semesters.append({"term": english_term(block.get("legend", "")),
                          "average": float(average.group(1)) if average else None,
                          "cumulative": float(cumulative.group(1)) if cumulative else None,
                          "courses": courses})
    return semesters


def passed_hours(semesters: list[dict]) -> float:
    """Credit hours of every course passed, each course counted once."""
    hours = {}
    for sem in semesters:
        for c in sem["courses"]:
            if c["passed"]:
                hours[c["code"]] = c["hours"]
    return sum(hours.values())


def transcript_lines(semesters: list[dict]) -> list[str]:
    """The semester blocks (see transcript.py)."""
    return transcript.blocks(semesters)


def parse_balance(text: str, tables: list[list[list[str]]]) -> dict:
    """{"total": float|None (positive = owed / debit, negative = credit),
    "rows": [(term, hours, balance)]} from the financial record page."""
    total = None
    match = re.search(r"الرصيد\s*الكلي\s*(-?\d[\d,]*(?:\.\d+)?)\s*(مدين|دائن)?", text or "")
    if match:
        total = float(match.group(1).replace(",", ""))
        if match.group(2) == "دائن":
            total = -total
    rows = []
    for table in tables:
        head = table[0]
        find = lambda word: next((i for i, h in enumerate(head) if word in h), None)
        term_col, hours_col, balance_col = find("الفصل"), find("الساعات"), find("الرصيد")
        if term_col is None or balance_col is None:
            continue
        for row in table[1:]:
            if len(row) > max(term_col, balance_col):
                rows.append((english_term(row[term_col]), _number(row[hours_col]) if hours_col is not None
                             and len(row) > hours_col else None, _number(row[balance_col]) or 0.0))
    return {"total": total, "rows": rows}


def parse_major(tables: list[list[list[str]]]) -> str | None:
    """The student's major as the financial-record page lists it (a column of its semester table) —
    the newest semester's. Only the major is taken from that page; nothing else of it is used."""
    for table in tables:
        head = table[0]
        col = next((i for i, h in enumerate(head) if "التخصص" in h), None)
        if col is None:
            continue
        for row in table[1:]:
            if len(row) > col and re.search(r"[A-Za-z\u0600-\u06FF]", row[col] or ""):
                return row[col].strip()
    return None


def balance_lines(info: dict) -> list[str]:
    lines = []
    total = info["total"]
    if total is not None:
        side = "owed to the university (debit)" if total > 0 else "in your favour (credit)" if total < 0 else "settled"
        lines.append(f"💰 *Total balance:* {abs(total):,.2f} JD — {side}".replace(".00", ""))
    due = [(t, h, b) for t, h, b in info["rows"] if b]
    for term, hours, amount in due:
        lines.append(f"• {term}" + (f" · {hours:g}h" if hours else "") + f" · {amount:,.2f} JD".replace(".00", ""))
    if info["rows"] and not due:
        lines.append("• Every semester shows a zero balance.")
    elif due and len(due) < len(info["rows"]):
        lines.append("• All other semesters: 0 JD")
    return lines


def parse_results(grids: list[dict]) -> list[dict]:
    """The rows of the course-results table — [{"code", "name", "activity",
    "status", "mark" (float|None), "passed" (True/False/None)}] — found by its
    column titles, so the order of the columns doesn't matter."""
    for grid in grids:
        heads = grid.get("heads", [])
        col = {key: next((i for i, h in enumerate(heads) if word in h), None)
               for key, word in (("code", "رمز"), ("name", "اسم"), ("activity", "النشاط"),
                                 ("status", "الحالة"), ("mark", "العلامة"), ("passed", "ناجح"))}
        if col["code"] is None or col["mark"] is None:
            continue
        rows = []
        for row in grid.get("rows", []):
            if len(row) < len(heads) or not re.search(r"\d", row[col["code"]]):
                continue                                      # the "no results" row
            passed = row[col["passed"]] if col["passed"] is not None else ""
            rows.append({
                "code": row[col["code"]], "name": row[col["name"]] if col["name"] is not None else "",
                "activity": row[col["activity"]] if col["activity"] is not None else "",
                "status": row[col["status"]] if col["status"] is not None else "",
                "mark": _number(row[col["mark"]]),
                "passed": True if passed == "\u2713" else False if passed == "\u2717" else None})
        return rows
    return []


def results_lines(raw: dict, tables: list[list[list[str]]], rows: list[dict] | None = None) -> list[str]:
    term = english_term(raw.get("term", "")) if raw.get("term") else "this semester"
    if rows:
        lines = [f"📅 *{term}*"]
        for r in rows:
            mark = f"{r['mark']:g}" if r["mark"] is not None else "—"
            verdict = "" if r["mark"] is None else (" ✅" if r["passed"] is not False and r["mark"] >= 50 else " ❌")
            lines.append(f"• {escape_md(r['code'])} {escape_md(r['name'])}"
                         + (f" ({escape_md(r['activity'])})" if r["activity"] else "") + f" · {mark}{verdict}")
        return lines
    if NO_RESULTS.search(raw.get("text", "")) and not any(len(t) > 1 and not NO_RESULTS.search(" ".join(t[1])) for t in tables):
        return [f"ℹ️ No results are published yet for {term}.", "They appear here as the instructors release them."]
    return [f"📅 *{term}*"] + table_lines(tables, translate=True)


def timetable_rows(html: str) -> list[dict]:
    """The sections table of the portal's home page: [{"code", "name", "section", "activity",
    "room", "time"}], where time is ZUJ's "[ 19:00_20:30 ] ن (ن) ر (ك)" (see schedule.parse_codes)."""
    import lxml.etree
    import lxml.html
    try:
        doc = lxml.html.fromstring(html or "<html></html>")
    except (ValueError, lxml.etree.ParserError):
        return []
    rows = []
    for tr in doc.xpath('//tbody[contains(@id, "schedules_data")]/tr'):
        row = {}
        for td in tr.xpath("./td"):
            title = td.xpath('.//span[contains(@class, "ui-column-title")]')
            key = TIMETABLE_COLUMNS.get(re.sub(r"\s+", " ", title[0].text_content()).strip()) if title else None
            if key:
                value = (td.xpath(".//label") or [td])[0].text_content()
                row[key] = re.sub(r"\s+", " ", value).strip()
        if row.get("time"):
            rows.append(row)
    return rows
