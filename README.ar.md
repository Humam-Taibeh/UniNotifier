**العربية** | [English](README.md)

# UniNotifier

**جامعتك على هاتفك — لحظة حدوث أي تغيير.**

يراقب UniNotifier منصة Moodle الخاصة بجامعة الزيتونة الأردنية (`elearning.zuj.edu.jo`) من جهازك
الشخصي الذي يعمل بنظام Windows، ويرسل كل ما يهمك إلى بوت تيليجرام خاص بك وحدك: الملفات الجديدة،
والواجبات، والاختبارات القصيرة (Quizzes)، والإعلانات ومنشورات المنتديات، وتذكيرات مواعيد التسليم،
وتنبيهات المحاضرات الإلكترونية مع زر **Join** للانضمام، وكشف العلامات والرصيد المالي عند الطلب،
وأداة لتخطيط المعدل. كما يحفظ كل ملف من ملفات المواد في مجلد مرتّب خاص بكل مادة.

يعمل بهدوء في الخلفية عبر مهمتين مجدولتين في Windows، ولا يحتاج إلى خادم ولا إلى اشتراك، ولا يغادر
جهازك أي شيء يخصك سوى الرسائل التي يرسلها إلى بوتك *أنت*.

- [المزايا](#المزايا) · [بنية النظام](#بنية-النظام) · [الإعداد في 6 خطوات](#الإعداد-في-6-خطوات)
- [الاستخدام اليومي](#الاستخدام-اليومي) · [أين تحفظ الملفات](#أين-تحفظ-الملفات) · [إضافات اختيارية](#إضافات-اختيارية)
- [التحديث والإيقاف المؤقت والإزالة](#التحديث-والإيقاف-المؤقت-والإزالة) · [حل المشكلات](#حل-المشكلات) · [الأمان والخصوصية](#الأمان-والخصوصية)
- كل سلوك بالتفصيل: **[docs/REFERENCE.md](docs/REFERENCE.md)** (بالإنجليزية)

---

## المزايا

| | |
|---|---|
| 📚 **كل ما على Moodle لحظة نشره** | الملفات والمجلدات والروابط والصفحات والملصقات (Labels) والاختبارات القصيرة والواجبات ومنشورات المنتديات والإعلانات ومحاضرات Teams وتسجيل الحضور — كلٌّ منها بنصه الكامل، مع لقطة شاشة واضحة، والملف نفسه (حتى 45 ميغابايت). ويُبلغك أيضاً عند تعديل نص أو إعادة رفع ملف. |
| 🗂️ **أرشيف مرتّب** | يُحفظ كل ملف في `<Course>\01_Lectures` أو `02_Assignments` أو `03_Exams`، ولا يُحفظ مرتين أبداً؛ وعندما يرفع المدرّس نسخة جديدة تحلّ محلّ القديمة، وتنتقل القديمة إلى `_Old_Versions`. |
| ⏰ **مواعيد التسليم والمحاضرات** | تذكير بالواجبات والاختبارات القصيرة قبل موعدها بـ 24 ساعة وبساعة واحدة (ويتوقف عند تسليمك)؛ وتنبيهات المحاضرات الإلكترونية قبل ساعة وعند بدايتها، مع زرَّي 🎥 Join Meeting و ✍️ Check In. |
| 📌 **تذكيراتك الخاصة** | زر "Add to Reminders" تحت أي إشعار، أو ملاحظة تكتبها بنفسك — تصلك في الدقيقة المحددة تماماً. |
| 📊 **العلامات والمعدل** | أداة Grade Needed لحساب ما تحتاجه لبلوغ معدل فصلي أو تراكمي مستهدف، وكشف العلامات والرصيد المالي عند الطلب، وميزة Finals Watch التي تُعلن كل علامة نهائية فور رصدها. |
| 🛡️ **هادئ ويُصلح نفسه بنفسه** | فحص عبر HTTP مباشرة يستغرق نحو 10 ثوانٍ كل 15 دقيقة، ونظرة خفيفة جداً كل دقيقتين، وإعادة محاولة تلقائية، واكتشاف أعمال الصيانة على المنصة، وتمرير التحقق الثنائي من Microsoft إليك عبر تيليجرام، وبوت يعيد تشغيل نفسه، وتنبيهات حين يحتاج الأمر إلى تدخلك. |

---

## بنية النظام

```
  SOURCES (read only)       YOUR WINDOWS PC                                                                YOU

+----------------------+    +------------------------------------------------+
| Moodle e-learning    |    | monitor.py     task "UniNotifier"              |
| elearning.zuj.edu.jo |<---| every 15 min: scan > diff > download > alert   |----+
| Microsoft SSO + 2FA  |    | sign-in, screenshots: headless Chromium        |    |
+----------------------+    +------------------------------------------------+    |
                                    |                            |                |
                                    | course files               | state          |
                                    v                            v                |    +----------------+
                            +------------------+    +------------------------+    +----> Telegram       |---> your phone:
                            | ARCHIVE_ROOT\    |    | data\  (git-ignored)   |         | Bot API        |     alerts, files
                            |  <Course>\       |    |  saved_items.json      |    +<---> (your bot)     |<--- taps, commands
                            |   01_Lectures    |    |  sessions, caches      |    |    +----------------+
                            |   02_Assignments |    |  logs\                 |    |
                            |   03_Exams       |    +------------------------+    |
                            +------------------+                 ^                |
+----------------------+                                         |                |
| Registration portal  |    +------------------------------------------------+    |
| portal.zuj.edu.jo    |<---| bot.py   task "UniNotifier_BotListener"        |    |
| transcript, balance  |    | always on: menus, reminders, lecture alerts,   |<---+
+----------------------+    | 2-min Moodle probe, /check -> monitor.py,      |
| Cambridge One        |    | portal answers: saved copy now, fresh behind,  |
| class assignments    |<---| Cambridge One every 30 min (optional)          |
| (optional)           |    +------------------------------------------------+
+----------------------+
```

المخطط بالإنجليزية لأن أسماءه هي أسماء الملفات والمهام كما تظهر على جهازك. على اليسار المصادر التي
يقرأ منها البرنامج فقط، وفي الوسط جهازك، وعلى اليمين أنت.

- **الفحص الدوري — `monitor.py`.** كل 15 دقيقة يقرأ جميع موادك (عبر HTTP مباشرة بالجلسة المحفوظة؛
  ولا يستخدم Chromium إلا لتسجيل الدخول والتقاط لقطات الشاشة)، ويقارنها بما رآه في المرة السابقة
  (`data\saved_items.json`)، ويحفظ المستندات الجديدة في أرشيفك، ويرسل لك ما هو جديد.
- **المساعد — `bot.py`.** يعمل باستمرار: يستجيب لضغطاتك وأوامرك، ويرسل تنبيهات المحاضرات والتذكيرات
  في دقيقتها، ويُلقي نظرة صغيرة جداً (نحو 1 كيلوبايت) على Moodle كل دقيقتين ويبدأ فحصاً فورياً إذا ظهر
  جديد، ولا يقرأ بوابة الطالب إلا عندما تطلب ذلك — فيجيبك فوراً من آخر نسخة محفوظة ثم يحدّثها في
  الخلفية (stale-while-revalidate) — وإن فعّلتها، يراقب منصة Cambridge One أيضاً.
- **تيليجرام** هو الشيء الوحيد الذي يغادر جهازك: بوتك الخاص، يتحدث إليك وحدك.

---

## الإعداد في 6 خطوات

نحو 20 دقيقة. جميع الأوامر أدناه تُكتب في **PowerShell** (ابحث عنه في قائمة Start)، داخل مجلد
المشروع بعد تنزيله.

### 1. المتطلبات

| تحتاج إلى | طريقة الحصول عليه | للتحقق |
|---|---|---|
| نظام Windows 10 أو 11 | — | — |
| إصدار **Python 3.11** أو أحدث | من [python.org/downloads](https://www.python.org/downloads/) — وأثناء التثبيت فعّل خيار **"Add python.exe to PATH"** | `py --version` |
| برنامج **Git** | من [git-scm.com/download/win](https://git-scm.com/download/win) (الإعدادات الافتراضية كافية) | `git --version` |
| تطبيق تيليجرام | على هاتفك | — |
| حساب Microsoft الجامعي | الحساب الذي تسجّل به الدخول إلى Moodle، وتطبيق Authenticator إن كنت تستخدمه | — |

### 2. تنزيل الكود وإنشاء البيئة الافتراضية

```powershell
cd $HOME\Documents
git clone https://github.com/<owner>/UniNotifier.git     # عنوان المستودع الذي حصلت منه على المشروع
cd UniNotifier
py -3 -m venv .venv
```

المجلد `.venv` هو بيئة افتراضية (virtual environment): نسخة خاصة من Python لهذا المشروع وحده، فلا
يُثبَّت أي شيء على مستوى النظام.

### 3. تثبيت المكتبات والمتصفح

```powershell
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

السطر الأخير ينزّل متصفح Chromium الذي يعمل دون واجهة (headless، نحو 150 ميغابايت)، ويُستخدم لتسجيل
الدخول والتقاط لقطات الشاشة.

### 4. إنشاء بوت تيليجرام الخاص بك

1. في تيليجرام افتح **[@BotFather](https://t.me/BotFather)** ← اضغط **Start** ← أرسل `/newbot`.
2. اختر اسماً يظهر للبوت (مثل *My Uni Bot*) واسم مستخدم ينتهي بـ `bot` (مثل `my_uni_2027_bot`).
3. سيرد عليك BotFather برمز (**token**) مثل `123456789:AAH4x…` — احتفظ به سراً، فهو بمثابة كلمة مرور البوت.
4. افتح **[@userinfobot](https://t.me/userinfobot)** ← **Start**. سيرد عليك برقمك التعريفي **Id**.
5. افتح **بوتك الجديد** (الرابط `t.me/<username>` الذي أعطاك إياه BotFather) واضغط **Start** — فالبوت
   لا يستطيع مراسلتك قبل أن تراسله أنت مرة واحدة على الأقل.

### 5. الإعدادات والجدول الدراسي

```powershell
copy .env.example .env
notepad .env
```

املأ القيم الأربع **المطلوبة** — `ZUJ_USERNAME` و `ZUJ_PASSWORD` و `TELEGRAM_BOT_TOKEN` و
`TELEGRAM_CHAT_ID` (رقمك التعريفي من @userinfobot) — وضع الرقم نفسه في `ADMIN_USER_ID`. كل ما عدا ذلك
اختياري ومشروح داخل الملف نفسه، والقيمة الفارغة تعني أن الميزة متوقفة. تُحفظ ملفات المواد في
`Documents\University` ما لم تحدد مساراً آخر في `ARCHIVE_ROOT` (ويمكنك استخدام `%USERPROFILE%` فيه).

```powershell
copy schedule.example.json schedule.json
notepad schedule.json
```

استبدل الحصص الواردة كمثال بجدولك الأسبوعي: لكل حصة يومها، ووقت بدايتها ونهايتها (بنظام 24 ساعة)،
واسم المادة بالإنجليزية والعربية، والشعبة، ونوعها (`In-Person` أو `Online` أو `Activity`). المحاضرات
الإلكترونية (`Online`) هي التي تصلك تنبيهاتها. ويمكنك اختيارياً وضع جدولك الرسمي في المجلد `assets\`
باسم `Weekly_Academic_Schedule.pdf` أو `Weekly_Schedule.png` (انظر [assets/README.md](assets/README.md)).

والآن دع أداة الفحص (doctor) تتحقق من كل شيء:

```powershell
.venv\Scripts\python -m uninotifier.doctor
```

```
[ OK ] Python 3.12.7
[ OK ] Settings (.env): everything required is filled in
[ OK ] Timetable: First Semester 2026/2027, 12 weekly classes
[ OK ] Course files are saved to C:\Users\you\Documents\University
[ OK ] Chromium for Playwright is installed
[ OK ] Telegram bot: @my_uni_2027_bot
[ OK ] The bot can message you
...
All set.
```

كل سطر `[FAIL]` يأتي معه سهم يخبرك بالضبط بما عليك فعله.

### 6. أول تسجيل دخول وأول فحص والتشغيل التلقائي

```powershell
# 1. سجّل الدخول إلى Moodle مرة واحدة في نافذة متصفح ظاهرة (ووافق على طلب Microsoft من هاتفك إن طُلب)
.venv\Scripts\python -m uninotifier.tools.login_refresh

# 2. الفحص الأول: يسجّل ما هو موجود على Moodle الآن بهدوء، ويرسل رسالة "monitoring activated"
.venv\Scripts\python monitor.py

# 3. رسالة تجريبية للتأكد من أن تيليجرام يعمل من البداية إلى النهاية
.venv\Scripts\python -m uninotifier.doctor --send

# 4. التشغيل التلقائي: الفحص كل 15 دقيقة والبوت الدائم، كمهام مجدولة في Windows
powershell -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1
```

يعثر السكربت `setup_tasks.ps1` على المشروع وعلى `.venv` تلقائياً، ويشغّل أداة الفحص مرة أخرى، ثم يسجّل
المهمتين في مُجدوِل المهام (Task Scheduler) لحساب Windows الخاص بك — دون الحاجة إلى صلاحيات المسؤول:

| المهمة | ما تشغّله | الإعدادات |
|---|---|---|
| `UniNotifier` | `monitor.py` كل 15 دقيقة | يعوّض الفحص الفائت فور استيقاظ الجهاز، وينتظر توفر الشبكة، ولا يشغّل أكثر من فحص في الوقت نفسه، بحد أقصى 15 دقيقة للفحص، ويعمل على البطارية |
| `UniNotifier_BotListener` | `bot.py` عند كل تسجيل دخول، وفوراً عند الإعداد | بلا حد زمني، ويُعاد تشغيله كل دقيقة إذا توقف بخطأ، ويعمل على البطارية |

افتح بوتك في تيليجرام وأرسل **/start**. هذا كل شيء — من الآن فصاعداً يعمل وحده.

> الفحص الأول لا يُغرقك بالرسائل أبداً: كل ما هو موجود على Moodle حالياً يصبح نقطة الأساس، ولا يُعلَن
> إلا عمّا يُنشر بعد ذلك.

---

## الاستخدام اليومي

كل شيء يتم بالأزرار: أرسل `/start` لتظهر القائمة. واجهة البوت بالإنجليزية، وهذه أهم الأوامر:

| الأمر | وظيفته |
|---|---|
| `/start` | لوحة التحكم والقائمة |
| `/tasks` | الواجبات والاختبارات القصيرة القادمة، وتذكيراتك |
| `/remind` | إضافة تذكير جديد |
| `/courses` · `/schedule` | موادك ومدرّسوها · ملفات جدولك الدراسي |
| `/need` · `/gpa` | حاسبة العلامة المطلوبة (Grade Needed) · مركز المعدل والعلامات |
| `/transcript` · `/balance` | كشف العلامات · الرصيد المالي، من بوابة الطالب (تحتاج قيم `PORTAL_*` في `.env`) |
| `/finals` | مراقبة العلامات النهائية (Finals Watch) في فترة الامتحانات |
| `/check` | تشغيل فحص فوري الآن |
| `/help` | دليل النظام (System Guide) |

لا يجيب البوت إلا عليك أنت (`ADMIN_USER_ID`)؛ وأي شخص آخر يعثر عليه لا يتلقى أي رد، ويصلك إشعار بذلك.

---

## أين تحفظ الملفات

| المسار | المحتوى | في Git؟ |
|---|---|---|
| `.env` | إعداداتك وكلمات المرور (من `.env.example`) | **أبداً** |
| `schedule.json` | جدولك الدراسي (من `schedule.example.json`) | **أبداً** |
| `assets\` | جدولك بصيغة PDF / PNG (اختياري) | **أبداً** |
| `data\` | كل ما يكتبه البرنامج: سجل المتابعة، وجلسات Moodle وبوابة الطالب، والملفات المؤقتة | **أبداً** |
| `data\logs\` | الملفان `monitor.log` و `bot.log` — أول مكان تبحث فيه عند أي مشكلة | **أبداً** |
| `ARCHIVE_ROOT` (افتراضياً `Documents\University`) | ملفات موادك، مجلد لكل مادة | خارج المشروع |
| `src\uninotifier\` | الكود البرمجي | نعم |
| `monitor.py`، `bot.py` | ملفا التشغيل اللذان تشغّلهما المهام المجدولة | نعم |
| `scripts\` | السكربتات `setup_tasks.ps1` و `remove_tasks.ps1` و `windows_recovery.ps1` | نعم |

يُنشأ المجلدان `data\` و `data\logs\` تلقائياً عند أول تشغيل. ويمكنك نقل مجلد المشروع بالكامل إلى مكان
آخر، ثم تشغيل `scripts\setup_tasks.ps1` مجدداً لتشير المهام إلى المكان الجديد.

---

## إضافات اختيارية

**بوابة الطالب** — اضبط `PORTAL_USERNAME` و `PORTAL_PASSWORD` لتفعيل كشف العلامات والرصيد المالي وميزة
Finals Watch. لا يتصل البرنامج بالبوابة إلا عندما تطلب ذلك (أو أثناء تفعيل Finals Watch).

**مراقبة الحالة** — حساب مجاني على [healthchecks.io](https://healthchecks.io) يُعلمك عندما يكون جهازك
مطفأً أو يتوقف البرنامج. أنشئ فحصين وضع رابطَي الـ ping الخاصين بهما في `HEALTHCHECK_URL` (الفترة
Period ‏15 دقيقة، ومهلة السماح Grace ساعة و45 دقيقة) و `BOT_HEALTHCHECK_URL` (الفترة 5 دقائق، ومهلة
السماح 15 دقيقة).

**نسخة احتياطية لحالتك خارج الجهاز** — احتفظ بنسخة من سجل المتابعة وخطة المعدل والتذكيرات في مستودع
GitHub **خاص** ومستقل (أنشئ مستودعاً خاصاً فارغاً أولاً):

```powershell
.venv\Scripts\python -m uninotifier.gitsync --init https://github.com/<you>/<private-repo>.git
```

بعد ذلك يحفظ كل فحص التغييرات على `saved_items.json` و `gpa.json` و `reminders.json` ويرفعها (commit
و push) — ولا يرفع أبداً الجلسات أو ملفات تعريف الارتباط (cookies) أو السجلات. وإن شغّلت الأمر دون رابط
فسيحتفظ بسجل محلي فقط.

**منصة Cambridge One** (لمواد اللغة الإنجليزية) — اضبط `CAMBRIDGE_ASSIGNMENTS_URL` و `CAMBRIDGE_UNTIL`،
ثم سجّل الدخول مرة واحدة بالأمر `.venv\Scripts\python -m uninotifier.cambridge --login`.

**الصمود أمام إعادة التشغيل بسبب تحديثات Windows** — تعمل المهام ما دمت مسجّل الدخول إلى Windows.
السكربت `scripts\windows_recovery.ps1` (يُشغَّل بصلاحيات المسؤول، والخيار `-Undo` يتراجع عنه) يمنع
Windows من إعادة تشغيل الجهاز تلقائياً أثناء تسجيل دخولك. التفاصيل في [docs/REFERENCE.md](docs/REFERENCE.md)
(بالإنجليزية).

---

## التحديث والإيقاف المؤقت والإزالة

```powershell
# التحديث إلى أحدث إصدار
git pull
.venv\Scripts\pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1     # يعيد تشغيل البوت على الكود الجديد

# إيقاف كل شيء مؤقتاً / استئنافه
Disable-ScheduledTask UniNotifier; Stop-ScheduledTask UniNotifier_BotListener
Enable-ScheduledTask UniNotifier; Start-ScheduledTask UniNotifier_BotListener

# إزالة المهام المجدولة (تبقى إعداداتك وبياناتك وملفات موادك كما هي)
powershell -ExecutionPolicy Bypass -File scripts\remove_tasks.ps1
```

للإزالة الكاملة: أزل المهام أولاً، ثم احذف مجلد المشروع (ومجلد الأرشيف أيضاً إن لم تعد بحاجة إلى
ملفات المواد).

---

## حل المشكلات

ابدأ دائماً بالأمر `.venv\Scripts\python -m uninotifier.doctor` — فهو يفحص كل شيء ويخبرك بما يجب فعله.
ثم راجع الملفين `data\logs\monitor.log` و `data\logs\bot.log`.

| المشكلة | الحل |
|---|---|
| رسالة *"UniNotifier can't start - some settings in .env need your attention"* | القائمة تحتها تسمّي كل إعداد يحتاج إلى تصحيح؛ صحّح `.env` ثم شغّل أداة الفحص |
| أداة الفحص تقول *"The bot can't reach your chat yet"* | افتح بوتك في تيليجرام واضغط **Start** |
| البوت لا يرد | هل المهمة `UniNotifier_BotListener` تعمل؟ (`Get-ScheduledTask UniNotifier*`) — وهل `ADMIN_USER_ID` هو رقمك التعريفي؟ |
| رسالة *"running scripts is disabled on this system"* | شغّل السكربت كما هو موضح: `powershell -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1` |
| الخطأ `No module named uninotifier` | شغّل `.venv\Scripts\pip install -r requirements.txt` **من داخل مجلد المشروع** |
| الخطأ `Executable doesn't exist … chromium` | شغّل `.venv\Scripts\python -m playwright install chromium` |
| تطلب Microsoft التحقق | يمرّر البوت الطلب إليك عبر تيليجرام — وافق عليه من هاتفك (أو أرسل رمز SMS ردّاً). الحل الأخير: `.venv\Scripts\python -m uninotifier.tools.login_refresh` |
| رسالة *"schedule.json needs fixing"* على تيليجرام | تسرد الرسالة كل خطأ، وتستمر التذكيرات باستخدام آخر نسخة سليمة حتى تصحّحه |
| لا يحدث شيء بعد إعادة تشغيل الجهاز | سجّل الدخول إلى Windows — فالمهام تعمل ما دمت مسجّل الدخول، وتعوّض الفحوص الفائتة تلقائياً |

---

## الأمان والخصوصية

- **كلمات المرور تبقى في `.env`** على جهازك. هذا الملف مستثنى من Git، وكل قيمة فيه تُخفى في السجلات وفي
  تنبيهات الأخطاء.
- **مالك واحد فقط.** لا يخدم البوت إلا `ADMIN_USER_ID` في محادثتك الخاصة، ويتجاهل بصمت الغرباء
  والمجموعات والرسائل المُعاد توجيهها.
- **لا يُتتبَّع أي ملف شخصي في Git.** الملفات `.env` و `schedule.json` و `assets\` و `data\` والسجلات كلها
  مستثناة من Git، ويفشل أحد الاختبارات إن أُضيف أيٌّ منها. وقبل تنفيذ `git push` يجب ألا يظهر أي منها في
  `git status`.
- **قراءة فقط على Moodle**، باستثناء علامة *Mark as done* التي تضغطها بنفسك، والعلامة التلقائية للملفات
  التي حفظها البرنامج؛ أما الواجبات والاختبارات القصيرة فلا يلمسها أبداً.

---

## للمطورين

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python tests\test_architecture.py          # جميع الاختبارات، دون اتصال: لا بوابة ولا تيليجرام
.venv\Scripts\python -m pyflakes src tests monitor.py bot.py
```

تعمل الاختبارات في مجلد مؤقت بملف `.env` وهمي وجدول دراسي ثابت مخصص للاختبار، ودون أي اتصال بالشبكة
(أي محاولة اتصال خارجي تُفشل التشغيل)، فلا تقرأ إعداداتك ولا تلمس بياناتك أبداً — وتنجح على نسخة
مستنسخة حديثاً تماماً كما تنجح على جهاز مُعَدّ. البنية وكل وحدة وكل سلوك بالتفصيل:
**[docs/REFERENCE.md](docs/REFERENCE.md)** (بالإنجليزية).
