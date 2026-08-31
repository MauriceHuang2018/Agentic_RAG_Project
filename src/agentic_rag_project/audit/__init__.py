"""Audit — WORM audit log writer & query API (DESIGN 2.2 #11).

Append-only `audit_logs` table (UPDATE/DELETE blocked at DB trigger layer
via alembic 0011). Redis buffer flushes asynchronously via
`flush_audit_buffer` Celery task. Query API under `/admin/audit-logs`
(see `api_gateway.admin_router`).

Public surface:
    - `AuditService` — record / flush_buffer / query + WORM adapter methods
    - `AuditEvent`   — structured payload (8 action types)
"""
from __future__ import annotations

from .events import AuditEvent
from .service import AuditService

__all__ = ["AuditEvent", "AuditService"]