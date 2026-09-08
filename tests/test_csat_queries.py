"""Tests for the csat_queries module (M4.4).

Four service-layer tests cover the three pure helpers
(`compute_csat_summary`, `compute_csat_timeseries`,
`compute_csat_by_category`) plus the dislike-attribution
distribution helper used by the L3 Prometheus collector.

Each test mocks the `Session.execute` chain so the assertions
run against deterministic inputs without hitting a real DB.
The CSAT SQL uses `func.date_trunc` (PG only), so mocking the
session is the right level of abstraction — the SQL itself is
verified by the live dev dry-run, not in unit tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from agentic_rag_project.observability.csat_queries import (
    compute_csat_by_category,
    compute_csat_summary,
    compute_csat_timeseries,
    compute_dislike_attribution_distribution,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    """Build a fresh `MagicMock` Session per call.

    `session.execute(...).scalar_one()` is the pattern used by
    `compute_csat_summary`; `session.execute(...).all()` is the
    pattern used by `compute_csat_timeseries` /
    `compute_csat_by_category` / the dislike distribution.
    Tests set the return value per call site.
    """
    sessions: list[MagicMock] = []

    def _factory() -> MagicMock:
        s = MagicMock()
        s.__enter__ = MagicMock(return_value=s)
        s.__exit__ = MagicMock(return_value=False)
        sessions.append(s)
        return s

    return _factory, sessions


# ---------------------------------------------------------------------------
# 1. compute_csat_summary — non-zero ratio
# ---------------------------------------------------------------------------


def test_compute_csat_summary_returns_correct_ratio(session_factory):
    """Like=42, Dislike=8 → csat_score=0.84 (42 / 50)."""
    factory, _ = session_factory
    ws = uuid.uuid4()

    session = factory()
    # Two `.scalar_one()` calls in order: like_count, dislike_count.
    session.execute.return_value.scalar_one.side_effect = [42, 8]

    data = compute_csat_summary(
        session, workspace_id=ws, window_days=7,
    )

    assert data.workspace_id == ws
    assert data.window_days == 7
    assert data.like_count == 42
    assert data.dislike_count == 8
    assert data.total == 50
    assert data.csat_score == pytest.approx(0.84)


# ---------------------------------------------------------------------------
# 2. compute_csat_summary — zero data
# ---------------------------------------------------------------------------


def test_compute_csat_summary_handles_zero_data(session_factory):
    """Zero-data workspace returns csat_score=0.0, NOT 404."""
    factory, _ = session_factory
    ws = uuid.uuid4()

    session = factory()
    session.execute.return_value.scalar_one.side_effect = [0, 0]

    data = compute_csat_summary(
        session, workspace_id=ws, window_days=7,
    )

    assert data.total == 0
    assert data.like_count == 0
    assert data.dislike_count == 0
    assert data.csat_score == 0.0


# ---------------------------------------------------------------------------
# 3. compute_csat_timeseries — buckets by day
# ---------------------------------------------------------------------------


def test_compute_csat_timeseries_buckets_by_day(session_factory):
    """Multiple (bucket, rating) rows fold into per-day points."""
    factory, _ = session_factory
    ws = uuid.uuid4()

    # 2026-08-25 has 2 likes + 1 dislike; 2026-08-26 has 1 like + 0 dislike.
    # SQLAlchemy returns rows in the order produced by the DB.
    bucket_25 = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    bucket_26 = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
    session = factory()
    session.execute.return_value.all.return_value = [
        (bucket_25, "like", 2),
        (bucket_25, "dislike", 1),
        (bucket_26, "like", 1),
    ]

    data = compute_csat_timeseries(
        session, workspace_id=ws, window_days=7, bucket="day",
    )

    assert data.bucket == "day"
    assert len(data.points) == 2
    assert data.points[0].ts == bucket_25
    assert data.points[0].like_count == 2
    assert data.points[0].dislike_count == 1
    assert data.points[0].total == 3
    assert data.points[0].csat_score == pytest.approx(2 / 3)
    assert data.points[1].ts == bucket_26
    assert data.points[1].like_count == 1
    assert data.points[1].dislike_count == 0
    assert data.points[1].csat_score == 1.0


# ---------------------------------------------------------------------------
# 4. compute_csat_by_category — sorted by dislike_count DESC
# ---------------------------------------------------------------------------


def test_compute_csat_by_category_orders_by_dislike_count_desc(session_factory):
    """Categories are returned with the worst category first."""
    factory, _ = session_factory
    ws = uuid.uuid4()

    # 3 categories with different dislike counts.
    session = factory()
    session.execute.return_value.all.return_value = [
        ("answer_quality", "like", 5),
        ("answer_quality", "dislike", 1),
        ("retrieval_miss", "like", 2),
        ("retrieval_miss", "dislike", 4),
        ("user_query", "like", 8),
        ("user_query", "dislike", 0),
    ]

    data = compute_csat_by_category(
        session, workspace_id=ws, window_days=7,
    )

    assert len(data.categories) == 3
    # retrieval_miss has 4 dislikes → first.
    assert data.categories[0].category_key == "retrieval_miss"
    assert data.categories[0].dislike_count == 4
    assert data.categories[0].like_count == 2
    assert data.categories[0].dislike_rate == pytest.approx(4 / 6)
    # answer_quality has 1 dislike → second.
    assert data.categories[1].category_key == "answer_quality"
    assert data.categories[1].dislike_count == 1
    # user_query has 0 dislikes → last.
    assert data.categories[2].category_key == "user_query"
    assert data.categories[2].dislike_count == 0
    assert data.categories[2].dislike_rate == 0.0


# ---------------------------------------------------------------------------
# 5. compute_dislike_attribution_distribution — used by L3 collector
# ---------------------------------------------------------------------------


def test_dislike_attribution_distribution_returns_category_counts(
    session_factory,
):
    """The L3 helper returns `{category_key: dislike_count}` dict."""
    factory, _ = session_factory
    ws = uuid.uuid4()

    session = factory()
    session.execute.return_value.all.return_value = [
        ("retrieval_miss", 4),
        ("answer_quality", 1),
    ]

    distribution = compute_dislike_attribution_distribution(
        session, workspace_id=ws, window_days=7,
    )

    assert distribution == {
        "retrieval_miss": 4,
        "answer_quality": 1,
    }