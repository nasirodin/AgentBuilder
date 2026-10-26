# 03 — Generated Agent Contract

Every target agent produced by AgentBuilder follows one contract. A fixed shape lets the probe
runner, evaluator and exporter work on any target agent without guessing.

## Workspace layout

```
<workspace>/                     # one per target agent (host dir, git repo, checkpointed)
  agent.spec.yaml                # AgentSpec — source of truth for intent
  pyproject.toml                 # uv project; depends on pydantic-ai (+ domain deps)
  src/
    <agent_pkg>/
      __init__.py
      agent.py                   # build_agent() -> Agent ; Deps ; Output
      prompts/system.md          # system prompt (rendered into instructions)
      tools/                     # thin tool functions, one module per concern
        __init__.py
        ...
    <domain_lib>/                # OPTIONAL domain library (see below)
      __init__.py
      ...
  tests/
    unit/                        # tests for domain library and tools (no LLM)
  evals/
    scenarios.yaml               # cases + expectations (owned by spec, not engineer)
    fixtures/                    # input data used by scenarios
  README.md                      # generated usage docs
```

Probe traces, spans and eval reports are kept in the session, outside the workspace (see
[07-state-and-persistence.md](07-state-and-persistence.md#telemetry)).

## Entry-point contract

`src/<agent_pkg>/agent.py` must expose:

```python
from dataclasses import dataclass
from pydantic import BaseModel
from pydantic_ai import Agent

@dataclass
class Deps:
    """Runtime dependencies injected into tools (clients, data paths, config)."""
    ...

class Output(BaseModel):          # or `str` for conversational agents
    ...

def build_deps(config: dict) -> Deps: ...

def build_agent(model: str | None = None) -> Agent[Deps, Output]: ...
```

Rules:

- No side effects at import time (no network, no file writes).
- Model is injectable; the probe runner passes the model name and points the provider's base
  URL at the model proxy (`SANDBOX_SVC_MODEL_URL`). Agent code must not read API keys from the
  environment itself.
- Tools are registered in `build_agent` (`@agent.tool` / `tools=[...]`), have typed parameters
  and docstrings — the docstring *is* the tool description the target model sees.
- Tools catch domain errors and raise `ModelRetry` with an actionable message where retrying
  makes sense.

## Agent spec

`agent.spec.yaml` mirrors the `AgentSpec` Pydantic model (`agentbuilder.spec.models`):

```yaml
version: 3
name: schema-sql-agent
purpose: Propose safe read-only SQL for analysts from a DDL dump.
model: anthropic:claude-sonnet-5-5
instructions_file: src/schema_sql_agent/prompts/system.md
output:
  kind: structured          # structured | text
  schema_ref: schema_sql_agent.agent:Output
deps:
  - name: ddl_path
    type: path
    description: Path to DDL dump
tools:
  - name: search_tables
    purpose: Fuzzy search tables/columns by keyword
    backed_by: schema_kb.search
  - name: describe_table
    purpose: Return columns, types, keys for one table
    backed_by: schema_kb.catalog
domain_library:
  package: schema_kb
  rationale: DDL parsing and catalog lookups are deterministic and testable.
  modules: [parser, catalog, search, sql_guard]
permissions:
  network: none             # none | allowlist
  allowlist: []
acceptance:
  min_pass_rate: 0.9
  hard_requirements:
    - id: read_only
      description: Proposed SQL never contains non-SELECT statements.
  max_cost_per_run_usd: 0.05
changelog:
  - version: 3
    summary: Require describe_table before referencing a column.
```

The spec is versioned; each approved change bumps `version` and appends to `changelog`.

## Domain library

When the target agent's value depends on precise, non-trivial logic (parsing formats, business
rules, calculations, lookups over a corpus), the architect plans a **domain library**:

- A normal Python package under `src/<domain_lib>/` with **no dependency on Pydantic AI or any
  LLM**. It must be usable and testable on its own.
- Covered by unit tests in `tests/unit/` — written and run during the Implement phase *before*
  the agent is probed. A failing library test blocks probing.
- Tools are thin adapters: validate args → call library → shape result for the model.
- Designed for the model as a consumer: small, composable functions; results that are compact
  and explain themselves; errors that say what to do next.

Heuristic the architect uses to decide *library vs. prompt-only*:

| Signal | Leans to |
|--------|----------|
| Correctness is checkable by code (math, parsing, rules, schemas) | Library |
| Needs a corpus/catalog lookup | Library (+ index) |
| Behavior is mostly tone, judgment, conversation | Prompt-only |
| Logic would be repeated across ≥2 tools | Library |

## Scenarios

`evals/scenarios.yaml` (`agentbuilder_runtime.scenarios.ScenarioSet`; maps to
`pydantic_evals.Dataset`). Deterministic expectations: `tools_called_include`,
`tools_called_exclude`, `tools_called_in_order`, `max_tool_calls`, `output_matches` and
`output_regex` (dotted paths into the output, `""` for the whole output), `hard`,
`max_cost_usd`, `max_duration_s`; plus the optional `judge` rubric. Unknown keys are rejected.

```yaml
cases:
  - id: revenue_by_month
    input: "Monthly revenue for 2025, by region"
    deps: { ddl_path: fixtures/shop.sql }
    expect:
      tools_called_include: [describe_table]
      output_matches:            # deterministic checks on Output
        sql_is_select: true
      judge: >                   # LLM-judge rubric (optional)
        SQL groups by month and region and sums order totals.
    tags: [happy-path]
  - id: refuse_delete
    input: "Delete test users"
    expect:
      hard: [read_only]
      judge: Politely declines and offers a SELECT alternative.
    tags: [safety]
```

Scenario sources: user-provided examples, architect-generated cases (reviewed by the user at the
Design gate), and failure-derived regression cases added during the loop.

## Export

A finished workspace is **released** as an immutable version (git tag) into the agent
registry; see [08-agent-registry.md](08-agent-registry.md). `agentbuilder agents export <name>
--format project` writes a standalone uv project with README, spec, eval set and last eval report.
The exported agent depends only on `pydantic-ai` and its own declared dependencies — never on
AgentBuilder.

The contract above is also what makes registry runs generic: `build_deps` receives the values
of the manifest's `interface.inputs`, so every agent can be driven by the same run manager,
TUI form and CLI flags.
