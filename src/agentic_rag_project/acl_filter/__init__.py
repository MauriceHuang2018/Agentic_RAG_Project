"""ACL Filter — Qdrant payload pre-filter builder (DESIGN 2.2 #8).

Four-layer permission resolver that produces a `qmodels.Filter` for
the Qdrant retriever. The filter is OR-combined across the layers
(workspace membership OR document owner OR explicit ACL), so a point
passes if ANY layer matches.

  L1 super_admin        — empty Filter (no restriction, sees everything)
  L2 workspace member   — `workspace_id IN ctx.workspace_ids`
  L3 document owner     — `owner_id == ctx.user_id` (always added for
                          non-super-admins so a revoked-role user can
                          still see their own uploads)
  L4 explicit ACL row   — `acl_workspace_ids IN <ACL-granted ws>`
                          OR `acl_user_ids IN <ACL-granted users>`

Failure-closed: any DB or import error during filter construction
returns a never-matching Filter rather than propagating an exception
or returning `None`. The sentinel is
`FieldCondition(workspace_id, MatchValue("__NEVER_MATCH__"))`,
which never matches a real chunk → HybridSearcher returns 0 hits →
synthesizer emits empty answer. Leaks 0 documents at the cost of
returning no answer; that's the safer failure mode.

Originally claimed "Implemented in T5.3" but the implementation was
never written (the empty docstring is what shipped). Closed
2026-09-03 as part of the P0 workspace_id_pipeline track.

DESIGN reference:
  docs/workspace_id_pipeline/DESIGN_workspace_id_pipeline.md §2.3
  docs/workspace_id_pipeline/TASK_workspace_id_pipeline.md T9
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from qdrant_client.http import models as qmodels
from sqlalchemy import select
from sqlalchemy.orm import Session

# `UserContext` lives in `api_gateway.dependencies`, which itself
# imports from `api_gateway/__init__.py` (the gateway package
# re-exports every router). Importing `UserContext` at module load
# would form a cycle:
#   acl_filter → api_gateway.dependencies → api_gateway
#   → chat_router → acl_filter.
# Use TYPE_CHECKING for the annotation so static type checkers still
# see the type, and lazy-import the real symbol inside
# `build_user_filter` at call time. P0 / 2026-09-03.
if TYPE_CHECKING:
    from agentic_rag_project.api_gateway.dependencies import UserContext

logger = logging.getLogger(__name__)


# Sentinel value used in the failure-closed filter. Must be a string
# that cannot match any real `workspace_id` UUID.
_NEVER_MATCH_SENTINEL = "__NEVER_MATCH__"


def _never_match_filter() -> qmodels.Filter:
    """Return a Filter that never matches any real Qdrant point.

    Used when filter construction fails. The constraint compares a
    UUID-typed payload field to a literal string sentinel, which the
    Qdrant server will never satisfy → empty result set → synthesizer
    emits empty answer. This is failure-closed: zero documents leak
    at the cost of zero retrievable chunks.
    """
    return qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="workspace_id",
                match=qmodels.MatchValue(value=_NEVER_MATCH_SENTINEL),
            )
        ]
    )


def build_user_filter(
    ctx: "UserContext",
    session: Session,
) -> qmodels.Filter:
    """Construct the Qdrant pre-filter for the calling user.

    Returns a `qmodels.Filter` whose `should` clause is the OR of the
    active permission layers. Layer 1 (super-admin) short-circuits
    to an empty Filter — no restriction at all.

    The function NEVER raises. Any exception during L2/L3/L4
    construction (DB outage, missing model, malformed UUID) is caught
    and the never-match sentinel is returned instead. This is the
    security boundary: better to return zero documents than to leak.

    Args:
        ctx:        The authenticated user context. MUST have
                    `user_id` (uuid.UUID) and either `is_super_admin`
                    True OR `workspace_ids` non-empty (L2) OR
                    `user_id` for L3 (always).
        session:    A SQLAlchemy `Session` used only for the L4 ACL
                    lookup. Not used when the user is a super-admin.

    Returns:
        A `qmodels.Filter` suitable for `HybridSearcher.hybrid_search`
        as the `qdrant_filter` parameter. Never `None`.
    """
    # --- L1: super-admin bypass ---------------------------------------
    if ctx.is_super_admin:
        return qmodels.Filter()  # empty Filter — no restriction

    # Everything below is in a single try/except so any failure
    # collapses to the never-match sentinel. We do NOT have nested
    # try/except per layer: a partial result (e.g. L4 failed but
    # L2+L3 succeeded) is more dangerous than a uniform failure,
    # because it can leak documents the operator didn't expect.
    try:
        should: list[qmodels.FieldCondition] = []

        # --- L2: workspace membership -------------------------------
        # FieldCondition directly in `should` (not a nested Filter)
        # so the test helpers can flatten + introspect.
        if ctx.workspace_ids:
            should.append(
                qmodels.FieldCondition(
                    key="workspace_id",
                    match=qmodels.MatchAny(
                        any=[str(w) for w in ctx.workspace_ids],
                    ),
                )
            )

        # --- L3: document owner -------------------------------------
        # Always added for non-super-admins. A user whose workspace
        # role was revoked (so they're no longer in workspace_ids)
        # can still see documents they uploaded. Tested by
        # test_document_owner_layer_via_explicit_acl_user_ids.
        should.append(
            qmodels.FieldCondition(
                key="owner_id",
                match=qmodels.MatchValue(value=str(ctx.user_id)),
            )
        )

        # --- L4: explicit ACL rows ----------------------------------
        # Query `acls` for rows that grant access to either the user
        # directly (acl.user_id == ctx.user_id) or any of their
        # workspaces (acl.workspace_id IN ctx.workspace_ids). Each
        # distinct hit becomes its own FieldCondition in the `should`
        # clause, so a point passes if ANY of these payload lists
        # contains the relevant id.
        #
        # Lazy import: ACL model lives in `db.models.documents`. If
        # that module fails to import (e.g. circular import during
        # test collection), the outer except catches it and returns
        # the never-match sentinel. Test failure mode is verified by
        # test_db_error_returns_failure_closed_filter.
        from agentic_rag_project.db.models.documents import ACL

        explicit_workspace_ids: list[uuid_pkg.UUID] = []  # type: ignore[name-defined]
        if ctx.workspace_ids:
            explicit_workspace_ids = (
                session.execute(
                    select(ACL.workspace_id)
                    .where(
                        ACL.workspace_id.in_(ctx.workspace_ids),
                        ACL.user_id.is_(None),
                    )
                    .distinct()
                )
                .scalars()
                .all()
            )
        if explicit_workspace_ids:
            should.append(
                qmodels.FieldCondition(
                    key="acl_workspace_ids",
                    match=qmodels.MatchAny(
                        any=[str(w) for w in explicit_workspace_ids],
                    ),
                )
            )

        explicit_user_ids: list[uuid_pkg.UUID] = (
            session.execute(
                select(ACL.user_id)
                .where(ACL.user_id == ctx.user_id)
                .distinct()
            )
            .scalars()
            .all()
        )
        if explicit_user_ids:
            should.append(
                qmodels.FieldCondition(
                    key="acl_user_ids",
                    match=qmodels.MatchAny(
                        any=[str(u) for u in explicit_user_ids],
                    ),
                )
            )

        return qmodels.Filter(should=should)

    except Exception:
        # Catch-all: DB outage, ImportError, session closed, UUID
        # conversion error, anything. Log at exception level so the
        # incident shows up in alerting.
        logger.exception(
            "acl_filter.build_user_filter failed; returning never-match sentinel "
            "(security: zero leak > partial leak)"
        )
        return _never_match_filter()


# Re-export for tests so they don't need to import uuid directly.
import uuid as uuid_pkg  # noqa: E402  (placed at bottom for clarity)
