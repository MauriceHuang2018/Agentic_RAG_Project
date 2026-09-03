"""TDD spec tests for `acl_filter.build_user_filter`.

These five tests pin the four-layer permission resolver design
(DESIGN docs/workspace_id_pipeline/DESIGN_workspace_id_pipeline.md §2.3):

  L1 super_admin        → empty Filter (no restriction)
  L2 workspace member   → workspace_id IN ctx.workspace_ids
  L3 document owner     → owner_id == ctx.user_id
  L4 explicit ACL row   → acl_user_ids contains ctx.user_id
                          OR acl_workspace_ids in ctx.workspace_ids
                          (combined with `should`)

Combined as `should=[L2, L3, L4]` so any layer matches.

Failure-closed: any DB / import error returns a never-matching Filter
(`workspace_id == "__NEVER_MATCH__"`), never None.

Tests are intentionally written BEFORE the implementation
(`acl_filter/__init__.py` currently only has a docstring). They will
fail with `ImportError: cannot import name 'build_user_filter'`
until T9 (impl commit).

Why TDD first (per CLAUDE.md "先写spec、再测试、再实现"):

  The interface is the contract every downstream caller depends on
  (chat_router → ChatService → retriever). Writing the spec first
  forces a concrete answer to "what does Layer 4 look like?" before
  the impl picks one and the wiring has to bend to it.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from qdrant_client.http import models as qmodels

from agentic_rag_project.acl_filter import build_user_filter
from agentic_rag_project.api_gateway.dependencies import UserContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ctx(
    *,
    user_id: uuid.UUID | None = None,
    workspace_ids: frozenset[uuid.UUID] | None = None,
    is_super_admin: bool = False,
) -> UserContext:
    """Build a UserContext for tests without touching the DB."""
    return UserContext(
        user_id=user_id or uuid.uuid4(),
        username="tester",
        is_super_admin=is_super_admin,
        status="enable",
        workspace_ids=workspace_ids or frozenset(),
        permissions=frozenset({"chat:read"}),
    )


def _field_conditions(filter_: qmodels.Filter) -> list[qmodels.FieldCondition]:
    """Flatten all FieldConditions across `must` + `should` clauses."""
    out: list[qmodels.FieldCondition] = []
    for clause in (filter_.must or []) + (filter_.should or []):
        if isinstance(clause, qmodels.FieldCondition):
            out.append(clause)
    return out


# ---------------------------------------------------------------------------
# L1 — super_admin sees everything (empty filter)
# ---------------------------------------------------------------------------


def test_super_admin_returns_empty_filter() -> None:
    """L1: super_admin gets `must=[]` so HybridSearcher returns everything."""
    ctx = _make_ctx(is_super_admin=True, workspace_ids=frozenset())

    result = build_user_filter(ctx, session=MagicMock())

    # Empty `must` AND empty `should` means "no restriction" in Qdrant.
    assert result.must is None or list(result.must) == []
    assert result.should is None or list(result.should) == []


# ---------------------------------------------------------------------------
# L2 — workspace membership match
# ---------------------------------------------------------------------------


def test_non_super_admin_returns_workspace_membership_match() -> None:
    """L2: non-super-admin gets `workspace_id IN ctx.workspace_ids`."""
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    ctx = _make_ctx(
        workspace_ids=frozenset({ws_a, ws_b}),
        is_super_admin=False,
    )

    result = build_user_filter(ctx, session=MagicMock())

    conds = _field_conditions(result)
    ws_cond = next(
        (c for c in conds if c.key == "workspace_id"),
        None,
    )
    assert ws_cond is not None, f"expected workspace_id clause, got {conds!r}"
    assert isinstance(ws_cond.match, qmodels.MatchAny)
    assert set(ws_cond.match.any) == {str(ws_a), str(ws_b)}


# ---------------------------------------------------------------------------
# L3 — document-owner layer (always present, separate from L2)
# ---------------------------------------------------------------------------


def test_document_owner_layer_via_explicit_acl_user_ids() -> None:
    """L3: every non-super-admin request also gets an `owner_id == ctx.user_id` clause.

    The owner clause lives alongside the workspace clause so that a user
    whose role in a workspace was revoked (so they no longer belong to
    `workspace_ids`) can still see their own uploaded documents.
    """
    ws = uuid.uuid4()
    user_id = uuid.uuid4()
    ctx = _make_ctx(user_id=user_id, workspace_ids=frozenset({ws}))

    result = build_user_filter(ctx, session=MagicMock())

    conds = _field_conditions(result)
    owner_cond = next(
        (c for c in conds if c.key == "owner_id"),
        None,
    )
    assert owner_cond is not None, f"expected owner_id clause, got {conds!r}"
    assert isinstance(owner_cond.match, qmodels.MatchValue)
    assert owner_cond.match.value == str(user_id)


# ---------------------------------------------------------------------------
# L4 — explicit ACL rows for this user / their workspaces
# ---------------------------------------------------------------------------


def test_explicit_acl_workspace_ids_layer() -> None:
    """L4: ACL rows matching `ctx.user_id` or `ctx.workspace_ids` produce filter clauses."""
    ws = uuid.uuid4()
    extra_ws = uuid.uuid4()  # workspace the user is NOT a member of, but has explicit ACL row
    user_id = uuid.uuid4()
    ctx = _make_ctx(user_id=user_id, workspace_ids=frozenset({ws}))

    # Simulate the `acls` table returning one row for an extra workspace
    # and one row for the user themselves (the two flavours of L4).
    acl_rows = MagicMock()
    acl_rows.all.return_value = [
        (extra_ws, None),  # workspace_id grant
        (None, user_id),   # user_id grant
    ]
    session = MagicMock()
    # Two `.execute(stmt).scalars().all()` calls in the impl — patch both.
    session.execute.return_value.scalars.return_value.all.side_effect = [
        [extra_ws],  # workspace_id list
        [user_id],   # user_id list
    ]

    result = build_user_filter(ctx, session=session)

    conds = _field_conditions(result)
    acl_ws_cond = next(
        (c for c in conds if c.key == "acl_workspace_ids"),
        None,
    )
    acl_user_cond = next(
        (c for c in conds if c.key == "acl_user_ids"),
        None,
    )
    assert acl_ws_cond is not None, "expected acl_workspace_ids clause"
    assert acl_user_cond is not None, "expected acl_user_ids clause"
    assert isinstance(acl_ws_cond.match, qmodels.MatchAny)
    assert acl_ws_cond.match.any == [str(extra_ws)]
    assert isinstance(acl_user_cond.match, qmodels.MatchAny)
    assert acl_user_cond.match.any == [str(user_id)]


# ---------------------------------------------------------------------------
# Failure-closed — DB error returns never-matching Filter
# ---------------------------------------------------------------------------


def test_db_error_returns_failure_closed_filter() -> None:
    """Any exception during filter construction → never-matching Filter.

    The sentinel is a FieldCondition on `workspace_id == "__NEVER_MATCH__"`,
    which never matches any real chunk. Result: HybridSearcher returns [],
    synthesizer emits empty answer. Better than leaking documents.
    """
    ctx = _make_ctx(workspace_ids=frozenset({uuid.uuid4()}))

    session = MagicMock()
    # Force every `session.execute(...)` to raise. The impl must NOT
    # propagate the exception — it must catch and return the sentinel.
    session.execute.side_effect = RuntimeError("simulated DB outage")

    with patch(
        "agentic_rag_project.db.models.documents.ACL",
        side_effect=ImportError("simulated import failure"),
        create=True,
    ):
        result = build_user_filter(ctx, session=session)

    conds = _field_conditions(result)
    assert conds, "failure-closed filter must have at least one FieldCondition"
    never_match_cond = next(
        (c for c in conds if c.key == "workspace_id"),
        None,
    )
    assert never_match_cond is not None
    assert isinstance(never_match_cond.match, qmodels.MatchValue)
    assert never_match_cond.match.value == "__NEVER_MATCH__"
