"""Feedback pipeline (T4.2).

Public surface:

  * Models: `Feedback`, `FeedbackAttribution`, `FeedbackCategory`,
    `FeedbackTag`, `FeedbackTicket`, `TicketStatus`
  * AutoAttributor + AttributionResult + AttributionError
  * Ticket state machine: `TicketStatusKey` (Python enum of keys),
    `next_status`, `can_transition`, `assert_transition`,
    `is_terminal`, `InvalidTicketTransition`,
    `DEFAULT_TICKET_STATUSES` (seed spec for the dictionary)
  * Persistence: `FeedbackRepository`
  * Service: `FeedbackService` (submit / list / seed_defaults)
  * Predefined categories: `DEFAULT_CATEGORIES`, `find_by_key`,
    `CategorySpec`
  * Heuristics: `is_ambiguous`, `is_out_of_scope`,
    `has_boundary_cutoff`, `has_document_conflict`,
    `is_document_expired`

Run `FeedbackService.seed_default_categories()` and
`FeedbackService.seed_default_ticket_statuses()` at startup
to ensure the system dictionaries are present.
"""

from agentic_rag_project.feedback.attributor import (
    AttributionError,
    AttributionResult,
    AutoAttributor,
)
from agentic_rag_project.feedback.categories import (
    DEFAULT_CATEGORIES,
    CategorySpec,
    find_by_key,
)
from agentic_rag_project.feedback.conflict import (
    DocumentCheckResult,
    has_document_conflict,
    is_document_expired,
)
from agentic_rag_project.feedback.cutoff import CutoffResult, has_boundary_cutoff
from agentic_rag_project.feedback.models import (
    AttributionStatus,
    Feedback,
    FeedbackAttribution,
    FeedbackCategory,
    FeedbackRating,
    FeedbackTag,
    FeedbackTicket,
    TicketStatus,
)
from agentic_rag_project.feedback.repository import (
    DuplicateCategoryKeyError,
    DuplicateTicketStatusKeyError,
    FeedbackRepository,
)
from agentic_rag_project.feedback.scope import HeuristicResult, is_ambiguous, is_out_of_scope
from agentic_rag_project.feedback.service import FeedbackService, SubmitResult
from agentic_rag_project.feedback.ticket_state import (
    DEFAULT_TICKET_STATUSES,
    InvalidTicketTransition,
    SYSTEM_TICKET_STATUS_KEYS,
    TicketStatusKey,
    TicketStatusSpec,
    TransitionResult,
    assert_transition,
    can_transition,
    is_terminal,
    next_status,
)

# `TicketStatus` is the ORM class (was the old enum name; kept as
# an alias on the ticket_state side for back-compat).
from agentic_rag_project.feedback.ticket_state import TicketStatus as TicketStatusEnum

__all__ = [
    "AttributionError",
    "AttributionResult",
    "AttributionStatus",
    "Auto Attributor",
    "DEFAULT_CATEGORIES",
    "DEFAULT_TICKET_STATUSES",
    "CutoffResult",
    "CategorySpec",
    "DocumentCheckResult",
    "DuplicateCategoryKeyError",
    "DuplicateTicketStatusKeyError",
    "Feedback",
    "FeedbackAttribution",
    "FeedbackCategory",
    "FeedbackRating",
    "FeedbackRepository",
    "FeedbackService",
    "FeedbackTag",
    "FeedbackTicket",
    "HeuristicResult",
    "InvalidTicketTransition",
    "SYSTEM_TICKET_STATUS_KEYS",
    "SubmitResult",
    "TicketStatus",
    "TicketStatusEnum",
    "TicketStatusKey",
    "TicketStatusSpec",
    "TransitionResult",
    "assert_transition",
    "can_transition",
    "find_by_key",
    "has_boundary_cutoff",
    "has_document_conflict",
    "is_ambiguous",
    "is_document_expired",
    "is_out_of_scope",
    "is_terminal",
    "next_status",
]
