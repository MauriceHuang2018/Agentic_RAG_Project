"""Tests for the T3 production wiring in `agentic_rag_project.main`.

The lifespan startup builds a real `ChatService` and registers it via
`chat_router.set_chat_service_factory(...)`. These tests verify:

  1. `_build_chat_service` swallows redis init failures → returns `None`.
  2. `_build_chat_service` swallows qdrant/searcher init failures → `None`.
  3. `_build_chat_service` returns a `ChatService` when collaborators
     are healthy (everything is stubbed).
  4. The lifespan function leaves the chat factory unset when
     `_build_chat_service` returns `None` (driven directly via asyncio
     to avoid TestClient lifespan merging).
  5. The lifespan function wires the chat factory when
     `_build_chat_service` returns a service.
  6. The lifespan survives the bootstrap steps (RBAC seed + Qdrant
     init) when all side-effects are stubbed out.

We monkeypatch the heavy collaborators (Redis / Qdrant / searcher /
runner / long_context / memory / filter / masker / synthesizer /
DB session / Qdrant init) because pulling them up for real would need
a live Postgres + Redis + Qdrant and an LLM endpoint — out of scope
for unit tests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from agentic_rag_project.api_gateway import chat_router
from agentic_rag_project.main import _build_chat_service, lifespan


# ---------------------------------------------------------------------------
# Fakes — minimal stand-ins that quack like the real collaborators.
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Mimics `redis.Redis` enough for `_build_chat_service`'s `.ping()` call."""

    def __init__(self, *, ping_raises: BaseException | None = None) -> None:
        self._ping_raises = ping_raises
        self.ping_called = 0

    def ping(self) -> bool:
        self.ping_called += 1
        if self._ping_raises is not None:
            raise self._ping_raises
        return True


class _FakeChatService:
    """Stand-in for `ChatService` so we can verify the factory was wired."""

    def __init__(self) -> None:
        self.handle_calls: list[dict[str, Any]] = []
        self.next_response: Any = None

    def handle(self, **kwargs: Any) -> Any:
        self.handle_calls.append(kwargs)
        return self.next_response


class _FakeSession:
    """Stub for the SQLAlchemy session used during the lifespan seed.

    The lifespan calls `seed_builtin_roles(session)`,
    `FeedbackService(session=session).seed_default_categories()`, etc.
    We patch those names so they never reach `.execute()` / `.add()` —
    this stub only needs to satisfy the `session.close()` call in the
    `finally` block.
    """

    def __init__(self) -> None:
        self.closed = False
        self.committed = False

    def close(self) -> None:
        self.closed = True

    def commit(self) -> None:
        self.committed = True


# ---------------------------------------------------------------------------
# Helpers — module-level monkeypatch fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_chat_factory() -> None:
    """Ensure the chat_router factory is cleared before AND after each test."""
    chat_router.set_chat_service_factory(None)
    yield
    chat_router.set_chat_service_factory(None)


@pytest.fixture
def patch_collaborators(monkeypatch: pytest.MonkeyPatch):
    """Patch every heavy collaborator in `main._build_chat_service`.

    Returns a dict of fakes so individual tests can introspect calls
    or swap behaviors (e.g. make `redis.ping()` raise).
    """
    fake_redis = _FakeRedis()
    fake_qdrant = object()
    fake_searcher = object()
    fake_two_stage = object()
    fake_router = object()
    fake_agent_runner = object()
    fake_long_context = object()
    fake_memory = object()
    fake_sensitive_filter = object()
    fake_masker = object()
    fake_direct_synthesizer = object()
    fake_chat_service = _FakeChatService()

    # Redis factory + ping path
    monkeypatch.setattr(
        "agentic_rag_project.main.get_redis_client",
        lambda settings: fake_redis,
    )

    # Qdrant factory
    monkeypatch.setattr(
        "agentic_rag_project.main.get_qdrant_client",
        lambda settings: fake_qdrant,
    )

    # Heavy collaborators — every collaborator class referenced by
    # `_build_chat_service` is replaced with a bare `object()` so the
    # constructor takes any kwargs. `ChatService` itself is monkeypatched
    # so the test can inspect `.handle` calls.
    monkeypatch.setattr(
        "agentic_rag_project.main.HybridSearcher",
        lambda **kwargs: fake_searcher,
    )
    monkeypatch.setattr(
        "agentic_rag_project.main.TwoStageSearcher",
        lambda **kwargs: fake_two_stage,
    )
    monkeypatch.setattr(
        "agentic_rag_project.main.ConfidenceRouter",
        lambda **kwargs: fake_router,
    )
    monkeypatch.setattr(
        "agentic_rag_project.main.AgentRunner",
        lambda **kwargs: fake_agent_runner,
    )
    monkeypatch.setattr(
        "agentic_rag_project.main.LongContextFallback",
        lambda **kwargs: fake_long_context,
    )
    monkeypatch.setattr(
        "agentic_rag_project.main.ConversationMemory",
        lambda **kwargs: fake_memory,
    )
    monkeypatch.setattr(
        "agentic_rag_project.main.SensitiveWordFilter",
        lambda **kwargs: fake_sensitive_filter,
    )
    monkeypatch.setattr(
        "agentic_rag_project.main.Masker",
        lambda **kwargs: fake_masker,
    )
    monkeypatch.setattr(
        "agentic_rag_project.main.DefaultLLMSynthesizer",
        lambda **kwargs: fake_direct_synthesizer,
    )
    monkeypatch.setattr(
        "agentic_rag_project.main.ChatService",
        lambda **kwargs: fake_chat_service,
    )

    return {
        "redis": fake_redis,
        "qdrant": fake_qdrant,
        "searcher": fake_searcher,
        "two_stage": fake_two_stage,
        "router": fake_router,
        "agent_runner": fake_agent_runner,
        "long_context": fake_long_context,
        "memory": fake_memory,
        "sensitive_filter": fake_sensitive_filter,
        "masker": fake_masker,
        "direct_synthesizer": fake_direct_synthesizer,
        "chat_service": fake_chat_service,
    }


