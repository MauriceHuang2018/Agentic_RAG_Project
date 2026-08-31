"""Qdrant client factory.

Lives in `retrieval_direct` (not `db`) because the rest of the system
treats Qdrant as the vector store for the retrieval pipeline, not as
the canonical store. The single `get_qdrant_client(settings)` helper
returns a cached client and is the one call site all retrieval code
should use.

In tests, callers should bypass this module and inject their own
`QdrantClient(":memory:")` directly.

Cache implementation note: we use a plain `dict` keyed by
`id(settings)` rather than `functools.lru_cache`. pydantic v2
`BaseModel.__hash__` is `None` and lru_cache hashes ALL positional
args — passing a `Settings` instance raises
`TypeError: unhashable type: 'Settings'`. The dict approach never
hashes `Settings`, only uses its object identity as the key.
"""

from __future__ import annotations

import logging

from qdrant_client import QdrantClient

from agentic_rag_project.config import Settings

logger = logging.getLogger(__name__)

# Per-instance cache. Keyed by `id(settings)` (CPython object identity).
# Settings is a process-level singleton via `get_settings()`'s lru_cache,
# so this dict holds at most one entry in production. Tests call
# `reset_qdrant_client_cache()` between cases to drop entries.
_qdrant_cache: dict[int, QdrantClient] = {}


def build_qdrant_client(settings: Settings) -> QdrantClient:
    """Construct a QdrantClient from the given settings (no caching).

    Passes `https=False` explicitly. The qdrant-client 1.x heuristic
    auto-promotes the URL to `https://` whenever an api_key is present,
    which silently breaks plain-HTTP dev Qdrants (port 6333, no TLS) —
    the first request fails with `SSL: WRONG_VERSION_NUMBER`. Pinning
    `https=False` keeps the URL scheme under the operator's control:
    deploy behind TLS and set this to `True` or a QDRANT_URL when the
    cluster actually terminates HTTPS.
    """
    kwargs: dict[str, object] = {
        "host": settings.qdrant_host,
        "port": settings.qdrant_port,
        "timeout": 30,
        "https": False,
    }
    if settings.qdrant_api_key and settings.qdrant_api_key != "__FROM_SECRET__":
        kwargs["api_key"] = settings.qdrant_api_key
    logger.info(
        "connecting to qdrant at %s:%d (https=%s)",
        settings.qdrant_host,
        settings.qdrant_port,
        kwargs["https"],
    )
    return QdrantClient(**kwargs)


def get_qdrant_client(settings: Settings) -> QdrantClient:
    """Return a cached QdrantClient for the given settings instance.

    The cache key is `id(settings)` so tests that build fresh Settings
    don't accidentally hit a stale client. Uses a plain dict instead of
    `lru_cache` because pydantic v2 Settings is unhashable. Build
    failures propagate WITHOUT caching, so the next call can retry.
    """
    key = id(settings)
    if key not in _qdrant_cache:
        _qdrant_cache[key] = build_qdrant_client(settings)
    return _qdrant_cache[key]


def reset_qdrant_client_cache() -> None:
    """Drop the cached client. Useful in tests that override env vars."""
    _qdrant_cache.clear()
