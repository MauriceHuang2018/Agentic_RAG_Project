"""Tests for `rbac.seed.seed_demo_data` (M6 — Page 2 chat smoke).

`seed_demo_data()` bootstraps two demo workspaces (`acme-hq` /
`acme-rd`) + two demo users (`alice` / `bob`) + their workspace
bindings so /chat is verifiable end to end without manually wiring
every row.

The tests below pin the contract:
  * Layout matches `demo2/app.js` mock data (Acme Corp · 总公司 / 研发部)
  * Idempotency (call twice → no duplicates, no IntegrityError)
  * Placeholder owners are `status='disable'` and refuse login
  * Bindings line up (alice in both workspaces, bob only in acme-hq)
  * Passwords come from settings, not hardcoded
  * All UUIDs are deterministic (uuid5) — same values across runs
  * Empty / missing-role input returns {} instead of partial seeding
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agentic_rag_project.db.models.base import Base
from agentic_rag_project.db.models.rbac import Role, UserRole
from agentic_rag_project.db.models.users import User, Workspace
from agentic_rag_project.rbac import (
    DEMO_ACME_HQ_ID,
    DEMO_ACME_HQ_NAME,
    DEMO_ACME_HQ_OWNER_ID,
    DEMO_ACME_RD_ID,
    DEMO_ACME_RD_NAME,
    DEMO_ACME_RD_OWNER_ID,
    DEMO_ALICE_USERNAME,
    DEMO_BOB_USERNAME,
    seed_builtin_roles,
    seed_demo_data,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Iterator[Session]:
    """In-memory SQLite with built-in roles seeded. Single shared connection."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    seed_builtin_roles(s)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def empty_session() -> Iterator[Session]:
    """In-memory SQLite WITHOUT `seed_builtin_roles` — used to verify
    `seed_demo_data` returns {} gracefully when the role catalog is
    missing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_seed_demo_data_creates_workspaces_and_users(
    session: Session,
) -> None:
    """First call: 2 workspaces + 2 demo users + 3 bindings."""
    result = seed_demo_data(session)

    # Returned id map matches the uuid5 constants.
    assert result["acme_hq_id"] == DEMO_ACME_HQ_ID
    assert result["acme_rd_id"] == DEMO_ACME_RD_ID
    assert result["alice_id"] is not None
    assert result["bob_id"] is not None
    assert result["alice_id"] != result["bob_id"]

    # Workspaces present with the right names.
    hq = session.get(Workspace, DEMO_ACME_HQ_ID)
    assert hq is not None
    assert hq.name == DEMO_ACME_HQ_NAME
    rd = session.get(Workspace, DEMO_ACME_RD_ID)
    assert rd is not None
    assert rd.name == DEMO_ACME_RD_NAME

    # Demo users present.
    alice = session.execute(
        select(User).where(User.username == DEMO_ALICE_USERNAME)
    ).scalar_one()
    assert alice.id == result["alice_id"]
    assert alice.status == "enable"
    assert alice.is_super_admin is False
    assert alice.display_name == "Alice"

    bob = session.execute(
        select(User).where(User.username == DEMO_BOB_USERNAME)
    ).scalar_one()
    assert bob.id == result["bob_id"]
    assert bob.status == "enable"
    assert bob.is_super_admin is False
    assert bob.display_name == "Bob"


def test_seed_demo_data_creates_placeholder_owners(
    session: Session,
) -> None:
    """The owner rows are present, locked, and refuse login."""
    seed_demo_data(session)

    hq_owner = session.get(User, DEMO_ACME_HQ_OWNER_ID)
    assert hq_owner is not None
    assert hq_owner.username == "__demo_acme_hq_owner__"
    assert hq_owner.status == "disable"
    assert hq_owner.is_super_admin is False
    # The hash is a placeholder marker — never a real bcrypt digest.
    assert hq_owner.password_hash.startswith("!")

    rd_owner = session.get(User, DEMO_ACME_RD_OWNER_ID)
    assert rd_owner is not None
    assert rd_owner.username == "__demo_acme_rd_owner__"
    assert rd_owner.status == "disable"

    # Workspaces correctly reference the placeholder owners.
    hq = session.get(Workspace, DEMO_ACME_HQ_ID)
    assert hq is not None
    assert hq.owner_id == DEMO_ACME_HQ_OWNER_ID
    rd = session.get(Workspace, DEMO_ACME_RD_ID)
    assert rd is not None
    assert rd.owner_id == DEMO_ACME_RD_OWNER_ID


def test_seed_demo_data_creates_bindings(
    session: Session,
) -> None:
    """alice → chat_user in both workspaces; bob → kb_admin in hq."""
    seed_demo_data(session)
    chat_user = session.execute(
        select(Role).where(Role.name == "chat_user")
    ).scalar_one()
    kb_admin = session.execute(
        select(Role).where(Role.name == "kb_admin")
    ).scalar_one()

    alice = session.execute(
        select(User).where(User.username == DEMO_ALICE_USERNAME)
    ).scalar_one()
    bob = session.execute(
        select(User).where(User.username == DEMO_BOB_USERNAME)
    ).scalar_one()

    bindings = session.execute(
        select(UserRole).where(UserRole.user_id.in_([alice.id, bob.id]))
    ).scalars().all()
    assert len(bindings) == 3

    # alice → chat_user in acme-hq
    assert any(
        b.user_id == alice.id
        and b.role_id == chat_user.id
        and b.workspace_id == DEMO_ACME_HQ_ID
        for b in bindings
    )
    # alice → chat_user in acme-rd
    assert any(
        b.user_id == alice.id
        and b.role_id == chat_user.id
        and b.workspace_id == DEMO_ACME_RD_ID
        for b in bindings
    )
    # bob → kb_admin in acme-hq
    assert any(
        b.user_id == bob.id
        and b.role_id == kb_admin.id
        and b.workspace_id == DEMO_ACME_HQ_ID
        for b in bindings
    )
    # bob has no binding in acme-rd
    assert not any(
        b.user_id == bob.id and b.workspace_id == DEMO_ACME_RD_ID
        for b in bindings
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_seed_demo_data_is_idempotent(session: Session) -> None:
    """Calling twice produces no duplicates and does not raise."""
    seed_demo_data(session)
    snapshot_users = (
        session.execute(select(User)).scalars().all()
    )
    snapshot_workspaces = (
        session.execute(select(Workspace)).scalars().all()
    )
    snapshot_bindings = (
        session.execute(select(UserRole)).scalars().all()
    )

    # Second call should not raise; counts unchanged.
    seed_demo_data(session)

    users_after = session.execute(select(User)).scalars().all()
    ws_after = session.execute(select(Workspace)).scalars().all()
    bindings_after = session.execute(select(UserRole)).scalars().all()

    assert len(users_after) == len(snapshot_users)
    assert len(ws_after) == len(snapshot_workspaces)
    assert len(bindings_after) == len(snapshot_bindings)


def test_seed_demo_data_preserves_pre_existing_alice_role(
    session: Session,
) -> None:
    """Re-seeding must not stomp on pre-existing bindings for alice.
    If alice already had kb_admin in acme-rd, seed_demo_data's
    second call should keep it (idempotent only adds its own
    bindings, never deletes)."""
    seed_demo_data(session)

    alice = session.execute(
        select(User).where(User.username == DEMO_ALICE_USERNAME)
    ).scalar_one()
    kb_admin = session.execute(
        select(Role).where(Role.name == "kb_admin")
    ).scalar_one()

    # Add an extra binding BEFORE the second seed pass.
    session.add(
        UserRole(
            user_id=alice.id,
            role_id=kb_admin.id,
            workspace_id=DEMO_ACME_RD_ID,
        )
    )
    session.commit()

    seed_demo_data(session)  # second call

    alice_bindings = session.execute(
        select(UserRole).where(UserRole.user_id == alice.id)
    ).scalars().all()
    # alice in acme-hq chat_user, acme-rd chat_user, acme-rd kb_admin
    assert len(alice_bindings) == 3


# ---------------------------------------------------------------------------
# Password handling
# ---------------------------------------------------------------------------


def test_seed_demo_data_passwords_come_from_settings(
    session: Session,
) -> None:
    """`seed_demo_data` reads passwords from Settings.demo_alice_password /
    demo_bob_password. Default values are 'alice_pass' / 'bob_pass'
    so we can verify the hash matches bcrypt of those literals."""
    import bcrypt as _bcrypt

    seed_demo_data(session)
    alice = session.execute(
        select(User).where(User.username == DEMO_ALICE_USERNAME)
    ).scalar_one()
    bob = session.execute(
        select(User).where(User.username == DEMO_BOB_USERNAME)
    ).scalar_one()
    assert _bcrypt.checkpw(b"alice_pass", alice.password_hash.encode())
    assert _bcrypt.checkpw(b"bob_pass", bob.password_hash.encode())


def test_seed_demo_data_rotates_passwords_via_env(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEMO_ALICE_PASSWORD / DEMO_BOB_PASSWORD env vars override the
    default literals. Setting them through env (which Settings picks
    up on the next get_settings()) re-hashes on the next seed pass."""
    import bcrypt as _bcrypt
    monkeypatch.setenv("DEMO_ALICE_PASSWORD", "new_alice_pw_2026")
    monkeypatch.setenv("DEMO_BOB_PASSWORD", "new_bob_pw_2026")
    # Drop lru_cache so the next get_settings() re-reads env.
    from agentic_rag_project.config import get_settings
    get_settings.cache_clear()
    try:
        seed_demo_data(session)
        alice = session.execute(
            select(User).where(User.username == DEMO_ALICE_USERNAME)
        ).scalar_one()
        bob = session.execute(
            select(User).where(User.username == DEMO_BOB_USERNAME)
        ).scalar_one()
        assert _bcrypt.checkpw(
            b"new_alice_pw_2026", alice.password_hash.encode()
        )
        assert _bcrypt.checkpw(
            b"new_bob_pw_2026", bob.password_hash.encode()
        )
    finally:
        # Restore defaults so subsequent tests aren't poisoned.
        monkeypatch.delenv("DEMO_ALICE_PASSWORD", raising=False)
        monkeypatch.delenv("DEMO_BOB_PASSWORD", raising=False)
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------


