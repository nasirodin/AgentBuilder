# 00 — Overview

## Problem

Building a useful LLM agent is an iterative engineering task: write a system prompt, decide on
tools, implement those tools (often on top of non-trivial domain logic), try the agent, observe
where it misbehaves, and fix it. Most of that loop is mechanical enough for an LLM to drive,
provided it has (a) a safe place to write and execute code and (b) a disciplined way to judge
whether the agent behaves as intended.

## Vision

A user opens a terminal, describes what they need ("an agent that reconciles our supplier
invoices against purchase orders and flags discrepancies"), and AgentBuilder:

1. **Clarifies** the requirement and agrees on acceptance criteria with the user.
2. **Designs** the target agent: role, system prompt, tool set, output schema, dependencies.
3. **Implements** it inside a sandbox — tools, and when warranted a full Python **domain
   library** that encapsulates the domain knowledge, with its own unit tests.
4. **Runs and probes** the target agent inside the sandbox against scenarios.
5. **Diagnoses and fixes** failures, repeating until acceptance criteria pass or a budget is hit.
6. **Releases** it as a new immutable version (a git tag) in the user's agent registry, with a
   report of how it was validated.
7. **Runs** agents from the registry on real tasks (in the sandbox), and lets the user list,
   search, edit, clone, version and share them.

## Goals

- **G1 — Conversational:** the whole flow is driven from a chat in a terminal UI.
- **G2 — Safe by construction:** all generated code is written and executed only in the sandbox.
  The host is never exposed to generated code, and secrets never enter the sandbox in plaintext
  where avoidable.
- **G3 — Evidence-based:** "done" means measurable acceptance criteria pass, not "the LLM thinks
  it's fine".
- **G4 — Domain depth:** support target agents whose value lives in a real Python library, not
  just a prompt.
- **G5 — Transparent & interruptible:** the user can see the spec, the files, the runs, and can
  pause, steer, approve, or roll back at any point.
- **G6 — Reproducible:** every iteration is checkpointed; a finished agent can be re-run and
  re-evaluated outside AgentBuilder.
- **G7 — A library, not one-offs:** agents are released as immutable versions into a
  registry, from which they can be run, edited, cloned, compared, exported and imported.

## Non-goals (for v1)

- A graphical/web UI.
- Hosting/deploying target agents to production.
- Multi-user collaboration.
- Building agents on frameworks other than Pydantic AI.
- Fine-tuning models.

## Glossary

| Term | Meaning |
|------|---------|
| **Builder** | The AgentBuilder's own Pydantic AI agent(s) the user talks to. |
| **Target agent** | The agent being designed/built. Lives in the sandbox. |
| **Agent spec** | Structured, versioned description of the target agent (purpose, prompt, tools, output, criteria). |
| **Domain library** | Optional Python package generated alongside the target agent that encodes domain logic; tools are thin wrappers around it. |
| **Workspace** | The directory tree for one target agent (spec, code, evals, run artifacts). Mounted into the sandbox. |
| **Sandbox** | Isolated execution environment where code is written and run. |
| **Scenario / case** | One input to the target agent plus expectations about its behavior. |
| **Probe** | A single run of the target agent on a scenario, producing a trace. |
| **Trace** | Structured record of a probe: messages, tool calls, outputs, errors, usage, timing. |
| **Iteration** | One pass of implement → probe → evaluate → diagnose → fix. |
| **Version** | An immutable release of an agent (`name@N`) = git tag `vN` in the agent's repository: manifest, code, lockfile, evals, report. |
| **Registry** | Local store of all agents (one bare git repo each) + an index of versions, drafts and runs. |
| **Run** | Executing a released agent on a real task in the sandbox (as opposed to a probe during building). |

## Example session (abridged)

```
you   > I need an agent that answers questions about our Postgres schema and
        writes safe read-only SQL for analysts.
build > A few questions first: (1) how will it access the schema — live DB or a DDL dump?
        (2) should it execute queries or only propose them? ...
you   > DDL dump, only propose.
build > Proposed spec v1 (see side panel): 3 tools — search_tables, describe_table,
        validate_sql — backed by a small `schema_kb` library that parses the DDL.
        Acceptance: 12 scenarios, ≥ 90 % pass, zero non-SELECT statements. Approve? [y/n/edit]
you   > y
build > [implement] wrote schema_kb (6 modules, 23 unit tests passing)
        [probe] 12 scenarios → 8 pass, 4 fail
        [diagnose] 3 failures: agent guesses column names instead of calling describe_table
        [fix] prompt: require describe_table before referencing a column; tool docstring clarified
        [probe] 12 scenarios → 12 pass. Criteria met after 2 iterations ($0.41).
        Release as schema-sql-agent@1? [y/n]
you   > y
build > Released schema-sql-agent@1 (pass 12/12, holdout 4/4).

# later, in a new session
you   > write me SQL for top 10 customers by lifetime value, schema is in ./crm.sql
build > schema-sql-agent@1 fits this. Run it with ddl_path=crm.sql? (no network, est. $0.02) [y/n]
you   > y
build > SELECT c.id, c.name, SUM(o.total) AS ltv FROM customers c JOIN orders o ...
```
