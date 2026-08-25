"""Tests for application settings loading."""

from __future__ import annotations

from agentic_rag_project.config import get_settings


def test_get_settings_returns_cached_instance() -> None:
    """`get_settings()` returns a singleton via lru_cache."""
    a = get_settings()
    b = get_settings()
    assert a is b


def test_default_settings_have_expected_defaults() -> None:
    """Defaults are sane for local development without an `.env` file."""
    settings = get_settings()
    assert settings.app_env == "development"
    assert settings.app_port == 8000
    assert settings.qdrant_vector_dim == 1024
    assert settings.litellm_long_context_threshold == 0.5


def test_settings_can_be_overridden_by_env(monkeypatch: object) -> None:
    """Environment variables override defaults (smoke check via monkeypatch).

    NOTE: This is a structural check — the lru_cache means we cannot fully
    exercise env reload in a single pytest run. The override semantics are
    provided by pydantic-settings itself.
    """
    import pytest

    monkeypatch_ = pytest.MonkeyPatch()  # type: ignore[call-arg]
    try:
        monkeypatch_.setenv("APP_PORT", "9999")
        from agentic_rag_project.config import Settings

        fresh = Settings()
        assert fresh.app_port == 9999
    finally:
        monkeypatch_.undo()