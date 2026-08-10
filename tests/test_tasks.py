"""Async handoff: the store's idempotency and Telegram's degradation ladder.

Both exist because of retries. n8n retries callbacks, so a completion can
arrive several times; and Telegram rejects audio formats we cannot transcode
without shipping ffmpeg.
"""

import httpx
import pytest

from tasks.callbacks import Delivery, TelegramNotifier, format_completion
from tasks.store import TaskStore


# -- store ------------------------------------------------------------------


def test_a_task_starts_pending_and_completes_once():
    store = TaskStore()
    task = store.create("slow_report", {"month": "July"})

    assert task.status == "pending"
    assert store.pending() == [task]

    completed, first = store.complete(task.id, result={"rows": 12})
    assert first is True
    assert completed.status == "done"
    assert store.pending() == []


def test_duplicate_callbacks_are_absorbed():
    """n8n retries; a retry must not become a second notification."""
    store = TaskStore()
    task = store.create("slow_report")

    _, first = store.complete(task.id, result="a")
    _, second = store.complete(task.id, result="b")

    assert (first, second) == (True, False)
    assert store.get(task.id).result == "a"  # the first result stands


def test_an_error_completion_is_marked_failed():
    store = TaskStore()
    task = store.create("slow_report")

    completed, _ = store.complete(task.id, error="workflow blew up")

    assert completed.status == "failed"
    assert completed.error == "workflow blew up"


def test_callbacks_for_unknown_tasks_are_reported_not_raised():
    """After a restart the id is gone, but n8n will still call."""
    task, first = TaskStore().complete("nope")

    assert task is None and first is False


def test_completed_tasks_are_evicted_but_pending_ones_survive():
    store = TaskStore(max_tasks=3)
    keep = store.create("still_running")
    for i in range(5):
        done = store.create(f"done_{i}")
        store.complete(done.id, result=i)

    assert store.get(keep.id) is not None
    assert len(store.pending()) == 1


# -- message formatting -----------------------------------------------------


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"message": "12 rows exported"}, "12 rows exported"),
        ({"summary": "all done"}, "all done"),
        (["one", "two"], "one; two"),
        ("plain text", "plain text"),
    ],
)
def test_completion_messages_unwrap_common_result_shapes(result, expected):
    assert expected in format_completion("slow_report", result, None, 65)


def test_completion_message_reports_duration_and_failure():
    text = format_completion("slow_report", None, "timed out", 125)

    assert "timed out" in text
    assert "2m 5s" in text


def test_empty_results_still_produce_a_sentence():
    """Silence would read as a bug; say it finished with nothing."""
    assert "no output" in format_completion("slow_report", None, None, 3)


# -- telegram ---------------------------------------------------------------


class FakeTTS:
    def __init__(self, audio=b"RIFFfake"):
        self.audio = audio

    def synthesize(self, text, trace=None):
        from agent.tts import Speech

        return Speech(text=text, audio=self.audio, engine="groq")


def notifier_with(handler, tts=None) -> TelegramNotifier:
    return TelegramNotifier(
        token="t", chat_id="1", tts=tts or FakeTTS(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_text_and_voice_are_both_delivered_when_telegram_accepts():
    seen = []

    def handler(request):
        seen.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True})

    delivery = notifier_with(handler).notify("done")

    assert delivery.text_sent is True
    assert delivery.audio_method == "sendVoice"
    assert seen == ["sendMessage", "sendVoice"]


def test_audio_falls_back_when_telegram_rejects_the_format():
    """sendVoice wants OGG/Opus; Groq returns WAV and we do not ship ffmpeg."""
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "sendVoice":
            return httpx.Response(200, json={"ok": False, "description": "wrong file format"})
        return httpx.Response(200, json={"ok": True})

    delivery = notifier_with(handler).notify("done")

    assert delivery.text_sent is True
    assert delivery.audio_method == "sendAudio"


def test_the_answer_survives_total_audio_failure():
    def handler(request):
        if request.url.path.endswith("sendMessage"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": False, "description": "nope"})

    delivery = notifier_with(handler).notify("done")

    assert delivery.text_sent is True
    assert delivery.audio_method is None


def test_a_browser_only_tts_sends_text_alone():
    class NoAudio(FakeTTS):
        def __init__(self):
            super().__init__(audio=None)

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    delivery = notifier_with(handler, tts=NoAudio()).notify("done")

    assert delivery.text_sent is True
    assert delivery.audio_method is None


def test_delivery_failure_never_raises():
    """A notification failure must not fail the callback and trigger a retry."""
    def handler(request):
        raise httpx.ConnectError("no route")

    delivery = notifier_with(handler).notify("done")

    assert delivery.ok is False
    assert "ConnectError" in delivery.error or "no route" in delivery.error


def test_an_unconfigured_notifier_says_so_instead_of_failing():
    delivery = TelegramNotifier(token=None, chat_id=None).notify("done")

    assert delivery.ok is False
    assert "TELEGRAM" in delivery.error
    assert isinstance(delivery, Delivery)
