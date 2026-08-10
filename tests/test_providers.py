"""The provider layer earns its keep by hiding vendor differences in history
shape, so those conversions are what these tests pin down.

The adapters are built with __new__ to skip the API-key check in __init__ --
none of this touches the network.
"""

import pytest

from agent.providers import (
    ClaudeProvider,
    Completion,
    GroqProvider,
    ToolCall,
    _parse_arguments,
)


@pytest.fixture
def call():
    return ToolCall(
        id="call_1",
        name="get_calendar_events",
        arguments={"day": "tomorrow"},
        raw_arguments='{"day":"tomorrow"}',
    )


@pytest.fixture
def groq():
    provider = GroqProvider.__new__(GroqProvider)
    provider.model = "llama-3.3-70b-versatile"
    return provider


@pytest.fixture
def claude():
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider.model = "claude-sonnet-5"
    return provider


def test_groq_parses_tool_calls_and_usage(groq):
    completion = groq._to_completion(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "f", "arguments": '{"a": 1}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "model": "llama-3.3-70b-versatile",
        }
    )

    assert completion.text == ""
    assert completion.tool_calls[0].arguments == {"a": 1}
    assert completion.usage.prompt_tokens == 10


def test_claude_parses_text_and_tool_use(claude):
    completion = claude._to_completion(
        {
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 1},
            "stop_reason": "tool_use",
        }
    )

    assert completion.text == "checking"
    assert completion.tool_calls[0].arguments == {"a": 1}
    assert completion.usage.prompt_tokens == 5


def test_tool_results_take_each_vendors_shape(groq, claude, call):
    """The divergence the interface exists to hide."""
    assert groq.tool_result_message(call, "{}") == {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "get_calendar_events",
        "content": "{}",
    }

    claude_result = claude.tool_result_message(call, "{}")
    assert claude_result["role"] == "user"
    assert claude_result["content"][0]["type"] == "tool_result"
    assert claude_result["content"][0]["tool_use_id"] == "call_1"


def test_assistant_message_round_trips_tool_calls(groq, claude, call):
    completion = Completion(text="checking", tool_calls=[call])

    groq_message = groq.assistant_message(completion)
    assert groq_message["tool_calls"][0]["function"]["arguments"] == '{"day":"tomorrow"}'

    claude_message = claude.assistant_message(completion)
    assert [b["type"] for b in claude_message["content"]] == ["text", "tool_use"]
    assert claude_message["content"][1]["input"] == {"day": "tomorrow"}


def test_claude_merges_parallel_tool_results_into_one_turn(claude, call):
    """Anthropic wants alternating roles; two parallel tool calls would break it."""
    result = claude.tool_result_message(call, "{}")

    merged = claude._merge_adjacent([result, result, {"role": "assistant", "content": "done"}])

    assert len(merged) == 2
    assert len(merged[0]["content"]) == 2
    assert merged[1]["role"] == "assistant"


def test_malformed_tool_arguments_come_back_as_data_not_an_exception():
    """A bad JSON blob should be handed back to the model, not crash the turn."""
    parsed = _parse_arguments('{"a": ', "f")

    assert "__parse_error__" in parsed


def test_non_object_arguments_are_wrapped():
    assert _parse_arguments("42", "f") == {"value": 42}
    assert _parse_arguments("", "f") == {}
