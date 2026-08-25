"""Production `LLMSynthesizer` implementation for the direct chat path.

`chat_service.ChatService` declares `direct_synthesizer: LLMSynthesizer`
and requires the FastAPI app layer to inject one at startup. Tests
pass `StubDirectSynth` or a lambda — this module is the production
default, wiring the synthesizer to the configured chat model and
recording token usage to the L1 `chat_tokens_total` counter.

Why a class instead of a function? The Protocol is
`def synthesize(self, *, system_prompt, user_prompt, timeout) -> str`,
so the binding target is an object with a `synthesize` method.
Returning a function would still satisfy `Protocol` thanks to
duck-typing, but the class makes model resolution explicit (you can
inject a custom `model_resolver` for tests / multi-tenant setups).
"""

from __future__ import annotations

from typing import Callable

from agentic_rag_project.observability.llm_metrics import completion_with_metrics


class DefaultLLMSynthesizer:
    """Default direct-path synthesizer.

    Forwards to `completion_with_metrics` so token usage flows into
    `chat_tokens_total{model=litellm_model, direction=in|out}`.

    `model_resolver` is a zero-arg callable returning the model name;
    defaults to `get_settings().litellm_model`. Tests can pass
    `model_resolver=lambda: "test-model"` to avoid hitting pydantic.
    """

    def __init__(
        self,
        *,
        model_resolver: Callable[[], str] | None = None,
    ) -> None:
        self._resolve_model = model_resolver

    def synthesize(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        timeout: float,
    ) -> str:
        from agentic_rag_project.config import get_settings

        settings = get_settings()
        model = (
            self._resolve_model()
            if self._resolve_model is not None
            else settings.litellm_model
        )
        response = completion_with_metrics(
            model=model,
            api_base=settings.litellm_base_url or None,
            api_key=settings.litellm_api_key or None,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout=timeout,
        )
        return response["choices"][0]["message"]["content"] or ""


__all__ = ["DefaultLLMSynthesizer"]