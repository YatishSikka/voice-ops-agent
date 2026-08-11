"""Registry translation is the project's core claim, so it gets the hardest tests.

The governing rule is that a broken workflow costs you that one tool and
nothing else -- the agent must still boot, and every other workflow must still
be callable.
"""

import json

import pytest

from tools.n8n_client import N8nError, Workflow
from tools.registry import ToolRegistry
from tools.schema import SchemaError, build_binding, parse_notes, slugify

SCHEMA = {
    "type": "object",
    "properties": {"day": {"type": "string", "description": "e.g. tomorrow"}},
    "required": ["day"],
}


def workflow(
    name="Get Calendar Events",
    wf_id="1",
    notes=None,
    description="Look up calendar events for a day",
    active=True,
    path="calendar",
    node_type="n8n-nodes-base.webhook",
    method="POST",
):
    node = {
        "type": node_type,
        "name": "Webhook",
        "parameters": {"path": path, "httpMethod": method},
        "notes": json.dumps(SCHEMA) if notes is None else notes,
    }
    return Workflow(
        id=wf_id, name=name, active=active, description=description,
        nodes=[node], tags=["agent-tool"],
    )


class FakeClient:
    """Stands in for N8nClient without a network."""

    def __init__(self, workflows=None, error=None):
        self.workflows = workflows or []
        self.error = error
        self.calls = []
        self.list_count = 0

    def list_workflows(self, tag=None):
        self.list_count += 1
        if self.error:
            raise self.error
        return self.workflows

    def call_webhook(self, path, payload, method="POST"):
        self.calls.append((path, payload, method))
        return {"events": ["standup at 9"]}


# -- translation ------------------------------------------------------------


def test_a_tagged_workflow_becomes_a_tool():
    binding = build_binding(workflow())

    assert binding.spec.name == "get_calendar_events"
    assert binding.spec.description == "Look up calendar events for a day"
    assert binding.spec.parameters["properties"]["day"]["type"] == "string"
    assert binding.webhook_path == "calendar"


def test_notes_may_carry_an_envelope_with_its_own_description():
    notes = json.dumps({"description": "from notes", "parameters": SCHEMA})
    binding = build_binding(workflow(notes=notes, description=""))

    assert binding.spec.description == "from notes"
    assert "day" in binding.spec.parameters["properties"]


def test_workflow_description_wins_over_notes():
    """The workflow field is the more discoverable one, so it takes precedence."""
    notes = json.dumps({"description": "from notes", "parameters": SCHEMA})
    binding = build_binding(workflow(notes=notes, description="from workflow"))

    assert binding.spec.description == "from workflow"


def test_fenced_json_in_notes_is_accepted():
    """People paste JSON into notes boxes inside code fences."""
    binding = build_binding(workflow(notes=f"```json\n{json.dumps(SCHEMA)}\n```"))

    assert "day" in binding.spec.parameters["properties"]


def test_a_tool_with_no_arguments_is_valid():
    binding = build_binding(workflow(notes=""))

    assert binding.spec.parameters == {"type": "object", "properties": {}}


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"node_type": "n8n-nodes-base.cron"}, "no webhook trigger"),
        ({"notes": "{not json"}, "not valid JSON"),
        ({"notes": '{"nope": 1}'}, "neither 'parameters' nor 'properties'"),
        ({"notes": '{"properties": []}'}, "'properties' must be an object"),
        ({"description": "", "notes": json.dumps(SCHEMA)}, "no description"),
        ({"path": ""}, "no path"),
    ],
)
def test_unusable_workflows_raise_schema_error(kwargs, expected):
    with pytest.raises(SchemaError) as exc:
        build_binding(workflow(**kwargs))

    assert expected in str(exc.value)


def test_required_naming_an_undeclared_property_is_rejected():
    """Otherwise the model is asked for a field the schema never describes."""
    notes = json.dumps({"type": "object", "properties": {}, "required": ["day"]})

    with pytest.raises(SchemaError) as exc:
        build_binding(workflow(notes=notes))

    assert "undeclared properties" in str(exc.value)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Get Calendar Events!", "get_calendar_events"),
        ("send-email", "send_email"),
        ("  Search   Notion  ", "search_notion"),
        ("2fa reset", "t_2fa_reset"),
    ],
)
def test_slugify(name, expected):
    assert slugify(name) == expected


