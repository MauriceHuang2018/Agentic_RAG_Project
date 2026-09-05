"""FastAPI application factory.

Exposes `app` at module level so `uvicorn agentic_rag_project.main:app` works
in addition to the `python -m agentic_rag_project` entry point.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agentic_rag_project.api_gateway import gateway_router, metrics_router
from agentic_rag_project.api_gateway.chat_router import set_chat_service_factory
from agentic_rag_project.chat import ChatService, DefaultLLMSynthesizer
from agentic_rag_project.config import get_settings
from agentic_rag_project.db.session import SessionLocal
from agentic_rag_project.feedback.service import FeedbackService
from agentic_rag_project.post_processor.filter import SensitiveWordFilter
from agentic_rag_project.post_processor.mask import Masker
from agentic_rag_project.rbac import seed_builtin_roles, seed_demo_data
from agentic_rag_project.retrieval_direct.long_context_fallback import (
    LongContextFallback,
)
from agentic_rag_project.retrieval_direct.memory import ConversationMemory
from agentic_rag_project.retrieval_direct.qdrant_client import get_qdrant_client
from agentic_rag_project.retrieval_direct.qdrant_init import ensure_all
from agentic_rag_project.retrieval_direct.redis_client import (
    get_history_redis_client,
    get_redis_client,
)
from agentic_rag_project.retrieval_direct.search import HybridSearcher
from agentic_rag_project.retrieval_direct.two_stage import TwoStageSearcher
from agentic_rag_project.agent_core.runner import AgentRunner
from agentic_rag_project.router.classifier import (
    ConfidenceRouter,
    KeywordClassifier,
    LLMClassifier,
)

logger = logging.getLogger(__name__)


def _build_chat_service() -> ChatService | None:
    """Construct the production `ChatService` with all real collaborators.

    Returns `None` if any collaborator can't be initialized — the lifespan
    logs the failure and leaves the chat factory unset, so `/chat/query`
    returns 503 instead of crashing the app. This matches the existing
    Qdrant-init failure mode (T2.1): a missing dependency is non-fatal
    at startup but surfaces as a real error on the first request that
    actually needs it.
    """
    settings = get_settings()

    try:
        redis_client = get_redis_client(settings)
        # Force a connect so we fail fast on bad creds/host.
        redis_client.ping()
        history_redis = get_history_redis_client(settings)
        history_redis.ping()
    except Exception as exc:
        logger.warning("chat service: redis init failed: %s", exc)
        return None

    try:
        qdrant = get_qdrant_client(settings)
        # Don't ping Qdrant here — `ensure_all` in lifespan startup does
        # the real collection / payload index bootstrap. The qdrant
        # factory itself is a thin wrapper; failures surface there.
        searcher = HybridSearcher(
            qdrant=qdrant,
            collection=settings.qdrant_collection,
            redis_cache=redis_client,
            cache_ttl_s=settings.qdrant_embedding_cache_ttl_s,
        )
        two_stage = TwoStageSearcher(
            searcher=searcher,
            parent_top_k=settings.two_stage_parent_top_k,
            child_top_k=settings.two_stage_child_top_k,
            fallback_threshold=settings.litellm_long_context_threshold,
        )
    except Exception as exc:
        logger.warning("chat service: qdrant / searcher init failed: %s", exc)
        return None

    router = ConfidenceRouter(
        # Bound the classifier's LLM call by an env-tunable budget so the
        # chat critical path doesn't wait forever when litellm's fallback
        # chain is slow. Default 10s (see `config.router_classifier_timeout_seconds`)
        # is comfortable for a healthy qwen3.7-plus (~3s measured).
        llm_classifier=LLMClassifier(
            timeout_seconds=settings.router_classifier_timeout_seconds,
            # Cap classifier output (route JSON is small) and kill the
            # reasoning trace — both default OFF per Settings. Wire them
            # unconditionally when the env didn't explicitly re-enable
            # thinking; that way operators can flip on for debugging by
            # setting CLASSIFIER_ENABLE_THINKING=true. Step 6 / 2026-09-05.
            max_tokens=settings.classifier_max_tokens,
            extra_body=(
                {"enable_thinking": False}
                if not settings.classifier_enable_thinking
                else None
            ),
        ),
        keyword_classifier=KeywordClassifier(),
    )
    # AgentRunner expects `searcher.two_stage_search(...)` per agent_core/runner.py:75.
    # Pass `two_stage` (TwoStageSearcher) instead of `searcher` (HybridSearcher), which
    # lacks that method. Surfaced 2026-08-25 during M3 Live E2E (see docs/m3_agentic_rag_intent/).
    agent_runner = AgentRunner(searcher=two_stage)
    long_context = LongContextFallback()
    memory = ConversationMemory(redis_client=history_redis)
    sensitive_filter = SensitiveWordFilter(redis_client=redis_client)
    masker = Masker()
    direct_synthesizer = DefaultLLMSynthesizer()

    return ChatService(
        searcher=searcher,
        two_stage=two_stage,
        router=router,
        agent_runner=agent_runner,
        long_context=long_context,
        memory=memory,
        sensitive_filter=sensitive_filter,
        masker=masker,
        direct_synthesizer=direct_synthesizer,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Run startup tasks and clean up at shutdown.

    Three idempotent bootstraps:
      - `seed_builtin_roles`: 5 built-in roles + permission keys (T5.2)
      - `seed_demo_data`: 2 demo workspaces + alice/bob users + bindings
        (M6 — Page 2 chat smoke). Inspired by `demo2/app.js` mock
        data. Passwords come from `.env` (DEMO_ALICE_PASSWORD /
        DEMO_BOB_PASSWORD). Idempotent by uuid5 UUID.
      - `ensure_all`: Qdrant `chunks_v1` collection + 8 payload indexes (T2.1)
      - `seed_default_categories` + `seed_default_ticket_statuses`
        (T4.2): make sure both feedback dictionaries are populated.
      - `set_chat_service_factory(_build_chat_service_or_none)`: wire
        the production `ChatService` (T3 wire-up). If any collaborator
        fails to initialize, the factory stays unset and `/chat/query`
        returns 503 — a missing dependency is non-fatal at startup
        but surfaces as a real error on the first request.

    Re-running any of these is a no-op, so the lifespan is safe to
    invoke from dev reloads, tests, and prod boots alike.
    """
    settings = get_settings()

    session = SessionLocal()
    try:
        seed_builtin_roles(session)
        seed_demo_data(session)
        svc = FeedbackService(session=session)
        svc.seed_default_categories()
        svc.seed_default_ticket_statuses()
        session.commit()
        logger.info("rbac + feedback dictionaries + demo data seeded")
    finally:
        session.close()

    try:
        qdrant = get_qdrant_client(settings)
        ensure_all(qdrant, settings)
    except Exception as exc:
        # Qdrant init is non-fatal at startup — log and continue so health
        # checks still respond. The retrieval path will surface a real
        # error on the first /chat request.
        logger.warning("qdrant init skipped: %s", exc)

    # T3 wire-up: build the ChatService at startup so the first /chat
    # request doesn't pay the construction cost. Failure is logged and
    # surfaced as 503 from the chat router, NOT as a startup crash.
    service = _build_chat_service()
    if service is None:
        set_chat_service_factory(None)
        logger.warning(
            "chat service not wired — /chat/query will return 503 "
            "until the dependency is healthy"
        )
    else:
        set_chat_service_factory(lambda: service)
        logger.info("chat service wired")

    yield


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    Currently exposes `/health`, `/metrics`, and the `/api/v1/*`
    gateway (auth + documents + chat + feedback). The `/chat/query`
    endpoint requires the lifespan to have wired the chat service —
    if not, the chat router returns 503.
    """
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Enterprise Agentic RAG prototype — phase1-mvp",
        lifespan=lifespan,
    )
    application.include_router(gateway_router)
    # Prometheus scrape endpoint — root path, IP-allowlisted (+ bearer).
    application.include_router(metrics_router)

    @application.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Liveness probe. Returns static OK without touching dependencies."""
        return {"status": "ok", "app": settings.app_name}

    return application


app = create_app()


__all__ = ["create_app"]