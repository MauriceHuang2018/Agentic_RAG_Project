// Placeholder unit test for the auth store. Covers the simplest contract:
//   - login() persists token + user + permission wildcard for super_admin
//   - logout() wipes state and clears localStorage
//
// Backend /auth/login response shape is fixed by api_gateway/router.py:44-86
// (snake_case fields); auth.ts maps to camelCase. Any drift fails this test
// immediately.

import { beforeEach, describe, it, expect, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useAuthStore } from '@/stores/auth';

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

describe('auth store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
  });

  it('logs in, persists token, marks super-admin', async () => {
    const auth = useAuthStore();
    await auth.loginWithCredentials('alice', 'hunter2');

    expect(auth.isAuthenticated).toBe(true);
    expect(auth.isSuperAdmin).toBe(true);
    expect(auth.username).toBe('alice');
    expect(localStorage.getItem('agentic_rag.access_token')).toBe('test-jwt');
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
});