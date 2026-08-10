"""Verify Telegram delivery, and find your chat id if you have not set one.

The async callback path is only as good as its last hop, and that hop is the
one that cannot be tested without a real bot. This sends an actual message and
an actual voice note, and reports which delivery method Telegram accepted --
`sendVoice` wants OGG/Opus while Groq returns WAV, so the fallback ladder
matters and this is how you find out where you land.

    python scripts/check_telegram.py            # send a test notification
    python scripts/check_telegram.py --no-voice # text only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import config
from tasks.callbacks import TelegramNotifier

TELEGRAM_API = "https://api.telegram.org"


def discover_chat_id(token: str) -> str | None:
    """Read the chat id from whatever you last sent the bot."""
    try:
        response = httpx.get(f"{TELEGRAM_API}/bot{token}/getUpdates", timeout=20)
        updates = response.json().get("result") or []
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  could not read updates: {exc}")
        return None

    for update in reversed(updates):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            who = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
            print(f"  found chat id {chat['id']} ({who})")
            return str(chat["id"])

    print("  no messages yet -- send your bot any message, then rerun")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-voice", action="store_true", help="skip the voice note")
    args = parser.parse_args()

    token = config.telegram_bot_token
    if not token:
        print("TELEGRAM_BOT_TOKEN is unset. Create a bot with @BotFather first.")
        return 1

    print("Bot identity:")
    try:
        me = httpx.get(f"{TELEGRAM_API}/bot{token}/getMe", timeout=20).json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  FAIL: {exc}")
        return 1
    if not me.get("ok"):
        print(f"  FAIL: {me.get('description')} -- is the token right?")
        return 1
    print(f"  @{me['result'].get('username')} ({me['result'].get('first_name')})")

    chat_id = config.telegram_chat_id
    if chat_id:
        print(f"\nChat id from .env: {chat_id}")
    else:
        print("\nTELEGRAM_CHAT_ID is unset, looking it up:")
        chat_id = discover_chat_id(token)
        if not chat_id:
            return 1
        print(f"\n  Add this to .env:  TELEGRAM_CHAT_ID={chat_id}")

    print("\nSending a test notification...")
    notifier = TelegramNotifier(token=token, chat_id=chat_id)
    delivery = notifier.notify(
        "Voice-Ops Agent test: if you can read this, async task notifications work.",
        with_voice=not args.no_voice,
    )

    print(f"  text : {'delivered' if delivery.text_sent else 'FAILED'}")
    if delivery.error:
        print(f"  error: {delivery.error}")
    if not args.no_voice:
        print(f"  audio: {delivery.audio_method or 'none accepted (text still delivered)'}")
        if delivery.audio_method == "sendVoice":
            print("         -> Telegram accepted it as a true voice note")
        elif delivery.audio_method:
            print("         -> sent as a file; sendVoice needs OGG/Opus, Groq returns WAV")

    return 0 if delivery.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
