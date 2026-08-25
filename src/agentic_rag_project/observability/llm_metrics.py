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

Failures are non-fatal: if a provider omits `usage`, the wrapper
silently records nothing — better than raising on the request hot path.
"""

from __future__ import annotations

import logging
from typing import Any

from agentic_rag_project.observability.chat_metrics import add_chat_tokens

logger = logging.getLogger(__name__)


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
    """
    import litellm  # type: ignore[import-not-found]

    response = litellm.completion(model=model, **kwargs)
    record_completion_tokens(model=model, response=response)
    return response


__all__ = [
    "completion_with_metrics",
    "record_completion_tokens",
]