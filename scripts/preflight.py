"""Phase 0 gate checks.

Three things can invalidate the build plan. This script verifies all three and
prints a pass/fail table:

  1. Hugging Face account is eligible to host a ZeroGPU Space
     (verified email + account older than 30 days).
  2. The n8n public API answers `GET /api/v1/workflows` with our API key.
     The whole tool-registry idea rests on this one call.
  3. The Groq key actually reaches all three endpoints we depend on
     (STT, LLM, TTS) -- listing a model is not the same as being allowed
     to call it.

Run it before writing code, and again whenever the environment moves
(new key, n8n migration, Space redeploy):

    python scripts/preflight.py
    python scripts/preflight.py --json      # machine-readable, for CI

Exit code is 0 when nothing FAILed, 1 otherwise. Unresolved checks (SKIP /
WARN) do not fail the run but are counted in the summary -- an unresolved
gate is not a passed gate.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import wave
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import config  # noqa: E402  (needs the sys.path line above)

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MIN_ACCOUNT_AGE_DAYS = 30

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


@dataclass
class Result:
    gate: str
    check: str
    status: str
    detail: str


def _short(exc: Exception) -> str:
    """Network errors stringify to something unreadable or empty. Fix both."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _body_hint(response: httpx.Response, limit: int = 160) -> str:
    """Best-effort human-readable reason out of an error response."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:limit]
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value.get("detail")
            if isinstance(value, str) and value.strip():
                return value.strip()[:limit]
    return json.dumps(payload)[:limit]


# --------------------------------------------------------------------------
# Gate 1 -- Hugging Face hosting eligibility
# --------------------------------------------------------------------------

def check_huggingface(client: httpx.Client) -> list[Result]:
    gate = "1. HF Space"
    if not config.hf_token:
        return [
            Result(gate, "token present", SKIP,
                   "HF_TOKEN unset -- cannot verify ZeroGPU eligibility. "
                   "A read token from huggingface.co/settings/tokens is enough."),
        ]

    headers = {"Authorization": f"Bearer {config.hf_token}"}
    try:
        whoami = client.get("https://huggingface.co/api/whoami-v2", headers=headers)
    except httpx.HTTPError as exc:
        return [Result(gate, "token valid", FAIL, _short(exc))]

    if whoami.status_code == 401:
        return [Result(gate, "token valid", FAIL, "token rejected (401) -- expired or revoked")]
    if whoami.status_code != 200:
        return [Result(gate, "token valid", FAIL,
                       f"HTTP {whoami.status_code}: {_body_hint(whoami)}")]

    me = whoami.json()
    username = me.get("name") or "?"
    plan = "PRO" if me.get("isPro") else "free"
    results = [Result(gate, "token valid", PASS, f"authenticated as {username} ({plan})")]

    # Email verification. The key has moved around across API versions, so treat
    # its absence as unknown rather than as a failure.
    verified = me.get("emailVerified")
    if verified is None:
        verified = (me.get("auth") or {}).get("emailVerified")
    if verified is True:
        results.append(Result(gate, "email verified", PASS, "verified"))
    elif verified is False:
        results.append(Result(gate, "email verified", FAIL,
                              "email not verified -- ZeroGPU hosting is blocked until it is"))
    else:
        results.append(Result(gate, "email verified", WARN,
                              "API did not report verification status; confirm at "
                              "huggingface.co/settings/account"))

    results.append(_check_hf_account_age(client, gate, username, headers))
    return results


def _check_hf_account_age(
    client: httpx.Client, gate: str, username: str, headers: dict[str, str]
) -> Result:
    """ZeroGPU requires an account older than 30 days.

    There is no documented, stable field for account creation date, so probe the
    overview endpoint and degrade to a manual check rather than guessing.
    """
    check = "account age >30d"
    try:
        overview = client.get(
            f"https://huggingface.co/api/users/{username}/overview", headers=headers
        )
    except httpx.HTTPError as exc:
        return Result(gate, check, WARN, f"could not query profile ({_short(exc)}); check manually")

    if overview.status_code != 200:
        return Result(gate, check, WARN,
                      f"profile endpoint returned HTTP {overview.status_code}; "
                      f"check the join date on huggingface.co/{username}")

    profile = overview.json()
    raw = next(
        (profile[k] for k in ("createdAt", "created_at", "joinedAt") if profile.get(k)), None
    )
    if not raw:
        return Result(gate, check, WARN,
                      f"no creation date in the API response; "
                      f"check the join date on huggingface.co/{username}")

    try:
        created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return Result(gate, check, WARN, f"unparseable creation date {raw!r}; check manually")

    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - created).days

    if age_days >= MIN_ACCOUNT_AGE_DAYS:
        return Result(gate, check, PASS, f"{age_days} days old (joined {created:%Y-%m-%d})")
    return Result(gate, check, FAIL,
                  f"only {age_days} days old -- ZeroGPU needs {MIN_ACCOUNT_AGE_DAYS}. "
                  f"Eligible on {created:%Y-%m-%d} + 30d. Fallback: Static Space + local "
                  "backend over a Cloudflare Tunnel.")


# --------------------------------------------------------------------------
# Gate 2 -- n8n public API
# --------------------------------------------------------------------------

def check_n8n(client: httpx.Client) -> list[Result]:
    gate = "2. n8n API"
    if not config.n8n_base_url or not config.n8n_api_key:
        missing = ", ".join(
            name for name, value in (
                ("N8N_BASE_URL", config.n8n_base_url),
                ("N8N_API_KEY", config.n8n_api_key),
            ) if not value
        )
        return [Result(gate, "credentials present", SKIP, f"unset: {missing}")]

    url = f"{config.n8n_base_url}/api/v1/workflows"
    headers = {"X-N8N-API-KEY": config.n8n_api_key, "accept": "application/json"}

    try:
        response = client.get(url, headers=headers, params={"limit": 1})
    except httpx.HTTPError as exc:
        return [Result(gate, "GET /api/v1/workflows", FAIL,
                       f"{_short(exc)} -- is {config.n8n_base_url} reachable?")]

    if response.status_code == 401:
        return [Result(gate, "GET /api/v1/workflows", FAIL,
                       "401 -- API key rejected (n8n UI -> Settings -> n8n API)")]
    if response.status_code in (403, 404):
        return [Result(gate, "GET /api/v1/workflows", FAIL,
                       f"HTTP {response.status_code} -- public API not available on this "
                       "instance/plan. Fall back to self-hosted CE now: `npx n8n`.")]
    if response.status_code != 200:
        return [Result(gate, "GET /api/v1/workflows", FAIL,
                       f"HTTP {response.status_code}: {_body_hint(response)}")]

    results = [Result(gate, "GET /api/v1/workflows", PASS,
                      f"200 from {config.n8n_base_url}")]
    results.append(_check_n8n_tag(client, gate, url, headers))
    return results


def _check_n8n_tag(
    client: httpx.Client, gate: str, url: str, headers: dict[str, str]
) -> Result:
    """Confirm we can select workflows by tag -- that is how the registry filters."""
    tag = config.n8n_tool_tag
    check = f"tag filter {tag!r}"

    try:
        response = client.get(url, headers=headers, params={"tags": tag})
        if response.status_code == 200:
            count = len(response.json().get("data") or [])
            source = "server-side"
        else:
            # Older n8n builds ignore or reject ?tags=. Filter client-side instead;
            # the registry can do the same, so this is a WARN at worst.
            response = client.get(url, headers=headers)
            response.raise_for_status()
            count = sum(
                1 for wf in (response.json().get("data") or [])
                if any(t.get("name") == tag for t in (wf.get("tags") or []))
            )
            source = "client-side (server ignored ?tags=)"
    except httpx.HTTPError as exc:
        return Result(gate, check, WARN, f"could not verify: {_short(exc)}")
    except ValueError as exc:
        return Result(gate, check, WARN, f"unparseable response: {_short(exc)}")

    if count:
        return Result(gate, check, PASS, f"{count} workflow(s) tagged, {source}")
    return Result(gate, check, WARN,
                  f"0 workflows tagged {tag!r} -- expected before Phase 2, "
                  "a problem after it")


# --------------------------------------------------------------------------
# Gate 3 -- Groq endpoints
# --------------------------------------------------------------------------

def _silent_wav(seconds: float = 1.0, rate: int = 16_000) -> bytes:
    """A valid 16 kHz mono WAV of silence -- enough to prove the STT route works."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def _rate_limit_note(response: httpx.Response) -> str:
    remaining = response.headers.get("x-ratelimit-remaining-requests")
    return f", {remaining} req left today" if remaining else ""


