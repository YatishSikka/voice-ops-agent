"""Thin REST wrapper around the n8n public API.

Deliberately thin: it knows how to reach n8n and nothing about tools or
schemas. Translating a workflow into something an LLM can call is
`tools/registry.py`'s job, which keeps the network layer easy to fake in
tests.

The public API is unavailable on n8n Cloud's trial (see README) -- this
expects a self-hosted instance or a paid plan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent._transport import TransportError, request_with_retry
from config import config

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Workflows do real work -- sending mail, hitting Google APIs -- so they get a
# longer leash than a metadata read.
WEBHOOK_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class N8nError(TransportError):
    """An n8n API call failed."""


@dataclass
class Workflow:
    """One n8n workflow, as far as the registry cares."""

    id: str
    name: str
    active: bool
    description: str = ""
    nodes: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Workflow:
        return cls(
            id=str(payload.get("id", "")),
            name=payload.get("name") or "",
            active=bool(payload.get("active")),
            # n8n returns null, not "", for an unset description.
            description=(payload.get("description") or "").strip(),
            nodes=payload.get("nodes") or [],
            tags=[t.get("name", "") for t in (payload.get("tags") or [])],
        )

    def webhook_node(self) -> dict[str, Any] | None:
        """The trigger the agent will POST to, or None if there isn't one."""
        for node in self.nodes:
            if node.get("type") == "n8n-nodes-base.webhook":
                return node
        return None


class N8nClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or config.n8n_base_url or "").rstrip("/")
        self.api_key = api_key or config.n8n_api_key
        if not self.base_url or not self.api_key:
            raise N8nError("N8N_BASE_URL and N8N_API_KEY must both be set -- see .env.example")
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT)
        self._owns_client = client is None
        self._headers = {"X-N8N-API-KEY": self.api_key, "accept": "application/json"}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- reads --------------------------------------------------------------

    def list_workflows(self, tag: str | None = None, hydrate: bool = True) -> list[Workflow]:
        """Every workflow, or only those carrying `tag`.

        n8n filters by tag server-side, but the result is re-checked here: a
        silently ignored query parameter would otherwise hand the agent every
        workflow in the instance as a callable tool.
        """
        params: dict[str, Any] = {"limit": 250}
        if tag:
            params["tags"] = tag

        payload = self._get("/workflows", params=params)
        workflows = [Workflow.from_api(item) for item in payload.get("data") or []]

        if tag:
            filtered = [w for w in workflows if tag in w.tags]
            if len(filtered) != len(workflows):
                log.warning(
                    "n8n ignored the tag filter; %d of %d workflows matched %r client-side",
                    len(filtered), len(workflows), tag,
                )
            workflows = filtered

        return [self._hydrate(w) for w in workflows] if hydrate else workflows

    def _hydrate(self, workflow: Workflow) -> Workflow:
        """Refetch a workflow that the list endpoint returned incompletely.

        The list response omits `description` altogether -- the key is simply
        absent, where fetch-by-id has it. Since the description is what the
        model reads to decide whether a tool applies, a workflow that looks
        undescribed is refetched before being written off.
        """
        if workflow.description:
            return workflow
        try:
            return self.get_workflow(workflow.id)
        except N8nError as exc:
            log.warning("Could not hydrate workflow %r: %s", workflow.name, exc)
            return workflow

    def get_workflow(self, workflow_id: str) -> Workflow:
        return Workflow.from_api(self._get(f"/workflows/{workflow_id}"))

    # -- writes -------------------------------------------------------------

    def call_webhook(
        self, path: str, payload: dict[str, Any], method: str = "POST", test_mode: bool = False
    ) -> Any:
        """Invoke a workflow's webhook and return its decoded response.

        Production webhooks (`/webhook/...`) only answer while the workflow is
        active; the test path (`/webhook-test/...`) needs the editor listening.
        Either way a 404 here usually means "not active", not "wrong URL".
        """
        prefix = "webhook-test" if test_mode else "webhook"
        url = f"{self.base_url}/{prefix}/{path.lstrip('/')}"

        kwargs: dict[str, Any] = {"headers": {"accept": "application/json"}}
        if method.upper() == "GET":
            kwargs["params"] = payload
        else:
            kwargs["json"] = payload

        response = request_with_retry(
            self._client, method.upper(), url,
            label=f"n8n webhook {path}", error_cls=N8nError,
            timeout=WEBHOOK_TIMEOUT, **kwargs,
        )
        try:
            return response.json()
        except ValueError:
            # Workflows are free to return plain text; that is still a result.
            return response.text

    # -- internals ----------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = request_with_retry(
            self._client, "GET", f"{self.base_url}/api/v1{path}",
            label="n8n api", error_cls=N8nError, headers=self._headers, params=params,
        )
        try:
            return response.json()
        except ValueError as exc:
            raise N8nError(f"n8n api: non-JSON response from {path}") from exc
