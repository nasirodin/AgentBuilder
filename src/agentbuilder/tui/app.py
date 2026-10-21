"""Textual chat UI. A thin client of the session's event stream (see design/06-tui.md)."""

import time

from rich.spinner import Spinner
from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Markdown, Static

from agentbuilder.orchestrator.events import AssistantDelta, AssistantMessage, Error, UserMessage
from agentbuilder.orchestrator.session import Session


class UserBubble(Static):
    pass


class AssistantBubble(Markdown):
    pass


class ErrorBubble(Static):
    pass


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
    BINDINGS = [("ctrl+q", "quit", "Quit"), ("ctrl+l", "clear", "Clear screen")]

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
        chat = self.query_one("#chat", VerticalScroll)
        reply: AssistantBubble | None = None
        stream = None
        try:
            async for ev in self.session.send(message):
                match ev:
                    case AssistantDelta(text=text):
                        if reply is None:  # first chunk: swap the spinner for the reply
                            reply = AssistantBubble()
                            await chat.mount(reply, before=thinking)
                            await thinking.remove()
                            stream = Markdown.get_stream(reply)
                        await stream.write(text)
                    case AssistantMessage():
                        pass
                    case Error(message=msg):
                        await chat.mount(ErrorBubble(msg))
        finally:
            if stream is not None:
                await stream.stop()
            if thinking.is_attached:
                await thinking.remove()
            prompt = self.query_one("#prompt", Input)
            prompt.disabled = False
            prompt.focus()

    async def action_clear(self) -> None:
        await self.query_one("#chat").remove_children()
