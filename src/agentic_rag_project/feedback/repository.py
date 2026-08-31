"""Feedback persistence layer (T4.2).

Pure functions over a SQLAlchemy session — no business logic,
no LLM. Each method is a single SQL transaction the caller can
wrap or chain. The repository returns ORM objects (so callers
get easy attribute access) but never raises `IntegrityError`
silently — callers see the raw exception.

DESIGN 4.6 — status keys on `feedback_tickets.status` are stored
as strings (FK to `ticket_statuses.key`). The repository accepts
either a `TicketStatusKey` enum member or a raw string, but
always writes the raw key to the DB.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentic_rag_project.feedback.models import (
    AttributionStatus,
    Feedback,
    FeedbackAttribution,
    FeedbackCategory,
    FeedbackTag,
    FeedbackTicket,
    TicketStatus,
)
from agentic_rag_project.feedback.ticket_state import (
    InvalidTicketTransition,
    SYSTEM_TICKET_STATUS_KEYS,
    TicketStatusKey,
)


class DuplicateCategoryKeyError(ValueError):
    """Raised when admin tries to insert a category whose key already exists."""


class DuplicateTicketStatusKeyError(ValueError):
    """Raised when admin tries to insert a ticket-status key that already exists."""


def _normalize_status(value: TicketStatusKey | str) -> str:
    """Coerce an enum member or string to the raw key string."""
    if isinstance(value, TicketStatusKey):
        return value.value
    if isinstance(value, str):
        return value
    raise InvalidTicketTransition(
        f"unsupported status value type: {type(value)!r}"
    )


class FeedbackRepository:
    """Thin wrapper around SQLAlchemy for the feedback tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Category / tag dictionary
    # ------------------------------------------------------------------

    def list_categories(self) -> Sequence[FeedbackCategory]:
        stmt = select(FeedbackCategory).order_by(FeedbackCategory.is_system.desc())
        return list(self._session.execute(stmt).scalars())

    def get_category_by_key(self, key: str) -> FeedbackCategory | None:
        return self._session.execute(
            select(FeedbackCategory).where(FeedbackCategory.key == key)
        ).scalar_one_or_none()

    def add_category(
        self,
        *,
        key: str,
        name_zh: str,
        description: str | None = None,
        is_system: bool = False,
        # `label` is the legacy NOT NULL column from migration 0001.
        # The ORM still declares it for schema parity. Default to
        # `name_zh` when callers don't pass one so new rows satisfy
        # the constraint without each call site having to remember.
        label: str | None = None,
    ) -> FeedbackCategory:
        row = FeedbackCategory(
            key=key,
            name_zh=name_zh,
            description=description,
            is_system=is_system,
            label=label or name_zh,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise DuplicateCategoryKeyError(f"category key exists: {key}") from exc
        return row

    def list_tags(self) -> Sequence[FeedbackTag]:
        stmt = select(FeedbackTag).order_by(FeedbackTag.tag_key)
        return list(self._session.execute(stmt).scalars())

    def add_tag(self, *, tag_key: str, label: str) -> FeedbackTag:
        row = FeedbackTag(tag_key=tag_key, label=label)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise DuplicateCategoryKeyError(f"tag key exists: {tag_key}") from exc
        return row

    # ------------------------------------------------------------------
    # Ticket-status dictionary
    # ------------------------------------------------------------------

    def list_ticket_statuses(self) -> Sequence[TicketStatus]:
        stmt = select(TicketStatus).order_by(
            TicketStatus.display_order, TicketStatus.key
        )
        return list(self._session.execute(stmt).scalars())

    def get_ticket_status_by_key(self, key: str) -> TicketStatus | None:
        return self._session.execute(
            select(TicketStatus).where(TicketStatus.key == key)
        ).scalar_one_or_none()

    def add_ticket_status(
        self,
        *,
        key: str,
        name_zh: str,
        description: str | None = None,
        color: str | None = None,
        is_terminal: bool = False,
        is_system: bool = False,
        display_order: int = 0,
    ) -> TicketStatus:
        row = TicketStatus(
            key=key,
            name_zh=name_zh,
            description=description,
            color=color,
            is_terminal=is_terminal,
            is_system=is_system,
            display_order=display_order,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise DuplicateTicketStatusKeyError(
                f"ticket status key exists: {key}"
            ) from exc
        return row

    def known_status_keys(self) -> set[str]:
        """All status keys currently in the table (system + admin-added)."""
        stmt = select(TicketStatus.key)
        return {row for row in self._session.execute(stmt).scalars()}

    # ------------------------------------------------------------------
    # Feedback CRUD
    # ------------------------------------------------------------------

    def create_feedback(
        self,
        *,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        rating: str,
        comment: str | None = None,
        ragas_scores: dict | None = None,
    ) -> Feedback:
        """Insert a `Feedback` row, or update an existing one.

        F5 partial — migration `0012` adds the UNIQUE constraint
        `uq_feedbacks_message_user` on `(message_id, user_id)`.
        Idempotent POST semantics: if the same user submits feedback
        for the same message twice (e.g. double-tap on the UI, or
        correcting their rating), the second submission updates the
        existing row instead of creating a duplicate.

        Implementation: we use the dialect-agnostic
        INSERT-then-UPDATE-on-IntegrityError pattern rather than
        `pg_insert(...).on_conflict_do_update(...)` so the same
        code runs in production PG and in the SQLite test harness.
        The UPDATE mutates only the user-controlled fields
        (`rating`, `comment`, `ragas_scores`) — never the
        `attribution_status` / `created_at` / `id` columns that the
        service pipeline manages.

        Returns the `Feedback` row in its post-write state. The
        caller can read `row.id` regardless of whether the row was
        inserted (new) or updated (existing).
        """
        row = Feedback(
            message_id=message_id,
            user_id=user_id,
            workspace_id=workspace_id,
            rating=rating,  # type: ignore[arg-type]
            comment=comment,
            ragas_scores=ragas_scores,
            attribution_status=AttributionStatus.PENDING,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            # Idempotency path: a row already exists for this
            # (message_id, user_id) tuple. Roll back the failed
            # INSERT and UPDATE the existing row in-place.
            self._session.rollback()
            existing = self._session.execute(
                select(Feedback)
                .where(Feedback.message_id == message_id)
                .where(Feedback.user_id == user_id)
            ).scalar_one_or_none()
            if existing is None:
                # The IntegrityError wasn't from the unique
                # constraint we expected. Re-raise so the caller
                # sees the original error and the transaction
                # state stays in the rolled-back position.
                raise
            existing.rating = rating  # type: ignore[assignment]
            existing.comment = comment
            existing.ragas_scores = ragas_scores
            # Reset attribution to PENDING so the new user input
            # is re-evaluated. The service's attributor cascade
            # overwrites this with SUCCEEDED / FAILED / SKIPPED.
            existing.attribution_status = AttributionStatus.PENDING
            self._session.flush()
            return existing
        return row

    def get_feedback(self, feedback_id: uuid.UUID) -> Feedback | None:
        return self._session.get(Feedback, feedback_id)

    def get_feedback_by_message(
        self,
        message_id: uuid.UUID,
        *,
        # M5 F1 — workspace filter at the repo layer (single source of
        # truth). Routes MUST pass `ctx.workspace_ids` so cross-tenant
        # reads are impossible regardless of caller. An empty iterable
        # is a programming error — the route layer short-circuits with
        # 404 before calling this method in that case. We pass an
        # explicit empty-tuple guard so a misuse returns 0 rows instead
        # of leaking all workspaces.
        workspace_ids: Sequence[uuid.UUID],
    ) -> Sequence[Feedback]:
        if not workspace_ids:
            # Defensive: refuse to return rows when caller provided no
            # workspace scope. Without this branch, an empty list would
            # SQL-render as `workspace_id IN ()` — PG rejects that as a
            # syntax error and the request would 500 instead of 404.
            return []
        stmt = (
            select(Feedback)
            .where(Feedback.message_id == message_id)
            .where(Feedback.workspace_id.in_(list(workspace_ids)))
            .order_by(Feedback.created_at.desc())
        )
        return list(self._session.execute(stmt).scalars())

    # ------------------------------------------------------------------
    # Attribution + ticket
    # ------------------------------------------------------------------

    def create_attribution(
        self,
        *,
        feedback_id: uuid.UUID,
        category_id: uuid.UUID,
        confidence: float,
        reasoning: str | None,
        matched_rule: str | None,
    ) -> FeedbackAttribution:
        row = FeedbackAttribution(
            feedback_id=feedback_id,
            category_id=category_id,
            confidence=confidence,
            reasoning=reasoning,
            matched_rule=matched_rule,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def create_ticket(
        self,
        *,
        feedback_id: uuid.UUID,
        status: TicketStatusKey | str = TicketStatusKey.COLLECTED,
        note: str | None = None,
        assigned_to: uuid.UUID | None = None,
    ) -> FeedbackTicket:
        status_key = _normalize_status(status)
        # Validate against the dictionary; admins can extend it but
        # can't hand the service a typo.
        if status_key not in self.known_status_keys() | set(SYSTEM_TICKET_STATUS_KEYS):
            raise InvalidTicketTransition(
                f"ticket status key not in dictionary: {status_key!r}"
            )
        row = FeedbackTicket(
            feedback_id=feedback_id,
            status=status_key,
            note=note,
            assigned_to=assigned_to,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def update_ticket_status(
        self,
        *,
        ticket_id: uuid.UUID,
        status: TicketStatusKey | str,
        note: str | None = None,
        assigned_to: uuid.UUID | None = None,
    ) -> FeedbackTicket:
        row = self._session.get(FeedbackTicket, ticket_id)
        if row is None:
            raise LookupError(f"ticket {ticket_id} not found")
        status_key = _normalize_status(status)
        if status_key not in self.known_status_keys() | set(SYSTEM_TICKET_STATUS_KEYS):
            raise InvalidTicketTransition(
                f"ticket status key not in dictionary: {status_key!r}"
            )
        row.status = status_key
        if note is not None:
            row.note = note
        if assigned_to is not None:
            row.assigned_to = assigned_to
        self._session.flush()
        return row

    def update_feedback_attribution_status(
        self,
        *,
        feedback_id: uuid.UUID,
        attribution_status: AttributionStatus,
    ) -> None:
        row = self._session.get(Feedback, feedback_id)
        if row is None:
            raise LookupError(f"feedback {feedback_id} not found")
        row.attribution_status = attribution_status
        self._session.flush()


__all__ = [
    "DuplicateCategoryKeyError",
    "DuplicateTicketStatusKeyError",
    "FeedbackRepository",
    "InvalidTicketTransition",
]
