# UniNotifier — where the project stands, and what comes next

Read this first when you pick the project up after a break. It says what is finished, what is
waiting for something outside the code, the rules the next feature must follow, and how the project
is kept in order. The details of every feature are in [REFERENCE.md](REFERENCE.md); the history is in
[../CHANGELOG.md](../CHANGELOG.md).

## 1. State

Finished and in daily use: Moodle monitoring (new items, edits, deadlines, forum posts, grades),
the local archive, task list and reminders, lecture alerts and automatic attendance check-in, GPA and
"grade needed" tools, the portal look-ups (transcript, balance, timetable, finals), the guest
look-up, the Registration Plan (phase 1), self-healing, failure alerts and state backup.

The suite (`tests/test_architecture.py`, one file per topic in `tests/`) runs offline and must stay
green; CI runs it on Windows with Python 3.11, 3.13, 3.14 and 3.15 and runs the student installer.
Branch coverage is about 80% overall (core logic about 86%). What is not covered is mostly code that
needs a real browser or a live site: the portal's browser sign-in, Microsoft's form, `tools/`, and
the temporary Cambridge One module.

Decisions already taken — do not reopen them without the owner:
- License: PolyForm Noncommercial 1.0.0.
- No formatter or style linter is adopted (the first reformat would touch about 12,000 lines of
  history for no behaviour gain). `pyflakes` and the test suite are the gates.
- The bot reads Moodle in English, always, from its own session (`?lang=en`), whatever language the
  owner uses; it must never change the owner's account language.
- The bot speaks English; everything a student sees from the bot is English.
- Campus days are Sunday to Wednesday (`reminders.CAMPUS_DAYS`). Lectures on other days are ignored
  on purpose until it is confirmed that they exist (see section 2).

## 2. To confirm with the university (before relying on them)

The registration helper holds these as constants in `registration.py`. They came from the owner and
are **not yet checked against the university's regulations**. Check them in the student guide or with
the registrar, then correct the constants and the tests that name them.

| Question | What the code assumes |
|---|---|
| Regular term load | 12 to 18 hours (offered as 12, 15, 18) |
| 21 hours | Only in the graduation term, or with an excellent CGPA. The CGPA threshold is unknown; the bot says the condition in words and shows no number |
| Summer load | Offered as 3, 6, 9; 12 only in the student's last term. Is the minimum really 3? Does a high CGPA also allow 12? |
| Course conditions | Only the prerequisites read from the study plan. Is there a minimum mark or passed-hours condition for some courses, or any condition besides the department head's approval? |
| Training and graduation project | 90 hours **passed**, with the department head's approval. Is 90 right, and are registered hours counted? |
| Military science | Graduation term only |
| Days | Are there lectures outside Sunday to Wednesday (a Saturday lab, a Thursday in summer)? If yes, the days become a setting instead of a constant |

## 3. To build when term registration opens

Needs the portal's registration page **open**: it does not exist mid-term. Read the real page first
(save it with `tools/record_pages.py`, without personal data in the tests), then build; never from a
guess. Entry on the portal: the side menu, Registration → Electronic registration.

The flow the owner asked for:
1. Pick the number of hours.
2. Pick the attendance days (Sunday and Tuesday, Monday and Wednesday, or all four).
3. Pick the courses, marking the **must-have** ones (their timetable may change) and the
   **alternates** ("I give 8 and want 6: if X is not found, register one of these two").
4. The bot reads every section, proposes a clash-free set and marks the closed ones.
5. After the owner approves, the bot registers the sections itself. For closed sections it looks
   about every 5 minutes and takes a seat the moment one frees, with an alert each time.
6. A visible switch shows whether that watch is running, and a way to stop it.

Cases to handle:
- The portal refuses (unpaid fees, "see the finance department", registration period closed): send a
  message with a screenshot saying registration is not possible now, and register nothing.
- A wanted course has no section on the chosen days: say so and offer to change the days to its
  days, attend all four days, or use an alternate.
- Difficulty balancing has no reliable generic source; the manual choice covers it.

Hard rules (a mistake here costs a student a seat, and other students will use it):
- Register only a section of a course the owner approved by name.
- **Never** drop a course; a section the student already holds must never be lost.
- Fees are the student's to check; the bot does not decide on them.
- Nothing is registered without the owner's approval of that exact set.
- Training and graduation project, military science: never suggested early.

### 3b. A course the bot cannot check in for — said up front

Today a student learns that auto check-in cannot work for a course only after a lecture was missed
or when "Check In by Hand" arrives. The bot should say it early, once (again when the timetable
changes): one message with every online course and its state — ✅ auto check-in possible, or ⚠️ not
possible with the reason (the course has no attendance activity; the instructor marks attendance
and gives no self-marking; no online sessions by the timetable) — and a button to the course's
attendance page, which shows how many sessions the student attended. It is built from what the bot
already reads. Some cases cannot be known before an instructor opens a session (a password or QR
code); the message says so. This also covers a student whose setup differs from the owner's, at the
start of a term or in the middle of one.

## 4. Later: the look of the bot

The messages, buttons and menus work and are tested. A pass over their order, wording and
consistency is wanted later; it changes no behaviour, so it comes after registration.

## 5. Left on purpose (known, small)

Found in the last audit and judged not worth the risk of changing without a real case:
- Claim-file code exists three times (daily, lectures, reminders).
- Data file paths are declared in several modules.
- `failsafe.json` is read-modify-written by two processes without a file lock.
- A `/check` that times out can leave a Chromium child process running.
- The meeting re-read loop has no guard on how often it runs.
- Session dates in `checkin`/`activities` are parsed in English only — safe because of the English
  session above.

## 6. How the project is kept in order

- One concern per commit, with a short prefix (`feat:`, `fix:`, `docs:`, `test:`...). A fix comes
  with a test that fails without it. See [../CONTRIBUTING.md](../CONTRIBUTING.md).
- Nothing personal in the repository: `.env`, `schedule.json`, `data/`, `tests/recorded/` are never
  committed; tests use the made-up student ID `SID`. gitleaks runs in the pre-commit hook
  (`git config core.hooksPath .githooks`).
- Measure before assuming: reproduce a bug against the recorded real pages or a local fake before
  changing code, and say which level a change reached: tested by the suite, run for real, or seen by
  the owner in Telegram.
- After changing bot code, restart the scheduled task `UniNotifier_BotListener` and check it runs.
- Docs stay true in the same change: README.md and README.ar.md together, REFERENCE.md,
  CHANGELOG.md, and NEWS.md only for what users would notice.
- After a push, wait for CI and fix a red run at once.
