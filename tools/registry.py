"""Runtime tool discovery -- the reason this project exists.

Every workflow tagged `agent-tool` becomes a callable the LLM can invoke.
Adding a capability means building a workflow in n8n; no code changes, no
redeploy, no restart beyond the cache TTL.

The rule that shapes the whole module: **a broken workflow must never take the
agent down.** Anything unusable is logged and skipped, so one malformed notes
field costs you one tool rather than the conversation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agent.providers import ToolSpec
from config import config
from tools.n8n_client import N8nClient, N8nError, Workflow
from tools.schema import SchemaError, ToolBinding, build_binding

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

        try:
            return self.client.call_webhook(
                binding.webhook_path, arguments, method=binding.http_method
            )
        except N8nError as exc:
            log.warning("Tool %r failed: %s", name, exc)
            return {"error": f"{name} failed: {exc}"}
