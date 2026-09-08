"""CSAT aggregation queries (M4.4).

Pure-Python helpers that aggregate the `feedbacks` /
`feedback_attributions` tables into three Pydantic response
shapes consumed by `admin_router.csat_*` endpoints. The
Prometheus L3 collector (`observability.collector.refresh_business_gauges`)
also reuses `compute_csat_summary` and `compute_csat_by_category`
so the gauge values and the dashboard API read from the same
SQL.

Why split out of `collector.py`?
---------------------------------
`refresh_business_gauges` is a side-effecting helper (sets
Prometheus gauge labels); the dashboard API needs the same
arithmetic as **data**, not as gauge writes. By extracting
the SQL into pure functions, both call sites share one source
of truth without importing each other's concerns (no FastAPI
in service layer, no Prometheus in service layer).

Window semantics
----------------
`window_days ∈ {1, 7, 30}` (enforced at the API layer). The
Prometheus L3 collector hard-codes 7 days; the dashboard API
accepts the three-window choice. With `window_days=7` the two
should agree to within ±0.01 (the gauge is cached up to 60s
behind the API's direct DB read).

Decision A (Align 2026-08-26): the CSAT denominator includes
all LIKE + DISLIKE rows, regardless of `attribution_status`.
`attribution_status=FAILED` rows still represent a real user
verdict; excluding them would couple CSAT to the attributor
uptime.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_rag_project.feedback.models import (
    Feedback,
    FeedbackAttribution,
    FeedbackCategory,
    FeedbackRating,
)


# ---------------------------------------------------------------------------
# Dataclasses — service-layer return shape (frozen / hashable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CSATSummary:
    """Single-point CSAT for one workspace in one rolling window."""

    workspace_id: uuid.UUID
    window_days: int
    like_count: int
    dislike_count: int
    total: int
    csat_score: float  # 0.0 if total == 0


@dataclass(frozen=True)
class CSATTimeseriesPoint:
    """One bucket of a CSAT time series."""

    ts: datetime
    like_count: int
    dislike_count: int
    total: int
    csat_score: float


@dataclass(frozen=True)
class CSATTimeseries:
    """Time-bucketed CSAT for one workspace."""

    workspace_id: uuid.UUID
    window_days: int
    bucket: Literal["hour", "day"]
    points: tuple[CSATTimeseriesPoint, ...]


@dataclass(frozen=True)
class CSATCategoryRow:
    """One row of per-category CSAT breakdown."""

    category_key: str
    like_count: int
    dislike_count: int
    total: int
    dislike_rate: float  # dislike_count / total if total > 0 else 0.0


@dataclass(frozen=True)
class CSATByCategory:
    """Per-category CSAT breakdown for one workspace."""

    workspace_id: uuid.UUID
    window_days: int
    categories: tuple[CSATCategoryRow, ...]


# ---------------------------------------------------------------------------
# Pydantic response models — API-layer serialization
# ---------------------------------------------------------------------------


class CSATSummaryResponse(BaseModel):
    """`GET /api/v1/admin/csat/summary` response."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    window_days: int = Field(ge=1, le=30)
    like_count: int = Field(ge=0)
    dislike_count: int = Field(ge=0)
    total: int = Field(ge=0)
    csat_score: float = Field(ge=0.0, le=1.0)
    generated_at: datetime


class CSATTimeseriesPointResponse(BaseModel):
    """One point in the timeseries response."""

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    like_count: int = Field(ge=0)
    dislike_count: int = Field(ge=0)
    total: int = Field(ge=0)
    csat_score: float = Field(ge=0.0, le=1.0)


class CSATTimeseriesResponse(BaseModel):
    """`GET /api/v1/admin/csat/timeseries` response."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    window_days: int = Field(ge=1, le=30)
    bucket: Literal["hour", "day"]
    points: list[CSATTimeseriesPointResponse]
    generated_at: datetime


class CSATCategoryRowResponse(BaseModel):
    """One row of the per-category response."""

    model_config = ConfigDict(extra="forbid")

    category_key: str
    like_count: int = Field(ge=0)
    dislike_count: int = Field(ge=0)
    total: int = Field(ge=0)
    dislike_rate: float = Field(ge=0.0, le=1.0)


class CSATByCategoryResponse(BaseModel):
    """`GET /api/v1/admin/csat/by-category` response."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    window_days: int = Field(ge=1, le=30)
    categories: list[CSATCategoryRowResponse]
    generated_at: datetime


# ---------------------------------------------------------------------------
# Service-layer helpers — pure functions, no FastAPI / Prometheus deps
# ---------------------------------------------------------------------------


