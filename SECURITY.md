# Security policy

UniNotifier holds a student's university password, a Slack app's tokens (or a Telegram bot token) and
live sign-in sessions. A flaw in how it handles them matters, so please report one **privately**.

## What is kept, where, and who sees it

Everything stays on **your own PC**, in your own Windows account. There is no UniNotifier server:
nothing you type or that the bot reads is sent to the developer or to anyone else.

| What | Where | Who can open it |
|---|---|---|
| Your passwords and the app's tokens (`xoxb-`, `xapp-`, or the Telegram bot token) | `.env` in the project folder | Only your Windows account (the setup limits the file to it); never in Git |
| Your sign-in sessions (Moodle, the portal) | `data\` | Only your Windows account (your user folder); never in Git, never in the backup |
| What the bot found: tasks, grades, your timetable and its picture | `data\` | The same; never in Git |
| The messages it sent you (marks, balance, screenshots) | your own Slack workspace (or Telegram chat) | You — like any message you receive there; Slack or Telegram stores it on their servers |
| The course files it downloads | `ARCHIVE_ROOT` (`Documents\University` by default) | Your Windows account |
| The optional backup | your own **private** GitHub repository | You. Only `saved_items.json`, `gpa.json` and `reminders.json`, added by name: never a session, a cookie or a password |

What leaves the PC, and to whom: the sign-ins go to the university's own sites (Moodle, Microsoft,
the portal, the exams site), and the messages to **your** Slack app (or Telegram bot), which answers only
you — `SLACK_USER` in Slack, your own Telegram Id in Telegram. What you type to the app is never written to
disk, and a friend's login for `/guest` is typed into a private Slack form: used once, in memory, never saved.
The optional health check (`HEALTHCHECK_URL`) receives only a short status — how many courses and
items a check read and how long it took, that the portal is under maintenance, or an exit code —
never a name, a mark or a message. Update Now downloads new code only from the address you cloned, refuses one
without encryption (`http://`, `git://`), and installs exactly the version it told you about — never a commit
pushed after that message.

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

- reveal a value from `.env` (in logs, Slack or Telegram messages, error alerts, the state backup or Git);
- let someone other than the owner (`SLACK_USER` / `ADMIN_USER_ID`) make the bot answer, act, or reveal data;
- make the bot change something on a university site that the student did not switch on;
- write files outside the archive folder (`ARCHIVE_ROOT`), e.g. through a crafted file name on Moodle;
- run code or commands from content read on Moodle, the student portal, Cambridge One, Slack or Telegram;
- push a session, cookie or password to a backup repository.

**Out of scope** — please report these to their owners instead:

- Moodle, the student portal, Microsoft sign-in, Slack, Telegram or Cambridge One themselves;
- a PC that is already compromised, or settings someone has shared on purpose.

## If you exposed your own secrets

Act at once — it takes two minutes:

- **Slack tokens** → [api.slack.com/apps](https://api.slack.com/apps) → your app: *OAuth & Permissions* →
  **Revoke** / reinstall for a new `xoxb-` token, and *Basic Information → App-Level Tokens* → revoke and
  generate a new `xapp-` one; then run `.venv\Scripts\python -m uninotifier.setup --slack`.
- **Telegram bot token** → in Telegram, @BotFather → `/revoke` → your bot; put the new token in `.env`.
- **University password** → change it on the university's Microsoft account page; update `.env`.
- **healthchecks.io URL** → delete the check and create a new one; update `.env`.

Then restart the bot (`scripts\setup_tasks.ps1` does it). Every value in `.env` is masked in
UniNotifier's own logs and alerts, so a log you shared from the program itself does not contain them.
