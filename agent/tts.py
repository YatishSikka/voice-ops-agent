"""Text to speech, with a fallback that survives Groq's preview gating.

Groq's TTS models sit behind a terms-acceptance gate and preview status, so the
plan treats server-side audio as the nice path and the browser's own
`speechSynthesis` as the guaranteed one. Both satisfy the same interface, and
`ResilientTTS` picks between them at runtime:

  * a **permanent** condition (403 terms gate, 404 unknown model, 401) degrades
    the engine for the rest of the process -- retrying it every turn would add
    a wasted round trip to every single reply;
  * a **temporary** condition (429, 5xx, timeout) degrades only that turn.

Either way `synthesize()` returns a Speech object rather than raising. A voice
agent that goes mute because TTS failed is a worse outcome than one that speaks
through the browser, and the caller still gets `engine` to display.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import config

from ._transport import TransportError, request_with_retry
from .timing import Trace

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Statuses that will still be true on the next turn.
PERMANENT_STATUS = (400, 401, 403, 404)


class TTSError(TransportError):
    """Synthesis failed."""


@dataclass
class Speech:
    """One spoken reply.

    `audio` is None when the browser must speak `text` itself -- the Gradio UI
    checks exactly this to decide between an audio player and a
    `speechSynthesis` call.
    """

    text: str
    audio: bytes | None = None
    mime: str = "audio/wav"
    engine: str = "browser"
    latency_ms: float = 0.0
    detail: str = ""

    @property
    def is_browser_fallback(self) -> bool:
        return self.audio is None

    def to_file(self, directory: str | Path | None = None) -> str | None:
        """Write the audio somewhere `gr.Audio` can load it.

        Spaces have no persistent disk, so this is deliberately a temp file --
        the audio is disposable once the turn is over.
        """
        if self.audio is None:
            return None
        target = Path(directory or tempfile.gettempdir())
        target.mkdir(parents=True, exist_ok=True)
        suffix = ".wav" if self.mime.endswith("wav") else ".mp3"
        path = target / f"reply-{uuid.uuid4().hex[:12]}{suffix}"
        path.write_bytes(self.audio)
        return str(path)


class BrowserTTS:
    """The always-available engine: hand the text back and let the client speak.

    Costs nothing, needs no key, and cannot be rate limited -- which is exactly
    why it is the floor under the voice loop.
    """

    name = "browser"

    def synthesize(self, text: str, *, trace: Trace | None = None) -> Speech:
        return Speech(text=text, audio=None, engine=self.name)

    def close(self) -> None:  # symmetry with GroqTTS
        return None


class GroqTTS:
    name = "groq"

    def __init__(
        self,
        model: str | None = None,
        voice: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not config.groq_api_key:
            raise TTSError("GROQ_API_KEY is unset -- see .env.example")
        self.model = model or config.tts_model
        self.voice = voice or config.tts_voice
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT)
        self._owns_client = client is None
        self._url = f"{config.groq_base_url}/audio/speech"
        self._headers = {"Authorization": f"Bearer {config.groq_api_key}"}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def synthesize(self, text: str, *, trace: Trace | None = None) -> Speech:
        """Raises TTSError -- ResilientTTS is what turns that into a fallback."""
        body = {
            "model": self.model,
            "voice": self.voice,
            "input": text,
            "response_format": "wav",
        }
        if trace is not None:
            with trace.span("tts", engine=self.name, model=self.model) as record:
                response = self._post(body)
                record.meta["bytes"] = len(response.content)
            latency = record.duration_ms  # set when the span closed
        else:
            response = self._post(body)
            latency = 0.0

        return Speech(
            text=text,
            audio=response.content,
            mime=response.headers.get("content-type", "audio/wav").split(";")[0],
            engine=self.name,
            latency_ms=latency,
        )

    def _post(self, body: dict[str, object]) -> httpx.Response:
        return request_with_retry(
            self._client, "POST", self._url,
            label="tts", error_cls=TTSError, headers=self._headers, json=body,
        )


class ResilientTTS:
    """Primary engine with a permanent-or-temporary fallback to the browser."""

    name = "resilient"

    def __init__(self, primary: GroqTTS | None = None, fallback: BrowserTTS | None = None) -> None:
        self.primary = primary
        self.fallback = fallback or BrowserTTS()
        self._degraded_reason: str | None = None

    @property
    def degraded(self) -> bool:
        return self._degraded_reason is not None

    def synthesize(self, text: str, *, trace: Trace | None = None) -> Speech:
        if self.primary is None or self.degraded:
            speech = self.fallback.synthesize(text, trace=trace)
            speech.detail = self._degraded_reason or "no server-side TTS configured"
            return speech

        try:
            return self.primary.synthesize(text, trace=trace)
        except TTSError as exc:
            permanent = exc.status_code in PERMANENT_STATUS
            if permanent:
                # Log once. Every later turn takes the fallback branch above.
                self._degraded_reason = str(exc)
                log.warning("TTS disabled for this process: %s", exc)
            else:
                log.warning("TTS unavailable this turn, using browser: %s", exc)

            speech = self.fallback.synthesize(text, trace=trace)
            speech.detail = str(exc)
            return speech

    def close(self) -> None:
        if self.primary is not None:
            self.primary.close()


def build_tts(client: httpx.Client | None = None) -> ResilientTTS:
    """Construct the TTS stack, tolerating a missing or gated Groq TTS."""
    try:
        primary: GroqTTS | None = GroqTTS(client=client)
    except TTSError as exc:
        log.warning("Server-side TTS unavailable: %s", exc)
        primary = None
    return ResilientTTS(primary=primary)
