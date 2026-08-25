"""RAGAS-style metrics (T4.1).

DESIGN 4.5 — five RAGAS-inspired metrics plus one in-house
`citation_accuracy`. Each is a pure function that takes its inputs
and returns a `MetricResult`. Two helpers live alongside:

  * `token_jaccard(a, b)` — fallback similarity for when no embedder
    is available (cheap, deterministic, good enough for unit tests).
  * `cosine_sim(a, b)`   — used when the caller supplies an embedder.

Why both? Production wants cosine similarity on real embeddings;
unit tests want a no-deps fallback. Keeping them separate lets each
caller pick without forcing the other side.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


# Metric names — mirrored in `db.models.audit.EvaluationResult.metric_name`.
METRIC_FAITHFULNESS = "faithfulness"
METRIC_ANSWER_RELEVANCY = "answer_relevancy"
METRIC_CONTEXT_PRECISION = "context_precision"
METRIC_CONTEXT_RECALL = "context_recall"
METRIC_ANSWER_SIMILARITY = "answer_similarity"
METRIC_CITATION_ACCURACY = "citation_accuracy"

ALL_METRIC_NAMES: tuple[str, ...] = (
    METRIC_FAITHFULNESS,
    METRIC_ANSWER_RELEVANCY,
    METRIC_CONTEXT_PRECISION,
    METRIC_CONTEXT_RECALL,
    METRIC_ANSWER_SIMILARITY,
    METRIC_CITATION_ACCURACY,
)


@dataclass
class MetricResult:
    """One metric's score for one (case, message) pair."""

    metric_name: str
    value: float
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(
                f"metric {self.metric_name} value {self.value} not in [0, 1]"
            )


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------


_TOKEN_SPLIT = re.compile(r"[\s,。.!?;:、，！？；：]+", flags=re.UNICODE)


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return {tok for tok in _TOKEN_SPLIT.split(text.lower()) if tok}


