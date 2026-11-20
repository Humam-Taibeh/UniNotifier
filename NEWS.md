# News

What is new, in plain words, for the people who use UniNotifier. The bot shows the entries a copy
does not have yet when it tells its owner that a new version is ready (newest first; one entry is
`## YYYY-MM-DD — Title`, then the text). The full list of changes is in [CHANGELOG.md](CHANGELOG.md).

## 2026-11-21 — A faster timetable, midterm marks, absences — and more privacy
- 📅 **The weekly timetable opens at once on Slack**: it is uploaded the first time only, then shown from
  Slack's own copy — about a third of a second instead of several seconds.
- 📝 **Midterm Watch** (`midterms`): each midterm mark the moment it appears on the exams site, with a picture.
- 🎒 **Absences** (`attendance`): after your last lecture the bot asks whether you went, and warns you before a
  course's absence limit. It also tells you when your balance changes and when registration opens.
- 🔒 **More private**: a friend's login for `guest` is typed into a private Slack form — never a chat message, never
  saved. What you type to the bot is no longer kept on your PC.
- ✅ **Your choice**: ticking *Mark as done* on Moodle by itself is now off unless you set `AUTO_MARK_DONE=true` in
  `.env` — your instructor sees those ticks. Automatic check-in marks you Present whether or not you attend: switch
  it off for a lecture you will miss. Read [DISCLAIMER.md](DISCLAIMER.md).
- The portal is read less often by itself (your balance every 6 hours, registration every 2 hours).

## 2026-11-16 — Your messages on Slack, when Telegram is blocked
- Telegram is blocked on some networks in Jordan. Your messages can now come on Slack instead —
  free, on your phone, the moment anything changes: run `.venv\Scripts\python -m uninotifier.setup --slack`
  and it walks you through it in five minutes.
- The whole bot comes with it: open the app's **Home** page for every menu (it changes in place as
  you tap), real buttons, and each alert as a neat card. Type `menu`, `tasks` or `check` to it.
- The doctor now says plainly when your network blocks Telegram, instead of "check the internet".

## 2026-11-15 — Your timetable, read and drawn for you
- With `PORTAL_USERNAME` and `PORTAL_PASSWORD` in `.env`, the bot reads your weekly timetable from the
  portal by itself — no `schedule.json` to write. One you wrote yourself still wins.
- 📅 Weekly Timetable now sends a timetable the bot draws (a picture and a PDF) when you keep none of
  your own in `assets\`. A Saturday lab is in it too.
- Your cumulative average is checked against the portal's own figure every time the transcript is
  read; if the two ever disagree, the portal's figure is shown and the message says so.
- Installing is one double-click and five short answers: `install.bat` asks for your settings and
  writes them itself — no file to edit. `AUTO_CHECK_IN=true` in `.env` turns automatic check-in on.
- With automatic check-in on, one message tells you up front which online courses it can check you in
  to, and why not for the others — before a lecture is missed.

## 2026-11-15 — A new quiz comes with its times
- A quiz found the minute it is published now comes with its opening and closing times, read from the
  quiz's own page — before, it could show "No due date set" until the next check.
- An Arabic quiz no longer carries the calendar's word "يُفتح" after its name.

## 2026-11-13 — Reminders you can change
- The buttons are always at the bottom of the chat, also after 🔗 View Original Post, with 🔙 Main Menu under it.
- Reminders can be changed: open one in Manage Reminders to edit its text, date or time — deleting asks first.
- You can type any date for a reminder (like `26/11`); the buttons showed only the next week.
- Your reminders are listed in full, with everything you saved in them — not just the first line.
- You can use Moodle in Arabic or English: the bot always reads it in English, in its own session.

## 2026-11-12 — Steadier checks
- A task found less than an hour before its time gets one reminder, saying the minutes really left.
- A file whose message failed now arrives with the file the next time, not as "already sent".
- A power cut while the Moodle sign-in was being saved no longer stops the checks until you fix it.
- Cambridge One stops by itself when your English class no longer opens there (the course is over),
  and tells you once — no need to remove anything from `.env`.

## 2026-11-12 — Assignments stay until you hand them in
- An assignment leaves your task list only when it is submitted — not when Moodle marks it done because
  you opened it.
- You are told when an assignment you have not submitted is closed or removed by the instructor.

## 2026-11-11 — Updates from the chat, and the Registration Plan
- The bot now tells you when a new version is ready, with what is new, and ⬇️ Update Now installs it.
- 🗓️ Registration Plan (under 📜 Academic Record & Balance): what to register next term, from your own
  study plan and its prerequisites — a suggestion for the hours you choose, or your own choice.
- Your marks are found in PDF files too, and a course's coursework total (42/60) shows the best mark it
  can still reach.
- Messages are shorter: a picture of what the instructor wrote, and what changed when it is edited.
- An assignment with an old due date that Moodle still accepts now stays in your task list until you
  hand it in.
- The project's license is now noncommercial: free to use and share, not to sell.
