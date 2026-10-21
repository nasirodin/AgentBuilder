"""Session: owns the conversation and routes user messages to the main agent.

This is the minimal seed of the phase state machine described in design/02-builder-agent.md;
for now every message goes to the main (front desk) agent.
"""

from collections.abc import AsyncIterator

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

from agentbuilder.orchestrator.events import (
    AssistantDelta,
    AssistantMessage,
    Error,
    UIEvent,
    UserMessage,
)


class Session:
    def __init__(self, agent: Agent[None, str]) -> None:
        self.agent = agent
        self.history: list[ModelMessage] = []

    async def send(self, message: UserMessage) -> AsyncIterator[UIEvent]:
        """Run one turn and yield UI events as the reply streams in."""
        chunks: list[str] = []
        try:
            async with self.agent.run_stream(message.text, message_history=self.history) as result:
                # No debounce: forward chunks as they arrive; the UI batches its own redraws.
                async for delta in result.stream_text(delta=True, debounce_by=None):
                    chunks.append(delta)
                    yield AssistantDelta(text=delta)
                self.history = result.all_messages()
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, history left unchanged
            yield Error(message=f"{type(exc).__name__}: {exc}")
            return
        yield AssistantMessage(text="".join(chunks))