def _window_cutoff(window_days: int) -> datetime:
    """UTC `window_days` ago — the floor of the rolling window."""
    return datetime.now(timezone.utc) - _ONE_DAY_TIMEDELTA * window_days


# Lazy import to avoid a top-level dependency on `datetime.timedelta`
# (Python's stdlib is fine; this keeps the helper narrow).
from datetime import timedelta as _td  # noqa: E402

_ONE_DAY_TIMEDELTA = _td(days=1)


def compute_csat_summary(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    window_days: int,
) -> CSATSummary:
    """Aggregate LIKE / DISLIKE counts and the resulting ratio.

    Returns `CSATSummary(csat_score=0.0, total=0)` for zero-data
    workspaces — CSAT 0.0 is a legal state (the workspace just
    opened), not a 404.

    Raises:
        SQLAlchemyError: propagate; caller (FastAPI) catches via
            catch-all and returns 500.
    """
    if window_days < 1 or window_days > 30:
        raise ValueError(
            f"window_days must be in [1, 30]; got {window_days}"
        )
    cutoff = _window_cutoff(window_days)
    like_count = session.execute(
        select(func.count(Feedback.id)).where(
            Feedback.workspace_id == workspace_id,
            Feedback.rating == FeedbackRating.LIKE.value,
            Feedback.created_at >= cutoff,
        )
    ).scalar_one()
    dislike_count = session.execute(
        select(func.count(Feedback.id)).where(
            Feedback.workspace_id == workspace_id,
            Feedback.rating == FeedbackRating.DISLIKE.value,
            Feedback.created_at >= cutoff,
        )
    ).scalar_one()
    total = int(like_count) + int(dislike_count)
    csat = float(like_count) / total if total else 0.0
    return CSATSummary(
        workspace_id=workspace_id,
        window_days=window_days,
        like_count=int(like_count),
        dislike_count=int(dislike_count),
        total=total,
        csat_score=csat,
    )


def compute_csat_timeseries(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    window_days: int,
    bucket: Literal["hour", "day"],
) -> CSATTimeseries:
    """Time-bucketed CSAT for one workspace.

    Empty windows return an empty `points` tuple; never raises
    on no-data.

    Raises:
        ValueError: `bucket ∉ {"hour", "day"}` or `window_days ∉
            [1, 30]`.
        SQLAlchemyError: propagate.
    """
    if bucket not in ("hour", "day"):
        raise ValueError(
            f"bucket must be one of ['hour', 'day']; got {bucket!r}"
        )
    if window_days < 1 or window_days > 30:
        raise ValueError(
            f"window_days must be in [1, 30]; got {window_days}"
        )
    cutoff = _window_cutoff(window_days)
    bucket_expr = func.date_trunc(bucket, Feedback.created_at)
    stmt = (
        select(
            bucket_expr.label("bucket_ts"),
            Feedback.rating,
            func.count(Feedback.id).label("n"),
        )
        .where(
            Feedback.workspace_id == workspace_id,
            Feedback.created_at >= cutoff,
        )
        .group_by(bucket_expr, Feedback.rating)
        .order_by(bucket_expr)
    )
    rows = session.execute(stmt).all()

    # Fold (bucket_ts, rating) -> (like, dislike)
    by_bucket: dict[datetime, tuple[int, int]] = {}
    for bucket_ts, rating, n in rows:
        if bucket_ts is None:
            continue
        like_so_far, dislike_so_far = by_bucket.get(bucket_ts, (0, 0))
        if rating == FeedbackRating.LIKE.value:
            like_so_far += int(n)
        elif rating == FeedbackRating.DISLIKE.value:
            dislike_so_far += int(n)
        by_bucket[bucket_ts] = (like_so_far, dislike_so_far)

    points = tuple(
        CSATTimeseriesPoint(
            ts=ts,
            like_count=likes,
            dislike_count=dislikes,
            total=likes + dislikes,
            csat_score=(
                float(likes) / (likes + dislikes)
                if (likes + dislikes) > 0
                else 0.0
            ),
        )
        for ts, (likes, dislikes) in sorted(by_bucket.items())
    )
    return CSATTimeseries(
        workspace_id=workspace_id,
        window_days=window_days,
        bucket=bucket,
        points=points,
    )


