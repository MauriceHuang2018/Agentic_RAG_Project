"""Evaluation-set loader (T4.1).

DESIGN 4.5 / TASK T4.1: offline evaluation runs a curated set of
`(query, expected_answer, expected_chunk_ids)` cases through the
chat pipeline and scores the output. This module owns the on-disk
format — both JSON-Lines and a single JSON array are accepted so
ops can author the eval set in whichever editor they prefer.

Format (JSONL):
  {"case_id": "q-001", "query": "...", "expected_answer": "...",
   "expected_chunk_ids": ["c-1"], "workspace_id": "...",
   "tags": ["multi-hop"], "metadata": {}}

Format (JSON): `[ {case}, {case}, ... ]`

Missing optional fields default to `None` / `[]` / `{}`; only `query`
is strictly required (a case with no expected answer still drives
faithfulness / context_* metrics, which are reference-free).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


@dataclass
class EvalCase:
    """One row in an evaluation set.

    The 6 evaluation metrics split into two groups:

      * Reference-based (need `expected_answer` / `expected_chunk_ids`):
        - answer_similarity, context_precision, context_recall,
          citation_accuracy
      * Reference-free:
        - faithfulness, answer_relevancy
    """

    query: str
    case_id: str = field(default_factory=lambda: f"c-{uuid.uuid4().hex[:8]}")
    expected_answer: str = ""
    expected_chunk_ids: list[str] = field(default_factory=list)
    workspace_id: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.query or not self.query.strip():
            raise ValueError("EvalCase.query must be a non-empty string")
        # Normalize empties so downstream code can rely on concrete types.
        if self.expected_chunk_ids is None:
            self.expected_chunk_ids = []
        if self.tags is None:
            self.tags = []
        if self.metadata is None:
            self.metadata = {}


def _coerce_case(raw: dict[str, Any]) -> EvalCase:
    """Translate a raw dict (from json) into an EvalCase, with defaults."""
    if not isinstance(raw, dict):
        raise ValueError(f"eval case must be a JSON object, got {type(raw)}")
    if "query" not in raw:
        raise ValueError(f"eval case missing required 'query' field: {raw}")
    return EvalCase(
        query=str(raw["query"]).strip(),
        case_id=str(raw.get("case_id") or f"c-{uuid.uuid4().hex[:8]}"),
        expected_answer=str(raw.get("expected_answer") or ""),
        expected_chunk_ids=list(raw.get("expected_chunk_ids") or []),
        workspace_id=(
            str(raw["workspace_id"])
            if raw.get("workspace_id") is not None
            else None
        ),
        tags=list(raw.get("tags") or []),
        metadata=dict(raw.get("metadata") or {}),
    )


def load_eval_set(path: str | Path) -> list[EvalCase]:
    """Load an evaluation set from disk.

    Detects the format by file extension:
      * `.jsonl` → one JSON object per line
      * `.json`  → single array of objects
      * anything else → sniff the first non-whitespace char

    Empty lines and lines starting with `#` are skipped (JSONL only).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"eval set not found: {path}")
    text = path.read_text(encoding="utf-8")

    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        cases = _parse_jsonl(text)
    elif suffix == ".json":
        cases = _parse_json_array(text)
    else:
        stripped = text.lstrip()
        if stripped.startswith("["):
            cases = _parse_json_array(text)
        else:
            cases = _parse_jsonl(text)

    if not cases:
        logger.warning("eval set %s loaded 0 cases", path)
    return cases


def _parse_jsonl(text: str) -> list[EvalCase]:
    out: list[EvalCase] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid JSONL at line {lineno}: {exc}"
            ) from exc
        out.append(_coerce_case(raw))
    return out


def _parse_json_array(text: str) -> list[EvalCase]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON array: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("JSON eval set must be an array of objects")
    return [_coerce_case(item) for item in data]


def dump_eval_set(cases: Iterable[EvalCase], path: str | Path) -> None:
    """Write `cases` as JSON Lines (one object per line)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(
                json.dumps(
                    {
                        "case_id": c.case_id,
                        "query": c.query,
                        "expected_answer": c.expected_answer,
                        "expected_chunk_ids": list(c.expected_chunk_ids),
                        "workspace_id": c.workspace_id,
                        "tags": list(c.tags),
                        "metadata": dict(c.metadata),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


__all__ = [
    "EvalCase",
    "dump_eval_set",
    "load_eval_set",
]
