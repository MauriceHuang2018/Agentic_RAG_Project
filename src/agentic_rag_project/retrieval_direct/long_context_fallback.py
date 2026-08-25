"""Long-context fallback path (T3.3).

DESIGN 4.4: when the two-stage retriever's top-1 score falls below
`LITELLM_LONG_CONTEXT_THRESHOLD` (default 0.5), the chat endpoint
switches from "answer grounded in a few short chunks" to "feed the
best parent chapters verbatim into a 128K model and let it answer
from the whole passage". This module owns that second path.

Why a separate module? Because:

  * The model name and prompt template are different from the
    short-context generator; bundling them in one place makes the
    call site (chat endpoint) read like a story:
      if result.fallback_triggered:
          return LongContextFallback().generate(query, parents, history)
  * The answer is tagged `[long-context]` so the audit trail and the
    downstream evaluation pipeline can distinguish it from the
    short-context path. Both paths land in `messages`; only the
    `metadata_["source"]` field differs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable

from agentic_rag_project.observability.llm_metrics import completion_with_metrics
from agentic_rag_project.retrieval_direct.search import SearchResult

logger = logging.getLogger(__name__)


LONG_CONTEXT_TAG = "[long-context]"

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_PARENT_CHARS = 90_000  # well under 128K tokens
DEFAULT_ANSWER_CHARS = 4000


class LongContextFallbackError(Exception):
    """Raised when the fallback cannot produce an answer."""


@dataclass
class LongContextResult:
    """The fallback's output — distinct from a normal `AgentResult`."""

    answer: str
    parents_used: list[str]  # chunk_ids of parents included in the prompt
    model: str
    truncated: bool = False  # True if input was capped at MAX_PARENT_CHARS

    def to_metadata(self) -> dict:
        """Return a dict suitable for `messages.metadata_["long_context"]`."""
        return {
            "source": "long-context",
            "model": self.model,
            "parents_used": self.parents_used,
            "truncated": self.truncated,
        }


# Type alias for the injected LLM callable. Same shape as the agent
# graph's `LLMCallFn` but kept distinct so we don't accidentally
# swap the two in tests.
LongContextLLM = Callable[[str, str, float], str]


FALLBACK_SYSTEM_PROMPT = (
    "You are an answer synthesizer operating in 'long-context' mode. "
    "You have been given one or more full document sections (parents) "
    "because the short retrieval pipeline could not find a single "
    "high-confidence hit. Read the provided sections carefully and "
    "answer the user's question directly. Cite the parent ids you "
    "used in the form `[parent_id]` next to the relevant claim. If the "
    "sections do not contain the answer, say so explicitly rather than "
    "fabricating."
)


def build_long_context_prompt(
    query: str,
    parents: list[SearchResult],
    history: Iterable[dict] | None = None,
    *,
    max_parent_chars: int = DEFAULT_MAX_PARENT_CHARS,
) -> tuple[str, bool]:
    """Render the user-side prompt for the fallback model.

    Returns `(prompt_text, truncated_flag)`. Parents are joined with
    separators so the model can see chapter boundaries; if the joined
    text exceeds `max_parent_chars`, the surplus is dropped and the
    flag is set so the caller can record that fact.
    """
    blocks: list[str] = []
    truncated = False
    used_ids: list[str] = []
    budget = max_parent_chars
    for parent in parents:
        body = parent.content or ""
        block = f"[{parent.chunk_id}]\n{body}"
        # If the block doesn't fit at all, stop — we don't emit a
        # parent header with an empty body, the model would just see
        # a useless label and waste tokens on it.
        if len(block) > budget:
            truncated = True
            break
        blocks.append(block)
        used_ids.append(parent.chunk_id)
        budget -= len(block)
        if budget <= 0:
            break

    history_block = ""
    if history:
        history_block = "Chat history:\n" + "\n".join(
            f"[{t.get('role', '?')}] {t.get('content', '')}"
            for t in list(history)[-10:]
        ) + "\n\n"

    prompt = (
        history_block
        + "Document sections:\n\n"
        + "\n\n---\n\n".join(blocks)
        + f"\n\nUser question: {query}"
    )
    return prompt, truncated


def default_long_context_call(
    system_prompt: str, user_prompt: str, timeout: float
) -> str:
    """Production LLM call for the fallback model.

    Uses `litellm.completion` against `litellm_long_context_model`,
    which is configured separately from the main chat model so a
    different (and expensive) 128K-capable model can be selected.

    Token accounting flows through `completion_with_metrics` so the
    L1 `chat_tokens_total` counter sees the long-context spend.
    """
    from agentic_rag_project.config import get_settings

    settings = get_settings()
    response = completion_with_metrics(
        model=settings.litellm_long_context_model,
        api_base=settings.litellm_base_url or None,
        api_key=settings.litellm_api_key or None,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        timeout=timeout,
    )
    return response["choices"][0]["message"]["content"] or ""


class LongContextFallback:
    """Run the long-context fallback path.

    All dependencies (LLM call, model name, prompts, char budgets)
    are injectable so tests can substitute fakes.
    """

    def __init__(
        self,
        *,
        llm_call: LongContextLLM | None = None,
        model: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_parent_chars: int = DEFAULT_MAX_PARENT_CHARS,
        max_answer_chars: int = DEFAULT_ANSWER_CHARS,
        system_prompt: str = FALLBACK_SYSTEM_PROMPT,
    ) -> None:
        if max_parent_chars <= 0:
            raise LongContextFallbackError("max_parent_chars must be positive")
        if max_answer_chars <= 0:
            raise LongContextFallbackError("max_answer_chars must be positive")
        if timeout_seconds <= 0:
            raise LongContextFallbackError("timeout_seconds must be positive")
        self._llm_call = llm_call or default_long_context_call
        self._model = model  # None → resolved from settings at call time
        self._timeout = timeout_seconds
        self._max_parent_chars = max_parent_chars
        self._max_answer_chars = max_answer_chars
        self._system_prompt = system_prompt

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def generate(
        self,
        query: str,
        parents: list[SearchResult],
        history: Iterable[dict] | None = None,
    ) -> LongContextResult:
        """Generate a fallback answer.

        The returned answer is prefixed with `[long-context]` so the
        caller can route it through evaluation / feedback / audit
        with the source-tag set correctly.
        """
        if not (query or "").strip():
            raise LongContextFallbackError("query must be non-empty")
        if not parents:
            raise LongContextFallbackError(
                "long-context fallback needs at least one parent chunk"
            )
        prompt, truncated = build_long_context_prompt(
            query,
            parents,
            history=history,
            max_parent_chars=self._max_parent_chars,
        )
        raw = self._llm_call(self._system_prompt, prompt, self._timeout)
        body = (raw or "").strip()
        if not body:
            raise LongContextFallbackError("long-context model returned empty answer")
        body = body[: self._max_answer_chars]
        # Prepend the tag only if the model didn't already include it.
        if not body.startswith(LONG_CONTEXT_TAG):
            body = f"{LONG_CONTEXT_TAG} {body}"
        model = self._model or self._resolve_default_model()
        return LongContextResult(
            answer=body,
            parents_used=[p.chunk_id for p in parents],
            model=model,
            truncated=truncated,
        )

    @staticmethod
    def _resolve_default_model() -> str:
        from agentic_rag_project.config import get_settings

        return get_settings().litellm_long_context_model