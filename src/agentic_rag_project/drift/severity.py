"""Severity classification for the drift_detector (T4.4).

Pure functions — no DB, no clock. The detector imports `severity_for`
and routes the result through the Prometheus counter + the
`drift_alerts` table.

Direction matters: for "lower is better" metrics (faithfulness,
CSAT, citation_accuracy, recall, precision, relevance), a drop is a
regression. For "higher is worse" metrics (hallucination, future
latency_p95), a rise is a regression. Improvements are still
recorded (so on-call can ack false positives), but at `info`
severity so they don't page.
"""

from __future__ import annotations

# Metrics where a higher current value is WORSE than baseline.
# Anything not listed here is treated as "lower is worse" — drift
# direction is symmetric in the math; only the ladder flips.
HIGHER_IS_WORSE: frozenset[str] = frozenset({"hallucination", "latency_p95"})


def drift_pct(*, baseline: float, current: float) -> float:
    """Signed drift percent: `(current - baseline) / baseline`.

    Returns 0.0 when `baseline == 0` (no signal — caller should treat
    as cold-start and skip). Negative when current < baseline.
    """
    if baseline == 0:
        return 0.0
    return (current - baseline) / baseline


def is_regression(*, metric_name: str, baseline: float, current: float) -> bool:
    """True iff `current` is WORSE than `baseline` for `metric_name`.

    Improvements (the opposite direction) are recorded as `info` so
    humans can see them, but don't escalate.
    """
    if current == baseline:
        return False
    higher_is_worse = metric_name in HIGHER_IS_WORSE
    if higher_is_worse:
        return current > baseline
    return current < baseline


def severity_for(
    *, metric_name: str, baseline: float, current: float
) -> tuple[str, float]:
    """Return `(severity, drift_pct)` per CONSENSUS_drift_detector §5.

    Severity ladder (regression only — improvements fall to `info`):

      |drift_pct| > 0.30  → "critical"
      |drift_pct| > 0.15  → "warning"
      otherwise           → "info"
    """
    pct = drift_pct(baseline=baseline, current=current)
    if not is_regression(
        metric_name=metric_name, baseline=baseline, current=current
    ):
        return "info", pct
    abs_pct = abs(pct)
    if abs_pct > 0.30:
        return "critical", pct
    if abs_pct > 0.15:
        return "warning", pct
    return "info", pct


__all__ = [
    "HIGHER_IS_WORSE",
    "drift_pct",
    "is_regression",
    "severity_for",
]
