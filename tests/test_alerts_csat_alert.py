"""M4.4-specific test for the CSATSuddenDrop Prometheus alert.

Adds the `rag_csat` group + `CSATSuddenDrop` rule to
`infra/prometheus/alerts.yaml`. This test guards against:

  * YAML parse failures introduced by the new group
    (UTF-8 is required — see MEMORY dev-env gotchas).
  * The `CSATSuddenDrop` rule existing in the right group.
  * The rule's expr using `avg_over_time` rather than bare `[1h]`
    so Prom has enough resolution for the drop calculation.
  * The rule having a `runbook_url` annotation (matches the
    convention set by every other alert in the file).

The existing `test_alert_rules.py` covers the cross-cutting
structural rules (severity in {info,warning,critical}, expr
references a declared metric, label matchers use declared
labels, every watched metric has a rule). This file is the
M4.4-specific smoke test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


_INFRA_DIR = Path(__file__).resolve().parent.parent / "infra" / "prometheus"
_ALERTS_PATH = _INFRA_DIR / "alerts.yaml"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def alerts_doc() -> dict:
    """Parse `alerts.yaml` (UTF-8) — module scope so we only read once."""
    if not _ALERTS_PATH.exists():
        pytest.skip(f"alerts.yaml not found at {_ALERTS_PATH}")
    # UTF-8 explicit — the file uses box-drawing chars + runbook URLs
    # that confuse the GBK default on Windows shells (see MEMORY).
    with _ALERTS_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# 1. CSATSuddenDrop rule shape
# ---------------------------------------------------------------------------


def test_csat_sudden_drop_rule_exists_in_rag_csat_group(alerts_doc: dict) -> None:
    """The `rag_csat` group must contain a `CSATSuddenDrop` rule."""
    groups = alerts_doc.get("groups", [])
    rag_csat_group = next(
        (g for g in groups if g.get("name") == "rag_csat"),
        None,
    )
    assert rag_csat_group is not None, "no `rag_csat` group in alerts.yaml"

    rules = rag_csat_group.get("rules", [])
    csat_drop_rule = next(
        (r for r in rules if r.get("alert") == "CSATSuddenDrop"),
        None,
    )
    assert csat_drop_rule is not None, (
        "CSATSuddenDrop rule missing from rag_csat group"
    )

    # The expr must use `avg_over_time` — bare `[1h]` would only
    # resolve at scrape granularity (≤30s) and is too noisy for
    # a 10%-drop threshold. We assert the function name appears.
    expr = csat_drop_rule.get("expr", "")
    assert "avg_over_time" in expr, (
        f"CSATSuddenDrop expr should use avg_over_time; got {expr!r}"
    )
    # And the threshold must be >0 with a debounce window.
    assert ">" in expr, "CSATSuddenDrop expr missing the threshold"
    assert csat_drop_rule.get("for"), "CSATSuddenDrop missing `for` debounce"

    # Convention parity with every other rule in the file.
    annotations = csat_drop_rule.get("annotations", {})
    assert "runbook_url" in annotations, (
        "CSATSuddenDrop missing runbook_url annotation"
    )
    assert annotations["runbook_url"].startswith("https://"), (
        f"runbook_url must be absolute: {annotations['runbook_url']!r}"
    )

    labels = csat_drop_rule.get("labels", {})
    assert labels.get("severity"), "CSATSuddenDrop missing severity label"
    assert labels.get("runbook"), "CSATSuddenDrop missing runbook label"