"""Runtime tool discovery -- the reason this project exists.

Every workflow tagged `agent-tool` becomes a callable the LLM can invoke.
Adding a capability means building a workflow in n8n; no code changes, no
redeploy, no restart beyond the cache TTL.

The rule that shapes the whole module: **a broken workflow must never take the
agent down.** Anything unusable is logged and skipped, so one malformed notes
field costs you one tool rather than the conversation.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from agent.providers import ToolSpec
from config import config
from tasks.store import store
from tools.n8n_client import N8nClient, N8nError, Workflow
from tools.schema import CONFIRM_KEY, SchemaError, ToolBinding, build_binding

log = logging.getLogger(__name__)


@dataclass
class Skipped:
    """A workflow that could not become a tool, and why.

    Surfaced rather than swallowed: "why isn't my workflow showing up?" is the
    question this project will get asked most.
    """

    workflow_name: str
    reason: str


@dataclass
class Discovery:
    bindings: dict[str, ToolBinding] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def specs(self) -> list[ToolSpec]:
        return [b.spec for b in self.bindings.values()]


class ToolRegistry:
    """Discovers n8n workflows and dispatches tool calls to them."""

    def __init__(
        self,
        client: N8nClient | None = None,
        tag: str | None = None,
        cache_ttl: int | None = None,
    ) -> None:
        self._client = client
        self.tag = tag or config.n8n_tool_tag
        self.cache_ttl = config.registry_cache_ttl if cache_ttl is None else cache_ttl
        self._cache: Discovery | None = None
        # Issued confirmation tokens -> the exact action each authorises.
        self._pending: dict[str, str] = {}

    @property
    def client(self) -> N8nClient:
        if self._client is None:
            self._client = N8nClient()
        return self._client

    # -- discovery ----------------------------------------------------------

    def discover(self, force: bool = False) -> Discovery:
        """Fetch and translate tagged workflows, honouring the cache TTL."""
        if not force and self._cache and (time.time() - self._cache.fetched_at) < self.cache_ttl:
            return self._cache

        try:
            workflows = self.client.list_workflows(tag=self.tag)
        except N8nError as exc:
            # n8n being unreachable should degrade the agent to conversation,
            # not crash it -- and stale tools beat no tools.
            log.error("Tool discovery failed: %s", exc)
            if self._cache:
                log.warning("Serving %d cached tools", len(self._cache.bindings))
                return self._cache
            return Discovery(fetched_at=time.time())

        discovery = self._translate(workflows)
        discovery.fetched_at = time.time()
        self._cache = discovery

        log.info(
            "Discovered %d tool(s) from %d tagged workflow(s); %d skipped",
            len(discovery.bindings), len(workflows), len(discovery.skipped),
        )
        for skip in discovery.skipped:
            log.warning("Skipped %r: %s", skip.workflow_name, skip.reason)
        return discovery

    def _translate(self, workflows: list[Workflow]) -> Discovery:
        discovery = Discovery()

        # Sort by id so name-collision resolution is deterministic across runs
        # rather than dependent on n8n's ordering.
        for workflow in sorted(workflows, key=lambda w: w.id):
            if not workflow.active:
                # An inactive workflow's production webhook 404s, so exposing
                # it would hand the model a tool guaranteed to fail.
                discovery.skipped.append(
                    Skipped(workflow.name, "workflow is not active -- activate it in n8n")
                )
                continue

            try:
                binding = build_binding(workflow)
            except SchemaError as exc:
                discovery.skipped.append(Skipped(workflow.name, str(exc)))
                continue

            name = binding.spec.name
            if name in discovery.bindings:
                existing = discovery.bindings[name].workflow_name
                discovery.skipped.append(
                    Skipped(
                        workflow.name,
                        f"tool name {name!r} already taken by workflow {existing!r} -- rename one",
                    )
                )
                continue

            discovery.bindings[name] = binding

        return discovery

    # -- the LLM-facing surface --------------------------------------------

    def specs(self, force: bool = False) -> list[ToolSpec]:
        return self.discover(force=force).specs

    def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run a tool call. Never raises -- the model gets errors as results.

        Feeding a failure back as a tool result lets the model apologise or try
        another route, which is a better turn than an exception that kills it.
        """
        discovery = self.discover()
        binding = discovery.bindings.get(name)

        if binding is None:
            # Re-check once: the model may be calling a tool added mid-session.
            discovery = self.discover(force=True)
            binding = discovery.bindings.get(name)
        if binding is None:
            known = ", ".join(sorted(discovery.bindings)) or "none"
            return {"error": f"unknown tool {name!r}; available: {known}"}

        if "__parse_error__" in arguments:
            return {"error": arguments["__parse_error__"]}

        if binding.is_destructive:
            gate = self._check_confirmation(name, arguments)
            if gate is not None:
                return gate
            arguments = {k: v for k, v in arguments.items() if k != CONFIRM_KEY}

        if binding.is_async:
            return self._dispatch_async(binding, name, arguments)

        try:
            return self.client.call_webhook(
                binding.webhook_path, arguments, method=binding.http_method
            )
        except N8nError as exc:
            log.warning("Tool %r failed: %s", name, exc)
            return {"error": f"{name} failed: {exc}"}

    def _check_confirmation(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Gate a destructive tool. Returns a prompt to relay, or None to proceed.

        The token is bound to the exact arguments it was issued for. Confirming
        "email Dana saying I'm late" must not authorise emailing someone else
        -- without that binding, a model could get a yes for a harmless action
        and then execute a different one with the same token.

        Tokens are single use, so a repeated call cannot re-fire the action.
        """
        token = str(arguments.get(CONFIRM_KEY) or "")
        payload = {k: v for k, v in arguments.items() if k != CONFIRM_KEY}
        fingerprint = self._fingerprint(name, payload)

        if token:
            expected = self._pending.pop(token, None)
            if expected is None:
                return {
                    "error": "That confirmation token is unknown or already used. "
                             "Ask the user again and use the new token."
                }
            if expected != fingerprint:
                log.warning("Confirmation token for %r reused with different arguments", name)
                return {
                    "error": "The arguments changed since the user confirmed. "
                             "Describe the new action and get a fresh confirmation."
                }
            return None  # confirmed, and for exactly this action

        token = secrets.token_urlsafe(8)
        self._pending[token] = fingerprint
        readable = ", ".join(f"{k}={v!r}" for k, v in payload.items()) or "no arguments"
        return {
            "status": "confirmation_required",
            CONFIRM_KEY: token,
            "action": f"{name}({readable})",
            "instruction": (
                "Do not perform this yet. Tell the user plainly what is about to "
                "happen, in one sentence, and ask them to confirm. If they agree, "
                f"call {name} again with the same arguments plus {CONFIRM_KEY} set "
                "to the token above. If they decline, do nothing and say so."
            ),
        }

    @staticmethod
    def _fingerprint(name: str, payload: dict[str, Any]) -> str:
        return f"{name}:{json.dumps(payload, sort_keys=True, default=str)}"

    def _dispatch_async(
        self, binding: ToolBinding, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Start a long-running workflow and return without waiting.

        The workflow is handed a task id and a URL to call when it finishes.
        The conversation ends here; delivery happens over Telegram.
        """
        task = store.create(name, arguments)
        callback_url = (
            f"{config.public_base_url}/tasks/{task.id}/complete"
            if config.public_base_url
            else None
        )
        if callback_url is None:
            log.warning(
                "PUBLIC_BASE_URL is unset; %r will run but its result cannot be "
                "delivered back to this process", name
            )

        payload = {**arguments, "__task_id": task.id, "__callback_url": callback_url}
        try:
            self.client.call_webhook(binding.webhook_path, payload, method=binding.http_method)
        except N8nError as exc:
            # Failing to *start* is worth telling the model about now, unlike
            # failing to finish, which arrives by message later.
            store.complete(task.id, error=str(exc))
            log.warning("Async tool %r failed to start: %s", name, exc)
            return {"error": f"{name} could not be started: {exc}"}

        log.info("Started async task %s for %r", task.id, name)
        return {
            "status": "started",
            "task_id": task.id,
            "message": (
                "The job is running in the background. Tell the user you will "
                "message them when it is done, and do not wait for a result."
            ),
        }
