"""Tests for the feedback pipeline (T4.2).

Covers the rule-based cascade in `AutoAttributor`, the ticket
state machine, the persistence layer, the orchestration service,
and the FastAPI routes.

Test files:
  * test_categories.py        — DEFAULT_CATEGORIES preset
  * test_ticket_state.py      — TicketStatus transitions
  * test_scope.py             — is_ambiguous / is_out_of_scope
  * test_cutoff.py            — has_boundary_cutoff
  * test_conflict.py          — has_document_conflict / is_document_expired
  * test_attributor.py        — AutoAttributor cascade
  * test_repository.py        — FeedbackRepository (SQLite in-process)
  * test_service.py           — FeedbackService.submit
  * test_feedback_router.py   — FastAPI /feedback/* routes
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agentic_rag_project.api_gateway import chat_router as _cr_module  # noqa: F401
from agentic_rag_project.api_gateway import feedback_router
from agentic_rag_project.api_gateway.dependencies import UserContext
from agentic_rag_project.feedback import (
    DEFAULT_CATEGORIES,
    DEFAULT_TICKET_STATUSES,
    SYSTEM_TICKET_STATUS_KEYS,
    AttributionError,
    AttributionResult,
    AttributionStatus,
    AutoAttributor,
    CutoffResult,
    DocumentCheckResult,
    DuplicateCategoryKeyError,
    DuplicateTicketStatusKeyError,
    Feedback,
    FeedbackAttribution,
    FeedbackCategory,
    FeedbackRating,
    FeedbackRepository,
    FeedbackService,
    FeedbackTag,
    FeedbackTicket,
    HeuristicResult,
    InvalidTicketTransition,
    SubmitResult,
    TicketStatus,           # ORM class
    TicketStatusKey,        # Python enum of status keys
    TransitionResult,
    assert_transition,
    can_transition,
    find_by_key,
    has_boundary_cutoff,
    has_document_conflict,
    is_ambiguous,
    is_document_expired,
    is_out_of_scope,
    is_terminal,
    next_status,
)
from agentic_rag_project.feedback.attributor import _safe_float
from agentic_rag_project.feedback.models import _Base

# M5 — F2 service-layer workspace check queries `messages.conversation
# .workspace_id`. The legacy `_Base` only declares feedback tables, so
# the engine fixture would fail with `no such table: messages` for any
# test that calls `service.submit`. `_M5_MIN_TABLES` is a tiny
# declarative that owns just `workspaces`, `conversations`, `messages`,
# `users` (the minimum for the workspace check to run).
from tests._m5_min_meta import (  # noqa: E402
    Conversation as _M5Conv,
    Message as _M5Msg,
    Workspace as _M5Ws,
    _M5_MIN_TABLES,
)


def _seed_message_for_f2(
    session: Any,
    *,
    message_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """M5 F2 helper — seed a Message + Conversation + Workspace row
    matching `(message_id, workspace_id)` so the service-layer
    defense-in-depth check (`message.conversation.workspace_id ==
    workspace_id`) passes in legacy tests.

    The legacy tests were authored before F2 existed and call
    `service.submit(message_id=uuid.uuid4(), workspace_id=uuid.uuid4())`
    without creating the corresponding messages row. After F2 they
    must pre-seed the row, otherwise `session.get(Message, mid)`
    returns None and `submit()` raises LookupError → 404.

    Use this at the top of any legacy test that exercises
    `service.submit` or `POST /feedback`.
    """
    conv_id = uuid.uuid4()
    session.add(_M5Ws(id=workspace_id))
    session.add(_M5Conv(id=conv_id, workspace_id=workspace_id))
    session.add(_M5Msg(id=message_id, conversation_id=conv_id, role="assistant", content=""))
    session.flush()


# ===========================================================================
# test_categories.py
# ===========================================================================


class TestDefaultCategories:
    def test_has_five_builtins(self) -> None:
        keys = {s.key for s in DEFAULT_CATEGORIES}
        assert keys == {"retrieval", "chunking", "generation", "knowledge", "user_query"}

    def test_each_has_zh_name(self) -> None:
        for spec in DEFAULT_CATEGORIES:
            assert spec.name_zh
            assert spec.description

    def test_find_by_key(self) -> None:
        spec = find_by_key("retrieval")
        assert spec is not None
        assert spec.name_zh == "检索问题"
        assert find_by_key("nope") is None


# ===========================================================================
# test_ticket_state.py
# ===========================================================================


class TestTicketTransitions:
    def test_can_transition_legal(self) -> None:
        assert can_transition(TicketStatusKey.COLLECTED, TicketStatusKey.PENDING_ATTRIBUTION)
        assert can_transition(TicketStatusKey.PENDING_ATTRIBUTION, TicketStatusKey.ATTRIBUTED)
        assert can_transition(TicketStatusKey.ATTRIBUTED, TicketStatusKey.CLOSED)
        assert can_transition(TicketStatusKey.ATTRIBUTED, TicketStatusKey.PENDING)
        assert can_transition(TicketStatusKey.PENDING, TicketStatusKey.IN_VERIFICATION)
        assert can_transition(TicketStatusKey.IN_VERIFICATION, TicketStatusKey.CLOSED)
        assert can_transition(TicketStatusKey.IN_VERIFICATION, TicketStatusKey.KICKED_BACK)
        assert can_transition(TicketStatusKey.KICKED_BACK, TicketStatusKey.PENDING)

    def test_can_transition_illegal(self) -> None:
        assert not can_transition(TicketStatusKey.COLLECTED, TicketStatusKey.CLOSED)
        assert not can_transition(TicketStatusKey.PENDING, TicketStatusKey.COLLECTED)
        assert not can_transition(TicketStatusKey.CLOSED, TicketStatusKey.ATTRIBUTED)

    def test_assert_transition_raises(self) -> None:
        with pytest.raises(InvalidTicketTransition):
            assert_transition(TicketStatusKey.CLOSED, TicketStatusKey.ATTRIBUTED)

    def test_is_terminal(self) -> None:
        assert is_terminal(TicketStatusKey.CLOSED)
        for s in [
            TicketStatusKey.COLLECTED,
            TicketStatusKey.PENDING_ATTRIBUTION,
            TicketStatusKey.ATTRIBUTED,
            TicketStatusKey.PENDING,
            TicketStatusKey.IN_VERIFICATION,
            TicketStatusKey.KICKED_BACK,
        ]:
            assert not is_terminal(s)


class TestNextStatus:
    def test_collected_to_pending_attribution(self) -> None:
        r = next_status(TicketStatusKey.COLLECTED, category_key="anything")
        assert r == TransitionResult(
            new_status=TicketStatusKey.PENDING_ATTRIBUTION.value, auto_closed=False
        )

    def test_pending_attribution_to_attributed(self) -> None:
        r = next_status(TicketStatusKey.PENDING_ATTRIBUTION, category_key="anything")
        assert r.new_status == TicketStatusKey.ATTRIBUTED.value
        assert not r.auto_closed

    def test_attributed_user_query_auto_closes(self) -> None:
        r = next_status(TicketStatusKey.ATTRIBUTED, category_key="user_query")
        assert r.new_status == TicketStatusKey.CLOSED.value
        assert r.auto_closed

    def test_attributed_other_routes_to_pending(self) -> None:
        for key in ("retrieval", "chunking", "generation", "knowledge"):
            r = next_status(TicketStatusKey.ATTRIBUTED, category_key=key)
            assert r.new_status == TicketStatusKey.PENDING.value
            assert not r.auto_closed

    def test_attributed_no_category_routes_to_pending(self) -> None:
        r = next_status(TicketStatusKey.ATTRIBUTED, category_key=None)
        assert r.new_status == TicketStatusKey.PENDING.value

    def test_invalid_for_other_states(self) -> None:
        for s in (
            TicketStatusKey.PENDING,
            TicketStatusKey.IN_VERIFICATION,
            TicketStatusKey.KICKED_BACK,
            TicketStatusKey.CLOSED,
        ):
            with pytest.raises(InvalidTicketTransition):
                next_status(s, category_key="retrieval")


class TestDefaultTicketStatuses:
    def test_has_seven_builtins(self) -> None:
        keys = {s.key for s in DEFAULT_TICKET_STATUSES}
        assert keys == SYSTEM_TICKET_STATUS_KEYS
        assert len(DEFAULT_TICKET_STATUSES) == 7

    def test_keys_are_distinct(self) -> None:
        keys = [s.key for s in DEFAULT_TICKET_STATUSES]
        assert len(set(keys)) == len(keys)

    def test_each_has_zh_name_and_display_order(self) -> None:
        for spec in DEFAULT_TICKET_STATUSES:
            assert spec.name_zh
            assert spec.display_order >= 0
            assert spec.is_system is True


# ===========================================================================
# test_scope.py
# ===========================================================================


class TestIsAmbiguous:
    def test_empty(self) -> None:
        r = is_ambiguous("")
        assert r.matched
        assert "empty" in r.reason

    def test_too_short(self) -> None:
        assert is_ambiguous("hi").matched
        assert is_ambiguous("a").matched

    def test_single_short_word(self) -> None:
        assert is_ambiguous("python").matched
        assert is_ambiguous("RAG").matched

    def test_normal_query(self) -> None:
        assert not is_ambiguous("公司年假政策是什么").matched
        assert not is_ambiguous("what is the leave policy").matched

    def test_trailing_vague_marker(self) -> None:
        assert is_ambiguous("请假流程等").matched
        assert is_ambiguous("...").matched

    def test_whitespace_separated_normal(self) -> None:
        # 8 chars but with whitespace — not ambiguous.
        assert not is_ambiguous("社保缴纳比例").matched


class TestIsOutOfScope:
    def test_empty_topics(self) -> None:
        r = is_out_of_scope("anything", [])
        assert not r.matched
        assert "no_topics" in r.reason

    def test_topic_match(self) -> None:
        r = is_out_of_scope("年假政策", ["年假", "考勤"])
        assert not r.matched

    def test_no_match(self) -> None:
        r = is_out_of_scope("彩票号码", ["年假", "考勤"])
        assert r.matched

    def test_empty_query(self) -> None:
        r = is_out_of_scope("", ["年假"])
        assert r.matched

    def test_case_insensitive_en(self) -> None:
        r = is_out_of_scope("Annual Leave Policy", ["annual leave"])
        assert not r.matched


# ===========================================================================
# test_cutoff.py
# ===========================================================================


class TestHasBoundaryCutoff:
    def test_empty_answer(self) -> None:
        r = has_boundary_cutoff("", ["chunk"])
        assert not r.matched

    def test_ellipsis_tail(self) -> None:
        r = has_boundary_cutoff("年假 15 天 ...", [])
        assert r.matched
        assert "ellipsis" in r.reason

    def test_dangling_connector(self) -> None:
        r = has_boundary_cutoff("公司政策允许员工请假，但是", [])
        assert r.matched

    def test_no_cutoff(self) -> None:
        r = has_boundary_cutoff("公司政策允许员工请假。", ["chunk"])
        assert not r.matched

    def test_chunk_starts_with_tail_token(self) -> None:
        r = has_boundary_cutoff(
            "公司政策规定年假是",
            ["年假是十五天，根据公司制度执行"],
        )
        # Tail token "年假是" (3 CJK chars) appears at start of chunk,
        # followed by "十" (CJK, so boundary is treated as continuing).
        assert r.matched
        assert "chunk_starts" in r.reason

    def test_chunk_starts_with_short_token_does_not_match(self) -> None:
        # Tail token "年假" is only 2 chars → too short to count as
        # a continuation marker. Should NOT fire.
        r = has_boundary_cutoff(
            "公司政策规定年假",
            ["年假 15 天"],
        )
        assert not r.matched

    def test_normal_completion(self) -> None:
        r = has_boundary_cutoff(
            "答案是十五天。",
            ["完全无关的内容"],
        )
        assert not r.matched


# ===========================================================================
# test_conflict.py
# ===========================================================================


class TestDocumentConflict:
    def test_not_enough_chunks(self) -> None:
        r = has_document_conflict(["only one"])
        assert not r.matched

    def test_consistent_chunks(self) -> None:
        r = has_document_conflict([
            "年假 15 天",
            "年假 15 天",
        ])
        assert not r.matched

    def test_conflict_days(self) -> None:
        r = has_document_conflict([
            "根据员工手册，年假 10 天",
            "新政策：年假 15 天",
        ])
        assert r.matched

    def test_no_numeric_phrase(self) -> None:
        r = has_document_conflict([
            "本手册解释年假规则",
            "年假规则详见正文",
        ])
        assert not r.matched


class TestIsDocumentExpired:
    def test_marker_zh(self) -> None:
        r = is_document_expired(["本版本已废弃"])
        assert r.matched
        assert "已废弃" in r.reason

    def test_marker_en(self) -> None:
        r = is_document_expired(["This API is deprecated"])
        assert r.matched

    def test_old_year_marker(self) -> None:
        # reference 2026, document is from 2020 — 6 years > max_year_age=2
        r = is_document_expired(
            ["员工手册 2020 版"],
            reference_year=2026,
        )
        assert r.matched

    def test_recent_year_marker_ok(self) -> None:
        r = is_document_expired(
            ["员工手册 2025 版"],
            reference_year=2026,
        )
        assert not r.matched

    def test_no_marker(self) -> None:
        r = is_document_expired(["普通文档内容"])
        assert not r.matched


# ===========================================================================
# test_attributor.py
# ===========================================================================


class TestSafeFloat:
    def test_none(self) -> None:
        assert _safe_float(None) is None

    def test_bool_rejected(self) -> None:
        assert _safe_float(True) is None

    def test_int(self) -> None:
        assert _safe_float(1) == 1.0

    def test_float(self) -> None:
        assert _safe_float(0.5) == 0.5

    def test_string_invalid(self) -> None:
        assert _safe_float("not a number") is None

    def test_string_numeric(self) -> None:
        assert _safe_float("0.7") == 0.7


class TestAutoAttributorUserQuery:
    def test_empty_query(self) -> None:
        attr = AutoAttributor()
        r = attr.attribute(query="", answer="hi", retrieved_chunks=["c"])
        assert r.category_key == "user_query"
        assert "ambiguous" in r.matched_rule

    def test_ambiguous_query(self) -> None:
        attr = AutoAttributor()
        r = attr.attribute(query="hi", answer="anything", retrieved_chunks=[])
        assert r.category_key == "user_query"

    def test_out_of_scope(self) -> None:
        attr = AutoAttributor(kb_topics=["年假", "考勤"])
        r = attr.attribute(
            query="明天彩票号码是多少",
            answer="nothing",
            retrieved_chunks=[],
        )
        assert r.category_key == "user_query"
        assert "out_of_scope" in r.matched_rule

    def test_no_topics_disables_scope(self) -> None:
        attr = AutoAttributor(kb_topics=[])
        r = attr.attribute(
            query="anything goes",
            answer="fine",
            retrieved_chunks=[],
        )
        # Falls through to default `generation` since user_query
        # was disabled.
        assert r.category_key == "generation"


class TestAutoAttributorRetrieval:
    def test_low_context_relevance(self) -> None:
        attr = AutoAttributor()
        r = attr.attribute(
            query="公司年假政策",
            answer="回答内容",
            retrieved_chunks=["chunk"],
            ragas_scores={"context_relevance": 0.3, "recall": 0.8},
        )
        assert r.category_key == "retrieval"

    def test_zero_recall(self) -> None:
        attr = AutoAttributor()
        r = attr.attribute(
            query="公司年假政策",
            answer="回答内容",
            retrieved_chunks=[],
            ragas_scores={"context_relevance": 0.9, "recall": 0},
        )
        assert r.category_key == "retrieval"


class TestAutoAttributorChunking:
    def test_boundary_cutoff(self) -> None:
        attr = AutoAttributor()
        r = attr.attribute(
            query="公司年假政策",
            answer="年假是 ... 但是",
            retrieved_chunks=[],
        )
        assert r.category_key == "chunking"


class TestAutoAttributorGeneration:
    def test_low_faithfulness_high_relevance(self) -> None:
        attr = AutoAttributor()
        r = attr.attribute(
            query="公司年假政策",
            answer="年假 10 天（错误）",
            retrieved_chunks=["年假 15 天"],
            ragas_scores={
                "context_relevance": 0.9,
                "recall": 1.0,
                "faithfulness": 0.3,
            },
        )
        assert r.category_key == "generation"

    def test_low_faithfulness_low_relevance_is_not_generation(self) -> None:
        # When context_relevance is also low, the `retrieval` rule fires
        # first (rule 2 before rule 4).
        attr = AutoAttributor()
        r = attr.attribute(
            query="公司年假政策",
            answer="年假 10 天",
            retrieved_chunks=["无关内容"],
            ragas_scores={
                "context_relevance": 0.3,
                "recall": 0.5,
                "faithfulness": 0.3,
            },
        )
        # retrieval wins over generation when relevance is low.
        assert r.category_key == "retrieval"


class TestAutoAttributorKnowledge:
    def test_document_conflict(self) -> None:
        attr = AutoAttributor()
        r = attr.attribute(
            query="公司年假政策",
            answer="年假 10 天",
            retrieved_chunks=[
                "员工手册规定年假 10 天",
                "新政策年假 15 天",
            ],
            ragas_scores={"context_relevance": 0.9, "recall": 1.0, "faithfulness": 0.9},
        )
        assert r.category_key == "knowledge"
        assert "conflict" in r.matched_rule

    def test_document_expired_marker(self) -> None:
        attr = AutoAttributor()
        r = attr.attribute(
            query="公司年假政策",
            answer="年假 10 天",
            retrieved_chunks=["本版本已废弃"],
            ragas_scores={"context_relevance": 0.9, "recall": 1.0, "faithfulness": 0.9},
        )
        assert r.category_key == "knowledge"
        assert "expired" in r.matched_rule


class TestAutoAttributorDefault:
    def test_default_is_generation(self) -> None:
        attr = AutoAttributor()
        r = attr.attribute(
            query="请问公司年假政策",
            answer="年假 15 天。",
            retrieved_chunks=["年假 15 天"],
        )
        assert r.category_key == "generation"
        assert r.matched_rule == "generation:default"


class TestAutoAttributorCustomKeys:
    def test_unknown_category_rejected(self) -> None:
        # Force the attributor to whitelist only one key, then trigger
        # the default. The default emits "generation" which is NOT in
        # the whitelist → AttributionError.
        attr = AutoAttributor(category_keys=("retrieval",))
        with pytest.raises(AttributionError):
            attr.attribute(
                query="anything here",
                answer="normal answer",
                retrieved_chunks=["c"],
            )


# ===========================================================================
# test_repository.py — uses an in-process SQLite engine
# ===========================================================================


@pytest.fixture
def engine():
    from sqlalchemy.pool import StaticPool

    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _Base.metadata.create_all(eng)
    # M5 F2 — the service-layer workspace check queries the messages
    # table to verify the message belongs to the claimed workspace.
    # Create just the messages + conversations + workspaces tables
    # (the minimum needed for that lookup) so the existing tests keep
    # passing without bootstrapping the full DB.
    _M5_MIN_TABLES.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False)


@pytest.fixture
def db_session(session_factory):
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


class TestCategoryRepository:
    def test_add_and_list(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        c1 = repo.add_category(key="retrieval", name_zh="检索问题", is_system=True)
        c2 = repo.add_category(key="my_custom", name_zh="自定义")
        db_session.commit()
        cats = list(repo.list_categories())
        assert {c.key for c in cats} == {"retrieval", "my_custom"}
        # System row listed first.
        assert cats[0].is_system is True

    def test_duplicate_key(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        repo.add_category(key="x", name_zh="X")
        db_session.commit()
        with pytest.raises(DuplicateCategoryKeyError):
            repo.add_category(key="x", name_zh="X2")
            db_session.commit()

    def test_get_by_key(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        repo.add_category(key="retrieval", name_zh="检索问题", is_system=True)
        db_session.commit()
        cat = repo.get_category_by_key("retrieval")
        assert cat is not None
        assert repo.get_category_by_key("nope") is None


class TestTagRepository:
    def test_add_tag(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        t = repo.add_tag(tag_key="vpn-issue", label="VPN 异常")
        db_session.commit()
        assert t.id is not None
        tags = list(repo.list_tags())
        assert len(tags) == 1
        assert tags[0].tag_key == "vpn-issue"

    def test_duplicate_tag(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        repo.add_tag(tag_key="vpn", label="VPN")
        db_session.commit()
        with pytest.raises(DuplicateCategoryKeyError):
            repo.add_tag(tag_key="vpn", label="VPN2")
            db_session.commit()


class TestFeedbackRepository:
    def test_create_and_get(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        f = repo.create_feedback(
            message_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            rating="like",
            comment="great",
        )
        db_session.commit()
        assert f.id is not None
        loaded = repo.get_feedback(f.id)
        assert loaded is not None
        assert loaded.comment == "great"

    def test_get_by_message(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        mid = uuid.uuid4()
        ws1, ws2 = uuid.uuid4(), uuid.uuid4()
        repo.create_feedback(
            message_id=mid,
            user_id=uuid.uuid4(),
            workspace_id=ws1,
            rating="dislike",
        )
        repo.create_feedback(
            message_id=mid,
            user_id=uuid.uuid4(),
            workspace_id=ws2,
            rating="like",
        )
        db_session.commit()
        items = repo.get_feedback_by_message(mid, workspace_ids=[ws1, ws2])
        assert len(items) == 2


class TestTicketStatusDictionary:
    def test_add_and_list(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        row = repo.add_ticket_status(
            key="collected",
            name_zh="已收集",
            description="d",
            color="gray",
            is_terminal=False,
            is_system=True,
            display_order=10,
        )
        db_session.commit()
        assert row.id is not None
        items = list(repo.list_ticket_statuses())
        assert len(items) == 1
        assert items[0].key == "collected"

    def test_duplicate_status_key(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        repo.add_ticket_status(key="collected", name_zh="X")
        db_session.commit()
        with pytest.raises(DuplicateTicketStatusKeyError):
            repo.add_ticket_status(key="collected", name_zh="Y")
            db_session.commit()

    def test_get_by_key(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        repo.add_ticket_status(key="closed", name_zh="已关闭", is_terminal=True)
        db_session.commit()
        assert repo.get_ticket_status_by_key("closed") is not None
        assert repo.get_ticket_status_by_key("nope") is None

    def test_known_status_keys(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        repo.add_ticket_status(key="collected", name_zh="X")
        repo.add_ticket_status(key="closed", name_zh="Y")
        db_session.commit()
        assert repo.known_status_keys() == {"collected", "closed"}


class TestTicketRepository:
    def test_create_and_update(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        # Seed the dictionary rows the FK references.
        for spec in DEFAULT_TICKET_STATUSES:
            repo.add_ticket_status(
                key=spec.key,
                name_zh=spec.name_zh,
                description=spec.description,
                color=spec.color,
                is_terminal=spec.is_terminal,
                is_system=spec.is_system,
                display_order=spec.display_order,
            )
        f = repo.create_feedback(
            message_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            rating="dislike",
        )
        ticket = repo.create_ticket(
            feedback_id=f.id,
            status=TicketStatusKey.COLLECTED.value,
        )
        db_session.commit()
        repo.update_ticket_status(
            ticket_id=ticket.id,
            status=TicketStatusKey.PENDING_ATTRIBUTION.value,
            note="attribution started",
        )
        db_session.commit()
        loaded = repo.get_feedback(f.id)
        assert loaded.ticket is not None
        assert loaded.ticket.status == TicketStatusKey.PENDING_ATTRIBUTION.value

    def test_create_ticket_unknown_status_raises(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        for spec in DEFAULT_TICKET_STATUSES:
            repo.add_ticket_status(
                key=spec.key,
                name_zh=spec.name_zh,
                description=spec.description,
                color=spec.color,
                is_terminal=spec.is_terminal,
                is_system=spec.is_system,
                display_order=spec.display_order,
            )
        f = repo.create_feedback(
            message_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            rating="dislike",
        )
        db_session.commit()
        with pytest.raises(InvalidTicketTransition):
            repo.create_ticket(
                feedback_id=f.id, status="not_a_real_status"
            )

    def test_update_ticket_not_found(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        with pytest.raises(LookupError):
            repo.update_ticket_status(
                ticket_id=uuid.uuid4(),
                status=TicketStatusKey.CLOSED.value,
            )


class TestAttributionRepository:
    def test_create_attribution(self, db_session) -> None:
        repo = FeedbackRepository(db_session)
        f = repo.create_feedback(
            message_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            rating="dislike",
        )
        c = repo.add_category(key="retrieval", name_zh="检索问题", is_system=True)
        db_session.commit()
        attr = repo.create_attribution(
            feedback_id=f.id,
            category_id=c.id,
            confidence=1.0,
            reasoning="low recall",
            matched_rule="retrieval:low_context_relevance_or_zero_recall",
        )
        db_session.commit()
        assert attr.id is not None
        loaded = repo.get_feedback(f.id)
        assert loaded.attribution is not None
        assert loaded.attribution.category.key == "retrieval"


# ===========================================================================
# test_service.py
# ===========================================================================


@pytest.fixture
def session_for_service(session_factory):
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


class TestFeedbackService:
    def test_seed_defaults_idempotent(self, session_for_service) -> None:
        svc = FeedbackService(session=session_for_service)
        first = svc.seed_default_categories()
        second = svc.seed_default_categories()
        session_for_service.commit()
        assert len(first) == 5
        assert second == []
        keys = {c.key for c in svc.list_categories()}
        assert keys == {"retrieval", "chunking", "generation", "knowledge", "user_query"}

    def test_seed_default_ticket_statuses_idempotent(self, session_for_service) -> None:
        svc = FeedbackService(session=session_for_service)
        first = svc.seed_default_ticket_statuses()
        second = svc.seed_default_ticket_statuses()
        session_for_service.commit()
        assert len(first) == 7
        assert second == []
        keys = {s.key for s in svc.list_ticket_statuses()}
        assert keys == SYSTEM_TICKET_STATUS_KEYS

    def test_submit_user_query_auto_closes_ticket(self, session_for_service) -> None:
        svc = FeedbackService(session=session_for_service)
        svc.seed_default_ticket_statuses()  # satisfy FK
        # M5 F2 — seed the message row so the workspace check passes.
        mid = uuid.uuid4()
        ws = uuid.uuid4()
        _seed_message_for_f2(session_for_service, message_id=mid, workspace_id=ws)
        session_for_service.commit()
        result = svc.submit(
            message_id=mid,
            user_id=uuid.uuid4(),
            workspace_id=ws,
            rating="dislike",
            comment="不明白问的是什么",
            query="hi",
            answer="anything",
            retrieved_chunks=[],
            ragas_scores={"context_relevance": 0.9, "recall": 1.0, "faithfulness": 0.9},
        )
        session_for_service.commit()
        assert result.attribution_status == AttributionStatus.SUCCEEDED
        assert result.category_key == "user_query"
        assert result.ticket_status == TicketStatusKey.CLOSED.value

    def test_submit_retrieval_routes_to_pending(self, session_for_service) -> None:
        svc = FeedbackService(session=session_for_service)
        svc.seed_default_ticket_statuses()
        # M5 F2 — seed the message row.
        mid = uuid.uuid4()
        ws = uuid.uuid4()
        _seed_message_for_f2(session_for_service, message_id=mid, workspace_id=ws)
        session_for_service.commit()
        result = svc.submit(
            message_id=mid,
            user_id=uuid.uuid4(),
            workspace_id=ws,
            rating="dislike",
            query="公司年假政策",
            answer="回答内容",
            retrieved_chunks=["chunk"],
            ragas_scores={"context_relevance": 0.2, "recall": 0.5, "faithfulness": 0.9},
        )
        session_for_service.commit()
        assert result.category_key == "retrieval"
        assert result.ticket_status == TicketStatusKey.PENDING.value

    def test_submit_skipped_when_no_query_or_answer(self, session_for_service) -> None:
        svc = FeedbackService(session=session_for_service)
        svc.seed_default_ticket_statuses()
        # M5 F2 — seed the message row.
        mid = uuid.uuid4()
        ws = uuid.uuid4()
        _seed_message_for_f2(session_for_service, message_id=mid, workspace_id=ws)
        session_for_service.commit()
        result = svc.submit(
            message_id=mid,
            user_id=uuid.uuid4(),
            workspace_id=ws,
            rating="like",
        )
        session_for_service.commit()
        assert result.attribution_status == AttributionStatus.SKIPPED
        assert result.category_key is None
        assert result.ticket_status == TicketStatusKey.PENDING_ATTRIBUTION.value

    def test_submit_attributor_failure_marked_failed(self, session_for_service) -> None:
        class _Boom(AutoAttributor):
            def attribute(self, **_):
                raise RuntimeError("nope")

        svc = FeedbackService(session=session_for_service, attributor=_Boom())
        svc.seed_default_ticket_statuses()
        # M5 F2 — seed the message row.
        mid = uuid.uuid4()
        ws = uuid.uuid4()
        _seed_message_for_f2(session_for_service, message_id=mid, workspace_id=ws)
        session_for_service.commit()
        result = svc.submit(
            message_id=mid,
            user_id=uuid.uuid4(),
            workspace_id=ws,
            rating="dislike",
            query="anything",
            answer="answer",
            retrieved_chunks=["c"],
        )
        session_for_service.commit()
        assert result.attribution_status == AttributionStatus.FAILED
        assert result.ticket_status == TicketStatusKey.PENDING_ATTRIBUTION.value

    def test_get_feedback_by_message(self, session_for_service) -> None:
        svc = FeedbackService(session=session_for_service)
        svc.seed_default_ticket_statuses()
        # M5 F2 — both rows share one message and one workspace.
        mid = uuid.uuid4()
        ws = uuid.uuid4()
        _seed_message_for_f2(session_for_service, message_id=mid, workspace_id=ws)
        session_for_service.commit()
        svc.submit(
            message_id=mid,
            user_id=uuid.uuid4(),
            workspace_id=ws,
            rating="like",
        )
        svc.submit(
            message_id=mid,
            user_id=uuid.uuid4(),
            workspace_id=ws,
            rating="dislike",
        )
        session_for_service.commit()
        rows = svc.get_feedback_by_message(mid, workspace_ids=[ws])
        assert len(rows) == 2


# ===========================================================================
# test_feedback_router.py — FastAPI TestClient
# ===========================================================================


def _make_app(session_factory) -> tuple[FastAPI, uuid.UUID]:
    """Build a FastAPI app with `_make_app`-style overrides.

    Returns `(app, ctx_ws_id)` — `ctx_ws_id` is the workspace_id the
    fake user belongs to. Tests that POST `/feedback` (or hit any
    service path that reads `message.conversation.workspace_id`)
    need it to seed a matching `messages` row so the M5 F2 check
    passes. Pre-M5 tests didn't need this because no workspace
    check existed.
    """
    app = FastAPI()
    # Pin the workspace_id once per test so the F2 check can match
    # the message row the test seeds. Without this, every call to
    # `_fake_user` produced a fresh random UUID and the route's
    # `session.get(Message, mid)` would never find a matching
    # workspace, so every POST returned 404.
    ctx_ws_id = uuid.uuid4()
    ctx_user_id = uuid.uuid4()

    # Override auth.
    from agentic_rag_project.api_gateway.dependencies import get_current_user

    def _fake_user() -> UserContext:
        return UserContext(
            user_id=ctx_user_id,
            username="alice",
            is_super_admin=False,
            status="enable",
            workspace_ids=frozenset({ctx_ws_id}),
            permissions=frozenset(),
        )

    app.dependency_overrides[get_current_user] = _fake_user

    # Override get_db to return a session bound to the in-memory engine.
    def _fake_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    from agentic_rag_project.api_gateway import feedback_router as fr
    from agentic_rag_project.db.session import get_db

    app.dependency_overrides[get_db] = _fake_db

    # Pin service factory to use the same session as the request.
    def _factory(session, ctx):
        # Seed both dictionaries up front so the FK on
        # `feedback_tickets.status` is always satisfied, regardless of
        # which test issues the request first.
        FeedbackService(session=session).seed_default_ticket_statuses()
        session.commit()
        return FeedbackService(
            session=session,
            attributor=AutoAttributor(kb_topics=["年假"]),
        )

    feedback_router.set_feedback_service_factory(_factory)

    app.include_router(feedback_router.router)
    return app, ctx_ws_id


class TestFeedbackRouter:
    def test_submit_user_query(self, session_factory) -> None:
        app, ws_id = _make_app(session_factory)
        # M5 F2 — seed the message row so the route + service layer
        # workspace check passes.
        mid = uuid.uuid4()
        sess = session_factory()
        _seed_message_for_f2(sess, message_id=mid, workspace_id=ws_id)
        sess.commit()
        sess.close()
        with TestClient(app) as client:
            resp = client.post(
                "/feedback",
                json={
                    "message_id": str(mid),
                    "rating": "dislike",
                    "comment": "doesn't make sense",
                    "query": "hi",
                    "answer": "irrelevant",
                    "retrieved_chunks": [],
                    "ragas_scores": {"context_relevance": 0.9, "recall": 1.0},
                },
            )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["attribution_status"] == "succeeded"
        assert data["category_key"] == "user_query"
        assert data["ticket_status"] == "closed"

    def test_submit_retrieval(self, session_factory) -> None:
        app, ws_id = _make_app(session_factory)
        # M5 F2 — seed the message row.
        mid = uuid.uuid4()
        sess = session_factory()
        _seed_message_for_f2(sess, message_id=mid, workspace_id=ws_id)
        sess.commit()
        sess.close()
        with TestClient(app) as client:
            resp = client.post(
                "/feedback",
                json={
                    "message_id": str(mid),
                    "rating": "dislike",
                    "query": "公司年假政策",
                    "answer": "回答内容",
                    "retrieved_chunks": ["chunk"],
                    "ragas_scores": {"context_relevance": 0.2, "recall": 0.5},
                },
            )
        assert resp.status_code == 201
        assert resp.json()["category_key"] == "retrieval"

    def test_invalid_rating_rejected(self, session_factory) -> None:
        app, _ = _make_app(session_factory)
        with TestClient(app) as client:
            resp = client.post(
                "/feedback",
                json={
                    "message_id": str(uuid.uuid4()),
                    "rating": "love",  # not in pattern
                },
            )
        # 422 from Pydantic.
        assert resp.status_code == 422

    def test_invalid_message_id(self, session_factory) -> None:
        app, _ = _make_app(session_factory)
        with TestClient(app) as client:
            resp = client.post(
                "/feedback",
                json={
                    "message_id": "not-a-uuid",
                    "rating": "like",
                },
            )
        assert resp.status_code == 400
        assert "message_id" in resp.json()["detail"]

    def test_list_categories(self, session_factory) -> None:
        app, _ = _make_app(session_factory)
        # Seed categories first.
        with TestClient(app) as client:
            resp = client.get("/feedback/categories")
        assert resp.status_code == 200
        # Either empty (just-created DB) or seeded.
        assert isinstance(resp.json(), list)

    def test_create_category_requires_admin(self, session_factory) -> None:
        app, _ = _make_app(session_factory)
        with TestClient(app) as client:
            resp = client.post(
                "/feedback/categories",
                json={"key": "my-key", "name_zh": "我的"},
            )
        assert resp.status_code == 403

    def test_create_category_admin_success(self, session_factory) -> None:
        app, _ = _make_app(session_factory)

        from agentic_rag_project.api_gateway.dependencies import get_current_user

        # Swap to super_admin for this test only.
        def _admin_user() -> UserContext:
            return UserContext(
                user_id=uuid.uuid4(),
                username="root",
                is_super_admin=True,
                status="enable",
                workspace_ids=frozenset(),
                permissions=frozenset({"*"}),
            )

        app.dependency_overrides[get_current_user] = _admin_user
        with TestClient(app) as client:
            resp = client.post(
                "/feedback/categories",
                json={"key": "my-key", "name_zh": "我的", "description": "x"},
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["key"] == "my-key"
        assert resp.json()["is_system"] is False

    def test_create_duplicate_category_409(self, session_factory) -> None:
        app, _ = _make_app(session_factory)

        from agentic_rag_project.api_gateway.dependencies import get_current_user

        def _admin_user() -> UserContext:
            return UserContext(
                user_id=uuid.uuid4(),
                username="root",
                is_super_admin=True,
                status="enable",
                workspace_ids=frozenset(),
                permissions=frozenset({"*"}),
            )

        app.dependency_overrides[get_current_user] = _admin_user
        with TestClient(app) as client:
            client.post("/feedback/categories", json={"key": "k", "name_zh": "K"})
            resp = client.post("/feedback/categories", json={"key": "k", "name_zh": "K2"})
        assert resp.status_code == 409

    def test_list_by_message(self, session_factory) -> None:
        app, ws_id = _make_app(session_factory)
        # M5 F2 — seed the message row so the route + service layer
        # workspace check passes.
        mid = str(uuid.uuid4())
        sess = session_factory()
        _seed_message_for_f2(sess, message_id=uuid.UUID(mid), workspace_id=ws_id)
        sess.commit()
        sess.close()
        with TestClient(app) as client:
            client.post(
                "/feedback",
                json={
                    "message_id": mid,
                    "rating": "like",
                },
            )
            resp = client.get(f"/feedback/by-message/{mid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["message_id"] == mid
        assert len(body["items"]) == 1

    def test_tags_admin_required(self, session_factory) -> None:
        app, _ = _make_app(session_factory)
        with TestClient(app) as client:
            resp = client.post(
                "/feedback/tags",
                json={"tag_key": "vpn", "label": "VPN"},
            )
        assert resp.status_code == 403

    def test_tags_list(self, session_factory) -> None:
        app, _ = _make_app(session_factory)
        with TestClient(app) as client:
            resp = client.get("/feedback/tags")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_ticket_statuses_seeded(self, session_factory) -> None:
        # The factory only seeds on POST routes; seed directly for
        # the GET endpoint.
        with session_factory() as s:
            FeedbackService(session=s).seed_default_ticket_statuses()
            s.commit()

        app, _ = _make_app(session_factory)
        with TestClient(app) as client:
            resp = client.get("/feedback/ticket-statuses")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        keys = {row["key"] for row in body}
        assert keys == SYSTEM_TICKET_STATUS_KEYS

    def test_create_ticket_status_requires_admin(self, session_factory) -> None:
        app, _ = _make_app(session_factory)
        with TestClient(app) as client:
            resp = client.post(
                "/feedback/ticket-statuses",
                json={"key": "on_hold", "name_zh": "挂起"},
            )
        assert resp.status_code == 403

    def test_create_ticket_status_admin_success(self, session_factory) -> None:
        app, _ = _make_app(session_factory)

        from agentic_rag_project.api_gateway.dependencies import get_current_user

        def _admin_user() -> UserContext:
            return UserContext(
                user_id=uuid.uuid4(),
                username="root",
                is_super_admin=True,
                status="enable",
                workspace_ids=frozenset(),
                permissions=frozenset({"*"}),
            )

        app.dependency_overrides[get_current_user] = _admin_user
        with TestClient(app) as client:
            resp = client.post(
                "/feedback/ticket-statuses",
                json={
                    "key": "on_hold",
                    "name_zh": "挂起",
                    "description": "管理员挂起",
                    "color": "blue",
                    "is_terminal": False,
                    "display_order": 80,
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["key"] == "on_hold"
        assert body["is_system"] is False
        assert body["display_order"] == 80

    def test_create_duplicate_ticket_status_409(self, session_factory) -> None:
        app, _ = _make_app(session_factory)

        from agentic_rag_project.api_gateway.dependencies import get_current_user

        def _admin_user() -> UserContext:
            return UserContext(
                user_id=uuid.uuid4(),
                username="root",
                is_super_admin=True,
                status="enable",
                workspace_ids=frozenset(),
                permissions=frozenset({"*"}),
            )

        app.dependency_overrides[get_current_user] = _admin_user
        with TestClient(app) as client:
            client.post(
                "/feedback/ticket-statuses",
                json={"key": "dup", "name_zh": "X"},
            )
            resp = client.post(
                "/feedback/ticket-statuses",
                json={"key": "dup", "name_zh": "Y"},
            )
        assert resp.status_code == 409
