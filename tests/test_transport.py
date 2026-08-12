"""A 429 means two different things, and they need different handling."""

import httpx
import pytest

from agent._transport import TransportError, is_daily_quota, request_with_retry

PER_MINUTE = '{"error":{"message":"Rate limit reached ... on requests per minute (RPM)"}}'
PER_DAY = '{"error":{"message":"Rate limit reached ... on tokens per day (TPD): Limit 100000"}}'


def test_daily_quota_is_recognised():
    assert is_daily_quota(PER_DAY) is True
    assert is_daily_quota(PER_MINUTE) is False


def test_a_daily_quota_is_not_retried():
    """Waiting out a daily limit costs 90s per call and changes nothing."""
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(429, text=PER_DAY)

    with pytest.raises(TransportError, match="daily quota"):
        request_with_retry(
            httpx.Client(transport=httpx.MockTransport(handler)),
            "POST", "http://x", label="llm",
        )

    assert len(attempts) == 1


def test_a_per_minute_limit_is_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 2:
            return httpx.Response(429, text=PER_MINUTE, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    response = request_with_retry(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "POST", "http://x", label="llm",
    )

    assert response.status_code == 200
    assert len(attempts) == 2
