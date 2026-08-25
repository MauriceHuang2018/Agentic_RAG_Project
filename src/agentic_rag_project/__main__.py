"""Entry point for `python -m agentic_rag_project` and the console script.

Boots the FastAPI application via uvicorn using settings sourced from
environment variables / `.env`. See `main.create_app` for the FastAPI
factory.
"""

from __future__ import annotations

import uvicorn

from agentic_rag_project.config import get_settings


def main() -> None:
    """Launch the API gateway with uvicorn.

    Reads host/port/log_level from application settings. Intended for local
    development; production deployments should run uvicorn (or gunicorn with
    uvicorn workers) directly against `main:app`.
    """
    settings = get_settings()
    uvicorn.run(
        "agentic_rag_project.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.app_log_level.lower(),
        reload=settings.app_env == "development",
    )


if __name__ == "__main__":
    main()