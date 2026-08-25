"""Qdrant collection + payload index initialization.

DESIGN 4.3 / TASK T2.1: idempotently create the `chunks_v1` collection with
hybrid dense+sparse vector support and the payload indexes required for
ACL pre-filtering. The HNSW params come from settings so the same code
runs in dev (smaller m/ef) and prod (larger).

`ensure_all(client, settings)` is the single entry point used by
`main.py` lifespan and any one-off bootstrap scripts. Every call is a
no-op if the collection + indexes already exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from agentic_rag_project.config import Settings

logger = logging.getLogger(__name__)

# Payload fields that must be indexed for ACL / two-stage retrieval. The
# values are Qdrant `PayloadSchemaType` literals; order is preserved by
# the indexer call so the index list is stable across runs.
PAYLOAD_INDEXES: tuple[tuple[str, qmodels.PayloadSchemaType], ...] = (
    ("workspace_id", qmodels.PayloadSchemaType.KEYWORD),
    ("owner_id", qmodels.PayloadSchemaType.KEYWORD),
    ("acl_user_ids", qmodels.PayloadSchemaType.KEYWORD),
    ("acl_workspace_ids", qmodels.PayloadSchemaType.KEYWORD),
    ("authority_level", qmodels.PayloadSchemaType.INTEGER),
    ("document_format", qmodels.PayloadSchemaType.KEYWORD),
    ("is_parent", qmodels.PayloadSchemaType.BOOL),
    ("parent_chunk_id", qmodels.PayloadSchemaType.KEYWORD),
)

# Named vector keys — kept in sync with `doc_processor.indexer._build_point`
# and the hybrid_search code path in T2.2.
VECTOR_DENSE_NAME = "dense"
VECTOR_SPARSE_NAME = "sparse"


@dataclass(frozen=True)
class CollectionSpec:
    """Resolved shape of the Qdrant collection for a given settings instance."""

    name: str
    vector_dim: int
    hnsw_m: int
    hnsw_ef_construct: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "CollectionSpec":
        return cls(
            name=settings.qdrant_collection,
            vector_dim=settings.qdrant_vector_dim,
            hnsw_m=settings.qdrant_hnsw_m,
            hnsw_ef_construct=settings.qdrant_hnsw_ef_construct,
        )


def build_collection_config(spec: CollectionSpec) -> dict[str, object]:
    """Build the `create_collection` config for the given spec."""
    return {
        "vectors_config": {
            VECTOR_DENSE_NAME: qmodels.VectorParams(
                size=spec.vector_dim,
                distance=qmodels.Distance.COSINE,
            ),
        },
        "sparse_vectors_config": {
            VECTOR_SPARSE_NAME: qmodels.SparseVectorParams(
                index=qmodels.SparseIndexParams(on_disk=False),
            ),
        },
        "hnsw_config": qmodels.HnswConfigDiff(
            m=spec.hnsw_m,
            ef_construct=spec.hnsw_ef_construct,
        ),
    }


def collection_exists(client: QdrantClient, name: str) -> bool:
    """Thin wrapper so callers / tests can stub a single symbol."""
    return client.collection_exists(collection_name=name)


def ensure_collection(
    client: QdrantClient,
    spec: CollectionSpec,
    *,
    overwrite: bool = False,
) -> bool:
    """Create the collection if absent (or forced).

    Returns True if the collection was created, False if it already existed.
    Re-running this is safe.
    """
    if collection_exists(client, spec.name):
        if overwrite:
            logger.warning("deleting existing collection %s (overwrite=True)", spec.name)
            client.delete_collection(collection_name=spec.name)
        else:
            logger.info("collection %s already exists — skipping create", spec.name)
            return False
    logger.info(
        "creating collection %s dim=%d hnsw(m=%d, ef_construct=%d)",
        spec.name,
        spec.vector_dim,
        spec.hnsw_m,
        spec.hnsw_ef_construct,
    )
    client.create_collection(
        collection_name=spec.name,
        **build_collection_config(spec),
    )
    return True


def ensure_payload_indexes(
    client: QdrantClient,
    name: str,
    *,
    indexes: tuple[tuple[str, qmodels.PayloadSchemaType], ...] = PAYLOAD_INDEXES,
) -> int:
    """Idempotently create the required payload indexes. Returns count attempted.

    Qdrant's `create_payload_index` is server-side idempotent — calling it
    for an already-indexed field is a no-op (or returns "already exists",
    which we swallow). The local in-memory Qdrant does not actually track
    payload indexes, but the call still succeeds with a warning, so the
    semantics are identical from the caller's point of view.
    """
    attempted = 0
    for field_name, schema in indexes:
        logger.info("ensuring payload index %s.%s (%s)", name, field_name, schema)
        try:
            client.create_payload_index(
                collection_name=name,
                field_name=field_name,
                field_schema=schema,
            )
        except Exception as exc:  # qdrant-client raises broad exceptions
            if "already exists" not in str(exc).lower():
                raise
        attempted += 1
    return attempted


def ensure_all(client: QdrantClient, settings: Settings) -> dict[str, object]:
    """Idempotent bootstrap: create collection + missing payload indexes.

    Returns a summary dict for logging / health endpoints:
      {
        "collection": "chunks_v1",
        "created": True/False,
        "indexes_created": N,
        "vector_dim": 1024,
        ...
      }
    """
    spec = CollectionSpec.from_settings(settings)
    created = ensure_collection(client, spec)
    indexes_attempted = ensure_payload_indexes(client, spec.name)
    summary = {
        "collection": spec.name,
        "created": created,
        "indexes_attempted": indexes_attempted,
        "vector_dim": spec.vector_dim,
        "hnsw_m": spec.hnsw_m,
        "hnsw_ef_construct": spec.hnsw_ef_construct,
        "vector_dense_name": VECTOR_DENSE_NAME,
        "vector_sparse_name": VECTOR_SPARSE_NAME,
    }
    logger.info("qdrant init complete: %s", summary)
    return summary
