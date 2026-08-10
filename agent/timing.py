"""Per-hop latency instrumentation.

The latency budget is a headline number for this project, and it cannot be
retrofitted -- if the hops are not timed from the first commit, the p95 in the
README is a guess. So every network hop in the voice loop records a span here,
and the eval harness aggregates them.

    trace = Trace("conversation")
    with trace.span("stt"):
        text = stt.transcribe(audio)
    with trace.span("llm"):
        reply = llm.chat(messages)
    print(trace.summary())     # stt 412ms | llm 730ms | total 1142ms

Spans are wall-clock and nest freely; `Trace.spans` keeps them in completion
order with their depth, which is what a Langfuse waterfall wants in Phase 5.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class Span:
    name: str
    duration_ms: float
    depth: int
    error: str | None = None
    meta: dict[str, object] = field(default_factory=dict)


@dataclass
class Trace:
    """Collects spans for one conversation turn."""

    name: str = "turn"
    spans: list[Span] = field(default_factory=list)
    _depth: int = 0

    @contextmanager
    def span(self, name: str, **meta: object) -> Iterator[Span]:
        """Time a block. Records the span even when the block raises."""
        started = time.perf_counter()
        self._depth += 1
        record = Span(name=name, duration_ms=0.0, depth=self._depth - 1, meta=dict(meta))
        try:
            yield record
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.duration_ms = (time.perf_counter() - started) * 1000
            self._depth -= 1
            self.spans.append(record)

    def total_ms(self) -> float:
        """Wall time of the top-level spans only, so nesting is not double-counted."""
        return sum(s.duration_ms for s in self.spans if s.depth == 0)

    def by_hop(self) -> dict[str, float]:
        """Total ms per span name -- the shape the eval scorecard reports."""
        totals: dict[str, float] = {}
        for span in self.spans:
            totals[span.name] = totals.get(span.name, 0.0) + span.duration_ms
        return totals

    def summary(self) -> str:
        parts = [f"{name} {ms:.0f}ms" for name, ms in self.by_hop().items()]
        parts.append(f"total {self.total_ms():.0f}ms")
        return " | ".join(parts)


@contextmanager
def stopwatch() -> Iterator[list[float]]:
    """Standalone timer for code with no Trace in scope.

    Yields a one-element list so the elapsed value survives the block:

        with stopwatch() as elapsed:
            ...
        print(elapsed[0])
    """
    holder = [0.0]
    started = time.perf_counter()
    try:
        yield holder
    finally:
        holder[0] = (time.perf_counter() - started) * 1000
