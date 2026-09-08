"""QueryGuardrail — 4-class pre-LLM check (M4.3.2).

R13: this is the **single** sanctioned entry point for running
pre-flight safety on a user query. The chat router calls
`QueryGuardrail.check(query)` as the first thing after
workspace resolution; nothing else reaches the LLM.

Four classes (DESIGN §4.6.3 + Decision B):
  1. `sensitive_word`  — word-list (re-uses `SensitiveWordFilter`).
  2. `prompt_injection` — regex (5 patterns).
  3. `PII`             — regex (phone / id_card / email).
  4. `out_of_scope`    — keyword set (30+ topics).

Each class returns one of two outcomes:
  - `allowed=True` + `sanitized_query` (the PII-redacted form when
    applicable, or the original).
  - `allowed=False` + `category` + `reason` + `matched_text` (a
    short snippet the caller can put in `block_reason`).
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_rag_project.post_processor.filter import SensitiveWordFilter

from .rules import (
    OUT_OF_SCOPE_KEYWORDS,
    PII_PATTERNS,
    PROMPT_INJECTION_PATTERNS,
)


@dataclass
class GuardrailResult:
    """Outcome of a single `QueryGuardrail.check` invocation.

    Attributes:
        allowed: True when the query may proceed (sanitized form is
            in `sanitized_query`).
        category: One of `"sensitive_word"`, `"prompt_injection"`,
            `"pii"`, `"out_of_scope"`, or `None` when allowed.
        reason: Human-readable short reason (e.g. "matched sensitive
            keyword '反动'"). Empty string when allowed.
        matched_text: A short snippet of the offending span
            (≤80 chars), safe to log.
        sanitized_query: The redacted form (PII hits) or the
            original input. Always set; on block, callers may use
            this as the audit `sanitized_query` field.
    """

    allowed: bool
    category: str | None
    reason: str
    matched_text: str
    sanitized_query: str


def _snippet(text: str, span: tuple[int, int]) -> str:
    """Return a short surrounding snippet for the matched span.

    Capped at 80 chars so audit logs stay bounded; the matched
    span itself is preserved (not truncated), with up to 20 chars
    of surrounding context on each side.
    """
    start, end = span
    s = max(0, start - 20)
    e = min(len(text), end + 20)
    snippet = text[s:e]
    if s > 0:
        snippet = "…" + snippet
    if e < len(text):
        snippet = snippet + "…"
    return snippet


class QueryGuardrail:
    """Stateless 4-class pre-LLM guardrail.

    Construction takes the `SensitiveWordFilter` (so admin updates
    flow through to the trie without code changes); the other 3
    classes use module-level rules.
    """

    def __init__(self, sensitive_filter: SensitiveWordFilter) -> None:
        self._sensitive = sensitive_filter

    def check(self, query: str) -> GuardrailResult:
        """Run the 4-class check; return the first hit or `allowed=True`.

        Precedence is intentional: PII (sanitizable) is reported
        before prompt_injection / out_of_scope (block-only) so the
        chat path can show the sanitized query to the LLM when only
        PII was detected. The sensitive_word class also reports
        `allowed=False` because echoing a banned phrase to the LLM
        risks leaking it into the answer.
        """
        if not query:
            # Empty input is a routing concern, not a policy block.
            # Pass through with `allowed=True` so the downstream
            # `ChatService` can raise `EmptyQueryError` → HTTP 400.
            # The guardrail stays neutral on the "is this a real
            # question?" decision.
            return GuardrailResult(
                allowed=True,
                category=None,
                reason="empty query",
                matched_text="",
                sanitized_query="",
            )

        # 1. sensitive_word — block + log
        hits = self._sensitive.matches(query)
        if hits:
            return GuardrailResult(
                allowed=False,
                category="sensitive_word",
                reason=f"matched sensitive keyword(s): {', '.join(hits[:3])}",
                matched_text=_snippet(query, (0, min(40, len(query)))),
                sanitized_query=query,  # do NOT redact before block
            )

        # 2. PII — redact + allow with sanitized_query
        sanitized = query
        pii_hit: tuple[str, str] | None = None
        for label, pattern in PII_PATTERNS:
            m = pattern.search(sanitized)
            if m is not None:
                pii_hit = (label, m.group(0))
                sanitized = pattern.sub(
                    lambda mo: "*" * len(mo.group(0)), sanitized
                )
        if pii_hit is not None:
            label, value = pii_hit
            return GuardrailResult(
                allowed=True,
                category="pii",
                reason=f"redacted {label}: {value[:4]}***",
                matched_text=value,
                sanitized_query=sanitized,
            )

        # 3. prompt_injection — block + log
        for pat in PROMPT_INJECTION_PATTERNS:
            m = pat.search(query)
            if m is not None:
                return GuardrailResult(
                    allowed=False,
                    category="prompt_injection",
                    reason="matched prompt-injection pattern",
                    matched_text=_snippet(query, m.span()),
                    sanitized_query=query,
                )

        # 4. out_of_scope — block + log
        lowered = query.lower()
        for kw in OUT_OF_SCOPE_KEYWORDS:
            if kw and kw.lower() in lowered:
                return GuardrailResult(
                    allowed=False,
                    category="out_of_scope",
                    reason=f"topic not in scope: {kw}",
                    matched_text=_snippet(
                        query, (lowered.find(kw.lower()), lowered.find(kw.lower()) + len(kw))
                    ),
                    sanitized_query=query,
                )

        return GuardrailResult(
            allowed=True,
            category=None,
            reason="",
            matched_text="",
            sanitized_query=query,
        )


__all__ = ["GuardrailResult", "QueryGuardrail"]
