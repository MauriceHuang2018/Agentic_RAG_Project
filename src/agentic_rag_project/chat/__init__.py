"""Chat package — service + DTOs for the `POST /chat/query` endpoint.

Wiring is split across three modules:

  * `schema`      — Pydantic request/response models
  * `chat_service`— orchestration logic with full collaborator DI
  * (router)      — see `api_gateway.chat_router` for the FastAPI shell

The HTTP shell is in `api_gateway` because that's where the auth /
RBAC dependencies already live; pulling it here would force this
package to import from `api_gateway`, creating a cycle.
"""

from agentic_rag_project.chat.chat_service import (
    AgentPathOutcome,
    ChatService,
    ChatServiceError,
    DirectPathOutcome,
    EmptyQueryError,
    LLMSynthesizer,
)
from agentic_rag_project.chat.default_synthesizer import DefaultLLMSynthesizer
from agentic_rag_project.chat.schema import (
    ChatQueryRequest,
    ChatQueryResponse,
    CitationItem,
    StepItem,
)

__all__ = [
    "AgentPathOutcome",
    "ChatQueryRequest",
    "ChatQueryResponse",
    "ChatService",
    "ChatServiceError",
    "CitationItem",
    "DefaultLLMSynthesizer",
    "DirectPathOutcome",
    "EmptyQueryError",
    "LLMSynthesizer",
    "StepItem",
]
