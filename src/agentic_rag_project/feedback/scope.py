"""Ambiguity / out-of-scope detection (T4.2).

Used as the FIRST rule in `AutoAttributor.cascade()`. These are
simple, deterministic heuristics — not LLM calls — because:

  * The decision needs to be cheap to run on every feedback row.
  * A deterministic rule is auditable (test_cases can pin exact
    behaviour for ambiguous Chinese prompts).
  * Out-of-scope detection only works if the KB topics are
    known; when the topic list is empty we report "unknown"
    rather than guess.

Both functions return a `HeuristicResult` so the attributor can
carry a `matched_rule` label back to the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HeuristicResult:
    """One heuristic's verdict — `matched=True` iff the rule fired."""

    matched: bool
    reason: str = ""


# Heuristic 1: ambiguity.
#
# Trigger conditions:
#  1. The query is too short (< 4 chars after stripping).
#  2. The query has no whitespace AND is short-ish (≤ 8 chars) — a
#     single-word query that doesn't look like a noun phrase.
#  3. The query ends in an ellipsis or has trailing whitespace
#     markers ("etc", "等", "之类") suggesting it's unfinished.
#
# These are deliberately conservative — false positives in this
# rule would attribute genuine retrieval failures to user error,
# which is the wrong direction.


def is_ambiguous(query: str) -> HeuristicResult:
    q = (query or "").strip()
    if not q:
        return HeuristicResult(matched=True, reason="empty_query")

    if len(q) < 4:
        return HeuristicResult(
            matched=True, reason=f"too_short:{len(q)}_chars"
        )

    # Single-short-word rule applies only to ASCII (or latin-script)
    # tokens — Chinese / Japanese / Korean phrases typically have no
    # whitespace and a 6-8 char run is still a meaningful noun phrase.
    has_cjk = any("一" <= ch <= "鿿" for ch in q)
    if not has_cjk and " " not in q and len(q) <= 8:
        return HeuristicResult(matched=True, reason="single_short_word")

    if q.endswith(("...", "。。", "等", "之类", "etc")):
        return HeuristicResult(matched=True, reason="trailing_vague_marker")

    return HeuristicResult(matched=False)


# Heuristic 2: out-of-scope.
#
# We compare the query (lowercased, no whitespace) against a list
# of KB topic substrings. Empty topic list → `matched=False` with
# `reason="no_topics_configured"` so the dashboard can show
# "scope-check disabled" rather than silently skipping.
#
# The matching is intentionally loose: any topic substring found
# in the query counts as in-scope. When the KB grows we'll swap
# this for an embedder-based cosine check (T5+).


def is_out_of_scope(query: str, kb_topics: list[str]) -> HeuristicResult:
    if not kb_topics:
        return HeuristicResult(matched=False, reason="no_topics_configured")
    q = (query or "").strip().lower().replace(" ", "")
    if not q:
        return HeuristicResult(matched=True, reason="empty_query")
    for topic in kb_topics:
        topic_norm = topic.strip().lower().replace(" ", "")
        if topic_norm and topic_norm in q:
            return HeuristicResult(matched=False)
    return HeuristicResult(matched=True, reason="no_topic_match")


__all__ = [
    "HeuristicResult",
    "is_ambiguous",
    "is_out_of_scope",
]
