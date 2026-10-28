# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1, adapted) with a private way to report.
- A pull request template with the checks the repository relies on.
- An "unofficial, use at your own risk" notice in both READMEs.

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
