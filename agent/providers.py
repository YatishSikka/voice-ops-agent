"""LLM provider interface, with Groq and Claude adapters.

The agent loop must not know which vendor is answering. That means the provider
owns three things, not just the request:

  1. the call itself                     -> chat()
  2. how a reply becomes history         -> assistant_message()
  3. how a tool result becomes history    -> tool_result_message()

(2) and (3) are where the vendors actually diverge -- OpenAI-style APIs put tool
results in a `role: "tool"` message keyed by `tool_call_id`, while Anthropic puts
them in `tool_result` content blocks inside a *user* message. Hiding that here is
what keeps `agent/loop.py` provider-neutral and makes LLM_PROVIDER a one-line
swap.

System prompts are passed to chat() as a keyword rather than as a message,
because Anthropic takes `system` as a top-level request field.

    llm = build_llm()
    reply = llm.chat(messages, tools=specs, system="You are ...")
    if reply.tool_calls:
        messages.append(llm.assistant_message(reply))
        messages.append(llm.tool_result_message(reply.tool_calls[0], '{"ok":true}'))
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import config

from ._transport import TransportError, request_with_retry
from .timing import Trace

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Llama models on Groq intermittently emit a tool call in Llama's *text*
# format -- <function=name{"arg": 1}</function> -- instead of structured JSON.
# Groq rejects that with a 400 carrying code "tool_use_failed". It is a
# sampling failure rather than a bad request, so the same prompt usually
# succeeds on a retry, and a 400 is otherwise never retried.
TOOL_FORMAT_RETRIES = 2
TOOL_USE_FAILED = "tool_use_failed"


class LLMError(TransportError):
    """Provider call failed in a way retrying will not fix."""


@dataclass(frozen=True)
class ToolSpec:
    """A callable exposed to the model.

    `tools/registry.py` produces these from n8n workflows; each provider renders
    them into its own wire format.
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema, object-typed


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class Completion:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _parse_arguments(raw: str, tool_name: str) -> dict[str, Any]:
    """Models emit tool arguments as a JSON string, and sometimes get it wrong.

    A malformed blob must not kill the turn -- the loop should be able to hand
    the error back to the model -- so return the problem as data.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"__parse_error__": f"invalid JSON arguments for {tool_name}: {exc}"}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


class LLMProvider(ABC):
    """Vendor-neutral chat interface used by agent/loop.py."""

    name: str = "abstract"
    model: str = ""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @abstractmethod
    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[ToolSpec] | None = None,
        system: str | None = None,
        trace: Trace | None = None,
    ) -> Completion:
        ...

    @abstractmethod
    def assistant_message(self, completion: Completion) -> dict[str, Any]:
        """The model's own reply, in the shape this vendor expects back."""

    @abstractmethod
    def tool_result_message(self, call: ToolCall, content: str) -> dict[str, Any]:
        """A tool's output, in the shape this vendor expects back."""

    # -- shared transport ---------------------------------------------------

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        """POST with bounded retry on 429 and 5xx, honouring Retry-After."""
        response = request_with_retry(
            self._client, "POST", url,
            label=self.name, error_cls=LLMError, headers=headers, json=payload,
        )
        try:
            return response.json()
        except ValueError as exc:
            raise LLMError(f"{self.name}: non-JSON response -- {response.text[:200]}") from exc


