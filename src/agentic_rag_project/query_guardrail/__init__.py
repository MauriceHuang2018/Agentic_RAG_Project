"""Query-time guardrail (M4.3.2).

R13 hard constraint: every LLM-bound input MUST pass through
`QueryGuardrail.check()` before being routed to the chat service.
The chat router wires this as the first line after workspace
resolution.

Public surface:
    - `QueryGuardrail` — runs the 4-class check (sensitive_word,
      prompt_injection, PII, out_of_scope).
    - `GuardrailResult` — what `check()` returns: allowed/denied +
      category + sanitized form.

The detection rules are listed in `rules.py` and intentionally
broad enough to catch obvious cases without a model call — false
positives degrade UX, false negatives leak data, so this layer
favors the latter for sensitive_word / PII / prompt_injection
and the former for out_of_scope.
"""
from __future__ import annotations

from .checker import GuardrailResult, QueryGuardrail

__all__ = ["GuardrailResult", "QueryGuardrail"]
