// Unit tests for the permission store. Covers:
//   - can(key) honors the super-admin shortcut even when the explicit
//     permissions list is empty
//   - can(key) honors the wildcard short-circuit (a user with only '*'
//     is treated as super-equivalent)
//   - can(key) does a plain `Array.includes` lookup for non-super_admin
//   - canAll / canAny mirror the rbac.guard semantics (AND/OR)
//   - non-super_admin + empty permissions rejects every key (the state
//     before /me/permissions resolves — also the state that hid the
//     original rbac bug for alice)
//
// The auth store is consumed directly because Pinia provides DI: each
// usePermissionStore() call inside an `it` block resolves the same
// singleton auth store created by `setActivePinia(createPinia())` in
// setup.ts.

import { beforeEach, describe, it, expect } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useAuthStore } from '@/stores/auth';
import { usePermissionStore } from '@/stores/permission';
import { PERMISSION_WILDCARD } from '@/constants/permissions';

describe('permission store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
  });

  it('can() returns true for any key when isSuperAdmin is true', () => {
    const auth = useAuthStore();
    auth.user = {
      id: 'u1',
      username: 'root',
      isSuperAdmin: true,
      status: 'enable',
    };
    // Even without the wildcard in `permissions` (loginWithCredentials
    // seeds it, but a state where it's missing must still pass).
    auth.permissions = [];

    const perm = usePermissionStore();
    expect(perm.can('chat:ask')).toBe(true);
    expect(perm.can('kb:delete')).toBe(true);
    expect(perm.can('does:not:exist')).toBe(true);
  });

  it('can() honors the wildcard short-circuit for non-super_admin', () => {
    const auth = useAuthStore();
    auth.user = {
      id: 'u2',
      username: 'special',
      isSuperAdmin: false,
      status: 'enable',
    };
    auth.permissions = [PERMISSION_WILDCARD];

    const perm = usePermissionStore();
    expect(perm.can('chat:ask')).toBe(true);
    expect(perm.can('kb:delete')).toBe(true);
  });

  it('can() returns true for keys the user explicitly holds', () => {
    const auth = useAuthStore();
    auth.user = {
      id: 'u3',
      username: 'alice',
      isSuperAdmin: false,
      status: 'enable',
    };
    auth.permissions = ['doc:read', 'chat:ask', 'chat:history:read', 'feedback:submit'];

    const perm = usePermissionStore();
    expect(perm.can('chat:ask')).toBe(true);
    expect(perm.can('feedback:submit')).toBe(true);
    expect(perm.can('kb:delete')).toBe(false);
  });

  it('can() returns false for keys the user does not hold (the pre-bug state)', () => {
    const auth = useAuthStore();
    auth.user = {
      id: 'u4',
      username: 'stranger',
      isSuperAdmin: false,
      status: 'enable',
    };
    auth.permissions = [];

    const perm = usePermissionStore();
    // This is exactly what blocked alice from /chat before /me/permissions
    // was wired up: rbac.guard saw `[]` and redirected to /forbidden.
    expect(perm.can('chat:ask')).toBe(false);
    expect(perm.can('kb:delete')).toBe(false);
    expect(perm.can('doc:read')).toBe(false);
  });

  it('canAll() requires every key (AND semantics)', () => {
    const auth = useAuthStore();
    auth.user = {
      id: 'u5',
      username: 'bob',
      isSuperAdmin: false,
      status: 'enable',
    };
    auth.permissions = ['doc:read', 'doc:write', 'kb:read'];

    const perm = usePermissionStore();
    expect(perm.canAll(['doc:read', 'doc:write'])).toBe(true);
    expect(perm.canAll(['doc:read', 'kb:delete'])).toBe(false);
    expect(perm.canAll([])).toBe(true);
  });

  it('canAny() accepts at least one key (OR semantics)', () => {
    const auth = useAuthStore();
    auth.user = {
      id: 'u6',
      username: 'bob',
      isSuperAdmin: false,
      status: 'enable',
    };
    auth.permissions = ['kb:read'];

    const perm = usePermissionStore();
    expect(perm.canAny(['kb:read', 'kb:delete'])).toBe(true);
    expect(perm.canAny(['kb:delete', 'doc:read'])).toBe(false);
    expect(perm.canAny([])).toBe(false);
  });

  it('isSuperAdmin mirrors the auth store flag', () => {
    const auth = useAuthStore();
    const perm = usePermissionStore();
    expect(perm.isSuperAdmin).toBe(false);

    auth.user = {
      id: 'u7',
      username: 'root',
      isSuperAdmin: true,
      status: 'enable',
    };
    expect(perm.isSuperAdmin).toBe(true);
  });
});
