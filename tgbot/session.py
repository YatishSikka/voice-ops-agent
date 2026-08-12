"""Per-chat conversation state, and who is allowed to talk to the bot.

One Telegram chat is one ongoing conversation, so history is keyed by chat id
rather than held globally -- otherwise two people talking to the same bot would
share a transcript.

History is trimmed, but never mid-exchange. Cutting between an assistant's tool
call and the tool result that answers it leaves a transcript both Groq and
Anthropic reject, so trimming only ever cuts at a user message.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from config import config

# Roughly a dozen exchanges. Long enough for follow-ups like "what about
# Friday?", short enough that a long chat does not creep toward the token limit.
MAX_HISTORY_MESSAGES = 40

# A conversation nobody has touched in this long starts fresh.
SESSION_IDLE_S = 60 * 60 * 6


@dataclass
class Session:
    chat_id: int
    history: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

    @property
    def idle_s(self) -> float:
        return time.time() - self.updated_at


def trim_history(history: list[dict[str, Any]], limit: int = MAX_HISTORY_MESSAGES) -> list[dict[str, Any]]:
    """Keep the tail, cutting only at a user message.

    A transcript that begins with a tool result -- or with an assistant turn
    whose tool call was dropped -- is rejected by the providers, so the cut
    point matters more than the exact length.
    """
    if len(history) <= limit:
        return history

    tail = history[-limit:]
    for index, message in enumerate(tail):
        if message.get("role") == "user" and not _is_tool_result(message):
            return tail[index:]
    # No clean boundary in the tail: start over rather than send something broken.
    return []


def _is_tool_result(message: dict[str, Any]) -> bool:
    """Anthropic puts tool results in a *user* message, which is not a turn."""
    content = message.get("content")
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    return False


class SessionStore:
    """Thread-safe: the poll loop and the callback endpoint both reach in."""

    def __init__(self, idle_timeout_s: float = SESSION_IDLE_S) -> None:
        self._sessions: dict[int, Session] = {}
        self._lock = threading.Lock()
        self.idle_timeout_s = idle_timeout_s

    def get(self, chat_id: int) -> Session:
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None or session.idle_s > self.idle_timeout_s:
                session = Session(chat_id=chat_id)
                self._sessions[chat_id] = session
            return session

    def update(self, chat_id: int, history: list[dict[str, Any]]) -> None:
        with self._lock:
            session = self._sessions.setdefault(chat_id, Session(chat_id=chat_id))
            session.history = trim_history(history)
            session.touch()

    def reset(self, chat_id: int) -> None:
        with self._lock:
            self._sessions[chat_id] = Session(chat_id=chat_id)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


sessions = SessionStore()


def is_allowed(chat_id: int | str) -> bool:
    """Whether this chat may use the agent.

    A Telegram bot username is public, so anyone who finds it can message it.
    The tools here read and write a real calendar, which makes an open bot a
    data leak rather than a demo. `TELEGRAM_ALLOWED_CHATS` is a comma-separated
    allowlist; unset means only `TELEGRAM_CHAT_ID`, and a bot with neither
    refuses everyone rather than serving everyone.
    """
    allowed = {
        chat.strip()
        for chat in (config.telegram_allowed_chats or "").split(",")
        if chat.strip()
    }
    if not allowed and config.telegram_chat_id:
        allowed = {str(config.telegram_chat_id)}
    return str(chat_id) in allowed
