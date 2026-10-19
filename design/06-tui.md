# 06 — Terminal UI

Built with [Textual](https://textual.textualize.io). The TUI is a thin client of the
orchestrator's event stream (see [01-architecture.md](01-architecture.md#event-model-orchestrator--ui)).

## Layout

```
┌─ AgentBuilder ─ schema-sql-agent ─ phase: PROBE (it 2/8) ─ $0.37 ─────────────────┐
│ ┌─ Chat ─────────────────────────────────┐ ┌─ [Spec] [Files] [Runs] [Report] ─────┐ │
│ │ you › I need an agent that ...         │ │ name: schema-sql-agent        v3     │ │
│ │ build › Proposed spec v1 ...           │ │ tools:                               │ │
│ │   ▸ write_file src/schema_kb/parser.py │ │   • search_tables  → schema_kb.search│ │
│ │   ▸ run_unit_tests ✓ 23 passed         │ │   • describe_table → schema_kb.catalog│ │
│ │ build › Probing 12 scenarios…          │ │ acceptance: ≥90 %, read_only         │ │
│ │   ██████████░░ 10/12                   │ │                                      │ │
│ │                                        │ │                                      │ │
│ └────────────────────────────────────────┘ └──────────────────────────────────────┘ │
│ ┌──────────────────────────────────────────────────────────────────────────────────┐ │
│ │ › _                                                                              │ │
│ └──────────────────────────────────────────────────────────────────────────────────┘ │
│ ^P pause  ^K commands  ^T toggle panel  ^L log                                       │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

- **Header/status bar:** agent name, phase, iteration, spend, sandbox status.
- **Chat pane:** streamed assistant text (Markdown), collapsible tool-call rows with status,
  inline progress for probes.
- **Side panel tabs:**
  - *Spec* — rendered current spec; diff view when a change is proposed.
  - *Files* — workspace tree; select to view with syntax highlighting; diff vs. last checkpoint.
  - *Runs* — probe list with pass/fail; drill into a trace (messages, tool calls, output).
  - *Report* — latest `EvalReport`, per-tag pass rates, iteration history.
- **Log drawer:** raw sandbox stdout/stderr and orchestrator logs.

## Screens

Besides the **Build** screen above, the app has:

**Agents** (`F2` / `/agents`) — the registry:

```
┌─ Agents ─ search: sql_ ────────────────────────────────────────────────────────┐
│ NAME               VER  STATUS      PASS   LAST RUN     TAGS                   │
│ ▸ schema-sql-agent  @3  released    100%   2h ago       sql, analytics         │
│   invoice-recon     @1  released     92%   yesterday    finance                │
│   invoice-recon     @2  draft        —     —            (session a1f3)         │
│   weather-brief     @4  deprecated   88%   3w ago       api, net:1 domain      │
├─────────────────────────────────────────────────────────────────────────────────┤
│ schema-sql-agent@3 — Propose safe read-only SQL from a DDL dump.               │
│ inputs: question (text), ddl_path (file)   network: none   secrets: none       │
│ versions: @1 → @2 → @3 (current)   avg cost $0.02   built 2026-10-19           │
│ [r]un [c]hat [e]dit [k]lone [v]ersions [d]iff [u]se version [x] archive [E]xport│
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Run** — opened by `r`/`c`: a form generated from `interface.inputs` (file pickers for file
inputs), then either the result view (one-shot) or a chat with the agent (interactive), with
live tool calls, cost, and produced files.

**Runs** (`F3` / `/runs`) — all runs (foreground and background): status, agent@version,
cost, duration; open to see trace and output; actions: rerun, rerun on another version,
flag ("expected X, got Y"), *edit agent from this run*.

## Interaction model

- Free text always goes to the builder (front desk while autonomous phases run).
- **Approval prompts** render as a modal/inline card with the diff and `[approve] [reject]
  [edit]`; `edit` opens the spec YAML in an editor widget (or `$EDITOR`).
- `ask_user` questions render with optional choice buttons + free text.
- **Interrupt:** `Ctrl+P` pauses at the next safe point; `Esc` twice cancels the current
  agent run.

## Slash commands

| Command | Action |
|---------|--------|
| `/new [name]` | Start a new target agent session. |
| `/sessions`, `/resume <id>` | List / resume sessions. |
| `/spec` | Show current spec; `/spec edit` to edit. |
| `/run` or `/build` | Start/continue the autonomous loop. |
| `/pause`, `/stop` | Pause / stop the loop. |
| `/probe "<input>"` | Ad-hoc single run of the target agent. |
| `/eval [tag]` | Run evaluation (optionally subset). |
| `/chat` | Talk *to the target agent* interactively inside the sandbox (manual probing). |
| `/diff [checkpoint]` | Show workspace diff. |
| `/rollback <checkpoint>` | Restore a checkpoint. |
| `/budget` | Show/adjust budgets. |
| `/release` | Release the current build as a new registry version. |
| `/agents [query]` | Open the Agents screen (optionally filtered). |
| `/use <agent>[@v] "<task>"` | Run a registry agent on a task (confirm card shows permissions/cost). |
| `/edit <agent>[@v]`, `/clone <agent> <new>` | Start an edit/clone build session. |
| `/runs` | Open the Runs screen. |
| `/shell` | Open an interactive shell in the sandbox (power users). |

## Headless mode

`agentbuilder build --spec agent.spec.yaml --auto-approve=safe --json` runs the same
orchestrator with a non-interactive event sink — useful for CI and for meta-evaluating the
builder itself.

## Testing

Textual's `App.run_test()` + pilot for UI tests; orchestrator driven by Pydantic AI
`TestModel`/`FunctionModel` so UI tests are deterministic and free.
