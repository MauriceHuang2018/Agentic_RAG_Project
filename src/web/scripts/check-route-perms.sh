#!/usr/bin/env bash
# check-route-perms.sh — static lint that every router meta.permKey / permKeys
# entry is in the PERMISSION_KEYS whitelist.
#
# Usage: pnpm check:perms   (or bash scripts/check-route-perms.sh)
#
# Walks src/router/routes.ts and src/constants/permissions.ts; emits each
# unknown perm key on its own line and exits non-zero if drift is found.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ROUTES_FILE="${WEB_ROOT}/src/router/routes.ts"
PERMS_FILE="${WEB_ROOT}/src/constants/permissions.ts"

if [ ! -f "${ROUTES_FILE}" ] || [ ! -f "${PERMS_FILE}" ]; then
  echo "[check-route-perms] error: required source files missing" >&2
  exit 1
fi

# Whitelist: every single-quoted perm string inside PERMISSION_KEYS = [...]
PERMS_KNOWN="$(grep -oE "'[a-z]+:[a-z_*]+'" "${PERMS_FILE}" | sort -u | sed "s/^'//; s/'$//")"

# Used keys: every permKey: '...' and every '...' inside permKeys: ['...', ...]
ROUTE_KEYS_RAW="$(
  grep -oE "permKey: *'[^']+'" "${ROUTES_FILE}" | sed -E "s/permKey: *'([^']+)'/\1/";
  grep -oE "permKeys: *\[[^]]+\]" "${ROUTES_FILE}" \
    | grep -oE "'[a-z]+:[a-z_*]+'" \
    | sed "s/^'//; s/'$//"
)"

ROUTE_KEYS="$(printf "%s\n" "${ROUTE_KEYS_RAW}" | sort -u)"

UNKNOWN="$(comm -23 <(printf "%s\n" "${ROUTE_KEYS}") <(printf "%s\n" "${PERMS_KNOWN}")"

if [ -z "${UNKNOWN}" ]; then
  TOTAL="$(printf "%s\n" "${ROUTE_KEYS}" | wc -l | tr -d ' ')"
  echo "[check-route-perms] OK — ${TOTAL} route perm keys all whitelisted"
  exit 0
fi

echo "[check-route-perms] FAIL — unknown perm key(s) referenced in routes:" >&2
printf "  %s\n" "${UNKNOWN}" >&2
echo >&2
echo "[check-route-perms] hint: add the key to constants/permissions.ts (or run pnpm gen:perms --write)" >&2
exit 1