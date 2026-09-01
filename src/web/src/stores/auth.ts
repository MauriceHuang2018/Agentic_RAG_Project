// auth store — holds JWT + user context + permission set.
// Login response shape (see backend api_gateway/router.py:44-86):
//   { access_token, token_type: "bearer", user: { id, username, is_super_admin, status } }
// JWT decode is intentionally NOT done here: signature verification lives on
// the server. We only inspect the payload client-side for `is_super_admin`
// to short-circuit the rbac.guard.
//
// T6.1 note: backend does not yet expose /me or /workspaces endpoints
// (Explore agent 2026-09-01). User/perm state therefore bootstraps solely
// from the login response until backend fills the gap.

import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { PERMISSION_WILDCARD } from '@/constants/permissions';
import { login as apiLogin, logout as apiLogout } from '@/api/endpoints/auth';

const TOKEN_STORAGE_KEY = 'agentic_rag.access_token';

export interface AuthUser {
  id: string;
  username: string;
  isSuperAdmin: boolean;
  status: 'enable' | 'disable';
}

function readStoredToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

function persistToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_STORAGE_KEY, token);
    else localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // localStorage unavailable (e.g. SSR) — silent, caller handles.
  }
}

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(readStoredToken());
  const user = ref<AuthUser | null>(null);
  const permissions = ref<string[]>([]);

  const isAuthenticated = computed<boolean>(() => Boolean(accessToken.value && user.value));
  const isSuperAdmin = computed<boolean>(() => Boolean(user.value?.isSuperAdmin));
  const userId = computed<string | null>(() => user.value?.id ?? null);
  const username = computed<string | null>(() => user.value?.username ?? null);

  /**
   * Submit credentials and bootstrap session. Throws on 401 / 403 so the
   * Login.vue component can render the i18n error message.
   */
  async function loginWithCredentials(identity: string, password: string): Promise<void> {
    const result = await apiLogin({ username: identity, password });
    accessToken.value = result.accessToken;
    user.value = {
      id: result.user.id,
      username: result.user.username,
      isSuperAdmin: Boolean(result.user.isSuperAdmin),
      status: result.user.status,
    };
    permissions.value = extractPermsFromUser(result.user);
    persistToken(result.accessToken);
  }

  /** Server-side logout (no-op response) + local state wipe. */
  async function logout(): Promise<void> {
    try {
      await apiLogout();
    } catch {
      // Network failures during logout must not block local cleanup.
    }
    clear();
  }

  /** Local state wipe only; use after API 401 from axios interceptor. */
  function clear(): void {
    accessToken.value = null;
    user.value = null;
    permissions.value = [];
    persistToken(null);
  }

  /** Re-evaluate after the backend starts returning a permission set on /me. */
  function setPermissions(next: string[]): void {
    permissions.value = [...new Set(next)];
  }

  return {
    accessToken,
    user,
    permissions,
    isAuthenticated,
    isSuperAdmin,
    userId,
    username,
    loginWithCredentials,
    logout,
    clear,
    setPermissions,
  };
});

/**
 * Backend /auth/login response carries no `permissions` field today; the set
 * is reconstructed by `_resolve_user_context` server-side on each request.
 * Until backend starts returning it, we conservatively grant only the
 * wildcard to super_admin so the rbac.guard short-circuits for them.
 */
function extractPermsFromUser(user: AuthUser): string[] {
  return user.isSuperAdmin ? [PERMISSION_WILDCARD] : [];
}