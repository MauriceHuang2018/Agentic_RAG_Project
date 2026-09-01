// RBAC permission key constants. Mirror `rbac/seed.py::PERMISSION_KEYS`.
// Sync via `scripts/gen-perm-keys-sync.sh`; manual edit discouraged.

export const PERMISSION_KEYS = [
  // Document
  'doc:read',
  'doc:write',
  'doc:delete',
  'doc:reindex',

  // Knowledge base
  'kb:read',
  'kb:write',

  // Feedback
  'feedback:submit',
  'feedback:read',
  'feedback:ticket:transition',
  'feedback-config:read',
  'feedback-config:write',

  // Chat
  'chat:ask',
  'chat:history:read',

  // Audit
  'audit:read',

  // Sensitive
  'sensitive:read',
  'sensitive:update',

  // Role / User / Workspace
  'role:read',
  'role:write',
  'role:delete',
  'role:assign',
  'user:read',
  'user:write',
  'user:delete',
  'workspace:read',
  'workspace:write',
  'workspace:create',

  // Evaluation / Drift / CSAT
  'evaluation:read',
  'drift:read',
  'drift:ack',
  'csat:read',

  // Self-service
  'me:read',
  'me:write',
] as const;

export type PermissionKey = (typeof PERMISSION_KEYS)[number];

/** Check whether a permKey is in the whitelist (compile-time + runtime guard). */
export function isKnownPermission(key: string): key is PermissionKey {
  return (PERMISSION_KEYS as readonly string[]).includes(key);
}

/** Wildcard used by super_admin short-circuit (`'*'`); see backend dependencies.py:250. */
export const PERMISSION_WILDCARD = '*';