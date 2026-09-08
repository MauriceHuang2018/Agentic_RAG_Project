// auth store — holds JWT + user context + permission set.
// Login response shape (see backend api_gateway/router.py:44-86):
//   { access_token, token_type: "bearer", user: { id, username, is_super_admin, status } }
// JWT decode is intentionally NOT done here: signature verification lives on
// the server. We only inspect the payload client-side for `is_super_admin`
// to short-circuit the rbac.guard before /me/permissions resolves.
//
// Permission set lifecycle (post-M6 2026-09-02):
//   * loginWithCredentials() optimistically seeds [PERMISSION_WILDCARD]
//     when is_super_admin is true so super_admin can navigate freely
//     before /me/permissions resolves.
//   * Login.vue (and any other caller) should `await fetchMyPermissions()`
//     after login to populate `permissions` with the real key set the
//     backend aggregated across the user's workspace bindings.
//   * Authoritative permission check still lives server-side in
//     `dependencies.require_permission`; `permissions` is the rbac.guard
//     UX gate only.

import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { PERMISSION_WILDCARD } from '@/constants/permissions';
import { login as apiLogin, logout as apiLogout } from '@/api/endpoints/auth';
import { listMyPermissions } from '@/api/endpoints/me';

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
   *
   * For super_admin users we optimistically seed `permissions` with the
   * wildcard so the rbac.guard short-circuits immediately. Non-super_admin
   * users get an empty `permissions` until the caller invokes
   * `fetchMyPermissions()` (typically right after login in Login.vue).
   * This keeps the Login→/chat hop fast for admins without locking
   * non-admin users out of routes they should be allowed to enter.
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
    // Optimistic seed: super_admin gets the wildcard immediately; everyone
    // else starts empty and must call `fetchMyPermissions()` to populate.
    permissions.value = result.user.isSuperAdmin ? [PERMISSION_WILDCARD] : [];
    persistToken(result.accessToken);
  }

  /**
   * Fetch the caller's real permission set from `GET /me/permissions`
   * and replace the optimistic `permissions` seed.
   *
   * Returns the deduplicated set for callers that want to render an
   * "allowed actions" view (Page 11 etc.) without re-reading the store.
   *
   * Errors propagate — the caller (Login.vue) is responsible for
   * deciding whether a transient 5xx should fall back to the optimistic
   * seed or surface an error. We deliberately do NOT swallow the error
   * here so the silent-failure mode that hid the original rbac bug
   * (empty perms for everyone) cannot return.
   */
  async function fetchMyPermissions(): Promise<string[]> {
    const next = await listMyPermissions();
    setPermissions(next);
    return permissions.value;
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

  /** Replace the permission set with a deduplicated copy of `next`. */
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
    fetchMyPermissions,
    logout,
    clear,
    setPermissions,
  };
});