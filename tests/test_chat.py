import asyncio

from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from textual.widgets import Footer, Input

from agentbuilder.builder.agents import build_main_agent
from agentbuilder.orchestrator.events import AssistantDelta, AssistantMessage, UserMessage
from agentbuilder.orchestrator.session import Session
from agentbuilder.tui.app import AgentBuilderApp, AssistantBubble, Thinking, UserBubble


def make_session() -> Session:
    return Session(build_main_agent(TestModel(custom_output_text="Hello **there**")))


async def test_session_streams_reply_and_keeps_history() -> None:
    session = make_session()
    events = [ev async for ev in session.send(UserMessage(text="hi"))]

    assert any(isinstance(ev, AssistantDelta) for ev in events)
    assert events[-1] == AssistantMessage(text="Hello **there**")
    assert len(session.history) == 2  # request + response

    _ = [ev async for ev in session.send(UserMessage(text="again"))]
    assert len(session.history) == 4


async def test_tui_sends_message_to_agent() -> None:
    app = AgentBuilderApp(make_session())
    async with app.run_test() as pilot:
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert str(app.query_one(UserBubble).render()) == "hi"
        assert app.query_one(AssistantBubble).source == "Hello **there**"
        assert len(app.session.history) == 2


async def test_input_is_not_covered_by_footer() -> None:
    app = AgentBuilderApp(make_session())
    async with app.run_test(size=(80, 24)):
        prompt, footer = app.query_one(Input).region, app.query_one(Footer).region
        assert not prompt.overlaps(footer)
        assert prompt.bottom <= footer.y


async def test_spinner_until_first_chunk_then_streams() -> None:
    first_chunk, rest = asyncio.Event(), asyncio.Event()

    async def slow_stream(messages, info: AgentInfo):
        await first_chunk.wait()
        yield "Hello "
        await rest.wait()
        yield "world"

    app = AgentBuilderApp(Session(build_main_agent(FunctionModel(stream_function=slow_stream))))
    async with app.run_test() as pilot:
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        await pilot.pause()
        assert app.query(Thinking) and not app.query(AssistantBubble)

        first_chunk.set()
        await pilot.pause(0.2)
        assert not app.query(Thinking)
        assert app.query_one(AssistantBubble).source == "Hello "  # partial reply visible

        rest.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(AssistantBubble).source == "Hello world"
        assert not app.query_one(Input).disabled
