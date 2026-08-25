"""Regression: Settings must never be used as a hash key.

pydantic v2 `BaseModel.__hash__` is `None` by default. Caches keyed
    on Settings instances must use `id()` instead. These tests lock in
    that contract by exercising the `get_*_client()` entry points and
    verifying the cache works (same instance returned for same
    `id(settings)`) and that the path does not raise
    `TypeError: unhashable type: 'Settings'`.

The tests don't need live Redis / Qdrant — `redis.Redis(**kwargs)`
and `QdrantClient(**kwargs)` are lazy constructors (no `.ping()` /
`.connect()` until first command), so they run in plain CI.
"""

from __future__ import annotations

from agentic_rag_project.config import get_settings
from agentic_rag_project.retrieval_direct.qdrant_client import (
    get_qdrant_client,
    reset_qdrant_client_cache,
)
from agentic_rag_project.retrieval_direct.redis_client import (
    get_history_redis_client,
    get_redis_client,
    reset_redis_client_cache,
)


def setup_function(_fn) -> None:
    """Clear all caches before each test for isolation."""
    reset_redis_client_cache()
    reset_qdrant_client_cache()


def test_get_redis_client_caches_per_settings_instance() -> None:
    """Same `id(settings)` -> same Redis client. Different -> different."""
    settings = get_settings()
    c1 = get_redis_client(settings)
    c2 = get_redis_client(settings)
    assert c1 is c2, (
        "get_redis_client must return the cached client for the same "
        "Settings instance (cache key = id(settings))"
    )


def test_get_history_redis_client_caches_per_settings_instance() -> None:
    """History DB cache also keyed by id(settings), not Settings itself."""
    settings = get_settings()
    c1 = get_history_redis_client(settings)
    c2 = get_history_redis_client(settings)
    assert c1 is c2


def test_get_qdrant_client_caches_per_settings_instance() -> None:
    """Qdrant cache likewise keyed by id(settings), not Settings itself."""
    settings = get_settings()
    c1 = get_qdrant_client(settings)
    c2 = get_qdrant_client(settings)
    assert c1 is c2


def test_reset_clears_caches() -> None:
    """`reset_*_cache()` must drop entries so next call rebuilds.

    Critical for tests that override env vars between cases: without
    a working reset, stale clients would carry the OLD env values
    into the NEW test.
    """
    settings = get_settings()
    c_before = get_redis_client(settings)
    reset_redis_client_cache()
    c_after = get_redis_client(settings)
    # Same settings instance, cache cleared -> fresh client object.
    assert c_before is not c_after, (
        "reset_redis_client_cache() must drop the cached entry; "
        "next call must rebuild and return a NEW object"
    )