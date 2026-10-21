"""`agentbuilder` entry point: launches the chat TUI."""

import sys

from pydantic_ai.exceptions import UserError

from agentbuilder.builder.agents import build_main_agent
from agentbuilder.orchestrator.session import Session
from agentbuilder.tui.app import AgentBuilderApp


def main() -> None:
    try:
        agent = build_main_agent()
    except UserError as exc:  # e.g. missing API key
        sys.exit(
            f"{exc}\n\nPut API keys in .secret (see .secret.example); "
            "AGENTBUILDER_MODEL=test in .env runs offline with canned replies."
        )
    AgentBuilderApp(Session(agent)).run()


if __name__ == "__main__":
    main()
