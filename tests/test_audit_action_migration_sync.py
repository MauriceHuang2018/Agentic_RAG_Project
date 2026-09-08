"""Sync guard: `audit.events.AuditAction` Literal must match the DB-side
whitelist in migration `0012_feedback_idempotency_and_audit_expand`.

Why: the audit pipeline has a Python-side whitelist (`AuditAction`
Literal in `events.py`) AND a Postgres CHECK constraint
(`audit_logs_action_check` in migration 0012). Both must be in
lock-step or every INSERT for a new literal will be rejected by
the DB. Adding a 12th action requires BOTH the Literal update
AND an additive migration that re-builds the CHECK; this test
catches the case where only one of the two is updated.

The test also catches the reverse drift — adding a literal to the
migration tuple without updating the Python Literal — which would
silently pass at runtime (no PG rejection) but make any caller
emitting that action a `TypeError` from the type checker.

How: AST parse both files, extract the literal set, compare.
    Fail message names the missing / extra literal so the
    contributor knows which side to update.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


# Paths relative to repo root. Both files are part of the M5
# sprint deliverables and are not expected to move; if they
# do, the test should fail loudly with the path it tried.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_EVENTS_PATH = (
    _REPO_ROOT
    / "src"
    / "agentic_rag_project"
    / "audit"
    / "events.py"
)
_MIGRATION_PATH = (
    _REPO_ROOT
    / "src"
    / "agentic_rag_project"
    / "db"
    / "migrations"
    / "versions"
    / "0012_feedback_idempotency_and_audit_expand.py"
)


def _extract_literal_members(path: Path, target_name: str) -> list[str]:
    """Find `target_name = Literal["a", "b", ...]` in `path` and
    return its member names in source order. Matches BOTH
    `AuditAction = Literal[...]` (bare Assign) and
    `AuditAction: TypeAlias = Literal[...]` (AnnAssign).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # Bare assignment: `Foo = Literal[...]`
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            target_id = getattr(target, "id", None)
            if target_id != target_name:
                continue
            if not isinstance(node.value, ast.Subscript):
                continue
            slice_node = node.value.slice
            members = _collect_str_consts(slice_node)
            if members is not None:
                return members
        # Annotated assignment: `Foo: TypeAlias = Literal[...]`
        if isinstance(node, ast.AnnAssign):
            target = node.target
            target_id = getattr(target, "id", None)
            if target_id != target_name:
                continue
            if not isinstance(node.value, ast.Subscript):
                continue
            members = _collect_str_consts(node.value.slice)
            if members is not None:
                return members
    raise AssertionError(f"{path}: no `{target_name}` Literal/assignment found")


def _collect_str_consts(node: ast.AST) -> list[str] | None:
    """If `node` is a Tuple of str Constants, return its members;
    else None. Used by `_extract_literal_members`."""
    if not isinstance(node, ast.Tuple):
        return None
    members: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            members.append(elt.value)
        else:
            return None  # any non-str breaks the Literal contract
    return members


def _extract_tuple_members(path: Path, target_name: str) -> list[str]:
    """Find `target_name = ("a", "b", ...)` in `path and return its
    members in source order. Matches both bare and annotated
    assignments. Used for the migration's `ALLOWED_ACTIONS_M5` tuple.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            target_id = getattr(target, "id", None)
            if target_id != target_name:
                continue
            if isinstance(node.value, ast.Tuple):
                members: list[str] = []
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(
                        elt.value, str
                    ):
                        members.append(elt.value)
                return members
        if isinstance(node, ast.AnnAssign):
            target = node.target
            target_id = getattr(target, "id", None)
            if target_id != target_name:
                continue
            if isinstance(node.value, ast.Tuple):
                members = []
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(
                        elt.value, str
                    ):
                        members.append(elt.value)
                return members
    raise AssertionError(f"{path}: no `{target_name}` tuple assignment found")


@pytest.fixture(scope="module")
def audit_action_members() -> list[str]:
    """Members of `AuditAction` Literal in `audit/events.py`."""
    if not _EVENTS_PATH.exists():
        pytest.skip(f"AuditAction source not found at {_EVENTS_PATH}")
    return _extract_literal_members(_EVENTS_PATH, "AuditAction")


@pytest.fixture(scope="module")
def migration_allowed_actions() -> list[str]:
    """Members of `ALLOWED_ACTIONS_M5` in migration 0012."""
    if not _MIGRATION_PATH.exists():
        pytest.skip(f"migration 0012 not found at {_MIGRATION_PATH}")
    return _extract_tuple_members(
        _MIGRATION_PATH, "ALLOWED_ACTIONS_M5"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_audit_action_literal_is_non_empty(
    audit_action_members: list[str],
) -> None:
    """Sanity: at least 1 literal; protects against accidental
    truncation of the Literal[...] annotation."""
    assert audit_action_members, "AuditAction Literal is empty"


def test_migration_allowed_actions_is_non_empty(
    migration_allowed_actions: list[str],
) -> None:
    """Sanity: at least 1 literal in the migration tuple."""
    assert migration_allowed_actions, "ALLOWED_ACTIONS_M5 is empty"


def test_audit_action_and_migration_must_have_same_members(
    audit_action_members: list[str],
    migration_allowed_actions: list[str],
) -> None:
    """The Python Literal and the DB CHECK must list exactly the same
    action names. Order does not matter — but the SET must match.
    """
    py_set = frozenset(audit_action_members)
    db_set = frozenset(migration_allowed_actions)

    missing_from_db = py_set - db_set
    missing_from_py = db_set - py_set

    assert not missing_from_db, (
        f"AuditAction literals missing from migration 0012 CHECK "
        f"(DB will reject these actions): {sorted(missing_from_db)}\n"
        f"  Python has: {sorted(py_set)}\n"
        f"  Migration has: {sorted(db_set)}"
    )
    assert not missing_from_py, (
        f"Migration 0012 CHECK has literals NOT in AuditAction "
        f"(caller will get TypeError from the type checker): "
        f"{sorted(missing_from_py)}\n"
        f"  Python has: {sorted(py_set)}\n"
        f"  Migration has: {sorted(db_set)}"
    )


def test_audit_action_count_matches_post_m5_baseline(
    audit_action_members: list[str],
) -> None:
    """The M5 close-out baseline is exactly 11 literals. If you add
    a 12th literal, this test will remind you to:
      1. Add an additive migration that re-builds the CHECK
      2. Update this test's expected count
    Do NOT update the count without also adding the migration.
    """
    assert len(audit_action_members) == 11, (
        f"AuditAction has {len(audit_action_members)} members; "
        f"M5 baseline is 11. If you added a new literal, also write "
        f"an additive migration that rebuilds audit_logs_action_check, "
        f"then update this expected count."
    )


def test_audit_action_count_matches_migration_count(
    audit_action_members: list[str],
    migration_allowed_actions: list[str],
) -> None:
    """Even before knowing the baseline count, the two sides must
    always agree on cardinality."""
    assert len(audit_action_members) == len(migration_allowed_actions), (
        f"count mismatch: Python Literal={len(audit_action_members)}, "
        f"migration tuple={len(migration_allowed_actions)}"
    )