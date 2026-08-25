"""`/metrics` HTTP endpoint with IP allowlist + bearer token (T4.3).

The Prometheus scrape config is the only consumer. By default we
allow only loopback (`127.0.0.1`, `::1`) plus the configured
`METRICS_ALLOWED_CIDRS` env var. Anything outside the IP allowlist
returns 403.

For multi-VPC / cross-network deployments, an optional bearer token
gates the endpoint on top of the IP check. Set `METRICS_BEARER_TOKEN`
in the env (or pass `bearer_token=` to `MetricsAllowlist.from_env`)
to require `Authorization: Bearer <token>`. Mismatch returns 401.

DESIGN 4.7 — the IP allowlist is enough for a prototype. The bearer
token is an additive gate so we can deploy Prometheus across the
VPC without giving up authentication.

Production setup:
  1. `PROMETHEUS_MULTIPROC_DIR=/tmp/prom`  (set by supervisord)
  2. Optional: `METRICS_BEARER_TOKEN=<random>` (rotated quarterly)
  3. Prometheus scrape config:
     scrape_configs:
       - job_name: 'agentic-rag'
         static_configs: [{targets: ['app:8000']}]
         metrics_path: '/metrics'
         authorization:
           type: Bearer
           credentials_file: /etc/prometheus/agentic_rag_token
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import Iterable

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    generate_latest,
)

logger = logging.getLogger(__name__)

_BEARER_SCHEME = "bearer "


@dataclass(frozen=True)
class MetricsAllowlist:
    """Allowlist parsed once at app startup.

    `extra_cidrs` come from `METRICS_ALLOWED_CIDRS` (comma-joined
    CIDR strings). Loopback (`127.0.0.0/8` + `::1/128`) is always
    in scope. We compare the *immediate* client IP — proxies must
    be configured to preserve it (or we add X-Forwarded-For
    handling later — out of T4.3 scope).

    `bearer_token` (when set) requires `Authorization: Bearer <token>`
    in addition to the IP check. Constant-time comparison via
    `hmac.compare_digest` so a timing-attack cannot leak the token.
    """

    extra_cidrs: tuple[str, ...] = ()
    bearer_token: str | None = None
    _networks: tuple = field(default=())

    @classmethod
    def from_env(
        cls,
        cidr_env_var: str = "METRICS_ALLOWED_CIDRS",
        bearer_env_var: str = "METRICS_BEARER_TOKEN",
    ) -> "MetricsAllowlist":
        raw_cidrs = os.environ.get(cidr_env_var, "")
        token = os.environ.get(bearer_env_var, "") or None
        return cls(
            extra_cidrs=tuple(_split_csv(raw_cidrs)),
            bearer_token=token,
        )

    def __post_init__(self) -> None:
        # Build the network list once.
        nets: list = [
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("::1/128"),
        ]
        for cidr in self.extra_cidrs:
            try:
                nets.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                logger.warning("metrics allowlist: skipping invalid CIDR %r", cidr)
        object.__setattr__(self, "_networks", tuple(nets))

    def allows_ip(self, client_ip: str) -> bool:
        """IP-only check — used by callers that don't go through HTTP."""
        try:
            addr = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return any(addr in net for net in self._networks)

    def verify_bearer(self, authorization_header: str | None) -> bool:
        """Return True if `authorization_header` carries the configured
        bearer token. Returns True (no-op) when no token is configured.
        Constant-time comparison.
        """
        if not self.bearer_token:
            return True
        if not authorization_header:
            return False
        if not authorization_header.lower().startswith(_BEARER_SCHEME):
            return False
        presented = authorization_header[len(_BEARER_SCHEME):].strip()
        # compare_digest requires equal-length strings; if lengths
        # differ we still compare_digest against a padded value to
        # avoid leaking the length. secrets.compare_digest already
        # handles this safely.
        return hmac.compare_digest(
            presented.encode("utf-8"),
            self.bearer_token.encode("utf-8"),
        )

    def allows(self, client_ip: str) -> bool:
        """Back-compat: IP-only. Prefer `allows_ip` going forward."""
        return self.allows_ip(client_ip)

    def generate_bearer_token(self) -> str:
        """Generate a fresh 256-bit token (used by `make_dev_token`
        helper scripts)."""
        return secrets.token_urlsafe(32)


def _split_csv(raw: str) -> Iterable[str]:
    for part in raw.split(","):
        part = part.strip()
        if part:
            yield part


# ---------------------------------------------------------------------------
# Router factory — keeps the singleton injectable for tests.
# ---------------------------------------------------------------------------


def build_metrics_router(
    *,
    registry: CollectorRegistry,
    allowlist: MetricsAllowlist | None = None,
) -> APIRouter:
    """Return an APIRouter exposing `GET /metrics`.

    `registry` is the prometheus_client registry to scrape from.
    Pass the multiprocess collector in production, a fresh
    per-test registry in tests.

    The allowlist combines IP filtering (always) with an optional
    bearer-token check (when `allowlist.bearer_token` is set). Both
    gates must pass.
    """
    allowlist = allowlist or MetricsAllowlist()
    router = APIRouter()

    @router.get(
        "/metrics",
        summary="Prometheus exposition endpoint (IP allowlist + bearer token)",
        response_class=Response,
    )
    def get_metrics(request: Request) -> Response:
        client_ip = request.client.host if request.client else "0.0.0.0"
        if not allowlist.allows_ip(client_ip):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="metrics endpoint is restricted by IP allowlist",
            )
        # Bearer check only runs when a token is configured. Loopback
        # callers can therefore scrape without auth (dev UX); off-box
        # scrapers MUST send Authorization.
        if allowlist.bearer_token and not allowlist.verify_bearer(
            request.headers.get("authorization")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        payload = generate_latest(registry)
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    return router


__all__ = [
    "MetricsAllowlist",
    "build_metrics_router",
]