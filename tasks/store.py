"""State for work that outlives the conversation that started it.

Deliberately in-memory. The free hosts this runs on have no persistent disk, so
a database here would be a database that quietly resets -- worse than none,
because it would look durable. **n8n is the durable record**: it has the
execution history, and it retries its own callbacks. This store exists to
correlate a callback with the conversation that asked for it, and to make
delivery idempotent.

That means a restart loses pending tasks. The consequence is a missed
notification, not lost work: the workflow still runs to completion in n8n.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["pending", "done", "failed"]

# Tasks are only kept long enough to correlate a callback with its request.
MAX_TASKS = 500


@dataclass
class Task:
    id: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    status: Status = "pending"
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    result: Any = None
    error: str | None = None
    notified: bool = False

    @property
    def duration_s(self) -> float:
        return (self.completed_at or time.time()) - self.created_at


class TaskStore:
    """Thread-safe: Gradio and the callback endpoint touch this concurrently."""

    def __init__(self, max_tasks: int = MAX_TASKS) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self.max_tasks = max_tasks

    def create(self, tool: str, arguments: dict[str, Any] | None = None) -> Task:
        task = Task(id=uuid.uuid4().hex[:16], tool=tool, arguments=arguments or {})
        with self._lock:
            self._tasks[task.id] = task
            self._evict()
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def complete(
        self, task_id: str, result: Any = None, error: str | None = None
    ) -> tuple[Task | None, bool]:
        """Mark a task finished. Returns (task, is_first_completion).

        n8n retries callbacks, so the same completion can arrive several times.
        The second caller gets `False` and must not notify again -- otherwise a
        retry storm becomes a notification storm on someone's phone.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None, False
            if task.status != "pending":
                return task, False

            task.status = "failed" if error else "done"
            task.result = result
            task.error = error
            task.completed_at = time.time()
            return task, True

    def mark_notified(self, task_id: str) -> None:
        with self._lock:
            if task := self._tasks.get(task_id):
                task.notified = True

    def pending(self) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.status == "pending"]

    def _evict(self) -> None:
        """Drop the oldest completed tasks once the store is full."""
        if len(self._tasks) <= self.max_tasks:
            return
        done = sorted(
            (t for t in self._tasks.values() if t.status != "pending"),
            key=lambda t: t.completed_at or 0,
        )
        for task in done[: len(self._tasks) - self.max_tasks]:
            self._tasks.pop(task.id, None)


# One store per process; the callback endpoint and the agent share it.
store = TaskStore()
