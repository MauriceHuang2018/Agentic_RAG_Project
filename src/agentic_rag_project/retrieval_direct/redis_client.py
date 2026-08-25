"""Redis client factory.

Lives alongside `qdrant_client.py` for symmetry. The retrieval layer
and the conversation-history module both reach into Redis, so a
shared factory keeps connection settings in one place. In tests,
callers should bypass this module and inject their own
`fakeredis.FakeRedis()` directly.

The factory caches the client per `id(settings)` so repeated
`get_redis_client(settings)` calls during a single request reuse the
same connection pool. `reset_redis_client_cache()` is for tests that
mutate env vars between cases.

Cache implementation note: we use a plain `dict` keyed by
`id(settings)` rather than `functools.lru_cache`. pydantic v2
`BaseModel.__hash__` is `None` and lru_cache hashes ALL positional
args — passing a `Settings` instance raises
`TypeError: unhashable type: 'Settings'`. The dict approach never
hashes `Settings`, only uses its object identity as the key.
"""

from __future__ import annotations

import logging

import redis

from agentic_rag_project.config import Settings

logger = logging.getLogger(__name__)

# Per-instance caches. Keyed by `id(settings)` (CPython object identity).
# Settings is a process-level singleton via `get_settings()`'s lru_cache,
# so these dicts hold at most one entry in production. Tests call
# `reset_redis_client_cache()` between cases to drop entries.
_redis_cache: dict[int, redis.Redis] = {}
_history_cache: dict[int, redis.Redis] = {}


def build_redis_client(settings: Settings) -> redis.Redis:
    """Construct a `redis.Redis` client from the given settings.

    Connection params come from `redis_host`, `redis_port`,
    `redis_db`, and (when set) `redis_password`. We use
    `decode_responses=False` so binary payloads (the conversation
    history JSON bytes) round-trip without surprises.
    """
    kwargs: dict[str, object] = {
        "host": settings.redis_host,
        "port": settings.redis_port,
        "db": settings.redis_db,
        "socket_timeout": 10,
        "socket_connect_timeout": 5,
    }
    if (
        settings.redis_password
        and settings.redis_password != "__FROM_SECRET__"
    ):
        kwargs["password"] = settings.redis_password
    logger.info(
        "connecting to redis at %s:%d db=%d",
        settings.redis_host,
        settings.redis_port,
        settings.redis_db,
    )
    return redis.Redis(**kwargs)


def build_history_redis_client(settings: Settings) -> redis.Redis:
    """Construct a `redis.Redis` client pinned to the conversation-history DB.

    Isolated from the main cache DB (`redis_db`) so a `FLUSHDB` on the cache
    (or eviction under memory pressure) does not drop chat state.
    Default DB index is 4; override with `REDIS_HISTORY_DB`.
    """
    kwargs: dict[str, object] = {
        "host": settings.redis_host,
        "port": settings.redis_port,
        "db": settings.redis_history_db,
        "socket_timeout": 10,
        "socket_connect_timeout": 5,
    }
    if (
        settings.redis_password
        and settings.redis_password != "__FROM_SECRET__"
    ):
        kwargs["password"] = settings.redis_password
    logger.info(
        "connecting to redis (history) at %s:%d db=%d",
        settings.redis_host,
        settings.redis_port,
        settings.redis_history_db,
    )
    return redis.Redis(**kwargs)


def get_redis_client(settings: Settings) -> redis.Redis:
    """Return a cached `redis.Redis` for the given settings instance.

    The cache key is `id(settings)` so tests that build fresh
    `Settings` instances don't accidentally hit a stale client. Uses a
    plain dict instead of `lru_cache` because pydantic v2 Settings is
    unhashable. Build failures propagate WITHOUT caching, so the next
    call can retry.
    """
    key = id(settings)
    if key not in _redis_cache:
        _redis_cache[key] = build_redis_client(settings)
    return _redis_cache[key]


def get_history_redis_client(settings: Settings) -> redis.Redis:
    """Return a cached `redis.Redis` pinned to the history DB index.

    See `get_redis_client` for the rationale on dict-vs-lru_cache.
    """
    key = id(settings)
    if key not in _history_cache:
        _history_cache[key] = build_history_redis_client(settings)
    return _history_cache[key]


def reset_redis_client_cache() -> None:
    """Drop both cached clients. Useful in tests that override env vars."""
    _redis_cache.clear()
    _history_cache.clear()


__all__ = [
    "build_history_redis_client",
    "build_redis_client",
    "get_history_redis_client",
    "get_redis_client",
    "reset_redis_client_cache",
]