@pytest.fixture
def patch_lifespan_bootstrap(monkeypatch: pytest.MonkeyPatch):
    """Stub the bootstrap steps that need a live DB / Qdrant.

    Without these, the lifespan would try to seed roles + categories
    against a real Postgres, which the unit test environment doesn't
    provide. The bootstrap is *what we're testing* but we don't want
    to drag in the real database; the `_build_chat_service` /
    `set_chat_service_factory` pair is the focus.
    """
    from agentic_rag_project import main as main_mod

    fake_session = _FakeSession()
    monkeypatch.setattr(main_mod, "SessionLocal", lambda: fake_session)
    monkeypatch.setattr(main_mod, "seed_builtin_roles", lambda session: None)
    # M6 (2026-09-01): seed_demo_data was added to the lifespan
    # alongside seed_builtin_roles. Stub it too so the lifespan tests
    # don't need a real Postgres (the bootstrap-internals tests in
    # `tests/test_demo_data_seed.py` cover the seed itself).
    monkeypatch.setattr(main_mod, "seed_demo_data", lambda session: {})
    monkeypatch.setattr(main_mod, "ensure_all", lambda q, s: None)
    monkeypatch.setattr(main_mod, "FeedbackService", lambda session: _FakeFeedbackService())
    return {"session": fake_session}


class _FakeFeedbackService:
    """Stand-in for `FeedbackService` used only by the lifespan seed."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def seed_default_categories(self) -> None:
        self.calls.append("categories")

    def seed_default_ticket_statuses(self) -> None:
        self.calls.append("statuses")


# ---------------------------------------------------------------------------
# _build_chat_service — failure paths
# ---------------------------------------------------------------------------


def test_build_chat_service_returns_none_when_redis_ping_fails(
    patch_collaborators: dict[str, Any],
) -> None:
    """If `redis.ping()` raises, the factory bails out early — never touches Qdrant."""
    patch_collaborators["redis"]._ping_raises = ConnectionError("redis unreachable")

    result = _build_chat_service()

    assert result is None
    assert patch_collaborators["redis"].ping_called == 1


def test_build_chat_service_returns_none_when_qdrant_init_fails(
    patch_collaborators: dict[str, Any],
) -> None:
    """If Qdrant client construction raises, the factory returns `None`."""
    # Force the HybridSearcher constructor to raise — simulates a Qdrant
    # failure mode that's caught at the qdrant/searcher boundary.
    import agentic_rag_project.main as main_mod

    def _raise(**kwargs: Any) -> None:
        raise RuntimeError("qdrant dead")

    original_searcher = main_mod.HybridSearcher
    main_mod.HybridSearcher = _raise

    try:
        result = _build_chat_service()
    finally:
        main_mod.HybridSearcher = original_searcher

    assert result is None
    # Redis ping still ran first.
    assert patch_collaborators["redis"].ping_called == 1


# ---------------------------------------------------------------------------
# _build_chat_service — happy path
# ---------------------------------------------------------------------------


def test_build_chat_service_returns_chat_service_on_success(
    patch_collaborators: dict[str, Any],
) -> None:
    """When every collaborator constructs OK, we get a real `ChatService`."""
    result = _build_chat_service()

    assert result is patch_collaborators["chat_service"]
    assert patch_collaborators["redis"].ping_called == 1


# ---------------------------------------------------------------------------
# Lifespan integration — drive the lifespan directly via asyncio.
# ---------------------------------------------------------------------------


def _drive_lifespan_sync() -> None:
    """Run the lifespan context to completion in a fresh event loop."""
    asyncio.run(_run_lifespan())


async def _run_lifespan() -> None:
    # `lifespan` takes a FastAPI but ignores it; pass None for tests.
    async with lifespan(None):  # type: ignore[arg-type]
        pass


def test_lifespan_leaves_factory_unset_when_build_fails(
    monkeypatch: pytest.MonkeyPatch,
    patch_collaborators: dict[str, Any],
    patch_lifespan_bootstrap: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When `_build_chat_service` returns `None`, factory stays unset."""
    patch_collaborators["redis"]._ping_raises = ConnectionError("redis down")
    monkeypatch.setattr(
        "agentic_rag_project.main._build_chat_service",
        _build_chat_service,
    )

    with caplog.at_level(logging.WARNING, logger="agentic_rag_project.main"):
        _drive_lifespan_sync()

    # The factory was never set — the chat router will return 503.
    assert chat_router._chat_service_factory is None
    # We logged the failure at WARNING level.
    assert any("chat service not wired" in r.message for r in caplog.records)


