"""Boundary-cutoff heuristic for the chunking category (T4.2).

The idea: if the assistant's answer ends mid-thought (truncated
sentence, dangling connector, or a tail-token that appears at the
start of any retrieved chunk) it's likely that the supporting
information was split across chunks.

This is a deliberately cheap heuristic. The LLM-side alternative
would be to ask an LLM "does the answer look truncated?", but
that wastes a generation per feedback row. The patterns below
catch the common cases; misses are still caught by the
`generation` default fallback in `AutoAttributor`.

Algorithm:
  1. Tokenise the answer's tail (last N chars).
  2. Look for cutoff markers ("...", "…", trailing conjunction).
  3. Search retrieved chunks for a continuation pattern.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CutoffResult:
    """Heuristic verdict for one feedback row."""

    matched: bool
    reason: str = ""


# Truncation / continuation markers.
_ELLIPSIS = ("...", "…")
# Connectors that strongly suggest the sentence continues.
_CONTINUATIONS = ("但是", "而且", "并且", "以及", "或者", "but", "and", "or", "so")


def has_boundary_cutoff(
    answer: str,
    retrieved_chunks: list[str],
    *,
    tail_chars: int = 40,
) -> CutoffResult:
    """Return `matched=True` if the answer looks truncated at a chunk boundary.

    `retrieved_chunks` may be the chunk `content` strings, or any
    list of strings — we don't introspect metadata. We deliberately
    ignore `chunk_id` because the issue is in the *text*, not the
    retrieval ranking.
    """
    if not answer:
        return CutoffResult(matched=False, reason="empty_answer")

    tail = answer.strip()[-tail_chars:]

    # (1) Ellipsis at the end.
    for marker in _ELLIPSIS:
        if tail.endswith(marker):
            return CutoffResult(matched=True, reason="ellipsis_tail")

    # (2) Trailing conjunction with no terminator.
    last_char = tail[-1]
    if last_char.isalpha() or "一" <= last_char <= "鿿":
        for connector in _CONTINUATIONS:
            if tail.lower().endswith(connector.lower()):
                return CutoffResult(matched=True, reason="dangling_connector")

    # (3) A chunk starts with what looks like a continuation of the
    # answer tail. We require a 3+ char shared prefix to avoid
    # false positives where the chunk merely restates the leading
    # noun of the answer (e.g. "年假"). The next char after the
    # token must be alphanumeric or CJK — punctuation / whitespace
    # means the chunk has terminated the phrase and is *not* a
    # continuation.
    if retrieved_chunks:
        tail_tokens = _tail_tokens(answer, n=4)
        for chunk in retrieved_chunks:
            if not chunk:
                continue
            chunk_head = chunk.lstrip()
            for token in tail_tokens:
                if not token or len(token) < 3:
                    continue
                if not chunk_head.startswith(token):
                    continue
                if len(chunk_head) <= len(token):
                    continue
                nxt = chunk_head[len(token)]
                if nxt.isalnum() or "一" <= nxt <= "鿿":
                    return CutoffResult(
                        matched=True, reason="chunk_starts_with_tail_token"
                    )
    return CutoffResult(matched=False)


def _tail_tokens(answer: str, *, n: int) -> list[str]:
    """Last `n` candidate continuation tokens from the answer tail.

    Splits the answer in three passes:
      1. Whitespace-separated tokens (3+ chars).
      2. 3-char sliding windows over CJK runs so we can detect
         boundaries mid-token.
      3. The literal last 3 chars of the answer — guarantees the
         absolute tail appears even when whitespace-tokenisation
         drops it (e.g. "年假是" from "公司政策规定年假是").
    """
    out: list[str] = []
    for tok in answer.replace("\n", " ").split():
        if len(tok) >= 3:
            out.append(tok)
        for i in range(0, len(tok) - 2):
            triple = tok[i : i + 3]
            if all("一" <= ch <= "鿿" for ch in triple):
                out.append(triple)
    if len(answer) >= 3:
        out.append(answer[-3:])
    return out[-n:]


__all__ = [
    "CutoffResult",
    "has_boundary_cutoff",
]
