"""Views derived from a trace's Pydantic AI message history.

The trace stores only the raw history; tool calls and their outcomes are read back from it
here, so there is one source of truth for what the agent did.
"""

from collections.abc import Collection
from dataclasses import dataclass, field

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from agentbuilder_runtime.trace import Trace

DEFAULT_OUTPUT_TOOLS = frozenset({"final_result"})
"""Pydantic AI's name for the tool that delivers structured output; it is not a real tool."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, object] = field(default_factory=dict)
    ok: bool = False
    """A successful return was recorded. False when the tool asked for a retry, failed, was
    denied, or the run ended before it returned."""
    error: str | None = None
    """The retry prompt or failure content sent back to the model."""


def messages(trace: Trace) -> list[ModelMessage]:
    return ModelMessagesTypeAdapter.validate_python(trace.messages)


def tool_calls(
    trace: Trace, *, output_tools: Collection[str] = DEFAULT_OUTPUT_TOOLS
) -> list[ToolCall]:
    """The agent's function tool calls in call order, excluding output tools."""
    history = messages(trace)
    calls: list[ToolCallPart] = []
    results: dict[str, ToolReturnPart | RetryPromptPart] = {}
    for message in history:
        for part in message.parts:
            if isinstance(message, ModelResponse) and isinstance(part, ToolCallPart):
                if part.tool_name not in output_tools:
                    calls.append(part)
            elif isinstance(part, ToolReturnPart | RetryPromptPart) and part.tool_call_id:
                results[part.tool_call_id] = part

    out: list[ToolCall] = []
    for call in calls:
        result = results.get(call.tool_call_id)
        ok = isinstance(result, ToolReturnPart) and result.outcome == "success"
        error: str | None = None
        if isinstance(result, RetryPromptPart):
            error = result.model_response()
        elif isinstance(result, ToolReturnPart) and not ok:
            error = str(result.content)
        out.append(ToolCall(name=call.tool_name, args=call.args_as_dict(), ok=ok, error=error))
    return out
