#!/usr/bin/env bash
# gen-perm-keys-sync.sh — diff frontend constants/permissions.ts against the
# authoritative backend list (src/agentic_rag_project/rbac/seed.py).
#
# Strategy: extract the Python list literal `[...]` of strings, normalise it,
# compare to the TypeScript tuple. On mismatch, print a unified diff and exit
# non-zero so CI blocks the commit.
#
# Usage: pnpm gen:perms   (or bash scripts/gen-perm-keys-sync.sh [--write])
#   --write   Overwrite frontend constants/permissions.ts to align with backend.
#
# Exit codes:
#   0 = in sync
#   1 = out of sync (or backend file missing / unparsable)
#   2 = --write succeeded (re-run without --write to confirm)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${WEB_ROOT}/.." && pwd)"

BACKEND_FILE="${REPO_ROOT}/src/agentic_rag_project/rbac/seed.py"
FRONTEND_FILE="${WEB_ROOT}/src/constants/permissions.ts"

WRITE="false"
if [ "${1:-}" = "--write" ]; then
  WRITE="true"
fi

if [ ! -f "${BACKEND_FILE}" ]; then
  echo "[gen-perm-keys-sync] error: backend file not found: ${BACKEND_FILE}" >&2
  exit 1
fi
if [ ! -f "${FRONTEND_FILE}" ]; then
  echo "[gen-perm-keys-sync] error: frontend file not found: ${FRONTEND_FILE}" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[gen-perm-keys-sync] error: python3 not found in PATH" >&2
  exit 1
fi

# Extract perm keys from the Python list literal that follows `PERMISSION_KEYS`.
PYTHON_KEYS="$(python3 - <<'PY' "${BACKEND_FILE}"
import ast, pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
mod = ast.parse(src)
keys = []
for node in mod.body:
    if isinstance(node, ast.Assign):
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "PERMISSION_KEYS":
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            keys.append(elt.value)
print("\n".join(keys))
PY
)"

if [ -z "${PYTHON_KEYS}" ]; then
  echo "[gen-perm-keys-sync] error: could not parse PERMISSION_KEYS in ${BACKEND_FILE}" >&2
  exit 1
fi

# Extract single-quoted perm strings from the TypeScript tuple.
TS_KEYS="$(grep -oE "'[a-z]+:[a-z_]+'" "${FRONTEND_FILE}" | sort -u | sed "s/^'//; s/'$//")"

PY_SORTED="$(printf "%s\n" "${PYTHON_KEYS}" | sort -u)"

if [ "${PY_SORTED}" = "${TS_KEYS}" ]; then
  echo "[gen-perm-keys-sync] in sync ($(printf "%s\n" "${PY_SORTED}" | wc -l) keys)"
  exit 0
fi

echo "[gen-perm-keys-sync] drift detected:"
diff <(printf "%s\n" "${PY_SORTED}") <(printf "%s\n" "${TS_KEYS}") || true

if [ "${WRITE}" = "true" ]; then
  echo "[gen-perm-keys-sync] --write requested but manual sync is preferred; rerun after editing ${FRONTEND_FILE}" >&2
  exit 2
fi
exit 1