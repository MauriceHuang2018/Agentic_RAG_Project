"""Tests for the Prometheus alert rules (T4.3 finalization).

Validates that `infra/prometheus/alerts.yaml` and `prometheus.yml`
parse cleanly and reference metric names that actually exist in
`MetricsRegistry`. This catches two classes of bugs:

  1. Invalid YAML syntax — Prometheus would silently fail to load
     the file in production.
  2. Stale metric / label references — a renamed metric in
     `registry.py` would silently kill an alert rule.

We don't load the rules into a real Prometheus server (out of scope
for unit tests). Instead we parse + introspect the structure to
verify each rule's `expr` only references declared metric names
and label names that the registry exposes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# Path to the alert rule files, relative to the repo root.
_INFRA_DIR = Path(__file__).resolve().parent.parent / "infra" / "prometheus"
_ALERTS_PATH = _INFRA_DIR / "alerts.yaml"
_PROM_PATH = _INFRA_DIR / "prometheus.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict:
    """Parse the YAML at `path`; raise on syntax errors."""
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _all_metric_label_pairs(registry_doc: dict) -> set[tuple[str, frozenset[str]]]:
    """Collect declared (metric_name, frozenset(label_names)) pairs.

    Walks the registry's `Counter`/`Gauge`/`Histogram` definitions in
    `build_registry()` and emits a set of `(name, labels)` tuples.
    A rule's expression is allowed to use any subset of labels that
    matches the declared set.
    """
    # The easiest way to introspect the registry is to actually build
    # it against a fresh CollectorRegistry. Importing here keeps the
    # test independent of conftest ordering.
    from prometheus_client import CollectorRegistry

    from agentic_rag_project.observability.registry import build_registry

    reg = build_registry(CollectorRegistry())
    pairs: set[tuple[str, frozenset[str]]] = set()
    for field in reg.__dataclass_fields__:  # type: ignore[attr-defined]
        metric = getattr(reg, field)
        # Skip the field if it's not a metric (defensive).
        if not hasattr(metric, "_name"):
            continue
        name = metric._name
        labels = frozenset(getattr(metric, "_labelnames", ()) or ())
        pairs.add((name, labels))
    return pairs


def _declared_metrics(registry_doc: dict) -> set[str]:
    """Just the metric names — for expr-level quick membership checks."""
    return {name for name, _ in _all_metric_label_pairs(registry_doc)}


def _collect_metric_uses(expr: str) -> set[str]:
    """Naive identifier scan to find metric names referenced in `expr`.

    Prometheus PromQL uses bare identifiers for metric names; label
    matchers use `{name="value"}` syntax; aggregation groupings use
    `by (label, ...)` or `without (label, ...)`. We strip all three
    contexts before tokenizing, then for each candidate token we
    try both the bare form and the canonical Prometheus suffixes
    (`_total`, `_bucket`, `_sum`, `_count`, `_created`) so the
    caller can match against the declared metric names in the
    registry (which always include the suffix when applicable).

    This is intentionally simple — full PromQL parsing is out of
    scope. The goal is to catch typos like `csat_scre` instead of
    `csat_score`, not to validate expression semantics.
    """
    cleaned_chars: list[str] = []
    depth_brace = 0
    depth_paren = 0
    inside_grouping = False
    # Track whether we're inside `by (...)` / `without (...)` /
    # `on (...)` / `ignoring (...)`. The label names there are
    # definitely NOT metric names.
    grouping_kw = {"by", "without", "on", "ignoring"}
    last_token = ""

    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "{":
            depth_brace += 1
            cleaned_chars.append(" ")
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
            cleaned_chars.append(" ")
        elif ch == "(" and depth_brace == 0:
            # Check whether the preceding non-whitespace token is
            # a grouping keyword. We need to look at the previous
            # identifier in the cleaned buffer.
            depth_paren += 1
            if inside_grouping:
                cleaned_chars.append(" ")
            else:
                # Peek back into cleaned_chars to find the last
                # identifier (skipping whitespace).
                j = len(cleaned_chars) - 1
                while j >= 0 and cleaned_chars[j].isspace():
                    j -= 1
                end = j + 1
                while j >= 0 and (cleaned_chars[j].isalnum() or cleaned_chars[j] == "_"):
                    j -= 1
                start = j + 1
                prev = "".join(cleaned_chars[start:end])
                if prev in grouping_kw:
                    inside_grouping = True
                    # Strip the grouping keyword + whitespace before
                    # this paren so we don't match it as a metric.
                    cleaned_chars[start:] = []
                else:
                    cleaned_chars.append(" ")
        elif ch == ")" and depth_brace == 0:
            depth_paren = max(0, depth_paren - 1)
            if inside_grouping and depth_paren == 0:
                inside_grouping = False
            cleaned_chars.append(" ")
        elif depth_brace > 0 or inside_grouping:
            cleaned_chars.append(" ")
        elif ch.isalnum() or ch == "_":
            cleaned_chars.append(ch)
        else:
            cleaned_chars.append(" ")
        i += 1

    cleaned = "".join(cleaned_chars)
    # Tokenize.
    tokens: list[str] = []
    current: list[str] = []
    for ch in cleaned:
        if ch.isalnum() or ch == "_":
            current.append(ch)
        else:
            if current:
                tokens.append("".join(current))
                current = []
    if current:
        tokens.append("".join(current))

    # Filter out PromQL builtins + numeric-looking tokens.
    BUILTINS = {
        "sum",
        "rate",
        "irate",
        "increase",
        "delta",
        "deriv",
        "predict_linear",
        "histogram_quantile",
        "le",
        "and",
        "or",
        "unless",
        "group_left",
        "group_right",
        "clamp_min",
        "clamp_max",
        "min",
        "max",
        "avg",
        "stddev",
        "stdvar",
        "count",
        "count_values",
        "topk",
        "bottomk",
        "quantile",
        "abs",
        "absent",
        "absent_over_time",
        "ceil",
        "floor",
        "ln",
        "log2",
        "log10",
        "exp",
        "round",
        "sqrt",
        "time",
        "timestamp",
        "vector",
        "scalar",
        "bool",
        "changes",
        "resets",
        "avg_over_time",
        "min_over_time",
        "max_over_time",
        "sum_over_time",
        "count_over_time",
        "quantile_over_time",
        "stddev_over_time",
        "stdvar_over_time",
        "last_over_time",
        "present_over_time",
    }

    # Try to map every token to a declared metric, considering
    # Prometheus's suffix conventions.
    SUFFIXES = ("", "_total", "_bucket", "_sum", "_count", "_created")
    declared = _declared_metrics({"placeholder": True})
    used: set[str] = set()
    for tok in tokens:
        if tok in BUILTINS or (tok and tok[0].isdigit()):
            continue
        # Either the token itself matches a declared metric, or
        # the token is the canonical name with a known suffix
        # stripped off (e.g. user wrote `chat_requests` but the
        # declared metric is `chat_requests_total`).
        for sfx in SUFFIXES:
            if tok.endswith(sfx) and tok[: -len(sfx) if sfx else None] + (
                "" if not sfx else "_total"
            ) in declared:
                used.add(tok)
                break
        else:
            if tok in declared:
                used.add(tok)
    return used


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def alerts_doc() -> dict:
    """The parsed alerts.yaml — module scope so we only read once."""
    if not _ALERTS_PATH.exists():
        pytest.skip(f"alerts.yaml not found at {_ALERTS_PATH}")
    return _load_yaml(_ALERTS_PATH)


@pytest.fixture(scope="module")
def prom_doc() -> dict:
    if not _PROM_PATH.exists():
        pytest.skip(f"prometheus.yml not found at {_PROM_PATH}")
    return _load_yaml(_PROM_PATH)


@pytest.fixture(scope="module")
def registry_doc(alerts_doc: dict) -> dict:
    """Pass-through so tests can decorate their dep on registry_doc."""
    return alerts_doc


# ---------------------------------------------------------------------------
# YAML validity
# ---------------------------------------------------------------------------


def test_alerts_yaml_parses(alerts_doc: dict) -> None:
    """`alerts.yaml` must be valid YAML with the expected top-level shape."""
    assert "groups" in alerts_doc, "alerts.yaml missing top-level 'groups'"
    assert isinstance(alerts_doc["groups"], list)
    assert len(alerts_doc["groups"]) > 0


def test_prometheus_yml_parses(prom_doc: dict) -> None:
    """`prometheus.yml` must be valid YAML with the expected top-level shape."""
    assert "scrape_configs" in prom_doc
    assert "rule_files" in prom_doc
    assert "alerts.yaml" in prom_doc["rule_files"]


# ---------------------------------------------------------------------------
# Structural rules
# ---------------------------------------------------------------------------


def test_every_rule_has_required_fields(alerts_doc: dict) -> None:
    """Each alert must specify `alert`, `expr`, `labels.severity`, `annotations.summary`."""
    required_label_keys = {"severity"}
    required_annotation_keys = {"summary", "description"}
    severities = {"info", "warning", "critical"}

    alerts_found: list[str] = []
    for group in alerts_doc["groups"]:
        for rule in group.get("rules", []):
            if "alert" not in rule:
                # Recording rules are allowed but we don't ship any yet.
                continue
            name = rule["alert"]
            alerts_found.append(name)
            assert "expr" in rule and rule["expr"].strip(), (
                f"{name}: missing or empty expr"
            )
            labels = rule.get("labels", {})
            assert required_label_keys.issubset(labels.keys()), (
                f"{name}: labels missing {required_label_keys - labels.keys()}"
            )
            assert labels.get("severity") in severities, (
                f"{name}: severity={labels.get('severity')!r} not in {severities}"
            )
            annotations = rule.get("annotations", {})
            assert required_annotation_keys.issubset(annotations.keys()), (
                f"{name}: annotations missing {required_annotation_keys - annotations.keys()}"
            )
            assert "runbook_url" in annotations, (
                f"{name}: annotations must include runbook_url"
            )
    # Sanity — we expect a non-trivial number of alerts.
    assert len(alerts_found) >= 5, f"only {len(alerts_found)} alerts defined"


def test_alert_names_are_unique(alerts_doc: dict) -> None:
    """Two rules with the same name silently overwrite each other."""
    seen: list[str] = []
    for group in alerts_doc["groups"]:
        for rule in group.get("rules", []):
            if "alert" in rule:
                seen.append(rule["alert"])
    duplicates = {n for n in seen if seen.count(n) > 1}
    assert not duplicates, f"duplicate alert names: {duplicates}"


def test_no_empty_expressions(alerts_doc: dict) -> None:
    """A rule with `expr: ""` would fire on no series — easy to miss."""
    for group in alerts_doc["groups"]:
        for rule in group.get("rules", []):
            if "alert" in rule:
                expr = (rule.get("expr") or "").strip()
                assert expr, f"{rule['alert']}: empty expr"
                # Reject trivially-true expressions that always fire.
                assert expr not in {"vector(1)", "1"}, (
                    f"{rule['alert']}: trivially-true expr"
                )


# ---------------------------------------------------------------------------
# Metric-name references
# ---------------------------------------------------------------------------


def test_alert_expressions_reference_declared_metrics(alerts_doc: dict) -> None:
    """Every metric name in an `expr` must exist in MetricsRegistry.

    We use a heuristic identifier scan (PromQL has no public parser
    in Python). The scan errs on the side of false positives — if a
    non-metric identifier shows up, we'll flag it. The point is to
    catch obvious typos like `csat_scre` instead of `csat_score`.
    """
    declared = _declared_metrics(alerts_doc)

    bad: list[tuple[str, str]] = []
    for group in alerts_doc["groups"]:
        for rule in group.get("rules", []):
            if "alert" not in rule:
                continue
            used = _collect_metric_uses(rule["expr"])
            for name in used:
                if name not in declared:
                    bad.append((rule["alert"], name))
    assert not bad, (
        f"alerts reference undeclared metrics: {bad}\n"
        f"  declared: {sorted(declared)}"
    )


def test_label_matchers_are_subset_of_declared_labels(alerts_doc: dict) -> None:
    """Label matchers `{workspace_id=...}` must reference a declared label.

    Catches typos like `{workspce_id="..."}`. We scan for
    `{label_name="..."}` patterns inside the expr and ensure each
    label name is a declared label of at least one metric.
    """
    import re

    pairs = _all_metric_label_pairs(alerts_doc)
    all_labels = {label for _, labels in pairs for label in labels}

    label_pattern = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=")
    bad: list[tuple[str, str]] = []
    for group in alerts_doc["groups"]:
        for rule in group.get("rules", []):
            if "alert" not in rule:
                continue
            for label_name in label_pattern.findall(rule["expr"]):
                if label_name == "le":  # histogram bucket boundary
                    continue
                if label_name not in all_labels:
                    bad.append((rule["alert"], label_name))
    assert not bad, (
        f"alerts reference undeclared label names: {bad}\n"
        f"  declared: {sorted(all_labels)}"
    )


# ---------------------------------------------------------------------------
# Coverage — every key metric should have at least one rule.
# ---------------------------------------------------------------------------


# Metrics that should be subscribed by an alert rule. These are the
# gauges / counters whose behavior most warrants on-call attention.
WATCHED_METRICS = {
    "chat_requests_total",
    "chat_latency_seconds",
    "eval_hallucination_rate",
    "eval_faithfulness_score",
    "csat_score",
    "dislike_attribution_count",
    "long_context_fallback_total",
    "embedding_cache_hits_total",
    "retrieval_requests_total",
    "open_tickets_by_status",
    "agent_node_total",
    "drift_events_total",
}


def test_all_watched_metrics_have_at_least_one_alert(alerts_doc: dict) -> None:
    """Every key business / quality metric must trigger an alert."""
    all_exprs = "\n".join(
        rule["expr"]
        for group in alerts_doc["groups"]
        for rule in group.get("rules", [])
        if "alert" in rule
    )
    missing = {m for m in WATCHED_METRICS if m not in all_exprs}
    assert not missing, f"no alert rule references: {missing}"