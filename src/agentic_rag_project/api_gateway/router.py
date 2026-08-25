"""Authentication endpoints (`/auth/login`, `/auth/logout`).

Login is the only public endpoint; everything else downstream inherits
auth via `Depends(get_current_user)` (DESIGN 5.2 row 1–2).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_rag_project.api_gateway.jwt import create_access_token
from agentic_rag_project.db.models import User
from agentic_rag_project.db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


def hash_password(plain: str) -> str:
    """Hash a plaintext password (bcrypt).

    Bcrypt silently truncates input > 72 bytes; we explicitly cap here so
    the behaviour is not implementation-dependent. The cap is applied
    after UTF-8 encoding (a multi-byte char is one or more bytes).
    """
    encoded = plain.encode("utf-8")[:72]
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt comparison."""
    encoded = plain.encode("utf-8")[:72]
    try:
        return bcrypt.checkpw(encoded, hashed.encode("ascii"))
    except ValueError:
        return False


@router.post("/login")
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Authenticate via OAuth2 password form. Returns access token + user.

    Returns 401 on bad credentials, 403 if the account is disabled.
    """
    user = db.execute(
        select(User).where(User.username == form.username)
    ).scalar_one_or_none()
    if user is None or not verify_password(form.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_credentials",
        )
    # M6 decision #9: soft-deleted users cannot log in. Returned as
    # `user_disabled` (not `invalid_credentials`) so admins can debug
    # from the response body without leaking the username list.
    if user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_disabled",
        )
    if user.status != "enable":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_disabled",
        )
    token = create_access_token(
        user.id, extra_claims={"is_super_admin": user.is_super_admin}
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "username": user.username,
            "is_super_admin": user.is_super_admin,
            "status": user.status,
        },
    }


@router.post("/logout")
async def logout() -> dict:
    """Stateless logout.

    JWTs are self-contained — the client is expected to drop the token.
    A future iteration may add a server-side revocation list (T5.6 audit
    hook), but the current contract is no-op success.
    """
    return {"ok": True}