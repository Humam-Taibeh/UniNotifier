"""Sends a one-off test message to verify the Telegram credentials in .env."""
import os

from dotenv import load_dotenv

from uninotifier import notifier
from uninotifier import paths


def main() -> None:
    load_dotenv(paths.ENV_FILE)
    ok = notifier.send_message(
        os.getenv("TELEGRAM_BOT_TOKEN"),
        os.getenv("TELEGRAM_CHAT_ID"),
        "🚀 *Test successful*\n\nUniNotifier is connected and running.",
        "https://elearning.zuj.edu.jo/my/",
    )
    print("✅ Message sent" if ok else "❌ Send failed — check .env and the log")


if __name__ == "__main__":
    main()
