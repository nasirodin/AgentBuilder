# 05 — Build–Probe–Fix Loop

The loop is the heart of AgentBuilder: turn "the code exists" into "the agent demonstrably does
what the user asked".

```mermaid
flowchart TD
    I[Implement / Fix] --> C{Static checks<br/>ruff · types · unit tests · import smoke}
    C -- fail --> I
    C -- pass --> P[Probe: run scenarios in sandbox]
    P --> E[Evaluate traces]
    E --> D{Criteria met?}
    D -- yes --> DONE[Done → report]
    D -- no --> G{Budget / stagnation?}
    G -- exhausted --> BLK[Blocked → ask user]
    G -- ok --> X[Diagnose & plan fix]
    X -- code/prompt fix --> I
    X -- spec change --> AP[Approval gate] --> I
```

## 1. Static gate

Before any LLM-backed probing (which costs money and is noisy), cheap deterministic checks must
pass:

1. `uv sync` succeeds.
2. `ruff check` (errors only) and a type check (pyright/mypy — **OPEN**).
3. `pytest tests/unit` — domain library and tool tests.
4. Import smoke test: `python -m agentbuilder_runtime.smoke` imports `build_agent()`, verifies
   the contract, lists tools and their schemas (no model call; uses Pydantic AI `TestModel`).

Failures here go straight back to the engineer without consuming an iteration of the budget
(but they do count towards a separate "static fix" cap).

## 2. Probe

The **probe runner** (`agentbuilder_runtime`, installed into the target venv from a bundled
wheel) executes, as a bubble-wrapped process:

```
python -m agentbuilder_runtime.probe \
    --scenarios evals/scenarios.yaml [--ids a,b] [--repeat N] \
    --model <model> --out <session>/probes/it-<n>/
```

For each case it builds deps, runs the agent (`agent.run` / `agent.iter` to capture each
node), and writes a **trace**:

```json
{
  "probe_id": "it3-revenue_by_month-r1",
  "scenario_id": "revenue_by_month",
  "input": "...",
  "model": "anthropic:claude-sonnet-5-5",
  "messages": [ /* Pydantic AI ModelMessage history (all_messages_json), incl. tool calls */ ],
  "output": { /* Output model or text */ },
  "error": null,
  "usage": {"requests": 4, "input_tokens": 3120, "output_tokens": 410, "cost_usd": 0.012},
  "duration_s": 6.4
}
```

- Scenarios run concurrently with a cap; each run has a timeout and `UsageLimits`.
- `--repeat N` for non-determinism: a case's pass rate across repeats is reported, and cases
  that flip are flagged `flaky`.
- Ad-hoc probing: the builder (or the user via `/probe "..."`) can run a single input without
  a scenario, to explore behavior.

### Telemetry

Every probe also records Pydantic AI's **OpenTelemetry spans**, so the builder can see *how* a
run unfolded (timing, nesting, retries), not only the final message history. Telemetry is kept
for every agent we build and is available to the builder agents during evaluation and
debugging.

- **Capture.** The runner instruments the agent
  (`Agent.instrument_all(InstrumentationSettings(tracer_provider=..., include_content=True))`)
  with an OTel SDK tracer provider whose exporter writes JSON lines to
  `<probe_id>.spans.jsonl` next to the trace. A per-span processor writes each span as it ends,
  so a run killed by its timeout keeps every finished span. There is no network exporter: the
  sandbox has no network and needs none for this.
- **Content.** Nested spans for the agent run, each model request (model, tokens, latency,
  finish reason) and each tool execution (arguments, result, duration, exception). Spans the
  agent's own code or libraries emit land in the same file. Files are size-capped; oversized
  attribute values are truncated with a marker.
- **Roles.** The trace is what the evaluators score; spans are for timing and diagnosis
  (deterministic latency checks may read span durations). Neither is authoritative for spend:
  both are written inside the sandbox by the process that runs agent code, so cost comes from
  the model proxy.
