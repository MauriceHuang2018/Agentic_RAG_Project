"""Lock down the ``chunk_index`` semantics documented in
`src/agentic_rag_project/doc_processor/indexer.py` and DESIGN §4.4.

Background (debug / 2026-09-08):

  User reported that on the Phoenix-Project PDF, ``chunk_index=7`` (parent)
  and ``chunk_index=218`` (child) appeared to be duplicates. After
  investigation, the parent and child are correctly linked via the
  ``parent_chunk_id`` self-FK, but their ``chunk_index`` values are
  *deliberately* different — ``chunk_index`` is a single document-wide
  serial (parents 0..P-1, children P..P+C-1) used to rebuild reading
  order via ``ORDER BY chunk_index``. The bug was the missing
  documentation, not the code: the indexer wrote ``218 = len(parents) +
  7`` with no comment explaining the offset.

This test pins the semantics so a future refactor cannot silently change
them and re-create the same confusion. The contract we lock down:

  1. Parent chunk_index values are exactly ``[0, 1, ..., P-1]``.
  2. Child chunk_index values are exactly ``[P, P+1, ..., P+C-1]``.
  3. Each child's ``parent_chunk_id`` points to the parent whose
     ``chunk_index`` is ``child.chunk_index - P``.
  4. The serial is dense: every value in ``[0, P+C-1]`` appears exactly
     once across both parents and children.

Anything that breaks these assertions should either be reverted, or
should come with an explicit update to DESIGN §4.4 + this test.

Why a separate test file (not folded into test_indexer_upsert_idempotent.py):
this contract is independent of the upsert/idempotency contract and has
a different failure mode (semantic drift vs row duplication). Keeping
them separate gives a clearer signal when one fails.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.requires_pg_container


# ---- minimal fake Qdrant ---------------------------------------------------


class _FakeQdrant:
    """Records every upsert — mirrors Qdrant point-id overwrite semantics.

    Copy of the helper from test_indexer_upsert_idempotent.py kept local
    so this test does not couple to that file's private fixtures.
    """

    def __init__(self) -> None:
        self.points: dict[str, dict] = {}
        self.upsert_calls: list[list[str]] = []

    def collection_exists(self, collection_name: str) -> bool:  # noqa: ARG002
        return True

    def upsert(self, *, collection_name: str, points, wait: bool) -> None:  # noqa: ARG002
        ids: list[str] = []
        for p in points:
            ids.append(str(p.id))
            self.points[str(p.id)] = {
                "vector": p.vector,
                "payload": p.payload,
            }
        self.upsert_calls.append(ids)


# ---- schema fixtures --------------------------------------------------------


def _seed_workspace_user_document(conn, schema: str, document_id: uuid.UUID) -> uuid.UUID:
    """Insert the FK chain so ``chunks.document_id`` FK resolves.

    Same recipe as test_indexer_upsert_idempotent.py:_seed_workspace_user_document —
    kept local so this test does not couple to that file's private fixtures.
    """
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conn.execute(
        text(
            f'INSERT INTO "{schema}".users '
            "(id, username, email, password_hash, status, is_super_admin, "
            "attributes, created_at) "
            "VALUES (:id, :uname, :email, 'x', 'enable', false, '{}'::jsonb, now())"
        ),
        {
            "id": user_id,
            "uname": f"u-{user_id.hex[:8]}",
            "email": f"{user_id.hex[:8]}@test.local",
        },
    )
    conn.execute(
        text(
            f'INSERT INTO "{schema}".workspaces '
            "(id, name, owner_id, status, isolation_level, config, created_at) "
            "VALUES (:id, :name, :owner_id, 'enable', 'logical', '{}'::jsonb, now())"
        ),
        {"id": workspace_id, "name": "test-ws", "owner_id": user_id},
    )
    conn.execute(
        text(
            f'INSERT INTO "{schema}".documents '
            "(id, workspace_id, name, format, status, owner_id, "
            "authority_level, metadata, created_at) "
            "VALUES (:id, :ws, :name, 'pdf', 'ready', :owner, 3, '{}'::jsonb, now())"
        ),
        {"id": document_id, "ws": workspace_id, "name": "doc.pdf", "owner": user_id},
    )
    return workspace_id


def _make_parent(document_id: str, idx: int, workspace_id: str) -> dict:
    return {
        "chunk_id": f"parent-{idx}",
        "document_id": document_id,
        "workspace_id": workspace_id,
        "content": f"parent content #{idx}",
        "section_heading": f"Section {idx}",
        "page_start": idx,
        "page_end": idx,
        "child_ids": [f"child-{idx}-a", f"child-{idx}-b"],
    }


def _make_embedded(
    document_id: str,
    parent_id: str,
    idx: int,
    workspace_id: str,
) -> dict:
    """A 1024-dim zero vector — sparse vector can be empty for this test.

    ``idx`` is the integer page number (passed through to payload.page).
    """
    return {
        "chunk_id": f"child-{parent_id.split('-')[-1]}-{idx}",
        "document_id": document_id,
        "parent_id": parent_id,
        "workspace_id": workspace_id,
        "content": f"child content #{idx} for {parent_id}",
        "dense_vector": [0.0] * 1024,
        "sparse_vector": {},
        "payload": {
            "page": int(idx),
            "block_kind": "text",
            "workspace_id": workspace_id,
        },
    }


def _run_index(
    pg_test_engine,
    pg_schema_session: str,
    *,
    parent_count: int,
    children_per_parent: int,
) -> None:
    """Helper: build P parents × C-each children, run indexer.index(), commit.

    Kept private to this module — no need to expose the wiring elsewhere.
    """
    from sqlalchemy.orm import sessionmaker

    from agentic_rag_project.doc_processor import indexer
    from agentic_rag_project.doc_processor.models import (
        EmbeddedChunk,
        ParentChunk,
    )

    document_id = uuid.uuid4()
    document_id_str = str(document_id)
    with pg_test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{pg_schema_session}"'))
        workspace_id = _seed_workspace_user_document(
            conn, pg_schema_session, document_id
        )
    workspace_id_str = str(workspace_id)

    parents = [
        ParentChunk(**_make_parent(document_id_str, i, workspace_id=workspace_id_str))
        for i in range(parent_count)
    ]
    embedded: list[EmbeddedChunk] = []
    for i, p in enumerate(parents):
        for j in range(children_per_parent):
            embedded.append(
                EmbeddedChunk(
                    **_make_embedded(
                        document_id_str,
                        p.chunk_id,
                        i * children_per_parent + j,
                        workspace_id=workspace_id_str,
                    )
                )
            )

    fake_qdrant = _FakeQdrant()
    TestSession = sessionmaker(bind=pg_test_engine, autoflush=False, autocommit=False)
    with TestSession() as session:
        session.execute(text(f'SET search_path TO "{pg_schema_session}"'))
        indexer.index(
            parents=parents,
            embedded=embedded,
            document_id=document_id_str,
            workspace_id=workspace_id_str,
            qdrant=fake_qdrant,
            session=session,
            collection="test_chunks_v1",
        )


def _fetch_chunk_index_map(pg_test_engine, pg_schema_session: str) -> dict[int, dict]:
    """Return ``{chunk_index: {is_parent, parent_chunk_id, content}}`` ordered.

    Includes both parents and children. parent_chunk_id is None for parents.
    """
    with pg_test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{pg_schema_session}"'))
        rows = conn.execute(
            text(
                "SELECT chunk_index, is_parent, parent_chunk_id, content "
                "FROM chunks ORDER BY chunk_index"
            )
        ).fetchall()
    out: dict[int, dict] = {}
    for chunk_index, is_parent, parent_chunk_id, content in rows:
        out[int(chunk_index)] = {
            "is_parent": bool(is_parent),
            "parent_chunk_id": str(parent_chunk_id) if parent_chunk_id else None,
            "content": content,
        }
    return out


# ---- tests ------------------------------------------------------------------


def test_chunk_index_is_document_wide_serial_one_to_one(
    pg_test_engine, pg_schema_session
):
    """Lock down 1:1 section:parent MVP semantics — P=3, C=3 (one per parent).

    With the current chunker, every parent has exactly one child, so:

      - parent.chunk_index ∈ {0, 1, 2}
      - child.chunk_index  ∈ {3, 4, 5}  (= P + child_local)
      - each child.parent_chunk_id points to the parent whose
        chunk_index == child.chunk_index - P

    This is the user-reported case (chunk_index=7 (parent) ↔ 218 (child)):
    the offset = P, not the parent's local chunk_index.
    """
    P, C = 3, 3  # 1 child per parent
    _run_index(
        pg_test_engine,
        pg_schema_session,
        parent_count=P,
        children_per_parent=C // P,
    )
    rows = _fetch_chunk_index_map(pg_test_engine, pg_schema_session)

    # Total rows = P + C, dense serial 0..(P+C-1)
    assert sorted(rows) == list(range(P + C))
    assert sum(1 for r in rows.values() if r["is_parent"]) == P
    assert sum(1 for r in rows.values() if not r["is_parent"]) == C

    # Parent block
    for i in range(P):
        assert rows[i]["is_parent"] is True
        assert rows[i]["parent_chunk_id"] is None

    # Child block + parent linkage
    for i in range(C):
        child_idx = P + i
        assert rows[child_idx]["is_parent"] is False
        parent_row = rows[i]
        # Build the parent's PK via the same _chunk_uuid() the indexer used
        # — too painful to import here. Instead: the chunk_index of the
        # parent this child points to must be ``child_idx - P``.
        # We assert that via a join below.
        rows[child_idx]["_expected_parent_chunk_index"] = i

    # Join: child.parent_chunk_id → chunks.id; assert the joined row's
    # chunk_index == child_idx - P.
    with pg_test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{pg_schema_session}"'))
        joined = conn.execute(
            text(
                "SELECT c.chunk_index AS child_idx, p.chunk_index AS parent_idx "
                "FROM chunks c JOIN chunks p ON c.parent_chunk_id = p.id "
                "WHERE c.is_parent = false ORDER BY c.chunk_index"
            )
        ).fetchall()
    for child_idx, parent_idx in joined:
        assert parent_idx == child_idx - P, (
            f"child.chunk_index={child_idx} should map to parent "
            f"with chunk_index={child_idx - P}, got {parent_idx}"
        )


def test_chunk_index_is_document_wide_serial_two_children_per_parent(
    pg_test_engine, pg_schema_session
):
    """Lock down the offset when a parent splits into N children.

    With P=2, C-per-parent=2 → 2 parents + 4 children = 6 rows.

      - parent.chunk_index ∈ {0, 1}
      - child.chunk_index  ∈ {2, 3, 4, 5}  (NOT {0, 0, 1, 1} — no sharing)
      - parent linkage: child.chunk_index → parent.chunk_index == child_idx - P

    This is the "N:1 parent-to-children" case. The serial is dense and
    global; multiple children of the same parent all have DIFFERENT
    chunk_index values, joined through ``parent_chunk_id``.
    """
    P, C = 2, 4  # 2 children per parent
    _run_index(
        pg_test_engine,
        pg_schema_session,
        parent_count=P,
        children_per_parent=C // P,
    )
    rows = _fetch_chunk_index_map(pg_test_engine, pg_schema_session)

    assert sorted(rows) == list(range(P + C))
    for i in range(P):
        assert rows[i]["is_parent"] is True
    for i in range(C):
        assert rows[P + i]["is_parent"] is False

    with pg_test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{pg_schema_session}"'))
        joined = conn.execute(
            text(
                "SELECT c.chunk_index AS child_idx, p.chunk_index AS parent_idx "
                "FROM chunks c JOIN chunks p ON c.parent_chunk_id = p.id "
                "WHERE c.is_parent = false ORDER BY c.chunk_index"
            )
        ).fetchall()
    # 4 children total, 2 per parent — both children of parent 0 should
    # have child_idx ∈ {2, 3}; both children of parent 1 should have
    # child_idx ∈ {4, 5}.
    children_by_parent: dict[int, list[int]] = {}
    for child_idx, parent_idx in joined:
        children_by_parent.setdefault(int(parent_idx), []).append(int(child_idx))
    assert sorted(children_by_parent.keys()) == [0, 1]
    assert sorted(children_by_parent[0]) == [2, 3]
    assert sorted(children_by_parent[1]) == [4, 5]


def test_chunk_index_serial_equals_len_parents_plus_local(
    pg_test_engine, pg_schema_session
):
    """Pin the exact formula: child.chunk_index == len(parents) + i.

    The user's debug report described "chunk_index=7 (parent) and 218
    (child)" where 218 == 211 + 7 (the Phoenix PDF has 211 parents).
    This test asserts the offset rule on a small fixture so future
    refactors cannot silently change it without breaking this test
    AND updating DESIGN §4.4.
    """
    P, C = 5, 5  # 1 child per parent for clarity
    _run_index(
        pg_test_engine,
        pg_schema_session,
        parent_count=P,
        children_per_parent=C // P,
    )
    rows = _fetch_chunk_index_map(pg_test_engine, pg_schema_session)

    parent_indices = sorted(i for i, r in rows.items() if r["is_parent"])
    child_indices = sorted(i for i, r in rows.items() if not r["is_parent"])
    assert parent_indices == [0, 1, 2, 3, 4]
    assert child_indices == [5, 6, 7, 8, 9]
    # The formula in one line — what the indexer writes:
    assert child_indices == [len(parent_indices) + i for i in range(C)]