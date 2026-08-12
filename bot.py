"""Telegram voice agent -- the main entry point.

Send the bot a voice message; it transcribes you, decides which n8n-backed
tools apply, runs them, and replies with a spoken voice note. Text works too,
for when you cannot talk.

Why Telegram rather than a web page: it is already on your phone, it records
and plays voice natively, and **long polling means no inbound network** -- no
public URL, no tunnel, no hosting tier that permits web apps. The agent runs
wherever a Python process can run.

    python bot.py

Runs two things in one process: the polling loop, and a small HTTP server that
n8n calls when a background workflow finishes. That server binds locally and
never needs to be exposed to the internet.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import httpx
import uvicorn

from agent.loop import AgentLoop
from agent.stt import GroqSTT, STTError
from agent.timing import Trace
from agent.tts import build_tts
from callback_api import api
from config import config
from tgbot.client import Incoming, TelegramClient, TelegramError
from tgbot.session import is_allowed, sessions

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# httpx logs the full request URL at INFO, and the Telegram bot token lives in
# the path -- so every call would write a working credential into the log.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Telegram redelivers anything unacknowledged, so a crash loses no messages.
POLL_ERROR_BACKOFF_S = 5

HELP = (
    "Send me a voice message and I'll do what you ask.\n\n"
    "Text works too. Commands:\n"
    "/tools - what I can currently do\n"
    "/new - start a fresh conversation\n"
    "/help - this message"
)


class VoiceAgentBot:
    def __init__(self) -> None:
        self.telegram = TelegramClient()
        self.stt = GroqSTT()
        self.tts = build_tts()
        self.agent = AgentLoop()
        self._offset: int | None = None

    # -- the loop -----------------------------------------------------------

    def run_forever(self) -> None:
        me = self.telegram._call("getMe")["result"]
        log.info("Listening as @%s", me.get("username"))

        while True:
            try:
                updates = self.telegram.get_updates(offset=self._offset)
            except (TelegramError, httpx.HTTPError) as exc:
                # Network blips are expected on a long poll; keep going.
                log.warning("Poll failed (%s); retrying in %ds", exc, POLL_ERROR_BACKOFF_S)
                time.sleep(POLL_ERROR_BACKOFF_S)
                continue

            for message in updates:
                # Acknowledge before handling: a message that crashes the
                # handler must not be redelivered forever.
                self._offset = message.update_id + 1
                try:
                    self.handle(message)
                except Exception:  # one bad message must not stop the bot
                    log.exception("Failed handling update %s", message.update_id)
                    self._say(message.chat_id, "Something went wrong handling that, sorry.")

    # -- one message --------------------------------------------------------

    def handle(self, message: Incoming) -> None:
        if not is_allowed(message.chat_id):
            # Say something rather than time out, but nothing about what the
            # bot does or who owns it.
            log.warning(
                "Refused chat %s (@%s)", message.chat_id, message.sender or "?"
            )
            return self._say(message.chat_id, "This assistant is private.")

        if message.is_command:
            return self.handle_command(message)

        trace = Trace("turn")
        if message.is_voice:
            self.telegram.send_chat_action(message.chat_id, "typing")
            try:
                text = self.transcribe(message, trace)
            except STTError as exc:
                log.warning("Transcription failed: %s", exc)
                return self._say(message.chat_id, "I couldn't make out that audio, sorry.")
            if not text:
                return self._say(message.chat_id, "That sounded empty -- try again?")
            # Echo what was heard: without it, a misheard request looks like a
            # stupid agent rather than a bad transcription.
            self.telegram.send_message(message.chat_id, f'Heard: "{text}"')
        else:
            text = message.text
            if not text:
                return None

        self.telegram.send_chat_action(message.chat_id, "record_voice")
        session = sessions.get(message.chat_id)
        turn = self.agent.run(
            text, history=session.history, trace=trace, chat_id=message.chat_id
        )
        sessions.update(
            message.chat_id, turn.messages + [{"role": "assistant", "content": turn.reply}]
        )

        self.reply(message.chat_id, turn.reply, trace)
        log.info(
            "chat %s | %s | tools: %s",
            message.chat_id, trace.summary(), turn.tools_used or "none",
        )
        return None

    def transcribe(self, message: Incoming, trace: Trace) -> str:
        audio, filename = self.telegram.download_file(message.voice_file_id)
        log.info("voice: %ds, %.1f KiB", message.voice_seconds, len(audio) / 1024)
        # Telegram sends Ogg/Opus, which Groq's Whisper accepts as-is.
        return self.stt.transcribe(audio, trace=trace, filename=filename).text

    def reply(self, chat_id: int, text: str, trace: Trace | None = None) -> None:
        """Speak the reply, and send the text alongside it.

        Text always goes out: a voice note you cannot replay in a noisy room is
        worse than a message you can read.
        """
        speech = self.tts.synthesize(text, trace=trace)
        if speech.audio is None:
            self.telegram.send_message(chat_id, text)
            return
        method = self.telegram.send_voice(chat_id, speech.audio, caption=text)
        if method is None:
            self.telegram.send_message(chat_id, text)

    # -- commands -----------------------------------------------------------

    def handle_command(self, message: Incoming) -> None:
        command = message.text.split()[0].lstrip("/").split("@")[0].lower()

        if command in ("start", "help"):
            return self.telegram.send_message(message.chat_id, HELP)

        if command == "new":
            sessions.reset(message.chat_id)
            return self.telegram.send_message(message.chat_id, "Fresh start. What do you need?")

        if command == "tools":
            return self.telegram.send_message(message.chat_id, self.describe_tools())

        return self.telegram.send_message(message.chat_id, f"Unknown command /{command}.\n\n{HELP}")

    def describe_tools(self) -> str:
        """What the registry can see right now, including what it skipped."""
        try:
            discovery = self.agent.registry.discover(force=True)
        except Exception as exc:  # noqa: BLE001 -- shown to the user, not swallowed
            return f"Could not reach n8n: {exc}"

        if not discovery.bindings and not discovery.skipped:
            return f"No workflows tagged '{config.n8n_tool_tag}' in n8n yet."

        lines = [f"{len(discovery.bindings)} tool(s):"]
        for name, binding in sorted(discovery.bindings.items()):
            marks = []
            if binding.is_async:
                marks.append("background")
            if binding.is_destructive:
                marks.append("asks first")
            suffix = f" [{', '.join(marks)}]" if marks else ""
            lines.append(f"• {name}{suffix}\n  {binding.spec.description[:150]}")

        if discovery.skipped:
            lines.append(f"\n{len(discovery.skipped)} skipped:")
            lines += [f"• {s.workflow_name}: {s.reason}" for s in discovery.skipped]
        return "\n".join(lines)

    def _say(self, chat_id: int, text: str) -> None:
        try:
            self.telegram.send_message(chat_id, text)
        except (TelegramError, httpx.HTTPError) as exc:
            log.warning("Could not send to %s: %s", chat_id, exc)


def serve_callbacks() -> None:
    """n8n posts here when a background workflow finishes."""
    port = int(os.environ.get("CALLBACK_PORT", "7860"))
    uvicorn.run(api, host="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1",
                port=port, log_level="warning")


def main() -> int:
    if not config.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is unset. Create a bot with @BotFather, then see .env.example.")
        return 1

    threading.Thread(target=serve_callbacks, daemon=True).start()
    VoiceAgentBot().run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
