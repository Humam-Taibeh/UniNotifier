# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Automatic attendance check-in for online lectures (`"auto_check_in": true` in `schedule.json`):
  only a day that is an online lecture (ZUJ's ك), never an in-person (و, م) or activity (ن) day —
  read from the portal's own timetable (the bot reads it by itself, once — and again on `/check` —
  with `auto_check_in` on, and tells you what it found, and any row it could not read; Sunday to
  Wednesday only), else from ZUJ's codes in Moodle, `schedule.json`, or the ك at the end of
  the Moodle course name; Present as soon as the instructor opens it, checked on Moodle, with a screenshot of your row as
  proof — or a 🚨 with the reason, the page and a button to check in by hand.
- Every day at noon, one message with every open assignment, quiz and Cambridge One task, closest
  first, until it is done; anything due within a day comes first under a loud 🚨 LAST DAY heading.
- Upcoming Tasks lists every open task: Cambridge One assignments and tasks without a due date too.
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1, adapted) with a private way to report.
- A pull request template with the checks the repository relies on.
- An "unofficial, use at your own risk" notice in both READMEs.

### Fixed
- A broken project file (a stray space in `bot.py` once kept the bot from starting after a Windows
  restart) is now told the moment it happens — file, line and how to restore it — instead of when the
  bot fails to start hours later; the "bot isn't responding" alert names that cause too.
- A check no longer stops when Windows refuses for a moment to save its state (the file was open
  elsewhere, e.g. in an antivirus scan): the save waits a little and tries again.
- Quizzes had no date and no reminders: this Moodle lists a quiz by its closing event, which was
  ignored. A quiz now has its opening and closing times, and one you have attempted stops reminding.

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
