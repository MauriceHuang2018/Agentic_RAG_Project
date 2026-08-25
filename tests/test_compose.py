"""Structural validation of Docker Compose / LiteLLM / Prometheus configs.

These tests verify the YAML is parseable, the expected services are
declared, and required env vars are referenced. They do NOT actually
build or run containers — that requires Docker and is out of scope for
unit tests.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _compose() -> dict:
    """Load docker-compose.yml."""
    with (_REPO_ROOT / "docker-compose.yml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _litellm() -> dict:
    """Load infra/litellm_config.yaml."""
    with (_REPO_ROOT / "infra" / "litellm_config.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _prometheus() -> dict:
    """Load infra/prometheus.yml."""
    with (_REPO_ROOT / "infra" / "prometheus.yml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_compose_has_ten_services() -> None:
    """Compose declares all 10 DESIGN-listed services."""
    services = _compose()["services"]
    expected = {
        "postgres",
        "redis",
        "qdrant",
        "litellm",
        "ragflow-deepdoc",
        "api-gateway",
        "celery-worker",
        "celery-beat",
        "prometheus",
        "grafana",
    }
    assert expected <= set(services.keys())


def test_postgres_uses_postgres_16() -> None:
    """Postgres pinned to v16 to match DESIGN's stack choice."""
    assert _compose()["services"]["postgres"]["image"].startswith("postgres:16")


def test_qdrant_has_grpc_port() -> None:
    """Qdrant exposes 6333 (HTTP) + 6334 (gRPC) per qdrant-client defaults."""
    ports = _compose()["services"]["qdrant"]["ports"]
    assert any("6333" in str(p) for p in ports)
    assert any("6334" in str(p) for p in ports)


def test_api_gateway_depends_on_infrastructure() -> None:
    """api-gateway waits for postgres / redis / qdrant / litellm health."""
    deps = _compose()["services"]["api-gateway"]["depends_on"]
    for svc in ("postgres", "redis", "qdrant", "litellm"):
        assert svc in deps


def test_celery_worker_uses_redis_broker() -> None:
    """Celery worker uses redis://redis:6379/1 as broker."""
    env = _compose()["services"]["celery-worker"]["environment"]
    assert env["CELERY_BROKER_URL"] == "redis://redis:6379/1"


def test_celery_beat_runs_drift_scheduler() -> None:
    """celery-beat command includes the celery app and beat scheduler."""
    cmd = _compose()["services"]["celery-beat"]["command"]
    joined = " ".join(cmd)
    assert "celery" in joined
    assert "beat" in joined
    assert "agentic_rag_project.doc_processor.celery_app" in joined


def test_litellm_roles_point_to_registered_models() -> None:
    """YAML `roles` is the single source of truth for which registered
    model plays which role. Each role value must reference a `model_name`
    in the same file's `model_list` — that's the structural guarantee
    that the `config.py` YAML loader resolves to a routable model.
    """
    cfg = _litellm()
    model_names = {m["model_name"] for m in cfg["model_list"]}
    roles = cfg.get("roles") or {}
    for role_key in ("primary", "long_context", "embedding"):
        assert role_key in roles, f"missing roles.{role_key}"
        assert roles[role_key] in model_names, (
            f"roles.{role_key}={roles[role_key]!r} not in model_list={sorted(model_names)}"
        )


def test_litellm_no_orphan_role_value() -> None:
    """Every role key has a non-empty string value."""
    cfg = _litellm()
    roles = cfg.get("roles") or {}
    assert roles, "roles block must not be empty"
    for key, value in roles.items():
        assert isinstance(value, str) and value, f"roles.{key} must be a non-empty string"


def test_litellm_fallbacks_cover_routable_models() -> None:
    """Every routable non-embedding model must appear as either a primary
    key or a backup value in `router_settings.fallbacks`. Embedding models
    are excluded — they have no chat-failover semantics. Structural
    assertion so sysadmin can rename models freely without breaking tests.
    """
    cfg = _litellm()
    model_names = {m["model_name"] for m in cfg["model_list"]}
    embedding = (cfg.get("roles") or {}).get("embedding")
    fallbacks = cfg["router_settings"]["fallbacks"]
    primaries: set[str] = set()
    backups: set[str] = set()
    for entry in fallbacks:
        # entry is a single-key dict: {primary: [backups]}
        assert isinstance(entry, dict) and len(entry) == 1, (
            f"each fallback entry must be {{primary: [backups]}}, got {entry!r}"
        )
        for primary, backup_list in entry.items():
            assert isinstance(backup_list, list) and backup_list, (
                f"fallbacks[{primary!r}] must be a non-empty list"
            )
            primaries.add(primary)
            backups.update(backup_list)
    covered = primaries | backups
    uncovered = {m for m in model_names if m != embedding} - covered
    assert not uncovered, (
        f"routable models with no fallback entry: {sorted(uncovered)}; "
        f"covered={sorted(covered)}"
    )


def test_litellm_fallbacks_only_reference_registered_models() -> None:
    """Every name mentioned in `router_settings.fallbacks` must also be
    in `model_list` — proxy rejects unknown fallback targets with 400.
    """
    cfg = _litellm()
    model_names = {m["model_name"] for m in cfg["model_list"]}
    for entry in cfg["router_settings"]["fallbacks"]:
        for primary, backup_list in entry.items():
            assert primary in model_names, (
                f"fallback primary {primary!r} not in model_list"
            )
            for backup in backup_list:
                assert backup in model_names, (
                    f"fallback backup {backup!r} (under {primary!r}) "
                    f"not in model_list"
                )


def test_litellm_config_settings_defaults_follow_yaml() -> None:
    """`config.py` imports `infra/litellm_config.yaml#roles` at module
    load. Verify the loader returned the YAML values AND the Settings
    field defaults reflect them — not stale hardcoded strings.

    This is the end-to-end wiring test: change a role in the YAML,
    this test must fail until config.py is reloaded with the new value.
    """
    import agentic_rag_project.config as cfg_module

    yaml_roles = _litellm().get("roles") or {}
    loaded_roles = getattr(cfg_module, "_LITELLM_ROLES")

    # Loader returned the YAML values
    assert loaded_roles.get("primary") == yaml_roles.get("primary")
    assert loaded_roles.get("long_context") == yaml_roles.get("long_context")
    assert loaded_roles.get("embedding") == yaml_roles.get("embedding")

    # Per-field Settings defaults reflect the loaded roles
    fields = cfg_module.Settings.model_fields
    assert fields["litellm_model"].default == yaml_roles["primary"]
    assert fields["litellm_long_context_model"].default == yaml_roles["long_context"]
    assert fields["litellm_embedding_model"].default == yaml_roles["embedding"]


def test_litellm_redis_cache_enabled() -> None:
    """LiteLLM caches responses in Redis (24h TTL) per DESIGN 4.5."""
    general = _litellm()["general_settings"]
    assert general["cache"] is True
    assert general["cache_params"]["type"] == "redis"


def test_prometheus_scrapes_api_gateway() -> None:
    """Prometheus targets include api-gateway:8000."""
    jobs = _prometheus()["scrape_configs"]
    api_job = next(j for j in jobs if j["job_name"] == "api-gateway")
    assert "api-gateway:8000" in api_job["static_configs"][0]["targets"]