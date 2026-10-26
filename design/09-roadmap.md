# 09 — Roadmap

Each milestone ends with something runnable end-to-end. Order favours proving the riskiest
parts (sandbox + loop) early.

## M0 — Project skeleton
- uv workspace: `src/agentbuilder`, `packages/sandbox`, `packages/runtime`; ruff, pytest,
  type checker (Q5), pre-commit.
- Config via pydantic-settings; Typer CLI entry point.
- Spec models (`AgentSpec`, `Scenario`, `EvalReport`) with round-trip YAML tests.

## M1 — Sandbox runtime (`packages/sandbox`, Linux/WSL2)
- Policy models; policy → bwrap mount plan (ordering, protected-path bootstrap, dangerous files).
- In-sandbox bridge; seccomp `AF_UNIX` filter via ctypes (x86-64, aarch64).
- Host asyncio HTTP (CONNECT) + SOCKS5 proxies with domain filtering and resolved-IP vetting;
  service bridging for the model proxy.
- Limits: rlimits, optional cgroups, timeouts, process-group kill, output caps.
- Violations: exact network decisions, heuristic filesystem/socket detection.
- `abx-sandbox` CLI for manual testing.
- Symlink-safe workspace file tools; host-side git checkpoints with hooks disabled.
- `agentbuilder doctor` prerequisite check + escape self-test.
- Workspace scaffold template for the generated agent contract.
- Tests: denied reads/writes/egress actually fail, timeouts, symlink traps.

## M2 — Walking skeleton (headless)
- Single engineer agent with file + exec tools; spec provided as YAML (no clarify/design yet).
- Probe runner + trace format; deterministic evaluators only.
- Telemetry: OpenTelemetry spans per probe, stored per session; `list_probes`, `read_trace`,
  `read_spans` for the engineer.
- Orchestrator loop: implement → static gate → probe → evaluate → (fix) with budgets.
- Model proxy as a sandbox service (keys out of sandbox).
- Demo: build a trivial calculator agent from a hand-written spec until it passes.

## M3 — Registry & runs (CLI)
- Manifest, per-agent bare git repos (versions = tags), release from a session, SQLite/FTS index.
- Export (git bundle / project / archive) and quarantined import.
- `agents list/show/search/versions/use/archive/delete/export/import`.
- Run manager: run policy, env cache, one-shot `agentbuilder run`, run records (trace + spans).
- Secrets via keyring, declared-secret injection gated by approved domains.
- Demo: release the M2 calculator agent and run it on new tasks.

## M4 — Terminal UI
- Textual app: Build screen (chat, status bar, Spec/Files/Runs tabs, approvals).
- Agents and Runs screens; run form generated from `interface.inputs`; interactive chat runs.
- Event stream wiring; pause/interrupt; background runs with notifications.

## M5 — Full builder phases
- Clarifier, architect (spec generation incl. scenarios), diagnostician with `FixPlan` and
  `compare_probes`; edit sessions read the base version's telemetry.
- LLM-judge evaluators via `pydantic_evals`.
- Approval gates for spec changes, dependencies, network.
- Edit/clone sessions seeded from a registry version, with regression gate.
- Front-desk routing: search registry → run / edit / build new.

## M6 — Domain libraries
- Architect heuristics for library vs. prompt-only; library scaffold + unit-test-first flow.
- Architect reuses libraries/patterns from existing registry agents.
- Demo: agent backed by a non-trivial generated library (e.g. DDL catalog, invoice rules).

## M7 — Robustness
- Checkpoints, rollback, best-checkpoint revert, resume after crash.
- Repeats/flakiness, holdout set, anti-gaming checks, stagnation detection.
- Network approval callback wired to the TUI ("allow this domain once / always / deny").
- Run feedback → regression scenario → edit session; batch runs; run and telemetry retention.
- Optional forwarding of stored spans to an OTLP endpoint (e.g. Logfire).
- macOS backend (Seatbelt profile generation).

## M8 — Meta-evaluation of the builder
- A benchmark of "agent requests" with hidden acceptance tests.
- Run AgentBuilder headless against them; track success rate, iterations, cost per build
  across builder prompt/model changes.
