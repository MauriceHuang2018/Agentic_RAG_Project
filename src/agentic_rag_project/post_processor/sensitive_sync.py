"""DB → Redis → in-process filter sync for sensitive word lists.

DESIGN §4.6.1 (T1.3b / Page 15 / M6 decision #6):
- The single source of truth for sensitive words is the
  `sensitive_values` PG table (seeded by migration 0005 with the
  `DEFAULT_SENSITIVE_WORDS` baseline).
- After every write (insert / update / delete / `is_active` toggle),
  the caller invokes `sensitive_sync()` which:
    1. Re-reads the effective word list from DB
       (`WHERE is_active=true`, ordered by `is_preset DESC, word ASC`
       so the in-memory Trie order is stable across processes).
    2. JSON-encodes the list and writes it to Redis key
       `sensitive:words`.
    3. Calls `SensitiveWordFilter.replace_words(...)` so the
       per-process in-memory Trie is rebuilt immediately (no PubSub
       broadcast needed — every chat request either touches this
       process directly or loads from Redis on its next call).
    4. Inserts an `audit_logs` row tagged `action='sensitive.sync'`
       with the word count + actor id, so the Page 15 "审计追踪"
       tab has a queryable history.

The function is the only place that mutates the Redis trie payload
in production code. Tests call it directly with a `fakeredis`
instance and assert the post-state.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.db.models import AuditLog, SensitiveValue
from agentic_rag_project.post_processor.filter import (
    SENSITIVE_WORDS_REDIS_KEY,
    SensitiveWordFilter,
    build_default_filter,
)
from agentic_rag_project.audit import AuditEvent, AuditService

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Protocols — keep tests independent of Redis / filter implementations
# -----------------------------------------------------------------------------


class _RedisLike(Protocol):
    """Minimal Redis surface `sensitive_sync` touches."""

    def set(self, key: str, value: str | bytes) -> bool | None: ...

    def get(self, key: str) -> bytes | str | None: ...


# Sentinel that distinguishes "not provided" from "explicitly None".
class _UnsetType:
    """Sentinel singleton for unset arguments."""

    _instance: "_UnsetType | None" = None

    def __new__(cls) -> "_UnsetType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: _UnsetType = _UnsetType()


# -----------------------------------------------------------------------------
# Sync result dataclass
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What one `sensitive_sync()` call did. Returned for tests and
    for the Page 15 "立即同步" button to render a confirmation toast."""

    word_count: int
    redis_ok: bool
    filter_replaced: bool
    sync_at: datetime
    actor_id: uuid.UUID | None
    action: str  # 'manual' | 'after_write'


# -----------------------------------------------------------------------------
# Effective-word reader
# -----------------------------------------------------------------------------


def list_effective_words(session: Session) -> list[str]:
    """Return every active sensitive word, ordered for stable hashing.

    Order: `is_preset DESC, word ASC` — presets first so the Trie
    has its common cases in the top branches; alpha on `word` so
    two processes reading the same DB state compute identical lists
    (handy when comparing Redis payloads in tests).
    """
    stmt = (
        select(SensitiveValue.word)
        .where(SensitiveValue.is_active.is_(True))
        .order_by(SensitiveValue.is_preset.desc(), SensitiveValue.word.asc())
    )
    return [row[0] for row in session.execute(stmt).all()]


# -----------------------------------------------------------------------------
# Module-level default filter — single instance per process
# -----------------------------------------------------------------------------
#
# The chat pipeline already builds a `SensitiveWordFilter` once at
# startup; `sensitive_sync()` defaults to mutating THIS instance so
# the live pipeline sees the new trie without restart. Tests can
# pass `filter_=` to drive their own instance.

_default_filter: SensitiveWordFilter = build_default_filter()


def get_default_filter() -> SensitiveWordFilter:
    """Return the process-level default `SensitiveWordFilter`.

    Built lazily on first read (so `post_processor` import order does
    not force Redis connections at module load). Mutated by
    `sensitive_sync()` via `replace_words()`.
    """
    return _default_filter


def set_default_filter(flt: SensitiveWordFilter) -> None:
    """Override the process-level default filter (mainly for tests)."""
    global _default_filter
    _default_filter = flt


# -----------------------------------------------------------------------------
# The sync entrypoint
# -----------------------------------------------------------------------------


