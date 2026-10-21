"""
Measures what the upload send buffer (telegram.UPLOAD_SEND_BUFFER) does on THIS connection.

    python -m uninotifier.tools.upload_benchmark [--size-mb 4] [--rounds 8] [--buffer 1048576]

It SENDS real documents to your own chat — silently, and each is deleted right after it
arrived — alternating the system's default send buffer (A) and the candidate (B), so a change
in the network during the run affects both alike. Prints each upload and the median / p90 of
both. Files are random bytes (nothing to compress), made in memory; nothing is written to disk.
"""

import argparse
import os
import statistics
import sys
import time

import requests

from uninotifier import notifier
from uninotifier import telegram
from uninotifier.bot import chat


def upload_once(size: int, send_buffer: int) -> tuple[float, bool]:
    """One upload with the given send buffer (0 = default): (seconds, delivered). Each upload gets a
    connection of its own, made the same way for both variants (the same handshake): only the send
    buffer differs. The clean-up (deleteMessage) goes through the bot's usual pool."""
    payload = os.urandom(size)
    session = requests.Session()
    session.mount("https://", telegram._Adapter(pool_connections=1, pool_maxsize=1, max_retries=0,
                                                send_buffer=send_buffer, upload=True))
    start = time.perf_counter()
    try:
        resp = session.post(
            f"{telegram.API}/bot{chat.BOT_TOKEN}/sendDocument",
            data={"chat_id": chat.CHAT_ID, "disable_notification": True, "caption": "upload benchmark (deleted at once)"},
            files={"document": ("benchmark.bin", payload)}, timeout=(telegram.CONNECT_TIMEOUT, 300))
    finally:
        session.close()
    seconds = time.perf_counter() - start
    try:
        message_id = resp.json()["result"]["message_id"]
        telegram.request(chat.BOT_TOKEN, "deleteMessage", json={"chat_id": chat.CHAT_ID, "message_id": message_id})
    except (KeyError, ValueError, TypeError):
        return seconds, False
    return seconds, True


def describe(name: str, seconds: list[float], size: int) -> str:
    ordered = sorted(seconds)
    p90 = ordered[max(0, round(len(ordered) * 0.9) - 1)]
    median = statistics.median(ordered)
    return (f"{name}: median {median:.1f} s ({size / median / 1e6:.2f} MB/s), "
            f"p90 {p90:.1f} s ({size / p90 / 1e6:.2f} MB/s), n={len(ordered)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size-mb", type=float, default=4)
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--buffer", type=int, default=1 << 20, help="the candidate send buffer in bytes")
    args = parser.parse_args()
    if not (chat.BOT_TOKEN and chat.CHAT_ID):
        print("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env")
        return 1
    size = int(args.size_mb * 1e6)
    results: dict[str, list[float]] = {"A": [], "B": []}
    failed = 0
    for n in range(args.rounds):
        for name, send_buffer in (("A", 0), ("B", args.buffer)) if n % 2 == 0 else (("B", args.buffer), ("A", 0)):
            try:
                seconds, ok = upload_once(size, send_buffer)
            except Exception as e:                 # a failed upload is reported, not timed
                print(f"  round {n + 1} {name}: failed ({notifier.redact(repr(e))[:160]})")
                failed += 1
                continue
            results[name].append(seconds)
            print(f"  round {n + 1} {name}: {seconds:.1f} s{'' if ok else ' (not confirmed)'}")
            sys.stdout.flush()
    print(f"\nsize {args.size_mb:g} MB, candidate buffer {args.buffer} bytes, {failed} failed")
    for name, label in (("A", "A default buffer"), ("B", f"B {args.buffer // 1024} KB buffer")):
        if results[name]:
            print(describe(label, results[name], size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
