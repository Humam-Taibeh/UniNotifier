"""
UniNotifier — the unattended, scheduled entry point. It orchestrates:

  moodle.py    reads courses, the full content tree, activity indexes, deadlines,
               labels/descriptions, forum posts; ticks "Mark as done"
  richtext.py  turns portal HTML into formatted Telegram text
  media.py     decides how each new item is handled and words the messages
  archive.py   downloads files into the per-course folder tree
  grades.py    looks for the student's number inside downloaded documents
  notifier.py  Telegram delivery (Markdown, inline button, auto-retry)
  schedule.py  lecture reminders and morning summary from schedule.json
  gitsync.py   commits/pushes the state to its own private repository (optional)
  zuj_auth.py  Microsoft SSO login

The run is silent unless something needs saying: a new item/file/grade/forum
post, edited course text, a due reminder (24h / 1h before a deadline, lecture
reminders), or an error that persisted long enough to need attention.
"""

from uninotifier.monitor.run import main

__all__ = ["main"]
