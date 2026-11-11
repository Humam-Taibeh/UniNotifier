"""What every test file shares: the throw-away home the whole run lives in (set up before anything
of the project is imported), the guards that fail a run that reaches the network or a browser,
and the helpers, fakes and invented fixtures the tests use.

Run the suite:  .venv\\Scripts\\python tests\\test_architecture.py
"""

import logging
import sys
import os
import re
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# The whole run lives in a throw-away home, set up BEFORE anything of the project is imported:
# a made-up .env (your real one is never read, so no real token, password or id is even in
# memory) and an empty data folder (your state is never read or written). Both are passed on
# in the environment, so every child process a test starts is isolated the same way.
_HOME = Path(tempfile.mkdtemp(prefix="uninotifier-test-home-"))
(_HOME / ".env").write_text(
    "TELEGRAM_BOT_TOKEN=123456:test-token\nTELEGRAM_CHAT_ID=42\nADMIN_USER_ID=42\n"
    "ZUJ_USERNAME=tester@example.invalid\nZUJ_PASSWORD=not-a-real-password\n"
    f"ARCHIVE_ROOT={_HOME / 'archive'}\n", encoding="utf-8")       # never the real Documents/University
for _key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ADMIN_USER_ID", "ZUJ_USERNAME", "ZUJ_PASSWORD",
             "PORTAL_USERNAME", "PORTAL_PASSWORD", "STUDENT_ID", "ARCHIVE_ROOT", "HEALTHCHECK_URL",
             "BOT_HEALTHCHECK_URL", "BOT_POLL_TIMEOUT", "UNINOTIFIER_MANUAL", "CAMBRIDGE_ASSIGNMENTS_URL",
             "CAMBRIDGE_UNTIL", "CAMBRIDGE_EMAIL", "CAMBRIDGE_PASSWORD"):
    os.environ.pop(_key, None)                      # nothing inherited from a shell that loaded the real one
