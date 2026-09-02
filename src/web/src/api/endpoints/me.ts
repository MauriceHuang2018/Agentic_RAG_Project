// `/me/*` endpoints — operations scoped to the authenticated user.
//
// Backend contract (api_gateway/me_router.py):
//   GET /me/workspaces → 200 [{ id, name, isolation_level, status }, ...]
//
// The backend `UserContext.workspace_ids` is already `__system__`-free
// (api_gateway.dependencies._resolve_user_context, M6 2026-08-25) so
// the frontend never sees the designated system container — no extra
// filtering required here. Pages that need to filter placeholder rows
// in the workspace list (Page 11 / 12) use `INTERNAL_NAME_PREFIX`
// from `@/constants/internals` (see Page 12 spec in
// docs/原型设计/前端页面规划与字段-表映射.md).

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