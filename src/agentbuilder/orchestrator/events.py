"""Events exchanged between the orchestrator and a UI (see design/01-architecture.md)."""

from typing import Literal

from pydantic import BaseModel


class UserMessage(BaseModel):
    """User -> orchestrator: free text typed in the chat input."""

    kind: Literal["user_message"] = "user_message"
    text: str


class AssistantDelta(BaseModel):
    """Orchestrator -> UI: a chunk of streamed assistant text."""

    kind: Literal["assistant_delta"] = "assistant_delta"
    text: str


class AssistantMessage(BaseModel):
    """Orchestrator -> UI: the assistant's reply is complete."""

    kind: Literal["assistant_message"] = "assistant_message"
    text: str


class Error(BaseModel):
    """Orchestrator -> UI: the turn failed."""

    kind: Literal["error"] = "error"
    message: str


type UIEvent = AssistantDelta | AssistantMessage | Error
