"""FastAPI dependencies for authentication and authorization.

Provides:
  - `get_current_user`   resolves a JWT OR `sk-`-prefixed API token
                          to a `UserContext`.
  - `require_super_admin` guards endpoints for global admins only.
  - `require_permission`  factory that builds a dependency requiring a
                          specific `<resource>:<action>` permission key
                          on the given workspace.

`UserContext` is a lightweight value object carrying the bits downstream
code needs without re-querying the DB on every call. Membership and role
checks still hit the DB once per request via `get_user_context`.

DESIGN §4.6.4 (M6 / T5.7 / Page 14): the Authorization header may
carry either a JWT (`Bearer <jwt>`) or a personal API token
(`Bearer sk-xxxxxxxx...`); the latter path looks the token up in
`users.api_tokens` and resolves to the owning user. Same response
shape regardless of credential type — no information leak about
which scheme was attempted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

# Local module access (no cycle); see `get_settings_cached` below.
from agentic_rag_project.config import get_settings

from agentic_rag_project.api_gateway.jwt import TokenError, decode_access_token
from agentic_rag_project.db.models import Permission, Role, User, UserRole, Workspace
from agentic_rag_project.db.session import get_db
from agentic_rag_project.rbac.constants import SYSTEM_WORKSPACE_ID
from agentic_rag_project.services.api_token_service import (
    API_TOKEN_SCHEME,
    validate_api_token,
)

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class UserContext:
    """Identity + workspace memberships for the requesting user.

    `permissions` is a flat set of `<resource>:<action>` keys the user
    holds across ALL their workspace memberships; super-admin requests
    carry a wildcard ('*') to short-circuit ACL checks downstream.
    """

    user_id: uuid.UUID
    username: str
    is_super_admin: bool
    status: str  # 'enable' | 'disable'
    workspace_ids: frozenset[uuid.UUID]
    permissions: frozenset[str]


def _resolve_user_context(db: Session, user_id: uuid.UUID) -> UserContext:
    """Load user + memberships + permissions from the DB.

    Raises 401 if user is missing, 403 if the account is disabled
    or soft-deleted (M6 decision #9). Soft-deleted users keep their
    `audit_logs.user_id` FK intact but every live request path
    short-circuits as `user_disabled` so existing JWTs are rejected
    on the next call without explicit revocation.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user_not_found",
        )
    if user.deleted_at is not None or user.status != "enable":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_disabled",
        )

    workspace_ids: set[uuid.UUID] = set()
    permission_keys: set[str] = set()
    if user.is_super_admin:
        # Super-admins see every workspace and bypass per-permission checks
        # via the '*' wildcard. They still flow through the same RBAC
        # path so audit logs remain consistent.
        all_workspaces = db.execute(select(Workspace.id)).scalars().all()
        # M6 revised (2026-08-25): filter `__system__` out of the
        # workspace list — it's the designated container for
        # system-scoped role bindings, not a user-switchable workspace.
        workspace_ids.update(ws for ws in all_workspaces if ws != SYSTEM_WORKSPACE_ID)
        permission_keys.add("*")
    else:
        # Aggregate role permissions across every workspace the user is in
        stmt = (
            select(UserRole.workspace_id, Permission.key)
            .join(Role, Role.id == UserRole.role_id)
            .join(
                __import__(
                    "agentic_rag_project.db.models", fromlist=["RolePermission"]
                ).RolePermission,
                onclause=__import__(
                    "agentic_rag_project.db.models", fromlist=["RolePermission"]
                ).RolePermission.role_id
                == Role.id,
            )
            .join(
                Permission,
                Permission.id
                == __import__(
                    "agentic_rag_project.db.models", fromlist=["RolePermission"]
                ).RolePermission.permission_id,
            )
            .where(
                UserRole.user_id == user.id,
                Role.status == "enable",
            )
        )
        for ws_id, perm_key in db.execute(stmt).all():
            # M6 revised (2026-08-25): drop `__system__` from the
            # workspace list but still collect its permissions. The
            # system_admin binding (workspace_id == SYSTEM_WORKSPACE_ID)
            # exists only to carry cross-workspace permissions
            # (audit:read, sensitive:read/update); the workspace
            # itself is never meant to be a switchable target.
            if ws_id != SYSTEM_WORKSPACE_ID:
                workspace_ids.add(ws_id)
            permission_keys.add(perm_key)

    return UserContext(
        user_id=user.id,
        username=user.username,
        is_super_admin=user.is_super_admin,
        status=user.status,
        workspace_ids=frozenset(workspace_ids),
        permissions=frozenset(permission_keys),
    )


