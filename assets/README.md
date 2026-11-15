# Your timetable pictures (optional)

You do not need to put anything here. With the portal login in `.env`, the bot reads your
timetable from the registration portal and **draws it itself**: that is what the
**📅 Weekly Timetable** button sends.

Put a file here only to change that:

| File name (exactly)              | What it does                                                        |
|----------------------------------|---------------------------------------------------------------------|
| `Weekly_Academic_Schedule.pdf`   | a timetable PDF of your own — sent instead of the drawn one          |
| `Weekly_Schedule.png`            | a timetable picture of your own — sent instead of the drawn one      |
| `logo.png`                       | a logo for the header of the drawn timetable (the project ships none) |

Either timetable file is enough. Everything in this folder except this README is git-ignored,
so nothing of yours ends up in the repository.