# git gives a hook its own variables (GIT_DIR, GIT_INDEX_FILE, …, as `git rev-parse --local-env-vars`
# lists them): a test that runs git in its throw-away folder would otherwise write into this repository
for _key in ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
             "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
             "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
             "GIT_COMMON_DIR"):
    os.environ.pop(_key, None)
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
os.environ["UNINOTIFIER_ENV"] = str(_HOME / ".env")
os.environ["UNINOTIFIER_DATA"] = str(_HOME / "data")
# your timetable (schedule.json, assets/) is git-ignored and yours alone: the tests read a fixed copy
# of a real one and stand-in timetable files, so a fresh clone passes exactly like this PC
(_HOME / "schedule.json").write_bytes((_FIXTURES / "schedule.json").read_bytes())
os.environ["UNINOTIFIER_SCHEDULE"] = str(_HOME / "schedule.json")
(_HOME / "assets").mkdir()
(_HOME / "assets" / "Weekly_Academic_Schedule.pdf").write_bytes(b"%PDF-1.4\n% stand-in timetable\n%%EOF\n")
(_HOME / "assets" / "Weekly_Schedule.png").write_bytes(bytes.fromhex(        # a 1x1 PNG
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffff3f0005fe02fea7d6a4ab0000000049454e44ae426082"))
os.environ["UNINOTIFIER_ASSETS"] = str(_HOME / "assets")
# what the screens show comes from a fixed copy of the tracker state (tests/fixtures), not from yours
(_HOME / "data").mkdir()
(_HOME / "data" / "saved_items.json").write_bytes((_FIXTURES / "saved_items.json").read_bytes())

from uninotifier import paths  # noqa: E402

assert paths.ENV_FILE == _HOME / ".env" and paths.DATA == _HOME / "data" and paths.SCHEDULE_FILE == _HOME / "schedule.json" \
    and paths.ASSETS == _HOME / "assets", "isolation must be set before any import"

# No test may reach the outside world or start a browser by accident. Both guards are installed
# before the project is imported (scan.py binds sync_playwright at import). A blocked attempt is
# refused (to the code it looks like no network) AND recorded: the code's own catch-alls may
# swallow the refusal, so the runner checks GUARD_HITS after every test and fails the run.
# Loopback stays open for the tests' own local servers; test_mfa_flow drives a local page with
# _REAL_SYNC_PLAYWRIGHT on purpose.
import socket  # noqa: E402

import playwright.sync_api as _playwright_api  # noqa: E402

GUARD_HITS: list[str] = []
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_REAL_CONNECT, _REAL_CONNECT_EX, _REAL_GETADDRINFO = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
_REAL_SYNC_PLAYWRIGHT = _playwright_api.sync_playwright


def _outside(address) -> str | None:
    host = address[0] if isinstance(address, tuple) else None
    return str(host) if host is not None and str(host) not in _LOOPBACK else None


def _guarded_connect(self, address):
    host = _outside(address)
    if host:
        GUARD_HITS.append(f"socket connect to {host}")
        raise ConnectionRefusedError(f"test guard: no network in tests ({host})")
    return _REAL_CONNECT(self, address)


def _guarded_connect_ex(self, address):
    host = _outside(address)
    if host:
        GUARD_HITS.append(f"socket connect to {host}")
        raise ConnectionRefusedError(f"test guard: no network in tests ({host})")
    return _REAL_CONNECT_EX(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host is not None and str(host) not in _LOOPBACK:
        GUARD_HITS.append(f"DNS lookup of {host}")
        raise socket.gaierror(socket.EAI_NONAME, f"test guard: no network in tests ({host})")
    return _REAL_GETADDRINFO(host, *args, **kwargs)


def _guarded_sync_playwright(*args, **kwargs):
    GUARD_HITS.append("sync_playwright() — a browser was started")
    raise RuntimeError("test guard: no browser in tests (use _REAL_SYNC_PLAYWRIGHT on purpose)")


socket.socket.connect, socket.socket.connect_ex = _guarded_connect, _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo
_playwright_api.sync_playwright = _guarded_sync_playwright

from uninotifier import schedule

SID = "209900001"
# Nothing a test logs may reach the real monitor.log / bot.log: the root logger is set up
# here first, so monitor.py's and bot.py's own logging setup (basicConfig) does nothing.
logging.basicConfig(handlers=[logging.NullHandler()], force=True)
logging.disable(logging.INFO)

from uninotifier import daily  # noqa: E402
from uninotifier import finals  # noqa: E402
from uninotifier import gpa  # noqa: E402
from uninotifier import lecturelinks  # noqa: E402
from uninotifier import lectures  # noqa: E402
from uninotifier import reminders  # noqa: E402

# Every state file a test could write — reminders, GPA, Finals Watch, lecture links,
# delivery claims — lives in a temporary folder for the whole run, never next to the
# real ones; a test that forgets to restore a path can't reach your data either.
_REMINDER_TMP = Path(tempfile.mkdtemp(prefix="uninotifier-test-"))
reminders.STATE_FILE = _REMINDER_TMP / "reminders.json"
reminders.SOURCES_FILE = _REMINDER_TMP / "reminder_sources.json"
reminders.CLAIM_DIR = _REMINDER_TMP / "reminders_sent"
gpa.STATE_FILE = _REMINDER_TMP / "gpa.json"
finals.STATE_FILE = _REMINDER_TMP / "finals.json"
lecturelinks.LINKS_FILE = _REMINDER_TMP / "lecture_links.json"
lectures.CLAIM_DIR = _REMINDER_TMP / "lecture_sent"
lectures.STATE_FILE = _REMINDER_TMP / "saved_items.json"
daily.CLAIM_DIR = _REMINDER_TMP / "daily_sent"

# The fail-loud layer must never reach Telegram or the real files from a test: its messages
# land in OUTBOX, its hold-back record, the bot's pulse and the schedule's last good copy
# live in the temp folder.
from uninotifier import access  # noqa: E402
from uninotifier import failsafe  # noqa: E402
from uninotifier import telegram  # noqa: E402
from uninotifier import heartbeat  # noqa: E402

failsafe.STATE_FILE = _REMINDER_TMP / "failsafe.json"
failsafe._credentials = lambda: ("123456:test-token", "42")
OUTBOX: list[str] = []
failsafe.send = lambda token, chat, text, *a, **k: OUTBOX.append(text) or True
heartbeat.FILE = _REMINDER_TMP / "bot_heartbeat.json"
telegram.STATS_FILE = _REMINDER_TMP / "telegram_stats.json"
from uninotifier import fileids  # noqa: E402

fileids.STORE = _REMINDER_TMP / "file_ids.json"
telegram.MIN_SEND_INTERVAL = 0


def _no_network(url, **kwargs):
    raise AssertionError(f"a test reached Telegram: {url.split('/bot')[0]} (patch telegram.post)")


_REAL_POST = telegram.post
telegram.post = _no_network          # no test may talk to the real Telegram
schedule.LAST_GOOD_FILE = _REMINDER_TMP / "schedule.last_good.json"


class _Package:
    """One handle on a package that is split into submodules (uninotifier.monitor,
    uninotifier.bot): reading or overriding a name reaches the submodule that defines
    it, so a test can still say `monitor.DATA_FILE = …` or `bot.send_view = …` and the
    code that uses the name sees the change. An imported name (is_authenticated) is
    overridden in every part that imported it; a name nobody has is an error."""

    def __init__(self, package: str):
        import ast
        import importlib
        import pkgutil
        pkg = importlib.import_module(package)
        object.__setattr__(self, "_modules", [importlib.import_module(f"{package}.{m.name}")
                                              for m in pkgutil.iter_modules(pkg.__path__)])
        homes = {}
        for module in self._modules:
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for node in tree.body:
                names = ([node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else
                         [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)] +
                         ([node.target.id] if isinstance(node, ast.AnnAssign) else []))
                for name in names:
                    homes.setdefault(name, module)
        object.__setattr__(self, "_homes", homes)

    def _home(self, name):
        if name in self._homes:
            return self._homes[name]
        for module in self._modules:                  # an imported name (a module such as daily)
            if hasattr(module, name):
                return module
        raise AttributeError(name)

    def __getattr__(self, name):
        return getattr(self._home(name), name)

    def __setattr__(self, name, value):
        if name in self._homes:
            setattr(self._homes[name], name, value)
            return
        users = [m for m in self._modules if hasattr(m, name)]   # an imported name: every part that uses it
        if not users:
            raise AttributeError(f"nothing in the package defines {name}")
        for module in users:
            setattr(module, name, value)


_PACKAGES: dict = {}


def _package(name: str) -> _Package:
    if name not in _PACKAGES:
        _PACKAGES[name] = _Package(name)
    return _PACKAGES[name]


COURSE_PAGE = """<html><body><ul>
<li data-for="section" data-id="10" data-number="0"><div data-for="sectioninfo">
  <div class="section_availability"></div></div>
  <ul data-for="cmlist">
  <li class="activity label modtype_label" id="module-19793" data-for="cmitem" data-id="19793">
    <button data-action="toggle-manual-completion" data-toggletype="manual:mark-done" data-cmid="19793">Mark as done</button>
    <div class="activity-altcontent text-break"><div class="no-overflow">
      <h3><img src="https://x/pluginfile.php/1/mod_label/intro/banner.jpg?time=5"></h3>
      <p>Arabic Communication</p><p>Lecture time :&nbsp;</p><p>Office hours :</p></div></div></li>
  <li class="activity forum modtype_forum" id="module-20770" data-for="cmitem" data-id="20770">
    <button data-action="toggle-manual-completion" data-toggletype="manual:undo" data-cmid="20770">Done</button></li>
  </ul></li>
<li data-for="section" data-id="11" data-number="1"><div data-for="sectioninfo">
  <div class="summarytext"><p>Join the lecture: <a href="https://teams.microsoft.com/l/meetup-join/abc">Teams</a></p></div></div>
  <ul data-for="cmlist">
  <li class="activity page modtype_page" id="module-21747" data-for="cmitem" data-id="21747">
    <button data-action="toggle-manual-completion" data-toggletype="manual:mark-done" data-cmid="21747">Mark as done</button>
    <div class="activity-altcontent text-break activity-description"><div class="no-overflow">
      <h4>Overview</h4><p>Aims to improve skills.</p><h4>Grades</h4>
      <ol><li>30% activities</li><li>30% midterm</li><li>40% final</li></ol></div></div></li>
  </ul></li>
<li data-for="section" data-id="12" data-number="2"><div data-for="sectioninfo"><p>&nbsp;.</p></div></li>
</ul></body></html>"""


def _failure_policy_checks(monitor):
    sent, saved = [], {"alerts": {}, "failures": {}}
    monitor.send_telegram = lambda text, url=None: sent.append(text) or True
    monitor.save_data = lambda data: None
    for _ in range(2):
        monitor.record_failure(saved, "network", "down")
    assert sent == [], "a transient network error must not alert"
    monitor.record_failure(saved, "network", "down")
    assert len(sent) == 1 and "3 consecutive checks" in sent[0]
    monitor.record_failure(saved, "network", "down")
    assert len(sent) == 1, "cooldown"
    monitor.clear_failures(saved)
    assert "Back to normal" in sent[-1] and saved["failures"] == {}
    monitor.record_failure(saved, "mfa", "renew")      # needs the user: immediate
    assert sent[-1] == "renew"
    assert monitor.failure_kind(Exception("Page.goto: net::ERR_NAME_NOT_RESOLVED")) == "network"
    assert monitor.failure_kind(KeyError("data")) == "unexpected"


FAKE_MICROSOFT = """<html><body><div id="lightbox"></div><script>
const screens = {
  number: `<div id="idDiv_SAOTCAS_Title">Approve sign in request</div>
           <div id="idRichContext_DisplaySign">47</div>
           <input type="checkbox" id="idChkBx_SAOTCAS_TD">
           <a id="signInAnotherWay" href="#" onclick="show('proofs')">I can't use my Microsoft Authenticator app right now</a>`,
  proofs: `<div id="idDiv_SAOTCS_Proofs">
           <div role="button" data-value="PhoneAppNotification" onclick="show('number')">Approve a request</div>
           <div role="button" data-value="OneWaySMS" onclick="show('code')">Text +XXX XXXXXXX12</div></div>`,
  code: `<div id="idDiv_SAOTCC_Description">We texted your phone +XXX XXXXXXX12.</div>
         <input id="idTxtBx_SAOTCC_OTC" name="otc"><span id="idSpan_SAOTCC_Error_OTC" style="display:none">Wrong</span>
         <input type="button" id="idSubmit_SAOTCC_Continue" value="Verify"
                onclick="if (document.getElementById('idTxtBx_SAOTCC_OTC').value === '123456') show('done');
                         else document.getElementById('idSpan_SAOTCC_Error_OTC').style.display = 'inline'">`,
  timeout: `<div id="idDiv_SAASTO_Title">We didn't hear from you</div>
            <a id="idA_SAASTO_Resend" href="#" onclick="show('number')">Send another request</a>`,
  denied: `<div id="idDiv_SAASDS_Title">Request denied</div>`,
  done: `<div id="finished">ok</div>`,
};
function show(name) { document.getElementById('lightbox').innerHTML = screens[name]; return false; }
</script></body></html>"""


def _portal_home(rows):
    """The portal's home page as PrimeFaces renders it: the sections table, a title span and a label per cell."""
    titles = ("رمز المقرر", "اسم المقرر", "الشعبة", "النشاط", "قاعة", "الوقت")
    body = "".join("<tr>" + "".join(f'<td><span class="ui-column-title">{title}</span><label>{value}</label></td>'
                                    for title, value in zip(titles, row)) + "</tr>" for row in rows)
    return ('<html><body><form id="contents"><div id="contents:schedules" class="ui-datatable"><table>'
            f'<thead><tr>{"".join(f"<th>{title}</th>" for title in titles)}</tr></thead>'
            f'<tbody id="contents:schedules_data">{body or ""}</tbody></table></div></form></body></html>')


PORTAL_ROWS = [("0900111", "مادة وجاهية", "1", "نظري", "101", "[ 09:30_11:00 ] ح (و) ث (و)"),
               ("0900222", "مادة مدمجة", "2", "نظري", "202", "[ 12:30_14:00 ] ح (م) ث (ن)"),
               ("0900333", "مادة إلكترونية", "3", "نظري", "", "[ 19:00_20:30 ] ن (ن) ر (ك)"),
               ("0900444", "مادة وجاهية", "1", "عملي", "60", "[ 16:00_18:00 ] س (و)")]


def monitor_failure_kind(exc):
    monitor = _package("uninotifier.monitor")
    return monitor.failure_kind(exc)


def _sem(term, courses, cumulative=None):
    """A transcript semester as portal.parse_transcript returns it; courses are
    (code, name, hours, mark) — mark None while in progress."""
    return {"term": term, "average": None, "cumulative": cumulative, "courses": [
        {"code": c, "name": n, "hours": h, "mark": m,
         "passed": None if m is None else m >= 50, "repeated": False} for c, n, h, m in courses]}


def _record():
    return [
        _sem("First semester 2026/2027", [("0101221", "جبر خطي (1)", 3, None), ("0135220", "قواعد البيانات", 3, None),
                                          ("0101272", "تحليل عددي (1)", 3, None)], 50.0),
        _sem("First semester 2025/2026", [("0112110", "رياضيات متقطعة", 3, 80), ("0101221", "جبر خطي (1)", 3, 35),
                                          ("0142251", "قواعد البيانات", 3, 35)], 50.0),   # the database course had another code
    ]


PLAN_HEADER = ("الحالة", "رمز المقرر", "اسم المقرر", "عدد الساعات المعتمدة", "المتطلبات السابقة")
PLAN_CATEGORIES = (                     # (name, required, passed, left, [(status, code, name, hours)]) — an invented plan
    ("متطلبات جامعية (اجبارية)", 9, 3, 6, [("مجتازة", "0900101", "مهارات الدراسة", 3),
                                           ("قيد الدراسة", "0900102", "الكتابة الأكاديمية", 3),
                                           ("", "0900103", "العمل التطوعي", 3)]),
    ("متطلبات تخصص (اختيارية)", 6, 3, 3, [("مجتازة", "0900201", "تصميم الواجهات", 3),
                                          ("قيد الدراسة", "0900202", "أمن الشبكات", 3),
                                          ("", "0900203", "الحوسبة السحابية", 3),
                                          ("", "0900204", "تطوير الألعاب", 3)]),
    ("متطلبات تخصص (اجبارية)", 7, 0, 7, [("راسب", "0900301", "هياكل البيانات", 3),
                                         ("", "0900302", "نظم التشغيل", 3),
                                         ("", "0900303", "حلقة بحث", 1)]),
    ("مواد استدراكية (اجبارية)", 6, 6, 0, [("معفاة", "0900001", "اللغة الانجليزية الاستدراكية", 3),
                                           ("معفاة", "0900002", "مهارات الحاسوب الاستدراكية", 3)]),
)


def _plan_html(categories=PLAN_CATEGORIES) -> str:
    """The portal's study-plan page in miniature: the categories repeated under each of its three tabs,
    PrimeFaces' phone-layout copy of every column title inside each cell, the totals in the table's footer."""
    def table(courses, totals):
        head = "".join(f'<th><span class="ui-column-title">{t}</span></th>' for t in PLAN_HEADER)
        rows = "".join("<tr>" + "".join(f'<td><span class="ui-column-title">{t}</span><label>{v}</label></td>'
                                        for t, v in zip(PLAN_HEADER, (*course, ""))) + "</tr>" for course in courses)
        foot = "".join(f"<label>{t}</label><label>{v}</label>"
                       for t, v in zip(("عدد الساعات المعتمدة", "اجتاز منها ", "الساعات المتبقة"), totals))
        return (f'<div class="ui-datatable"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'
                f'<div class="ui-datatable-footer"><div>{foot}</div></div></div>')

    def group(tab):
        links = "".join(f'<li><a href="#p:{tab}:c{n}">{name}</a></li>' for n, (name, *_) in enumerate(categories))
        panels = "".join(f'<div id="p:{tab}:c{n}" class="ui-tabs-panel">{table(courses, (need, done, left))}</div>'
                         for n, (_, need, done, left, courses) in enumerate(categories))
        return f'<div class="ui-tabs"><ul class="ui-tabs-nav">{links}</ul><div class="ui-tabs-panels">{panels}</div></div>'
    tabs = (("analysePlan", "تحليل الخطة"), ("passedCourses", "المواد المجتازة من الخطة"),
            ("remainCourses", "المواد المتبقية من الخطة"))
    return ('<html><body><div class="ui-tabs"><ul class="ui-tabs-nav">'
            + "".join(f'<li><a href="#p:{tab}">{title}</a></li>' for tab, title in tabs) + '</ul><div class="ui-tabs-panels">'
            + "".join(f'<div id="p:{tab}" class="ui-tabs-panel">{group(tab)}</div>' for tab, _ in tabs)
            + "</div></div></body></html>")


def _sample_plan() -> dict:
    from uninotifier import degreeplan
    return degreeplan.parse(_plan_html())


REGISTRATION_PLAN = (                   # an invented plan with prerequisites: (status, code, name, hours, what comes first)
    ("متطلبات جامعية (اجبارية)", 9, 6, 3, [("مجتازة", "0900101", "مهارات الدراسة", 3),
                                           ("مجتازة", "0900102", "الكتابة الأكاديمية", 3),
                                           ("", "0900103", "العلوم العسكرية", 3)]),
    ("متطلبات تخصص (اجبارية)", 22, 6, 16, [("مجتازة", "0900211", "مبادئ البرمجة", 3),
                                           ("راسب", "0900212", "هياكل البيانات", 3, "(0900211 مبادئ البرمجة)"),
                                           ("", "0900313", "الخوارزميات", 3, "(0900212 هياكل البيانات)"),
                                           ("قيد الدراسة", "0900251", "قواعد البيانات (1)", 3),
                                           ("", "0900352", "قواعد البيانات (2)", 3, "(0900251 قواعد البيانات (1))"),
                                           ("", "0900314", "نظم التشغيل", 3, "(0900999 مادة من خطة أخرى)"),
                                           ("", "0900490", "تدريب ميداني", 3),
                                           ("", "0900492", "مشروع التخرج", 1)]),
    ("متطلبات تخصص (اختيارية)", 3, 0, 3, [("", "0900331", "أمن الشبكات", 3),
                                          ("", "0900332", "الحوسبة السحابية", 3)]),
    ("مواد استدراكية (اجبارية)", 6, 3, 3, [("معفاة", "0900001", "اللغة الانجليزية الاستدراكية", 3),
                                           ("", "0900002", "مهارات الحاسوب الاستدراكية", 3)]),
)


def _sample_transcript():
    """A parsed transcript: a term in progress, two finished terms (one with a failed attempt a later
    retake replaced), Arabic and Latin names, a name with Markdown characters."""
    from uninotifier import portal
    heads = ["", "رمز المقرر", "اسم المقرر", "الحالة", "عدد الساعات", "العلامة", "ناجح؟", "معادة"]
    ok, no = "✓", "✗"

    def row(code, name, hours, mark, passed, repeated=False):
        return ["1", code, name, "مسجلة", str(hours), str(mark), passed, ok if repeated else ""]

    raw = [
        {"legend": "الفصل الأول 2026/2027 20261", "heads": heads, "text": "المعدل التراكمي 71.3",
         "rows": [row("0135220", "قواعد البيانات", 3, "", ""), row("0101221", "جبر خطي (1)", 3, "", ""),
                  row("0135321", "Big_Data *2*", 3, "", "")]},
        {"legend": "الفصل الثاني 2025/2026 20252", "heads": heads, "text": "المعدل الفصلي 76 المعدل التراكمي 71.3",
         "rows": [row("0125220", "برمجة تطبيقات الانترنت (HTML)", 3, 88, ok), row("0135214", "الاحصاء", 3, 74, ok)]},
        {"legend": "الفصل الأول 2024/2025 20241", "heads": heads, "text": "المعدل الفصلي 58",
         "rows": [row("0101221", "جبر خطي (1)", 3, 35, no, True), row("0142251", "قواعد البيانات", 3, 35, no),
                  row("0420155", "القانون في حياتنا", 2, 95, ok), row("0112212", "تراكيب البيانات", 3, 30, no)]},
    ]
    return portal.parse_transcript(raw)


def _approx(a, b, tol=1e-6):
    return a is not None and abs(a - b) < tol


def _legacy_markdown_ok(text: str) -> bool:
    """Telegram's legacy Markdown rejects a message with an unclosed * _ or `. Read left to right the
    way Telegram does: a backslash escapes _ * ` [ outside a code span, inside one it is a letter."""
    text = re.sub(r"(?<!\\)\[[^\]]*\]\([^)]*\)", "", text)            # [label](url): its underscores are the url's
    i, in_code, stars, unders = 0, False, 0, 0
    while i < len(text):
        c = text[i]
        if in_code:
            in_code = c != "`"
        elif c == "\\" and i + 1 < len(text) and text[i + 1] in "_*`[":
            i += 1
        elif c == "`":
            in_code = True
        elif c == "*":
            stars += 1
        elif c == "_":
            unders += 1
        i += 1
    return not in_code and stars % 2 == 0 and unders % 2 == 0


OWNER_ID = "424242"


def _tg(user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="private", text=None, data=None, is_bot=False, **user_extra):
    """An update the way Telegram sends it: a message (text) or a button press (data)."""
    user = {"id": int(user_id), "is_bot": is_bot, "first_name": "T", **user_extra}
    chat = {"id": int(chat_id), "type": chat_type}
    if data is not None:
        return {"update_id": 1, "callback_query": {"id": "cb", "from": user, "data": data,
                                                    "message": {"message_id": 5, "chat": chat}}}
    return {"update_id": 1, "message": {"message_id": 1, "from": user, "chat": chat, "text": text}}


def _as_owner(bot):
    """The bot and the access gate believe the owner is OWNER_ID; returns what to restore."""
    keep = (bot.CHAT_ID, access.ADMIN_USER_ID)
    bot.CHAT_ID, access.ADMIN_USER_ID = OWNER_ID, OWNER_ID
    return keep


def _fresh_failsafe():
    failsafe.wait_idle()          # a delivery an earlier test started in the background may still be writing the file
    failsafe.STATE_FILE.unlink(missing_ok=True)
    failsafe._inflight.clear()
    OUTBOX.clear()


def _resilience_checks(subprocess, sys, requests, gitsync, httpctx, monitor):
    # 1. per request: connection resets / timeouts / 502-504 are retried with backoff
    class Resp:
        def __init__(self, code):
            self.status_code, self.url, self.headers, self.text = code, "u", {}, ""
    calls = []

    def flaky(outcomes):
        def request(method, url, timeout=None, allow_redirects=True, **kw):
            calls.append(url)
            out = outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return Resp(out)
        return request
    keep = httpctx.BACKOFF_SECONDS
    httpctx.BACKOFF_SECONDS = 0
    try:
        http = httpctx.HttpContext(requests.cookies.RequestsCookieJar())
        session = http._session()
        session.request = flaky([requests.ConnectionError("Connection aborted. 10054"),
                                 requests.ReadTimeout("handshake timed out"), 200])
        assert http.get("https://x/a?sesskey=s").status == 200 and len(calls) == 3
        session.request = flaky([503, 200])
        assert http.get("https://x/b").status == 200
        session.request = flaky([404])
        assert http.get("https://x/c").status == 404, "a real answer is not retried"
        session.request = flaky([requests.ConnectionError("down")] * 3)
        try:
            http.get("https://x/d")
            raise AssertionError("should have raised after the retries")
        except requests.ConnectionError:
            pass
        session.request = flaky([502, 502, 502])
        assert http.get("https://x/e").status == 502, "after the retries the answer is returned as is"
    finally:
        httpctx.BACKOFF_SECONDS = keep

    # 2. per check: a network failure is retried, sign-in and other errors are not
    keep = (monitor.run_scan, monitor.SCAN_RETRY_SECONDS)
    monitor.SCAN_RETRY_SECONDS = (0, 0)
    attempts = []

    def scan(outcomes):
        def run_scan(saved, notify_new, mfa_handler=None):
            attempts.append(1)
            out = outcomes.pop(0)
            if isinstance(out, BaseException):
                raise out
            return out
        return run_scan
    try:
        monitor.run_scan = scan([requests.ConnectionError("Connection aborted."), {"ok": 1}])
        assert monitor.scan_with_retries({}, True) == {"ok": 1} and monitor.SUMMARY["attempts"] == 2
        for error, tries in ((monitor.LoginError("bad password"), 1), (ValueError("parser broke"), 1),
                             (requests.ReadTimeout("Read timed out"), 3)):
            attempts.clear()
            monitor.run_scan = scan([error] * 3)
            try:
                monitor.scan_with_retries({}, True)
                raise AssertionError("should have raised")
            except type(error):
                pass
            assert len(attempts) == tries, (error, attempts)
    finally:
        monitor.run_scan, monitor.SCAN_RETRY_SECONDS = keep

    # 3. a bounded command really ends — even when its own child holds the output open
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "grandchild.pid"
        hold = ("import subprocess, sys, time\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                f"open(r'{marker}', 'w').write(str(p.pid))\n"
                "time.sleep(120)\n")
        started = time.monotonic()
        try:
            gitsync.run_bounded([sys.executable, "-c", hold], timeout=3)
            raise AssertionError("should have timed out")
        except subprocess.TimeoutExpired:
            pass
        assert time.monotonic() - started < 20, time.monotonic() - started
        assert not monitor.pid_alive(int(marker.read_text())), "the whole tree is killed"
    assert gitsync.run_bounded([sys.executable, "-c", "print('hi')"], timeout=30).stdout.strip() == "hi"

    # 4. the run lock: a dead holder's lock is taken over at once; a live holder's is respected
    with tempfile.TemporaryDirectory() as tmp:
        keep = monitor.LOCK_FILE
        monitor.LOCK_FILE = Path(tmp) / "monitor.lock"
        try:
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            monitor.LOCK_FILE.write_text(str(dead.pid))
            assert monitor.acquire_lock() and monitor.LOCK_FILE.read_text() == str(os.getpid())
            alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                monitor.LOCK_FILE.write_text(str(alive.pid))
                assert not monitor.acquire_lock()
            finally:
                alive.kill()
            monitor.LOCK_FILE.write_text("")                 # being written: its age decides
            assert not monitor.acquire_lock()
        finally:
            monitor.LOCK_FILE = keep

    # 5. a retried check finds its own download "identical" — still a new file, attached
    with tempfile.TemporaryDirectory() as tmp:
        saved = Path(tmp) / "Lecture 3.pdf"
        saved.write_bytes(b"%PDF")
        first = {"v": True}

        def download(context, url, dest):
            dup, first["v"] = not first["v"], False
            return {"path": saved, "sha256": "h", "modified": None, **({"duplicate": True} if dup else {})}
        keep = (monitor.archive.download_resource, monitor.archive.target)
        monitor.archive.download_resource = download
        monitor.archive.target = lambda course, kind, name: Path(tmp)
        try:
            item = {"name": "Lecture 3", "type": "resource", "course": "20261-Big Data-AllSec-3-",
                    "url": "https://x/mod/resource/view.php?id=901"}
            assert not monitor.process_item(None, "s", item).get("duplicate")     # attempt 1
            assert not monitor.process_item(None, "s", item)["duplicate"], "attempt 2: its own copy"
            other = {**item, "name": "Lecture 3 (again)", "url": "https://x/mod/resource/view.php?id=902"}
            assert monitor.process_item(None, "s", other)["duplicate"], "another item sharing the file"
        finally:
            monitor.archive.download_resource, monitor.archive.target = keep


class _FakePage:
    """Just enough of a Playwright page for the maintenance checks."""

    def __init__(self, text="", title="", png=b"PNG", broken=False, url="about:blank"):
        self.text, self._title, self.png, self.broken, self.url = text, title, png, broken, url

    def title(self):
        if self.broken:
            raise RuntimeError("page closed")
        return self._title

    def inner_text(self, selector, timeout=None):
        if self.broken:
            raise RuntimeError("page closed")
        return self.text

    def screenshot(self, type="png", timeout=None):
        if self.broken:
            raise RuntimeError("page closed")
        return self.png

    def goto(self, url, wait_until=None, timeout=None):
        pass


TEAMS_PAGE = """<html><body><div role="main"><h2>رابط الدخول إلى المحاضرة</h2>
<table class="generaltable w-auto mb-4"><tbody>
<tr><td class="cell c0">Meeting type</td><td class="cell c1 lastcol">Recurring meeting</td></tr>
<tr><td class="cell c0">Repeat on</td><td class="cell c1 lastcol">Monday, Wednesday</td></tr>
<tr><td class="cell c0">Meeting time</td><td class="cell c1 lastcol">7:00 PM – 8:30 PM</td></tr>
<tr class="lastrow"><td class="cell c0">Organizer</td><td class="cell c1 lastcol">Dr. Someone</td></tr>
</tbody></table>
<input type="text" id="teamscheduler-meetingurl" class="form-control"
 value="https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC%40thread.v2/0?context=%7b%22Tid%22%3a%22x%22%7d" readonly>
<form method="get" action="https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC%40thread.v2/0">
<button type="submit">Join Teams Meeting</button></form></div></body></html>"""

ATTENDANCE_PAGE = """<html><body><div role="main"><table class="generaltable attwidth"><thead><tr>
<th class="header c0 datecol">Date</th><th class="header c1 desccol">Description</th>
<th class="header c2 statuscol">Status</th><th class="header c3 pointscol">Points</th></tr></thead><tbody>
<tr><td class="datecol cell c0"><nobr>Mon 12 Oct 2026</nobr> <nobr>7PM - 8:30PM</nobr></td>
 <td class="desccol cell c1"><div class="text_to_html">Regular class session</div></td>
 <td class="statuscol cell c2">Present</td><td class="pointscol cell c3">2 / 2</td></tr>
<tr><td class="datecol cell c0"><nobr>Wed 14 Oct 2026</nobr> <nobr>7PM - 8:30PM</nobr></td>
 <td class="desccol cell c1"><div class="text_to_html">Regular class session</div></td>
 <td class="statuscol cell c2"><a href="https://elearning.zuj.edu.jo/mod/attendance/attendance.php?sessid=55&amp;sesskey=SECRET">Submit attendance</a></td>
 <td class="pointscol cell c3">? / 2</td></tr>
<tr><td class="datecol cell c0"><nobr>Mon 19 Oct 2026</nobr> <nobr>7PM - 8:30PM</nobr></td>
 <td class="desccol cell c1"><div class="text_to_html">Regular class session</div></td>
 <td class="statuscol cell c2">?</td><td class="pointscol cell c3">? / 2</td></tr>
</tbody></table></div></body></html>"""

CHECK_IN_PAGE = """<html><body><form action="attendance.php" method="post">
<input type="hidden" name="sessid" value="55"><input type="hidden" name="sesskey" value="SECRET">
<input type="radio" name="status" id="s1" value="101"><label for="s1">Present</label>
<input type="radio" name="status" id="s2" value="102"><label for="s2">Late</label>
<input type="submit" name="submitbutton" value="Save changes"></form></body></html>"""


def _check_in_form(choices=("Present (Available for 10 minutes)", "Late", "Absent"), extra=""):
    """mod_attendance's studentattendance form as Moodle 4 (Boost) renders it, inside a page
    with another form (the search box) — the shape check_in() has to cope with."""
    radios = "".join(f'<div class="form-check form-check-inline fitem"><label class="form-check-label">'
                     f'<input type="radio" class="form-check-input" name="status" id="id_status_{101 + n}" '
                     f'value="{101 + n}">{label}</label></div>' for n, label in enumerate(choices))
    group = (f'<div class="form-group row fitem" id="fgroup_id_statusarray"><label class="col-form-label">'
             f'Student Name:</label>{radios}</div>') if choices else ""
    return f"""<html><body><form action="https://elearning.zuj.edu.jo/search/index.php"><input name="q" type="text" value="">
</form><div role="main"><form autocomplete="off" action="https://elearning.zuj.edu.jo/mod/attendance/attendance.php"
 method="post" accept-charset="utf-8" id="mform1" class="mform"><div style="display: none;">
<input name="sessid" type="hidden" value="55" /><input name="sesskey" type="hidden" value="SECRETKEY1" />
<input name="_qf__mod_attendance\\form\\studentattendance" type="hidden" value="1" />
<input name="mform_isexpanded_id_session" type="hidden" value="1" /></div>
<fieldset class="clearfix collapsible" id="id_session"><legend class="ftoggler">Wednesday, 4 November 2026 7:00PM</legend>
{extra}{group}</fieldset><div class="form-group row fitem femptylabel" id="fgroup_id_buttonar">
<input type="submit" class="btn btn-primary" name="submitbutton" id="id_submitbutton" value="Save changes">
<input type="submit" class="btn btn-secondary" name="cancel" id="id_cancel" value="Cancel" data-cancel="1">
</div></form></div><script>M.cfg = {{"sesskey":"SECRETKEY1"}};</script></body></html>"""


def _guest_sem(term, rows, average=None):
    return {"term": term, "average": average, "cumulative": None,
            "courses": [{"code": c, "name": n, "hours": h, "mark": m,
                         "passed": None if m is None else m >= 50, "repeated": False} for c, n, h, m in rows]}


def _with_env(values: dict, fn):
    """Run fn with these process variables set ("" = set but empty, None = removed), then put them back."""
    keep = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return fn()
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


REPO_PATH = "github.com/Humam-Taibeh/UniNotifier"      # the project's own public address
PUBLIC_REPO = f"https://{REPO_PATH}.git"


def portal_sections():
    from uninotifier import portal
    return portal.SECTIONS
