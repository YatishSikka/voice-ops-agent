"""Translate an n8n workflow into an LLM tool definition.

This module and `registry.py` are the point of the project, so the rules are
written down rather than implied.

An n8n webhook accepts arbitrary JSON and declares no schema, so a workflow has
to say what it expects. Two fields that already exist in the n8n editor carry
that, which is what keeps "add a tool without touching code" literally true:

  * the **webhook node's Notes** field holds the JSON Schema for the arguments
  * the **workflow description** holds the prose the model reads
  * the **workflow name** becomes the tool name, slugified

The Notes field accepts either a bare JSON Schema::

    {"type": "object",
     "properties": {"day": {"type": "string", "description": "e.g. tomorrow"}},
     "required": ["day"]}

or an envelope that carries the description alongside it::

    {"description": "Look up calendar events for a day",
     "parameters": {"type": "object", "properties": {...}}}

Anything unparseable is a skip with a log line, never an exception: one broken
workflow must not take the agent down with it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from agent.providers import ToolSpec
from tools.n8n_client import Workflow

log = logging.getLogger(__name__)

# OpenAI and Anthropic both accept ^[a-zA-Z0-9_-]{1,64}$ for tool names.
NAME_MAX = 64
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class SchemaError(ValueError):
    """A workflow cannot be expressed as a tool."""


@dataclass(frozen=True)
class ToolBinding:
    """A ToolSpec plus what the registry needs in order to invoke it."""

    spec: ToolSpec
    workflow_id: str
    workflow_name: str
    webhook_path: str
    http_method: str = "POST"
    is_async: bool = False


@dataclass(frozen=True)
class NotesSpec:
    """What a webhook node's Notes field declares."""

    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)
    is_async: bool = False


def slugify(name: str) -> str:
    """Workflow name -> a legal tool name.

    'Get Calendar Events!' -> 'get_calendar_events'
    """
    slug = _SLUG_STRIP.sub("_", name.strip().lower()).strip("_")
    if not slug:
        raise SchemaError(f"workflow name {name!r} slugifies to nothing")
    if slug[0].isdigit():
        # Leading digits are legal per the pattern but read badly as identifiers.
        slug = f"t_{slug}"
    return slug[:NAME_MAX]


def parse_notes(notes: str) -> NotesSpec:
    """Read a webhook node's Notes field.

    Tolerates a ```json fenced block, since that is how people paste JSON into
    a notes box.
    """
    text = (notes or "").strip()
    if not text:
        return NotesSpec()

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"notes are not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SchemaError(f"notes must be a JSON object, got {type(parsed).__name__}")

    description = str(parsed.get("description", "")).strip()
    # "async": true means the workflow takes long enough that the conversation
    # should not wait for it -- fire it and notify on completion instead.
    is_async = bool(parsed.get("async", False))

    # Envelope form: {"description": ..., "parameters": {...}}
    if "parameters" in parsed:
        schema = parsed["parameters"]
        if not isinstance(schema, dict):
            raise SchemaError("'parameters' must be an object")
        return NotesSpec(description, _normalise(schema), is_async)

    # Bare-schema form.
    if "properties" in parsed or parsed.get("type") == "object":
        return NotesSpec(description, _normalise(parsed), is_async)

    raise SchemaError(
        "notes JSON has neither 'parameters' nor 'properties' -- "
        "expected a JSON Schema or a {description, parameters} envelope"
    )


def _normalise(schema: dict[str, Any]) -> dict[str, Any]:
    """Fill in what providers require but authors routinely omit."""
    out = dict(schema)
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    if not isinstance(out["properties"], dict):
        raise SchemaError("'properties' must be an object")

    required = out.get("required")
    if required is not None:
        if not isinstance(required, list):
            raise SchemaError("'required' must be a list")
        unknown = [key for key in required if key not in out["properties"]]
        if unknown:
            # A required-but-undefined property makes the model invent values.
            raise SchemaError(f"'required' names undeclared properties: {unknown}")
    return out


def webhook_path(node: dict[str, Any]) -> str:
    path = str((node.get("parameters") or {}).get("path") or "").strip()
    if not path:
        raise SchemaError("webhook node has no path")
    return path.lstrip("/")


def build_binding(workflow: Workflow) -> ToolBinding:
    """Turn one workflow into a callable tool definition.

    Raises SchemaError for every reason a workflow cannot be a tool, so the
    registry has exactly one failure mode to catch and log.
    """
    node = workflow.webhook_node()
    if node is None:
        raise SchemaError("no webhook trigger node")

    notes = parse_notes(node.get("notes", ""))
    schema = notes.schema

    # Workflow description wins: it is the more discoverable field, and the one
    # a person editing in the UI is most likely to keep current.
    description = workflow.description or notes.description
    if not description:
        raise SchemaError(
            "no description -- set the workflow description, or a 'description' "
            "key in the webhook node's notes"
        )

    if not schema:
        # A tool taking no arguments is legitimate; a tool whose arguments are
        # undeclared is not, because the model would have to guess them.
        schema = EMPTY_SCHEMA

    method = str((node.get("parameters") or {}).get("httpMethod") or "POST").upper()

    if notes.is_async:
        # Tell the model what it is choosing: the result arrives later, by
        # message, and it should say so rather than pretend to have an answer.
        description = (
            f"{description} This runs in the background and can take a while; "
            "the result is sent to the user by message when it finishes."
        )

    return ToolBinding(
        spec=ToolSpec(name=slugify(workflow.name), description=description, parameters=schema),
        workflow_id=workflow.id,
        workflow_name=workflow.name,
        webhook_path=webhook_path(node),
        http_method=method,
        is_async=notes.is_async,
    )