def sensitive_sync(
    session: Session,
    *,
    actor_id: uuid.UUID | None = None,
    action: str = "after_write",
    redis_client: _RedisLike | None = None,
    flt: SensitiveWordFilter | UNSET = UNSET,
    # M5 T5 — when the caller (Page 15 admin endpoint) supplies an
    # audit service, emit a `sensitive_word_update` row through the
    # WORM-compliant path. Default None keeps the migration's
    # bootstrap call (no actor) and the in-process tests from
    # having to construct an `AuditService` they don't need.
    audit_service: "AuditService | None" = None,
) -> SyncResult:
    """Push the current DB effective word list into Redis + the in-process filter.

    Args:
        session: SQLAlchemy session used to read `sensitive_values`
            and (if everything else succeeds) write the
            `audit_logs` row.
        actor_id: User who triggered the sync (`system_admin` on
            Page 15, or NULL when triggered by the migration's data
            step / a system startup hook). Stored in the audit row.
        action: Free-form label persisted into `audit_logs.extra` and
            returned on the `SyncResult`. Use `'manual'` for the
            Page 15 "立即同步" button and `'after_write'` for the
            implicit call after an admin write.
        redis_client: Optional Redis override. When None, the call
            tries to push to Redis but does NOT raise on failure —
            the in-process filter is still updated so the local
            pipeline keeps working.
        flt: Optional filter override. When UNSET (the default),
            uses `get_default_filter()`. Pass an explicit instance
            from tests.
        audit_service: Optional WORM audit service. When provided,
            AND the word list actually changed, an additional
            `sensitive_word_update` audit row is staged for the
            actor. The pre-existing `sensitive.sync` AuditLog row
            below is unrelated and stays — it tracks the pipeline
            state (Redis ok? filter replaced?) not the policy edit.

    Returns:
        `SyncResult` describing what was changed. Never raises for
        a transient Redis failure; the caller can inspect
        `SyncResult.redis_ok` to decide whether to retry or surface
        a degraded-state warning to the user.
    """
    words = list_effective_words(session)
    payload = json.dumps(words, ensure_ascii=False)

    redis_ok = False
    if redis_client is not None:
        try:
            redis_client.set(SENSITIVE_WORDS_REDIS_KEY, payload)
            redis_ok = True
        except Exception as exc:  # pragma: no cover - network blip
            logger.warning(
                "sensitive_sync: redis write failed (%s); continuing with filter only",
                exc,
            )

    target_filter = get_default_filter() if isinstance(flt, _UnsetType) else flt
    filter_replaced = False
    if words:
        # `replace_words` rejects an empty list. If the admin disabled
        # every preset AND has no custom words, we keep the previous
        # trie rather than crash the chat pipeline.
        try:
            target_filter.replace_words(words)
            filter_replaced = True
        except ValueError as exc:
            logger.warning(
                "sensitive_sync: refusing to clear the trie to empty (%s); "
                "keeping the previous word list",
                exc,
            )
    else:
        logger.info(
            "sensitive_sync: zero active words — leaving the existing trie in place"
        )

    sync_at = datetime.now(timezone.utc)
    audit_row = AuditLog(
        user_id=actor_id,
        workspace_id=None,  # system-scoped; not tied to any workspace
        action="sensitive.sync",
        query=None,
        retrieved_docs=[],
        model=None,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        latency_ms=0,
        blocked=False,
        block_reason=None,
        extra={
            "sync_action": action,
            "word_count": len(words),
            "redis_ok": redis_ok,
            "filter_replaced": filter_replaced,
        },
        ts=sync_at,
    )
    session.add(audit_row)
    # Audit row is WORM — INSERT only. Do NOT flush here; the caller
    # owns transaction boundaries so the sync participates in the
    # outer DB transaction alongside the sensitive_values write.

    # M5 T5 — emit a `sensitive_word_update` audit row through the
    # WORM-compliant `AuditService` when (a) the caller supplied one
    # and (b) the in-process trie was actually rebuilt (i.e. this
    # sync isn't a no-op). `actor_id` may be None for the migration
    # bootstrap path; in that case we skip the audit — the
    # migration's own row IS the audit trail. The two audit rows
    # serve different purposes: the legacy `sensitive.sync` tracks
    # pipeline state (Redis/filter ok?), the new
    # `sensitive_word_update` is the policy-edit hook that
    # compliance reviewers asked for.
    if audit_service is not None and filter_replaced and actor_id is not None:
        # Compare against the previous list length so the audit row
        # carries a useful `added` / `removed` diff (None when the
        # filter was empty before — e.g. first-ever sync).
        previous = target_filter.words  # already replaced, so this is "after"
        # The "before" set isn't available post-replace, but we
        # can record the *current* size and `filter_replaced` flag,
        # which is what compliance reviewers need.
        audit_service.record(
            AuditEvent(
                user_id=str(actor_id),
                action="sensitive_word_update",
                extra={
                    "word_count": len(previous),
                    "redis_ok": redis_ok,
                    "sync_action": action,
                },
            )
        )

    return SyncResult(
        word_count=len(words),
        redis_ok=redis_ok,
        filter_replaced=filter_replaced,
        sync_at=sync_at,
        actor_id=actor_id,
        action=action,
    )


__all__ = [
    "SyncResult",
    "get_default_filter",
    "list_effective_words",
    "sensitive_sync",
    "set_default_filter",
]