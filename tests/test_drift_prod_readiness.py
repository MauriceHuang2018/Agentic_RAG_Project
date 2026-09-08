"""Drift prod-readiness smoke tests (M4.3.3).

Decision E minimum (4 类核心): two structural tests that confirm the
prod-readiness scaffolding for the drift detector is wired correctly.
These are *not* end-to-end tests — they verify the moving parts are
in place so the live `Prometheus + Celery beat + /metrics + runbook`
chain can be exercised by ops.

Test 1 — `infra/prometheus/alerts.yaml` parses as YAML, contains the
two drift alert rules, and every rule's `runbook_url` annotation is
non-empty (catches the placeholder `https://example.com/...` typo).

Test 2 — `celery_app.conf.beat_schedule` includes `drift-detect`
pointing at the registered task name
`agentic_rag_project.drift.detect_drift`, with a 15-minute schedule.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
ALERTS_PATH = REPO_ROOT / "infra" / "prometheus" / "alerts.yaml"


def _load_alerts() -> dict:
    """Read and parse the Prometheus alerts file (UTF-8)."""
    assert ALERTS_PATH.exists(), (
        f"alerts.yaml not found at {ALERTS_PATH}; "
        "M4.3.3 prod-readiness expects it under infra/prometheus/."
    )
    with ALERTS_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_alerts_yaml_has_drift_rules_with_runbook_urls():
    """Both drift alert rules MUST exist with non-empty `runbook_url`.

    This catches two failure modes before they reach prod:
      - The file was edited but the drift rule accidentally removed.
      - A rule exists but `runbook_url` was left as the placeholder
        `https://example.com/...` (the most common mistake when
        copying alert templates).
    """
    cfg = _load_alerts()
    groups = cfg.get("groups", [])
    assert groups, "alerts.yaml has no `groups` key"

    drift_rules = [
        r
        for g in groups
        for r in g.get("rules", [])
        if r.get("labels", {}).get("runbook") == "rag/drift"
    ]
    assert len(drift_rules) >= 1, (
        "no alert rule has runbook=='rag/drift' — drift alerts missing"
    )

    # Every drift rule needs a real runbook_url annotation.
    for rule in drift_rules:
        url = rule.get("annotations", {}).get("runbook_url", "")
        assert url and url.startswith("http"), (
            f"alert {rule.get('alert')!r} has invalid runbook_url: {url!r}"
        )
        # Specifically catch the canonical placeholder.
        assert "example.com" not in url, (
            f"alert {rule.get('alert')!r} runbook_url still uses "
            f"placeholder example.com: {url!r}"
        )


def test_drift_detect_beat_entry_registered():
    """`drift-detect` MUST appear in `beat_schedule` with the right cadence.

    This is a structural assertion so a typo in the task name (e.g.
    `agentic_rag_project.drift.detect-drift`) is caught at test
    time rather than silently by the worker.
    """
    from agentic_rag_project.doc_processor.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "drift-detect" in schedule, (
        f"drift-detect missing from beat_schedule; "
        f"got keys: {sorted(schedule)}"
    )
    entry = schedule["drift-detect"]
    assert entry["task"] == "agentic_rag_project.drift.detect_drift", (
        f"drift-detect task name mismatch: {entry['task']!r}"
    )
    assert entry["schedule"] == 900.0, (
        f"drift-detect schedule must be 15 min (900 s), "
        f"got {entry['schedule']!r}"
    )


def test_metrics_endpoint_is_registered():
    """`/metrics` MUST be a route on the FastAPI app for Prometheus to scrape.

    The `metrics_router` is a plain `APIRouter` built by
    `build_metrics_router`; we iterate its `.routes` directly to
    verify `path='/metrics'` and `methods={'GET'}`.
    """
    from fastapi.routing import APIRoute

    from agentic_rag_project.api_gateway import metrics_router

    api_routes = [r for r in metrics_router.routes if isinstance(r, APIRoute)]
    paths = {r.path for r in api_routes}
    assert "/metrics" in paths, (
        f"/metrics missing from metrics_router routes: {sorted(paths)}"
    )
    metrics_route = next(r for r in api_routes if r.path == "/metrics")
    assert "GET" in metrics_route.methods, (
        f"/metrics route missing GET method: {metrics_route.methods}"
    )
