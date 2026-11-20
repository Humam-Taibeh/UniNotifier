# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Slack is the way in**: the setup asks where the messages go (Enter = Slack: it works on every network) and a
  Slack-only install needs no Telegram at all. The README (English and Arabic) is written around Slack.
- **Midterm Watch** (`/midterms`): with the portal login, the exams site (`exams.zuj.edu.jo`, the same sign-in) is
  read every 15 minutes and each midterm mark is told as it appears or changes — with a picture, out of 100 and the
  best mark the course can still reach; marks can be typed by hand; a summary screen; on and off.
- **Absences, counted** (`/attendance`): after the day's last lecture the bot asks whether you went (again at 10 PM),
  counts per course (5 allowed; blended and once-a-week courses 2; limits settable per course) and warns before a
  course's limit; earlier absences can be entered.
- **Balance and registration watches**: told when the tuition balance changes, and once when course registration
  opens on the portal; a Register button reads the registration page.
- A pinned announcement keeps its whole wording and its picture; 🖼️ Show Original brings it back.
- `tools.resend_post` sends a forum discussion's notification again.
- `AUTO_MARK_DONE` in `.env`: ticking *Mark as done* on Moodle by itself for saved files is now a choice.
- `DISCLAIMER.md` (and a Disclaimer section in both READMEs): unofficial; it assists and never decides; your
  attendance is your instructor's; your account, your responsibility.
- A `commit-msg` hook (`core.hooksPath .githooks`): a commit names only the person who makes it.
- Messages on Slack instead of Telegram (`NOTIFY_VIA=slack`), for a network that blocks Telegram:
  `python -m uninotifier.setup --slack` makes the Slack app with you (its settings in
  `docs/slack-app-manifest.json`, copied for pasting), checks its token, finds you and greets you there.
  Every message, file and album then goes to Slack in Slack's format, with retries as before; the
  Microsoft sign-in picture too.
