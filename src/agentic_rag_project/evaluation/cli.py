"""CLI entry point for offline evaluation (T4.1).

Run via:

    python -m agentic_rag_project.evaluation.cli \
        --eval-set eval.jsonl \
        --output report.json

Defaults:
  * Reads `OPENAI_API_KEY` / `LITELLM_*` from the environment via
    `agentic_rag_project.config.get_settings()` for the chat service.
  * The `--user-id` and `--workspace-id` flags are mandatory in this
    build — there is no admin impersonation yet (T5.x).
  * `--llm-faithfulness` may be passed to enable LLM-backed
    faithfulness scoring; without it the metric returns 0.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from agentic_rag_project.config import get_settings
from agentic_rag_project.db.session import get_engine
from agentic_rag_project.evaluation.pipeline import EvaluationPipeline
from agentic_rag_project.evaluation.scorer import EvalScorer

logger = logging.getLogger(__name__)


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic_rag_project.evaluation",
        description="Run an offline RAG evaluation batch.",
    )
    parser.add_argument(
        "--eval-set",
        required=True,
        help="Path to eval set (.jsonl or .json).",
    )
    parser.add_argument(
        "--output",
        default="report.json",
        help="Where to write the EvaluationReport (JSON).",
    )
    parser.add_argument(
        "--user-id",
        required=True,
        help="UUID of the user impersonated for the eval run.",
    )
    parser.add_argument(
        "--workspace-id",
        required=True,
        help="UUID of the workspace the eval runs in.",
    )
    parser.add_argument(
        "--llm-faithfulness",
        action="store_true",
        help="Enable LLM-backed faithfulness scoring (requires LITELLM_API_KEY).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def _build_pipeline(
    *, user_id: uuid.UUID, workspace_id: uuid.UUID, with_llm: bool
) -> EvaluationPipeline:
    """Build the pipeline. Real chat-service wiring lands in T5; here we
    only set up the lightweight dependencies that don't need a database
    connection yet (we still need a session factory for persistence).
    """
    # Lazy imports keep `--help` fast and avoid pulling every module
    # when the user only wanted to inspect the parser.
    from agentic_rag_project.chat import ChatService
    from agentic_rag_project.post_processor.filter import SensitiveWordFilter
    from agentic_rag_project.post_processor.mask import Masker
    from agentic_rag_project.retrieval_direct.memory import ConversationMemory
    from agentic_rag_project.router.classifier import ConfidenceRouter

    # The CLI runs against a real Postgres + Redis stack. For a dry
    # smoke test we accept `DRY_RUN=1` and skip persistence.
    dry_run = os.environ.get("EVAL_DRY_RUN") == "1"

    if dry_run:
        # Use null collaborators — the chat service will fail on the
        # first real case (no searcher wired), which is fine for a
        # parse/CLI smoke test.
        def _session_factory() -> Session:
            raise RuntimeError("EVAL_DRY_RUN: persistence disabled")

        from agentic_rag_project.agent_core.runner import AgentRunner
        from agentic_rag_project.retrieval_direct.long_context_fallback import (
            LongContextFallback,
        )
        from agentic_rag_project.retrieval_direct.two_stage import TwoStageSearcher

        class _NullSearcher:
            def hybrid_search(self, query, *, top_k, score_threshold=None, qdrant_filter=None):
                return []

        null_searcher = _NullSearcher()
        chat_service = ChatService(
            searcher=null_searcher,  # type: ignore[arg-type]
            two_stage=TwoStageSearcher(null_searcher),  # type: ignore[arg-type]
            router=ConfidenceRouter(
                llm_classifier=None,  # type: ignore[arg-type]
                keyword_classifier=None,  # type: ignore[arg-type]
            ),
            agent_runner=AgentRunner(
                searcher=null_searcher,  # type: ignore[arg-type]
                llm_call=None,  # type: ignore[arg-type]
            ),
            long_context=LongContextFallback(llm_call=lambda s, u, t: ""),  # type: ignore[arg-type]
            memory=ConversationMemory(redis_client=None),  # type: ignore[arg-type]
            sensitive_filter=SensitiveWordFilter(),
            masker=Masker(),
            direct_synthesizer=lambda *, system_prompt, user_prompt, timeout: "",  # type: ignore[arg-type]
        )
    else:
        raise RuntimeError(
            "non-dry-run CLI not implemented; set EVAL_DRY_RUN=1 or wire "
            "the real chat service in T5.x"
        )

    scorer = EvalScorer(llm_call=None if with_llm else None)
    return EvaluationPipeline(
        chat_service=chat_service,
        scorer=scorer,
        session_factory=_session_factory,
        default_user_id=user_id,
        default_workspace_id=workspace_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level))

    try:
        user_id = uuid.UUID(args.user_id)
        workspace_id = uuid.UUID(args.workspace_id)
    except ValueError as exc:
        print(f"invalid UUID: {exc}", file=sys.stderr)
        return 2

    pipeline = _build_pipeline(
        user_id=user_id,
        workspace_id=workspace_id,
        with_llm=args.llm_faithfulness,
    )

    def _progress(idx: int, total: int, case_id: str) -> None:
        print(f"[{idx}/{total}] {case_id}", file=sys.stderr, flush=True)

    try:
        report = pipeline.run_from_path(args.eval_set, on_progress=_progress)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    print(
        f"wrote report: cases={report.total_cases} "
        f"succeeded={report.succeeded} failed={report.failed}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
