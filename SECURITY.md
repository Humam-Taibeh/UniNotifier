# Security policy

UniNotifier holds a student's university password, a Telegram bot token and live sign-in
sessions. A flaw in how it handles them matters, so please report one **privately**.

## Reporting a vulnerability

**Don't open a public issue.** Use GitHub's private reporting instead:

1. Open the repository's **Security** tab → **Report a vulnerability**
   (or go straight to <https://github.com/Humam-Taibeh/UniNotifier/security/advisories/new>).
2. Describe what you found, how to reproduce it, and what an attacker could do with it.
3. Never include real credentials — yours or anyone else's. Made-up values show the problem just as well.

You'll get a reply within **7 days** (this is a one-maintainer project, so it's best effort). Once the
issue is confirmed, it is fixed on `main`, a release follows, and you're credited in the advisory if
you'd like to be.

## Supported versions

| Version | Supported |
|---|---|
| Latest release and `main` | ✅ |
| Anything older | ❌ — update with `git pull` (see the README) |

## What counts

**In scope** — anything in this repository that could:

- reveal a value from `.env` (in logs, Telegram messages, error alerts, the state backup or Git);
- let someone other than `ADMIN_USER_ID` make the bot answer, act, or reveal data;
- write files outside the archive folder (`ARCHIVE_ROOT`), e.g. through a crafted file name on Moodle;
- run code or commands from content read on Moodle, the student portal, Cambridge One or Telegram;
- push a session, cookie or password to a backup repository.

**Out of scope** — please report these to their owners instead:

- Moodle, the student portal, Microsoft sign-in, Telegram or Cambridge One themselves;
- a PC that is already compromised, or settings someone has shared on purpose.

## If you exposed your own secrets

Act at once — it takes two minutes:

- **Bot token** → in Telegram, @BotFather → `/revoke` → your bot; put the new token in `.env`.
- **University password** → change it on the university's Microsoft account page; update `.env`.
- **healthchecks.io URL** → delete the check and create a new one; update `.env`.

Then restart the bot (`scripts\setup_tasks.ps1` does it). Every value in `.env` is masked in
UniNotifier's own logs and alerts, so a log you shared from the program itself does not contain them.
