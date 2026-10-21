"""
Record what the real Moodle answers, so the tests can check the parsers
against your real pages — offline, any time.

Every request a full read makes (the course list, deadlines, each course's
activity tree and page, its pages, every forum, Teams meetings, attendance)
goes through a Recorder that keeps the exact answer. What the read produced
is stored next to it as the expected result. tests/test_architecture.py then
replays the answers (Replay) and runs the same read again: if a code change
makes your real pages read differently, the test says so.

Recordings hold your courses and posts, so tests/recorded/ is kept out of Git.
The session key is replaced by SESSKEY. Read-only: nothing is ticked, sent or
saved anywhere else.

Run:  .venv\\Scripts\\python -m uninotifier.tools.record_pages
      (re-run it whenever you want the tests to check the pages as they are today)
"""

import json
import sys
from urllib.parse import urlsplit

from uninotifier import activities, media, moodle, paths
from uninotifier.httpctx import HttpContext

RECORDED = paths.ROOT / "tests" / "recorded"
MOODLE_FILE = RECORDED / "moodle.json"
PLACEHOLDER = "SESSKEY"


def _key(method: str, url: str, data) -> str:
    return f"{method} {url} {data if isinstance(data, str) else json.dumps(data, sort_keys=True) if data else ''}"


class _Answer:
    """A recorded answer, shaped like httpctx.HttpResponse."""

    def __init__(self, rec: dict):
        self.status, self.url, self._body = rec["status"], rec["url"], rec["body"]
        self.ok = 200 <= self.status < 300
        self.headers = {k.lower(): v for k, v in rec["headers"].items()}

    def text(self) -> str:
        return self._body

    def json(self):
        return json.loads(self._body)

    def body(self) -> bytes:
        return self._body.encode("utf-8")


class Recorder:
    """Passes every request to the real context and keeps the answer."""

    def __init__(self, inner, sesskey: str):
        self.inner, self.sesskey, self.calls = inner, sesskey, {}
        self.request = self

    def _clean(self, value):
        return value.replace(self.sesskey, PLACEHOLDER) if isinstance(value, str) else value

    def _keep(self, method: str, url: str, data, resp):
        body = resp.text() if "text" in resp.headers.get("content-type", "text") or "json" in \
            resp.headers.get("content-type", "") else ""
        self.calls[_key(method, self._clean(url), self._clean(data))] = {
            "status": resp.status, "url": self._clean(resp.url), "body": self._clean(body),
            "headers": {k: v for k, v in resp.headers.items()
                        if k.lower() in ("content-type", "location", "last-modified", "content-length")}}
        return resp

    def get(self, url, timeout=30000, max_redirects=None):
        return self._keep("GET", url, None, self.inner.get(url, timeout=timeout, max_redirects=max_redirects))

    def head(self, url, timeout=30000, max_redirects=None):
        return self._keep("HEAD", url, None, self.inner.head(url, timeout=timeout, max_redirects=max_redirects))

    def post(self, url, data=None, headers=None, timeout=30000):
        return self._keep("POST", url, data, self.inner.post(url, data=data, headers=headers, timeout=timeout))


class Replay:
    """Answers from a recording; a request that was never recorded is an error
    (the code now asks the portal something new — record again)."""

    def __init__(self, calls: dict):
        self.calls = calls
        self.request = self

    def _answer(self, method, url, data=None):
        key = _key(method, url, data)
        if key not in self.calls:
            raise KeyError(f"not in the recording: {method} {urlsplit(url).path}")
        return _Answer(self.calls[key])

    def get(self, url, timeout=30000, max_redirects=None):
        return self._answer("GET", url)

    def head(self, url, timeout=30000, max_redirects=None):
        return self._answer("HEAD", url)

    def post(self, url, data=None, headers=None, timeout=30000):
        return self._answer("POST", url, data)


def summary(content: dict) -> dict:
    """What a read is compared on: every item's type, name and text fingerprint."""
    return {iid: [it["type"], it["name"], it.get("hash")] for iid, it in sorted(content.items())}


def read_everything(context, sesskey: str) -> dict:
    """The same reads a check makes, in the same way — the expected result."""
    courses = moodle.fetch_courses(context, sesskey)
    events = moodle.fetch_deadline_events(context, sesskey)
    contents = {cid: moodle.scan_course(context, sesskey, cid, course) for cid, course in courses.items()}
    forums, meetings, sessions = {}, {}, {}
    for content in contents.values():
        for iid, item in content.items():
            if item["type"] in media.CONTAINER_TYPES:
                posts = moodle.fetch_forum_posts(context, sesskey, item["url"])
                forums[iid] = sorted(p["id"] for p in posts) if posts is not None else None
            elif item["type"] in media.MEETING_TYPES:
                meetings[iid] = activities.parse_meeting(context.request.get(item["url"]).text())["url"]
            elif item["type"] in media.ATTENDANCE_TYPES:
                sessions[iid] = len(activities.parse_sessions(context.request.get(item["url"]).text()))
    return {"courses": {cid: c["name"] for cid, c in sorted(courses.items())},
            "deadlines": sorted(str(e.get("id")) for e in events),        # not filtered by today's date
            "content": {cid: summary(c) for cid, c in sorted(contents.items())},
            "forums": forums, "meetings": meetings, "sessions": sessions}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    http = HttpContext.from_state_file(paths.DATA / "auth_state.json")
    sesskey = http.sesskey() if http else None
    if not sesskey:
        sys.exit("The saved session isn't valid — let the monitor sign in once (or run tools.login_refresh).")
    recorder = Recorder(http, sesskey)
    expected = read_everything(recorder, sesskey)
    RECORDED.mkdir(parents=True, exist_ok=True)
    MOODLE_FILE.write_text(json.dumps({"calls": recorder.calls, "expected": expected}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    items = sum(len(c) for c in expected["content"].values())
    print(f"Recorded {len(recorder.calls)} answers: {len(expected['courses'])} courses, {items} items, "
          f"{len(expected['forums'])} forums, {len(expected['deadlines'])} deadlines -> {MOODLE_FILE}")


if __name__ == "__main__":
    main()