def test_seed_demo_data_returns_empty_when_roles_missing(
    empty_session: Session,
) -> None:
    """Without `seed_builtin_roles`, the role catalog is empty →
    `seed_demo_data` returns {} and leaves the DB untouched."""
    result = seed_demo_data(empty_session)
    assert result == {}

    # No workspaces / demo users were created.
    assert empty_session.execute(
        select(Workspace).where(Workspace.id == DEMO_ACME_HQ_ID)
    ).scalar_one_or_none() is None
    assert empty_session.execute(
        select(User).where(User.username == DEMO_ALICE_USERNAME)
    ).scalar_one_or_none() is None


def test_seed_demo_data_uuids_are_deterministic() -> None:
    """The exported UUID constants must be stable forever — they're
    the join keys for the demo bindings."""
    assert DEMO_ACME_HQ_ID == uuid.uuid5(
        uuid.NAMESPACE_DNS, "demo-workspace.acme-hq.rag.local"
    )
    assert DEMO_ACME_RD_ID == uuid.uuid5(
        uuid.NAMESPACE_DNS, "demo-workspace.acme-rd.rag.local"
    )
    assert DEMO_ACME_HQ_OWNER_ID == uuid.uuid5(
        uuid.NAMESPACE_DNS, "demo-owner.acme-hq.rag.local"
    )
    assert DEMO_ACME_RD_OWNER_ID == uuid.uuid5(
        uuid.NAMESPACE_DNS, "demo-owner.acme-rd.rag.local"
    )
    # Cross-collision check: all four demo UUIDs are distinct.
    ids = {
        DEMO_ACME_HQ_ID,
        DEMO_ACME_RD_ID,
        DEMO_ACME_HQ_OWNER_ID,
        DEMO_ACME_RD_OWNER_ID,
    }
    assert len(ids) == 4


def test_seed_demo_data_names_match_demo2_mock(
    session: Session,
) -> None:
    """The display names mirror the demo2/app.js prototype mock data
    (总公司 / 研发部) so the demo /chat UX matches the prototype's
    mental model."""
    seed_demo_data(session)
    hq = session.get(Workspace, DEMO_ACME_HQ_ID)
    rd = session.get(Workspace, DEMO_ACME_RD_ID)
    assert hq is not None and "总公司" in hq.name
    assert rd is not None and "研发部" in rd.name
