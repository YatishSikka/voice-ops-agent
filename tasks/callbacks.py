"""Delivering a finished task to your phone.

The agent ends the turn when it hands off slow work, so the result has to reach
you somewhere other than the browser tab you already closed. Telegram is that
somewhere: free, no per-message cost, and it takes voice notes, which keeps the
project's voice-first premise intact past the end of the conversation.

Audio format is the wrinkle. Telegram's `sendVoice` wants OGG/Opus, Groq
returns WAV, and transcoding would mean shipping ffmpeg. Rather than carry that
dependency for a nicety, delivery degrades: try a voice note, fall back to an
audio file, fall back to a document. **The text is always sent first**, so a
failure anywhere in that chain costs you the audio, never the answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from agent.tts import ResilientTTS, build_tts
from config import config

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Telegram caps caption/message length; leave room rather than get a 400.
MAX_MESSAGE_CHARS = 3500


class TelegramError(RuntimeError):
    """Delivery failed."""


@dataclass
class Delivery:
    """What actually got through, for logging and tests."""

    text_sent: bool = False
    audio_method: str | None = None  # sendVoice | sendAudio | sendDocument | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.text_sent


class TelegramNotifier:
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        tts: ResilientTTS | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.token = token or config.telegram_bot_token
        self.chat_id = chat_id or config.telegram_chat_id
        self._tts = tts
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT)
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def tts(self) -> ResilientTTS:
        if self._tts is None:
            self._tts = build_tts()
        return self._tts

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def notify(
        self, text: str, with_voice: bool = True, chat_id: int | str | None = None
    ) -> Delivery:
        """Send a message, and its spoken form when that is possible.

        `chat_id` overrides the configured default so a finished task goes back
        to whoever asked for it rather than to one hard-coded chat.

        Never raises: a notification failure must not fail the callback that
        triggered it, or n8n will retry work that already succeeded.
        """
        chat_id = chat_id or self.chat_id
        if not (self.token and chat_id):
            log.info("Telegram not configured; notification dropped: %s", text[:80])
            return Delivery(error="TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset")

        delivery = Delivery()
        try:
            self._post("sendMessage", {"chat_id": chat_id, "text": text[:MAX_MESSAGE_CHARS]})
            delivery.text_sent = True
        except (httpx.HTTPError, TelegramError) as exc:
            delivery.error = str(exc)
            log.warning("Telegram text delivery failed: %s", exc)
            return delivery

        if with_voice:
            delivery.audio_method = self._try_voice(text, chat_id)
        return delivery

    def _try_voice(self, text: str, chat_id: int | str) -> str | None:
        """Best-effort spoken delivery. Returns the method that worked."""
        speech = self.tts.synthesize(text)
        if speech.audio is None:
            return None  # browser-only TTS has no bytes to send

        # Most Telegram-native first, most tolerant last.
        attempts = (
            ("sendVoice", "voice", "reply.ogg"),
            ("sendAudio", "audio", "reply.wav"),
            ("sendDocument", "document", "reply.wav"),
        )
        for method, field_name, filename in attempts:
            try:
                self._post(
                    method,
                    data={"chat_id": chat_id},
                    files={field_name: (filename, speech.audio, "audio/wav")},
                )
                return method
            except (httpx.HTTPError, TelegramError) as exc:
                log.debug("Telegram %s failed: %s", method, exc)
        log.warning("All Telegram audio methods failed; text was still delivered")
        return None

    def _post(
        self,
        method: str,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        response = self._client.post(url, json=json_body, data=data, files=files)
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: non-JSON response") from exc
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', response.status_code)}")
        return body


def format_completion(tool: str, result: Any, error: str | None, duration_s: float) -> str:
    """Turn a finished task into something worth reading on a phone."""
    minutes, seconds = divmod(int(duration_s), 60)
    took = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    if error:
        return f"Your {tool.replace('_', ' ')} task failed after {took}: {error}"

    summary = result
    if isinstance(result, dict):
        # Workflows usually wrap the interesting part; unwrap the obvious cases.
        for key in ("message", "summary", "result", "text"):
            if isinstance(result.get(key), str):
                summary = result[key]
                break
        else:
            summary = ", ".join(f"{k}: {v}" for k, v in list(result.items())[:5])
    elif isinstance(result, list):
        summary = "; ".join(str(item) for item in result[:5])

    body = str(summary).strip() if summary not in (None, "") else "It finished with no output."
    return f"Your {tool.replace('_', ' ')} task finished in {took}. {body}"
