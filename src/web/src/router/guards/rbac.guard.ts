// rbac.guard — block routes when the user lacks `meta.permKey` (or any of
// `meta.permKeys`). Super-admin short-circuits via wildcard `'*'` mirror of
// backend `dependencies.py::require_permission`.

import type { NavigationGuardWithThis } from 'vue-router';
import { useAuthStore } from '@/stores/auth';
import { PERMISSION_WILDCARD } from '@/constants/permissions';

export const rbacGuard: NavigationGuardWithThis<undefined> = function (to, _from, next) {
  const auth = useAuthStore();

  // Public routes (e.g. /login) declare no permKey — skip guard entirely.
  const required = collectRequiredPerms(to.meta);
  if (required.length === 0) {
    return next();
  }

  // Super-admin wildcard — matches backend `_resolve_user_context` for super.
  if (auth.isSuperAdmin || auth.permissions.includes(PERMISSION_WILDCARD)) {
    return next();
  }

  const allowed = required.some((key) => auth.permissions.includes(key));
  if (!allowed) {
    return next({ path: '/forbidden', query: { from: to.fullPath }, replace: true });
  }

  return next();
};

/** Pull permKey(s) from route meta in a type-safe way. */
function collectRequiredPerms(meta: unknown): string[] {
  if (!meta || typeof meta !== 'object') return [];
  const m = meta as Record<string, unknown>;
  const out: string[] = [];
  if (typeof m.permKey === 'string') out.push(m.permKey);
  if (Array.isArray(m.permKeys)) {
    for (const item of m.permKeys) if (typeof item === 'string') out.push(item);
  }
  return out;
}