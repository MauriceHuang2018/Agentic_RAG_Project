"""Tests for F5 partial — feedback idempotency (M5 security).

DESIGN §4.4 / §5.3 — partial fix for the free-replay gap:

  Pre-M5: `repo.create_feedback` did a plain INSERT. If a user
          double-tapped the rating button, or the client retried
          a POST on flaky network, two rows were inserted for the
          same `(message_id, user_id)` tuple.

  Post-M5: migration `0012` adds UNIQUE(message_id, user_id). The
           repository catches the resulting IntegrityError and
           UPDATEs the existing row's mutable fields
           (`rating`, `comment`, `ragas_scores`) instead of
           inserting a duplicate.

These tests pin:

  1. Same `(message_id, user_id)` three times in a row → exactly
     one row exists, with the latest rating + comment.
  2. Different `user_id` for the same `message_id` → two distinct
     rows (the constraint is on the tuple, not just message_id).
  3. Different `message_id` for the same `user_id` → two distinct
     rows (same reasoning).
  4. Existing row's `attribution_status` is reset to PENDING so
     the service pipeline re-runs the attributor on the new
     input. This is the contract the T3 service relies on.

Note: the SQLite test harness mirrors the PG schema via the
`UniqueConstraint` declaration on the `Feedback` ORM model — see
`feedback/models.py::__table_args__`. The 0012 migration adds the
same constraint with `IF NOT EXISTS`, so the model declaration
and migration chain stay in sync.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from agentic_rag_project.feedback import (
    AttributionStatus,
    FeedbackRating,
    FeedbackRepository,
)
from agentic_rag_project.feedback.models import Feedback, _Base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """SQLite in-memory engine. The Feedback ORM's `__table_args__`
    declares `UniqueConstraint("message_id", "user_id",
    name="uq_feedbacks_message_user")`, so this table carries the
    same constraint as production PG after migration 0012.
    """
    from sqlalchemy.pool import StaticPool

    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False)


def _count_feedbacks(session_factory) -> int:
    with session_factory() as s:
        return int(
            s.execute(select(func.count()).select_from(Feedback)).scalar_one()
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_t4_same_user_same_message_three_times_creates_one_row(
    session_factory,
) -> None:
    """Calling `repo.create_feedback` three times with the same
    `(message_id, user_id)` produces ONE row, not three. The
    latest submission's `rating` and `comment` are persisted."""
    msg_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    # First write — fresh insert.
    with session_factory() as s:
        repo = FeedbackRepository(s)
        row1 = repo.create_feedback(
            message_id=msg_id,
            user_id=user_id,
            workspace_id=ws_id,
            rating="like",
            comment="first comment",
        )
        s.commit()
        # Capture the id while still attached to the session —
        # commit() expires attributes; accessing row1.id outside
        # the `with` block would raise DetachedInstanceError.
        first_id = row1.id
    assert _count_feedbacks(session_factory) == 1

    # Second write — same tuple, different rating. Should UPDATE.
    with session_factory() as s:
        repo = FeedbackRepository(s)
        row2 = repo.create_feedback(
            message_id=msg_id,
            user_id=user_id,
            workspace_id=ws_id,
            rating="dislike",
            comment="changed my mind",
        )
        s.commit()
        # Capture the id while still attached to the session —
        # commit() expires attributes; accessing row2.id outside
        # the `with` block would raise DetachedInstanceError.
        row2_id = row2.id

    assert _count_feedbacks(session_factory) == 1
    # Same row identity — the UPDATE was in-place, not a new INSERT.
    assert row2_id == first_id

    # Third write — same tuple, third rating.
    with session_factory() as s:
        repo = FeedbackRepository(s)
        row3 = repo.create_feedback(
            message_id=msg_id,
            user_id=user_id,
            workspace_id=ws_id,
            rating="like",
            comment="changed my mind again",
        )
        s.commit()
        row3_id = row3.id

    assert _count_feedbacks(session_factory) == 1
    assert row3_id == first_id

    # Verify the persisted state matches the latest call.
    with session_factory() as s:
        loaded = s.get(Feedback, first_id)
        assert loaded is not None
        assert loaded.rating == FeedbackRating.LIKE
        assert loaded.comment == "changed my mind again"


