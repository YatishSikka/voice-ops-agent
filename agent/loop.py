"""The agentic tool-calling loop.

Runs the standard cycle -- ask the model, run any tools it asks for, feed the
results back, repeat until it answers in prose -- with two constraints that
come from this being a *voice* agent:

  * **It must always produce something sayable.** A stack trace cannot be read
    aloud, so every failure path ends in a spoken sentence.
  * **It must terminate.** `max_iterations` caps the cycle; a model that keeps
    calling tools gets cut off with an honest answer rather than looping until
    the rate limit does it instead.

Tools come from `ToolRegistry`, so what the agent can do is whatever is tagged
in n8n at the moment the turn starts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agent.providers import Completion, LLMError, LLMProvider, ToolCall, build_llm
from agent.timing import Trace
from config import config
from tools.registry import ToolRegistry

log = logging.getLogger(__name__)

# Spoken, not written: the reply goes through TTS, where markdown becomes
# noise and a long paragraph becomes a hostage situation.
SYSTEM_PROMPT = """You are a voice-controlled operations assistant.

Your replies are spoken aloud, so:
- Keep them to one or two short sentences.
- Use plain prose. No markdown, no bullet points, no code, no emoji.
- Say numbers and dates the way a person would: "tomorrow at 2" not "14:00".

You have tools backed by real automations. Use them when the user asks for
something they cover, and never invent results you did not get from a tool.
If a tool fails, say briefly what went wrong.

Distinguish actions from questions. If you are asked to *do* something you have
no tool for, say plainly that you cannot. If you are asked a question you can
simply answer, answer it -- having no tool is not a reason to refuse to think."""

# Said aloud when the model will not stop calling tools.
GIVE_UP_REPLY = "Sorry, I got stuck working on that. Could you try asking a different way?"


@dataclass
class ToolInvocation:
    """One tool call and what it returned -- for the UI and the eval harness."""

    name: str
    arguments: dict[str, Any]
    result: Any
    failed: bool = False


@dataclass
class TurnResult:
    reply: str
    invocations: list[ToolInvocation] = field(default_factory=list)
    iterations: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def tools_used(self) -> list[str]:
        return [i.name for i in self.invocations]


def _stringify(result: Any) -> str:
    """Tool results go back to the model as text."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError):
        return str(result)


class AgentLoop:
    def __init__(
        self,
        llm: LLMProvider | None = None,
        registry: ToolRegistry | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        max_iterations: int | None = None,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations or config.max_tool_iterations

    @property
    def llm(self) -> LLMProvider:
        if self._llm is None:
            self._llm = build_llm()
        return self._llm

    @property
    def registry(self) -> ToolRegistry:
        if self._registry is None:
            self._registry = ToolRegistry()
        return self._registry

    def run(
        self,
        user_text: str,
        history: list[dict[str, Any]] | None = None,
        trace: Trace | None = None,
    ) -> TurnResult:
        """Run one turn to a spoken answer. Never raises."""
        messages: list[dict[str, Any]] = list(history or [])
        messages.append({"role": "user", "content": user_text})

        # Refreshed per turn, so a workflow tagged mid-conversation is usable
        # in the next breath -- that is the whole pitch.
        specs = self.registry.specs()
        result = TurnResult(reply="", messages=messages)

        for iteration in range(1, self.max_iterations + 1):
            result.iterations = iteration
            try:
                completion: Completion = self.llm.chat(
                    messages, tools=specs or None, system=self.system_prompt, trace=trace
                )
            except LLMError as exc:
                log.error("LLM call failed: %s", exc)
                result.reply = "Sorry, I could not reach the language model just now."
                result.error = str(exc)
                return result

            if not completion.tool_calls:
                result.reply = (completion.text or "").strip() or GIVE_UP_REPLY
                return result

            messages.append(self.llm.assistant_message(completion))
            for call in completion.tool_calls:
                messages.append(self._run_tool(call, result, trace))

        # Out of iterations: answer with whatever prose we have rather than
        # leaving the user in silence.
        log.warning("Hit max_iterations=%d with tools still pending", self.max_iterations)
        result.reply = GIVE_UP_REPLY
        result.error = f"exceeded max_iterations={self.max_iterations}"
        return result

    def _run_tool(
        self, call: ToolCall, result: TurnResult, trace: Trace | None
    ) -> dict[str, Any]:
        log.info("tool %s(%s)", call.name, call.arguments)

        if trace is not None:
            with trace.span("tool", tool=call.name):
                output = self.registry.dispatch(call.name, call.arguments)
        else:
            output = self.registry.dispatch(call.name, call.arguments)

        failed = isinstance(output, dict) and "error" in output
        result.invocations.append(
            ToolInvocation(call.name, call.arguments, output, failed=failed)
        )
        return self.llm.tool_result_message(call, _stringify(output))
