"""Audit — WORM audit log writer & query API (DESIGN 2.2 #11).

Append-only audit_logs table (UPDATE/DELETE blocked at DB trigger layer).
Redis buffer flushes asynchronously. Query API under `/admin/audit-logs`.
Implemented in T5.6.
"""