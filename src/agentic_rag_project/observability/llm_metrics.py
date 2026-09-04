"""LiteLLM wrapper that records `chat_tokens_total` (T4.3 finalization).

The four LLM call sites — agent runner, long-context fallback,
query classifier, and direct-path synthesizer — all funnel through
`completion_with_metrics(model, **kwargs)` instead of calling
`litellm.completion` directly. After the model returns, we extract
`usage.prompt_tokens` + `usage.completion_tokens` from the response
and increment `chat_tokens_total{model,direction}` via the existing
`add_chat_tokens` helper.

Why a wrapper instead of recording tokens inline? Three reasons:

  1. The four call sites already pass different kwargs (`response_format`
     for the classifier, longer `timeout` for the long-context path,
     etc.); a single wrapper keeps token accounting identical without
     having to duplicate the read from `response.usage` four times.
  2. Tests can monkey-patch `litellm.completion` via the `litellm`
     attribute on this module — every caller goes through this wrapper,
     so a single patch covers all four sites.
  3. If we later swap LiteLLM for a different gateway, only this one
     file changes.

Resilience (M3.x synthesizer hang, 2026-09-04):
`completion_with_metrics` retries **once** on
`litellm.exceptions.Timeout` (the exception the litellm proxy raises
when an upstream attempt times out — verified during the alice-chat
8-minute-hang investigation). A single retry covers the common case
of a transient upstream stall (the next attempt usually lands on a
healthy connection in <1s) without masking genuine capacity problems
(we still propagate the second Timeout, which FastAPI's global
handler maps to a fast 500). Non-timeout exceptions (`BadRequestError`,
`PermissionDeniedError`, etc.) bypass the retry path entirely so
deterministic failures stay loud.

Failures are non-fatal: if a provider omits `usage`, the wrapper
silently records nothing — better than raising on the request hot path.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agentic_rag_project.observability.chat_metrics import add_chat_tokens

logger = logging.getLogger(__name__)


# Module-level so tests can monkeypatch it to 0 and keep the suite
# fast. Production reads the constant directly inside the retry loop.
_RETRY_SLEEP_SECONDS: float = 1.5


def _extract_usage(response: Any) -> tuple[int, int]:
    """Return `(prompt_tokens, completion_tokens)` from a litellm response.

    `litellm.completion` returns a `ModelResponse` (dict-like). The
    `usage` object carries `prompt_tokens`, `completion_tokens`, and
    `total_tokens`; some providers omit it. Defensive read so the hot
    path never raises on a missing field.
    """
    usage = None
    if hasattr(response, "usage"):
        usage = response.usage
    elif hasattr(response, "get"):
        usage = response.get("usage")
    if not usage:
        return 0, 0
    if hasattr(usage, "get"):
        prompt = usage.get("prompt_tokens") or 0
        completion = usage.get("completion_tokens") or 0
    else:
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
    try:
        prompt = max(int(prompt), 0)
        completion = max(int(completion), 0)
    except (TypeError, ValueError):
        return 0, 0
    return prompt, completion


def record_completion_tokens(*, model: str, response: Any) -> tuple[int, int]:
    """Read `usage` off a response and bump `chat_tokens_total`.

    Returns `(prompt_tokens, completion_tokens)` so tests can assert
    on the counts without re-reading `usage`.
    """
    prompt, completion = _extract_usage(response)
    if prompt:
        add_chat_tokens(model=model, direction="in", count=prompt)
    if completion:
        add_chat_tokens(model=model, direction="out", count=completion)
    return prompt, completion


def completion_with_metrics(model: str, **kwargs: Any) -> Any:
    """Drop-in replacement for `litellm.completion` that records tokens.

    `model` is required as a positional keyword (callers already pass
    it explicitly). All other kwargs (`messages`, `timeout`,
    `response_format`, `api_base`, `api_key`) are forwarded unchanged.

    `litellm` is imported lazily inside the function. Tests can patch
    it via `monkeypatch.setitem(sys.modules, "litellm", fake)`.

    Resilience: retries once on `litellm.exceptions.Timeout` after
    sleeping `_RETRY_SLEEP_SECONDS`. Non-timeout exceptions propagate
    immediately so deterministic failures stay loud. See module
    docstring for the M3.x rationale.
    """
    import litellm  # type: ignore[import-not-found]

    try:
        response = litellm.completion(model=model, **kwargs)
    except litellm.exceptions.Timeout:
        logger.warning(
            "llm completion timed out; retrying once (model=%s)", model
        )
        time.sleep(_RETRY_SLEEP_SECONDS)
        response = litellm.completion(model=model, **kwargs)
    record_completion_tokens(model=model, response=response)
    return response


__all__ = [
    "completion_with_metrics",
    "record_completion_tokens",
]