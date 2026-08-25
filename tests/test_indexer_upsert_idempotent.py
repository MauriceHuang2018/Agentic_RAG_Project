"""Verify indexer is idempotent: running ``index()`` twice on the same
parents + embedded children must NOT double PG row count or Qdrant
point count. Backed by a real Postgres test container because
``ON CONFLICT (id) DO UPDATE`` is Postgres-specific syntax — SQLite
silently ignores ``Dialect.insert()`` upsert behavior and would mask
the bug we are locking down here.

Why this test exists: prior to the upsert change, ``_write_chunks_row``
called ``session.add(row)`` so a re-run (retry, manual reindex, two
CI workers racing) appended a duplicate row per chunk. After the
fix, only the deterministic PK (``_chunk_uuid(chunk_id)``) decides
uniqueness, so the second pass is a no-op for row count and a content
refresh for the row that was actually edited (we don't edit in this
test, so content stays identical).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.requires_pg_container


# ---- minimal fake Qdrant ---------------------------------------------------
# Real qdrant_client is not in scope for this test — we just need to verify
# the indexer's upsert path runs and that the fake tracks point counts the
# same way qdrant does (idempotent upsert by point id).


class _FakeQdrant:
    """Records every upsert; mirrors Qdrant's point-id overwrite semantics."""

    def __init__(self) -> None:
        self.points: dict[str, dict] = {}
        self.upsert_calls: list[list[str]] = []  # each call: list of point ids

    def collection_exists(self, collection_name: str) -> bool:  # noqa: ARG002
        # Real indexer calls ensure_collection only on first index() of a
        # session; we always return True so ensure_collection is a no-op.
        return True

    def upsert(self, *, collection_name: str, points, wait: bool) -> None:  # noqa: ARG002
        ids: list[str] = []
        for p in points:
            ids.append(str(p.id))
            # Mirror Qdrant: id-keyed overwrite, no duplicate storage.
            self.points[str(p.id)] = {
                "vector": p.vector,
                "payload": p.payload,
            }
        self.upsert_calls.append(ids)


# ---- schema fixtures: insert a workspace + user + document row -----------


def _seed_workspace_user_document(conn, schema: str, document_id: uuid.UUID) -> None:
    """Insert the FK chain so ``chunks.document_id`` FK resolves.

    ``document_id`` MUST equal ``indexer._chunk_uuid(<doc_string>)`` because
    indexer converts the doc string into a UUID via that helper and uses it
    as the FK target. We seed with that same UUID so the FK resolves.

    Bypasses ORM so the test doesn't pick up side effects from other models.
    """
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    # Order matters: workspaces.owner_id FK references users.id, so users
    # must be inserted first. documents.owner_id FK also references users.
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


def _make_parent(document_id: str, idx: int) -> dict:
    return {
        "chunk_id": f"parent-{idx}",
        "document_id": document_id,
        "content": f"parent content #{idx}",
        "section_heading": f"Section {idx}",
        "page_start": idx,
        "page_end": idx,
        "child_ids": [f"child-{idx}-a", f"child-{idx}-b"],
    }


def _make_embedded(document_id: str, parent_id: str, idx: int) -> dict:
    """A 1024-dim zero vector — sparse vector can be empty for this test.

    ``idx`` is the integer page number (passed through to payload.page).
    """
    return {
        "chunk_id": f"child-{parent_id.split('-')[-1]}-{idx}",
        "document_id": document_id,
        "parent_id": parent_id,
        "content": f"child content #{idx} for {parent_id}",
        "dense_vector": [0.0] * 1024,
        "sparse_vector": {},
        "payload": {"page": int(idx), "block_kind": "text"},
    }


@pytest.fixture
def make_index_call(pg_test_engine, pg_schema_session):
    """Return a callable that wires the right Session + Qdrant fake and runs indexer.index()."""

    from agentic_rag_project.doc_processor import indexer
    from agentic_rag_project.doc_processor.models import (
        EmbeddedChunk,
        ParentChunk,
    )

    def _run(document_id_str: str) -> tuple[dict, int, int]:
        """Run indexer.index() once. Return (qdrant_fake_dict, pg_rows, pg_children_rows)."""
        # Build parents + embedded deterministically so re-running yields the
        # same chunk_ids and the same deterministic PKs.
        parents = [
            ParentChunk(**_make_parent(document_id_str, i)) for i in range(3)
        ]
        embedded: list[EmbeddedChunk] = []
        # Two children per parent; idx is a numeric page number for payload.
        for i, p in enumerate(parents):
            for j in (0, 1):
                embedded.append(
                    EmbeddedChunk(**_make_embedded(document_id_str, p.chunk_id, i * 10 + j))
                )

        fake_qdrant = _FakeQdrant()
        from sqlalchemy.orm import sessionmaker

        TestSession = sessionmaker(bind=pg_test_engine, autoflush=False, autocommit=False)
        with TestSession() as session:
            # Bind every table reference inside the session to the per-test schema.
            session.execute(text(f'SET search_path TO "{pg_schema_session}"'))
            result = indexer.index(
                parents=parents,
                embedded=embedded,
                document_id=document_id_str,
                qdrant=fake_qdrant,
                session=session,
                collection="test_chunks_v1",
            )

        with pg_test_engine.connect() as conn:
            conn.execute(text(f'SET search_path TO "{pg_schema_session}"'))
            total = conn.execute(text("SELECT count(*) FROM chunks")).scalar()
            children = conn.execute(
                text("SELECT count(*) FROM chunks WHERE is_parent = false")
            ).scalar()

        return {"qdrant": fake_qdrant, "result": result, "pg_total": total, "pg_children": children}

    return _run


