"""ACL Filter — Qdrant payload pre-filter builder (DESIGN 2.2 #8).

Three-layer permission resolution:
  1. super_admin (USER.is_super_admin) — global visibility
  2. workspace membership (USER_ROLE.workspace_id)
  3. document owner (auto-granted)
  4. explicit ACL entries (highest precedence)

Failure-closed: if filter construction fails, queries return empty rather
than leaking documents. Implemented in T5.3.
"""