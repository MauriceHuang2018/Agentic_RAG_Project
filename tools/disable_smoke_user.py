"""Disable the smoke placeholder user + re-point its workspace ownership.

WHY THIS EXISTS (2026-09-08):
    `tests/integration/manualRun/04_index.py` seeds a `smoke` user with
    `password_hash="!smoke-placeholder"` — i.e. NOT a real bcrypt hash,
    just a non-NULL placeholder so the `workspaces.owner_id` NOT NULL FK
    is satisfied. The user is `is_super_admin=True` but cannot log in.

    It started showing up in the frontend's user list (Login.vue iterates
    accounts seeded by seed_demo_data / seed_demo_user / etc.), which is
    misleading — a user you can see but cannot use.

    Plan agreed with the operator on 2026-09-08: keep the user row + the
    indexed documents (they're still useful for other dev work), but
    neuter the login surface and re-point ownership so the smoke workspace
    + its documents can keep existing without the smoke user.

WHAT IT DOES (one transaction):
    1. `users` row where username='smoke':
         status='disable'
         is_super_admin=False
         username='__smoke_disabled__' (renamed to free up the handle for
         `admin` to take over the operator-account slot)
    2. `workspaces` row id=11111111-... ('smoke-ws'):
         owner_id = SYSTEM_OWNER_USER_ID (a permanently-disabled user
         already seeded by `rbac.seed` to satisfy the NOT NULL FK).
    3. `documents` rows where workspace_id=11111111-...:
         owner_id = SYSTEM_OWNER_USER_ID (same rationale).

WHAT IT DOES NOT DO:
    - Does NOT delete documents / chunks / Qdrant points. Indexing data
      stays intact; the workspace + docs remain queryable.
    - Does NOT touch user_role bindings referencing the smoke user (none
      exist — smoke had no RBAC roles).
    - Does NOT change the smoke UUID (`22222222-...`). Anything still
      FKing on it (none after step 2+3) keeps working.

Idempotent: re-running detects the renamed username and is a no-op.

Usage::

    uv run python tools/disable_smoke_user.py            # apply
    uv run python tools/disable_smoke_user.py --dry-run  # preview only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo-root importable so `agentic_rag_project` resolves without packaging.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from sqlalchemy import select, update  # noqa: E402

from agentic_rag_project.db.models import Document, User, Workspace  # noqa: E402
from agentic_rag_project.db.session import SessionLocal  # noqa: E402
from agentic_rag_project.rbac.constants import SYSTEM_OWNER_USER_ID  # noqa: E402


# Mirrored from tests/integration/manualRun/04_index.py — the manual script
# uses literal UUIDs so it can be re-run idempotently without depending on
# python-side random UUID generation.
_SMOKE_USER_ID = "22222222-2222-2222-2222-222222222222"
_SMOKE_WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
_DISABLED_USERNAME = "__smoke_disabled__"


def _preview(session, *, dry_run: bool) -> None:
    """Print the rows this script WOULD mutate, without committing."""
    user = session.get(User, _SMOKE_USER_ID)
    if user is None:
        print(f"  [skip] no user row at id={_SMOKE_USER_ID}")
    else:
        print(
            f"  users.{user.username} (id={_SMOKE_USER_ID}):\n"
            f"    status          {user.status!r} -> 'disable'\n"
            f"    is_super_admin  {user.is_super_admin} -> False\n"
            f"    username        {user.username!r} -> {_DISABLED_USERNAME!r}"
        )

    ws = session.get(Workspace, _SMOKE_WORKSPACE_ID)
    if ws is None:
        print(f"  [skip] no workspace row at id={_SMOKE_WORKSPACE_ID}")
    else:
        print(
            f"  workspaces.{ws.name} (id={_SMOKE_WORKSPACE_ID}):\n"
            f"    owner_id        {ws.owner_id} -> {SYSTEM_OWNER_USER_ID}"
        )

    docs = session.execute(
        select(Document).where(Document.workspace_id == _SMOKE_WORKSPACE_ID)
    ).scalars().all()
    if not docs:
        print("  [skip] no documents in smoke-ws (nothing to re-point)")
    else:
        print(
            f"  documents.{len(docs)} rows in smoke-ws:\n"
            f"    owner_id        -> {SYSTEM_OWNER_USER_ID} (all)"
        )

    if dry_run:
        print("\n  --dry-run: not committing.")


def _apply(session) -> None:
    """Run the mutation in a single transaction."""
    # 1. Disable + rename the smoke user. Idempotent: if the username is
    #    already renamed, skip.
    user = session.get(User, _SMOKE_USER_ID)
    if user is not None and user.username != _DISABLED_USERNAME:
        user.status = "disable"
        user.is_super_admin = False
        user.username = _DISABLED_USERNAME
        session.flush()
        print(f"  users.{_DISABLED_USERNAME}: status=disable, super_admin=False")

    # 2. Re-point smoke-ws.owner_id. Idempotent: if owner is already the
    #    system owner, skip.
    ws = session.get(Workspace, _SMOKE_WORKSPACE_ID)
    if ws is not None and ws.owner_id != SYSTEM_OWNER_USER_ID:
        ws.owner_id = SYSTEM_OWNER_USER_ID
        session.flush()
        print(f"  workspaces.{ws.name}: owner_id -> system_owner")

    # 3. Re-point documents.owner_id in smoke-ws. Idempotent: UPDATE ... WHERE
    #    current owner still equals smoke, so re-runs touch zero rows.
    result = session.execute(
        update(Document)
        .where(Document.workspace_id == _SMOKE_WORKSPACE_ID)
        .where(Document.owner_id == _SMOKE_USER_ID)
        .values(owner_id=SYSTEM_OWNER_USER_ID)
    )
    print(f"  documents: {result.rowcount} row(s) re-pointed to system_owner")

    session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the intended mutations without committing",
    )
    args = parser.parse_args()

    print("=== disable_smoke_user.py ===")
    print(
        f"  smoke user    id={_SMOKE_USER_ID}\n"
        f"  smoke workspace id={_SMOKE_WORKSPACE_ID}\n"
        f"  new owner     {SYSTEM_OWNER_USER_ID}\n"
    )

    with SessionLocal() as session:
        _preview(session, dry_run=args.dry_run)
        if args.dry_run:
            return 0
        _apply(session)

    print("\n  done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())