"""Textual chat UI. A thin client of the session's event stream (see design/06-tui.md)."""

import time
from typing import ClassVar

from rich.markup import escape
from rich.spinner import Spinner
from textual import work
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Markdown, Static
from textual.widgets.markdown import MarkdownStream

from agentbuilder.orchestrator.events import (
    AssistantDelta,
    AssistantMessage,
    Error,
    ToolCall,
    ToolResult,
    UserMessage,
)
from agentbuilder.orchestrator.session import Session


class UserBubble(Static):
    pass


class AssistantBubble(Markdown):
    pass


class ErrorBubble(Static):
    pass


def _clip(text: str, lines: int = 6, width: int = 200) -> str:
    rows = [r[:width] for r in text.strip().splitlines()]
    more = f"\n… ({len(rows) - lines} more lines)" if len(rows) > lines else ""
    return "\n".join(rows[:lines]) + more


class ToolBubble(Static):
    """A tool call and, once it finishes, the start of its result."""

    def __init__(self, call: ToolCall) -> None:
        args = call.args
        shown = (
            args.get("command")
            or args.get("code")
            or ", ".join(f"{k}={v!r}" for k, v in args.items())
        )
        self.header = f"[b]{call.name}[/b]  {escape(_clip(str(shown), lines=4))}"
        super().__init__(self.header + "\n[i]running…[/i]")

    def finish(self, result: ToolResult) -> None:
        self.update(self.header + "\n" + escape(_clip(result.content)))


class Thinking(Static):
    """Animated spinner with elapsed time, shown until the first reply chunk arrives."""

    def on_mount(self) -> None:
        self._started = time.monotonic()
        self._spinner = Spinner("dots")
        self.set_interval(1 / 12, self.refresh)

    def render(self) -> Spinner:
        self._spinner.update(text=f" Thinking… {time.monotonic() - self._started:.0f}s")
        return self._spinner


class AgentBuilderApp(App[None]):
    TITLE = "AgentBuilder"
    CSS_PATH = "app.tcss"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+l", "clear", "Clear screen"),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat")
        yield Input(placeholder="Describe the agent you need…  (Enter to send)", id="prompt")
        yield Footer()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        event.input.disabled = True
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(UserBubble(text))
        thinking = Thinking()
        await chat.mount(thinking)
        chat.anchor()
        self.run_turn(UserMessage(text=text), thinking)

    @work(exclusive=True)
    async def run_turn(self, message: UserMessage, thinking: Thinking) -> None:
        """Spinner while waiting for the model or a tool; hidden while text streams."""
        chat = self.query_one("#chat", VerticalScroll)
        spinner: Thinking | None = thinking
        stream: MarkdownStream | None = None
        tools: dict[str, ToolBubble] = {}
        try:
            async for ev in self.session.send(message):
                match ev:
                    case AssistantDelta(text=text):
                        if stream is None:  # new text block
                            reply = AssistantBubble()
                            await chat.mount(reply, before=spinner)
                            if spinner is not None:
                                await spinner.remove()
                                spinner = None
                            stream = Markdown.get_stream(reply)
                        await stream.write(text)
                    case ToolCall(id=call_id):
                        if stream is not None:  # text after the tool call is a new bubble
                            await stream.stop()
                            stream = None
                        tools[call_id] = ToolBubble(ev)
                        await chat.mount(tools[call_id], before=spinner)
                        if spinner is None:
                            spinner = Thinking()
                            await chat.mount(spinner)
                    case ToolResult(id=call_id) if call_id in tools:
                        tools[call_id].finish(ev)
                    case ToolResult() | AssistantMessage():
                        pass
                    case Error(message=msg):
                        await chat.mount(ErrorBubble(msg))
        finally:
            if stream is not None:
                await stream.stop()
            if spinner is not None and spinner.is_attached:
                await spinner.remove()
            prompt = self.query_one("#prompt", Input)
            prompt.disabled = False
            prompt.focus()

    async def action_clear(self) -> None:
        await self.query_one("#chat").remove_children()
