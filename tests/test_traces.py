from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, ToolCallPart
from pydantic_ai.models.test import TestModel

from agentbuilder.evals.traces import ToolCall, tool_calls
from agentbuilder_runtime import Trace


class Output(BaseModel):
    result: float


def trace_of(agent: Agent[None, Output]) -> Trace:
    """Run like the probe runner does and keep only the JSON history."""
    result = agent.run_sync("What is 1 + 1?")
    return Trace(
        probe_id="p1",
        scenario_id=None,
        input="What is 1 + 1?",
        model="test",
        messages=ModelMessagesTypeAdapter.dump_python(result.all_messages(), mode="json"),
        output=result.output.model_dump(mode="json"),
        duration_s=0,
    )


def test_tool_calls_with_retry_and_output_tool() -> None:
    agent = Agent(TestModel(), output_type=Output)
    attempts = 0

    @agent.tool_plain
    def evaluate(expression: str) -> float:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelRetry("cannot parse expression")
        return 2.0

    calls = tool_calls(trace_of(agent))
    # The structured-output call (`final_result`) is not a tool call of the agent.
    assert [(c.name, c.ok) for c in calls] == [("evaluate", False), ("evaluate", True)]
    assert calls[0].args == {"expression": "a"}
    assert calls[0].error is not None and "cannot parse expression" in calls[0].error
    assert calls[1].error is None


def test_call_without_return_is_not_ok() -> None:
    # A run killed mid-tool: the call is recorded, its return never is.
    response = ModelResponse(parts=[ToolCallPart("evaluate", {"expression": "1/0"}, "c1")])
    trace = Trace(
        probe_id="p1",
        scenario_id=None,
        input="x",
        model="test",
        messages=ModelMessagesTypeAdapter.dump_python([response], mode="json"),
        duration_s=30,
    )
    assert tool_calls(trace) == [ToolCall(name="evaluate", args={"expression": "1/0"}, ok=False)]
