# Dockerfile for the api-gateway / celery-worker image.
#
# Multi-stage build:
#   - builder installs deps into a slim venv
#   - runtime copies the venv and source tree
#
# Used by docker-compose for `api-gateway`, `celery-worker`,
# `celery-beat`. Each service overrides CMD / ENTRYPOINT.

# ---- builder ----
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install uv for reproducible dependency installs.
RUN pip install --no-cache-dir uv==0.12.5

# Copy only manifests first so dependency layer is cached.
COPY pyproject.toml uv.lock* ./

# Install deps into /app/.venv (skip the project itself — we COPY src later).
RUN uv sync --frozen --no-install-project --no-dev

# ---- runtime ----
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    # Source lives at /app/src (line 42 COPY) but isn't installed into
    # the venv (builder uses `uv sync --no-install-project`). Without
    # this, `python -m agentic_rag_project` fails with
    # "No module named agentic_rag_project" — the symptom is the
    # rag-api container crash-looping on every restart. Fixed
    # 2026-09-01.
    PYTHONPATH=/app/src

WORKDIR /app

# Copy the prebuilt venv from builder.
COPY --from=builder /app/.venv /app/.venv

# Copy the source tree.
COPY src ./src
COPY pyproject.toml ./

# Create non-root user for runtime.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data/documents \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Default entrypoint (overridden per service in compose).
CMD ["python", "-m", "agentic_rag_project"]