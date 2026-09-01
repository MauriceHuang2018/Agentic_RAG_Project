// permission store — thin wrapper exposing `can(key)` for use in templates
// and component-level guards. Reads from auth store so it stays a single
// source of truth. The router-level rbac.guard consumes auth.permissions
// directly for performance; this store exists for in-component checks
// (e.g. v-if="perm.can('doc:write')" on action buttons).

import { defineStore } from 'pinia';
import { computed } from 'vue';
import { useAuthStore } from '@/stores/auth';
import { PERMISSION_WILDCARD } from '@/constants/permissions';

export const usePermissionStore = defineStore('permission', () => {
  const auth = useAuthStore();

  const isSuperAdmin = computed<boolean>(() => auth.isSuperAdmin);

  /**
   * Returns true when the current user has the given perm key.
   * Super-admin wildcard (`'*'`) is honored regardless of explicit membership.
   */
  function can(key: string): boolean {
    if (isSuperAdmin.value) return true;
    return auth.permissions.includes(PERMISSION_WILDCARD) || auth.permissions.includes(key);
  }

  /**
   * Returns true only when the user has *all* listed keys.
   * Useful for compound gates like "view AND edit this resource".
   */
  function canAll(keys: readonly string[]): boolean {
    return keys.every(can);
  }

  /**
   * Returns true when the user has at least one of the listed keys.
   * Mirrors the OR semantics used by rbac.guard for meta.permKeys[].
   */
  function canAny(keys: readonly string[]): boolean {
    return keys.some(can);
  }

  return { isSuperAdmin, can, canAll, canAny };
});