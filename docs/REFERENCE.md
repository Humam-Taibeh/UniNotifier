# UniNotifier — reference

How every part behaves, in depth. To install and run it, see the [README](../README.md).

- [Authentication](#authentication) · [Silent operation](#silent-operation) · [Local archive](#local-archive)
- [Deep check & smart handling](#deep-check--smart-handling) · [How it works](#how-it-works) · [Project layout](#project-layout)
- [Settings check and the doctor](#settings-check-and-the-doctor) · [Scheduled tasks](#scheduled-tasks) · [Moving to a new PC](#moving-to-a-new-pc)
- [Testing](#testing) · [Security notes](#security-notes) · [Interactive bot](#interactive-bot)

## Authentication

This Moodle instance doesn't use a plain username/password form — login
goes through Microsoft's real sign-in flow (`login.microsoftonline.com`),
which can include:

1. A passkey/FIDO nudge screen. Playwright's CDP `WebAuthn` domain is used
   to register a virtual authenticator for the browser session, so this
   never reaches out to a real OS-level authenticator (which otherwise
   could pop up a native Windows Hello dialog, even during a headless
   background run). The virtual authenticator has no matching credential,
   so the ceremony fails fast and the flow falls through to password sign-in.
2. Email + password.
3. For accounts with it enabled, a genuine second factor (Microsoft
   Authenticator push notification or an SMS code). **No script can
   complete this on your behalf** — it requires your phone.

### Second factor from Telegram

When the saved session expires and Microsoft asks for verification,
`monitor.py` relays it to Telegram (`mfa.py`) instead of failing:

| Microsoft shows | You get on Telegram |
|---|---|
| Authenticator number matching | `🔐 Microsoft Sign-in Approval` with the number (e.g. 4️⃣7️⃣) and a screenshot of the sign-in box. Enter it in Authenticator; the check carries on by itself |
| A plain "approve" push | The same message, asking you to tap **Approve** |
| An SMS / app code | `📱 Verification Code Needed`; reply in the chat with the digits |
| "We didn't hear from you" | One fresh request is sent automatically |

Every approval message has a **📱 Text me a code instead** button: the
monitor switches Microsoft to SMS and then waits for the code you reply
with. A wrong code is reported and can be re-sent. "Don't ask again for N
days" is ticked whenever offered, and the renewed session is saved to
`auth_state.json`. Success ends with `✅ Signed in`.

One attempt waits up to 5 minutes. An unanswered attempt is retried
automatically at most **once an hour** and **never between 00:00 and
07:00**, so an ignored request can't become a stream of Authenticator
pushes; the expiry message has a **🔄 Try again now** button, and `/check`
always tries immediately. Only `bot.py` reads Telegram; it hands your code
or choice to the waiting monitor through `mfa_request.json` /
`mfa_response.json` (git-ignored), so **`bot.py` must be running** for SMS
codes (number matching works without it). An unrecognised verification
screen is still sent to you as a screenshot, and its HTML is kept in
`mfa_debug/` for diagnosis.

`python -m uninotifier.tools.login_refresh` remains as a last resort: it opens a real, visible
browser window so you can complete the sign-in yourself.

## Silent operation

The scheduled run is fully silent: headless browser, no windows. Telegram
messages are sent **only** when:

1. a new item, assignment, file, forum topic or reply is detected;
2. an instructor edits existing text (a label, page, description or
   section summary);
3. a deadline reminder is due (24 h and 1 h before);
4. an error has **persisted**: a network outage is reported after 3
   consecutive failed checks, any other unexpected error after 2, and an
   expired session / manual verification at once. Repeats are throttled to
   one per 6 hours, and a `✅ Back to normal` message follows once checks
   succeed again;
   a portal **under maintenance** (`maintenance.py` recognises "قيد الصيانة", "تحت الصيانة",
   "Under Maintenance", "Service Unavailable", "Bad Gateway", …) is reported at once with a
   screenshot of the page (`maintenance_snapshot.png`), and the run ends cleanly — exit 0,
   state untouched, healthchecks.io gets a success ping noting the maintenance. Any other
   sign-in failure that isn't a wrong password or a Microsoft verification also comes with
   the screenshot — and when the browser was already inside Moodle (past the sign-in page) the
   alert says so, that the password is fine and that the next check retries, instead of
   printing the raw timeout. If Moodle's own session lapses while Microsoft's is still valid,
   the sign-in simply lands back on the dashboard and counts as signed in. The registration portal (`/grades`, Finals Watch) does the same;
5. a day ends with nothing new (the 11:59 PM recap, below).

**Network hiccups.** The university server drops connections now and then (WinError
10054, TLS handshake timeouts). Each request is retried twice with backoff (2 s, 4 s,
`httpctx.py`), and a check that still fails on the network is run again twice (after
20 s and 60 s) before the run counts as failed — so healthchecks.io gets a failure
entry only when all of that failed. A run killed while holding `monitor.lock` (the
scheduler's 15-minute limit, a shutdown) no longer blocks the next checks: a lock
whose process is gone is taken over at once. The Git backup can't stall a run either
— git gets a hard time limit with the whole process tree killed, gives up on a stalled
transfer, and is never allowed to open a credential window (pin the account with
`git config --local credential.https://github.com.username <you>` when the credential
manager holds more than one GitHub account).

The very first run saves a silent baseline. A message that fails to
deliver (after 3 attempts with backoff) is retried on the next run rather
than lost. The bot token is masked (`bot<redacted>`) in every log line,
including network tracebacks.

## Local archive

Every course gets a folder under `ARCHIVE_ROOT` (set in `.env`; without it,
`University` in the Documents folder Explorer shows — OneDrive's when OneDrive backs up
Documents) with this structure:

```
<Course in English>/
  01_Lectures                   lectures, slides, and the syllabus / course plan
  02_Assignments
  03_Exams                      exam papers, solutions, exam dates
00_Schedules/                   your own timetables and notes — the bot never
                                files, renames or de-duplicates anything here
```

Course folders always carry the **English** name (`schedule.json` knows the
Arabic portal names and aliases such as "Databases" → "Database Systems"), so a
file never lands in a second folder for the same course, and each course
holds exactly those three subfolders. Every downloaded file stays inside its course:
a syllabus or course plan goes with the lectures, an exam schedule or dates file
with the exams.

**No duplicate files.** Two Moodle items often point at the very same file. Before
anything is written, the download is compared (size, then SHA-256) with the files in its
destination folder; identical bytes are never saved again, and no `name (1).pdf` is
created — the item simply points at the existing file. (A file the instructor
re-uploads with different content takes the file's place, and the version it replaces moves,
unchanged, into an `_Old_Versions` folder beside it — created only then — as `<name>_v<n> (<date>)`;
the record keeps both, so neither is downloaded again.) `python -m uninotifier.tools.tidy_archive` cleans up an archive that already has copies
(dry run by default, `--apply` to do it): it collapses byte-identical copies that share a
base name, keeping the clean name, and re-routes misfiled files — it never deletes a
file whose content differs.

**Practice questions** ("suggested questions", exercises, worksheets, problem sheets;
Arabic أسئلة / تمارين too) go to `02_Assignments`; `01_Lectures`
is for lecture notes, chapter slides and course material. Exam words (midterm, final,
quiz…) still send a file to `03_Exams`.

`python -m uninotifier.tools.migrate_archive` moved an existing archive to this layout (dry run by
default, `--apply` to do it; it only renames or moves inside the semester
folder, never overwrites, and takes the monitor's run lock).

New files (resources; folders are extracted from their zip) are downloaded
with the existing session, filed by type/name keywords, and recorded
(id + size) in `saved_items.json` so they're never downloaded twice. Files
up to 45 MB are attached to the Telegram message; anything else gets a
full-page screenshot of the portal page. Every message has a
**🔗 Open in Portal** button.

## Deep check & smart handling

Each run scans every course three ways and merges the results: the full
section/activity tree (`core_courseformat_get_state` — every week;
`core_course_get_contents` itself isn't exposed on this server), the
assignment and quiz activity indexes (`/mod/assign/index.php`,
`/mod/quiz/index.php`) as a confirmation pass, and the rendered page as a
fallback. The server-rendered course page (one plain HTTP request, no
browser) adds what the tree doesn't carry: the content of labels ("Text and
media area" — e.g. an instructor's bio card), every activity's description,
section summaries, and which activities have a manual "Mark as done" toggle.

| Item kind | Handling |
|---|---|
| Files (PDF, Word, PPTX, Zip…) and folders | Downloaded, filed into the course's archive folder by type (lectures & slides, assignments, exams), attached to Telegram with `📂 Saved to: <course>/<folder>` |
| URL / video links | Not downloaded. `🎥 New Video` (video hosts) or `🔗 New Link` with the direct link |
| Labels, pages, section summaries | Full formatted text **plus** screenshots of exactly that part of the portal |
| Assignments, quizzes, other activities | Screenshots of the activity page + deadline + its description as text |
| Forum topics and replies | Subject, author, time and the full post; attachments forwarded as files |
| Unrecognised type, or a file/link that couldn't be handled | `⚠️ Unclassified Item` with the link |

Every message carries the item's own text in full (long text is split into
numbered parts). The screenshot is one full-length image of exactly that
block, rendered at 2x and sent as a **PNG file**, so Telegram keeps it
uncompressed and the text stays sharp when zoomed. Only a block taller than
Chromium's ~16k-pixel limit is split, into the fewest equal parts. Untitled items
get a real title: a page named "." is shown under its section's title, a
label under its first line.

### Edits and forum posts

Each label / page / description / section summary is fingerprinted (its
visible text and image files). When an instructor edits one — e.g. fills in
the empty "lecture time" in a bio card — you get `✏️ Content Updated` with
the current version. Every forum (including each course's Announcements) is
read on each run; new topics arrive as `📢 New Announcement` (Announcements
forum) or `🗣️ New Forum Topic`, replies as `💬 New Forum Reply`.

### Teams lectures and attendance

Two activity types ZUJ uses for online teaching are read from their own pages
(`activities.py`):

* **Teams meeting** (`teamscheduler`) — `💻 New Online Lecture` with the meeting's
  details (repeat days, period, time, organizer) and a **🔗 Join Teams Lecture** button
  that opens the meeting itself (the raw link is never pasted). The join link also becomes
  the course's lecture link, so the lecture reminders carry 🎥 Join Meeting. A meeting
  saved before its link was read is read again, quietly, until the link is known.
* **Attendance** (`attendance`) — `📝 Attendance Check-in` with the next session and a
  **✍️ Check In / تأكيد الحضور** button to the attendance page, where check-in opens
  during each session. `activities.check_in()` prepares automatic self check-in: it finds
  the open session's submit link, reads the form and picks *Present*, but submits nothing
  until `SUBMIT_VERIFIED` is set — once a real check-in page has been seen, that is the
  one step left. Password-protected sessions are never submitted.

### Mark as done

| Item | Behaviour |
|---|---|
| Files, folders, links, pages, labels | Ticked **automatically** once delivered; the message has a `↩️ Undo Done` button |
| Forums and other activities | Never automatic; the message has a `✅ Mark as Done` button |
| Assignments and quizzes | **Never touched** — Moodle completes them on submission/attempt |

Only activities whose completion is manual on the portal get a button. The
buttons are handled by `bot.py`, which calls the portal over plain HTTP with
the saved session (refreshed in `auth_state.json` after every run).

### Grade finder

Every downloaded `.xlsx/.xls/.docx/.txt/.csv` (and every such file inside
an extracted folder) is searched for the student number (`STUDENT_ID` in
`.env`, else `PORTAL_USERNAME`; without either, files aren't searched). If found, the row's marks are put in the
notification: `🎉 تم رصد علامتك! | الدرجة: 36/40`. Limitation: a file
re-uploaded under an existing item isn't re-downloaded.

### Class schedule

Each online lecture produces two Telegram messages (`lectures.py`, sent on the
minute by the bot and, as a fallback when the bot is down, by the monitor — a
claim file guarantees each goes out once):

1. **One hour before** — plain text, no buttons: `⏰ Reminder: <course> starts in 1 hour`.
2. **At the start** — the actionable alert: **🎥 Join Meeting**, **🌐 Open Course
   Page**, a screenshot of the post where the link was published (or a note
   that no link has been posted yet) and **✍️ Check In** for each attendance
   activity of that course — whichever online course it is. A late start (PC was off) says "in progress,
   started N min ago".

`schedule.json` holds the weekly timetable (days, times, rooms, mode). Only
`Online` slots get alerts (`remind_modes`), 60 minutes ahead
(`lecture_reminder_minutes`); a morning overview can be switched on with
`morning_summary_hour` (off by default). These work even when the portal check fails.

### Online lecture links

Courses with an `"Online"` slot in `schedule.json` get a meeting link for
the semester (`lecturelinks.py`). Every run looks for Teams / Zoom /
Google Meet links in everything that course publishes — labels, section
summaries, descriptions, pages, URL activities and forum posts. The first
time a link appears it is saved and announced (`📌 Lecture Link Saved`), and
from then on each lecture reminder carries two buttons — **🎥 Join Meeting**
(opens Teams / Zoom directly) and **🌐 Open Course Page** (the exact post,
section or item the link was found in) — plus a screenshot of that post, so
you can check the context at a glance. The screenshot is taken during the run
that finds the link and kept in `lecture_link_shots/`. Without a link, the
reminder says it hasn't been posted yet and the next one carries it once it is.

The newest link wins: every link ever seen is remembered, so an old link
left in a label never overrides a newer one posted later. There is no screen or
command for links — the lecture reminder is where the link is used. Links are kept in
`lecture_links.json` (git-ignored).

### Guest look-up (`/guest`)

To let someone else see what the bot does with *their* record — once, from Telegram:

1. `/guest` (or 👤 Guest Look-up under 📜 Academic Record & Balance), then their student ID.
2. Their portal password — the bot **deletes that message at once** and keeps the password
   in memory only for the sign-in.
3. If the portal sends them a verification code, read it from their phone and reply with it.
4. Their record arrives in the layout of your own transcript: the screenshot with an executive
   summary — 📘 major, 🎓 cumulative GPA, ⏱️ passed hours (and what is in progress / to repeat),
   📚 attempted hours, 🏁 hours left (only when `"degree_hours"` — the credit hours of the whole
   degree plan — is set in `schedule.json`; it is never guessed. The same line appears on your own
   transcript), 📌 status — then one text message
   with the semesters (the term in progress first, this term's published marks filled in). The major
   is read from the financial page of their portal, which is used for nothing else and shown nowhere.
5. **🎯 Target GPA** (under the record): pick 60–90% or type your own, and see the average they need
   over this term, this + the next, and this + the next two terms — the university's rules (marks
   under 35 count as 35, a retake replaces the old attempt), the same numbers the planner gives for
   the term in progress.
6. **🚪 Sign Out & Delete Messages** removes the whole session from the chat (or **Sign Out,
   Keep Messages**). Nothing is stored or synced at any point: not your GPA base, retakes,
   grades, saved state or `portal_debug/`. No alerts are set up for them.

The same report is available on this PC without Telegram: `.venv\Scripts\python -m uninotifier.guest`.

### Saved portal answers and session reuse

**Transcript** and **Balance** answer at once from the last read, stamped `🕒 As of`. When that
copy is 15 minutes old or more, the portal is read right behind it and the same message is
updated in place: only the stamp when nothing changed, the screenshot and text when something
did (`🔄 updated`). A refresh that fails leaves the saved copy and says so. Grades (the Finals
Watch check) are always read fresh. Saved answers live in `data/portal_cache/`.

Every portal read of your own keeps the session (`data/portal_session.json`) and the next read
within 15 minutes reuses it — no sign-in, measured 9.3 s → 4.1 s. Measured on 2026-10-20: a
session survives 20 idle minutes but not 40, and the portal has never asked for a code; its
cookies end with the browser, so it has no "trusted device" to keep. An ended session simply
signs in again. A guest look-up never uses or replaces your session.

### Finals Watch (`/finals`)

The portal is on-demand everywhere except here: while **finals mode** is on, the
bot signs in every 30 minutes (`finals.py`) and tells you:

* `🎓 Grade Released` — one message per course the moment its mark appears
  (`✏️ Grade Updated` if the portal changes it), marking a retake and what it replaces;
* `🏁 Semester Results` — once every course has a mark: the semester average and
  your **new cumulative GPA**, with a button to make it your CGPA in `/gpa`.

Turn it on with `/finals on` (off: `/finals off`; `/finals check` reads the portal
now), or let `schedule.json` do it: `"finals": {"from": "2027-01-04", "to": "2027-01-31",
"interval_minutes": 30}`. The first check of a term only reports how many grades are
already out. An unattended check never waits for a verification code; it says so once
every few hours and leaves it to `/finals check`.

Each reading is compared with the last one kept in `finals.json` (`seen`: course code →
mark), so a new or changed grade is announced exactly once. When every course of the term
has its grade, Finals Watch **pauses itself** (`⏸️ Finals Watch Paused`, with a ▶️ Resume
button) and stops signing in to the portal; `/finals on` resumes it — a late correction
isn't noticed while paused. A `schedule.json` window with a new `from` date arms it again.

The new CGPA is computed from your **full course record** (`gradebook.py`), not the
portal's cumulative field, which updates late. The rules, verified against the portal
(the record reproduces its cumulative average at every semester): every attempted
course counts — passed or failed — with its credit hours; a mark below 35 is recorded
as 35; a retaken course replaces its old attempt (matched by code or name) and its
hours aren't counted twice. So the hours behind your CGPA are *all* attempted hours,
not only passed ones — `/transcript` shows both.

### Grade Needed and Course Adjustments

ZUJ grades on a 100-point scale. The rules, all in `gpa.py`: pass mark 50 per
course; courses are 3 credit hours unless changed; semester average = Σ(grade × hours) / Σ hours;
CGPA = (CGPA × passed hours + semester average × semester hours) /
(passed hours + semester hours). A **retake** replaces its old grade in the
points and adds no hours: new points = old points − old grade × hours + new
grade × hours.

* **🎯 Grade Needed** (`/need`, all buttons): pick **📊 Target Semester GPA** or
  **🎓 Target Cumulative CGPA**, then a target — 50, 60, 70, 75, 80, 85, 90 or
  ✏️ Custom Value. The plan locks every course that already has a grade (🔒
  official marks from `/grades` or Finals Watch win over typed ones) and shares the rest of
  the target equally over the courses still open: the mark each needs, the semester average
  and CGPA it leads to, and — for a course with a syllabus — the final-exam mark it takes.
  A cumulative target uses your base (CGPA and record hours) and treats retakes correctly.
* **⚙️ Course Adjustments** (the calculator): tap a course to set its hours (1 / 2 / 3),
  flag it as a retake (it asks for the previous grade), enter its grade, its syllabus
  (`Midterm 30, Assignments 10, Project 20, Final 40`, adding up to 100) and marks so far
  (`Midterm 24, Assignments 8`); **⚙️ CGPA & Record Hours** sets the base. It shows the
  semester average, projected CGPA and any course below the 50 pass mark.

Everything is kept in `gpa.json` (git-ignored: it holds grades). The "record hours"
you give it are all attempted hours, failed courses included (see Finals Watch);
They are **synced automatically**: every transcript read (`/transcript`, 📜 Academic Record &
Balance) and every Finals Watch check sets the base to the record *before the current term*
— all attempted hours, failures at 35, a retake counted once — so this term's grades,
locked into the planner as they are published (`portal_grade`), are never counted twice.

### Official portal data (on demand)

`/transcript` and `/balance` (and 🔄 Check Grades Now in Finals Watch, also `/grades`) sign in to
[portal.zuj.edu.jo](https://portal.zuj.edu.jo/faces/ui/login.xhtml) and read the page. The answer is
**one message** — the page's screenshot with its summary (and, for a balance, every figure) in the
caption; only a transcript's semesters follow as a single text — and the hub you came from returns
under it as the menu. While it works the chat shows "typing…", not a status message. **Nothing is
polled** — the portal is contacted only when you ask.

Put the login in `.env` (never in chat; it is re-read on every request, so no
restart is needed):

    PORTAL_USERNAME=your portal username
    PORTAL_PASSWORD=your portal password

The portal sends a verification code after the password; the bot asks for it
in the chat and types it in. Reading the transcript also re-syncs the Grade Needed
base and retakes (see below).

Each page is opened by its address (read from the portal's own menu) and, should an
address ever change, found by its Arabic / English menu text; the screenshot is the
source of truth. If a page isn't found, the
bot sends what it saw (and the menu entries) and keeps a dump in
`portal_debug/` (git-ignored; it holds personal data) so the navigation can
be tightened.

### Speed

A check reads the portal over plain HTTP with the saved session cookies
(`httpctx.py`), a few courses and forums at a time, and starts the browser only
when something needs it: signing in, a screenshot of something new, the weekly
teacher refresh, or the **deep check** every 6 hours (the one pass that needs
rendered pages: it confirms through the activity indexes that no assignment or
quiz was missed — it has never found anything the activity tree lacked). A
routine check with nothing new takes about **9 seconds** end to end (it was
about 50) and the log line `Portal read in … s (no browser)` shows it; `/check`
reports the time too.

### Layout watchdog

A changed page rarely raises an error — it just reads as *less*. After every check
`watchdog.py` compares what was read with what was known, course by course: a course that
suddenly reads far fewer items (under half of at least 4), page text that is no longer
readable, forums that can't be read, or an empty course list. Such a course is treated as
unreadable for that run — its known items are kept, so nothing goes missing and nothing is
announced twice later — and you get **🧩 Moodle looks different** (repeated at most every
6 hours), then **✅ Moodle reads normally again**. A shortfall that lasts 3 days is taken
as real (the instructor removed the items) and said so. An empty course list fails the run
and leaves the state untouched.

### Alerts are recorded as they are sent

Every ID — a new item, a forum post, a file update, an edit, a deadline reminder — is
written to `saved_items.json` the moment its Telegram message is delivered, not at the
end of the run. A run that dies half-way (killed by `/check`'s time limit, the PC
going to sleep, a second run taking over a stale lock) therefore never makes the next
cycle announce the same things again. Saving state also refreshes the run lock, so a
long run that keeps delivering is never mistaken for a dead one.

### Day separators & daily recap

The first automatic message of each calendar day comes with a date line above it —
`━━━━━━━━ 📅 Tuesday, 13 Oct 2026 ━━━━━━━━` — sent silently, so the phone buzzes
only for the message itself. Replies to commands never get one.

At **11:59 PM**, if the day was checked and nothing new turned up on any course (no
item, file, forum post, file update or edit), one line closes it:
`✨ Daily Recap: No new files, assignments, or announcements posted today across your
courses.` Busy days get no recap. If the PC was off or asleep at 11:59 PM, the first
check after midnight sends the previous day's recap instead — unless that check finds
new items itself (they may have been posted the evening before). A day without a single
successful check gets no recap. The bot and the monitor share claim files in
`daily_sent/`, so neither message is ever sent twice; the day record lives in
`saved_items.json` under `days` (`daily.py`).

### Heartbeat (healthchecks.io)

`monitor.py` pings your [healthchecks.io](https://healthchecks.io) check, so you
hear about what the bot itself can't tell you: the PC off or asleep, the
scheduled task stopped, a hung run, or the session failing for hours.

1. Create a check there. Suggested settings: **Period 15 minutes** (the
   schedule) and **Grace time 1 hour 45 minutes**, so an alert comes after about
   2 hours without a successful check. Add Telegram / e-mail as its integration.
2. Put its ping URL in `.env` as `HEALTHCHECK_URL=https://hc-ping.com/<uuid>`
   (leave it unset to disable). Treat the URL as a secret; it is never logged.

Each run sends `/start`, then a success ping when it finishes cleanly. A failed
run only adds an entry to the check's log and does **not** reset the countdown,
so one network blip stays quiet while a persistent failure alerts after the
grace time. Pings never raise and never delay a run by more than a few seconds.

### Who can use the bot (`access.py`)

Only you. `ADMIN_USER_ID` in `.env` is your Telegram user id; an update is served only when it comes
from that user (`from.id`), the sender is not a bot, and the conversation is your private chat with
that same id. Everyone else — a stranger who found the bot, a group it was added to, a forwarded or
channel post — is **dropped silently**: no reply, no button answer, nothing that shows the bot exists.
You are told when an unknown id tries (the id, the username and name Telegram shows, never what was
typed): once per id per day, at most 5 a day in all. With no valid `ADMIN_USER_ID` nobody is served and
the bot refuses to start (it falls back to `TELEGRAM_CHAT_ID`, with a warning, when the line is missing).
`TELEGRAM_CHAT_ID` must be your private chat — the same number. The bot is single-user by design: there are
no roles and no other users' data.

### How the chat behaves (`telegram.py`, `bot/chat.py`)

- **One menu, always at the bottom.** A screen with buttons replaces the previous one (the old
  message is deleted — or, if Telegram will not delete it, loses its buttons). Pressing a button on
  an old screen puts the answer at the bottom and removes the old screen. Files the bot sends on
  request (the timetable) are followed by the menu again, silently. Alerts and their buttons (Join
  Meeting, Mark as Done, SMS code) are not menus and are never touched; an automatic alert that
  lands under the menu leaves it where it is until you use the bot again. Which message is the menu
  lives in `data/ui_state.json`.
- **Buttons answer at once** with the toast at the top of the chat; the same button pressed twice
  within a second is handled once. *Mark as Done* no longer waits for the portal before answering:
  after 2.5 s it says "updating…" and the outcome shows on the button (a late failure says
  "tap to retry"). Long jobs show *typing…* / *sending a document…* instead of a message.
- **Files are uploaded once.** Uploads to Telegram can be as slow as ~0.35 MB/s, so the timetable album (1.4 MB) took
  5-9 s every time. Telegram remembers a file it has received: the first upload's `file_id` is kept by the file's
  *content* (SHA-256 + size, `data/file_ids.json`) and every later send uses it — nothing is uploaded, the album
  arrives in a fraction of a second. A changed file is uploaded again; an id Telegram no longer accepts is dropped
  and the file uploaded afresh (`fileids.py`, used by `send_document` / `send_document_group`).
- **One door to Telegram.** Connections are kept alive and reused (0.28 s per call instead of 0.78 s,
  and far fewer failures), a dead pooled connection is replaced and the call repeated (never a send),
  sends to one chat are paced, a "slow down" (HTTP 429) is honoured, and every outcome is counted per
  hour in `data/telegram_stats.json` — read it to see how the connection really behaves
  (`python -c "from uninotifier import telegram; print(telegram.summary(24))"`). The long poll is 20 s
  (`BOT_POLL_TIMEOUT` in `.env` changes it); an outage is logged once when it starts and once when it ends.

### The bot's heartbeat, and being told when something breaks

The always-on bot has two loops that must keep turning — listening to Telegram and the
20-second clock (lecture and reminder alerts). Each stamps `data/bot_heartbeat.json`.

- **Locally**, every scheduled check reads that pulse. If a loop has been silent for
  10 minutes you get *"The bot isn't responding"* (at most every 6 hours while it lasts)
  and *"The bot is back"* afterwards. The first 10 minutes after Windows starts are skipped,
  because the bot has not had time to start.
- **Remotely**, a second healthchecks.io check (`BOT_HEALTHCHECK_URL`, suggested period
  **5 minutes**, grace **15 minutes**) is pinged by the bot only while both loops are
  turning — so a hung bot, or a PC that is off, is noticed even when nothing local can speak.

**The bot heals itself** (`selfheal.py`). Before alerting, the scheduled check does what you
would do by hand: it ends the stuck process (only if its command line is this project's
`bot.py`) and starts the `UniNotifier_BotListener` task — up to 3 times, 10+ minutes apart.
You hear *"I restarted it and it is working again"* when it worked, and *"isn't responding"*
only when it did not. A check the bot started itself (`/check`) never restarts the bot.

**Surviving Windows restarts** (`scripts/windows_recovery.ps1`, run elevated; `-Undo` reverses
it). Windows Update's night restarts used to silence everything until you signed in, because
the tasks only run while you are signed in. The script's lasting part is (1): Windows no
longer restarts the PC by itself while you are signed in (`NoAutoRebootWithLoggedOnUsers`;
updates install at 03:00 and you choose the restart). It can also (2) sign the account in
automatically after a restart and (3) lock the screen right after — but with Windows Hello-only
sign-in (`DevicePasswordLessBuildVersion = 2`) Windows ignores the automatic sign-in; on such
a PC keep only part (1) with `-Undo -KeepUpdatePolicy`. After a restart you must
sign in once; the checks catch up by themselves, and healthchecks.io tells you if it takes long.
Every earlier value is recorded in `data/windows_recovery.before.json`.

Bugs no longer fail silently (`failsafe.py`). A handler that raises, a background clock that
keeps failing, a check that crashes on a bug, or a launcher that cannot import its code sends
you *🚨 UniNotifier needs attention* with the error and where it happened — each kind of
problem at most once per 6 hours (and again at once if it is a different problem; the
hold-back record is `data/failsafe.json`). A button that fails still answers you with
*"Something went wrong"*, and what you typed is never written to the log.

`schedule.json` is validated every time it changes and when the bot starts: a misspelt
setting, a day that does not exist, a time like `7:00 PM`, a lecture without an end time, a
bad mode or finals dates are each named in a message. **A broken file never switches the
reminders off** — the last good copy (`data/schedule.last_good.json`) stays in force until
you fix it, and you are told when it is valid again.

### State backup and recovery

Besides the Git backup below, after every good run `saved_items.json` is copied to
`data/backups/` as `saved_items.latest.json` and one file per day (14 days kept). Writes are
atomic **and flushed to the disk**, so a power cut cannot leave half a file. If the file is
ever unreadable, the damaged one is kept in `data/backups/` as `saved_items.corrupt-…json`,
the newest good backup is put back, and you are told (with no backup the run starts a fresh
baseline and says so). An *empty* `saved_items.json` is not damage — it is how
`monitor.py --announce` is used.

### How fast a check is

A scheduled check is almost all waiting for the portal and now takes about 15 s (it was 29 s). Two things were
trimmed, measured with a profiler on a real run: the **file update check** asked the portal about every archived
file on every run (32 HEAD requests, ~10 s) — now each file is asked about **once per 2 hours**, its turn fixed by
its id and spread evenly over the runs (a lecturer re-uploading a file is therefore noticed within two hours; `/check`
asks about all of them at once; `FILE_PROBE_SECONDS` in `monitor/config.py`) — and the two **healthchecks.io pings** are
sent by a background worker beside the work, in order, and the process ends only after the last one is out.

### Cloud state backup

Optional, and never in the code repository: `data/` is git-ignored. `python -m uninotifier.gitsync --init
<url of an empty PRIVATE repository>` makes `data/` a git repository of its own, whose `.gitignore` admits
only `saved_items.json`, `gpa.json` and `reminders.json` — sessions, cookies, caches and logs never leave the
PC. After each run a change is committed (`sync: update tracker state`) and pushed. With no network (or no
remote: run `--init` without a URL for local history only) the commit stays local and a later run pushes it;
the check never fails because of git. A credential-account pin of the code repository
(`credential.https://github.com.username`) is copied, so a credential manager holding several GitHub
accounts never opens a picker in the hidden task. Without `data/.git` nothing happens.

## How it works

1. **Session check.** Reuse the cached session (`auth_state.json`) if
   present and still valid; otherwise run the SSO flow (see
   [Authentication](#authentication)).
2. **Course list.** Fetched via Moodle's
   `core_course_get_enrolled_courses_by_timeline_classification` AJAX
   call — the same one the dashboard uses.
3. **Content scan.** Each course page is visited and every content item
   (`li[data-for="cmitem"]`) is read directly from the server-rendered
   HTML — Moodle renders all sections server-side regardless of whether
   they're visually collapsed, so no "expand all" interaction is needed.
4. **Deadlines.** Assignment due dates and quiz closing times are fetched in
   one batch via `core_calendar_get_action_events_by_timesort` (the same data
   source as the dashboard's "Timeline" block — this Moodle lists a quiz there
   by its *close* event only); a quiz's opening time comes from
   `core_calendar_get_calendar_upcoming_view`. Each open assignment's page says
   whether it is submitted, each open quiz's page whether you attempted it —
   also for those without any date (kept under `undated`).
5. **Diff & notify.** Everything is compared against `saved_items.json`.
   New courses/content trigger an immediate Telegram message; tracked
   deadlines are checked against the current time and trigger a 24h and/or
   1h reminder exactly once each.
6. **Persist.** The full current state — including which reminders have
   already fired — is written back to `saved_items.json`.

## Project layout

```
UniNotifier/
├── monitor.py, bot.py        launchers — the two scheduled tasks start these (they never change)
├── src/uninotifier/          the code
│   ├── monitor/              the scheduled check: config · state · alerts · timed · items · notify · scan · run
│   ├── bot/                  the Telegram bot: chat · views · gpa_flow · reminder_flow · portal_flow ·
│   │                         guest_flow · finals_flow · checks · app
│   ├── moodle.py, httpctx.py, richtext.py, media.py, activities.py, probe.py   reading Moodle, wording messages
│   ├── archive.py, grades.py                                           the archive and marks in files
│   ├── lectures.py, lecturelinks.py, schedule.py, reminders.py, daily.py   time-based messages
│   ├── tasks.py              every open task: the Upcoming Tasks list and the noon digest
│   ├── gpa.py, gpa_ui.py, gradebook.py, transcript.py, portal.py, finals.py, guest.py   grades, GPA, the portal
│   ├── cambridge.py                                                    Cambridge One assignments (optional)
│   ├── watchdog.py, maintenance.py, healthcheck.py, gitsync.py          keeping itself honest; state backup
│   ├── failsafe.py, heartbeat.py, selfheal.py, fileio.py   fail-loud alerts, the bot's pulse, its self-restart, crash-safe writes
│   ├── telegram.py, access.py, fileids.py                 the one door to Telegram; who may use the bot; uploaded-once files
│   ├── envcheck.py, doctor.py                             the settings checked before a run; the setup check
│   ├── zuj_auth.py, mfa.py, notifier.py, guide.py, paths.py            sign-in, Telegram, /help, where files live
│   └── tools/                login_refresh · send_test_message · showcase · record_pages ·
│                             migrate_archive · tidy_archive · upload_benchmark   (python -m uninotifier.tools.<name>)
├── install.bat               the double-click installer (runs scripts/install.ps1)
├── backup_usb.bat            double-click: copies the whole project to a USB drive (runs scripts/backup_usb.ps1)
├── scripts/                  install.ps1 · backup_usb.ps1 · setup_tasks.ps1 · remove_tasks.ps1 · windows_recovery.ps1
├── tests/                    test_architecture.py; fixtures/ (a fixed tracker and timetable);
│                             recorded/ (your real pages, kept out of Git)
├── docs/REFERENCE.md         this file
├── .github/                  CI (the suite + pyflakes on Python 3.11, 3.13, 3.14 and 3.15, and the installer, on Windows),
│                             CodeQL, dependency review on pull requests (actions pinned to commits),
│                             Dependabot (weekly), issue forms with a privacy promise, a pull request template
├── requirements.txt          exact package versions (requirements-dev.txt adds pyflakes and pylint)
├── SECURITY.md, CONTRIBUTING.md   reporting a vulnerability privately; the rules the tests enforce
├── CODE_OF_CONDUCT.md, CHANGELOG.md   how people treat each other here; what changed in each version
├── .env.example, schedule.example.json   the templates — copied to .env and schedule.json (git-ignored)
├── assets/                   your timetable PDF / PNG (git-ignored, optional)
└── data/                     everything the program writes (git-ignored): saved_items.json, sessions,
                              GPA, reminders, caches, claim files, backups/, logs/ (monitor.log, bot.log);
                              optionally its own private backup repository (see Cloud state backup)
```

Every path comes from `paths.py`. `pip install --no-deps -e .` (README step 3, and the installer) makes the
package importable in editable mode, so the tools run with `python -m`; the launchers and the tests work
without it too. `requirements.txt` holds only exact pins, which Dependabot can update. `UNINOTIFIER_ENV`,
`UNINOTIFIER_DATA`, `UNINOTIFIER_SCHEDULE` and `UNINOTIFIER_ASSETS` move the settings, the state folder,
the timetable and its pictures elsewhere — the tests use them; never set them system-wide.

## Settings check and the doctor

`envcheck.py` lists every `.env` variable once: who needs it (the check needs the Moodle login and
Telegram; the bot needs Telegram), what a valid value looks like, and where it comes from. Both
launchers call `envcheck.require()` first: a missing value — or one still copied from `.env.example` —
ends the run with exit code 2 and a plain list of what to fix, written to `data/logs/` and, when the
Telegram settings themselves are fine, sent to you (once per 6 hours, through `failsafe.py`). A missing
`schedule.json` is reported the same way, with the instruction to copy `schedule.example.json`.

`python -m uninotifier.doctor` checks the whole installation — Python, `.env`, `schedule.json`, the
archive folder (written to), Chromium, the bot token and your chat (`--offline` skips Telegram, `--send`
sends a test message), the Moodle session, the state backup and the scheduled tasks — and prints one
`[ OK ]` / `[FAIL]` / `[WARN]` line each, with the fix under every failure. Exit code 0 means nothing
blocks a run; `scripts/setup_tasks.ps1` runs it before registering anything. A `schedule.json` whose
classes are all still `schedule.example.json`'s fails (it would announce lectures the student doesn't
have); a few example classes left among real ones are a warning. `schedule.json` may carry a UTF-8
byte-order mark (Notepad's "UTF-8 with BOM"): the doctor and the bot both read it.

## Scheduled tasks

`scripts/setup_tasks.ps1` (no administrator rights; `-IntervalMinutes`, `-SkipCheck`, `-NoStart`) finds
the project from its own location and uses `.venv\Scripts\pythonw.exe` (no console window). It registers,
for the signed-in user (logon type *Interactive*, *Limited*):

| Task | Trigger | Settings |
|---|---|---|
| `UniNotifier` → `monitor.py` | once, repeating every 15 minutes indefinitely | start when available (a missed run starts right after the PC wakes), only with a network, `IgnoreNew` (one run at a time; `monitor.lock` too), 15-minute time limit, allowed on battery and not stopped when unplugged |
| `UniNotifier_BotListener` → `bot.py` | at sign-in (and started at once) | no time limit, `IgnoreNew`, restarted every minute up to 10 times on failure, not stopped when idle, allowed on battery |

Running it again replaces both tasks and restarts the bot on the current code. `selfheal.py` restarts
the bot task by name, so keep the default names. `scripts/remove_tasks.ps1` stops and removes both and
touches nothing else. The tasks run while you are signed in to Windows; see *Surviving Windows restarts*
above for update restarts.

## Moving to a new PC

1. Follow the README's setup steps 1–5 (`.env` and `schedule.json` are yours to copy over by hand).
2. With a state backup repository: `git clone <your private backup repo> data` **before** the first run,
   so the tracker, GPA plan and reminders come back (then `python -m uninotifier.gitsync --init` to keep
   backing up). Without one, the first run simply records a fresh, silent baseline.
3. Sign in once with `python -m uninotifier.tools.login_refresh`, then run `scripts/setup_tasks.ps1`.

## Testing

```powershell
.venv\Scripts\python tests\test_architecture.py
```

runs the whole suite offline: no portal, no Telegram. Every state file a test writes goes
to a temporary folder and nothing reaches `data/logs/`, so running it never touches your data. The
timetable is a fixture (`tests/fixtures/schedule.json`) and the timetable pictures are stand-ins, so a
fresh clone with no `.env`, `schedule.json` or `assets/` passes exactly like a configured PC.

**Your real pages, replayed.** `python -m uninotifier.tools.record_pages` records every
answer Moodle gives a full read (with your saved session, read-only) into
`tests/recorded/` — kept out of Git. From then on the suite replays them: today's code must
read your real pages exactly as it did when they were recorded — every course, item, name,
text fingerprint, forum post, deadline, Teams link and attendance session. Record again
whenever you want the tests to check the pages as they are now. Together with the layout
watchdog (which notices the *site* changing while the bot runs), this catches a change in
the code that would read your pages differently.

## Security notes

`.env` (passwords, the bot token), `data/` (the Moodle and portal sessions, your grades, the tracker),
`schedule.json`, `assets/` and every log are git-ignored, and `test_no_personal_files_are_tracked` fails
if any of them is ever tracked; `test_no_personal_values_in_the_code` fails if your student ID or a
personal path appears in the code, the scripts or the docs. Every `.env` value is masked in logs and
alerts (`notifier.redact`). Before pushing anywhere, check that `git status` lists none of them.

## Interactive bot

`bot.py` is a separate long-polling process (the scheduled `monitor.py` is
unchanged). `/start` opens a two-line dashboard — semester, monitor status (🟢 live,
🟡 paused when the last check is over 45 min old, 🟠 last check failed) and the number of
courses — above a four-row menu:

| Row | Buttons |
|-----|---------|
| 1 | 📚 My Courses & Schedule (📋 Course List & Details · 📅 Weekly Timetable) · ⏳ Upcoming Tasks & Reminders |
| 2 | 📊 GPA & Grade Hub (🎯 Grade Needed — ⚙️ Course Adjustments inside it · 🏁 Finals Watch — 🔄 Check Grades Now and the background switch) · 📜 Academic Record & Balance (📜 Transcript · 💰 Balance · 👤 Guest Look-up) |
| 3 | 📖 System Guide |

There is no manual-check button: the 15-minute schedule and the status line cover it
(`/check` still works when typed — the Microsoft-verification alert relies on it).
📅 Weekly Timetable sends just the PDF and the PNG preview.

### Upcoming Tasks & Reminders

The planner (`/tasks`) lists every open task (`tasks.py`) — assignments not submitted,
quizzes not attempted (with their opening and closing times), Cambridge One assignments
with activities left, and those without a due date — each with course, countdown and a
**🔗 Open Task** button, then your own reminders. A task whose date has passed leaves the list.

**📋 Your Open Tasks** — every day at noon one message lists what can be worked on now,
closest first, every day until it is done; anything due within 24 hours comes first under a
loud **🚨 LAST DAY** heading. A quiz that has not opened yet is left out (its 24 h / 1 h
reminders come before it opens), and a day with nothing open gets no message. The bot sends
it on its clock; a check that runs later that day sends it if the bot could not (a claim file
in `data/daily_sent/` makes sure it goes out once). A day the PC was off is not caught up.

* **📌 Add to Reminders** sits under every notification except assignments and quizzes
  (those are tracked here by themselves): announcements, forum posts, course pages, files,
  links, syllabi, Teams lectures, attendance, re-uploaded files and edited content. A forum
  is announced itself when its own description carries an announcement (instructors write
  them straight into a forum's intro); an empty forum stays silent and only its posts are sent. Tapping it offers **🏫 Next Campus Day** (an hour before
  your first in-person class of the next campus day — Sunday to Wednesday only; Thursday to
  Saturday, weekend labs included, never count), **⏰ Before Next Lecture** (30 minutes before
  that course's next *in-person* lecture — online sessions and activity slots are ignored; an
  online-only course uses your next class on campus) or **🗓️ Custom Date & Time**.
* **➕ New Custom Reminder** (or `/remind`): type a short note, then pick a day (Today,
  Tomorrow, Next Sunday, the rest of the week) and an hour — or type a time like `18:45`.
* At the exact minute the bot sends `⏰ Reminder` with **⏰ Snooze 1 h** and **✅ Done**
  (and 🌐 Open on Moodle when it came from a post). A reminder pinned from a message
  remembers that message: it is delivered as a **reply quoting it**, and in Upcoming Tasks
  **🔗 View Original Post** sends a reply to it — tap the quote and Telegram jumps straight
  to the announcement and its screenshot (private chats have no message links). If that
  message was deleted, the Moodle link is offered instead. The scheduled monitor sends due reminders
  too when the bot is down; a claim file makes sure each goes out once.
* 🗑️ Manage Reminders deletes them. State: `reminders.json` (bot), `reminder_sources.json`
  (what each 📌 refers to, written by the monitor) — both git-ignored.

`/help` opens the **System Guide** (`guide.py`), one section per hub: 📚 Schedule & Moodle
Monitoring, ⏳ Tasks & Custom Reminders Engine, 📊 Smart GPA, Target Allocation & Finals
Watch, 📜 Academic Records & Financials, ⚙️ Architecture, Healthchecks & Stealth Engine —
each ONE self-contained message in plain English (no sub-pages, no ◀️ ▶️ carousel).
Commands are left to Telegram's own / menu. Pages are edited in place; every one has 🔙 Back to Hub.

The 📜 Transcript is an executive summary — 🎓 Cumulative GPA (with the portal's own rounded
figure), ⏱️ Passed Hours (with ⏳ in progress · ❌ to repeat · 🔁 retaken), 📚 Total Attempted Hours and
📌 Academic Status (the portal's "الحالة", e.g. منتظم → Regular / Good Standing) — followed by the semester
blocks: the term in progress first (⏳), then each semester with its average and cumulative, every course
marked ✅ passed · ❌ failed · 🔁 replaced by a later retake, with mark and hours (`transcript.py`; an Arabic
course name is Unicode-isolated so it cannot reorder the mark beside it). Behind it, each
reading silently re-syncs the Grade Needed base (the record before this term: every
attempted hour, failures at 35) and this term's **retakes**: a course already attempted
(same code or name) is flagged with its old recorded grade, so its new mark replaces the
old one in the weighted sum and its hours count once. A retake set by hand is left alone.

Commands: `/start`, `/help`, `/courses`, `/tasks`, `/remind`, `/schedule`, `/need`
(Grade Needed), `/gpa` (GPA & Grade Hub), `/record`
(Academic Record & Balance), `/grades`, `/transcript`, `/balance` (portal, on demand),
`/finals`. Only `TELEGRAM_CHAT_ID` is answered. `/check` launches `monitor.py`, which holds `monitor.lock`, so a
manual check and the scheduled task never overlap.

Start it without a console window:
`.venv\Scripts\pythonw.exe bot.py` (the "UniNotifier_BotListener" task does it at logon).

Submitted assignments are detected from the portal's submission-status
table; they are marked `✅ Submitted` and their 24h / 1h reminders stop.

Instructor names come from each course's participants page (refreshed weekly). Courses that
don't list a teacher can be filled in by hand in `schedule.json`:
`"teachers": {"Numerical Analysis (1)": "Dr. Name"}`. `/schedule` sends
`assets/Weekly_Academic_Schedule.pdf` and `assets/Weekly_Schedule.png` together as one Telegram album. Lab slots use an optional `"kind": "Lab"` field.
