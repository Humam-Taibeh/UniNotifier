"""
The System Guide behind /help: one hub, five sections — one per hub of the main menu.

    📖 Hub ── 📚 Schedule & Moodle Monitoring
           ├─ ⏳ Tasks & Custom Reminders Engine
           ├─ 📊 Smart GPA, Target Allocation & Finals Watch
           ├─ 📜 Academic Records & Financials
           └─ ⚙️ Architecture, Healthchecks & Stealth Engine

Every section is ONE self-contained message — what the feature does, the rules and the triggers —
edited into the same message (callback "help" / "help:<section>") with 🔙 Back to Hub and
🏠 Main Menu under it; no sub-pages, no ◀️ ▶️ carousel. (Buttons of the earlier two-level guide
still work: they open the section they used to belong to.) Commands are left to Telegram's own /
menu. Text uses Telegram's legacy Markdown: folder and file names sit in `code` so their
underscores aren't read as italics.
"""

from dataclasses import dataclass

DIVIDER = "━━━━━━━━━━━━━━━━━━"
HUB = "help"
HUB_BUTTON = {"text": "🔙 Back to Hub", "callback_data": HUB}
MENU_BUTTON = {"text": "🏠 Main Menu", "callback_data": "menu"}


@dataclass
class Page:
    icon: str
    title: str
    lines: list[str]


