"""Output post-processor (T3.4).

DESIGN 4.2 — every assistant answer passes through two stages before
it goes back to the user:

  1. `SensitiveWordFilter`  — refuses the answer if it contains a
     banned phrase. The list is hot-loaded from Redis so admins can
     update it without a deploy.
  2. `Masker`               — redacts phone numbers, ID cards, and
     emails so PII never leaves the API.

DESIGN §4.6.1 (M6 / Page 15): the source of truth for the word list
is the `sensitive_values` PG table; `sensitive_sync` keeps Redis
+ the in-process filter in lock-step with that table.
"""

from agentic_rag_project.post_processor.filter import (
    DEFAULT_REFUSAL_TEXT,
    DEFAULT_SENSITIVE_WORDS,
    SENSITIVE_WORDS_REDIS_KEY,
    SensitiveWordFilter,
    build_default_filter,
)
from agentic_rag_project.post_processor.mask import (
    DEFAULT_EMAIL_PATTERN,
    DEFAULT_ID_PATTERN,
    DEFAULT_MASK_CHAR,
    DEFAULT_PHONE_PATTERN,
    Masker,
    build_default_masker,
)
from agentic_rag_project.post_processor.sensitive_sync import (
    SyncResult,
    get_default_filter,
    list_effective_words,
    sensitive_sync,
    set_default_filter,
)

__all__ = [
    # filter
    "DEFAULT_EMAIL_PATTERN",
    "DEFAULT_ID_PATTERN",
    "DEFAULT_MASK_CHAR",
    "DEFAULT_PHONE_PATTERN",
    "DEFAULT_REFUSAL_TEXT",
    "DEFAULT_SENSITIVE_WORDS",
    "Masker",
    "SENSITIVE_WORDS_REDIS_KEY",
    "SensitiveWordFilter",
    "build_default_filter",
    "build_default_masker",
    # M6 / Page 15 sync
    "SyncResult",
    "get_default_filter",
    "list_effective_words",
    "sensitive_sync",
    "set_default_filter",
]