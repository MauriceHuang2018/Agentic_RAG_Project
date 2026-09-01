// workspace store — tracks the active workspace and the workspace list.
//
// Backend status (Explore agent 2026-09-01):
//   - GET /api/v1/workspaces does NOT exist.
//   - UserContext.workspace_ids is constructed inside get_current_user and
//     not exposed via any HTTP endpoint yet.
//   - JWT payload does NOT carry workspace_ids.
//
// Until backend adds the list endpoint, the store accepts workspaces via
// setFromLogin() (future use) and falls back to a single "primary" workspace
// derived from the first non-`__system__` membership. This is a deliberate
// "best-effort" state — chat requests will return 403 if the guessed id is
// wrong, and the user will see a toast prompting them to contact an admin.

import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { isInternalName } from '@/constants/internals';

export interface WorkspaceItem {
  id: string;
  name: string;
  isolationLevel?: 'logical' | 'physical';
  status?: 'enable' | 'disable';
}

const PRIMARY_FALLBACK_NAME = 'primary';
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
   * Bootstrap from login (when backend adds workspace_ids to login response).
   * Drops `__system__` rows per backend `_resolve_user_context` semantics.
   */
  function setFromLogin(items: WorkspaceItem[]): void {
    const filtered = items.filter((w) => !isInternalName(w.name));
    setList(filtered);
    if (!activeWorkspaceId.value && filtered.length > 0) {
      setActive(filtered[0].id);
    }
  }

  /**
   * Last-resort fallback: when neither backend nor local cache has any
   * workspace data, create a single "primary" placeholder. The next /chat/query
   * will either succeed (backend default workspace) or 403 with a clear toast.
   */
  function ensureFallback(): void {
    if (workspaces.value.length > 0) return;
    const fallback: WorkspaceItem = {
      id: '00000000-0000-0000-0000-000000000000',
      name: PRIMARY_FALLBACK_NAME,
      isolationLevel: 'logical',
      status: 'enable',
    };
    workspaces.value = [fallback];
    if (!activeWorkspaceId.value) setActive(fallback.id);
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