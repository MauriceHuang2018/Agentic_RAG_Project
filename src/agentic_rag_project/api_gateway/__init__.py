"""API Gateway — unified entry for HTTP requests.

Responsibilities (DESIGN 2.2 #1):
- Authentication & RBAC enforcement via FastAPI dependencies
- Request routing to downstream modules
- Audit logging hook
- Wiring of /auth, /workspaces endpoints (DESIGN 5.2)
- /metrics endpoint for Prometheus (T4.3) — mounted at root,
  IP-allowlisted, no auth dependency.

Implementation lands in T5.1. Document upload/status endpoints are
mounted by T1.5. Chat endpoint mounted by T3 wire-up.
"""

from __future__ import annotations

from fastapi import APIRouter

from agentic_rag_project.api_gateway.admin_router import (
    get_router as get_admin_router,
)
from agentic_rag_project.api_gateway.chat_router import (
    get_router as get_chat_router,
)
from agentic_rag_project.api_gateway.documents_router import (
    get_router as get_documents_router,
)
from agentic_rag_project.api_gateway.feedback_router import (
    get_router as get_feedback_router,
)
from agentic_rag_project.api_gateway.me_router import router as me_router
from agentic_rag_project.api_gateway.router import router as auth_router
from agentic_rag_project.config import get_settings
from agentic_rag_project.observability import (
    MetricsAllowlist,
    build_metrics_router,
)
from agentic_rag_project.observability.registry import get_default_registry

# Aggregator for the api-gateway; mounted in main.py under /api/v1.
gateway_router = APIRouter()
gateway_router.include_router(auth_router, prefix="/api/v1")
gateway_router.include_router(get_documents_router(), prefix="/api/v1")
gateway_router.include_router(get_chat_router(), prefix="/api/v1")
gateway_router.include_router(get_feedback_router(), prefix="/api/v1")
gateway_router.include_router(get_admin_router(), prefix="/api/v1")
# `/me/*` — user-scoped endpoints (workspace picker, future /me/avatar,
# /me/api-tokens per `docs/原型设计/前端页面规划与字段-表映射.md` §Page 14).
gateway_router.include_router(me_router, prefix="/api/v1")

# Prometheus scrape endpoint — mounted at ROOT, not under /api/v1,
# per the standard Prometheus convention. The allowlist gates
# non-loopback IPs; an optional bearer token adds a second layer for
# cross-VPC scrapers. Configuration:
#   * `METRICS_ALLOWED_CIDRS` — comma-joined extra CIDRs (loopback
#     is always allowed).
#   * `METRICS_BEARER_TOKEN` — when set, requires
#     `Authorization: Bearer <token>` from non-loopback callers.
_settings = get_settings()
metrics_router = build_metrics_router(
    registry=get_default_registry(),
    allowlist=MetricsAllowlist(
        extra_cidrs=tuple(
            c.strip()
            for c in _settings.metrics_allowed_cidrs.split(",")
            if c.strip()
        ),
        bearer_token=_settings.metrics_bearer_token or None,
    ),
)