def test_t4_different_users_same_message_creates_two_rows(
    session_factory,
) -> None:
    """The UNIQUE constraint is on (message_id, user_id) — not
    just message_id. Two different users both commenting on the
    same assistant message produces two distinct rows. (Without
    this, the second user's feedback would silently overwrite
    the first user's, which is a different bug.)"""
    msg_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    with session_factory() as s:
        FeedbackRepository(s).create_feedback(
            message_id=msg_id,
            user_id=uuid.uuid4(),
            workspace_id=ws_id,
            rating="like",
            comment="user 1",
        )
        s.commit()
    with session_factory() as s:
        FeedbackRepository(s).create_feedback(
            message_id=msg_id,
            user_id=uuid.uuid4(),
            workspace_id=ws_id,
            rating="dislike",
            comment="user 2",
        )
        s.commit()

    assert _count_feedbacks(session_factory) == 2


def test_t4_same_user_different_messages_creates_two_rows(
    session_factory,
) -> None:
    """Symmetric to the previous test: same user, two different
    assistant messages → two distinct rows."""
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    with session_factory() as s:
        FeedbackRepository(s).create_feedback(
            message_id=uuid.uuid4(),
            user_id=user_id,
            workspace_id=ws_id,
            rating="like",
            comment="msg A",
        )
        s.commit()
    with session_factory() as s:
        FeedbackRepository(s).create_feedback(
            message_id=uuid.uuid4(),
            user_id=user_id,
            workspace_id=ws_id,
            rating="dislike",
            comment="msg B",
        )
        s.commit()

    assert _count_feedbacks(session_factory) == 2


def test_t4_idempotent_update_resets_attribution_status_to_pending(
    session_factory,
) -> None:
    """When the user re-submits, the existing row's
    `attribution_status` is reset to PENDING so the service
    pipeline re-runs the attributor on the new input. The
    contract: a re-submission isn't a no-op — it triggers a
    fresh attribution pass.
    """
    msg_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    # First write — defaults to PENDING via the model default.
    with session_factory() as s:
        FeedbackRepository(s).create_feedback(
            message_id=msg_id,
            user_id=user_id,
            workspace_id=ws_id,
            rating="dislike",
        )
        s.commit()
        # Simulate the service having written SUCCEEDED.
        loaded = s.execute(
            select(Feedback).where(Feedback.message_id == msg_id)
        ).scalar_one()
        loaded.attribution_status = AttributionStatus.SUCCEEDED
        s.commit()

    # Second write — re-submission.
    with session_factory() as s:
        FeedbackRepository(s).create_feedback(
            message_id=msg_id,
            user_id=user_id,
            workspace_id=ws_id,
            rating="like",
        )
        s.commit()

    with session_factory() as s:
        loaded = s.execute(
            select(Feedback).where(Feedback.message_id == msg_id)
        ).scalar_one()
        assert loaded.attribution_status == AttributionStatus.PENDING


def test_t4_update_changes_ragas_scores_field(
    session_factory,
) -> None:
    """The UPSERT path must update the `ragas_scores` JSON field
    too, not just `rating` / `comment`. Without this, a user who
    corrected their rating would leave the old RAGAS scores
    attached, which is a stale-data bug.
    """
    msg_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    with session_factory() as s:
        FeedbackRepository(s).create_feedback(
            message_id=msg_id,
            user_id=user_id,
            workspace_id=ws_id,
            rating="dislike",
            ragas_scores={"recall": 0.3, "context_relevance": 0.4},
        )
        s.commit()

    with session_factory() as s:
        FeedbackRepository(s).create_feedback(
            message_id=msg_id,
            user_id=user_id,
            workspace_id=ws_id,
            rating="like",
            ragas_scores={"recall": 0.9, "context_relevance": 0.95},
        )
        s.commit()

    with session_factory() as s:
        loaded = s.execute(
            select(Feedback).where(Feedback.message_id == msg_id)
        ).scalar_one()
        assert loaded.ragas_scores == {
            "recall": 0.9,
            "context_relevance": 0.95,
        }


__all__ = [
    "test_t4_same_user_same_message_three_times_creates_one_row",
    "test_t4_different_users_same_message_creates_two_rows",
    "test_t4_same_user_different_messages_creates_two_rows",
    "test_t4_idempotent_update_resets_attribution_status_to_pending",
    "test_t4_update_changes_ragas_scores_field",
]