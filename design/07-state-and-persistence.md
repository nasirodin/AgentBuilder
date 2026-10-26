# 07 — State & Persistence

## Host-side layout

```
~/.agentbuilder/                       # or $AGENTBUILDER_HOME
  config.toml                          # models, budgets, sandbox backend, policies
  sessions/
    <session_id>/
      session.json                     # Session state (phase, iteration, budgets, spec version)
      chat.jsonl                       # user-facing transcript (events)
      builder/                         # Pydantic AI message histories per phase run
        <run_id>.json
      reports/
        it-<n>.json                    # EvalReport per iteration
      probes/
        it-<n>/                        # one probe batch (writable only during its probe exec)
          <probe_id>.json              # trace
          <probe_id>.spans.jsonl       # OpenTelemetry spans
      artifacts/                       # large tool outputs, full logs (by id)
      workspace/                       # target project; the only writable dir for sandboxed processes
      tmp/  uv-cache/                  # session scratch, writable by sandboxed processes
      policy.json                      # generated SandboxPolicy (all network profiles)
      sock/                            # http/socks/model proxy Unix sockets while live
```

Whether workspaces live under `~/.agentbuilder` or in a user-chosen project directory is
**OPEN** (likely: default under home, `--workspace PATH` to override).

## Session state

```python
class Session(BaseModel):
    id: str
    name: str
    created_at: datetime
    phase: Phase
    iteration: int
    spec_version: int
    best_checkpoint: str | None
    budgets: Budgets            # limits + spent (builder / target)
    sandbox_policy: SandboxPolicy  # rebuilt from spec + approvals on start/resume
    pending_approval: ApprovalRequest | None
```

Persisted after every event that changes it (small JSON, atomic write via temp file + rename).
On crash/restart, `agentbuilder resume <id>` restores the session, kills any orphaned sandboxed
process groups, rebuilds the sandbox policy, restarts the proxies, and continues from the last completed step (phase runs are idempotent at the step
level: a half-finished phase run is restarted from its beginning on the last checkpoint).

## Telemetry

Probe traces and spans live under `probes/`, outside the workspace. The runner's output
directory is bound writable only into its own probe exec; afterwards it is host-owned and
read-only to every later sandboxed command, so target-agent code cannot rewrite past
telemetry. Telemetry is kept for the life of the session and is not deleted on release: an
edit session of `name@N` reads the telemetry of the session that built it (provenance records
the session id). Retention (age / size, configurable) prunes spans of old iterations first and
keeps the traces of the best and released iterations.

## Checkpoints

- The workspace is a git repo owned by the host: `.git` is `denyWrite` for sandboxed processes,
  and `checkpoint()` runs `git add -A && git commit` **on the host** with hooks/fsmonitor
  disabled (`-c core.hooksPath=/dev/null`), with a structured message (`it3: fix describe_table docstring [pass 0.83→1.00]`).
- Checkpoint ids are commit SHAs; reports reference the SHA they evaluated.
- Rollback = host-side `git reset --hard <sha>` (after confirmation) + sandboxed `uv sync`.
- The orchestrator tracks the **best checkpoint** (highest pass rate, no hard violations) for
  automatic revert on regression.

## Transcripts

- `chat.jsonl` stores UI events, enabling exact UI replay on resume.
- Builder message histories are stored per phase run using Pydantic AI's
  `ModelMessagesTypeAdapter` — useful for debugging the builder and for meta-evals.

## Registry & runs

Released agents and run records live in `~/.agentbuilder/registry/`, separate from sessions;
see [08-agent-registry.md](08-agent-registry.md#registry-storage). Release reads the host-side
workspace at a chosen checkpoint (default: best), so no code is executed while bundling.
Deleting a session never deletes released versions; a draft's session id is recorded in the
registry so drafts can be listed and resumed.