def check_groq(client: httpx.Client) -> list[Result]:
    gate = "3. Groq"
    if not config.groq_api_key:
        return [Result(gate, "key present", SKIP,
                       "GROQ_API_KEY unset -- get one at console.groq.com/keys")]

    base = config.groq_base_url
    headers = {"Authorization": f"Bearer {config.groq_api_key}"}
    results: list[Result] = []

    # Model catalogue first: it makes a later 404 easy to read as "wrong model
    # name" rather than "endpoint down".
    available: set[str] = set()
    try:
        models = client.get(f"{base}/models", headers=headers)
        if models.status_code == 200:
            available = {m.get("id") for m in models.json().get("data", []) if m.get("id")}
            results.append(Result(gate, "list models", PASS, f"{len(available)} models visible"))
        elif models.status_code == 401:
            return [Result(gate, "list models", FAIL, "401 -- GROQ_API_KEY rejected")]
        else:
            results.append(Result(gate, "list models", WARN,
                                  f"HTTP {models.status_code}: {_body_hint(models)}"))
    except httpx.HTTPError as exc:
        return [Result(gate, "list models", FAIL, _short(exc))]

    for label, model in (
        ("LLM", config.llm_model),
        ("STT", config.stt_model),
        ("TTS", config.tts_model),
    ):
        if available and model not in available:
            results.append(Result(gate, f"{label} model listed", WARN,
                                  f"{model!r} is not in the catalogue -- check the name"))

    results.append(_check_groq_llm(client, gate, base, headers))
    results.append(_check_groq_stt(client, gate, base, headers))
    results.append(_check_groq_tts(client, gate, base, headers))
    return results