- **Storage.** The probe batch's output directory is writable only during that probe exec and
  host-owned afterwards, so later sandboxed commands cannot rewrite past telemetry (see
  [07-state-and-persistence.md](07-state-and-persistence.md#telemetry)). Telemetry is kept for
  every iteration of every build session and for every registry run, subject to retention. An
  edit session of a released agent can read the telemetry of the session that built its base
  version and of that version's runs.
- **Builder access.** Read-only tools (`list_probes`, `read_trace`, `read_spans`,
  `compare_probes`; see [02-builder-agent.md](02-builder-agent.md#builder-tools)) let the
  evaluator, engineer and diagnostician query probes across iterations, e.g. the same scenario
  before and after a fix. Results are bounded summaries with handles to drill down.
- **Untrusted content.** Traces and spans contain model output, tool results and fixture data.
  Tools hand them to builder agents labelled as data; instructions inside them are never
  followed.
- **Export (optional).** The host can forward stored spans to a user-configured OTLP endpoint
  (e.g. Logfire) for viewing; never from inside the sandbox.

## 3. Evaluate

Built on `pydantic_evals` evaluators; two families:

**Deterministic** (computed in code, cheap, trusted):
- Output schema validity, field-level matches/regex/predicates.
- Tool-usage expectations: must-call, must-not-call, ordering, max calls.
- Hard requirements (e.g. `read_only` → SQL parser check).
- Cost/latency ceilings.
- No unhandled exceptions; retries within limit.

**LLM judge** (rubric-based, for qualities code can't check):
- Uses a *different* model/prompt than the target agent where possible.
- Returns `{pass: bool, score: 0..1, reason: str}` per rubric item.
- Judge prompts are fixed by the spec; the engineer cannot change them.

`EvalReport` aggregates: pass rate overall and by tag, hard-requirement violations, per-case
results with reasons, cost, latency percentiles, diff vs. previous iteration (fixed / regressed
/ still failing).

**Done** = `pass_rate ≥ min_pass_rate` AND zero hard-requirement violations AND no regressions on
cases that previously passed (configurable).

## 4. Diagnose

The diagnostician receives the report, failing traces (summarised), the spec and relevant files.
It drills into traces and spans of any visible probe, from this or earlier iterations, with the
telemetry tools (e.g. comparing a failing case with its last passing run).
It must output a `FixPlan`:

```python
class FailureAnalysis(BaseModel):
    scenario_ids: list[str]
    category: Literal["prompt", "tool-description", "tool-bug", "library-bug",
                      "missing-tool", "spec-ambiguity", "model-limitation", "flaky"]
    evidence: str                 # quote from trace
    root_cause: str

class FixPlan(BaseModel):
    analyses: list[FailureAnalysis]
    changes: list[PlannedChange]  # file + intent, minimal
    requires_spec_change: bool
    new_regression_cases: list[Scenario]   # e.g. unit test for a library bug
    expected_effect: str
```

Guidelines encoded in its prompt:
- Prefer the **lowest layer fix**: a library bug gets a unit test + library fix, not a prompt
  workaround.
- One hypothesis per failure cluster; avoid shotgun edits.
- `spec-ambiguity` and `model-limitation` escalate to the user rather than being "fixed" in code.
- Never modify acceptance criteria, judge rubrics, or scenario expectations.

## 5. Fix

The engineer applies the `FixPlan`, re-runs the static gate, then the orchestrator re-probes:
first the **failing cases** (fast feedback), then the **full set** (regression check) only if the
targeted cases now pass.

## Loop control (orchestrator)

| Guard | Default |
|-------|---------|
| Max iterations | 8 |
| Max target-agent spend per build | configurable, e.g. $5 |
| Max builder spend per build | configurable, e.g. $10 |
| Stagnation | stop after 3 iterations without pass-rate improvement, or same failure signature twice |
| Wall-clock | 60 min |
| Checkpoint | after every iteration (git commit + report) |
| Rollback | if an iteration regresses, offer automatic revert to best checkpoint |

When stopping without success, the builder produces a **blocked report**: what passes, what
fails, best hypothesis, and concrete questions or options for the user.

## Anti-gaming measures

- Scenario expectations, judge rubrics and criteria are read-only to the engineer (enforced by
  the file tools, not just the prompt).
- A hidden **holdout** subset (architect-generated, never shown to diagnose/fix agents) is
  evaluated at Done to detect overfitting to visible cases. The telemetry tools hide holdout
  probes from those agents too.
- Hard-coding detection: deterministic check that scenario input strings don't appear verbatim
  in source files.
