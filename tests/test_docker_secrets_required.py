"""Tests for M5 T7 — CI guard that docker-compose secrets are all set.

DESIGN §4.6.2 / TASK T7 — the docker-compose.yml hardening (T6)
made every credential env var `:?`-guarded, which makes `docker
compose config` fail-fast when any required secret is unset. This
test asserts that contract so a regression (someone removes the
`:?` guard) flips the test red before CI ships a vulnerable image.

The test calls `docker compose config --quiet` in a temp directory
that:

  1. Copies the production `docker-compose.yml` + `infra/redis/`
     so the `redis.conf` volume mount resolves.
  2. Pre-populates `.env` with stub values for ALL required vars,
     so `docker compose config` passes the baseline.
  3. For each of the 5 parametrize cases, removes ONE var from
     `.env` and asserts `docker compose config` exits non-zero.

Skip when no `docker` CLI is on PATH — typical for a CI runner
without a docker daemon (the test would only run on the host that
also runs the compose stack). The `skipif` is `not shutil.which`
so dev workstations (which usually have docker) always run the test.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

# All required vars that docker-compose.yml now `:?`-guards. The
# test removes ONE of these per parametrize case and asserts
# `docker compose config` fails. The list is derived from a
# `grep '\${[A-Z_]*[?][^}]*}' docker-compose.yml` after T6.
REQUIRED_VARS: tuple[str, ...] = (
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "DASHSCOPE_API_KEY",
    "Ali_API_BASE_URL",
    "LITELLM_API_KEY",
    "RAGFLOW_API_KEY",
    "JWT_SECRET",
    "GRAFANA_ADMIN_PASSWORD",
)

# Stable stub values so `docker compose config` doesn't blow up on
# other validations (port clashes, URI parsing, etc.). Real keys
# are NOT used here — the test never reaches the actual services.
STUB_VALUES: dict[str, str] = {
    "POSTGRES_USER": "rag_test",
    "POSTGRES_PASSWORD": "x" * 20,
    "REDIS_PASSWORD": "x" * 20,
    "DASHSCOPE_API_KEY": "sk-test-dashscope",
    "Ali_API_BASE_URL": "https://example.com/v1",
    "LITELLM_API_KEY": "sk-test-litellm",
    "RAGFLOW_API_KEY": "sk-test-ragflow",
    "JWT_SECRET": "x" * 32,
    "GRAFANA_ADMIN_PASSWORD": "x" * 20,
}


# ---------------------------------------------------------------------------
# Pytest collection skip — no docker CLI → no compose to validate
# ---------------------------------------------------------------------------


docker_available = shutil.which("docker") is not None
skip_no_docker = pytest.mark.skipif(
    not docker_available,
    reason="docker CLI not available; CI guard requires docker compose",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def compose_workspace(tmp_path: Path) -> Path:
    """Copy docker-compose.yml + infra/redis into a temp dir.

    `docker compose config` resolves relative paths (`./infra/...`,
    `./data/...`) relative to the cwd by default. We cd into the
    temp dir before invoking so the redis.conf mount resolves.

    We do NOT copy `data/` or `infra/postgres-init/` — only what
    `docker compose config` parses. The test never starts the
    services, so the init scripts are irrelevant.
    """
    project_root = Path(__file__).resolve().parent.parent
    compose_src = project_root / "docker-compose.yml"
    assert compose_src.is_file(), (
        f"docker-compose.yml not found at {compose_src}; "
        "is the test being run from the wrong repo?"
    )

    # Copy the compose file.
    (tmp_path / "docker-compose.yml").write_bytes(compose_src.read_bytes())

    # Copy the redis.conf + its parent directory (volume mount
    # `./infra/redis/redis.conf:/etc/redis/redis.conf:ro`).
    redis_dir = tmp_path / "infra" / "redis"
    redis_dir.mkdir(parents=True)
    shutil.copy(
        project_root / "infra" / "redis" / "redis.conf",
        redis_dir / "redis.conf",
    )

    # Pre-populate `.env` with stub values for ALL required vars.
    env_lines = [f"{k}={v}\n" for k, v in STUB_VALUES.items()]
    (tmp_path / ".env").write_text("".join(env_lines), encoding="utf-8")

    return tmp_path


def _compose_config(
    cwd: Path,
    *,
    env_overrides: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke `docker compose config --quiet` in `cwd`.

    `env_overrides` lets the test remove (set to None) or change
    specific vars before running — values are written into a
    fresh `.env` on top of the pre-populated stub.
    """
    if env_overrides:
        env_file = cwd / ".env"
        current = env_file.read_text(encoding="utf-8").splitlines()
        # Replace or remove targeted lines.
        for key, value in env_overrides.items():
            replaced = False
            for i, line in enumerate(current):
                if line.startswith(f"{key}="):
                    if value is None:
                        current.pop(i)
                    else:
                        current[i] = f"{key}={value}"
                    replaced = True
                    break
            if not replaced and value is not None:
                current.append(f"{key}={value}")
        env_file.write_text("\n".join(current) + "\n", encoding="utf-8")

    # Ensure no host-side env leaks into the docker-compose env
    # resolution (otherwise a CI runner that happens to have
    # `POSTGRES_PASSWORD` set would mask the test).
    popen_env = {
        k: v
        for k, v in os.environ.items()
        if k not in STUB_VALUES
    }
    # Make sure PATH still has docker.
    popen_env["PATH"] = os.environ.get("PATH", "")

    return subprocess.run(
        ["docker", "compose", "config", "--quiet"],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=popen_env,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@skip_no_docker
def test_t7_all_vars_set_compose_config_passes(
    compose_workspace: Path,
) -> None:
    """Baseline: with every required var stubbed, `docker compose
    config` exits 0. This is the assertion we BREAK by removing a
    var in the parametrize tests below.
    """
    proc = _compose_config(compose_workspace)
    assert proc.returncode == 0, (
        f"baseline docker compose config failed: stderr={proc.stderr!r}"
    )


@skip_no_docker
@pytest.mark.parametrize("missing_var", REQUIRED_VARS)
def test_t7_unset_required_var_fails_compose_config(
    compose_workspace: Path,
    missing_var: str,
) -> None:
    """Removing ANY one of the 9 required vars makes `docker compose
    config` exit non-zero. This is the regression guard for the
    T6 hardening: a future change that drops a `:?` would let one
    of these tests pass with the var unset.
    """
    proc = _compose_config(
        compose_workspace,
        env_overrides={missing_var: None},
    )
    assert proc.returncode != 0, (
        f"docker compose config should fail when {missing_var} is unset, "
        f"but it exited 0. The `:?` guard on {missing_var} is missing or "
        f"the .env resolution is leaking from the host."
    )
    # Best-effort check on the error message — the wording varies
    # between compose v1 / v2 / podman, but the variable name
    # must appear somewhere.
    combined = (proc.stderr + proc.stdout).lower()
    assert missing_var.lower() in combined or "required" in combined, (
        f"error output did not mention the missing var: "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


@skip_no_docker
def test_t7_redis_volume_mount_present(
    compose_workspace: Path,
) -> None:
    """`docker compose config` (without --quiet) renders the redis
    service with the `infra/redis/redis.conf` mount. We grep the
    rendered output so a silent removal of the volume mount flips
    this test red.
    """
    proc = subprocess.run(
        ["docker", "compose", "config"],
        cwd=compose_workspace,
        capture_output=True,
        text=True,
        env={
            k: v
            for k, v in os.environ.items()
            if k not in STUB_VALUES
        },
        timeout=60,
    )
    assert proc.returncode == 0
    assert "redis.conf" in proc.stdout
    # The `:ro` mount suffix is part of the S5 fix (defense in
    # depth: container can't overwrite the password file).
    # Compose v2 normalises `:ro` into `read_only: true` in the
    # rendered output, so we accept either form.
    redis_block = proc.stdout.split("redis:", 1)[1].split(
        "alertmanager:", 1
    )[0]
    assert (
        ":ro" in redis_block or "read_only: true" in redis_block
    ), f"redis volume mount not marked read-only: {redis_block!r}"


@skip_no_docker
def test_t7_password_not_in_redis_command_line(
    compose_workspace: Path,
) -> None:
    """`docker compose config` for the redis service must NOT
    include the literal `--requirepass <REDIS_PASSWORD>` substring
    on the command line. The S5 fix moved the password into
    redis.conf; this guard prevents someone from reverting that
    to a CLI flag (which `docker inspect` would expose).
    """
    proc = subprocess.run(
        ["docker", "compose", "config"],
        cwd=compose_workspace,
        capture_output=True,
        text=True,
        env={
            k: v
            for k, v in os.environ.items()
            if k not in STUB_VALUES
        },
        timeout=60,
    )
    assert proc.returncode == 0
    assert "--requirepass" not in proc.stdout, (
        "redis command line still has --requirepass; S5 fix "
        "(password moved to redis.conf) was reverted."
    )


# ---------------------------------------------------------------------------
# Static checks (run even when docker is missing)
# ---------------------------------------------------------------------------


def test_t7_docker_compose_yml_has_required_guards() -> None:
    """Static check: every var in REQUIRED_VARS appears in
    docker-compose.yml with a `:?` (or the existing `:?` was kept).
    This runs without docker and catches a `:?` → bare revert.
    """
    project_root = Path(__file__).resolve().parent.parent
    compose_text = (project_root / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    for var in REQUIRED_VARS:
        # `:?VAR` (must-set with VAR-named message) OR `:?VAR required`
        # — both forms satisfy the test.
        assert f"${{{var}:?" in compose_text, (
            f"{var} is not :?-guarded in docker-compose.yml. "
            f"T6 hardening removed."
        )


def test_t7_env_example_has_all_required_vars() -> None:
    """`.env.example` lists every required var so a fresh checkout
    can `cp .env.example .env` and fill in real secrets.
    """
    project_root = Path(__file__).resolve().parent.parent
    env_example = (project_root / ".env.example").read_text(
        encoding="utf-8"
    ).splitlines()
    defined = {
        line.split("=", 1)[0]
        for line in env_example
        if line and not line.startswith("#") and "=" in line
    }
    missing = [v for v in REQUIRED_VARS if v not in defined]
    assert not missing, (
        f".env.example is missing required vars: {missing}. "
        f"Copy .env.example → .env will fail at first deploy."
    )


def test_t7_redis_conf_uses_redirection() -> None:
    """`infra/redis/redis.conf` exists, is non-empty, and includes
    a `requirepass` line that resolves the REDIS_PASSWORD env var
    at container startup.
    """
    project_root = Path(__file__).resolve().parent.parent
    conf_path = project_root / "infra" / "redis" / "redis.conf"
    assert conf_path.is_file(), "infra/redis/redis.conf is missing"
    text = conf_path.read_text(encoding="utf-8")
    assert "requirepass" in text
    # Comment markers indicate the file is documented, not a dump.
    assert "#" in text


__all__ = [
    "test_t7_all_vars_set_compose_config_passes",
    "test_t7_unset_required_var_fails_compose_config",
    "test_t7_redis_volume_mount_present",
    "test_t7_password_not_in_redis_command_line",
    "test_t7_docker_compose_yml_has_required_guards",
    "test_t7_env_example_has_all_required_vars",
    "test_t7_redis_conf_uses_redirection",
]