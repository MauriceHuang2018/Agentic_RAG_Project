"""Tests for T2.4 — citations + multi-turn memory.

Covers:
  * record_citations resolves document_name + page_no from PG
  * Missing chunks are skipped (warning, no exception)
  * Empty results short-circuit
  * Citation rows preserve rank ordering via insertion order
  * ConversationMemory append/load roundtrip
  * ConversationMemory eviction at max_turns
  * ConversationMemory TTL applied (7-day default)
  * format_history_for_prompt respects max_chars budget + chronology
  * clear() removes the key
  * Constructor rejects invalid ttl / max_turns
"""

from __future__ import annotations

import json
import uuid

import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agentic_rag_project.db.models.base import Base
from agentic_rag_project.db.models.documents import Chunk, Document
from agentic_rag_project.db.models.users import User, Workspace
from agentic_rag_project.db.models.conversations import Conversation, Message
from agentic_rag_project.retrieval_direct.citations import (
    CitationError,
    record_citations,
)
from agentic_rag_project.retrieval_direct.memory import (
    HISTORY_KEY_PREFIX,
    ConversationMemory,
    HistoryTurn,
    MemoryError,
)
from agentic_rag_project.retrieval_direct.search import SearchResult
from agentic_rag_project.retrieval_direct.citations import _chunk_uuid