def test_lifespan_wires_factory_when_build_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    patch_collaborators: dict[str, Any],
    patch_lifespan_bootstrap: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When `_build_chat_service` returns a service, factory is set + callable."""
    monkeypatch.setattr(
        "agentic_rag_project.main._build_chat_service",
        _build_chat_service,
    )

    with caplog.at_level(logging.INFO, logger="agentic_rag_project.main"):
        _drive_lifespan_sync()

    factory = chat_router._chat_service_factory
    assert factory is not None
    # The factory is a zero-arg callable that returns the ChatService.
    service = factory()
    assert service is patch_collaborators["chat_service"]
    # Bootstrap log line confirms the happy path.
    assert any("chat service wired" in r.message for r in caplog.records)


def test_lifespan_runs_rbac_and_feedback_bootstrap(
    patch_lifespan_bootstrap: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifespan seeds RBAC + feedback dictionaries (idempotent)."""
    # Track which bootstrap calls were made.
    from agentic_rag_project import main as main_mod

    calls: list[str] = []

    def _track_seed_roles(session: Any) -> None:
        calls.append("roles")

    class _TrackingFeedback:
        def __init__(self, session: Any) -> None:
            pass

        def seed_default_categories(self) -> None:
            calls.append("categories")

        def seed_default_ticket_statuses(self) -> None:
            calls.append("statuses")

    monkeypatch.setattr(main_mod, "seed_builtin_roles", _track_seed_roles)
    monkeypatch.setattr(main_mod, "FeedbackService", _TrackingFeedback)
    # Force build failure so we don't accidentally depend on Redis fakes.
    monkeypatch.setattr(
        "agentic_rag_project.main._build_chat_service",
        lambda: None,
    )

    _drive_lifespan_sync()

    assert "roles" in calls
    assert "categories" in calls
    assert "statuses" in calls
    # Session was opened and closed.
    fake_session = patch_lifespan_bootstrap["session"]
    assert fake_session.closed is True


def test_lifespan_swallows_qdrant_init_failure(
    monkeypatch: pytest.MonkeyPatch,
    patch_lifespan_bootstrap: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Qdrant init failure is logged at WARNING and doesn't crash the lifespan."""
    from agentic_rag_project import main as main_mod

    def _raise_qdrant(q: Any, s: Any) -> None:
        raise ConnectionError("qdrant unreachable")

    monkeypatch.setattr(main_mod, "ensure_all", _raise_qdrant)
    # Build fails too — that's fine, both failures are non-fatal.
    monkeypatch.setattr(
        "agentic_rag_project.main._build_chat_service",
        lambda: None,
    )

    with caplog.at_level(logging.WARNING, logger="agentic_rag_project.main"):
        _drive_lifespan_sync()

    # Qdrant failure was logged.
    assert any("qdrant init skipped" in r.message for r in caplog.records)
    # Factory stays unset (build also failed).
    assert chat_router._chat_service_factory is None