def token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity over the lowercased token sets.

    Used as a fallback when an embedder is unavailable. Returns 0.0
    when either side is empty so a degenerate input still maps to a
    well-defined score.
    """
    sa, sb = _tokenize(a), _tokenize(b)
    if not sa and not sb:
        return 0.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0 on mismatch/empty."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Metric implementations
# ---------------------------------------------------------------------------


def context_precision(
    *,
    retrieved_chunk_ids: list[str],
    expected_chunk_ids: list[str],
) -> MetricResult:
    """What fraction of the retrieved chunks are also expected?

    `k = max(1, len(retrieved_chunk_ids))` so an empty retrieval still
    returns 0 (not div-by-zero). When `expected_chunk_ids` is empty
    the metric is reported as 0.0 with `details.skipped="no_expected"`.
    """
    if not expected_chunk_ids:
        return MetricResult(
            metric_name=METRIC_CONTEXT_PRECISION,
            value=0.0,
            details={"skipped": "no_expected"},
        )
    if not retrieved_chunk_ids:
        return MetricResult(
            metric_name=METRIC_CONTEXT_PRECISION,
            value=0.0,
            details={"retrieved": 0, "expected": len(expected_chunk_ids)},
        )
    exp = set(expected_chunk_ids)
    hits = sum(1 for cid in retrieved_chunk_ids if cid in exp)
    return MetricResult(
        metric_name=METRIC_CONTEXT_PRECISION,
        value=hits / max(1, len(retrieved_chunk_ids)),
        details={
            "hits": hits,
            "retrieved": len(retrieved_chunk_ids),
            "expected": len(expected_chunk_ids),
        },
    )


def context_recall(
    *,
    retrieved_chunk_ids: list[str],
    expected_chunk_ids: list[str],
) -> MetricResult:
    """What fraction of expected chunks did retrieval surface?"""
    if not expected_chunk_ids:
        return MetricResult(
            metric_name=METRIC_CONTEXT_RECALL,
            value=0.0,
            details={"skipped": "no_expected"},
        )
    exp = set(expected_chunk_ids)
    hit = sum(1 for cid in expected_chunk_ids if cid in retrieved_chunk_ids)
    return MetricResult(
        metric_name=METRIC_CONTEXT_RECALL,
        value=hit / len(exp),
        details={"hits": hit, "expected": len(expected_chunk_ids)},
    )


def answer_similarity(
    *,
    expected_answer: str,
    answer: str,
    embedder: Callable[[list[str]], list[list[float]]] | None = None,
) -> MetricResult:
    """Cosine similarity on real embeddings, falling back to Jaccard."""
    if not expected_answer or not answer:
        return MetricResult(
            metric_name=METRIC_ANSWER_SIMILARITY,
            value=0.0,
            details={"skipped": "empty_input"},
        )
    if embedder is not None:
        try:
            vecs = embedder([expected_answer, answer])
            if (
                isinstance(vecs, list)
                and len(vecs) == 2
                and isinstance(vecs[0], list)
                and isinstance(vecs[1], list)
            ):
                return MetricResult(
                    metric_name=METRIC_ANSWER_SIMILARITY,
                    value=cosine_sim(vecs[0], vecs[1]),
                    details={"method": "cosine"},
                )
        except Exception as exc:
            logger.warning("answer_similarity embedder failed: %s", exc)
    return MetricResult(
        metric_name=METRIC_ANSWER_SIMILARITY,
        value=token_jaccard(expected_answer, answer),
        details={"method": "token_jaccard"},
    )


def answer_relevancy(
    *,
    query: str,
    answer: str,
    embedder: Callable[[list[str]], list[list[float]]] | None = None,
) -> MetricResult:
    """Same shape as `answer_similarity`, but against the query."""
    if not query or not answer:
        return MetricResult(
            metric_name=METRIC_ANSWER_RELEVANCY,
            value=0.0,
            details={"skipped": "empty_input"},
        )
    if embedder is not None:
        try:
            vecs = embedder([query, answer])
            if (
                isinstance(vecs, list)
                and len(vecs) == 2
                and isinstance(vecs[0], list)
                and isinstance(vecs[1], list)
            ):
                return MetricResult(
                    metric_name=METRIC_ANSWER_RELEVANCY,
                    value=cosine_sim(vecs[0], vecs[1]),
                    details={"method": "cosine"},
                )
        except Exception as exc:
            logger.warning("answer_relevancy embedder failed: %s", exc)
    return MetricResult(
        metric_name=METRIC_ANSWER_RELEVANCY,
        value=token_jaccard(query, answer),
        details={"method": "token_jaccard"},
    )


def citation_accuracy(
    *,
    answer: str,
    citations: list[str],
    expected_chunk_ids: list[str],
) -> MetricResult:
    """In-house metric: how accurate are the in-text `[chunk_id]` citations?

    Algorithm: `precision = |cited ∩ expected| / max(1, |cited|)`.
    Cited chunk_ids are extracted from the answer via the
    `[xxx]` pattern (matches `retrieval_direct.long_context_fallback`
    format as well as the synthesize_node convention). When the
    answer cites nothing the metric is 0.0 with `no_citations=True`.
    """
    cited = _extract_chunk_citations(answer)
    if not cited:
        return MetricResult(
            metric_name=METRIC_CITATION_ACCURACY,
            value=0.0,
            details={"cited": [], "hits": 0, "no_citations": True},
        )
    if not expected_chunk_ids:
        return MetricResult(
            metric_name=METRIC_CITATION_ACCURACY,
            value=0.0,
            details={"cited": sorted(cited), "hits": 0, "skipped": "no_expected"},
        )
    exp = set(expected_chunk_ids)
    hits = sum(1 for cid in cited if cid in exp)
    return MetricResult(
        metric_name=METRIC_CITATION_ACCURACY,
        value=hits / max(1, len(cited)),
        details={
            "cited": sorted(cited),
            "expected": sorted(expected_chunk_ids),
            "hits": hits,
        },
    )


_CHUNK_REF = re.compile(r"\[([A-Za-z0-9_\-]+)\]")


def _extract_chunk_citations(answer: str) -> set[str]:
    """Pull `[chunk_id]` references out of the answer text.

    Filters out common false-positives like brackets containing single
    characters or pure digits — those usually denote list markers,
    not chunk references. Chunk ids in this codebase are uuid5 strings
    (8+ alphanumerics with dashes) or short tokens like `p-1`/`c-12`.
    """
    if not answer:
        return set()
    out: set[str] = set()
    for m in _CHUNK_REF.finditer(answer):
        cid = m.group(1).strip()
        if not cid:
            continue
        # Reject single-char and pure-digit refs (markdown list markers).
        if len(cid) < 2:
            continue
        if cid.isdigit():
            continue
        out.add(cid)
    return out


# ---------------------------------------------------------------------------
# LLM-backed metric: faithfulness
# ---------------------------------------------------------------------------


# System prompt: keep it short. The model has to parse the JSON strictly.
_FAITHFULNESS_SYSTEM_PROMPT = (
    "You are evaluating whether an assistant answer is faithful to the "
    "provided context. You will be given a list of context passages and "
    "an answer. Split the answer into individual sentences, then for each "
    "sentence return whether it is supported by the context. Respond with "
    "strict JSON only, no commentary, matching this schema:\n"
    '{"sentences": [{"text": "<sentence>", "supported": true|false}]}\n'
    "Use `true` only when the sentence's claim is directly backed by some "
    "context passage."
)


def _split_sentences(text: str) -> list[str]:
    """Cheap sentence splitter (Chinese + English).

    Avoids heavier NLP — we only need stable segments for the LLM to
    judge. The LLM itself will further split long sentences if needed.
    Splits after `.!?。！？` and consumes any trailing whitespace so
    Chinese punctuation without a space (the common case) still breaks.
    """
    if not text:
        return []
    parts = re.split(r"(?<=[.!?。！？])\s*", text)
    return [p.strip() for p in parts if p and p.strip()]


def _parse_faithfulness_json(payload: Any) -> list[dict[str, Any]] | None:
    """Extract `[{text, supported}, ...]` from an LLM response.

    Returns None when the payload can't be parsed — caller decides
    whether to retry or fall back to 0.0.
    """
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if not isinstance(payload, dict):
        return None
    sentences = payload.get("sentences")
    if not isinstance(sentences, list):
        return None
    out: list[dict[str, Any]] = []
    for item in sentences:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        supported = item.get("supported", False)
        if not isinstance(text, str):
            continue
        out.append({"text": text, "supported": bool(supported)})
    return out


def _extract_json(text: str) -> Any | None:
    """Tolerantly extract a JSON object from a possibly-noisy LLM reply."""
    if not text:
        return None
    text = text.strip()
    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl >= 0:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find the first {...} block.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def faithfulness(
    *,
    answer: str,
    contexts: list[str],
    llm_call: Callable[[str, str, float], str] | None,
    timeout: float = 30.0,
    max_retries: int = 1,
) -> MetricResult:
    """Faithfulness — fraction of answer sentences supported by context.

    The LLM is asked to split the answer into sentences and mark each
    `supported`/`!supported`. The metric is `supported / total`. When
    the LLM is missing or its JSON is unparseable after one retry, we
    return 0.0 — partial credit for a corrupt reply would mask
    genuine regressions.
    """
    sentences = _split_sentences(answer)
    if not sentences or not contexts:
        return MetricResult(
            metric_name=METRIC_FAITHFULNESS,
            value=0.0,
            details={
                "skipped": "empty_input",
                "n_sentences": len(sentences),
                "n_contexts": len(contexts),
            },
        )
    if llm_call is None:
        return MetricResult(
            metric_name=METRIC_FAITHFULNESS,
            value=0.0,
            details={"skipped": "no_llm"},
        )

    user_prompt = (
        "[context]\n" + "\n\n".join(contexts) + "\n\n[answer]\n" + answer
    )

    parsed: list[dict[str, Any]] | None = None
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = llm_call(_FAITHFULNESS_SYSTEM_PROMPT, user_prompt, timeout)
        except Exception as exc:
            last_error = f"llm_call raised: {exc}"
            continue
        candidate = _parse_faithfulness_json(_extract_json(raw))
        if candidate is not None:
            parsed = candidate
            break
        last_error = "unparseable JSON"

    if not parsed:
        return MetricResult(
            metric_name=METRIC_FAITHFULNESS,
            value=0.0,
            details={"error": last_error or "no_response", "retries": max_retries},
        )

    supported = sum(1 for s in parsed if s.get("supported"))
    total = max(1, len(parsed))
    return MetricResult(
        metric_name=METRIC_FAITHFULNESS,
        value=supported / total,
        details={
            "n_sentences": len(parsed),
            "n_supported": supported,
            "retries": max_retries,
        },
    )


# ---------------------------------------------------------------------------
# Bundle: convenience runner
# ---------------------------------------------------------------------------


def compute_all_metrics(
    *,
    query: str,
    answer: str,
    contexts: list[str],
    retrieved_chunk_ids: list[str],
    expected_answer: str,
    expected_chunk_ids: list[str],
    llm_call: Callable[[str, str, float], str] | None = None,
    embedder: Callable[[list[str]], list[list[float]]] | None = None,
    faithfulness_timeout: float = 30.0,
) -> list[MetricResult]:
    """Run all 6 metrics on a single (case, answer) pair."""
    citations = sorted(_extract_chunk_citations(answer))
    return [
        faithfulness(
            answer=answer,
            contexts=contexts,
            llm_call=llm_call,
            timeout=faithfulness_timeout,
        ),
        answer_relevancy(
            query=query,
            answer=answer,
            embedder=embedder,
        ),
        context_precision(
            retrieved_chunk_ids=retrieved_chunk_ids,
            expected_chunk_ids=expected_chunk_ids,
        ),
        context_recall(
            retrieved_chunk_ids=retrieved_chunk_ids,
            expected_chunk_ids=expected_chunk_ids,
        ),
        answer_similarity(
            expected_answer=expected_answer,
            answer=answer,
            embedder=embedder,
        ),
        citation_accuracy(
            answer=answer,
            citations=citations,
            expected_chunk_ids=expected_chunk_ids,
        ),
    ]


__all__ = [
    "ALL_METRIC_NAMES",
    "METRIC_ANSWER_RELEVANCY",
    "METRIC_ANSWER_SIMILARITY",
    "METRIC_CITATION_ACCURACY",
    "METRIC_CONTEXT_PRECISION",
    "METRIC_CONTEXT_RECALL",
    "METRIC_FAITHFULNESS",
    "MetricResult",
    "answer_relevancy",
    "answer_similarity",
    "citation_accuracy",
    "compute_all_metrics",
    "context_precision",
    "context_recall",
    "cosine_sim",
    "faithfulness",
    "token_jaccard",
]
