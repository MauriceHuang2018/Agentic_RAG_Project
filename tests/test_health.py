"""Smoke test for the FastAPI health endpoint.

Verifies that `create_app()` boots and `/health` returns the expected
payload without touching any external dependency.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agentic_rag_project.config import get_settings
from agentic_rag_project.main import create_app


def test_health_endpoint_returns_ok() -> None:
    """`/health` returns 200 with `status: ok` and the configured app name."""
    settings = get_settings()
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "app": settings.app_name}


def test_create_app_is_idempotent() -> None:
    """`create_app()` can be called repeatedly without side effects."""
    app_a = create_app()
    app_b = create_app()

    assert app_a is not app_b  # fresh instances
    assert app_a.title == app_b.title