// `/me/*` endpoints — operations scoped to the authenticated user.
//
// Backend contract (api_gateway/me_router.py):
//   GET /me/workspaces    → 200 [{ id, name, isolation_level, status }, ...]
//   GET /me/permissions   → 200 { permissions: string[] }  (sorted; '*' for super_admin)
//
// The backend `UserContext.workspace_ids` is already `__system__`-free
// (api_gateway.dependencies._resolve_user_context, M6 2026-08-25) so
// the frontend never sees the designated system container — no extra
// filtering required here. Pages that need to filter placeholder rows
// in the workspace list (Page 11 / 12) use `INTERNAL_NAME_PREFIX`
// from `@/constants/internals` (see Page 12 spec in
// docs/原型设计/前端页面规划与字段-表映射.md).
//
// `GET /me/permissions` exists to feed `useAuthStore.permissions`
// after login — `rbac.guard` and `stores/permission.ts::can` consult
// it. The authoritative permission check still lives server-side in
// `dependencies.require_permission`; this is the UX gate only.

import { httpClient } from '../client';

export interface MeWorkspace {
  id: string;
  name: string;
  /** 'logical' | 'physical' — see workspaces.isolation_level. */
  isolationLevel?: 'logical' | 'physical';
  /** 'enable' | 'disable' — disabled workspaces should not be selectable. */
  status?: 'enable' | 'disable';
}

/**
 * Fetch the workspaces the caller is a member of, ready for
 * `useWorkspaceStore.setFromLogin`. Returns an empty array when the
 * user has no workspace bindings — the store + Chat.vue handle that
 * with a "no workspace available" state instead of a fake UUID.
 *
 * Throws on 401 (token expired → axios interceptor redirects to
 * /login) or 5xx.
 */
export async function listMyWorkspaces(): Promise<MeWorkspace[]> {
  const r = await httpClient.get<MeWorkspace[]>('/me/workspaces');
  return r.data;
}

/**
 * Fetch the permission keys the caller holds, ready to be set as
 * `useAuthStore.permissions`. Mirrors `_resolve_user_context.permissions`
 * server-side.
 *
 * Returned list is sorted alphabetically (backend contract). For
 * super_admin the response is exactly `["*"]` (matches
 * `PERMISSION_WILDCARD` in `@/constants/permissions`). For users
 * with no role bindings the response is `[]` (NOT 404), so callers
 * can `await listMyPermissions()` unconditionally after login and
 * get a usable set (possibly empty).
 *
 * Throws on 401 (token expired → axios interceptor redirects to
 * /login) or 5xx. The store wraps this in a try/catch so a transient
 * 5xx doesn't lock the user out — they fall through to the
 * no-permissions UX (route-level gating will redirect /forbidden).
 */
export async function listMyPermissions(): Promise<string[]> {
  const r = await httpClient.get<{ permissions: string[] }>('/me/permissions');
  return r.data.permissions;
}