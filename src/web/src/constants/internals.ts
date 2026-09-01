// Internal-name prefix. Mirror `agentic_rag_project.rbac.constants.INTERNAL_NAME_PREFIX = "__"`.
// Used by Page 11 (users) / Page 12 (workspaces) / Page 9 (roles) to filter
// placeholder rows like `__system_owner__` / `__system__`. DO NOT hard-code.

export const INTERNAL_NAME_PREFIX = '__';

/** Returns true when `name` starts with the internal placeholder prefix. */
export function isInternalName(name: string | null | undefined): boolean {
  return typeof name === 'string' && name.startsWith(INTERNAL_NAME_PREFIX);
}