"""
Run this by hand whenever monitor.py reports that it needs a fresh login
(an "MFA required" alert on Telegram, or MFARequiredError in data/logs/monitor.log).

It opens a real, visible browser window and walks through the university's
Microsoft sign-in. If a second factor is requested, approve it yourself
(Authenticator push on your phone, or an SMS/Authenticator code) in that
window. Once you land on the dashboard, the session — including Microsoft's
"this device is trusted" cookie, typically good for ~30 days — is saved to
auth_state.json, which monitor.py's scheduled runs then reuse automatically
without prompting for MFA again until it expires.
"""

import sys

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import os

from uninotifier.zuj_auth import LoginError, install_virtual_authenticator, login_via_sso
from uninotifier import paths

load_dotenv(paths.ENV_FILE)
SESSION_FILE = paths.DATA / "auth_state.json"

USERNAME = os.getenv("ZUJ_USERNAME")
PASSWORD = os.getenv("ZUJ_PASSWORD")


def main() -> None:
    if not USERNAME or not PASSWORD:
        print("Missing ZUJ_USERNAME / ZUJ_PASSWORD in .env")
        sys.exit(1)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        install_virtual_authenticator(context, page)

        print("Opening the sign-in window...")
        print("If asked for a second factor, approve it on your phone or enter the code.")
        try:
            login_via_sso(page, USERNAME, PASSWORD, interactive=True, timeout_seconds=300)
        except LoginError as e:
            print(f"Login failed: {e}")
            browser.close()
            sys.exit(1)

        context.storage_state(path=str(SESSION_FILE))
        print(f"\nSession saved to {SESSION_FILE.name} — monitor.py will reuse it from now on.")
        browser.close()


if __name__ == "__main__":
    main()