# ---------------------------------------------------------------------------
# fixtures (citations)
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    """In-memory PG + a workspace + user + doc + 2 chunks + a message."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    user = User(
        id=uuid.uuid4(),
        username="u",
        email="u@x",
        password_hash="x",
    )
    s.add(user)
    s.flush()
    ws = Workspace(id=uuid.uuid4(), name="ws", owner_id=user.id)
    s.add(ws)
    s.flush()
    doc = Document(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        name="policy.pdf",
        format="pdf",
        owner_id=user.id,
    )
    s.add(doc)
    s.flush()
    conv = Conversation(id=uuid.uuid4(), user_id=user.id, workspace_id=ws.id)
    s.add(conv)
    s.flush()
    msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv.id,
        role="assistant",
        content="answer",
    )
    s.add(msg)
    # Two chunks belonging to the doc.
    chunks = []
    for i, page in enumerate([3, 7]):
        cid = _chunk_uuid(f"chunk-{i}")
        c = Chunk(
            id=cid,
            document_id=doc.id,
            chunk_index=i,
            content=f"chunk-{i} content",
            content_hash=f"h-{i}",
            is_parent=False,
            position={"page": page, "section_path": "x"},
        )
        s.add(c)
        chunks.append(c)
    s.commit()
    s.refresh(msg)
    # Stash everything on the session for the tests. Use the ORIGINAL string
    # chunk keys (not the resolved UUIDs) so `_chunk_uuid` produces the same
    # uuid5 as the rows we just inserted.
    s.test_msg_id = msg.id  # type: ignore[attr-defined]
    s.test_doc_id = doc.id  # type: ignore[attr-defined]
    s.test_chunk_keys = [f"chunk-{i}" for i in range(len(chunks))]  # type: ignore[attr-defined]
    try:
        yield s
    finally:
        s.close()


def _hit(chunk_id_str: str, score: float) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id_str,
        document_id="doc-1",
        content="",
        score=score,
        payload={},
    )


# ---------------------------------------------------------------------------
# citations
# ---------------------------------------------------------------------------


def test_record_citations_writes_rows(session: Session) -> None:
    msg_id: uuid.UUID = session.test_msg_id  # type: ignore[attr-defined]
    chunk_ids: list[str] = session.test_chunk_keys  # type: ignore[attr-defined]
    results = [_hit(chunk_ids[0], 0.9), _hit(chunk_ids[1], 0.7)]
    rows = record_citations(session, message_id=msg_id, search_results=results)
    assert len(rows) == 2
    assert rows[0].document_name == "policy.pdf"
    assert rows[0].page_no == 3
    assert rows[1].page_no == 7
    assert rows[0].relevance_score == pytest.approx(0.9)
    assert rows[1].relevance_score == pytest.approx(0.7)


def test_record_citations_empty_results_returns_empty(session: Session) -> None:
    msg_id: uuid.UUID = session.test_msg_id  # type: ignore[attr-defined]
    assert record_citations(session, message_id=msg_id, search_results=[]) == []


def test_record_citations_skips_missing_chunks(session: Session) -> None:
    msg_id: uuid.UUID = session.test_msg_id  # type: ignore[attr-defined]
    chunk_ids: list[str] = session.test_chunk_keys  # type: ignore[attr-defined]
    results = [
        _hit(chunk_ids[0], 0.9),
        _hit("does-not-exist", 0.5),
        _hit(chunk_ids[1], 0.7),
    ]
    rows = record_citations(session, message_id=msg_id, search_results=results)
    # Only the 2 real chunks land; the missing one is skipped.
    assert len(rows) == 2
    assert [r.relevance_score for r in rows] == pytest.approx([0.9, 0.7])


def test_record_citations_preserves_rank_order(session: Session) -> None:
    msg_id: uuid.UUID = session.test_msg_id  # type: ignore[attr-defined]
    chunk_ids: list[str] = session.test_chunk_keys  # type: ignore[attr-defined]
    # Reverse the input order — output must follow input order.
    results = [_hit(chunk_ids[1], 0.7), _hit(chunk_ids[0], 0.9)]
    rows = record_citations(session, message_id=msg_id, search_results=results)
    assert [r.relevance_score for r in rows] == pytest.approx([0.7, 0.9])
    assert rows[0].page_no == 7
    assert rows[1].page_no == 3


# ---------------------------------------------------------------------------
# memory — CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
def redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=False)


@pytest.fixture
def memory(redis: fakeredis.FakeRedis) -> ConversationMemory:
    return ConversationMemory(redis, ttl_seconds=3600, max_turns=5)


def test_constructor_rejects_invalid_ttl(redis: fakeredis.FakeRedis) -> None:
    with pytest.raises(MemoryError):
        ConversationMemory(redis, ttl_seconds=0)
    with pytest.raises(MemoryError):
        ConversationMemory(redis, ttl_seconds=-1)


def test_constructor_rejects_invalid_max_turns(
    redis: fakeredis.FakeRedis,
) -> None:
    with pytest.raises(MemoryError):
        ConversationMemory(redis, max_turns=0)


def test_load_history_empty_for_missing_conversation(
    memory: ConversationMemory,
) -> None:
    assert memory.load_history(uuid.uuid4()) == []


def test_append_then_load_roundtrip(memory: ConversationMemory) -> None:
    conv_id = uuid.uuid4()
    memory.append_turn(conv_id, HistoryTurn(role="user", content="hi"))
    memory.append_turn(
        conv_id, HistoryTurn(role="assistant", content="hello", citation_chunk_ids=["c1"])
    )
    history = memory.load_history(conv_id)
    assert len(history) == 2
    assert history[0].role == "user"
    assert history[1].role == "assistant"
    assert history[1].citation_chunk_ids == ["c1"]


def test_max_turns_evicts_oldest(memory: ConversationMemory) -> None:
    conv_id = uuid.uuid4()
    for i in range(7):
        memory.append_turn(conv_id, HistoryTurn(role="user", content=f"u-{i}"))
    history = memory.load_history(conv_id)
    # max_turns=5 → keep last 5.
    assert len(history) == 5
    assert [t.content for t in history] == ["u-2", "u-3", "u-4", "u-5", "u-6"]


def test_ttl_is_applied(memory: ConversationMemory) -> None:
    conv_id = uuid.uuid4()
    memory.append_turn(conv_id, HistoryTurn(role="user", content="hi"))
    ttl = memory.ttl(conv_id)
    # Within a small delta of the configured 3600s TTL.
    assert 3500 < ttl <= 3600


def test_clear_removes_history(memory: ConversationMemory) -> None:
    conv_id = uuid.uuid4()
    memory.append_turn(conv_id, HistoryTurn(role="user", content="hi"))
    assert memory.history_size(conv_id) == 1
    memory.clear(conv_id)
    assert memory.history_size(conv_id) == 0


def test_corrupt_history_returns_empty(memory: ConversationMemory, redis: fakeredis.FakeRedis) -> None:
    conv_id = uuid.uuid4()
    redis.setex(f"{HISTORY_KEY_PREFIX}{conv_id}", 60, b"not json")
    assert memory.load_history(conv_id) == []


# ---------------------------------------------------------------------------
# format_history_for_prompt
# ---------------------------------------------------------------------------


def test_format_empty_returns_empty(memory: ConversationMemory) -> None:
    assert memory.format_history_for_prompt([]) == ""


def test_format_single_turn(memory: ConversationMemory) -> None:
    out = memory.format_history_for_prompt(
        [HistoryTurn(role="user", content="hi")]
    )
    assert "[User] hi" in out
    assert out.endswith("\n")


def test_format_chronological_order(memory: ConversationMemory) -> None:
    turns = [
        HistoryTurn(role="user", content="first"),
        HistoryTurn(role="assistant", content="second"),
        HistoryTurn(role="user", content="third"),
    ]
    out = memory.format_history_for_prompt(turns)
    assert out.find("first") < out.find("second") < out.find("third")


def test_format_truncates_chronological_window_when_over_budget(
    memory: ConversationMemory,
) -> None:
    # Each turn renders as "[User] " (7) + 50 chars + 1 newline = 58 chars.
    # Newest turns survive; the oldest gets dropped first.
    turns = [HistoryTurn(role="user", content="x" * 50) for _ in range(5)]
    out = memory.format_history_for_prompt(turns, max_chars=250)
    # 250 / 58 = 4.31 → 4 newest turns fit, the first one is dropped.
    assert out.count("[User]") == 4
    # The 4 surviving turns all still render their content.
    assert out.count("x" * 50) == 4


def test_format_budget_too_small_for_any_turn(memory: ConversationMemory) -> None:
    turns = [HistoryTurn(role="user", content="hello world")]
    # Budget smaller than a single line → no output (don't truncate mid-turn).
    assert memory.format_history_for_prompt(turns, max_chars=3) == ""
