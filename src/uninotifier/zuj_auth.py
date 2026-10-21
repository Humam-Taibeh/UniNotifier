"""
Shared Microsoft Entra ID (OIDC) sign-in flow for the ZUJ Moodle portal.

Authentication is not a plain form post: Microsoft may interpose a
passkey/FIDO nudge screen, a "choose a way to sign in" picker, and — for
this tenant — a genuine second-factor challenge (Authenticator app push or
SMS code). This module drives that flow defensively, reacting to whatever
is currently on screen rather than assuming a fixed sequence, and tells the
two failure modes apart:

- LoginError: an ordinary failure (bad credentials, unrecognized page).
- MFARequiredError: Microsoft is demanding a second factor that no headless
  script can complete. The caller should fall back to a cached session or
  prompt for an interactive login (see tools/login_refresh.py).
"""

import time
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PWTimeoutError

BASE_URL = "https://elearning.zuj.edu.jo"
OIDC_LOGIN_URL = f"{BASE_URL}/auth/oidc/?source=loginpage"
DASHBOARD_URL = f"{BASE_URL}/my/"


class LoginError(Exception):
    pass


class InvalidCredentialsError(LoginError):
    """Microsoft rejected the username or password — the one sign-in failure
    a screenshot can't explain better than its own message."""


class MFARequiredError(LoginError):
    # True when the user was already told on Telegram (by mfa.TelegramMFA),
    # so the monitor must not send a second alert about it.
    notified = False


def install_virtual_authenticator(context, page) -> None:
    """Register a CDP virtual WebAuthn authenticator.

    Without this, a passkey/FIDO step in the sign-in flow is handed to the
    real OS authenticator — on Windows that's Windows Hello, which pops up
    a native PIN/biometric dialog even during a background/headless run,
    since that prompt lives outside the browser window. No credential is
    ever registered on the virtual authenticator, so any WebAuthn call
    against it fails fast instead of waiting on, or popping up, real
    hardware/OS UI.
    """
    cdp = context.new_cdp_session(page)
    cdp.send("WebAuthn.enable", {"enableUI": False})
    cdp.send("WebAuthn.addVirtualAuthenticator", {
        "options": {
            "protocol": "ctap2",
            "transport": "internal",
            "hasResidentKey": True,
            "hasUserVerification": True,
            "isUserVerified": True,
            "automaticPresenceSimulation": True,
        }
    })


def is_authenticated(page) -> bool:
    page.goto(DASHBOARD_URL, wait_until="networkidle")
    return urlparse(page.url).path.startswith("/my/")


def _is_mfa_challenge(page) -> bool:
    """Detect Microsoft's dedicated second-factor page (PageID=ConvergedTFA),
    as opposed to the ordinary email/password/passkey-nudge screens."""
    meta = page.locator('meta[name="PageID"]')
    return meta.count() > 0 and meta.first.get_attribute("content") == "ConvergedTFA"


def login_via_sso(
    page,
    username: str,
    password: str,
    *,
    interactive: bool = False,
    timeout_seconds: int = 45,
    mfa_handler=None,
) -> None:
    """Drive the Microsoft sign-in flow to completion.

    mfa_handler (used by the unattended monitor): an object whose step(page)
    is called while the second-factor page is showing — mfa.TelegramMFA
    relays it to Telegram. It raises MFARequiredError when it gives up.

    interactive=False and no handler: bails out immediately with
    MFARequiredError if a second factor is demanded — there's no one there
    to approve it.

    interactive=True (used by tools/login_refresh.py, run by hand): a real person
    is watching the browser window, so instead of bailing it just keeps
    waiting while the MFA page is shown, giving them time to approve the
    push notification or type in a code.
    """
    page.goto(OIDC_LOGIN_URL, wait_until="networkidle")
    page.fill('input[type="email"]', username)
    page.click('input[type="submit"]')

    password_filled = False
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeoutError:
            pass  # the verification page polls for the approval: never fully idle
        page.wait_for_timeout(800)

        if urlparse(page.url).hostname == "elearning.zuj.edu.jo":
            if mfa_handler is not None:
                mfa_handler.succeeded()
            return

        if _is_mfa_challenge(page):
            if mfa_handler is not None:
                mfa_handler.step(page)  # raises MFARequiredError when it gives up
                deadline = max(deadline, time.monotonic() + 10)
                page.wait_for_timeout(1500)
                continue
            if not interactive:
                raise MFARequiredError(
                    "Microsoft is asking for a second sign-in factor (Authenticator app "
                    "or SMS code) that can't be completed automatically. Run "
                    "python -m uninotifier.tools.login_refresh once, interactively, to renew the trusted session."
                )
            # A person is watching — give them real time to respond.
            deadline = max(deadline, time.monotonic() + 5)
            page.wait_for_timeout(1500)
            continue

        pw_input = page.locator('input[type="password"]')
        if pw_input.count() and pw_input.first.is_visible():
            if password_filled:
                error_el = page.locator("#usernameError, #passwordError, #errorText")
                message = error_el.first.text_content() if error_el.count() else None
                raise InvalidCredentialsError((message or "Invalid credentials").strip())
            pw_input.first.fill(password)
            page.click('input[type="submit"]')
            password_filled = True
            continue

        use_password = page.get_by_text("Use my password", exact=False)
        if use_password.count() and use_password.first.is_visible():
            use_password.first.click()
            continue

        primary_button = page.locator("#idSIButton9")
        if primary_button.count() and primary_button.first.is_visible() and primary_button.first.is_enabled():
            primary_button.first.click()
            continue

        # Nothing actionable yet (e.g. the FIDO bridge resolving itself) — keep polling.

    raise LoginError("Sign-in flow did not reach the portal within the expected time")
