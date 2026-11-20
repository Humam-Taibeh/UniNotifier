"""
Tuition balance watch: the bot looks at the portal's financial record every so often and tells you when the
balance CHANGED — a payment, a discount, a new fee — with the screenshot, the old and the new amount.

The first look only remembers the balance (nothing to tell yet); a balance you read yourself (💰 Balance) is
remembered too, so you are never told about something you just saw. Nothing is sent when it did not change.
The portal is read at most every INTERVAL_SECONDS, one sign-in per look, and never while another portal
request is running. A look that needs a verification code is skipped (an unattended look cannot ask for one).

State: data/balance_watch.json — {"total": the last balance, "seen": when it was last read}.
"""

import json
import time

from uninotifier import fileio, paths

STATE_FILE = paths.DATA / "balance_watch.json"
INTERVAL_SECONDS = 6 * 60 * 60        # how often the portal is looked at (each look is a full sign-in: kept rare)
TOLERANCE = 0.005                     # amounts are money: a change is anything of half a fils or more


def load() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def due(now: float | None = None, state: dict | None = None) -> bool:
    state = load() if state is None else state
    return (time.time() if now is None else now) - float(state.get("checked") or 0) >= INTERVAL_SECONDS


def observe(total: float, now: float | None = None, checked: bool = True) -> float | None:
    """The balance as just read. Returns the balance known before it (None the first time), and remembers this one."""
    state = load()
    before = state.get("total")
    state["total"] = float(total)
    state["seen"] = time.time() if now is None else now
    if checked:
        state["checked"] = state["seen"]
    fileio.write_json_atomic(STATE_FILE, state)
    return before


def changed(before: float | None, now: float) -> bool:
    return before is not None and abs(now - before) >= TOLERANCE


def _money(value: float) -> str:
    """1700 -> '1,700'; 1234.5 -> '1,234.50'."""
    return f"{value:,.0f}" if abs(value - round(value)) < TOLERANCE else f"{value:,.2f}"


def _amount(value: float) -> str:
    return f"{_money(abs(value))} JD" + (" credit" if value < 0 else "")


def message(before: float, now: float) -> str:
    """The alert's words: what it was, what it is, and what that most likely means."""
    diff = now - before
    lines = ["💰 *Tuition Balance Changed*", "", f"*Before:* {_amount(before)}", f"*Now:* {_amount(now)}", ""]
    if diff < 0:
        lines.append(f"📉 *Down by {_money(abs(diff))} JD* — a payment was recorded, or a discount came in.")
    else:
        lines.append(f"📈 *Up by {_money(diff)} JD* — a new fee was added.")
    return "\n".join(lines)


def skipped(now: float | None = None) -> None:
    """A look that could not read the balance (portal down, a code asked): the next one is in INTERVAL_SECONDS, not at once."""
    state = load()
    state["checked"] = time.time() if now is None else now
    fileio.write_json_atomic(STATE_FILE, state)