def _check_groq_llm(
    client: httpx.Client, gate: str, base: str, headers: dict[str, str]
) -> Result:
    check = f"LLM call ({config.llm_model})"
    try:
        response = client.post(
            f"{base}/chat/completions",
            headers=headers,
            json={
                "model": config.llm_model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
        )
    except httpx.HTTPError as exc:
        return Result(gate, check, FAIL, _short(exc))

    if response.status_code == 200:
        return Result(gate, check, PASS, f"200 OK{_rate_limit_note(response)}")
    return Result(gate, check, FAIL, f"HTTP {response.status_code}: {_body_hint(response)}")


def _check_groq_stt(
    client: httpx.Client, gate: str, base: str, headers: dict[str, str]
) -> Result:
    check = f"STT call ({config.stt_model})"
    try:
        response = client.post(
            f"{base}/audio/transcriptions",
            headers=headers,
            files={"file": ("silence.wav", _silent_wav(), "audio/wav")},
            data={"model": config.stt_model, "response_format": "json"},
        )
    except httpx.HTTPError as exc:
        return Result(gate, check, FAIL, _short(exc))

    if response.status_code == 200:
        # Silence transcribes to "" or a stray token; either way the route works.
        return Result(gate, check, PASS,
                      f"200 OK on 1s of silence{_rate_limit_note(response)}")
    return Result(gate, check, FAIL, f"HTTP {response.status_code}: {_body_hint(response)}")


def _check_groq_tts(
    client: httpx.Client, gate: str, base: str, headers: dict[str, str]
) -> Result:
    """TTS is the known-fragile hop -- preview models sit behind a terms gate."""
    check = f"TTS call ({config.tts_model})"
    fallback = "Browser speechSynthesis fallback ships instead; keep Groq TTS as a Phase 4 upgrade."
    try:
        response = client.post(
            f"{base}/audio/speech",
            headers=headers,
            json={
                "model": config.tts_model,
                "voice": config.tts_voice,
                "input": "preflight",
                "response_format": "wav",
            },
        )
    except httpx.HTTPError as exc:
        return Result(gate, check, WARN, f"{_short(exc)}. {fallback}")

    if response.status_code == 200:
        kib = len(response.content) / 1024
        return Result(gate, check, PASS,
                      f"{kib:.1f} KiB of audio, voice {config.tts_voice}"
                      f"{_rate_limit_note(response)}")

    # Not a FAIL: the plan already carries a fallback for exactly this.
    reason = _body_hint(response)
    if response.status_code in (400, 403) and "terms" in reason.lower():
        reason = f"model requires accepting terms at console.groq.com -- {reason}"
    return Result(gate, check, WARN, f"HTTP {response.status_code}: {reason}. {fallback}")


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def render_table(results: list[Result]) -> str:
    gate_w = max(len(r.gate) for r in results)
    check_w = max(len(r.check) for r in results)
    lines: list[str] = []
    current: str | None = None
    for result in results:
        new_gate = result.gate != current
        if current is not None and new_gate:
            lines.append("")
        current = result.gate
        # Print the gate name once per group; the repeats are noise.
        label = result.gate if new_gate else ""
        lines.append(
            f"  {label:<{gate_w}}  {result.check:<{check_w}}  "
            f"[{result.status}]  {result.detail}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        results = check_huggingface(client) + check_n8n(client) + check_groq(client)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print("\nVoice-Ops Agent -- Phase 0 preflight\n")
        print(render_table(results))

    failed = [r for r in results if r.status == FAIL]
    unresolved = [r for r in results if r.status in (WARN, SKIP)]

    if not args.json:
        print(
            f"\n  {len(results) - len(failed) - len(unresolved)} passed, "
            f"{len(failed)} failed, {len(unresolved)} unresolved\n"
        )
        for result in failed:
            print(f"  BLOCKED  {result.gate} / {result.check}: {result.detail}")
        if unresolved and not failed:
            print("  An unresolved gate is not a passed gate -- clear the SKIP/WARN rows above.")
        print()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
