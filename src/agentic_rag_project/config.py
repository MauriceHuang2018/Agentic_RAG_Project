"""Application settings loaded from environment / .env file.

Centralized via pydantic-settings so every module can `get_settings()` and
get a cached Settings instance. Secrets are read from env vars and must NOT
be committed to the repo. See `.env.example` for the full key list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path as _Path

import yaml as _yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_litellm_yaml_defaults() -> dict[str, str]:
    """Read `roles` from `infra/litellm_config.yaml` at module import.

    `roles` is the single source of truth for which registered model plays
    which role (primary chat / 128K fallback / RAG embedding). Sysadmin
    edits the YAML to swap models without code changes; env vars
    (`LITELLM_MODEL`, etc.) still override these defaults.

    Missing file (e.g., stripped CI) returns {} so the per-field fallback
    defaults below take effect. Malformed YAML / parse error also returns
    {} silently — sysadmin will see a separate runtime error from the
    litellm proxy itself when it loads the same file.
    """
    candidates = [
        _Path(__file__).resolve().parents[3] / "infra" / "litellm_config.yaml",
        _Path("infra/litellm_config.yaml"),
    ]
    for path in candidates:
        if path.exists():
            try:
                cfg = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                roles = cfg.get("roles") or {}
                if isinstance(roles, dict):
                    return {str(k): str(v) for k, v in roles.items() if v}
            except Exception:
                # Malformed YAML / parse error — fall back to per-field defaults.
                pass
    return {}


_LITELLM_ROLES = _load_litellm_yaml_defaults()


class Settings(BaseSettings):
    """Strongly-typed application configuration.

    All fields map to environment variables (case-insensitive). Required
    secrets should be provided at runtime; default values are placeholders
    only for local development.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_env: str = Field(default="development")
    app_name: str = Field(default="agentic-rag")
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    app_log_level: str = Field(default="INFO")

    # PostgreSQL
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="rag_db")
    postgres_user: str = Field(default="rag")
    postgres_password: str = Field(default="__FROM_SECRET__")

    # Redis
    redis_host: str = Field(default="localhost")
    redis_port: int = Field(default=6379)
    redis_password: str = Field(default="__FROM_SECRET__")
    # DB 0 — app embedding cache (`embed:cache:*`), Celery-adjacent tenants.
    redis_db: int = Field(default=0)
    # DB 4 — conversation history (`conv:history:*`), isolated from cache so
    # `FLUSHDB`/evictions on the cache DB don't drop chat state.
    redis_history_db: int = Field(default=4)

    # Qdrant
    qdrant_host: str = Field(default="localhost")
    qdrant_port: int = Field(default=6333)
    qdrant_api_key: str = Field(default="__FROM_SECRET__")
    qdrant_collection: str = Field(default="chunks_v1")
    qdrant_vector_dim: int = Field(default=1024)
    # HNSW params: dev uses smaller m/ef_construct for fast indexing;
    # prod bumps both for better recall (DESIGN 4.3).
    qdrant_hnsw_m: int = Field(default=16)
    qdrant_hnsw_ef_construct: int = Field(default=100)
    # Embedding cache TTL (seconds) — `embed:cache:<hash>` keys in Redis.
    qdrant_embedding_cache_ttl_s: int = Field(default=3600)

    # LiteLLM
    litellm_base_url: str = Field(default="http://localhost:4000")
    litellm_api_key: str = Field(default="__FROM_SECRET__")
    # `litellm_model` defaults to `infra/litellm_config.yaml#roles.primary`
    # (loaded by `_load_litellm_yaml_defaults()` at module import). Env var
    # `LITELLM_MODEL` still overrides. Sysadmin swaps the primary model by
    # editing the YAML `roles` block + `model_list` — no code change.
    litellm_model: str = Field(default=_LITELLM_ROLES.get("primary", "gpt-4o"))
    # `litellm_fallback_models` is an env-only override slot. The default
    # routing fallback chain lives in `infra/litellm_config.yaml` under
    # `router_settings.fallbacks` and is consumed by the LiteLLM proxy
    # itself, not by this app.
    litellm_fallback_models: str = Field(default="")
    # `litellm_embedding_model` defaults to
    # `infra/litellm_config.yaml#roles.embedding`. Must match a
    # `model_name` in the same file's `model_list` — the proxy rejects
    # unknown names with 400.
    litellm_embedding_model: str = Field(default=_LITELLM_ROLES.get("embedding", "text-embedding-v4"))
    # DashScope's `text-embedding-v4` upstream caps per-request batch at 10
    # inputs (verified 2026-08-24 against the live LiteLLM proxy: returns
    # `<400> InternalError.Algo.InvalidParameter: ... batch size is invalid,
    # it should not be larger than 10`, no fallback route configured in
    # infra/litellm_config.yaml for embeddings). When swapping to another
    # embedding backend (OpenAI text-embedding-3-* = up to ~2048, BGE-M3
    # direct = ~32), update this default AND the explanatory comment.
    litellm_embedding_batch_size: int = Field(default=10)
    # `litellm_long_context_model` defaults to
    # `infra/litellm_config.yaml#roles.long_context`. Invoked ONLY by
    # `retrieval_direct.long_context_fallback` when two-stage search score
    # falls below `litellm_long_context_threshold`.
    litellm_long_context_model: str = Field(default=_LITELLM_ROLES.get("long_context", "claude-sonnet-4-5"))
    litellm_long_context_threshold: float = Field(default=0.5)

    # RAGFlow DeepDoc Server (independent LitServe service, port 9390).
    # Used only by the visual_router for scanned PDFs and images; structured
    # formats (PDF text / DOCX / PPTX / XLSX / MD / TXT) bypass this entirely.
    ragflow_base_url: str = Field(default="http://localhost:9390")
    ragflow_api_key: str = Field(default="__FROM_SECRET__")
    rapidocr_fallback_enabled: bool = Field(default=True)
    # Auto-detect scanned PDFs (low text density) and route to DeepDoc visual path.
    pdf_scanned_avg_chars_threshold: int = Field(default=100)

    # Storage
    storage_root: str = Field(default="./data/documents")
    storage_max_upload_mb: int = Field(default=50)

    # JWT
    jwt_secret: str = Field(default="__FROM_SECRET__")
    jwt_algorithm: str = Field(default="HS256")
    jwt_expire_minutes: int = Field(default=1440)

    # Celery
    celery_broker_url: str = Field(default="redis://localhost:6379/1")
    celery_result_backend: str = Field(default="redis://localhost:6379/2")

    # Two-stage retrieval
    two_stage_parent_top_k: int = Field(default=3)
    two_stage_child_top_k: int = Field(default=10)

    # Evaluation
    eval_enabled: bool = Field(default=True)
    drift_window_days: int = Field(default=7)
    drift_threshold: float = Field(default=0.05)

    # Observability (T4.3)
    # `METRICS_ALLOWED_CIDRS` is comma-joined extra CIDR strings
    # (loopback is always allowed). `METRICS_BEARER_TOKEN` — when set
    # — requires `Authorization: Bearer <token>` for non-loopback
    # callers. Leave empty in dev for loopback-only access.
    metrics_allowed_cidrs: str = Field(default="")
    metrics_bearer_token: str = Field(default="")

    # Demo accounts (M6 — `rbac.seed._ensure_demo_data`).
    # Passwords live in `.env` (`DEMO_ALICE_PASSWORD` / `DEMO_BOB_PASSWORD`)
    # so source never carries a secret. The literal defaults here match
    # the original dev seed values; rotate by editing `.env`.
    demo_alice_password: str = Field(default="alice_pass")
    demo_bob_password: str = Field(default="bob_pass")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Cached so repeated `get_settings()` calls (FastAPI Depends, Celery tasks)
    do not re-parse `.env` on every invocation.
    """
    return Settings()