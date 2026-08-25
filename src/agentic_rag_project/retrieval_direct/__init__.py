"""Retrieval Direct — direct-search pipeline (DESIGN 2.2 #6).

Implements vector+BM25 hybrid search (T2.2) and the two-stage parent/child
chunk retrieval strategy (T2.4) with citations + multi-turn memory (T2.4).
The Qdrant collection + payload indexes are bootstrapped by `qdrant_init`
(T2.1) on app startup. Long-context fallback (128K) lives here too (T3.3).

ACL filtering is the caller's responsibility — every entry point takes an
explicit `qdrant_filter` argument. `acl_filter.build_user_filter` (T5.3)
produces that filter; failure-closed semantics live there.
"""

from agentic_rag_project.retrieval_direct.qdrant_client import (
    build_qdrant_client,
    get_qdrant_client,
    reset_qdrant_client_cache,
)
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
from agentic_rag_project.retrieval_direct.citations import (
    CitationError,
    record_citations,
)
from agentic_rag_project.retrieval_direct.long_context_fallback import (
    LONG_CONTEXT_TAG,
    LongContextFallback,
    LongContextFallbackError,
    LongContextResult,
    build_long_context_prompt,
)
from agentic_rag_project.retrieval_direct.memory import (
    ConversationMemory,
    HISTORY_KEY_PREFIX,
    HISTORY_MAX_TURNS_DEFAULT,
    HistoryTurn,
    MemoryError,
)
from agentic_rag_project.retrieval_direct.search import (
    EMBED_CACHE_PREFIX,
    EmbeddedQuery,
    HybridSearcher,
    SearchError,
    SearchResult,
)
from agentic_rag_project.retrieval_direct.two_stage import (
    PAYLOAD_IS_PARENT,
    PAYLOAD_PARENT_CHUNK_ID,
    TwoStageError,
    TwoStageResult,
    TwoStageSearcher,
)

__all__ = [
    "CollectionSpec",
    "ConversationMemory",
    "CitationError",
    "EMBED_CACHE_PREFIX",
    "EmbeddedQuery",
    "HISTORY_KEY_PREFIX",
    "HISTORY_MAX_TURNS_DEFAULT",
    "HistoryTurn",
    "HybridSearcher",
    "LONG_CONTEXT_TAG",
    "LongContextFallback",
    "LongContextFallbackError",
    "LongContextResult",
    "MemoryError",
    "PAYLOAD_INDEXES",
    "PAYLOAD_IS_PARENT",
    "PAYLOAD_PARENT_CHUNK_ID",
    "SearchError",
    "SearchResult",
    "TwoStageError",
    "TwoStageResult",
    "TwoStageSearcher",
    "VECTOR_DENSE_NAME",
    "VECTOR_SPARSE_NAME",
    "build_collection_config",
    "build_qdrant_client",
    "build_long_context_prompt",
    "collection_exists",
    "ensure_all",
    "ensure_collection",
    "ensure_payload_indexes",
    "get_qdrant_client",
    "record_citations",
    "reset_qdrant_client_cache",
]