def test_index_is_idempotent_across_two_runs(pg_test_engine, pg_schema_session, make_index_call):
    """Re-indexing the same document must not duplicate PG rows or Qdrant points."""
    from agentic_rag_project.doc_processor import indexer

    # document_id is a string the indexer converts via _chunk_uuid; we seed
    # the documents row with that exact UUID so the chunks.document_id FK resolves.
    document_id = f"smoke-idem-{uuid.uuid4().hex[:8]}"
    document_uuid = indexer._chunk_uuid(document_id)

    with pg_test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{pg_schema_session}"'))
        _seed_workspace_user_document(conn, pg_schema_session, document_uuid)

    # Pass 1: baseline
    first = make_index_call(document_id)
    assert first["result"].parent_chunks_written == 3
    assert first["result"].child_chunks_written == 6
    assert first["pg_total"] == 9
    assert first["pg_children"] == 6
    assert len(first["qdrant"].points) == 6

    # Pass 2: must be a no-op for row counts.
    second = make_index_call(document_id)
    assert second["pg_total"] == 9, (
        f"PG row count doubled on reindex: {first['pg_total']} -> {second['pg_total']}"
    )
    assert second["pg_children"] == 6
    assert len(second["qdrant"].points) == 6, (
        f"Qdrant point count doubled on reindex: "
        f"{len(first['qdrant'].points)} -> {len(second['qdrant'].points)}"
    )

    # Each index() call issues one Qdrant upsert with the full set of child
    # ids. Across both calls, the upsert payloads are identical (same
    # chunk_ids produce the same deterministic point ids).
    assert len(first["qdrant"].upsert_calls) == 1
    assert len(second["qdrant"].upsert_calls) == 1
    assert first["qdrant"].upsert_calls[0] == second["qdrant"].upsert_calls[0]


def test_index_upsert_updates_content_on_change(pg_test_engine, pg_schema_session):
    """When content changes between runs, ON CONFLICT must refresh the column."""
    from agentic_rag_project.doc_processor import indexer
    from agentic_rag_project.doc_processor.models import EmbeddedChunk, ParentChunk
    from sqlalchemy.orm import sessionmaker

    document_id = f"smoke-upd-{uuid.uuid4().hex[:8]}"
    document_uuid = indexer._chunk_uuid(document_id)
    with pg_test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{pg_schema_session}"'))
        _seed_workspace_user_document(conn, pg_schema_session, document_uuid)

    # Single parent + single child, then mutate the child's content and rerun.
    parent = ParentChunk(**_make_parent(document_id, 0))
    embedded_v1 = [
        EmbeddedChunk(
            **_make_embedded(document_id, parent.chunk_id, 0),
        )
    ]
    # Mutate the in-memory content without changing chunk_id so the second
    # index() call hits ON CONFLICT and updates content_hash / position.
    embedded_v1[0].content = "child v1"
    fake = _FakeQdrant()
    TestSession = sessionmaker(bind=pg_test_engine, autoflush=False, autocommit=False)
    with TestSession() as session:
        session.execute(text(f'SET search_path TO "{pg_schema_session}"'))
        indexer.index(
            parents=[parent],
            embedded=embedded_v1,
            document_id=document_id,
            qdrant=fake,
            session=session,
            collection="test_chunks_v1",
        )

    # Re-run with mutated child content but the same chunk_id.
    embedded_v2 = [
        EmbeddedChunk(
            **_make_embedded(document_id, parent.chunk_id, 0),
        )
    ]
    embedded_v2[0].content = "child v2 EDITED"
    with TestSession() as session:
        session.execute(text(f'SET search_path TO "{pg_schema_session}"'))
        indexer.index(
            parents=[parent],
            embedded=embedded_v2,
            document_id=document_id,
            qdrant=fake,
            session=session,
            collection="test_chunks_v1",
        )

    with pg_test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{pg_schema_session}"'))
        rows = conn.execute(
            text(
                "SELECT content FROM chunks WHERE is_parent = false "
                "ORDER BY chunk_index"
            )
        ).fetchall()
    assert len(rows) == 1, f"expected 1 child row, got {len(rows)}"
    assert rows[0][0] == "child v2 EDITED"
