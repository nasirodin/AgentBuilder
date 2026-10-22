import json
import shutil
import sys
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from agentbuilder.builder.agents import build_main_agent
from agentbuilder.execution import SandboxExecutor, format_result, sandbox_toolset
from agentbuilder.orchestrator.events import AssistantMessage, ToolCall, ToolResult, UserMessage
from agentbuilder.orchestrator.session import Session
from agentbuilder.tui.app import AgentBuilderApp, AssistantBubble, ToolBubble
from agentbuilder_sandbox import ExecResult, SandboxUnavailable, Violation


class FakeExecutor(SandboxExecutor):
    def __init__(self, tmp_path: Path, error: str | None = None) -> None:
        super().__init__(tmp_path / "session")
        self.calls: list[tuple[list[str], bytes | None, float]] = []
        self.error = error

    async def run(
        self, cmd: list[str], *, stdin: bytes | None = None, timeout_s: float = 60
    ) -> ExecResult:
        self.calls.append((cmd, stdin, timeout_s))
        if self.error:
            raise SandboxUnavailable(self.error)
        return ExecResult(
            exit_code=0,
            stdout="hi\n",
            stderr="",
            truncated=False,
            violations=[Violation(kind="network", detail="deny http x.com:80 (no)")],
            duration_s=0.25,
            timed_out=False,
        )


def tool_then_text(tool: str, args: dict[str, object]):
    """A streaming model that calls `tool` once, then answers with the tool's result."""

    async def stream(messages: list[ModelMessage], info: AgentInfo):
        returns = [
            p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)
        ]
        if not returns:
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args))}
            return
        yield f"Got: {returns[-1].content}"

    return FunctionModel(stream_function=stream)


def test_format_result() -> None:
    text = format_result(
        ExecResult(
            exit_code=124,
            stdout="out",
            stderr="err\n",
            truncated=False,
            violations=[Violation(kind="filesystem", detail="Read-only file system")],
            duration_s=1.0,
            timed_out=True,
        )
    )
    assert text.startswith("exit code: 124 (1.0s) - TIMED OUT")
    assert "stdout:\nout" in text and "stderr:\nerr" in text
    assert "- filesystem: Read-only file system" in text


async def test_session_runs_tools_and_reports_events(tmp_path: Path) -> None:
    executor = FakeExecutor(tmp_path)
    agent = build_main_agent(
        tool_then_text("run_bash", {"command": "echo hi"}), toolsets=[sandbox_toolset(executor)]
    )
    events = [ev async for ev in Session(agent).send(UserMessage(text="run it"))]

    assert executor.calls == [(["bash", "-c", "echo hi"], None, 60)]
    call = next(ev for ev in events if isinstance(ev, ToolCall))
    result = next(ev for ev in events if isinstance(ev, ToolResult))
    assert call.name == "run_bash" and call.args == {"command": "echo hi"}
    assert result.id == call.id and "stdout:\nhi" in result.content
    assert "- network: deny http x.com:80 (no)" in result.content
    assert isinstance(events[-1], AssistantMessage) and events[-1].text.startswith(
        "Got: exit code: 0"
    )


async def test_unavailable_sandbox_runs_nothing(tmp_path: Path) -> None:
    executor = FakeExecutor(tmp_path, error="the sandbox needs Linux")
    agent = build_main_agent(
        tool_then_text("run_bash", {"command": "ls"}), toolsets=[sandbox_toolset(executor)]
    )
    events = [ev async for ev in Session(agent).send(UserMessage(text="ls"))]
    result = next(ev for ev in events if isinstance(ev, ToolResult))
    assert result.content == "The sandbox is unavailable, nothing was run: the sandbox needs Linux"


async def test_tui_shows_tool_calls(tmp_path: Path) -> None:
    agent = build_main_agent(
        tool_then_text("run_bash", {"command": "echo hi"}),
        toolsets=[sandbox_toolset(FakeExecutor(tmp_path))],
    )
    app = AgentBuilderApp(Session(agent))
    async with app.run_test() as pilot:
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        bubble = app.query_one(ToolBubble)
        assert "run_bash" in str(bubble.render()) and "stdout:" in str(bubble.render())
        assert app.query_one(AssistantBubble).source.startswith("Got: exit code: 0")


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not shutil.which("bwrap"),
    reason="needs Linux with bubblewrap",
)
async def test_real_sandbox(tmp_path: Path) -> None:
    executor = SandboxExecutor(tmp_path / "session")
    try:
        r = await executor.run(["bash", "-c", "echo hi > f.txt; cat f.txt; ls ~/..; pwd"])
        assert r.exit_code == 0, r.stderr
        assert r.stdout.startswith("hi\n") and r.stdout.strip().endswith("/session/workspace")
        assert (executor.workspace / "f.txt").read_text() == "hi\n"
        r = await executor.run(
            ["python3", "-"], stdin=b"import os; print(os.listdir(os.path.expanduser('~')))"
        )
        assert r.stdout == "[]\n"  # HOME is the session's empty scratch dir
        r = await executor.run(["bash", "-c", f"ls {Path.home()}"])
        assert r.stdout.strip() == ""  # the real home is hidden
    finally:
        await executor.aclose()
