"""AuditEvent payload dataclass.

Fields are aligned with DESIGN 2.2 #11 (`audit_logs` row shape). `ts`
is intentionally NOT a field here — the DB column defaults to `now()`
so the wall clock is authoritative (avoid app/DB clock skew).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Whitelist of audit actions (DESIGN §4.6.2 / TASK M4.3 §三.1).
# Adding a new action here MUST come with a corresponding
# `feedback_categories` / `feedback_tags` policy + admin-API enablement;
# do NOT add casually.
#
# M5 (2026-08-27) adds 3 new actions to cover audit gaps surfaced by
# `docs/security_audit/TODO_security_audit.md`:
#   * `csat_read`              — admin calls /admin/csat/* (M4.4 TODO #5)
#   * `role_bind`              — RBAC role-binding mutation (co-exists with
#                                legacy `role_assign`; decision 8)
#   * `sensitive_word_update`  — sensitive word hot-reload (more specific
#                                than `sensitive_update` which covers
#                                per-row sensitive-value writes)
# The 11-value whitelist is mirrored in the DB CHECK constraint by
# migration `0012_feedback_idempotency_and_audit_expand.py`. Adding a
# 12th value requires another additive migration — there is no path
# for a runtime literal that the DB does not accept.
AuditAction = Literal[
    "query",
    "ingest",
    "delete",
    "access_denied",
    "guardrail_block",
    "feedback_submit",
    "role_assign",
    "sensitive_update",
    "csat_read",
    "role_bind",
    "sensitive_word_update",
]


@dataclass
class AuditEvent:
    """One row destined for `audit_logs`.

    Attributes:
        user_id: UUID string (or `str(uuid.UUID)`). No PII leak into logs.
        action: One of `AuditAction` literals.
        query: Original user query (only on `query` / `guardrail_block`).
        sanitized_query: Query after PII redaction (when applicable).
        retrieved_doc_ids: Doc IDs the retrieval step returned.
        model: LLM model name used (e.g. `openai/qwen3.5-plus`).
        extra: Free-form JSON for action-specific context (e.g.
            `{"block_reason": "prompt_injection", "rule_version": "v1.0.0"}`).
    """

    user_id: str
    action: AuditAction
    query: str | None = None
    sanitized_query: str | None = None
    retrieved_doc_ids: list[str] = field(default_factory=list)
    model: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = ["AuditAction", "AuditEvent"]