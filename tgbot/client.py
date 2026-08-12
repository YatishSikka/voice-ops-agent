"""Telegram Bot API client -- both directions.

Telegram is the product's front door, not just its notification channel, so
this covers receiving as well as sending.

**Long polling, not webhooks.** `getUpdates` means the bot reaches out to
Telegram rather than being called, so it needs no public URL, no tunnel and no
inbound port. That is what lets the whole system run from a laptop or any free
host that can keep a process alive.

Audio out degrades on purpose: `sendVoice` wants OGG/Opus and Groq returns WAV,
so it tries voice, then audio, then document. Telegram has so far accepted the
WAV as a voice note, but the ladder means a stricter day costs the audio, never
the answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import config

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 3500

# Long-poll timeout. Telegram holds the connection open until something
# happens, so a long timeout means fewer requests, not slower replies.
POLL_TIMEOUT_S = 25


class TelegramError(RuntimeError):
    """A Telegram API call was rejected."""


@dataclass
class Incoming:
    """One inbound message, reduced to what the agent cares about."""

    update_id: int
    chat_id: int
    message_id: int
    text: str = ""
    voice_file_id: str = ""
    voice_seconds: int = 0
    sender: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_voice(self) -> bool:
        return bool(self.voice_file_id)

    @property
    def is_command(self) -> bool:
        return self.text.startswith("/")


class TelegramClient:
    def __init__(self, token: str | None = None, client: httpx.Client | None = None) -> None:
        self.token = token or config.telegram_bot_token
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is unset -- see .env.example")
        # Read timeout must outlast the long poll or every poll raises.
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(POLL_TIMEOUT_S + 20, connect=10.0)
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- receiving ----------------------------------------------------------

    def get_updates(self, offset: int | None = None, timeout: int = POLL_TIMEOUT_S) -> list[Incoming]:
        """Long-poll for new messages.

        `offset` acknowledges everything before it -- Telegram redelivers
        anything unacknowledged, so this is what stops the bot reprocessing the
        same message forever.
        """
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": '["message"]',
        }
        if offset is not None:
            params["offset"] = offset

        body = self._call("getUpdates", params=params)
        return [msg for msg in (self._parse(u) for u in body.get("result", [])) if msg]

    @staticmethod
    def _parse(update: dict[str, Any]) -> Incoming | None:
        message = update.get("message")
        if not message:
            return None
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        voice = message.get("voice") or message.get("audio") or {}
        return Incoming(
            update_id=update.get("update_id", 0),
            chat_id=chat.get("id", 0),
            message_id=message.get("message_id", 0),
            text=(message.get("text") or message.get("caption") or "").strip(),
            voice_file_id=voice.get("file_id", ""),
            voice_seconds=voice.get("duration", 0),
            sender=sender.get("username") or sender.get("first_name") or "",
            raw=update,
        )

    def download_file(self, file_id: str) -> tuple[bytes, str]:
        """Fetch a voice note. Returns (bytes, filename).

        Telegram voice notes are Opus in an Ogg container, served as `.oga`.
        Groq's Whisper accepts Ogg, so no transcoding is needed.
        """
        info = self._call("getFile", params={"file_id": file_id})
        path = (info.get("result") or {}).get("file_path")
        if not path:
            raise TelegramError(f"no file_path for {file_id}")

        response = self._client.get(f"{API_ROOT}/file/bot{self.token}/{path}")
        if response.status_code != 200:
            raise TelegramError(f"download failed: HTTP {response.status_code}")

        name = path.rsplit("/", 1)[-1] or "voice.oga"
        # Groq matches on extension; .oga is not in its list but .ogg is.
        if name.endswith(".oga"):
            name = name[:-4] + ".ogg"
        return response.content, name

    # -- sending ------------------------------------------------------------

    def send_message(self, chat_id: int | str, text: str) -> None:
        self._call("sendMessage", json_body={"chat_id": chat_id, "text": text[:MAX_MESSAGE_CHARS]})

    def send_chat_action(self, chat_id: int | str, action: str = "typing") -> None:
        """Show 'recording audio…' so a slow turn does not look like a hang."""
        try:
            self._call("sendChatAction", json_body={"chat_id": chat_id, "action": action})
        except (TelegramError, httpx.HTTPError):
            pass  # cosmetic only

    def send_voice(self, chat_id: int | str, audio: bytes, caption: str = "") -> str | None:
        """Send spoken audio, degrading through the delivery methods.

        Returns the method that worked, or None if none did.
        """
        attempts = (
            ("sendVoice", "voice", "reply.ogg"),
            ("sendAudio", "audio", "reply.wav"),
            ("sendDocument", "document", "reply.wav"),
        )
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1000]

        for method, field_name, filename in attempts:
            try:
                self._call(
                    method, data=data, files={field_name: (filename, audio, "audio/wav")}
                )
                return method
            except (TelegramError, httpx.HTTPError) as exc:
                log.debug("Telegram %s failed: %s", method, exc)
        return None

    # -- transport ----------------------------------------------------------

    def _call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{API_ROOT}/bot{self.token}/{method}"
        response = self._client.request(
            "POST" if (json_body or data or files) else "GET",
            url, params=params, json=json_body, data=data, files=files,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: non-JSON response") from exc
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', response.status_code)}")
        return body
