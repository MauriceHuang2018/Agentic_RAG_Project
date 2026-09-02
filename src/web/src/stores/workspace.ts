// workspace store — tracks the active workspace and the workspace list.
//
// Backend contract (api_gateway/me_router.py + dependencies.py):
//   - GET /api/v1/me/workspaces returns the caller's workspaces
//     ({id, name, isolation_level, status}), already filtered for the
//     designated `__system__` container by `_resolve_user_context`.
//   - The store seeds itself in `setFromLogin()` (called from
//     Login.vue after a successful /auth/login).
//
// Pre-`/me/workspaces` (closed 2026-09-02): the store used to inject
// a hard-coded `00000000-...-0000` placeholder so the chat page could
// send SOMETHING. That broke every chat request with 403
// `not_a_member_of_workspace` because the UUID didn't match any real
// membership. Today the empty case is handled by Chat.vue's
// empty-state alert — never by faking a workspace id.

import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { isInternalName } from '@/constants/internals';

export interface WorkspaceItem {
  id: string;
  name: string;
  isolationLevel?: 'logical' | 'physical';
  status?: 'enable' | 'disable';
}

const ACTIVE_STORAGE_KEY = 'agentic_rag.active_workspace_id';

function readStoredActive(): string | null {
  try {
    return localStorage.getItem(ACTIVE_STORAGE_KEY);
  } catch {
    return null;
  }
}

function persistActive(id: string | null): void {
  try {
    if (id) localStorage.setItem(ACTIVE_STORAGE_KEY, id);
    else localStorage.removeItem(ACTIVE_STORAGE_KEY);
  } catch {
    // ignore
  }
}

export const useWorkspaceStore = defineStore('workspace', () => {
  const workspaces = ref<WorkspaceItem[]>([]);
  const activeWorkspaceId = ref<string | null>(readStoredActive());

  const activeWorkspace = computed<WorkspaceItem | null>(() => {
    const id = activeWorkspaceId.value;
    if (!id) return null;
    return workspaces.value.find((w) => w.id === id) ?? null;
  });

  const visibleWorkspaces = computed<WorkspaceItem[]>(() =>
    workspaces.value.filter((w) => !isInternalName(w.name)),
  );

  function setList(next: WorkspaceItem[]): void {
    workspaces.value = next;
    // If the previously active workspace disappeared (e.g. disabled), fall back.
    if (activeWorkspaceId.value && !next.some((w) => w.id === activeWorkspaceId.value)) {
      const fallback = visibleWorkspaces.value[0] ?? next[0];
      setActive(fallback?.id ?? null);
    }
  }

  function setActive(id: string | null): void {
    activeWorkspaceId.value = id;
    persistActive(id);
  }

  /**
   * Bootstrap from `GET /me/workspaces` (called by Login.vue right
   * after a successful /auth/login). Filters `__system__` defensively
   * even though the backend already removed it — Page 12 source data
   * may surface placeholder rows in unrelated contexts.
   *
   * Also auto-picks the first workspace as `activeWorkspaceId` if
   * nothing is currently selected AND we have at least one option.
   */
  function setFromLogin(items: WorkspaceItem[]): void {
    const filtered = items.filter((w) => !isInternalName(w.name));
    setList(filtered);
    if (!activeWorkspaceId.value && filtered.length > 0) {
      setActive(filtered[0].id);
    }
  }

  /**
   * Legacy no-op retained for any caller that imported it before
   * `/me/workspaces` shipped. Used to inject a hard-coded
   * `00000000-...-0000` placeholder — that path sent bogus chat
   * requests and got 403 from `_resolve_workspace_id`. Now the
   * function simply bails out and lets the caller decide (Chat.vue
   * renders an empty-state when `visibleWorkspaces.length === 0`).
   */
  function ensureFallback(): void {
    if (workspaces.value.length > 0) return;
    // Deliberately a no-op: do NOT fabricate a workspace id.
  }

  function clear(): void {
    workspaces.value = [];
    activeWorkspaceId.value = null;
    persistActive(null);
  }

  return {
    workspaces,
    activeWorkspaceId,
    activeWorkspace,
    visibleWorkspaces,
    setList,
    setActive,
    setFromLogin,
    ensureFallback,
    clear,
  };
});