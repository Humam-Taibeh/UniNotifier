# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

### Fixed
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

## [1.0.0] - 2026-10-28

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

[Unreleased]: https://github.com/Humam-Taibeh/UniNotifier/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Humam-Taibeh/UniNotifier/releases/tag/v1.0.0
