#!/usr/bin/env bash
# gen-openapi-ts.sh — regenerate src/api/types.gen.ts from backend /openapi.json.
# Usage: pnpm gen:openapi   (or bash scripts/gen-openapi-ts.sh)
# Requirements:
#   - Backend running and reachable (default http://localhost:8000).
#   - `openapi-typescript` installed (devDependency).
# Env overrides:
#   OPENAPI_URL  — override default http://localhost:8000/openapi.json
#   TYPES_OUT    — override default src/api/types.gen.ts

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

OPENAPI_URL="${OPENAPI_URL:-http://localhost:8000/openapi.json}"
TYPES_OUT="${TYPES_OUT:-${WEB_ROOT}/src/api/types.gen.ts}"

echo "[gen-openapi-ts] fetch ${OPENAPI_URL}"
if ! command -v curl >/dev/null 2>&1; then
  echo "[gen-openapi-ts] error: curl not found in PATH" >&2
  exit 1
fi

TMP_JSON="$(mktemp -t openapi.XXXXXX.json)"
trap 'rm -f "${TMP_JSON}"' EXIT

if ! curl --silent --show-error --fail --max-time 30 "${OPENAPI_URL}" -o "${TMP_JSON}"; then
  echo "[gen-openapi-ts] error: failed to fetch ${OPENAPI_URL}" >&2
  echo "[gen-openapi-ts] hint: start backend or set OPENAPI_URL" >&2
  exit 1
fi

if [ ! -s "${TMP_JSON}" ]; then
  echo "[gen-openapi-ts] error: ${OPENAPI_URL} returned empty body" >&2
  exit 1
fi

mkdir -p "$(dirname "${TYPES_OUT}")"

echo "[gen-openapi-ts] generate ${TYPES_OUT}"
# npx picks up openapi-typescript from devDependencies; --no-install avoids the
# network round-trip when the binary is already cached.
npx --no-install openapi-typescript "${TMP_JSON}" --output "${TYPES_OUT}"

echo "[gen-openapi-ts] done ($(wc -l <"${TYPES_OUT}") lines)"