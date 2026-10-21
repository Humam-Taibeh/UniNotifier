"""
Microsoft second-factor sign-in, completed from Telegram.

When the saved session expires, Microsoft shows a verification page
(PageID "ConvergedTFA"). Instead of failing, the monitor hands that page to
TelegramMFA, which reacts to whatever sub-screen is showing:

  number      Authenticator push with number matching -> sends the number
              (and a screenshot of the sign-in box) to Telegram; the user
              types it into Authenticator and the page moves on by itself
  push        a plain "approve the request" push -> same, without a number
  code        an SMS / app code is expected -> asks for it on Telegram and
              types the reply in
  proofs      the "verify your identity" method picker -> picks SMS when the
              user asked for it, otherwise the Authenticator app
  timeout     "we didn't hear from you" -> sends one fresh request
  denied      the user denied the request -> stops

The monitor never reads Telegram itself (bot.py owns getUpdates). The two
processes meet in two small files: the monitor writes MFA_REQUEST while it
waits; bot.py writes the user's code / choice to MFA_RESPONSE.
"""

import json
import logging
import re
import time
import uuid
from pathlib import Path

from uninotifier import fileio
from uninotifier import notifier
from uninotifier.notifier import escape_md
from uninotifier.zuj_auth import MFARequiredError
from uninotifier import paths

log = logging.getLogger("uninotifier")

MFA_REQUEST = paths.DATA / "mfa_request.json"
MFA_RESPONSE = paths.DATA / "mfa_response.json"
DEBUG_DIR = paths.DATA / "mfa_debug"

WAIT_SECONDS = 5 * 60          # how long one sign-in attempt waits for the user
UNKNOWN_SCREEN_SECONDS = 10    # an unrecognised screen this long is reported
REQUEST_TTL = 10 * 60          # bot.py ignores a request file older than this

SMS_CALLBACK = "mfa:sms"
SMS_BUTTON = "📱 Text me a code instead"
RETRY_BUTTON = "🔄 Try again now"

# Microsoft's sign-in markup; several selectors each, as ids differ between
# page versions.
NUMBER = "#idRichContext_DisplaySign, .displaySign"
PUSH = "#idDiv_SAOTCAS_Title, #idDiv_SAOTCAS_Description"
CODE_INPUT = "#idTxtBx_SAOTCC_OTC, input[name='otc'], input[autocomplete='one-time-code']"
CODE_SUBMIT = "#idSubmit_SAOTCC_Continue"
CODE_ERROR = "#idSpan_SAOTCC_Error_OTC"
CODE_DESCRIPTION = "#idDiv_SAOTCC_Description"
PROOFS = "#idDiv_SAOTCS_Proofs [data-value], div[role='button'][data-value]"
SMS_PROOF = "[data-value='OneWaySMS']"
APP_PROOFS = ("[data-value='PhoneAppNotification']", "[data-value='PhoneAppOTP']")
TIMEOUT = "#idDiv_SAASTO_Title"
RESEND = "#idA_SAASTO_Resend"
DENIED = "#idDiv_SAASDS_Title"
REMEMBER = "#idChkBx_SAOTCAS_TD, #idChkBx_SAOTCC_TD, input[name='rememberMFA']"
ANOTHER_WAY = "#signInAnotherWay"
ANOTHER_WAY_TEXT = re.compile(r"can.t use my Microsoft Authenticator|sign in another way|"
                              r"different verification option|other ways to sign in", re.I)
SIGN_IN_BOX = "#lightbox, .sign-in-box, [data-viewid]"

KEYCAPS = {str(d): f"{d}️⃣" for d in range(10)}


def keycaps(number: str) -> str:
    """'47' -> 4️⃣7️⃣ — reads like the number on the sign-in screen."""
    return "".join(KEYCAPS.get(ch, ch) for ch in number)


SMS_ROWS = [[{"text": SMS_BUTTON, "callback_data": SMS_CALLBACK}]]


def approval_text(number: str | None, remaining: str) -> str:
    """The Authenticator request: the number to type in, or just "Approve"."""
    if number:
        lines = ["🔐 *Microsoft Sign-in Approval*", "",
                 "Your portal session expired. Open *Microsoft Authenticator* on your phone "
                 "and enter this number:", "", f"{keycaps(number)}   (*{number}*)"]
    else:
        lines = ["🔐 *Microsoft Sign-in Approval*", "",
                 "Your portal session expired. Open *Microsoft Authenticator* on your phone "
                 "and tap *Approve*."]
    lines += ["", f"⏱️ Expires in {remaining}. Checks continue by themselves once approved."]
    return "\n".join(lines)


def code_text(where: str, remaining: str) -> str:
    """The request for an SMS / app code; `where` is Microsoft's own wording."""
    return "\n".join([
        "📱 *Verification Code Needed*", "",
        escape_md(where) if where else "Microsoft sent you a verification code.",
        "", "Reply to this chat with the code (digits only).",
        f"⏱️ Expires in {remaining}."])


