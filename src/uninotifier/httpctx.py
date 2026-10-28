"""
Plain-HTTP access to the Moodle portal, shaped like Playwright's request
context so the functions written for the browser (ajax_call, fetch_html,
forum reads, downloads, ...) run on it unchanged:

    http = HttpContext.from_state_file(SESSION_FILE)
    sesskey = http.sesskey()            # None when the saved session has expired
    http.request.get(url, timeout=30000, max_redirects=0)
    http.request.post(url, data=json_text, headers={...})

Most of a check needs no browser, which is what makes it fast; the browser is
only started for signing in, screenshots and the occasional deep check.
Each thread gets its own requests.Session (copied from the main cookies) so a
check can read several courses at once.

The university server drops connections now and then (WinError 10054, TLS
handshake timeouts, a 502 from its proxy). Every request is retried RETRIES
times with exponential backoff before the error reaches the caller, so one
dropped connection doesn't fail the whole check.
"""

import copy
import json
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from uninotifier import fileio
from uninotifier.zuj_auth import BASE_URL

SESSKEY_RE = re.compile(r'"sesskey":"([a-zA-Z0-9]+)"')
RETRIES = 2                       # extra attempts after a dropped connection, a timeout or a 502-504
BACKOFF_SECONDS = 2.0             # before the first retry; doubles each time (2 s, 4 s)
RETRY_STATUS = {502, 503, 504}

log = logging.getLogger("uninotifier")


class HttpResponse:
    """The slice of Playwright's APIResponse the project uses."""

    def __init__(self, response: requests.Response):
        self._r = response
        self.status = response.status_code
        self.ok = 200 <= response.status_code < 300
        self.url = response.url
        self.headers = response.headers          # case-insensitive, like Playwright's

    def text(self) -> str:
        return self._r.text

    def json(self):
        return self._r.json()

    def body(self) -> bytes:
        return self._r.content


class HttpContext:
    def __init__(self, cookies: requests.cookies.RequestsCookieJar):
        self._cookies = cookies
        self._local = threading.local()
        self.request = self                       # context.request.get(...) works too

    # ------------------------------------------------------------ building

    @classmethod
    def from_cookie_list(cls, cookies: list[dict]) -> "HttpContext":
        """From Playwright's storage_state cookies."""
        jar = requests.cookies.RequestsCookieJar()
        for c in cookies:
            jar.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        return cls(jar)

    @classmethod
    def from_state_file(cls, path: Path) -> "HttpContext | None":
        try:
            return cls.from_cookie_list(json.loads(Path(path).read_text(encoding="utf-8")).get("cookies", []))
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------- session

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.cookies = copy.deepcopy(self._cookies)
            session.headers["User-Agent"] = "Mozilla/5.0 UniNotifier"
            self._local.session = session
        return session

    def _send(self, method: str, url: str, timeout: int, max_redirects: int | None = None,
              **kwargs) -> HttpResponse:
        follow = max_redirects != 0              # Playwright's max_redirects=0: don't follow
        for attempt in range(RETRIES + 1):
            try:
                resp = self._session().request(method, url, timeout=timeout / 1000,
                                               allow_redirects=follow, **kwargs)
                if resp.status_code not in RETRY_STATUS or attempt == RETRIES:
                    return HttpResponse(resp)
                reason = f"HTTP {resp.status_code}"
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == RETRIES:
                    raise
                reason = type(e).__name__
            wait = BACKOFF_SECONDS * 2 ** attempt
            # the path only: Moodle's query strings carry the session key
            log.warning("%s %s: %s — retry %d/%d in %g s", method, urlsplit(url).path, reason,
                        attempt + 1, RETRIES, wait)
            time.sleep(wait)

    def get(self, url: str, timeout: int = 30000, max_redirects: int | None = None) -> HttpResponse:
        return self._send("GET", url, timeout, max_redirects)

    def head(self, url: str, timeout: int = 30000, max_redirects: int | None = None) -> HttpResponse:
        return self._send("HEAD", url, timeout, max_redirects)

    def post(self, url: str, data: str | None = None, headers: dict | None = None,
             timeout: int = 30000) -> HttpResponse:
        return self._send("POST", url, timeout, data=data, headers=headers)

    # --------------------------------------------------------------- Moodle

    def sesskey(self) -> str | None:
        """The session's key, or None when the dashboard sends us to the login."""
        resp = self.get(f"{BASE_URL}/my/", timeout=30000)
        match = SESSKEY_RE.search(resp.text()) if resp.ok else None
        return match.group(1) if match and "/login/" not in resp.url else None

    def save_cookies(self, path: Path) -> None:
        """Write refreshed cookie values back into the saved browser state, so
        the file the bot and the browser share stays current. Other parts of
        the file (local storage, other sites' cookies) are left alone."""
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        fresh = {(c.name, c.domain.lstrip(".")): c.value for c in self._session().cookies}
        changed = False
        for c in state.get("cookies", []):
            value = fresh.get((c["name"], c.get("domain", "").lstrip(".")))
            if value is not None and value != c["value"]:
                c["value"], changed = value, True
        if changed:
            fileio.write_json_atomic(path, state)
