import time

import pytest

from agent.timing import Trace, stopwatch


def test_records_one_span_per_hop():
    trace = Trace()
    with trace.span("stt"):
        time.sleep(0.01)
    with trace.span("llm"):
        time.sleep(0.01)

    assert set(trace.by_hop()) == {"stt", "llm"}
    assert trace.total_ms() > 0


def test_nested_spans_are_not_double_counted():
    """total_ms is wall time, so a tool span inside the llm span must not add to it."""
    trace = Trace()
    with trace.span("llm"):
        time.sleep(0.01)
        with trace.span("tool"):
            time.sleep(0.01)

    assert trace.total_ms() < sum(trace.by_hop().values())
    assert [s.depth for s in trace.spans] == [1, 0]


def test_span_is_recorded_even_when_the_block_raises():
    trace = Trace()
    with pytest.raises(ValueError), trace.span("llm"):
        raise ValueError("kaboom")

    assert len(trace.spans) == 1
    assert "kaboom" in trace.spans[0].error


def test_stopwatch_survives_its_block():
    with stopwatch() as elapsed:
        time.sleep(0.01)
    assert elapsed[0] >= 10
