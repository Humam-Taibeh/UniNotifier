# Contributing

Thanks for helping! UniNotifier is built for Al-Zaytoonah University's Moodle and student portal,
running on a student's own Windows PC. Fixes and improvements within that scope are very welcome;
for anything bigger, open a feature request first so we can agree on the shape. Everyone taking part
agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Set up

```powershell
.\install.bat                                         # or follow the README's setup steps
.venv\Scripts\pip install -r requirements-dev.txt     # pyflakes and pylint
```

## Before you open a pull request

```powershell
.venv\Scripts\python tests\test_architecture.py
.venv\Scripts\python -m pyflakes src tests monitor.py bot.py
```

Both must pass — CI runs them on Windows with Python 3.11, 3.13, 3.14 and 3.15, and runs the student installer.

## The rules the tests enforce

1. **Tests are offline.** No test may reach Moodle, the portal, Telegram or any other host, or start a
   real browser: guards refuse it and fail the run. Use local fixtures (`tests/fixtures/`) and loopback
   servers. The suite runs on a made-up `.env` in a temporary folder and uses the made-up student ID
   `SID`, never a real one. A new test is a `def test_…()` **and** an entry in the runner list at the
   bottom of `tests/test_architecture.py`.
2. **No personal data, ever.** Never commit `.env`, `schedule.json`, `data/`, `assets/` timetables, logs or
   `tests/recorded/` (real pages). Tests fail if a personal path, a real ID, or one of those files shows up.
3. **Both READMEs change together.** `README.md` and `README.ar.md` must keep identical code blocks,
   diagram and badges, and tables of the same shape — the parity test compares them. In the Arabic file,
   start every paragraph, list item and table cell with an Arabic word: GitHub picks the text direction
   from the first letter. `docs/REFERENCE.md` is English only.
4. **The bot speaks English.** Every button, alert and message the bot writes is in English; content
   from Moodle or the portal stays as it was published.
5. **Fail loudly.** In code that runs unattended, a catch-all `except` must tell the owner through
   `failsafe`, and state files are written through `fileio` — guard tests check both.

## Dependencies

Versions are pinned exactly in `requirements.txt`. Dependabot proposes updates every Monday; merge one
only when CI is green. Playwright updates arrive on their own because they change the browser that
signs in — after merging one, watch the next real check.

## Commit messages

A short prefix and an imperative summary: `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `deps:`, `refactor:`.
