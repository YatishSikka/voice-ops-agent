"""Telegram transport and per-chat session state.

Driven through a mock transport, so none of this touches the real API.
"""

import httpx
import pytest

from tgbot.client import TelegramClient, TelegramError
from tgbot.session import MAX_HISTORY_MESSAGES, SessionStore, trim_history


def client_with(handler) -> TelegramClient:
    return TelegramClient(token="t", client=httpx.Client(transport=httpx.MockTransport(handler)))


def update(update_id=1, text=None, voice=None, chat_id=42):
    message = {"message_id": 7, "chat": {"id": chat_id}, "from": {"username": "yatish"}}
    if text:
        message["text"] = text
    if voice:
        message["voice"] = voice
    return {"update_id": update_id, "message": message}


# -- receiving --------------------------------------------------------------


def test_text_and_voice_messages_are_both_recognised():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": [
            update(1, text="what's on my calendar"),
            update(2, voice={"file_id": "abc", "duration": 3}),
        ]})

    messages = client_with(handler).get_updates()

    assert messages[0].text == "what's on my calendar"
    assert messages[0].is_voice is False
    assert messages[1].is_voice is True
    assert messages[1].voice_file_id == "abc"


def test_offset_is_sent_so_messages_are_not_redelivered():
    """Without acknowledging, Telegram replays the same update forever."""
    seen = {}

    def handler(request):
        seen["offset"] = request.url.params.get("offset")
        return httpx.Response(200, json={"ok": True, "result": []})

    client_with(handler).get_updates(offset=99)

    assert seen["offset"] == "99"


def test_updates_without_a_message_are_ignored():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 1, "edited_message": {"text": "hi"}},
            update(2, text="real"),
        ]})

    assert [m.text for m in client_with(handler).get_updates()] == ["real"]


def test_commands_are_flagged():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": [update(1, text="/tools")]})

    assert client_with(handler).get_updates()[0].is_command is True


def test_voice_downloads_are_renamed_for_groq():
    """Telegram serves .oga; Groq matches on extension and knows .ogg."""
    def handler(request):
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/file_1.oga"}})
        return httpx.Response(200, content=b"OggS-audio-bytes")

    audio, filename = client_with(handler).download_file("abc")

    assert audio == b"OggS-audio-bytes"
    assert filename.endswith(".ogg")


def test_a_rejected_call_raises():
    def handler(request):
        return httpx.Response(200, json={"ok": False, "description": "Unauthorized"})

    with pytest.raises(TelegramError, match="Unauthorized"):
        client_with(handler).get_updates()


# -- sending ----------------------------------------------------------------


def test_voice_falls_back_through_the_delivery_methods():
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method in ("sendVoice", "sendAudio"):
            return httpx.Response(200, json={"ok": False, "description": "wrong format"})
        return httpx.Response(200, json={"ok": True})

    assert client_with(handler).send_voice(1, b"audio") == "sendDocument"


def test_send_voice_reports_total_failure_rather_than_raising():
    """The caller falls back to text; an exception here would lose the reply."""
    def handler(request):
        return httpx.Response(200, json={"ok": False, "description": "nope"})

    assert client_with(handler).send_voice(1, b"audio") is None


def test_chat_action_failures_are_swallowed():
    """It is a typing indicator; it must never break a turn."""
    def handler(request):
        raise httpx.ConnectError("down")

    client_with(handler).send_chat_action(1)  # must not raise


# -- sessions ---------------------------------------------------------------


def test_each_chat_gets_its_own_conversation():
    store = SessionStore()
    store.update(1, [{"role": "user", "content": "mine"}])

    assert store.get(2).history == []
    assert store.get(1).history[0]["content"] == "mine"


def test_reset_clears_only_that_chat():
    store = SessionStore()
    store.update(1, [{"role": "user", "content": "a"}])
    store.update(2, [{"role": "user", "content": "b"}])

    store.reset(1)

    assert store.get(1).history == []
    assert store.get(2).history != []


def test_an_idle_conversation_starts_fresh():
    """Age the session explicitly rather than sleeping -- the clock's
    granularity on Windows makes a zero timeout unreliable."""
    store = SessionStore(idle_timeout_s=60)
    store.update(1, [{"role": "user", "content": "old"}])
    store.get(1).updated_at -= 120

    assert store.get(1).history == []


def test_short_history_is_untouched():
    history = [{"role": "user", "content": str(i)} for i in range(5)]

    assert trim_history(history) == history


def test_trimming_cuts_at_a_user_message():
    """Starting a transcript on a tool result is rejected by the providers."""
    history = []
    for i in range(MAX_HISTORY_MESSAGES + 10):
        history.append({"role": "user", "content": f"q{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})

    trimmed = trim_history(history)

    assert len(trimmed) <= MAX_HISTORY_MESSAGES
    assert trimmed[0]["role"] == "user"


def test_trimming_never_starts_on_an_anthropic_tool_result():
    """Anthropic puts tool results in a user message -- not a real turn."""
    tool_result = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}],
    }
    history = [tool_result] * (MAX_HISTORY_MESSAGES + 5)
    history.append({"role": "user", "content": "a real question"})

    trimmed = trim_history(history)

    assert trimmed == [] or trimmed[0]["content"] == "a real question"


def test_a_tail_with_no_clean_boundary_starts_over():
    """Better an empty history than one the provider will reject."""
    history = [{"role": "assistant", "content": "x"}] * (MAX_HISTORY_MESSAGES + 5)

    assert trim_history(history) == []
