"""Personal API Token service — Page 14 (M6 / T5.7).

DESIGN §4.6.4: each user can mint personal access tokens for
programmatic access to the chat API. Token contract:

  - Public format: `sk-<8-char-prefix><secret>` (e.g. `sk-1A2b3C4d...`)
    — the prefix is what the UI displays so the user can identify a
    token at a glance; the secret after the prefix is what makes the
    token unguessable.
  - At rest: only `token_hash` (SHA-256 hex of the full plaintext
    token, 64 chars) and `token_prefix` (first 8 chars of the
    SECRET, i.e. the characters immediately after `sk-`) are stored.
  - The plaintext is shown to the user ONCE at creation time and
    never persisted. Loss of the plaintext = revocation + new token.
  - Lookups are by `(token_prefix, token_hash)`; the prefix index
    keeps the row count small per scan, the hash check filters the
    last few candidates.
  - Revoked or expired tokens are rejected at lookup time; the
    `last_used_at` column is bumped on every successful auth.

The bearer scheme prefix `sk-` distinguishes these tokens from
short-lived JWTs (`Authorization: Bearer sk-1A2b3C4d...`) without
requiring a new HTTP scheme.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.db.models import ApiToken, User

logger = logging.getLogger(__name__)


# Public constants — re-exported via `services/__init__.py`.
API_TOKEN_SCHEME: Final[str] = "sk-"
API_TOKEN_PREFIX_LEN: Final[int] = 8
# A plaintext token is `sk-` + 8 prefix chars + at least 24 secret
# chars = 35 chars total minimum. We round up to 40 for safety so
# entropy stays above 190 bits.
API_TOKEN_SECRET_MIN_LEN: Final[int] = 32


class ApiTokenError(Exception):
    """Raised when an API token operation fails (validation, persistence)."""


@dataclass(frozen=True, slots=True)
class GeneratedToken:
    """One freshly minted token. The plaintext is exposed ONLY in
    `plaintext`; it never gets stored."""

    plaintext: str  # `sk-xxxxxxxx...` — show ONCE to the user
    token_prefix: str  # 8 chars after `sk-` — stored + displayed
    token_hash: str  # 64-char hex — stored


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------------------------------------------------------
# Pure helpers (no DB)
# -----------------------------------------------------------------------------


def token_prefix_from(plaintext: str) -> str:
    """Return the 8-char prefix stored alongside the hash.

    Raises `ApiTokenError` if the input is malformed (wrong scheme,
    too short, non-ascii prefix).
    """
    if not plaintext.startswith(API_TOKEN_SCHEME):
        raise ApiTokenError(
            f"token must start with '{API_TOKEN_SCHEME}'"
        )
    body = plaintext[len(API_TOKEN_SCHEME):]
    if len(body) < API_TOKEN_PREFIX_LEN:
        raise ApiTokenError(
            f"token body too short (got {len(body)}, "
            f"need >= {API_TOKEN_PREFIX_LEN})"
        )
    prefix = body[:API_TOKEN_PREFIX_LEN]
    try:
        prefix.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ApiTokenError("token prefix must be ascii") from exc
    return prefix


def hash_api_token(plaintext: str) -> str:
    """SHA-256 hex digest of the FULL plaintext token (including `sk-`)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_token() -> GeneratedToken:
    """Build a fresh token: scheme + 8-char prefix + URL-safe secret."""
    # `secrets.token_urlsafe(32)` yields ~43 url-safe chars; we trim
    # to the minimum to keep the public token compact while still
    # giving ~190 bits of entropy.
    secret = secrets.token_urlsafe(API_TOKEN_SECRET_MIN_LEN)
    plaintext = f"{API_TOKEN_SCHEME}{secrets.token_hex(4)}{secret}"
    return GeneratedToken(
        plaintext=plaintext,
        token_prefix=token_prefix_from(plaintext),
        token_hash=hash_api_token(plaintext),
    )


# -----------------------------------------------------------------------------
# DB-backed helpers
# -----------------------------------------------------------------------------


def issue_api_token(
    session: Session,
    *,
    user_id: uuid.UUID,
    name: str,
    expires_in_days: int | None = None,
) -> tuple[GeneratedToken, ApiToken]:
    """Mint a new token, persist its hash + prefix, return both the
    plaintext bundle and the persisted row.

    Args:
        session: SQLAlchemy session. Caller commits.
        user_id: Owner of the new token.
        name: User-supplied label (e.g. "my laptop").
        expires_in_days: When set, compute `expires_at = now + days`.
            When None, the token never expires (production default
            for system_admin dev tokens; users can set their own
            expiry in Page 14).
    """
    if session.get(User, user_id) is None:
        raise ApiTokenError(f"user {user_id} not found")
    if not name.strip():
        raise ApiTokenError("name must not be empty")

    gen = generate_api_token()
    now = _utcnow()
    expires_at = (
        now + timedelta(days=expires_in_days) if expires_in_days else None
    )
    row = ApiToken(
        user_id=user_id,
        name=name.strip(),
        token_hash=gen.token_hash,
        token_prefix=gen.token_prefix,
        created_at=now,
        expires_at=expires_at,
        revoked_at=None,
        last_used_at=None,
    )
    session.add(row)
    session.flush()  # populate row.id without committing
    return gen, row


def lookup_active_token(
    session: Session,
    plaintext: str,
) -> ApiToken | None:
    """Resolve a plaintext token to its persisted row.

    Returns None when the prefix/hash don't match, or when the row
    is revoked / expired / its owner is soft-deleted. The caller
    (auth dependency) treats None as 401 with the same response
    body as a JWT failure so token-existence isn't leakable.
    """
    try:
        prefix = token_prefix_from(plaintext)
    except ApiTokenError:
        return None
    token_hash = hash_api_token(plaintext)
    row = session.execute(
        select(ApiToken).where(
            ApiToken.token_prefix == prefix,
            ApiToken.token_hash == token_hash,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    # Revocation / expiry / soft-delete checks.
    if row.revoked_at is not None:
        return None
    now = _utcnow()
    if row.expires_at is not None and row.expires_at <= now:
        return None
    owner = session.get(User, row.user_id)
    if owner is None or owner.deleted_at is not None or owner.status != "enable":
        return None
    return row


def validate_api_token(
    session: Session,
    plaintext: str,
) -> tuple[uuid.UUID, ApiToken] | None:
    """Lookup + bump `last_used_at`, returning the (user_id, row) on success.

    Wraps `lookup_active_token` and adds the `last_used_at` write.
    Callers should still wrap this in a transaction so the bump and
    the downstream work commit together.
    """
    row = lookup_active_token(session, plaintext)
    if row is None:
        return None
    row.last_used_at = _utcnow()
    session.flush()
    return row.user_id, row


__all__ = [
    "API_TOKEN_PREFIX_LEN",
    "API_TOKEN_SCHEME",
    "API_TOKEN_SECRET_MIN_LEN",
    "ApiTokenError",
    "GeneratedToken",
    "generate_api_token",
    "hash_api_token",
    "issue_api_token",
    "lookup_active_token",
    "token_prefix_from",
    "validate_api_token",
]