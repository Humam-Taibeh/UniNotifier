# Disclaimer

UniNotifier is an independent, noncommercial student project. It helps a student keep up with **their own**
courses at Al-Zaytoonah University of Jordan. Please read this before you use it.

## Not official

UniNotifier is not affiliated with, endorsed, sponsored or supported by Al-Zaytoonah University of Jordan,
Moodle, Microsoft, Slack, Telegram or Cambridge University Press & Assessment. Their names are used only to
say which sites and apps it works with. The university's own systems — Moodle (`elearning.zuj.edu.jo`), the
registration portal (`portal.zuj.edu.jo`) and the exams site (`exams.zuj.edu.jo`) — are always the official
record.

## It assists; it never decides

- **Attendance.** Your attendance is what your instructor records, always. Automatic check-in
  (`AUTO_CHECK_IN`, off by default) only presses the button you could press yourself during an online
  lecture — and it does so **whether or not you are actually attending**. Using it for a lecture you do not
  attend may break your university's attendance rules. Switch it off, or check in by hand, when you will
  not be there. The absence counter (`/attendance`) is a personal note from your own answers, not the
  official count.
- **Marks and GPA.** Marks, averages, hours and remaining courses are read from the university's own pages
  and recalculated to help you plan. The portal and your instructors are the official source; when they
  differ, they are right.
- **Registration.** The Registration Plan is advice from your study plan's rules as published. Nothing is
  ever registered, added or dropped for you: you register on the portal yourself.
- **Moodle.** Automatic *Mark as done* (`AUTO_MARK_DONE`, off by default) ticks a file the bot saved for you.
  Your instructor's completion report then shows it as done. Assignments, quizzes and forums are never
  touched, and nothing is ever submitted.

## Your account, your responsibility

- UniNotifier signs in with **your own** account, the way you would in a browser, at a gentle pace: a full
  check every 15 minutes, a small look every 2 minutes, and a few portal reads a day (the README lists them).
  It only reads, apart from the two opt-in actions above.
- Use it only with your own account. `/guest` looks up a friend's record with *their* login, only while they
  are there and agree; the login is typed into a private form in Slack (in Telegram the message is deleted
  at once), used once and never saved.
- Follow your university's rules on the use of its systems. If the university asks you to stop using an
  automated tool, stop.
- Your passwords stay in `.env` on your PC. The messages the bot sends you are kept by Slack (or Telegram)
  in your own private workspace or chat.

## No warranty

UniNotifier is provided "as is", without warranty of any kind, as the [LICENSE](LICENSE) says. A message can
arrive late or not at all — when the PC is off, the network is down, or a university site changes. Always
check Moodle and the portal for anything that matters: deadlines, exams, marks and attendance.

Questions or a problem? See [SECURITY.md](SECURITY.md) for security reports, and the README for everything
else.