class GroqProvider(LLMProvider):
    """Groq via its OpenAI-compatible endpoint."""

    name = "groq"

    def __init__(self, model: str | None = None, client: httpx.Client | None = None) -> None:
        super().__init__(client)
        if not config.groq_api_key:
            raise LLMError("GROQ_API_KEY is unset -- see .env.example")
        self.model = model or config.llm_model
        self._url = f"{config.groq_base_url}/chat/completions"
        self._headers = {"Authorization": f"Bearer {config.groq_api_key}"}

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[ToolSpec] | None = None,
        system: str | None = None,
        trace: Trace | None = None,
    ) -> Completion:
        wire: list[dict[str, Any]] = list(messages)
        if system:
            wire = [{"role": "system", "content": system}, *wire]

        payload: dict[str, Any] = {"model": self.model, "messages": wire}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
                for spec in tools
            ]
            payload["tool_choice"] = "auto"

        if trace is not None:
            with trace.span("llm", provider=self.name, model=self.model):
                data = self._post_tolerating_bad_tool_format(payload)
        else:
            data = self._post_tolerating_bad_tool_format(payload)

        return self._to_completion(data)

    def _post_tolerating_bad_tool_format(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Retry the malformed-tool-call 400, which resampling usually fixes."""
        for attempt in range(TOOL_FORMAT_RETRIES + 1):
            try:
                return self._post(self._url, self._headers, payload)
            except LLMError as exc:
                retryable = exc.status_code == 400 and TOOL_USE_FAILED in str(exc)
                if not retryable or attempt == TOOL_FORMAT_RETRIES:
                    raise
                log.warning(
                    "Model emitted a malformed tool call (attempt %d/%d); resampling",
                    attempt + 1, TOOL_FORMAT_RETRIES,
                )
        raise LLMError(f"{self.name}: exhausted tool-format retries")  # unreachable

    def _to_completion(self, data: dict[str, Any]) -> Completion:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = [
            ToolCall(
                id=call.get("id", ""),
                name=(call.get("function") or {}).get("name", ""),
                arguments=_parse_arguments(
                    (call.get("function") or {}).get("arguments", ""),
                    (call.get("function") or {}).get("name", "?"),
                ),
                raw_arguments=(call.get("function") or {}).get("arguments", ""),
            )
            for call in (message.get("tool_calls") or [])
        ]
        usage = data.get("usage") or {}
        return Completion(
            text=message.get("content") or "",
            tool_calls=calls,
            finish_reason=choice.get("finish_reason", ""),
            usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
            model=data.get("model", self.model),
            raw=data,
        )

    def assistant_message(self, completion: Completion) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": completion.text or None}
        if completion.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.raw_arguments or "{}"},
                }
                for call in completion.tool_calls
            ]
        return message

    def tool_result_message(self, call: ToolCall, content: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content}


class ClaudeProvider(LLMProvider):
    """Anthropic Messages API.

    Written against httpx rather than the `anthropic` SDK so that switching
    providers costs nothing at deploy time -- Hugging Face Spaces installs
    requirements.txt, and Claude is not the default path.

    NOTE: unverified against the live API. The plan carries Claude as a swap
    option, not the shipping provider, so treat this as reviewed-but-untested
    until a run proves it.
    """

    name = "claude"
    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"
    MAX_TOKENS = 2048

    def __init__(self, model: str | None = None, client: httpx.Client | None = None) -> None:
        super().__init__(client)
        if not config.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is unset but LLM_PROVIDER=claude")
        self.model = model or config.anthropic_model
        self._headers = {
            "x-api-key": config.anthropic_api_key,
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[ToolSpec] | None = None,
        system: str | None = None,
        trace: Trace | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.MAX_TOKENS,
            "messages": self._merge_adjacent(messages),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {"name": s.name, "description": s.description, "input_schema": s.parameters}
                for s in tools
            ]

        if trace is not None:
            with trace.span("llm", provider=self.name, model=self.model):
                data = self._post(self.API_URL, self._headers, payload)
        else:
            data = self._post(self.API_URL, self._headers, payload)

        return self._to_completion(data)

    @staticmethod
    def _merge_adjacent(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Anthropic expects alternating roles.

        Two parallel tool calls produce two tool_result messages in a row, so
        fold consecutive same-role messages into one with concatenated blocks.
        """
        merged: list[dict[str, Any]] = []
        for message in messages:
            content = message.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            if merged and merged[-1]["role"] == message["role"]:
                merged[-1]["content"].extend(blocks)
            else:
                merged.append({"role": message["role"], "content": list(blocks)})
        return merged

    def _to_completion(self, data: dict[str, Any]) -> Completion:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                arguments = block.get("input") or {}
                calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=arguments,
                        raw_arguments=json.dumps(arguments),
                    )
                )
        usage = data.get("usage") or {}
        return Completion(
            text="".join(text_parts),
            tool_calls=calls,
            finish_reason=data.get("stop_reason", ""),
            usage=Usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0)),
            model=data.get("model", self.model),
            raw=data,
        )

    def assistant_message(self, completion: Completion) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        if completion.text:
            blocks.append({"type": "text", "text": completion.text})
        for call in completion.tool_calls:
            blocks.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
        return {"role": "assistant", "content": blocks}

    def tool_result_message(self, call: ToolCall, content: str) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call.id, "content": content}
            ],
        }


def build_llm(client: httpx.Client | None = None) -> LLMProvider:
    """Construct the provider named by LLM_PROVIDER."""
    providers = {"groq": GroqProvider, "claude": ClaudeProvider}
    try:
        return providers[config.llm_provider](client=client)
    except KeyError:
        raise LLMError(
            f"unknown LLM_PROVIDER {config.llm_provider!r}; expected one of {sorted(providers)}"
        ) from None
