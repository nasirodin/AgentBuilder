"""Session: owns the conversation and routes user messages to the main agent.

This is the minimal seed of the phase state machine described in design/02-builder-agent.md;
for now every message goes to the main (front desk) agent.
"""

from collections.abc import AsyncIterator

from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
)

from agentbuilder.orchestrator.events import (
    AssistantDelta,
    AssistantMessage,
    Error,
    ToolCall,
    ToolResult,
    UIEvent,
    UserMessage,
)


class Session:
    def __init__(self, agent: Agent[None, str]) -> None:
        self.agent = agent
        self.history: list[ModelMessage] = []

    async def send(self, message: UserMessage) -> AsyncIterator[UIEvent]:
        """Run one turn and yield UI events as the reply streams in.

        Uses the event stream rather than `run_stream`, which would end the run at the first
        text and skip tool calls the model makes after it.
        """
        try:
            async with self.agent.run_stream_events(
                message.text, message_history=self.history
            ) as events:
                async for ev in events:
                    match ev:
                        case PartStartEvent(part=TextPart(content=text)) if text:
                            yield AssistantDelta(text=text)
                        case PartDeltaEvent(delta=TextPartDelta(content_delta=text)) if text:
                            yield AssistantDelta(text=text)
                        case FunctionToolCallEvent(part=part):
                            yield ToolCall(
                                id=part.tool_call_id, name=part.tool_name, args=part.args_as_dict()
                            )
                        case FunctionToolResultEvent(part=part):
                            content = (
                                part.model_response()
                                if isinstance(part, RetryPromptPart)
                                else part.model_response_str()
                            )
                            yield ToolResult(
                                id=part.tool_call_id, name=part.tool_name or "", content=content
                            )
                        case AgentRunResultEvent(result=result):
                            self.history = result.all_messages()
                            yield AssistantMessage(text=result.output)
                        case _:
                            pass
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, history left unchanged
            yield Error(message=f"{type(exc).__name__}: {exc}")
