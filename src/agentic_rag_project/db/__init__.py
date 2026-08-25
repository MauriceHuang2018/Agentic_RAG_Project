"""Database — SQLAlchemy ORM models, Alembic migrations, Qdrant init.

Hosts the 17 PG tables defined in DESIGN 4.1 (users, workspaces, roles,
permissions, role_permissions, user_roles, documents, chunks, acls,
conversations, messages, citations, feedbacks, feedback_tags,
feedback_categories, audit_logs, evaluation_results, drift_alerts).
Implemented in T1.3 (Alembic) / T2.1 (Qdrant).
"""