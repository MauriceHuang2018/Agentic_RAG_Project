"""JWT creation and verification utilities.

`create_access_token()` produces an HS256-signed JWT carrying `sub`
(user_id) and `exp` (expiry). `decode_access_token()` validates the
signature and expiration, raising a typed exception on failure so the
HTTP layer can return 401 cleanly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

from agentic_rag_project.config import get_settings


class TokenError(Exception):
    """Raised when a JWT cannot be decoded or has expired."""


def create_access_token(user_id: uuid.UUID, extra_claims: dict[str, Any] | None = None) -> str:
    """Build an HS256 access token for `user_id`.

    `extra_claims` (e.g. `is_super_admin`) is merged into the payload so
    downstream code does not need to re-query the DB for every request.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int(
            (now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()
        ),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Validate signature + exp and return the JWT payload.

    Raises:
        TokenError: on signature mismatch, malformed token, or expiry.
    """
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise TokenError(str(exc)) from exc