def test_slugify_rejects_a_name_with_nothing_usable():
    with pytest.raises(SchemaError):
        slugify("!!!")


def test_parse_notes_on_empty_input():
    notes = parse_notes("   ")

    assert notes.description == ""
    assert notes.schema == {}
    assert notes.is_async is False


# -- registry behaviour -----------------------------------------------------


def test_one_broken_workflow_does_not_hide_the_others():
    """The rule the whole module exists to honour."""
    registry = ToolRegistry(
        client=FakeClient([
            workflow(name="Good One", wf_id="1", path="good"),
            workflow(name="Broken", wf_id="2", notes="{not json"),
            workflow(name="Also Good", wf_id="3", path="also"),
        ]),
        cache_ttl=0,
    )

    discovery = registry.discover()

    assert sorted(discovery.bindings) == ["also_good", "good_one"]
    assert len(discovery.skipped) == 1
    assert "not valid JSON" in discovery.skipped[0].reason


def test_inactive_workflows_are_skipped_with_an_actionable_reason():
    """An inactive workflow's production webhook 404s -- exposing it is a trap."""
    registry = ToolRegistry(client=FakeClient([workflow(active=False)]), cache_ttl=0)

    discovery = registry.discover()

    assert discovery.bindings == {}
    assert "not active" in discovery.skipped[0].reason


def test_name_collisions_resolve_deterministically_by_workflow_id():
    first = workflow(name="Send Email", wf_id="1", path="a")
    second = workflow(name="send-email", wf_id="2", path="b")

    registry = ToolRegistry(client=FakeClient([first, second]), cache_ttl=0)
    reversed_order = ToolRegistry(client=FakeClient([second, first]), cache_ttl=0)

    winner = registry.discover().bindings["send_email"].workflow_id
    assert winner == "1"
    assert reversed_order.discover().bindings["send_email"].workflow_id == "1"
    assert "already taken" in registry.discover().skipped[0].reason


def test_results_are_cached_until_the_ttl_expires():
    client = FakeClient([workflow()])
    registry = ToolRegistry(client=client, cache_ttl=60)

    registry.discover()
    registry.discover()
    assert client.list_count == 1

    registry.discover(force=True)
    assert client.list_count == 2


def test_unreachable_n8n_serves_stale_tools_rather_than_none():
    client = FakeClient([workflow()])
    registry = ToolRegistry(client=client, cache_ttl=0)
    assert len(registry.discover().bindings) == 1

    client.error = N8nError("connection refused")
    assert len(registry.discover().bindings) == 1  # stale beats empty


def test_unreachable_n8n_with_no_cache_degrades_to_no_tools():
    registry = ToolRegistry(client=FakeClient(error=N8nError("refused")), cache_ttl=0)

    assert registry.discover().bindings == {}  # conversation still works


# -- dispatch ---------------------------------------------------------------


def test_dispatch_posts_to_the_right_webhook():
    client = FakeClient([workflow()])
    registry = ToolRegistry(client=client, cache_ttl=60)

    result = registry.dispatch("get_calendar_events", {"day": "tomorrow"})

    assert result == {"events": ["standup at 9"]}
    assert client.calls == [("calendar", {"day": "tomorrow"}, "POST")]


def test_unknown_tool_comes_back_as_a_result_not_an_exception():
    """The model should get the chance to apologise or pick another tool."""
    registry = ToolRegistry(client=FakeClient([workflow()]), cache_ttl=60)

    result = registry.dispatch("nonexistent", {})

    assert "unknown tool" in result["error"]
    assert "get_calendar_events" in result["error"]


