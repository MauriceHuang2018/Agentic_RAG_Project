"""Offline evaluation pipeline (T4.1).

Public surface:
  * `EvalCase` / `load_eval_set` / `dump_eval_set` — on-disk format
  * `EvalScorer` / `ScoredCase` — runs the 6 metrics per case
  * `EvaluationPipeline` / `EvaluationReport` — runs an entire set
  * `record_evaluation_results` — writes per-metric rows to PG
  * `cli.main` — `python -m agentic_rag_project.evaluation.cli ...`
"""

from agentic_rag_project.evaluation.eval_set import (
    EvalCase,
    dump_eval_set,
    load_eval_set,
)
from agentic_rag_project.evaluation.metrics import (
    ALL_METRIC_NAMES,
    METRIC_ANSWER_RELEVANCY,
    METRIC_ANSWER_SIMILARITY,
    METRIC_CITATION_ACCURACY,
    METRIC_CONTEXT_PRECISION,
    METRIC_CONTEXT_RECALL,
    METRIC_FAITHFULNESS,
    MetricResult,
    answer_relevancy,
    answer_similarity,
    citation_accuracy,
    compute_all_metrics,
    context_precision,
    context_recall,
    cosine_sim,
    faithfulness,
    token_jaccard,
)
from agentic_rag_project.evaluation.persistence import record_evaluation_results
from agentic_rag_project.evaluation.pipeline import (
    CaseOutcome,
    EvaluationPipeline,
    EvaluationReport,
    PerMetricStats,
    aggregate_by_metric,
)
from agentic_rag_project.evaluation.scorer import EvalScorer, ScoredCase

__all__ = [
    "ALL_METRIC_NAMES",
    "CaseOutcome",
    "EvalCase",
    "EvalScorer",
    "EvaluationPipeline",
    "EvaluationReport",
    "METRIC_ANSWER_RELEVANCY",
    "METRIC_ANSWER_SIMILARITY",
    "METRIC_CITATION_ACCURACY",
    "METRIC_CONTEXT_PRECISION",
    "METRIC_CONTEXT_RECALL",
    "METRIC_FAITHFULNESS",
    "MetricResult",
    "PerMetricStats",
    "ScoredCase",
    "aggregate_by_metric",
    "answer_relevancy",
    "answer_similarity",
    "citation_accuracy",
    "compute_all_metrics",
    "context_precision",
    "context_recall",
    "cosine_sim",
    "dump_eval_set",
    "faithfulness",
    "load_eval_set",
    "record_evaluation_results",
    "token_jaccard",
]
