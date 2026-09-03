"""Embedding client — LiteLLM BGE-M3 (1024-dim) for dense vectors.

DESIGN 4.2: dense vectors come from BGE-M3 (1024-dim). Sparse BM25
weights are computed locally — they are cheap token-id+weight pairs
that Qdrant's sparse-vector side expects.

`embed_texts(texts)` returns a list of dense vectors, one per input,
preserving order. `embed_chunks(chunks)` adds a sparse-vector pass
and packages the result as `EmbeddedChunk` DTOs ready for the indexer.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from typing import Sequence

import httpx

from agentic_rag_project.config import get_settings
from agentic_rag_project.doc_processor.models import ChildChunk, EmbeddedChunk

logger = logging.getLogger(__name__)

DENSE_DIM = 1024  # BGE-M3 fixed dimensionality

# Stop-words for BM25 — minimal English/Chinese set; kept inline to
# avoid adding a `nltk`/`jieba` dependency just for retrieval.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "in", "on", "for", "to", "and", "or",
        "is", "are", "was", "were", "be", "been", "being", "by", "with",
        "的", "了", "和", "是", "在", "也", "都", "而", "及", "与",
    }
)

_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


class EmbedderError(Exception):
    """Raised when the embedding endpoint is unreachable or returns malformed data."""


class LiteLLMEmbedder:
    """Calls `/v1/embeddings` on the LiteLLM proxy and assembles EmbeddedChunk."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
        batch_size: int | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.litellm_base_url).rstrip("/")
        self._api_key = api_key or settings.litellm_api_key
        self._model = model or settings.litellm_embedding_model
        # Read batch size from settings (DashScope text-embedding-v4 caps at 10
        # — see config.litellm_embedding_batch_size comment for the upstream
        # constraint source). Override via the constructor for tests / one-offs.
        self._batch_size = batch_size or settings.litellm_embedding_batch_size
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=60.0)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one dense vector per text. Order matches input.

        Inputs are split into ``self._batch_size``-sized sub-batches
        because the underlying DashScope ``text-embedding-v4`` upstream
        rejects larger requests with HTTP 400 and LiteLLM has no
        fallback configured for embedding models. Order is preserved
        across sub-batches so callers can zip with the input sequence.
        """
        if not texts:
            return []
        url = f"{self._base_url}/v1/embeddings"
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            payload = {"model": self._model, "input": batch}
            try:
                response = self._client.post(url, json=payload, headers=self._headers())
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise EmbedderError(
                    f"litellm embeddings failed (batch {start}-{start + len(batch)}): {exc}"
                ) from exc
            data = response.json()
            items = data.get("data", [])
            if len(items) != len(batch):
                raise EmbedderError(
                    f"litellm returned {len(items)} embeddings for {len(batch)} inputs "
                    f"(batch {start}-{start + len(batch)})"
                )
            for item in items:
                vec = item.get("embedding")
                if not isinstance(vec, list) or len(vec) != DENSE_DIM:
                    raise EmbedderError(
                        f"embedding dim mismatch: expected {DENSE_DIM}, "
                        f"got {len(vec) if isinstance(vec, list) else 'n/a'}"
                    )
                vectors.append(vec)
        return vectors

    def embed_chunks(self, chunks: Sequence[ChildChunk]) -> list[EmbeddedChunk]:
        """Dense + sparse pass for child chunks."""
        if not chunks:
            return []
        texts = [c.content for c in chunks]
        dense = self.embed_texts(texts)
        result: list[EmbeddedChunk] = []
        for chunk, vec in zip(chunks, dense, strict=True):
            sparse = _bm25_sparse(chunk.content)
            result.append(
                EmbeddedChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    # Mirror the chunk's workspace_id onto both the
                    # EmbeddedChunk attribute AND the Qdrant payload
                    # dict so the chat ACL filter can scope retrieval
                    # without a second hop back to PG (P0 / 2026-09-03).
                    workspace_id=chunk.workspace_id,
                    parent_id=chunk.parent_id,
                    content=chunk.content,
                    dense_vector=vec,
                    sparse_vector=sparse,
                    payload={
                        "document_id": chunk.document_id,
                        "workspace_id": chunk.workspace_id,
                        "parent_id": chunk.parent_id,
                        "block_kind": chunk.block_kind,
                        "page": chunk.page,
                    },
                )
            )
        return result

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "LiteLLMEmbedder":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _term_to_int(term: str) -> int:
    """Map a term to a stable 32-bit unsigned integer for Qdrant sparse indices.

    Qdrant's SparseVector requires non-negative int indices. We mask the
    SHA1 hash to 32 bits so identical terms across documents collide on
    the same id, enabling term-level sparse aggregation at retrieval time.
    """
    digest = hashlib.sha1(term.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0xFFFFFFFF


def _bm25_sparse(text: str, k1: float = 1.5, b: float = 0.75) -> dict[int, float]:
    """Compute a single-document BM25-style sparse vector.

    Qdrant's sparse vector interface takes {token_id (int): weight (float)}.
    We use a 32-bit SHA1 hash of the term as the id so identical tokens
    across chunks land in the same id space.
    """
    tokens = _tokenize(text)
    if not tokens:
        return {}
    tf = Counter(tokens)
    doc_len = len(tokens)
    avg_dl = max(doc_len, 1)
    norm = 1.0 - b + b * (doc_len / avg_dl)
    sparse: dict[int, float] = {}
    for term, freq in tf.items():
        token_id = _term_to_int(term)
        idf = math.log(1.0 + 1.0)  # single-doc context; no corpus IDF yet
        weight = idf * (freq * (k1 + 1.0)) / (freq + k1 * norm)
        if weight > 0:
            sparse[token_id] = float(weight)
    return sparse