def compute_dislike_attribution_distribution(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    window_days: int,
) -> dict[str, int]:
    """Count DISLIKE feedback rows per attribution category_key.

    Returns `{category_key: dislike_count}` for one workspace in
    the rolling window. Used by the L3 gauge collector to fill
    `feedback_dislike_rate{workspace_id, category_key}` where
    the gauge value is `category_dislike / workspace_total_dislike`.

    Note this is **not** the same as `compute_csat_by_category`:
    the dashboard endpoint exposes per-category dislike_rate as
    `category_dislike / category_total`; the Prometheus gauge
    uses `category_dislike / workspace_total`. Both are valid
    analytics views of the same data — see DESIGN §3.2.

    Returns an empty dict when the workspace has no disliked
    feedback in the window.

    Raises:
        ValueError: `window_days ∉ [1, 30]`.
        SQLAlchemyError: propagate.
    """
    if window_days < 1 or window_days > 30:
        raise ValueError(
            f"window_days must be in [1, 30]; got {window_days}"
        )
    cutoff = _window_cutoff(window_days)
    stmt = (
        select(
            FeedbackCategory.key,
            func.count(Feedback.id).label("n"),
        )
        .join(
            FeedbackAttribution,
            FeedbackAttribution.category_id == FeedbackCategory.id,
        )
        .join(
            Feedback,
            Feedback.id == FeedbackAttribution.feedback_id,
        )
        .where(
            Feedback.workspace_id == workspace_id,
            Feedback.rating == FeedbackRating.DISLIKE.value,
            Feedback.created_at >= cutoff,
        )
        .group_by(FeedbackCategory.key)
    )
    rows = session.execute(stmt).all()
    return {
        cat_key: int(n)
        for cat_key, n in rows
        if cat_key is not None
    }


def compute_csat_by_category(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    window_days: int,
) -> CSATByCategory:
    """Per-category CSAT breakdown for one workspace.

    Categories with zero rows in the window are omitted from the
    result. The returned tuple is sorted by `dislike_count` DESC
    so the worst category leads the list.

    Raises:
        ValueError: `window_days ∉ [1, 30]`.
        SQLAlchemyError: propagate.
    """
    if window_days < 1 or window_days > 30:
        raise ValueError(
            f"window_days must be in [1, 30]; got {window_days}"
        )
    cutoff = _window_cutoff(window_days)
    stmt = (
        select(
            FeedbackCategory.key,
            Feedback.rating,
            func.count(Feedback.id).label("n"),
        )
        .join(
            FeedbackAttribution,
            FeedbackAttribution.category_id == FeedbackCategory.id,
        )
        .join(
            Feedback,
            Feedback.id == FeedbackAttribution.feedback_id,
        )
        .where(
            Feedback.workspace_id == workspace_id,
            Feedback.created_at >= cutoff,
        )
        .group_by(FeedbackCategory.key, Feedback.rating)
    )
    rows = session.execute(stmt).all()

    # Fold (category_key, rating) -> (like, dislike)
    by_cat: dict[str, tuple[int, int]] = {}
    for cat_key, rating, n in rows:
        if cat_key is None:
            continue
        like_so_far, dislike_so_far = by_cat.get(cat_key, (0, 0))
        if rating == FeedbackRating.LIKE.value:
            like_so_far += int(n)
        elif rating == FeedbackRating.DISLIKE.value:
            dislike_so_far += int(n)
        by_cat[cat_key] = (like_so_far, dislike_so_far)

    rows_sorted = sorted(
        by_cat.items(),
        key=lambda kv: (kv[1][1], kv[1][0]),
        reverse=False,
    )
    rows_sorted = sorted(
        by_cat.items(),
        key=lambda kv: kv[1][1],  # dislike_count
        reverse=True,
    )
    categories = tuple(
        CSATCategoryRow(
            category_key=cat_key,
            like_count=likes,
            dislike_count=dislikes,
            total=likes + dislikes,
            dislike_rate=(
                float(dislikes) / (likes + dislikes)
                if (likes + dislikes) > 0
                else 0.0
            ),
        )
        for cat_key, (likes, dislikes) in rows_sorted
    )
    return CSATByCategory(
        workspace_id=workspace_id,
        window_days=window_days,
        categories=categories,
    )


__all__ = [
    "CSATByCategory",
    "CSATByCategoryResponse",
    "CSATCategoryRow",
    "CSATCategoryRowResponse",
    "CSATSummary",
    "CSATSummaryResponse",
    "CSATTimeseries",
    "CSATTimeseriesPoint",
    "CSATTimeseriesPointResponse",
    "CSATTimeseriesResponse",
    "compute_csat_by_category",
    "compute_csat_summary",
    "compute_csat_timeseries",
    "compute_dislike_attribution_distribution",
]