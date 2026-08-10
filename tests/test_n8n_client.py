"""Client-level tests, driven through a mock transport rather than a live n8n.

These pin the two n8n API quirks the registry would otherwise trip over.
"""

import httpx
import pytest

from tools.n8n_client import N8nClient, N8nError

SUMMARY = {  # what GET /workflows returns -- note: no "description" key at all
    "id": "1",
    "name": "Get Calendar Events",
    "active": True,
    "nodes": [{"type": "n8n-nodes-base.webhook", "parameters": {"path": "cal"}}],
    "tags": [{"name": "agent-tool"}],
}
FULL = {**SUMMARY, "description": "Look up calendar events for a day"}


def client_with(handler) -> N8nClient:
    transport = httpx.MockTransport(handler)
    return N8nClient(
        base_url="http://n8n.test",
        api_key="k",
        client=httpx.Client(transport=transport),
    )


def test_workflows_are_refetched_when_the_list_omits_the_description():
    """The list endpoint drops `description`; the registry needs it."""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/v1/workflows":
            return httpx.Response(200, json={"data": [SUMMARY]})
        return httpx.Response(200, json=FULL)

    workflows = client_with(handler).list_workflows(tag="agent-tool")

    assert workflows[0].description == "Look up calendar events for a day"
    assert "/api/v1/workflows/1" in seen


def test_no_refetch_when_the_description_is_already_present():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": [FULL]})

    assert client_with(handler).list_workflows(tag="agent-tool")[0].description
    assert calls == ["/api/v1/workflows"]


def test_a_server_that_ignores_the_tag_filter_is_corrected_client_side():
    """Otherwise every workflow in the instance becomes a callable tool."""
    untagged = {**FULL, "id": "2", "name": "Unrelated", "tags": [{"name": "other"}]}

    def handler(request):
        return httpx.Response(200, json={"data": [FULL, untagged]})

    workflows = client_with(handler).list_workflows(tag="agent-tool")

    assert [w.name for w in workflows] == ["Get Calendar Events"]


def test_hydration_failure_keeps_the_summary_rather_than_dropping_the_workflow():
    def handler(request):
        if request.url.path == "/api/v1/workflows":
            return httpx.Response(200, json={"data": [SUMMARY]})
        # 404 rather than 500: a non-retryable status keeps the test fast.
        return httpx.Response(404, text="gone")

    workflows = client_with(handler).list_workflows(tag="agent-tool")

    assert len(workflows) == 1
    assert workflows[0].description == ""


def test_webhook_call_targets_the_production_path():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    result = client_with(handler).call_webhook("cal", {"day": "today"})

    assert seen["url"] == "http://n8n.test/webhook/cal"
    assert result == {"ok": True}


def test_webhook_test_mode_uses_the_editor_path():
    def handler(request):
        return httpx.Response(200, json={"path": str(request.url)})

    result = client_with(handler).call_webhook("cal", {}, test_mode=True)

    assert result["path"] == "http://n8n.test/webhook-test/cal"


def test_a_plain_text_workflow_response_is_still_a_result():
    def handler(request):
        return httpx.Response(200, text="done")

    assert client_with(handler).call_webhook("cal", {}) == "done"


def test_missing_configuration_fails_loudly(monkeypatch):
    """Empty args fall back to config, so the config itself must be empty here."""
    from types import SimpleNamespace

    import tools.n8n_client as module

    monkeypatch.setattr(
        module, "config", SimpleNamespace(n8n_base_url=None, n8n_api_key=None)
    )
    with pytest.raises(N8nError, match="must both be set"):
        N8nClient()
