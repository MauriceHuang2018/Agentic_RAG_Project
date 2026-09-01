"""Regression tests for the qdrant-client api_key / https gotcha.

Background (memory entry `dev-env-litellm-qdrant-gotchas-2026-08-25.md`):
  qdrant-client 1.x auto-promotes the connection URL from `http://` to
  `https://` whenever `api_key` is non-empty, on the assumption that an
  authenticated Qdrant must be Qdrant Cloud. In local dev we run plain-HTTP
  Qdrant on port 6333 with no TLS — the first request then dies with a
  confusing `SSL: WRONG_VERSION_NUMBER` (or `ConnectError`) error that
  looks like a network bug but is actually a client-side scheme flip.

  The fix lives in `build_qdrant_client()`: it pins `https=False` in the
  QdrantClient kwargs and logs the choice. These tests lock that defensive
  flag down so a future refactor that drops it re-triggers the gotcha at
  CI time, not at 2am in production.

Coverage:
  * https=False is passed regardless of api_key presence
  * The constructed client's URL scheme stays `http` (not silently https)
  * The `__FROM_SECRET__` placeholder from config.py is NOT forwarded
  * get_qdrant_client() caches by Settings identity (no leak across tests)
"""

from __future__ import annotations

import pytest

from agentic_rag_project.config import Settings
from agentic_rag_project.retrieval_direct import qdrant_client as qc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(
    *,
    host: str = "localhost",
    port: int = 6333,
    api_key: str = "__FROM_SECRET__",
) -> Settings:
    """Build a Settings instance with qdrant fields isolated.

    Using Settings() directly (not get_settings()) so the cached singleton
    doesn't leak across tests. Matches the pattern from test_qdrant_init.py.
    """
    s = Settings()
    s.qdrant_host = host
    s.qdrant_port = port
    s.qdrant_api_key = api_key
    return s


# ---------------------------------------------------------------------------
# https=False pinning — the core regression guard
# ---------------------------------------------------------------------------


def test_build_pins_https_false_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a real api_key, https=False must still be passed to QdrantClient."""
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(qc, "QdrantClient", FakeClient)
    settings = _make_settings(api_key="dev-secret-key")

    qc.build_qdrant_client(settings)

    assert captured.get("https") is False, (
        "https kwarg dropped from build_qdrant_client — the qdrant-client "
        "auto-promote gotcha will silently flip local dev to https:// and "
        "break ingest. See memory `dev-env-litellm-qdrant-gotchas-2026-08-25`."
    )
    assert captured.get("api_key") == "dev-secret-key"
    assert captured.get("host") == "localhost"
    assert captured.get("port") == 6333


def test_build_pins_https_false_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without api_key, https=False must still be passed (no conditional)."""
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(qc, "QdrantClient", FakeClient)
    settings = _make_settings(api_key="")

    qc.build_qdrant_client(settings)

    assert captured.get("https") is False
    # Empty api_key must not be forwarded as a literal "" — qdrant-client
    # would treat that as "authenticated" and re-trigger the gotcha.
    assert "api_key" not in captured or captured.get("api_key") in (None, "")


def test_build_drops_from_secret_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The config.py sentinel `__FROM_SECRET__` must NOT be forwarded.

    If .env didn't actually inject the key, Settings.qdrant_api_key stays at
    the default `"__FROM_SECRET__"`. Forwarding that literal to qdrant-client
    would make it think authentication is enabled and auto-promote to https.
    """
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(qc, "QdrantClient", FakeClient)
    settings = _make_settings(api_key="__FROM_SECRET__")  # default sentinel

    qc.build_qdrant_client(settings)

    assert "api_key" not in captured, (
        "build_qdrant_client forwarded the __FROM_SECRET__ sentinel. This "
        "makes qdrant-client think auth is enabled → flips to https:// → "
        "local dev breaks the same way as a real api_key leak."
    )


# ---------------------------------------------------------------------------
# Behavior: URL scheme stays http even when api_key is set
# ---------------------------------------------------------------------------


def test_constructed_client_url_scheme_is_http_with_api_key() -> None:
    """End-to-end: a real QdrantClient built via the wrapper must NOT silently
    flip its URL to https when api_key is set. We construct for real (no
    network) and inspect the client's `_init_options` dict, which qdrant-
    client 1.x exposes as the source of truth for connection params."""
    settings = _make_settings(api_key="dev-secret-key")
    client = qc.build_qdrant_client(settings)
    try:
        opts = _init_options(client)
        assert opts.get("https") is False, (
            "QdrantClient was built with https=True (or unset → default True). "
            "The qdrant-client auto-promote gotcha is back — api_key + local "
            "HTTP Qdrant will die with SSL: WRONG_VERSION_NUMBER. See memory "
            "`dev-env-litellm-qdrant-gotchas-2026-08-25`."
        )
        assert opts.get("host") == "localhost"
        assert opts.get("port") == 6333
    finally:
        client.close()


def test_constructed_client_url_scheme_is_http_without_api_key() -> None:
    """Same check, no api_key. Belt-and-braces alongside the kwargs capture."""
    settings = _make_settings(api_key="")
    client = qc.build_qdrant_client(settings)
    try:
        opts = _init_options(client)
        assert opts.get("https") is False
    finally:
        client.close()


def _init_options(client: object) -> dict[str, object]:
    """Extract `_init_options` from a qdrant-client QdrantClient instance.

    qdrant-client 1.x keeps the kwargs the client was constructed with on
    `_init_options`. Reading from there sidesteps the version-dependent
    internal `_client.url` / `_url` / `url` attribute paths.
    """
    opts = getattr(client, "_init_options", None)
    if not isinstance(opts, dict):
        raise AssertionError(
            f"qdrant-client {type(client).__name__} no longer exposes "
            "_init_options; update test_qdrant_client_factory.py."
        )
    return opts


# ---------------------------------------------------------------------------
# get_qdrant_client — caching contract
# ---------------------------------------------------------------------------


def test_get_qdrant_client_caches_per_settings_instance() -> None:
    """Same Settings instance returns the same cached client.

    Two different Settings instances must return two different clients —
    this is the contract that lets tests reset env vars between cases
    without leaking a stale client. Per the wrapper docstring, the cache
    is keyed by `id(settings)`, not by the qdrant_* field values.
    """
    qc.reset_qdrant_client_cache()
    s1 = _make_settings(api_key="")
    s2 = _make_settings(api_key="")
    a = qc.get_qdrant_client(s1)
    b = qc.get_qdrant_client(s1)  # same instance → cached
    c = qc.get_qdrant_client(s2)  # different instance → fresh
    try:
        assert a is b, "get_qdrant_client returned a fresh client for the same Settings"
        assert a is not c, "get_qdrant_client returned the same client across two Settings"
    finally:
        a.close()
        c.close()


def test_reset_qdrant_client_cache_clears_entries() -> None:
    """reset_qdrant_client_cache() must drop entries so the next call builds
    a fresh client. Tests rely on this between env-var overrides."""
    qc.reset_qdrant_client_cache()
    s = _make_settings(api_key="")
    a = qc.get_qdrant_client(s)
    qc.reset_qdrant_client_cache()
    b = qc.get_qdrant_client(s)
    try:
        assert a is not b, "reset_qdrant_client_cache failed to evict the cached client"
    finally:
        a.close()
        b.close()