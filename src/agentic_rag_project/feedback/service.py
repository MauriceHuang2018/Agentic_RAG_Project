"""Feedback orchestration (T4.2).

The `FeedbackService` is the only place that:

  1. Persists a `Feedback` row.
  2. Runs the `AutoAttributor` cascade.
  3. Writes the attribution + advances the ticket state machine.
  4. Translates any attributor error into `AttributionStatus.FAILED`
     on the feedback row (so the user submission still succeeds).

The service is intentionally thin. Each step is a separate
function so individual callers (admin scripts, the CLI, the
scheduled re-attributor in T4.4) can re-run the attribution step
without re-inserting the feedback row.

DESIGN 4.6 — status keys are data-driven (`ticket_statuses`
table), but transitions stay in code (see `ticket_state`). The
service stores the raw key string on `feedback_tickets.status`
so the FK is enforced by the DB.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from agentic_rag_project.feedback.attributor import (
    AttributionResult,
    AutoAttributor,
)
from agentic_rag_project.feedback.models import (
    AttributionStatus,
    Feedback,
    FeedbackAttribution,
    FeedbackCategory,
    FeedbackTicket,
    TicketStatus,
)
from agentic_rag_project.feedback.repository import FeedbackRepository
from agentic_rag_project.feedback.ticket_state import (
    DEFAULT_TICKET_STATUSES,
    TicketStatusKey,
    next_status,
)
from agentic_rag_project.observability.feedback_metrics import record_feedback

logger = logging.getLogger(__name__)


@dataclass
class SubmitResult:
    """Return value of `FeedbackService.submit()`."""

    feedback_id: uuid.UUID
    attribution_status: AttributionStatus
    category_key: str | None
    # Raw status key string from `ticket_statuses.key` — what the
    # FK column actually holds. Callers that want a typed enum can
    # coerce via `TicketStatusKey(value)` if it's a known key.
    ticket_status: str | None
    matched_rule: str | None
    reasoning: str | None


class FeedbackService:
    """Compose repository + attributor + state machine."""

    def __init__(
        self,
        *,
        session: Session,
        attributor: AutoAttributor | None = None,
    ) -> None:
        self._session = session
        self._repo = FeedbackRepository(session)
        self._attributor = attributor or AutoAttributor()

    # ------------------------------------------------------------------
    # submit — main public entry point
    # ------------------------------------------------------------------

    def submit(
        self,
        *,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        rating: str,
        comment: str | None = None,
        ragas_scores: dict | None = None,
        retrieved_chunks: list[str] | None = None,
        query: str | None = None,
        answer: str | None = None,
        reference_year: int | None = None,
    ) -> SubmitResult:
        """Record a user feedback row and (best-effort) auto-attribute it.

        Steps:
          1. Persist the `Feedback` row.
          2. Create a ticket in `COLLECTED` state.
          3. Run the attributor cascade (if inputs are provided).
          4. On success: write `FeedbackAttribution` + advance the
             ticket through PENDING_ATTRIBUTION → ATTRIBUTED → (CLOSED
             or PENDING).
          5. On failure: mark the feedback `attribution_status=FAILED`
             and leave the ticket at `PENDING_ATTRIBUTION` so admin
             triage can finish manually.
        """
        repo = self._repo

        # 1 — feedback row.
        feedback = repo.create_feedback(
            message_id=message_id,
            user_id=user_id,
            workspace_id=workspace_id,
            rating=rating,
            comment=comment,
            ragas_scores=ragas_scores,
        )

        # 2 — ticket at COLLECTED → advance to PENDING_ATTRIBUTION
        # immediately (the user already triggered attribution by
        # submitting).
        ticket = repo.create_ticket(
            feedback_id=feedback.id,
            status=TicketStatusKey.COLLECTED.value,
        )
        ticket.status = TicketStatusKey.PENDING_ATTRIBUTION.value
        self._session.flush()

        # 3 — run attributor.
        chunks = list(retrieved_chunks or [])
        if query is None or answer is None:
            # Caller didn't provide enough to attribute — leave it
            # in PENDING_ATTRIBUTION for manual triage.
            record_feedback(
                rating=feedback.rating,
                attribution_status=AttributionStatus.SKIPPED,
                category_key=None,
            )
            return SubmitResult(
                feedback_id=feedback.id,
                attribution_status=AttributionStatus.SKIPPED,
                category_key=None,
                ticket_status=TicketStatusKey.PENDING_ATTRIBUTION.value,
                matched_rule=None,
                reasoning=None,
            )

        try:
            attr_result = self._attributor.attribute(
                query=query,
                answer=answer,
                retrieved_chunks=chunks,
                ragas_scores=ragas_scores,
                reference_year=reference_year,
            )
        except Exception as exc:
            logger.warning(
                "attributor failed for feedback %s: %s",
                feedback.id,
                exc,
            )
            repo.update_feedback_attribution_status(
                feedback_id=feedback.id,
                attribution_status=AttributionStatus.FAILED,
            )
            record_feedback(
                rating=feedback.rating,
                attribution_status=AttributionStatus.FAILED,
                category_key=None,
            )
            return SubmitResult(
                feedback_id=feedback.id,
                attribution_status=AttributionStatus.FAILED,
                category_key=None,
                ticket_status=TicketStatusKey.PENDING_ATTRIBUTION.value,
                matched_rule=None,
                reasoning=str(exc),
            )

        # 4 — write attribution + advance ticket.
        category = repo.get_category_by_key(attr_result.category_key)
        if category is None:
            # The built-in category isn't in the DB yet — fall back
            # to seeding it on the fly so the FK doesn't reject.
            category = repo.add_category(
                key=attr_result.category_key,
                name_zh=_default_name(attr_result.category_key),
                is_system=True,
            )

        repo.create_attribution(
            feedback_id=feedback.id,
            category_id=category.id,
            confidence=attr_result.confidence,
            reasoning=attr_result.reasoning,
            matched_rule=attr_result.matched_rule,
        )
        repo.update_feedback_attribution_status(
            feedback_id=feedback.id,
            attribution_status=AttributionStatus.SUCCEEDED,
        )
        record_feedback(
            rating=feedback.rating,
            attribution_status=AttributionStatus.SUCCEEDED,
            category_key=attr_result.category_key,
        )

        # Advance ticket: PENDING_ATTRIBUTION → ATTRIBUTED → (CLOSED/PENDING)
        hop1 = next_status(
            TicketStatusKey.PENDING_ATTRIBUTION.value, category_key=None
        )
        ticket.status = hop1.new_status
        # Second hop depends on category.
        hop2 = next_status(hop1.new_status, category_key=attr_result.category_key)
        ticket.status = hop2.new_status
        self._session.flush()

        return SubmitResult(
            feedback_id=feedback.id,
            attribution_status=AttributionStatus.SUCCEEDED,
            category_key=attr_result.category_key,
            ticket_status=ticket.status,
            matched_rule=attr_result.matched_rule,
            reasoning=attr_result.reasoning,
        )

    # ------------------------------------------------------------------
    # admin helpers
    # ------------------------------------------------------------------

    def seed_default_categories(self) -> list[FeedbackCategory]:
        """Idempotently insert the 5 system categories.

        Existing rows (matched by `key`) are skipped. Called at app
        startup from the FastAPI lifespan hook.
        """
        from agentic_rag_project.feedback.categories import DEFAULT_CATEGORIES

        inserted: list[FeedbackCategory] = []
        for spec in DEFAULT_CATEGORIES:
            existing = self._repo.get_category_by_key(spec.key)
            if existing is not None:
                continue
            inserted.append(
                self._repo.add_category(
                    key=spec.key,
                    name_zh=spec.name_zh,
                    description=spec.description,
                    is_system=True,
                )
            )
        return inserted

    def seed_default_ticket_statuses(self) -> list[TicketStatus]:
        """Idempotently insert the 7 system ticket statuses.

        Existing rows (matched by `key`) are skipped. Called at
        app startup *after* `seed_default_categories` so the FK
        on `feedback_tickets.status` always has a parent row.
        """
        inserted: list[TicketStatus] = []
        for spec in DEFAULT_TICKET_STATUSES:
            existing = self._repo.get_ticket_status_by_key(spec.key)
            if existing is not None:
                continue
            inserted.append(
                self._repo.add_ticket_status(
                    key=spec.key,
                    name_zh=spec.name_zh,
                    description=spec.description,
                    color=spec.color,
                    is_terminal=spec.is_terminal,
                    is_system=spec.is_system,
                    display_order=spec.display_order,
                )
            )
        return inserted

    def list_categories(self) -> list[FeedbackCategory]:
        return list(self._repo.list_categories())

    def list_ticket_statuses(self) -> list[TicketStatus]:
        return list(self._repo.list_ticket_statuses())

    def get_feedback(self, feedback_id: uuid.UUID) -> Feedback | None:
        return self._repo.get_feedback(feedback_id)

    def get_feedback_by_message(
        self, message_id: uuid.UUID
    ) -> list[Feedback]:
        return list(self._repo.get_feedback_by_message(message_id))


def _default_name(key: str) -> str:
    return {
        "retrieval": "检索问题",
        "chunking": "切片问题",
        "generation": "生成问题",
        "knowledge": "知识库问题",
        "user_query": "用户问题",
    }.get(key, key)


__all__ = [
    "FeedbackService",
    "SubmitResult",
]
