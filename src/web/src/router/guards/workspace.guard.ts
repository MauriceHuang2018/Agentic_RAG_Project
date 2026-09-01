// workspace.guard — gate routes by workspace membership / selection.
// Backend `dependencies.py::_resolve_user_context` already filters out
// `__system__` from `UserContext.workspace_ids`, but Page 11/12 surfaces that
// row explicitly via `INTERNAL_NAME_PREFIX` filter (see constants/internals.ts).
// This guard's job is the lightweight UX gate: if the route declares
// `meta.workspaceRequired` and the active workspace is unset, push the user
// to a workspace picker instead of letting downstream 403s bubble up.

import type { NavigationGuardWithThis } from 'vue-router';
import { useWorkspaceStore } from '@/stores/workspace';

export const workspaceGuard: NavigationGuardWithThis<undefined> = function (to, _from, next) {
  const ws = useWorkspaceStore();

  const meta = (to.meta ?? {}) as { workspaceRequired?: boolean };
  if (!meta.workspaceRequired) return next();

  if (!ws.activeWorkspaceId) {
    return next({ path: '/chat', query: { workspaceRequired: '1' }, replace: true });
  }
  return next();
};