def test_dispatch_refetches_before_declaring_a_tool_unknown():
    """A workflow added mid-session should be reachable without a restart."""
    client = FakeClient([])
    registry = ToolRegistry(client=client, cache_ttl=600)
    assert registry.discover().bindings == {}

    client.workflows = [workflow()]
    result = registry.dispatch("get_calendar_events", {"day": "today"})

    assert result == {"events": ["standup at 9"]}


def test_malformed_tool_arguments_are_reported_back_to_the_model():
    registry = ToolRegistry(client=FakeClient([workflow()]), cache_ttl=60)

    result = registry.dispatch(
        "get_calendar_events", {"__parse_error__": "invalid JSON arguments"}
    )

    assert "invalid JSON arguments" in result["error"]


def test_a_failing_workflow_returns_an_error_result():
    class Failing(FakeClient):
        def call_webhook(self, path, payload, method="POST"):
            raise N8nError("n8n webhook calendar: HTTP 404")

    registry = ToolRegistry(client=Failing([workflow()]), cache_ttl=60)

    result = registry.dispatch("get_calendar_events", {"day": "today"})

    assert "404" in result["error"]


# -- confirmation gate ------------------------------------------------------


def destructive_workflow(**kwargs):
    notes = json.dumps({
        "description": "Send an email",
        "destructive": True,
        "parameters": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
        },
    })
    kwargs.setdefault("name", "Send Email")
    kwargs.setdefault("path", "send-email")
    return workflow(notes=notes, description="", **kwargs)


def test_a_destructive_tool_asks_before_acting():
    client = FakeClient([destructive_workflow()])
    registry = ToolRegistry(client=client, cache_ttl=60)

    result = registry.dispatch("send_email", {"to": "dana", "body": "late"})

    assert result["status"] == "confirmation_required"
    assert "dana" in result["action"]
    assert client.calls == []  # nothing was sent


def test_confirming_lets_it_through_once():
    client = FakeClient([destructive_workflow()])
    registry = ToolRegistry(client=client, cache_ttl=60)
    args = {"to": "dana", "body": "late"}

    token = registry.dispatch("send_email", dict(args))["__confirm"]
    registry.dispatch("send_email", {**args, "__confirm": token})

    assert len(client.calls) == 1
    # The token must not reach the workflow itself.
    assert "__confirm" not in client.calls[0][1]


def test_a_token_cannot_be_replayed():
    """Otherwise one yes could fire the same action repeatedly."""
    client = FakeClient([destructive_workflow()])
    registry = ToolRegistry(client=client, cache_ttl=60)
    args = {"to": "dana", "body": "late"}

    token = registry.dispatch("send_email", dict(args))["__confirm"]
    registry.dispatch("send_email", {**args, "__confirm": token})
    replay = registry.dispatch("send_email", {**args, "__confirm": token})

    assert "unknown or already used" in replay["error"]
    assert len(client.calls) == 1


def test_a_token_cannot_authorise_different_arguments():
    """The security-relevant case: confirm one action, execute another."""
    client = FakeClient([destructive_workflow()])
    registry = ToolRegistry(client=client, cache_ttl=60)

    token = registry.dispatch("send_email", {"to": "dana", "body": "late"})["__confirm"]
    swapped = registry.dispatch(
        "send_email", {"to": "everyone", "body": "I quit", "__confirm": token}
    )

    assert "arguments changed" in swapped["error"]
    assert client.calls == []


def test_an_invented_token_is_refused():
    client = FakeClient([destructive_workflow()])
    registry = ToolRegistry(client=client, cache_ttl=60)

    result = registry.dispatch("send_email", {"to": "dana", "__confirm": "made-up"})

    assert "unknown or already used" in result["error"]
    assert client.calls == []


def test_the_confirm_argument_is_declared_in_the_schema():
    """Providers reject arguments a schema does not declare."""
    binding = build_binding(destructive_workflow())

    assert "__confirm" in binding.spec.parameters["properties"]
    assert "confirmation" in binding.spec.description.lower()


def test_non_destructive_tools_are_unaffected():
    client = FakeClient([workflow()])
    registry = ToolRegistry(client=client, cache_ttl=60)

    registry.dispatch("get_calendar_events", {"day": "today"})

    assert len(client.calls) == 1