def _visible(page, selector: str) -> bool:
    loc = page.locator(selector)
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def detect(page) -> str:
    """Which second-factor sub-screen is showing."""
    if _visible(page, DENIED):
        return "denied"
    if _visible(page, TIMEOUT):
        return "timeout"
    if _visible(page, CODE_INPUT):
        return "code"
    if _visible(page, NUMBER):
        return "number"
    if _visible(page, PROOFS):
        return "proofs"
    if _visible(page, PUSH):
        return "push"
    return "other"


def read_number(page) -> str | None:
    try:
        text = page.locator(NUMBER).first.inner_text().strip()
    except Exception:
        return None
    return text if re.fullmatch(r"\d{1,3}", text) else None


def _write_json(path: Path, data: dict) -> None:
    fileio.write_json_atomic(path, data)


def read_request() -> dict | None:
    """The monitor's pending request, if fresh (used by bot.py)."""
    try:
        req = json.loads(MFA_REQUEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return req if time.time() - req.get("created", 0) < REQUEST_TTL else None


def answer_request(**response) -> bool:
    """bot.py: hand the user's code / choice to the waiting monitor."""
    req = read_request()
    if not req:
        return False
    _write_json(MFA_RESPONSE, {"id": req["id"], **response})
    return True


class TelegramMFA:
    def __init__(self, token: str, chat_id: str, wait_seconds: int = WAIT_SECONDS):
        self.token, self.chat_id = token, chat_id
        self.request_id = uuid.uuid4().hex
        self.started: float | None = None
        self.deadline = 0.0
        self.message_id: int | None = None   # the photo/message that carries the buttons
        self.shown: tuple | None = None       # (screen, number) last reported
        self.want_sms = False
        self.awaiting_code = False
        self.submitted_code: str | None = None
        self.resent = False
        self.unknown_since: float | None = None
        self.wait_seconds = wait_seconds

    # ----------------------------------------------------------- telegram

    def _send_screen(self, page, caption: str, rows: list) -> None:
        """A screenshot of the sign-in box with the instructions as caption;
        plain text when the screenshot fails."""
        self._clear_buttons()
        png = None
        try:
            box = page.locator(SIGN_IN_BOX).first
            png = box.screenshot() if box.count() and box.is_visible() else page.screenshot()
        except Exception:
            log.exception("Could not screenshot the sign-in page")
        markup = json.dumps({"inline_keyboard": rows})
        result = None
        if png:
            result = notifier.upload(self.token, "sendPhoto",
                                     {"chat_id": self.chat_id, "caption": caption,
                                      "parse_mode": "Markdown", "reply_markup": markup},
                                     "photo", "sign-in.png", png)
        if result is None:
            result = notifier.call_api(self.token, "sendMessage", {
                "chat_id": self.chat_id, "text": caption, "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": rows}})
        self.message_id = result.get("message_id") if isinstance(result, dict) else None

    def _say(self, text: str, rows: list | None = None) -> None:
        notifier.send_message(self.token, self.chat_id, text, rows=rows)

    def _clear_buttons(self) -> None:
        """Remove the buttons of the previous instruction message: they no
        longer apply once the screen has moved on."""
        if self.message_id:
            notifier.call_api(self.token, "editMessageReplyMarkup", {
                "chat_id": self.chat_id, "message_id": self.message_id,
                "reply_markup": {"inline_keyboard": []}})
            self.message_id = None

    def _remaining(self) -> str:
        return f"{max(0, int(self.deadline - time.monotonic())) // 60 + 1} min"

    # ------------------------------------------------------- coordination

    def _expect(self, expects: str) -> None:
        _write_json(MFA_REQUEST, {"id": self.request_id, "created": time.time(), "expects": expects})

    def _take_response(self) -> dict | None:
        try:
            resp = json.loads(MFA_RESPONSE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        MFA_RESPONSE.unlink(missing_ok=True)
        return resp if resp.get("id") == self.request_id else None

    def close(self) -> None:
        MFA_REQUEST.unlink(missing_ok=True)
        MFA_RESPONSE.unlink(missing_ok=True)

    # -------------------------------------------------------------- flow

    def step(self, page) -> None:
        """Called repeatedly by the sign-in loop while Microsoft shows its
        verification page. Raises MFARequiredError when the attempt ends
        without success (denied, expired)."""
        now = time.monotonic()
        if self.started is None:
            self.started, self.deadline = time.time(), now + self.wait_seconds
            log.info("Microsoft verification requested — handing it to Telegram")
        if now > self.deadline:
            self._fail("⌛ *Sign-in request expired*\n\nNo answer within "
                       f"{self.wait_seconds // 60} minutes. I'll try again automatically later, "
                       "or tap below to try now.", "verification timed out")

        response = self._take_response()
        if response and response.get("choice") == "sms":
            self.want_sms = True
            self._say("📱 *Switching to SMS…* a code will be texted to your phone.")
        screen = detect(page)
        if screen != "other":
            self.unknown_since = None

        if self.want_sms and screen in ("number", "push"):
            self._open_other_methods(page)
        elif screen == "proofs":
            self._choose_method(page)
        elif screen in ("number", "push"):
            self._report_push(page, screen)
        elif screen == "code":
            self._handle_code(page, response)
        elif screen == "timeout":
            if self.resent or not _visible(page, RESEND):
                self._fail("⌛ *No response to the Authenticator request.*\n\n"
                           "I'll try again automatically later, or tap below to try now.",
                           "push request timed out")
            page.locator(RESEND).first.click()
            self.resent, self.shown = True, None
            self._say("🔁 *No response yet — sent a fresh request to Authenticator.*")
        elif screen == "denied":
            self._fail("🚫 *Sign-in request denied* in Authenticator.\n\n"
                       "If that wasn't you, change your university password.", "request denied")
        else:
            self._report_unknown(page, now)

    def _report_push(self, page, screen: str) -> None:
        number = read_number(page) if screen == "number" else None
        if (screen, number) == self.shown:
            return
        self.shown = (screen, number)
        self._check_remember(page)
        self._expect("choice")
        self._send_screen(page, approval_text(number, self._remaining()), SMS_ROWS)
        log.info("Sent the sign-in %s to Telegram", "number" if number else "approval request")

    def _handle_code(self, page, response: dict | None) -> None:
        code = (response or {}).get("code")
        if code:
            self._check_remember(page)
            page.locator(CODE_INPUT).first.fill(code)
            submit = page.locator(CODE_SUBMIT)
            if submit.count() and submit.first.is_visible():
                submit.first.click()
            else:
                page.locator(CODE_INPUT).first.press("Enter")
            self.submitted_code = code
            self._say("🔑 *Code received — verifying…*")
            log.info("Submitted the verification code from Telegram")
            return
        rejected = self.submitted_code and _visible(page, CODE_ERROR)
        if self.awaiting_code and not rejected:
            return
        if rejected:
            self.submitted_code = None
            self._say("❌ *Microsoft rejected that code.* Check the latest SMS and send it again.")
        else:
            where = ""
            if _visible(page, CODE_DESCRIPTION):
                where = page.locator(CODE_DESCRIPTION).first.inner_text().strip()
            self._clear_buttons()
            self._say(code_text(where, self._remaining()))
        self.awaiting_code = True
        self._expect("code")

    def _open_other_methods(self, page) -> None:
        link = page.locator(ANOTHER_WAY)
        if not (link.count() and link.first.is_visible()):
            link = page.get_by_text(ANOTHER_WAY_TEXT)
        if link.count() and link.first.is_visible():
            link.first.click()
        else:
            self.want_sms = False
            self._say("⚠️ Microsoft didn't offer another method on this screen — "
                      "please use the Authenticator number above.")

    def _choose_method(self, page) -> None:
        options = (SMS_PROOF, *APP_PROOFS) if self.want_sms else (*APP_PROOFS, SMS_PROOF)
        for selector in options:
            if _visible(page, selector):
                page.locator(selector).first.click()
                log.info("Chose verification method %s", selector)
                return
        self._report_unknown(page, time.monotonic())

    def _check_remember(self, page) -> None:
        """Tick "Don't ask again for N days" when offered: fewer prompts."""
        try:
            box = page.locator(REMEMBER)
            if box.count() and box.first.is_visible() and not box.first.is_checked():
                box.first.check()
        except Exception:
            pass  # cosmetic: never let it break the sign-in

    def _report_unknown(self, page, now: float) -> None:
        """A verification screen this module doesn't recognise: keep its
        HTML for diagnosis and show the user what's on it (it may still
        contain a number to type)."""
        if self.unknown_since is None:
            self.unknown_since = now
            return
        if now - self.unknown_since < UNKNOWN_SCREEN_SECONDS or self.shown == ("other", None):
            return
        self.shown = ("other", None)
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            (DEBUG_DIR / f"{stamp}.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(DEBUG_DIR / f"{stamp}.png"), full_page=True)
        except Exception:
            log.exception("Could not save the unrecognised sign-in page")
        log.warning("Unrecognised Microsoft verification screen (saved to %s)", DEBUG_DIR.name)
        self._expect("choice")
        self._send_screen(page, "🔐 *Microsoft Sign-in Approval*\n\nMicrosoft is asking for "
                          "verification on an unfamiliar screen — here it is. If it shows a "
                          "number, enter it in Authenticator.",
                          [[{"text": SMS_BUTTON, "callback_data": SMS_CALLBACK}]])

    # ----------------------------------------------------------- outcome

    def _fail(self, text: str, reason: str):
        self._clear_buttons()
        self._say(text, [[{"text": RETRY_BUTTON, "callback_data": "check"}]])
        self.close()
        err = MFARequiredError(f"Microsoft verification not completed: {reason}")
        err.notified = True
        raise err

    def succeeded(self) -> None:
        """The portal was reached after verification."""
        if self.started is None:
            return
        self._clear_buttons()
        self._say("✅ *Signed in* — the session is renewed and checks continue automatically.")
        log.info("Microsoft verification completed from Telegram")
        self.close()
