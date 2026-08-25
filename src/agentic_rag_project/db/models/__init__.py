"""SQLAlchemy ORM models for the 17 PG tables defined in DESIGN 4.1,
plus M6 additions (3 new tables + 5 new columns).

Split by domain to keep each module small:
  - base          shared declarative Base + mixins
  - users         User, Workspace, ApiToken, PasswordResetAudit
  - rbac          Role, Permission, RolePermission, UserRole
  - documents     Document, Chunk, ACL
  - conversations Conversation, Message, Citation
  - feedback      Feedback, FeedbackTag, FeedbackCategory
  - audit         AuditLog, EvaluationResult, DriftAlert
  - sensitive     SensitiveValue (M6)
"""

from agentic_rag_project.db.models.base import Base
from agentic_rag_project.db.models.users import (
    ApiToken,
    PasswordResetAudit,
    User,
    Workspace,
)
from agentic_rag_project.db.models.rbac import (
    Permission,
    Role,
    RolePermission,
    UserRole,
)
from agentic_rag_project.db.models.documents import ACL, Chunk, Document
from agentic_rag_project.db.models.conversations import (
    Citation,
    Conversation,
    Message,
)
from agentic_rag_project.db.models.feedback import (
    Feedback,
    FeedbackCategory,
    FeedbackTag,
)
from agentic_rag_project.db.models.audit import (
    AuditLog,
    DriftAlert,
    EvaluationResult,
)
from agentic_rag_project.db.models.sensitive import SensitiveValue

__all__ = [
    "Base",
    # users (+ M6 user_extras)
    "User",
    "Workspace",
    "ApiToken",
    "PasswordResetAudit",
    # rbac
    "Role",
    "Permission",
    "RolePermission",
    "UserRole",
    # documents
    "Document",
    "Chunk",
    "ACL",
    # conversations
    "Conversation",
    "Message",
    "Citation",
    # feedback
    "Feedback",
    "FeedbackTag",
    "FeedbackCategory",
    # audit
    "AuditLog",
    "EvaluationResult",
    "DriftAlert",
    # M6 sensitive info
    "SensitiveValue",
]