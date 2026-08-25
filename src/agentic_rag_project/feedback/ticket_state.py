"""Ticket state machine for feedback (T4.2).

DESIGN 4.6 — the *set* of legal status keys is data-driven
(`ticket_statuses` table, seeded by `seed_default_ticket_statuses`)
but the *transitions* between them are kept in code because they
encode business policy rather than admin preference (e.g. the
`user_query → CLOSED` auto-close rule).

This module owns:
  * The 7 system-predefined status keys (`TicketStatusKey` enum).
  * The legal-transitions graph (immutable; hard-coded).
  * The `next_status` cascade used by `FeedbackService.submit()`
    when attribution produces a category.

Adding a new status means: insert a row into `ticket_statuses`,
add the key to `_SYSTEM_KEYS` here, and (if the new status needs
to participate in attribution routing) extend the auto-close set
and the `next_status` cascade. Operations that don't change the
graph can add rows freely without touching this file.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class TicketStatusKey(str, enum.Enum):
    """Code-side reference for the 7 system status keys.

    Renamed from `TicketStatus` after the same name was taken by
    the new `ticket_statuses` ORM model. The `.value` of each
    member is the canonical key string stored in the database.
    """

    COLLECTED = "collected"
    PENDING_ATTRIBUTION = "pending_attribution"
    ATTRIBUTED = "attributed"
    PENDING = "pending"
    IN_VERIFICATION = "in_verification"
    CLOSED = "closed"
    KICKED_BACK = "kicked_back"


# Backwards-compat alias — code written against the old name still
# works. New code should prefer `TicketStatusKey`.
TicketStatus = TicketStatusKey


# Set of system keys used by `seed_default_ticket_statuses` and
# by validation in `FeedbackService` / repository writes.
SYSTEM_TICKET_STATUS_KEYS: frozenset[str] = frozenset(
    member.value for member in TicketStatusKey
)


@dataclass(frozen=True)
class TicketStatusSpec:
    """Spec for seeding one row of `ticket_statuses`."""

    key: str
    name_zh: str
    description: str
    color: str
    is_terminal: bool
    is_system: bool
    display_order: int


DEFAULT_TICKET_STATUSES: tuple[TicketStatusSpec, ...] = (
    TicketStatusSpec(
        key="collected",
        name_zh="已收集",
        description="用户提交反馈，等待进入归因流程。",
        color="gray",
        is_terminal=False,
        is_system=True,
        display_order=10,
    ),
    TicketStatusSpec(
        key="pending_attribution",
        name_zh="待归因",
        description="AutoAttributor 正在运行或待跑。",
        color="blue",
        is_terminal=False,
        is_system=True,
        display_order=20,
    ),
    TicketStatusSpec(
        key="attributed",
        name_zh="已归因",
        description="归因完成，等待路由到处理队列。",
        color="purple",
        is_terminal=False,
        is_system=True,
        display_order=30,
    ),
    TicketStatusSpec(
        key="pending",
        name_zh="待处理",
        description="责任人已分配，处理中。",
        color="orange",
        is_terminal=False,
        is_system=True,
        display_order=40,
    ),
    TicketStatusSpec(
        key="in_verification",
        name_zh="待验证",
        description="修复完成，等待自动/人工验证。",
        color="yellow",
        is_terminal=False,
        is_system=True,
        display_order=50,
    ),
    TicketStatusSpec(
        key="closed",
        name_zh="已关闭",
        description="终态 — 处理完毕 / 无需处理 / 验证通过。",
        color="green",
        is_terminal=True,
        is_system=True,
        display_order=60,
    ),
    TicketStatusSpec(
        key="kicked_back",
        name_zh="打回重审",
        description="验证失败，回退到 `pending`。",
        color="red",
        is_terminal=False,
        is_system=True,
        display_order=70,
    ),
)


# Auto-close: these categories resolve the ticket without
# requiring an owner. Anything else routes to `pending`.
_AUTO_CLOSE_CATEGORY_KEYS: frozenset[str] = frozenset({"user_query"})


# Legal transitions, expressed as a {from: {to, ...}} map. An
# unknown transition raises `InvalidTicketTransition`.
_LEGAL_TRANSITIONS: dict[TicketStatusKey, frozenset[TicketStatusKey]] = {
    TicketStatusKey.COLLECTED: frozenset({TicketStatusKey.PENDING_ATTRIBUTION}),
    TicketStatusKey.PENDING_ATTRIBUTION: frozenset({TicketStatusKey.ATTRIBUTED}),
    TicketStatusKey.ATTRIBUTED: frozenset(
        {TicketStatusKey.CLOSED, TicketStatusKey.PENDING}
    ),
    TicketStatusKey.PENDING: frozenset({TicketStatusKey.IN_VERIFICATION}),
    TicketStatusKey.IN_VERIFICATION: frozenset(
        {TicketStatusKey.CLOSED, TicketStatusKey.KICKED_BACK}
    ),
    TicketStatusKey.KICKED_BACK: frozenset({TicketStatusKey.PENDING}),
    TicketStatusKey.CLOSED: frozenset(),  # terminal
}


class InvalidTicketTransition(ValueError):
    """Raised when an illegal state change is requested."""


@dataclass(frozen=True)
class TransitionResult:
    """The outcome of `next_status` — both the new state and why."""

    new_status: str
    auto_closed: bool


def next_status(
    current: TicketStatusKey | str,
    *,
    category_key: str | None,
) -> TransitionResult:
    """Compute the next ticket status given the attribution outcome.

    Called by `FeedbackService.submit()` after `AutoAttributor`
    has produced a category. `current` accepts either a
    `TicketStatusKey` enum member or the raw key string (so the
    service can pass the value it read from the DB).
    """
    current_key = _coerce_key(current)

    if current_key == TicketStatusKey.COLLECTED:
        return TransitionResult(
            new_status=TicketStatusKey.PENDING_ATTRIBUTION.value,
            auto_closed=False,
        )
    if current_key == TicketStatusKey.PENDING_ATTRIBUTION:
        return TransitionResult(
            new_status=TicketStatusKey.ATTRIBUTED.value, auto_closed=False
        )
    if current_key == TicketStatusKey.ATTRIBUTED:
        if category_key in _AUTO_CLOSE_CATEGORY_KEYS:
            return TransitionResult(
                new_status=TicketStatusKey.CLOSED.value, auto_closed=True
            )
        return TransitionResult(
            new_status=TicketStatusKey.PENDING.value, auto_closed=False
        )
    raise InvalidTicketTransition(
        f"cannot compute next_status from {current_key.value!r} via attribution"
    )


def can_transition(src: TicketStatusKey | str, dst: TicketStatusKey | str) -> bool:
    """True iff `src → dst` is in the legal-transition map."""
    return _coerce_key(dst) in _LEGAL_TRANSITIONS.get(_coerce_key(src), frozenset())


def assert_transition(
    src: TicketStatusKey | str, dst: TicketStatusKey | str
) -> None:
    """Raise `InvalidTicketTransition` if the move isn't legal."""
    if not can_transition(src, dst):
        raise InvalidTicketTransition(
            f"illegal transition: {_coerce_key(src).value} → {_coerce_key(dst).value}"
        )


def is_terminal(status: TicketStatusKey | str) -> bool:
    """A terminal status has no outgoing transitions."""
    return len(_LEGAL_TRANSITIONS.get(_coerce_key(status), frozenset())) == 0


def _coerce_key(value: TicketStatusKey | str) -> TicketStatusKey:
    """Accept either an enum member or the raw key string."""
    if isinstance(value, TicketStatusKey):
        return value
    if isinstance(value, str):
        try:
            return TicketStatusKey(value)
        except ValueError as exc:
            raise InvalidTicketTransition(f"unknown status key: {value!r}") from exc
    raise InvalidTicketTransition(f"unsupported status value type: {type(value)!r}")


__all__ = [
    "DEFAULT_TICKET_STATUSES",
    "InvalidTicketTransition",
    "SYSTEM_TICKET_STATUS_KEYS",
    "TicketStatus",  # alias for back-compat
    "TicketStatusKey",
    "TicketStatusSpec",
    "TransitionResult",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "next_status",
]
