"""Speech to text via Groq Whisper.

Gradio hands the mic recording over as a filepath, so `transcribe()` accepts a
path or raw bytes and normalises both into one multipart upload.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from config import config

from ._transport import TransportError, request_with_retry
from .timing import Trace

REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# A WAV header alone is 44 bytes. Anything near that carries no speech, and
# Groq bills a 10-second minimum per request -- so drop it before the network.
MIN_AUDIO_BYTES = 2_000


class STTError(TransportError):
    """Transcription failed."""


@dataclass
class Transcript:
    text: str
    latency_ms: float = 0.0
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.text.strip())


class GroqSTT:
    def __init__(self, model: str | None = None, client: httpx.Client | None = None) -> None:
        if not config.groq_api_key:
            raise STTError("GROQ_API_KEY is unset -- see .env.example")
        self.model = model or config.stt_model
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT)
        self._owns_client = client is None
        self._url = f"{config.groq_base_url}/audio/transcriptions"
        self._headers = {"Authorization": f"Bearer {config.groq_api_key}"}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def transcribe(
        self,
        audio: str | Path | bytes | None,
        *,
        language: str | None = None,
        prompt: str | None = None,
        trace: Trace | None = None,
        filename: str | None = None,
    ) -> Transcript:
        """Transcribe a recording.

        Silence and empty input return an empty Transcript rather than raising:
        a user who taps the mic and says nothing is a normal event in a voice
        UI, not an error the loop should have to catch.
        """
        payload, detected = self._read(audio)
        filename = filename or detected
        if payload is None or len(payload) < MIN_AUDIO_BYTES:
            return Transcript(text="", model=self.model)

        data: dict[str, str] = {"model": self.model, "response_format": "json"}
        if language:
            data["language"] = language
        if prompt:
            # Whisper uses this to bias spelling -- worth feeding it the tool
            # names once the registry is live, so "Notion" survives the ASR.
            data["prompt"] = prompt

        mime = mimetypes.guess_type(filename)[0] or "audio/wav"
        span = trace.span("stt", model=self.model) if trace is not None else None
        if span is not None:
            with span as record:
                response = self._post(payload, filename, mime, data)
                result = self._to_transcript(response, record.duration_ms)
                record.meta["chars"] = len(result.text)
        else:
            response = self._post(payload, filename, mime, data)
            result = self._to_transcript(response, 0.0)
        return result

    def _post(
        self, payload: bytes, filename: str, mime: str, data: dict[str, str]
    ) -> httpx.Response:
        return request_with_retry(
            self._client, "POST", self._url,
            label="stt", error_cls=STTError,
            headers=self._headers,
            files={"file": (filename, payload, mime)},
            data=data,
        )

    def _to_transcript(self, response: httpx.Response, latency_ms: float) -> Transcript:
        try:
            body = response.json()
        except ValueError as exc:
            raise STTError(f"stt: non-JSON response -- {response.text[:200]}") from exc
        return Transcript(
            text=(body.get("text") or "").strip(),
            latency_ms=latency_ms,
            model=self.model,
            raw=body,
        )

    @staticmethod
    def _read(audio: str | Path | bytes | None) -> tuple[bytes | None, str]:
        if audio is None:
            return None, "audio.wav"
        if isinstance(audio, bytes):
            return audio, "audio.wav"
        path = Path(audio)
        if not path.is_file():
            raise STTError(f"stt: no such audio file: {path}")
        return path.read_bytes(), path.name