PAGES: dict[str, Page] = {
    # ------------------------------------------------- 1: schedule & Moodle
    "schedule": Page("📚", "Schedule & Moodle Monitoring", [
        "*What it does*",
        "Every 15 minutes the bot signs in to Moodle (the e-learning site) in the background, reads all "
        "your courses and tells you what is new. Every 2 minutes it also takes a quick look for changes, "
        "so most news reaches you within a few minutes. When nothing changed, it stays quiet.",
        "",
        "*What counts as new*",
        "• A new course, assignment, quiz, file, folder, link, page or label.",
        "• A forum post or reply, in full with its attachments. An announcement an instructor writes "
        "into a forum's description counts too.",
        "• Text an instructor edited after it was announced.",
        "• A file the instructor uploaded again with different content.",
        "• A Teams lecture (with 🔗 Join Teams Lecture) or an attendance check-in (with ✍️ Check In).",
        "Each alert carries the full text and a screenshot. Under everything except assignments and "
        "quizzes you also get 📌 Add to Reminders (assignments and quizzes get deadline reminders anyway).",
        "",
        "*How one check works*",
        "1. It takes a lock, so only one check runs at a time.",
        "2. It reuses the saved session. A Microsoft sign-in happens only when that expires.",
        "3. It reads every course and compares with what it already knew.",
        "4. It downloads and files whatever is new, and takes the screenshots.",
        "5. Each alert is written down the moment it is sent, so a check that gets cut short never "
        "repeats one.",
        "A routine check takes about 15 seconds.",
        "",
        "*Your files*",
        "• Every file is saved in its course folder: `01_Lectures`, "
        "`02_Assignments` or `03_Exams`, chosen from the name and the "
        "activity type. `00_Schedules` is yours — the bot never writes there.",
        "• The same bytes are never saved twice. A new version takes the file's place; the old one "
        "moves to `_Old_Versions` in the same folder.",
        "• Each file is checked for a new version once every 2 hours. /check looks at all of them at once.",
        "• Files up to 45 MB are attached to the alert; bigger ones arrive as a full-page screenshot.",
        "• Files and links the bot saved are ticked ✅ Done on Moodle for you.",
        "",
        "*Your week*",
        "• 📋 Course List & Details — your courses in timetable order: grouped by the days they meet (Sun · Tue, "
        "Mon · Wed), earliest lecture first. A course that meets at the same time on both days says it once; "
        "only what differs between the days (a room here, online there) is spelled out.",
        "• 📅 Weekly Timetable — the official PDF and a quick image.",
    ]),

    # ---------------------------------------------- 2: tasks & reminders
    "tasks": Page("⏳", "Tasks & Custom Reminders Engine", [
        "*Deadlines — automatic*",
        "Every open assignment and quiz — and your Cambridge One assignments — shows up in Upcoming "
        "Tasks with a countdown and a 🔗 Open Task button, even weeks ahead or with no due date. You "
        "get a reminder 24 hours and 1 hour before it is due (a quiz: before it opens), each sent "
        "once. Once the bot sees you submitted it (or attempted the quiz), it stops.",
        "",
        "*📋 Your Open Tasks — every day at noon*",
        "One message lists everything you can work on now, closest first, every day until it is "
        "done. Anything due within a day comes first under 🚨 LAST DAY. A quiz that has not opened "
        "yet waits for its own reminders; with nothing open, there is no message.",
        "",
        "*📌 Add to Reminders* (under announcements, posts, pages, files and links)",
        "• 🏫 Next Campus Day — an hour before your first in-person class on the next campus day "
        "(Sunday to Wednesday).",
        "• ⏰ Before Next Lecture — 30 minutes before that course's next in-person lecture. Online "
        "sessions and activity slots don't count.",
        "• 🗓️ Custom Date & Time — pick a day and an hour with buttons, or type a time.",
        "",
        "*➕ Your own reminders*",
        "• Type a note, choose when, and it arrives at that exact minute with ⏰ Snooze 1 h and ✅ Done.",
        "• A reminder pinned from a post comes back as a reply quoting that post, and 🔗 View Original "
        "Post in Upcoming Tasks jumps straight to it.",
        "",
        "*🎥 Online lectures*",
        "• One hour before: a text heads-up with the course and time.",
        "• At the start: 🎥 Join Meeting, Open Course Page and a screenshot of the post that shared the link.",
        "• The bot looks at the clock every 20 seconds, so alerts land on the minute. If the bot is "
        "down, the next scheduled check sends them instead — never both.",
        "• In-person lectures and activity slots stay silent; their rooms are in Course List & Details.",
        "",
        "*✅ Automatic check-in* (switch it on in schedule.json)",
        "• Only online lectures (ك, live on Teams): never an in-person day (و, م), where your instructor "
        "marks you, nor an activity day (ن). The bot reads which days are which from the portal's own "
        "timetable, by itself, once — send /check to read it again if your courses change.",
        "• During each online lecture, the attendance page is read every minute. When your "
        "instructor opens check-in, the bot chooses Present and saves — then checks that Moodle recorded it.",
        "• ✅ Checked In comes with a screenshot of your row. If it could not be done (a password, a QR "
        "code, no Present choice, Moodle did not record it), 🚨 Check In by Hand tells you why, with the "
        "page and a button — and ✅ follows once you did.",
        "• A lecture whose check-in never opened ends with a quiet ℹ️ note.",
        "",
        "*📅 The day in the chat*",
        "• A date line appears silently above the first message of each day.",
        "• At 11:59 PM a day with nothing new ends with a ✨ Daily Recap.",
        "",
        "*Everything that can send you a message*",
        "A new course or item · edited text · a re-uploaded file · a forum post · a Teams lecture · an "
        "attendance check-in · your mark found inside a results file · a deadline 24 h or 1 h away · "
        "an online lecture an hour away and at its start · an attendance check-in done or failed · "
        "your own reminders · a problem that lasted. "
        "Nothing else.",
    ]),

    # ------------------------------------------------------------- 3: GPA
    "gpa": Page("📊", "Smart GPA, Target Allocation & Finals Watch", [
        "*🎯 Grade Needed*",
        "• Choose Semester GPA or Cumulative GPA, then a target (50, 60, 70, 75, 80, 85, 90 or your own).",
        "• Grades already published are locked 🔒. The rest of the target is shared equally over the "
        "courses still open, and each shows the final-exam mark it needs once its syllabus is entered.",
        "• ⚙️ Course Adjustments sits inside Grade Needed: hours, retakes, grades, syllabus weights "
        "and marks so far, per course.",
        "",
        "*🏁 Finals Watch*",
        "• 🔄 Check Grades Now reads the portal and shows this term's grades — published ones with "
        "their mark, the rest as ⏳ — and locks the published marks into Grade Needed.",
        "• Switch it On during finals and it checks by itself every few minutes: each released grade "
        "is announced the moment it appears, and when every course has one you get the semester "
        "average and your new cumulative GPA. It then pauses itself; Resume keeps watching for corrections.",
        "• If `schedule.json` has finals dates, it switches on by itself between them. Otherwise it is off.",
        "• If the portal asks for a verification code, it says so — tap Check Grades Now and reply "
        "with the code.",
        "",
        "*The rules (ZUJ)*",
        "• Marks are out of 100 and the pass mark is 50.",
        "• Your cumulative GPA counts every attempted course, failed ones too, weighted by credit hours.",
        "• A mark below 35 is recorded as 35.",
        "• A retaken course replaces its earlier attempt: the old mark leaves the sum and its hours "
        "count once.",
        "• Example: Calculus (3 h), first attempt 30 → recorded as 35. Retaken at 72 → the course "
        "counts once, as 72 over 3 h.",
        "",
        "*Where the numbers come from*",
        "Your cumulative base is read from your transcript — every attempted hour, as the university "
        "counts it. It is refreshed whenever you open the transcript or check grades, so there is "
        "nothing to set up.",
    ]),

    # ---------------------------------------------------------- 4: records
    "records": Page("📜", "Academic Records & Financials", [
        "*📜 Transcript*",
        "• A summary on top: cumulative GPA (with the portal's own figure when it differs), passed "
        "hours (and how many are in progress or still to repeat), attempted hours and your academic "
        "status.",
        "• Below it, every semester — the one in progress first (⏳), then the others. ✅ passed, "
        "❌ failed, 🔁 an attempt a later retake replaced; each with its mark and hours.",
        "• Reading it also refreshes the GPA base and the retakes behind Grade Needed.",
        "",
        "*💰 Balance*",
        "• Your total tuition balance and the amount per semester, in one message with a screenshot "
        "of the page.",
        "",
        "*👤 Guest Look-up*",
        "• For a friend who lets you look at their record, once. You send their student ID and "
        "password — the password message is deleted at once and kept in memory only for the sign-in. "
        "The portal's verification code goes to THEIR phone, so they read it to you.",
        "• You get the same summary and semester layout as your own transcript, with their major, "
        "and 🎯 Target GPA shows what average they need to reach a goal.",
        "• Nothing is stored and none of your own data is touched. 🚪 Sign Out ends the session and "
        "can remove every message of it from the chat.",
        "",
        "*🔒 How the portal is read*",
        "• Only when you tap. The registration portal is never polled automatically — Finals Watch "
        "and the one look at your timetable for automatic check-in, which you switch on yourself, "
        "are the two exceptions.",
        "• If it asks for a verification code, reply to the bot with it.",
        "• If the portal is down or under maintenance, you get a screenshot of what it shows.",
    ]),

    # ----------------------------------------------------------- 5: system
    "system": Page("⚙️", "Architecture, Healthchecks & Stealth Engine", [
        "*🔒 Private*",
        "• The bot answers only you. Anyone else who writes to it is ignored without a reply — they "
        "cannot even tell it exists — and you get an alert with their id.",
        "• Passwords live only in the local `.env`; the bot token is masked in every log line.",
        "",
        "*🔐 Signing in*",
        "• Checks reuse a saved session. A full Microsoft sign-in happens only when it expires.",
        "• If Microsoft asks for verification, the number to tap in Authenticator arrives here. Tap "
        "📱 Text me a code for an SMS instead, then reply with the code.",
        "• An unanswered request is retried at most once an hour and never between midnight and 7 AM, "
        "so a missed prompt can't become a stream of pushes. /check always tries at once.",
        "",
        "*🩺 When something goes wrong*",
        "• A dropped connection is retried first: each request twice, then the whole check twice.",
        "• Problems are reported only when they last: network down after 3 failed checks in a row "
        "(45 min), an unexpected error after 2, an expired session or a wrong password at once, portal "
        "maintenance at once with a screenshot. The same alert repeats at most every 6 hours, and "
        "✅ Back to normal follows.",
        "• A bug in the bot itself sends you a 🚨 message saying where. A broken `schedule.json` is "
        "named, and the last good copy stays in force.",
        "• If the bot stops responding, the next scheduled check restarts it (up to 3 times) and "
        "tells you.",
        "",
        "*💓 Watched from outside*",
        "• Every successful check pings healthchecks.io, which emails you if the checks stop. The "
        "bot has its own ping the same way.",
        "• The saved state is backed up on every run (14 days kept), and pushed to your own private backup repository when one is set up.",
        "",
        "*🕶️ Stealth*",
        "• A headless browser: no windows, no console, your PC is never interrupted.",
        "• A virtual security key answers passkey prompts, so Windows Hello never pops up.",
        "• Date lines and recaps arrive silently; only real news makes the phone buzz.",
    ]),
}