- The whole bot on Slack (`slackbot.py`), with the app's live connection (Socket Mode: `SLACK_APP_TOKEN`,
  `SLACK_USER`; the setup asks for both): its screens on the app's Home page, redrawn in place at every
  press; real buttons, side by side, the way back apart at the bottom; commands typed without the "/".
  A notification that fits is Slack's card (title, course, details, time, up to three buttons); anything
  longer, and every screen, sits in a box (Slack's container) — nothing folds under "Show more". A Slack
  that refuses a layout gets the next one down (box, coloured frame, plain text): no message is lost to
  its look. Files the bot sends land above the menu that follows them.
- **No `schedule.json` is needed any more.** With the portal login in `.env`, the bot reads the weekly
  timetable from the registration portal for every student (not only with automatic check-in on) and
  builds its own schedule from it and Moodle's course list, joined by the course code
  (`autoschedule.py`, `data/schedule.auto.json`): the course names, sections, rooms, modes, the
  semester and the credit hours. A `schedule.json` written by hand still wins.
- **The weekly timetable is drawn by the bot** (`timetable_image.py`): a picture and a one-page PDF
  made from the schedule in force — a column per day, a row per time, a card per class, breaks, the
  courses and credit hours in the header. 📅 Weekly Timetable sends them when `assets\` holds no
  timetable of your own, and the *Timetable read from the portal* message carries the picture. A
  `logo.png` of your own in `assets\` appears in its header; the project ships no logo.
- **A setup that asks instead of a file to edit** (`python -m uninotifier.setup`, run by `install.bat`):
  five short questions in the console — the university e-mail and password, the bot's token (checked
  with Telegram at once), who you are on Telegram (found from the Start you press in your bot: no Id to
  look up), the optional portal login and automatic check-in. It writes `.env` itself, keeps every
  explanation and everything an existing `.env` holds, proves that every value reads back exactly, and
  limits the file to your Windows account. The installer no longer opens Notepad or writes a
  `schedule.json`, and after the doctor it offers to finish: the Moodle sign-in, the first check and
  the scheduled tasks.
- 🤖 Auto Check-In — Your Online Courses: one message, once the attendance pages are read, with every
  online course and whether the check-in can work for it (✅, or ⚠️/⏳ with the reason: no attendance
  activity, no online session, no session listed yet) and a button to each attendance page. Sent
  again only when a course's state changes.
- `python -m uninotifier.setup --demo`: the setup tried with made-up answers — nothing contacted,
  nothing written; it shows what `.env` would hold.
- `AUTO_CHECK_IN=true` in `.env` switches automatic attendance check-in on — the place for it when
  there is no `schedule.json`. A `schedule.json` that sets `auto_check_in` is still obeyed.
- The portal's timetable keeps every day it lists: a Saturday lab is shown in the timetable (the
  check-in still counts Sunday to Wednesday only).
- Manage Reminders: a reminder opens to its full text with Edit Text, Change Date, Change Time and Delete
  (Delete asks first). The day picker has ✏️ Type a Date, because the buttons reach only the coming week.
- 🔒 Assignment Closed — Not Submitted and 🗑️ Assignment Removed: told once when an assignment you
  have not handed in stops accepting work, or is taken out of the course.
- 🎓 Class Ended — Cambridge One: when the class has not opened for two days while the account still
  signs in (or Cambridge answers that there is no such class), the watch stops by itself and says so
  once — no more sign-ins or *sign in again* alerts after the course is over. `--login` turns it back on.

### Changed
- **The weekly timetable on Slack is uploaded once, then shown from Slack's own copy**: one message — the picture
  and a link to the PDF — with no upload and no waiting (measured on a real workspace: 9.5 s → 0.3 s a press). A new
  timetable is uploaded again and the old files go (`slackfiles.py`, `data/slack_files.json`).
- One connection pool to Slack, kept open and shared by every thread (it was one per thread, so each timetable
  press paid fresh TLS handshakes).
- The unattended portal looks are rarer: the balance every 6 hours (was hourly), registration every 2 hours (was
  30 minutes) — each look is a full sign-in.
- Automatic *Mark as done* is off unless `AUTO_MARK_DONE=true` (an existing install that wants it sets it).
- Slack screens follow one layout: title, line, the whole content, line, controls, line, Back + Main Menu; a screen
  you leave is removed; a screen too big for one box continues in the next.
- Finals Watch looks like Midterm Watch; notifications no longer carry a Detected time or Status line.
- The cumulative average the bot computes is checked against the portal on every record: it is shown,
  and feeds Grade Needed, only when the same rule gives the newest finished semester the figure the
  portal itself prints. A record it does not reproduce gets the portal's own figure and a ⚠️ line, and
  the Grade Needed base is left alone — never a figure that is wrong for that student.
- A study-plan course whose status is a word the bot does not know is shown with the portal's own word
  (❔) and is never offered in the Registration Plan; it used to be listed as a course still to take.
- An old transcript row without a result (withdrawn, never graded) is no longer counted as *in progress*.
- The test suite is one file per topic instead of a single 9,000-line file; it runs the same way.

### Security
- On Slack a guest's login (`/guest`) is typed into a private Slack form — never a chat message (Slack does not let
  an app delete one), never on disk, in memory only for the sign-in. What you type to the app is no longer written to
  `data/slack_messages.json`.
- The data backup adds its three files **by name**: an edited or deleted `data/.gitignore` can no longer let the
  Microsoft sign-in cookies, the portal session or the Slack message map reach GitHub (anything else staged is taken
  out and you are told; the `.gitignore` is put back).
- ⬇️ Update Now installs exactly the version it was offered for — never a commit pushed after the message — and a
  refused update puts the old packages back too.
- The portal's menu fallback never clicks a link whose words say it does something (confirm, cancel, delete, add,
  drop, pay …): the bot only opens pages.
- The Cambridge One sign-in recorder hides logins, passwords and tokens in what it saves.
- A link address taken from a Moodle page can no longer end a Slack link early, relabel it or add a `<!here>`.
- ⬇️ Update Now refuses to install code from an address without encryption (`http://`, `git://`):
  anyone on the same network could have changed it on its way. https, ssh and a folder on the PC are
  accepted, so a student who keeps a copy of their own can still update from it.
- The setup limits `.env` to your Windows account in two steps, by the account's exact security Id
  (not its name): your access is given first, and only then is everyone else's taken away, so the
  file can never end up open to nobody.
- `SECURITY.md` says what is kept, where, and who can see it.

### Fixed
- A Slack-only install now starts: with no Telegram id the Slack owner gets the owner's number; a bot that cannot
  tell who you are says so before it stops (it used to exit silently).
- Slack removes older copies only of the answers that are one living copy (portal answers, the timetable) — never
  last week's attendance proof or another course's file with the same name; the message file is no longer locked
  while Slack is asked (another process could take the lock and undo a save); a message Slack took is never
  reported unsent, so never sent twice.
- An announcement's kept pictures go when it leaves the remembered list.
- Slack: a student ID typed is read as typed (Slack had made it a link); Show Original sends its picture with its
  words and the post's whole page.
- A message whose sender was stopped half-way (the bot restarted, an update, a power cut) is no longer
  held back for up to a week: a claim taken before a send records its process and is confirmed once the
  message is out; one left by a process that is gone is taken over and the message goes out on the next
  tick. Lecture alerts, reminders, the noon digest and the check-in messages use it, through one shared
  claim code (`daily.claim_in`) instead of three copies.
- When Telegram cannot be reached but the internet works, the doctor now says the network blocks
  Telegram (some providers and routers do) and what to do — a hotspot to be sure, a VPN to fix it —
  instead of "check the internet connection" followed by "All set."
- A `/check` past its 15-minute limit now ends the browser the check started as well; before, Chromium
  was left running and `/check` could wait on it for ever.
- The bot and the monitor take turns writing `failsafe.json`, so one no longer writes back a copy
  without the other's record (an alert told twice, or a "fine again" never sent).
- A quiz published a minute before a check was announced, and listed, with "No due date set": Moodle's
  calendar did not list it yet. A quiz the calendar does not list is now dated from its own page
  (`Opens:` / `Closes:`), so it comes with its times and gets its reminders from the first message.
- The calendar's Arabic event words are taken off a task's name also when they carry vowel marks
  ("اختبار 1 يُفتح" was kept whole).
- 🔗 View Original Post buttons say which reminder they are for — `(1)`, `(2)`, as the list numbers
  them — instead of a cut-off title, and follow the list when a reminder is done.
- 🔗 View Original Post no longer leaves the menu above its reply: the reply is the bottom screen, takes the
  previous menu out and has 🔙 Main Menu under it. A test now fails when a screen is sent around the menu.
- Upcoming Tasks & Reminders shows each reminder whole: only its first line was listed, so a note
  of several lines (a pasted announcement with its date, time and place) looked cut off. If ten long
  reminders would not fit in one Telegram message, whole reminders are left off the end and the
  screen says how many.
- The bot reads Moodle in English whatever language you use there: dates and attendance choices were
  only understood in English, so an Arabic Moodle would have made it miss lectures. It asks for English
  in its own session (`?lang=en`); your own Moodle language is not touched.
- An assignment leaves the task list only when it is handed in: Moodle's own *done* mark (which opening
  the page can set) no longer hides one that is still to do.
