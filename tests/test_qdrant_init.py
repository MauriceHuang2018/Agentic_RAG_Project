"""Tests for the Qdrant collection bootstrap (T2.1).

Covers:
  * Collection is created on first call
  * Re-running is idempotent (no error, summary shows created=False)
  * Payload indexes are created and stable on re-run
  * `ensure_all` returns a stable summary dict
  * Custom HNSW params are honored

Uses qdrant-client's in-memory mode so the tests do not need a live
Qdrant server. The `:memory:` instance behaves identically to a real
collection for collection/index/upsert operations.
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from agentic_rag_project.config import Settings
from agentic_rag_project.retrieval_direct.qdrant_init import (
    PAYLOAD_INDEXES,
    VECTOR_DENSE_NAME,
    VECTOR_SPARSE_NAME,
    CollectionSpec,
    build_collection_config,
    collection_exists,
    ensure_all,
    ensure_collection,
    ensure_payload_indexes,
)


@pytest.fixture
def client() -> QdrantClient:
    """An in-memory QdrantClient per test."""
    return QdrantClient(location=":memory:")


@pytest.fixture
def settings() -> Settings:
    """Build a Settings instance with a unique collection name per test."""
    # Use Settings(...) directly (not get_settings()) so cached singletons
    # don't leak across tests. Each call returns a fresh Settings.
    s = Settings()
    # Pin a unique collection name so tests can be order-independent.
    s.qdrant_collection = f"test_{uuid.uuid4().hex[:8]}"
    return s


# ---------------------------------------------------------------------------
# CollectionSpec
# ---------------------------------------------------------------------------


def test_collection_spec_reads_from_settings(settings: Settings) -> None:
    spec = CollectionSpec.from_settings(settings)
    assert spec.name == settings.qdrant_collection
    assert spec.vector_dim == settings.qdrant_vector_dim
    assert spec.hnsw_m == settings.qdrant_hnsw_m
    assert spec.hnsw_ef_construct == settings.qdrant_hnsw_ef_construct


def test_build_collection_config_uses_named_vectors(settings: Settings) -> None:
    spec = CollectionSpec.from_settings(settings)
    cfg = build_collection_config(spec)
    assert VECTOR_DENSE_NAME in cfg["vectors_config"]
    assert VECTOR_SPARSE_NAME in cfg["sparse_vectors_config"]
    dense = cfg["vectors_config"][VECTOR_DENSE_NAME]
    assert dense.size == settings.qdrant_vector_dim
    assert dense.distance == qmodels.Distance.COSINE


# ---------------------------------------------------------------------------
# ensure_collection
# ---------------------------------------------------------------------------


def test_ensure_collection_creates_when_absent(client: QdrantClient, settings: Settings) -> None:
    spec = CollectionSpec.from_settings(settings)
    assert not collection_exists(client, spec.name)
    created = ensure_collection(client, spec)
    assert created is True
    assert collection_exists(client, spec.name)


def test_ensure_collection_idempotent(client: QdrantClient, settings: Settings) -> None:
    spec = CollectionSpec.from_settings(settings)
    ensure_collection(client, spec)
    # Second call should be a no-op (returns False).
    assert ensure_collection(client, spec) is False
    assert collection_exists(client, spec.name)


def test_ensure_collection_overwrite_recreates(client: QdrantClient, settings: Settings) -> None:
    spec = CollectionSpec.from_settings(settings)
    ensure_collection(client, spec)
    sentinel_id = str(uuid.uuid4())
    client.upsert(
        collection_name=spec.name,
        points=[
            qmodels.PointStruct(
                id=sentinel_id,
                vector={VECTOR_DENSE_NAME: [0.0] * spec.vector_dim},
                payload={"workspace_id": "w"},
            )
        ],
    )
    # overwrite=True should drop the existing data.
    created = ensure_collection(client, spec, overwrite=True)
    assert created is True
    # After recreation the collection has zero points.
    info = client.get_collection(collection_name=spec.name)
    assert info.points_count == 0


# ---------------------------------------------------------------------------
# ensure_payload_indexes
# ---------------------------------------------------------------------------


def test_ensure_payload_indexes_creates_all_required(
    client: QdrantClient, settings: Settings
) -> None:
    spec = CollectionSpec.from_settings(settings)
    ensure_collection(client, spec)
    attempted = ensure_payload_indexes(client, spec.name)
    # First run attempts every PAYLOAD_INDEXES entry.
    assert attempted == len(PAYLOAD_INDEXES)


def test_ensure_payload_indexes_idempotent(
    client: QdrantClient, settings: Settings
) -> None:
    spec = CollectionSpec.from_settings(settings)
    ensure_collection(client, spec)
    first = ensure_payload_indexes(client, spec.name)
    # Second call must not raise — server-side idempotency (or local-Qdrant
    # no-op with warning) is acceptable.
    second = ensure_payload_indexes(client, spec.name)
    assert first == len(PAYLOAD_INDEXES)
    assert second == len(PAYLOAD_INDEXES)


def test_payload_indexes_list_includes_all_required_fields() -> None:
    """Static invariant: DESIGN 4.3 lists exactly these 8 fields."""
    field_names = {name for name, _ in PAYLOAD_INDEXES}
    assert field_names == {
        "workspace_id",
        "owner_id",
        "acl_user_ids",
        "acl_workspace_ids",
        "authority_level",
        "document_format",
        "is_parent",
        "parent_chunk_id",
    }


# ---------------------------------------------------------------------------
# ensure_all
# ---------------------------------------------------------------------------


def test_ensure_all_first_call_creates_everything(
    client: QdrantClient, settings: Settings
) -> None:
    summary = ensure_all(client, settings)
    assert summary["created"] is True
    assert summary["indexes_attempted"] == len(PAYLOAD_INDEXES)
    assert summary["collection"] == settings.qdrant_collection
    assert summary["vector_dim"] == settings.qdrant_vector_dim


def test_ensure_all_second_call_is_noop(
    client: QdrantClient, settings: Settings
) -> None:
    ensure_all(client, settings)
    summary = ensure_all(client, settings)
    assert summary["created"] is False
    assert summary["indexes_attempted"] == len(PAYLOAD_INDEXES)


def test_ensure_all_summary_includes_vector_names(
    client: QdrantClient, settings: Settings
) -> None:
    summary = ensure_all(client, settings)
    assert summary["vector_dense_name"] == "dense"
    assert summary["vector_sparse_name"] == "sparse"


def test_ensure_all_respects_custom_hnsw_params(
    client: QdrantClient, settings: Settings
) -> None:
    settings.qdrant_hnsw_m = 32
    settings.qdrant_hnsw_ef_construct = 200
    summary = ensure_all(client, settings)
    assert summary["hnsw_m"] == 32
    assert summary["hnsw_ef_construct"] == 200


# ---------------------------------------------------------------------------
# end-to-end: create collection + indexes + upsert a point, then verify
# ---------------------------------------------------------------------------


def test_collection_supports_named_dense_and_sparse_upsert(
    client: QdrantClient, settings: Settings
) -> None:
    spec = CollectionSpec.from_settings(settings)
    ensure_all(client, settings)
    point_id = str(uuid.uuid4())
    # A simple smoke upsert using both named vectors.
    point = qmodels.PointStruct(
        id=point_id,
        vector={
            VECTOR_DENSE_NAME: [0.1] * spec.vector_dim,
            VECTOR_SPARSE_NAME: qmodels.SparseVector(indices=[1, 2], values=[0.5, 0.7]),
        },
        payload={
            "workspace_id": "ws-1",
            "owner_id": "u-1",
            "acl_user_ids": ["u-2"],
            "acl_workspace_ids": ["ws-1"],
            "authority_level": 3,
            "document_format": "pdf",
            "is_parent": False,
            "parent_chunk_id": "parent-1",
            "page_no": 1,
            "section_path": ["Chapter 1"],
            "chunk_id": "c-1",
            "document_id": "d-1",
            "content": "hello world",
        },
    )
    client.upsert(collection_name=spec.name, points=[point], wait=True)

    # Verify with a pre-filter that uses one of the indexed fields.
    result = client.query_points(
        collection_name=spec.name,
        query=[0.1] * spec.vector_dim,
        using=VECTOR_DENSE_NAME,
        query_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="workspace_id", match=qmodels.MatchValue(value="ws-1")
                )
            ]
        ),
        limit=5,
        with_payload=True,
    )
    assert len(result.points) == 1
    assert result.points[0].payload["chunk_id"] == "c-1"
