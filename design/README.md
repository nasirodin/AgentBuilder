# AgentBuilder — Design Documents

AgentBuilder is a terminal application in which a user chats with a **builder agent** that
designs, implements, runs, probes and iteratively fixes *other* agents. Everything the builder
writes or executes happens inside an isolated **sandbox**. Agents (both the builder and the
agents it produces) are built on [Pydantic AI](https://ai.pydantic.dev).

> Status: **design phase**. These documents describe intent, not existing code.
> Anything marked **OPEN** is undecided — see [90-open-questions.md](90-open-questions.md).

## Reading order

| #  | Document | What it covers |
|----|----------|----------------|
| 00 | [Overview](00-overview.md) | Vision, goals, non-goals, glossary, example session |
| 01 | [Architecture](01-architecture.md) | Components, boundaries, data flow, package layout |
| 02 | [Builder agent](02-builder-agent.md) | The main chat agent: phases, sub-agents, tools, prompting |
| 03 | [Generated agent contract](03-generated-agent.md) | What a produced agent looks like on disk: spec, tools, domain library |
| 04 | [Sandbox](04-sandbox.md) | Isolation model, sandbox API, backends, secrets & network policy |
| 05 | [Build–probe–fix loop](05-eval-loop.md) | Running the generated agent, evaluating it, diagnosing and repairing |
| 06 | [Terminal UI](06-tui.md) | Layout, interaction model, commands, approvals |
| 07 | [State & persistence](07-state-and-persistence.md) | Sessions, workspaces, checkpoints |
| 08 | [Agent registry](08-agent-registry.md) | Bundling, storing, listing, editing and running agents |
| 09 | [Roadmap](09-roadmap.md) | Milestones from walking skeleton to full loop |
| 90 | [Open questions](90-open-questions.md) | Unresolved decisions |
| 91 | [Decision log](91-decisions.md) | Decisions taken, with rationale (ADR-lite) |

## Conventions

- Diagrams use Mermaid.
- "Builder" = the AgentBuilder's own agent(s). "Target agent" = the agent being built.
- Model identifiers are configuration, never hard-coded; examples use `anthropic:claude-opus-5-5`.
