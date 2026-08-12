"""The HTTP surface n8n calls when a background workflow finishes.

Shared by `bot.py` and the Gradio dev UI so there is one implementation of the
completion path. It binds locally and never needs exposing to the internet:
Telegram is reached by outbound polling, and n8n runs alongside it.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request

from tasks.callbacks import TelegramNotifier, format_completion
from tasks.store import store

log = logging.getLogger("callbacks")

api = FastAPI(title="Voice-Ops Agent")

_notifier: TelegramNotifier | None = None


def notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


@api.post("/tasks/{task_id}/complete")
async def complete_task(task_id: str, request: Request) -> dict[str, object]:
    """Receive a finished task and notify whoever asked for it.

    Always answers 200 once the task is known. n8n retries non-2xx responses,
    and a retry here would mean a second notification for work that already
    succeeded -- so duplicates are absorbed rather than argued with.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a workflow may post nothing at all
        body = {}

    error = body.get("error") if isinstance(body, dict) else None
    result = body.get("result", body) if isinstance(body, dict) else body

    task, first = store.complete(task_id, result=result, error=error)
    if task is None:
        # Unknown id: most likely this process restarted. Say so rather than
        # 404, so n8n stops retrying something no one can act on.
        log.warning("Callback for unknown task %s", task_id)
        return {"ok": False, "reason": "unknown task id"}

    if not first:
        log.info("Duplicate callback for task %s ignored", task_id)
        return {"ok": True, "duplicate": True}

    message = format_completion(task.tool, task.result, task.error, task.duration_s)
    # Back to the chat that asked, not to a globally configured one.
    delivery = notifier().notify(message, chat_id=task.chat_id)
    store.mark_notified(task_id)
    log.info(
        "Task %s delivered to %s: text=%s audio=%s",
        task_id, task.chat_id, delivery.text_sent, delivery.audio_method,
    )

    return {"ok": True, "notified": delivery.text_sent, "audio": delivery.audio_method}


@api.get("/healthz")
async def healthz() -> dict[str, object]:
    return {"ok": True, "pending_tasks": len(store.pending())}