- A course text that holds an HTML comment (text pasted from Word) no longer fails the course's read.
- A description keeps the words before its first formatting tag.
- A saved Moodle session cut short by a power loss no longer stops every check until it is deleted by
  hand: the check signs in again, and the session is now written in one step.
- `saved_items.json` that another process holds open for a moment is read again instead of being set
  aside as damaged and replaced by an older backup.
- A new file whose message failed is sent WITH the file on the next check (it said "same file as an
  item already sent" about a file never sent; a folder was saved twice).
- An assignment or quiz first seen less than an hour before its time gets one reminder, saying the
  minutes really left — not a "24 hours" and a "1 hour" reminder in the same minute.
- Moodle's own "sign-in failed" page is no longer taken for a successful sign-in.
- Two writers of the same state file (the bot and a check) no longer share one temporary file.
- Unreadable instructor pages no longer start the browser on every check (tried again after 6 h).
- An unexpected error reading the portal timetable no longer signs in to the portal every 10 minutes.
- A folder download that fails leaves no empty folder (each retry used to add "name (1)", "(2)", …).
- A long file name is never cut so that it ends in a space or a dot (Windows drops those).
- A nameless Moodle item or deadline is called "Untitled" (it was the Arabic "بدون اسم").
- The portal-setup and verification-code messages say when the portal is really contacted, in full sentences.

## [1.1.0] - 2026-11-11

### Changed
- The license is now PolyForm Noncommercial 1.0.0: free for any noncommercial use, with the copyright
  notice kept; selling it needs the author's permission. Version 1.0.0 stays under MIT.

### Added
- The bot tells you when a new version is ready (once a day, and on `/update`), with what is new from
  `NEWS.md`, and ⬇️ Update Now installs it and restarts the bot — only on your press, only a clean
  clone, and a version that does not start is taken back.
- 🗓️ Registration Plan (`/register`, under 📜 Academic Record & Balance, and in a guest look-up): what to
  register next term, from your study plan and its prerequisites as the portal lists them — choose the
  hours the term allows and get the courses, the most important first; every course that is not open
  yet is listed with its reason (a prerequisite, final year only, graduation term only).
- Automatic attendance check-in for online lectures (`"auto_check_in": true` in `schedule.json`):
  only a day that is an online lecture (ZUJ's ك), never an in-person (و, م) or activity (ن) day —
  read from the portal's own timetable (the bot reads it by itself, once — and again on `/check` —
  with `auto_check_in` on, and tells you what it found, and any row it could not read; Sunday to
  Wednesday only), else from ZUJ's codes in Moodle, `schedule.json`, or the ك at the end of
  the Moodle course name; Present as soon as the instructor opens it, checked on Moodle, with a screenshot of your row as
  proof — or a 🚨 with the reason, the page and a button to check in by hand.
- Every day at noon, one message with every open assignment, quiz and Cambridge One task, closest
  first, until it is done; anything due within a day comes first under a loud 🚨 LAST DAY heading.
- Your mark on a quiz or an assignment is told the moment Moodle shows it on the activity's own page
  (🎉 Grade Posted, with a picture of the page); a corrected mark is told again.
- Your marks are found in PDF files too, in a new version an instructor uploads over a file, in a
  file attached to a forum post, in a line of text and in lists typed with Arabic digits.
- The transcript counts your degree from your own study plan on the portal — any major, any plan
  year — instead of a number typed into `schedule.json`: the plan's hours, passed, this term (and
  how many are retakes), failed and not retaken yet, and what is left now and after this term. A
  guest look-up gets the same from the guest's plan.
