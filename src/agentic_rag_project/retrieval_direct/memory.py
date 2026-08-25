"""Multi-turn conversation memory (T2.4 second half).

DESIGN 4.2 / TASK T2.4: short-term memory is a Redis-backed sliding
window keyed by conversation id (7-day TTL). The actual langgraph
checkpointer integration (which auto-creates the `checkpoint_*` PG
tables) lands in T3.2; here we provide the data shape and the
prompt-formatting helper that the agent loop and the chat endpoint will
both depend on.

Data layout (Redis)
  `conv:history:<conversation_id>` → JSON list of `HistoryTurn`s
  TTL = 7 days (overridable per instance)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
HISTORY_KEY_PREFIX = "conv:history:"
HISTORY_MAX_TURNS_DEFAULT = 20


class RedisLike(Protocol):
    """Just the surface we touch — lets tests use `fakeredis.FakeRedis`."""

    def get(self, key: str) -> bytes | None: ...

    def setex(
        self, key: str, time: int, value: bytes | str
    ) -> Any: ...

    def delete(self, *keys: str) -> Any: ...

    def ttl(self, key: str) -> int: ...


@dataclass
class HistoryTurn:
    """One visible turn in a conversation."""

    role: str  # 'user' | 'assistant' | 'system'
    content: str
    citation_chunk_ids: list[str] = field(default_factory=list)
    turn_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "HistoryTurn":
        return cls(
            role=str(payload.get("role", "")),
            content=str(payload.get("content", "")),
            citation_chunk_ids=list(payload.get("citation_chunk_ids", [])),
            turn_id=str(payload.get("turn_id", uuid.uuid4())),
        )


class MemoryError(Exception):
    """Raised on memory-store failures."""


def _key(conversation_id: uuid.UUID | str) -> str:
    cid = str(conversation_id)
    return f"{HISTORY_KEY_PREFIX}{cid}"


class ConversationMemory:
    """Redis-backed sliding-window history for a single conversation.

    The class is cheap to instantiate; the `redis` client is shared
    across instances.
    """

    def __init__(
        self,
        redis_client: RedisLike,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_turns: int = HISTORY_MAX_TURNS_DEFAULT,
    ) -> None:
        if ttl_seconds <= 0:
            raise MemoryError("ttl_seconds must be positive")
        if max_turns <= 0:
            raise MemoryError("max_turns must be positive")
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._max_turns = max_turns

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def append_turn(
        self,
        conversation_id: uuid.UUID | str,
        turn: HistoryTurn,
    ) -> None:
        """Append a turn and (re-)apply the TTL.

        If the new turn exceeds `max_turns`, the oldest are evicted first.
        """
        history = self.load_history(conversation_id)
        history.append(turn)
        if len(history) > self._max_turns:
            history = history[-self._max_turns :]
        self._write(conversation_id, history)

    def load_history(
        self, conversation_id: uuid.UUID | str
    ) -> list[HistoryTurn]:
        """Return all stored turns in chronological order. Empty if absent."""
        raw = self._redis.get(_key(conversation_id))
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "history key %s has corrupt json: %s", _key(conversation_id), exc
            )
            return []
        if not isinstance(payload, list):
            return []
        return [HistoryTurn.from_json(item) for item in payload if isinstance(item, dict)]

    def clear(self, conversation_id: uuid.UUID | str) -> None:
        self._redis.delete(_key(conversation_id))

    def history_size(self, conversation_id: uuid.UUID | str) -> int:
        return len(self.load_history(conversation_id))

    def ttl(self, conversation_id: uuid.UUID | str) -> int:
        """Return remaining TTL in seconds (-2 if missing, -1 if no TTL)."""
        return int(self._redis.ttl(_key(conversation_id)))

    # ------------------------------------------------------------------
    # prompt formatting
    # ------------------------------------------------------------------

    def format_history_for_prompt(
        self,
        turns: Iterable[HistoryTurn],
        *,
        max_chars: int = 4000,
    ) -> str:
        """Render history into a prompt-ready string with a char budget.

        Walks turns newest-to-oldest and keeps as many as fit in the
        budget, then re-emits in chronological order. The result is
        always terminated with a trailing newline so callers can
        concatenate it with the next prompt section without an extra
        newline.
        """
        turns_list = list(turns)
        if not turns_list:
            return ""

        # Decide which tail to keep, newest first.
        kept: list[HistoryTurn] = []
        budget = max_chars
        for turn in reversed(turns_list):
            line = self._format_turn_line(turn)
            if len(line) + 1 > budget:
                break
            kept.append(turn)
            budget -= len(line) + 1
        kept.reverse()

        if not kept:
            return ""

        lines = [self._format_turn_line(t) for t in kept]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _format_turn_line(turn: HistoryTurn) -> str:
        # Compact one-line role marker, optional inline citation hint
        # so the model can pattern-match against chunk ids it has seen.
        marker = {
            "user": "[User]",
            "assistant": "[Assistant]",
            "system": "[System]",
        }.get(turn.role, f"[{turn.role}]")
        return f"{marker} {turn.content}".rstrip()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _write(
        self, conversation_id: uuid.UUID | str, history: list[HistoryTurn]
    ) -> None:
        payload = json.dumps([t.to_json() for t in history]).encode("utf-8")
        self._redis.setex(_key(conversation_id), self._ttl, payload)