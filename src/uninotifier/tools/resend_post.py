"""
Sends the notification of one forum discussion again — the full card with its picture and its
📌 Add to Reminders button — for a post whose notification was missed or whose reminder was not made.

Run:  .venv\\Scripts\\python -m uninotifier.tools.resend_post "https://elearning.zuj.edu.jo/mod/forum/discuss.php?d=5464"
"""

import json
import re
import sys

from dotenv import load_dotenv

from uninotifier import moodle, paths
from uninotifier.httpctx import HttpContext
from uninotifier.monitor import items, notify, scan, state


def main(url: str) -> int:
    load_dotenv(paths.ENV_FILE)
    match = re.search(r"discuss\.php\?d=(\d+)", url)
    if not match:
        print("Send a discussion link like .../mod/forum/discuss.php?d=5464")
        return 2
    http = HttpContext.from_state_file(state.SESSION_FILE)
    sesskey = http.sesskey() if http else None
    if not sesskey:
        print("The saved portal session has expired — let the bot sign in first.")
        return 1
    page_html = moodle.fetch_html(http, f"{moodle.BASE_URL}/mod/forum/discuss.php?d={match.group(1)}") or ""
    forum_ids = set(re.findall(r"mod/forum/view\.php\?id=(\d+)", page_html))
    saved = json.loads((paths.DATA / "saved_items.json").read_text(encoding="utf-8"))
    forum = next((it for it in saved.get("content", {}).values()
                  if it.get("type") == "forum" and it.get("url", "").rpartition("id=")[2] in forum_ids), None)
    if not forum:
        print("I couldn't tell which course forum that discussion belongs to.")
        return 1
    result = moodle.ajax_call(http, sesskey, "mod_forum_get_discussion_posts",
                              {"discussionid": int(match.group(1)), "sortby": "created", "sortdirection": "ASC"})
    posts = [moodle._post(p, forum["url"]) for p in (result.get("data") or {}).get("posts", [])]
    if not posts:
        print("That discussion has no posts I can read.")
        return 1
    browser = scan.Browser()
    try:
        browser.sign_in()
        for post in posts:
            if post["is_reply"]:
                continue
            notify.download_attachments(http, post)
            post["_shots"], _ = items.capture_screens(browser.page, post["url"], f'#p{post["id"]}')
            print("sent" if notify.notify_forum_post(forum, post) else "NOT sent", ascii(post["subject"]))
    finally:
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
