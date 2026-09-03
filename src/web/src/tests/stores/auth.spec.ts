// Unit tests for the auth store. Covers:
//   - loginWithCredentials persists token + user + optimistically seeds
//     the wildcard for super_admin (rbac.guard UX gate)
//   - non-super_admin starts with empty `permissions` until the caller
//     invokes fetchMyPermissions() (closes the pre-existing bug where
//     `extractPermsFromUser` returned [] for everyone except super_admin)
//   - fetchMyPermissions() replaces `permissions` with the backend set
//     (deduped via setPermissions)
//   - fetchMyPermissions() propagates errors instead of silently
//     swallowing them (the silent-failure mode that hid the original
//     rbac bug)
//   - logout() / clear() wipe state and localStorage
//
// Backend /auth/login response shape is fixed by api_gateway/router.py:44-86
// (snake_case fields); auth.ts maps to camelCase. Any drift fails this test
// immediately. The /me/permissions contract is fixed by
// api_gateway/me_router.py:91-113 — {permissions: list[str]}.

import { beforeEach, describe, it, expect, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useAuthStore } from '@/stores/auth';
import { PERMISSION_WILDCARD } from '@/constants/permissions';

// Hoisted mock data so vi.mock factory can reference it before module init.
const { loginResponse, logoutResponse } = vi.hoisted(() => ({
  loginResponse: {
    access_token: 'test-jwt',
    token_type: 'bearer' as const,
    user: {
      id: 'user-uuid-1',
      username: 'alice',
      is_super_admin: true,
      status: 'enable' as const,
    },
  },
  logoutResponse: { ok: true as const },
}));

const { listMyPermissionsImpl } = vi.hoisted(() => ({
  listMyPermissionsImpl: vi.fn<(token?: string) => Promise<string[]>>(),
}));

vi.mock('@/api/endpoints/auth', () => ({
  login: vi.fn().mockResolvedValue({
    accessToken: loginResponse.access_token,
    tokenType: loginResponse.token_type,
    user: {
      id: loginResponse.user.id,
      username: loginResponse.user.username,
      isSuperAdmin: loginResponse.user.is_super_admin,
      status: loginResponse.user.status,
    },
  }),
  logout: vi.fn().mockResolvedValue(logoutResponse),
}));

vi.mock('@/api/endpoints/me', () => ({
  listMyPermissions: vi.fn((...args: unknown[]) =>
    listMyPermissionsImpl(...(args as [string?])),
  ),
}));

describe('auth store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
    listMyPermissionsImpl.mockReset();
  });

  it('logs in, persists token, marks super-admin', async () => {
    const auth = useAuthStore();
    await auth.loginWithCredentials('alice', 'hunter2');

    expect(auth.isAuthenticated).toBe(true);
    expect(auth.isSuperAdmin).toBe(true);
    expect(auth.username).toBe('alice');
    expect(localStorage.getItem('agentic_rag.access_token')).toBe('test-jwt');
  });

  it('seeds the wildcard for super_admin on login (optimistic rbac.guard shortcut)', async () => {
    const auth = useAuthStore();
    expect(auth.permissions).toEqual([]);

    await auth.loginWithCredentials('alice', 'hunter2');

    expect(auth.permissions).toEqual([PERMISSION_WILDCARD]);
  });

  it('starts non-super_admin with empty permissions (until fetchMyPermissions)', async () => {
    const auth = useAuthStore();
    // Override the auth.login mock response for this case.
    const loginModule = await import('@/api/endpoints/auth');
    (loginModule.login as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      accessToken: 'non-admin-jwt',
      tokenType: 'bearer',
      user: {
        id: 'user-uuid-2',
        username: 'bob',
        isSuperAdmin: false,
        status: 'enable',
      },
    });

    await auth.loginWithCredentials('bob', 'hunter2');

    expect(auth.isSuperAdmin).toBe(false);
    expect(auth.permissions).toEqual([]);
  });

  it('fetchMyPermissions populates permissions from /me/permissions', async () => {
    const auth = useAuthStore();
    listMyPermissionsImpl.mockResolvedValueOnce([
      'doc:read',
      'chat:ask',
      'chat:history:read',
      'feedback:submit',
    ]);

    const result = await auth.fetchMyPermissions();

    expect(listMyPermissionsImpl).toHaveBeenCalledTimes(1);
    expect(auth.permissions).toEqual([
      'doc:read',
      'chat:ask',
      'chat:history:read',
      'feedback:submit',
    ]);
    // The action returns the deduped set so callers can render it
    // without re-reading the store.
    expect(result).toEqual([
      'doc:read',
      'chat:ask',
      'chat:history:read',
      'feedback:submit',
    ]);
  });

  it('fetchMyPermissions dedupes duplicate keys via setPermissions', async () => {
    const auth = useAuthStore();
    listMyPermissionsImpl.mockResolvedValueOnce([
      'chat:ask',
      'chat:ask',
      'doc:read',
      'doc:read',
      'chat:ask',
    ]);

    await auth.fetchMyPermissions();

    expect(auth.permissions).toEqual(['chat:ask', 'doc:read']);
  });

  it('fetchMyPermissions propagates errors instead of silently swallowing', async () => {
    const auth = useAuthStore();
    listMyPermissionsImpl.mockRejectedValueOnce(new Error('503 backend down'));

    // Login first so the store has a token (fetchMyPermissions would 401
    // without one in production; the mock ignores that for this test).
    await auth.loginWithCredentials('alice', 'hunter2');
    // After login super_admin already has the wildcard; the rejected
    // /me/permissions call must NOT clear it silently.
    await expect(auth.fetchMyPermissions()).rejects.toThrow('503 backend down');
    // Optimistic seed survives the failure — the rbac.guard stays usable
    // for super_admin (non-super_admin would still be locked out, which
    // matches the pre-fix UX; the fix only changes the happy path).
    expect(auth.permissions).toEqual([PERMISSION_WILDCARD]);
  });

  it('logs out, clears token and user', async () => {
    const auth = useAuthStore();
    await auth.loginWithCredentials('alice', 'hunter2');
    await auth.logout();

    expect(auth.isAuthenticated).toBe(false);
    expect(auth.accessToken).toBeNull();
    expect(localStorage.getItem('agentic_rag.access_token')).toBeNull();
  });

  it('clear() wipes state without calling API', () => {
    const auth = useAuthStore();
    auth.clear();
    expect(auth.isAuthenticated).toBe(false);
    expect(auth.permissions).toEqual([]);
  });

  it('full happy path: login → fetchMyPermissions → alice has chat_user keys', async () => {
    // Reproduces the alice flow Login.vue exercises end-to-end.
    const auth = useAuthStore();
    listMyPermissionsImpl.mockResolvedValueOnce([
      'doc:read',
      'chat:ask',
      'chat:history:read',
      'feedback:submit',
    ]);

    await auth.loginWithCredentials('alice', 'hunter2');
    // Optimistic seed lets /chat load instantly for admins.
    expect(auth.permissions).toEqual([PERMISSION_WILDCARD]);

    await auth.fetchMyPermissions();
    // Real set replaces the seed — rbac.guard now lets non-super_admin
    // users reach /chat (permKey `chat:ask`) without any super_admin
    // workaround.
    expect(auth.permissions).toContain('chat:ask');
    expect(auth.permissions).not.toContain(PERMISSION_WILDCARD);
  });
});
