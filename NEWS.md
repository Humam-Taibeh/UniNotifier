# News

What is new, in plain words, for the people who use UniNotifier. The bot shows the entries a copy
does not have yet when it tells its owner that a new version is ready (newest first; one entry is
`## YYYY-MM-DD — Title`, then the text). The full list of changes is in [CHANGELOG.md](CHANGELOG.md).

## 2026-11-12 — Steadier checks
- You can use Moodle in Arabic or English: the bot always reads it in English, in its own session.
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