SECTIONS = ["schedule", "tasks", "gpa", "records", "system"]

# the buttons of the earlier two-level guide (sections with deep dives) open the section they belonged to
OLD_PAGES = {"schedule.check": "schedule", "schedule.archive": "schedule",
             "tasks.triggers": "tasks", "tasks.lecture": "tasks",
             "gpa.rules": "gpa", "gpa.finals": "gpa",
             "system.signin": "system", "system.failures": "system"}


def heading(page: Page) -> str:
    return f"{page.icon} *{page.title}*"


def view_hub(status: str = "") -> tuple[str, list]:
    lines = ["📖 *UniNotifier — System Guide*", DIVIDER]
    if status:
        lines += [status, ""]
    lines += ["One section per hub of the main menu — what each does and how it works.",
              "", "Choose a section 👇"]
    rows = [[{"text": f"{PAGES[k].icon} {PAGES[k].title}", "callback_data": f"help:{k}"}] for k in SECTIONS]
    rows.append([MENU_BUTTON])
    return "\n".join(lines), rows


def view(key: str, status: str = "") -> tuple[str, list]:
    """A section by key — one self-contained message; an unknown key shows the hub."""
    page = PAGES.get(OLD_PAGES.get(key, key))
    if page is None:
        return view_hub(status)
    return "\n".join([heading(page), DIVIDER, *page.lines]), [[HUB_BUTTON, MENU_BUTTON]]
