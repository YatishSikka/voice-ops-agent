"""Shared retry policy for the three Groq hops.

STT, LLM and TTS all sit behind the same free-tier rate limit (~30 RPM), so a
429 is an expected condition on every one of them rather than an exception on
one. Keeping the policy here means the voice loop backs off consistently
instead of three modules disagreeing about what a 429 means.

Phase 4 replaces the sleep-and-retry with a real request queue; the call sites
should not have to change when it does.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx

MAX_RETRIES = 3
BACKOFF_BASE_S = 1.5
RETRY_STATUS = (429, 500, 502, 503, 529)


class TransportError(RuntimeError):
    """A hop failed in a way retrying did not fix.

    `status_code` is the HTTP status when there was a response, and None when
    the request never completed. Callers use it to tell a permanent condition
    (403 terms gate, 404 wrong model) from a temporary one -- tts.py relies on
    this to decide between degrading forever and degrading for one turn.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def backoff_seconds(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    # Jitter matters: turns retrying in lockstep just re-create the 429 they
    # are backing off from.
    return BACKOFF_BASE_S * (2**attempt) * (0.5 + random.random() / 2)


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    label: str,
    error_cls: type[Exception] = TransportError,
    max_retries: int = MAX_RETRIES,
    **kwargs: Any,
) -> httpx.Response:
    """Issue a request, retrying transient failures. Returns a 200 response.

    Raises `error_cls` on a non-retryable status or exhausted retries, so
    callers get one exception type per hop instead of raw httpx errors.
    """
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == max_retries:
                raise _build(
                    error_cls, f"{label}: transport failure -- {last_error}", None
                ) from exc
            time.sleep(backoff_seconds(attempt))
            continue

        if response.status_code == 200:
            return response

        if response.status_code in RETRY_STATUS and attempt < max_retries:
            time.sleep(backoff_seconds(attempt, response.headers.get("retry-after")))
            last_error = f"HTTP {response.status_code}"
            continue

        raise _build(
            error_cls,
            f"{label}: HTTP {response.status_code} -- {response.text[:300]}",
            response.status_code,
        )

    raise _build(error_cls, f"{label}: exhausted retries -- {last_error}", None)


def _build(error_cls: type[Exception], message: str, status_code: int | None) -> Exception:
    """Attach the status where the exception type supports it."""
    if issubclass(error_cls, TransportError):
        return error_cls(message, status_code)
    return error_cls(message)
