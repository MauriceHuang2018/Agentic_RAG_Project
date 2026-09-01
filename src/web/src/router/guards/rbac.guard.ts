// rbac.guard — block routes when the user lacks the required permissions.
//
// IMPORTANT (security default): when a route declares multiple keys via
// `meta.permKeys`, the user must have ALL of them (AND semantics) unless the
// route explicitly opts into OR via `meta.permMode: 'any'`. AND is the safer
// default; OR is reserved for unified admin views where a single page hosts
// multiple role capabilities (e.g. a doc-admin page that any doc:* capability
// is allowed to enter).
//
// This guard is a UX gate (hide the page). The authoritative security
// boundary lives in `backend/dependencies.py::require_permission`, which
// checks each action call. A misconfigured frontend gate is mitigated by the
// backend re-check on every API call.
//
// Super-admin short-circuits via wildcard `'*'`, mirroring backend
// `_resolve_user_context` for super.

import type { NavigationGuardWithThis } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import { PERMISSION_WILDCARD } from '@/constants/permissions';

export const rbacGuard: NavigationGuardWithThis<undefined> = function (to, _from, next) {
  const auth = useAuthStore();

  // Public routes (e.g. /login) declare no permKey — skip guard entirely.
  const rule = collectRequiredPerms(to.meta);
  if (rule.keys.length === 0) {
    return next();
  }

  // Super-admin wildcard — matches backend `_resolve_user_context` for super.
  if (auth.isSuperAdmin || auth.permissions.includes(PERMISSION_WILDCARD)) {
    return next();
  }

  const held = new Set(auth.permissions);
  const allowed = rule.mode === 'any'
    ? rule.keys.some((key) => held.has(key))
    : rule.keys.every((key) => held.has(key));

  if (!allowed) {
    return next({ path: '/forbidden', query: { from: to.fullPath }, replace: true });
  }

  return next();
};

/** Perm requirement shape parsed from route meta. */
interface PermRule {
  keys: string[];
  mode: 'any' | 'all';
}

/** Pull permKey(s) and permMode from route meta in a type-safe way. */
function collectRequiredPerms(meta: unknown): PermRule {
  const empty: PermRule = { keys: [], mode: 'all' };
  if (!meta || typeof meta !== 'object') return empty;
  const m = meta as Record<string, unknown>;

  const keys: string[] = [];
  if (typeof m.permKey === 'string') keys.push(m.permKey);
  if (Array.isArray(m.permKeys)) {
    for (const item of m.permKeys) if (typeof item === 'string') keys.push(item);
  }
  if (keys.length === 0) return empty;

  // Default 'all' (AND) is the safer gate; routes that intentionally want
  // ANY-of (OR) must opt in explicitly via `permMode: 'any'`.
  const mode: 'any' | 'all' = m.permMode === 'any' ? 'any' : 'all';
  return { keys, mode };
}