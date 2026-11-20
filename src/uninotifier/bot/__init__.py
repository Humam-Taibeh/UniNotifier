"""
UniNotifier interactive bot — answers Telegram commands and dashboard buttons.

A long-polling process, separate from the scheduled monitor.py. It reads
saved_items.json / schedule.json, and a "fresh check" simply launches
monitor.py as a subprocess (monitor.py holds a run lock, so a manual check can
never overlap a scheduled one). The only portal call it makes itself is the
"Mark as Done" toggle, over plain HTTP with the saved session cookies.

  /start    dashboard with inline buttons
  /tasks    upcoming assignments and quizzes (submitted ones are hidden)
  /items    everything tracked on the portal, per course
  /gpa      semester / cumulative GPA calculator (hours, retakes, grades)
  /need     the final-exam mark needed to reach a target:  /need 60
  /grades /transcript /balance   official data from portal.zuj.edu.jo, on request
  /finals   finals-season watcher: announces each released grade, then the semester
            average and new cumulative GPA
  /check    run a monitor pass now and report what it found
  /schedule the weekly timetable, as a PDF and an image
  /courses  courses with section, instructor and weekly lecture times

Only the owner is answered (access.py: the Telegram id, or SLACK_USER on Slack); everyone else is ignored.

Run:  .venv\\Scripts\\pythonw.exe bot.py        (no console window)
"""

from uninotifier.bot.app import main

__all__ = ["main"]