- 📝 Placement Exams in the transcript's summary and in Remaining Courses (and a guest's): the
  Arabic, English and computer-skills exams from the plan's remedial courses — exempt, or a course
  to take on top of the plan's hours, with those hours said apart from the degree's.
- 📚 Remaining Courses under the transcript (and a guest's): what the plan still asks for, group by
  group; an elective group lists what can be chosen.
- 📝 Coursework Total in a course's panel (⚙️ Course Adjustments): type your marks before the final
  once, as `42/60`, and the panel shows the best mark the course can still end on and what the
  final must bring to pass. It replaces the Set Syllabus and Enter Marks buttons.
- Grade Needed knows what a course can still reach: with its coursework total (30 of 50), it shows
  🔝 Best possible: 80, asks that course for no more
  than that and the other courses for the difference, and says a target is out of reach from the
  real ceilings.
- Upcoming Tasks lists every open task: Cambridge One assignments and tasks without a due date too.
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1, adapted) with a private way to report.
- A pull request template with the checks the repository relies on.
- An "unofficial, use at your own risk" notice in both READMEs.

### Changed
- Messages are shorter: what an instructor wrote is in the picture, not typed out again under it
  (the wording is sent only when there is no picture or file). A forum post or announcement arrives
  as a picture of the post. An edit says what changed — *Was* / *Now*, line by line — instead of
  repeating the whole text.
- A file that looks like a marks list in which your mark was not found (a scanned PDF, or your
  number is not in it) says so, so you open it.
- A transcript is one message — the screenshot with the summary — with ✅ Courses Taken and
  📚 Remaining Courses as buttons, instead of a second long message with every semester.
- `scripts/windows_recovery.ps1` is safer to hand to other people: by default it only stops Windows Update
  from restarting the PC while you are signed in; automatic sign-in (`-AutoSignIn`) and the lock task
  (`-LockAfterSignIn`) are opt-in; `-Preview` shows every setting it would change without changing or
  needing administrator rights; it asks you to type YES (`-Yes` skips it); it tests that the account really
  has a blank password (`PasswordRequired = False` does not mean that); it now sets and, with `-Undo`,
  restores the "only Windows Hello" setting that made Windows ignore automatic sign-in. The READMEs explain
  the three ways to run the project (scheduled tasks, by hand, tasks plus this script) and why none of it
  is dangerous.

### Removed
- 📖 System Guide: the button, its five pages and `guide.py`. The guide is the README and
  `docs/REFERENCE.md`; `/help` says where they are.
- The date line above the first message of each day and the 11:59 PM "Daily Recap" (and the
  next-morning catch-up): they filled the chat on quiet days. Only real news, lecture alerts and
  reminders are sent now.
