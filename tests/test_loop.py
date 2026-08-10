"""The loop's job is to always end in something sayable, and to terminate.

Both are tested with a scripted provider rather than a live model, so the
failure paths are exercised deterministically.
"""

from agent.loop import GIVE_UP_REPLY, AgentLoop
from agent.providers import Completion, LLMError, ToolCall, ToolSpec

SPEC = ToolSpec(name="get_calendar_events", description="d", parameters={"type": "object"})


class ScriptedLLM:
    """Returns queued completions; records what it was asked."""

    name = "scripted"

    def __init__(self, *completions, error=None):
        self.queue = list(completions)
        self.error = error
        self.calls = []

    def chat(self, messages, tools=None, system=None, trace=None):
        self.calls.append({"messages": list(messages), "tools": tools, "system": system})
        if self.error:
            raise self.error
        return self.queue.pop(0) if self.queue else Completion(text="done")

    def assistant_message(self, completion):
        return {"role": "assistant", "content": completion.text, "_calls": completion.tool_calls}

    def tool_result_message(self, call, content):
        return {"role": "tool", "tool_call_id": call.id, "content": content}


class FakeRegistry:
    def __init__(self, specs=(SPEC,), result=None):
        self._specs = list(specs)
        self.result = result if result is not None else {"events": ["standup"]}
        self.dispatched = []

    def specs(self, force=False):
        return self._specs

    def dispatch(self, name, arguments):
        self.dispatched.append((name, arguments))
        return self.result


def call(name="get_calendar_events", **args):
    return ToolCall(id="c1", name=name, arguments=args, raw_arguments="{}")


def test_a_plain_answer_returns_immediately():
    llm = ScriptedLLM(Completion(text="Nothing on tomorrow."))
    result = AgentLoop(llm=llm, registry=FakeRegistry()).run("what's on tomorrow?")

    assert result.reply == "Nothing on tomorrow."
    assert result.iterations == 1
    assert result.invocations == []


def test_a_tool_call_is_executed_and_fed_back():
    llm = ScriptedLLM(
        Completion(text="", tool_calls=[call(day="tomorrow")]),
        Completion(text="You have standup at nine."),
    )
    registry = FakeRegistry()

    result = AgentLoop(llm=llm, registry=registry).run("what's on tomorrow?")

    assert result.reply == "You have standup at nine."
    assert registry.dispatched == [("get_calendar_events", {"day": "tomorrow"})]
    assert result.tools_used == ["get_calendar_events"]
    assert result.iterations == 2
    # The tool result must reach the model on the second call.
    assert any(m.get("role") == "tool" for m in llm.calls[1]["messages"])


def test_parallel_tool_calls_all_run_before_the_next_turn():
    llm = ScriptedLLM(
        Completion(text="", tool_calls=[call(day="today"), call(day="tomorrow")]),
        Completion(text="Two days checked."),
    )
    registry = FakeRegistry()

    AgentLoop(llm=llm, registry=registry).run("check both days")

    assert len(registry.dispatched) == 2


def test_the_loop_terminates_when_the_model_will_not_stop():
    """Without the cap this runs until the rate limit stops it."""
    llm = ScriptedLLM(*[Completion(text="", tool_calls=[call()]) for _ in range(20)])

    result = AgentLoop(llm=llm, registry=FakeRegistry(), max_iterations=3).run("go")

    assert result.reply == GIVE_UP_REPLY
    assert result.iterations == 3
    assert "max_iterations=3" in result.error


def test_an_llm_failure_still_produces_something_sayable():
    llm = ScriptedLLM(error=LLMError("groq: HTTP 429"))

    result = AgentLoop(llm=llm, registry=FakeRegistry()).run("hello")

    assert "could not reach" in result.reply.lower()
    assert "429" in result.error
    assert not result.reply.startswith("Traceback")


def test_a_failing_tool_is_reported_not_raised():
    llm = ScriptedLLM(
        Completion(text="", tool_calls=[call()]),
        Completion(text="That did not work, sorry."),
    )
    registry = FakeRegistry(result={"error": "calendar failed: HTTP 404"})

    result = AgentLoop(llm=llm, registry=registry).run("check")

    assert result.invocations[0].failed is True
    assert result.reply == "That did not work, sorry."


def test_an_empty_model_reply_still_says_something():
    """TTS given an empty string produces silence, which reads as a hang."""
    result = AgentLoop(llm=ScriptedLLM(Completion(text="   ")), registry=FakeRegistry()).run("hi")

    assert result.reply == GIVE_UP_REPLY


def test_tools_are_offered_only_when_some_exist():
    llm = ScriptedLLM(Completion(text="I have no tools for that."))

    AgentLoop(llm=llm, registry=FakeRegistry(specs=[])).run("do something")

    assert llm.calls[0]["tools"] is None


def test_history_is_carried_into_the_next_turn():
    llm = ScriptedLLM(Completion(text="Yes."))
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]

    result = AgentLoop(llm=llm, registry=FakeRegistry()).run("and now?", history=history)

    assert [m["content"] for m in llm.calls[0]["messages"]][:2] == ["earlier", "ok"]
    assert result.messages[-1]["content"] == "and now?"


def test_the_system_prompt_forbids_markdown():
    """The reply is spoken, so formatting characters get read out or mangled."""
    llm = ScriptedLLM(Completion(text="ok"))

    AgentLoop(llm=llm, registry=FakeRegistry()).run("hi")

    assert "markdown" in llm.calls[0]["system"].lower()
