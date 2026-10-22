"""`agentbuilder` entry point: launches the chat TUI."""

import sys

from pydantic_ai.exceptions import UserError

from agentbuilder.builder.agents import build_main_agent
from agentbuilder.config import get_settings
from agentbuilder.execution import SandboxExecutor, new_session_dir, sandbox_toolset
from agentbuilder.orchestrator.session import Session
from agentbuilder.tui.app import AgentBuilderApp


def main() -> None:
    settings = get_settings()
    executor = SandboxExecutor(
        new_session_dir(settings.data_dir), allowed_domains=settings.sandbox_allowed_domains
    )
    try:
        agent = build_main_agent(settings=settings, toolsets=[sandbox_toolset(executor)])
    except UserError as exc:  # e.g. missing API key
        sys.exit(
            f"{exc}\n\nPut API keys in .secret (see .secret.example); "
            "AGENTBUILDER_MODEL=test in .env runs offline with canned replies."
        )
    AgentBuilderApp(Session(agent)).run()


if __name__ == "__main__":
    main()