def _resolve_from_api_token(
    db: Session, plaintext: str
) -> uuid.UUID:
    """Look up an `sk-`-prefixed API token and return the owning user_id.

    Returns None when the token is malformed, unknown, revoked,
    expired, or its owner is soft-deleted / disabled — the caller
    surfaces a 401 in all of these so the response shape doesn't
    leak the token's status.
    """
    validated = validate_api_token(db, plaintext)
    if validated is None:
        return None  # type: ignore[return-value]
    return validated[0]


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> UserContext:
    """Resolve the Authorization header to a UserContext.

    Supports two credential schemes:
      - JWT  (`Bearer <jwt>`)            — primary auth for browser
                                            and short-lived client use.
      - API token (`Bearer sk-xxxxx...`) — personal access token for
                                            programmatic / CLI clients
                                            (Page 14 / T5.7).

    The resolved context is also stashed on `request.state.user_ctx` so
    downstream dependencies (audit logger, etc.) can read it without
    re-parsing the token.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_bearer_token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    user_id: uuid.UUID | None = None

    # M6 / T5.7: `sk-`-prefixed tokens branch BEFORE the JWT path so a
    # token that happens to look like a JWT (e.g. accidentally
    # generated with three base64 segments) does not blow up the
    # JWT decode with confusing error logs.
    if token.startswith(API_TOKEN_SCHEME):
        user_id = _resolve_from_api_token(db, token)
    else:
        try:
            payload = decode_access_token(token)
        except TokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"invalid_token: {exc}",
            ) from exc
        try:
            user_id = uuid.UUID(payload["sub"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="malformed_subject",
            ) from exc

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
        )

    ctx = _resolve_user_context(db, user_id)
    request.state.user_ctx = ctx
    return ctx


def require_super_admin(
    ctx: Annotated[UserContext, Depends(get_current_user)],
) -> UserContext:
    """Block the request unless `is_super_admin=True`."""
    if not ctx.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="super_admin_required",
        )
    return ctx


def require_permission(permission_key: str):
    """Build a dependency that enforces `permission_key` membership.

    Usage:
        @router.post(...)
        def create_role(
            _: Annotated[None, Depends(require_permission("admin:role:create"))],
        ):
            ...
    """
    wildcard = "*"

    def _checker(
        ctx: Annotated[UserContext, Depends(get_current_user)],
    ) -> UserContext:
        if wildcard in ctx.permissions or permission_key in ctx.permissions:
            return ctx
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"permission_denied: {permission_key}",
        )

    return _checker


def resolve_target_workspace(
    ctx: UserContext,
    workspace_id: str,
) -> uuid.UUID:
    """Resolve a request's `workspace_id` query param to a `uuid.UUID`.

    Used by the M4.4 CSAT admin endpoints (and any future
    workspace-scoped read API) to enforce that the caller is a
    member of the requested workspace. Super-admins bypass the
    membership check (they see every workspace).

    The input is a **required** string — CSAT endpoints always
    take `workspace_id` as a required `Query(...)`. For endpoints
    where the caller may omit the parameter, the caller should
    default to `next(iter(ctx.workspace_ids))` and pass that
    string in instead (the `feedback_router.post_feedback` path
    uses this pattern).

    Raises:
        HTTPException(400): `workspace_id` is not a valid UUID.
        HTTPException(403): caller is not a super-admin and is
            not a member of the requested workspace.

    Why centralised: three CSAT endpoints need identical
    validation logic; inlining it once per route duplicates
    error shapes and risks drift if the rule changes.
    """
    try:
        target = uuid.UUID(workspace_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid_workspace_id: {workspace_id!r}",
        ) from exc
    if ctx.is_super_admin:
        return target
    if target not in ctx.workspace_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not_a_member_of_workspace",
        )
    return target


# ---------------------------------------------------------------------------
# M4.3 — governance dependencies (audit service + query guardrail)
# ---------------------------------------------------------------------------


def get_redis_dependency():
    """Return the lifespan-cached `redis.Redis` client.

    Reuses `retrieval_direct.redis_client.get_redis_client()` which
    caches by `id(settings)` (dict-keyed, not lru_cache — see
    `lifespan_settings_hash_bug` closed 2026-08-25).
    """
    from agentic_rag_project.retrieval_direct.redis_client import (
        get_redis_client,
    )

    return get_redis_client(get_settings())


def get_session_factory():
    """Return a callable that opens a fresh Session on each call."""
    from agentic_rag_project.db.session import _session_factory

    return _session_factory()


def get_audit_service():
    """FastAPI dependency that produces a request-scoped `AuditService`.

    Reuses the cached Redis client + the process-wide session factory
    so we don't open a new connection per call. Also passes the
    Prometheus metrics singleton so `record()` can bump
    `audit_log_total{action, status}` for the `HighAccessDeniedRate`
    alert (M5 close-out, 2026-08-27).
    """
    from agentic_rag_project.audit import AuditService
    from agentic_rag_project.observability.registry import get_metrics

    return AuditService(
        redis_client=get_redis_dependency(),
        session_factory=get_session_factory(),
        metrics=get_metrics(),
    )


def get_query_guardrail():
    """FastAPI dependency that produces the singleton `QueryGuardrail`.

    The guardrail is read-mostly after construction (its trie + regex
    are immutable until admin updates the word list), so a single
    module-level object is fine.
    """
    from agentic_rag_project.post_processor.filter import (
        build_default_filter,
    )
    from agentic_rag_project.query_guardrail import QueryGuardrail

    return QueryGuardrail(
        sensitive_filter=build_default_filter(get_redis_dependency()),
    )