- The optional morning overview (`morning_summary_hour` in `schedule.json`): it was off by default,
  listed online lectures only and duplicated the reminder an hour before each one. A `schedule.json`
  that still has the line is told so on Telegram — delete the line.
- The Lecture Links screen and `/setlink`: a meeting link only matters on the lecture alert, which finds
  it in the course by itself.

### Fixed
- An assignment whose due date had already passed when it appeared (a date left over from last year's
  copy of the course) is listed in Upcoming Tasks and the noon digest for as long as Moodle still accepts
  a submission, and one handed in after its date is no longer shown as pending.
- The reminders list keeps an Arabic note, the course and the time on lines of their own.
- A picture of a quiz or assignment page no longer comes out blank white: on those pages the
  wrapper around the content is the floating one, and it was hidden with the floating widgets.
- A course the university renames in Moodle mid-term keeps its timetable, instructor and folder:
  another section tag (`-Sec12-`) or the same words in another order are recognised, and any other
  new name is tied to the course by its Moodle id, which does not change (`course_names.json`).
- The transcript and the balance carry their own way back (🔙 Academic Record · 🏠 Main Menu); the
  menu is no longer repeated under them, and each course list ends with the other list.
- When a check crashed on a bug you were told, but never that it works again: the first run that
  goes through now says so, once.
- A broken project file (a stray space in `bot.py` once kept the bot from starting after a Windows
  restart) is now told the moment it happens — file, line and how to restore it — instead of when the
  bot fails to start hours later; the "bot isn't responding" alert names that cause too.
- A check no longer stops when Windows refuses for a moment to save its state (the file was open
  elsewhere, e.g. in an antivirus scan): the save waits a little and tries again.
- Quizzes had no date and no reminders: this Moodle lists a quiz by its closing event, which was
  ignored. A quiz now has its opening and closing times, and one you have attempted stops reminding.
- A quiz you had taken stayed "not attempted": Moodle 5 lists attempts in a different block than the
  one that was looked for. It is read after the quiz closes too.
- A reminder for something published less than a day ahead said "in 24 hours"; it says the time
  really left. Every reminder sent is written to the log.
- A finished Cambridge One assignment stayed in the open tasks: an activity opened again after it
  was handed in, or one waiting for the teacher's mark, did not count as done. Handed in once is
  done, as on Cambridge's own Completed list; progress reads "8 of 10 activities done — 2 left".
- The two-minute look at Moodle crashed when Moodle answered with one error object instead of its
  list of answers; that now counts as a failed look, tried again at the next one.
- The same items were announced as new again and again: when the portal dropped a connection the
  course was read through the browser, which has no section descriptions, so they were forgotten and
  "new" on the next check. What that read lacks is now kept, and an item that was announced once is
  never announced as new again.
- "Content Updated" arrived over and over for a block that holds files or assignments: the theme's
  own icons carry a cache number in their address, which counted as an edit whenever the portal
  cleared its cache. Only pictures of the content itself count now.
- The picture of an edited sub-section showed the whole course page; it shows that block only.
- Names with `&` in them read `&amp;`.

### Security
- `certifi` is pinned to a release newer than 2026.10.18 (pip-audit: PYSEC-2026-4320): the older
  bundle trusted a root certificate whose intermediate certificates were revoked after their private
  keys leaked, which would let someone on the network path impersonate a site to `requests`.

## 1.0.0 - 2026-10-28

Published under the MIT license; its release was withdrawn when the license changed in 1.1.0.

First public release.

### Added
- Moodle monitoring every 15 minutes: new files, assignments, quizzes, announcements and forum posts, each
  with its full text and a screenshot; course files saved into a tidy folder per course.
- Deadline reminders 24 h and 1 h ahead, online-lecture alerts with a Join button, attendance check-in,
  and personal reminders.
- Grades and GPA: transcript and balance on demand, a "grade needed" planner, and Finals Watch.
- Microsoft sign-in (including 2FA) relayed to Telegram, a bot that restarts itself, and alerts when
  something needs attention.
- Optional Cambridge One watching for English courses, with automatic sign-in when credentials are set.
- A double-click installer (`install.bat`), a USB backup helper (`backup_usb.bat`), and `doctor`, which
  checks a setup.
- English and Arabic READMEs, with previews drawn from invented sample data.

[Unreleased]: https://github.com/Humam-Taibeh/UniNotifier/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Humam-Taibeh/UniNotifier/releases/tag/v1.1.0
