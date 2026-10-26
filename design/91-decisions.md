# 91 — Decision Log

Lightweight ADRs. Add new entries at the bottom; never edit accepted ones — supersede them.

---

### D1 — Pydantic AI for both builder and target agents
**Status:** accepted · 2026-10-19
**Context:** Need typed tool calls, structured outputs, model-agnostic providers, test models.
**Decision:** Use Pydantic AI everywhere; target agents follow a fixed contract on top of it.
**Consequences:** Builder can reason about one framework only; evals via `pydantic_evals`;
deterministic tests via `TestModel`/`FunctionModel`.

### D2 — All generated code is written and executed only in a sandbox
**Status:** accepted · 2026-10-19
**Context:** Target-agent code is LLM-generated and may be wrong or malicious (incl. via prompt
injection).
**Decision:** The host never imports/executes workspace code. Builder tools only reach the
sandbox via the `Sandbox` protocol.
**Consequences:** Probing requires an in-sandbox runner and trace format; slightly slower
iteration; strong safety story.

### D3 — Deterministic orchestrator around LLM phase agents
**Status:** accepted · 2026-10-19
**Context:** Budgets, approvals and stopping criteria must be reliable.
**Decision:** Phases, gates, budgets and checkpoints are controlled by plain code; LLM agents
return typed outputs that the orchestrator validates.
**Consequences:** More code up front; predictable behavior; easier testing.

### D4 — Docker as first real sandbox backend
**Status:** superseded by D8 · 2026-10-19
**Context:** Must run on Windows dev machines; need FS/process/network isolation.
**Decision:** Docker Desktop (WSL2) containers with hardening + internal network + egress
gateway; pluggable for future microVM/remote backends.
**Consequences:** Docker is a prerequisite; gVisor only on Linux hosts.

### D5 — API keys never enter the sandbox
**Status:** accepted · 2026-10-19
**Decision:** Target agents call models through a host-controlled model proxy. (Transport
refined by D8: a per-session Unix socket allowed via srt's `allowUnixSockets`.)
**Consequences:** Proxy must pass through the provider APIs Pydantic AI uses (OpenAI-compatible
and Anthropic at minimum); usage metering comes for free.

### D6 — Acceptance criteria are immutable to the fixing agents
**Status:** accepted · 2026-10-19
**Decision:** `evals/` and spec acceptance sections are write-protected for engineer and
diagnostician tools; changes go through an approved spec change.
**Consequences:** Prevents "passing" by weakening tests; some loops will escalate to the user.

### D7 — Textual for the terminal UI
**Status:** proposed · 2026-10-19
**Decision:** Textual app consuming an orchestrator event stream; headless sink for CI.

### D8 — Process-level sandbox via Anthropic sandbox-runtime (srt)
**Status:** superseded by D9 · 2026-10-19 · supersedes D4
**Context:** We want the "bubble-wrapped process" model used by
[anthropics/sandbox-runtime](https://github.com/anthropics/sandbox-runtime): kernel-enforced
filesystem and network restrictions on ordinary processes, without containers.
**Decision:** Every command touching generated code runs as `srt --settings <session policy> --
<cmd>` (bubblewrap on Linux/WSL2, Seatbelt on macOS). Writes are limited to the workspace and
session scratch; the user's home is read-denied; egress is default-deny through srt's proxies
with approval-gated allowlists; `.git`, `evals/` and the spec are write-denied. The workspace is
a plain host directory that trusted Python reads/writes directly (symlink-safe, never
executed).
**Consequences:** Node + srt (+ bwrap, socat, ripgrep on Linux) become prerequisites. srt is a
beta preview, so it is pinned and hidden behind the `Sandbox` protocol. srt gives no CPU/memory
limits, so we add timeouts, process-group kill and cgroup/rlimit caps ourselves. Isolation is
weaker than a VM (shared kernel, same uid) — accepted for a single-user dev tool. On Windows we
run under WSL2 for v1.

### D9 — Own Python sandbox runtime modelled on srt (no Node)
**Status:** accepted · 2026-10-19 · supersedes D8
**Context:** We want srt's bubble-wrapped-process model but not a Node dependency or a
cross-language sidecar.
**Decision:** Implement a standalone Python package `agentbuilder_sandbox` (in
`packages/sandbox`) with srt-like semantics: bubblewrap mount plan (read allow-by-default with
deny/allow, write deny-by-default with allow/deny), `--unshare-net` plus host-side asyncio
HTTP/SOCKS5 filtering proxies reached through Unix sockets and an in-sandbox Python bridge,
resolved-IP vetting, seccomp `AF_UNIX` block, mandatory dangerous-file deny-write list, our own
resource limits and violation reporting. The model proxy is exposed as a bridged "service"
(loopback URL inside the sandbox). Linux/WSL2 only for v1; macOS (Seatbelt) later; no native
Windows.
**Consequences:** No Node anywhere (also steers the type-checker choice away from pyright).
We own security-critical code, so the sandbox package gets its own escape-probe test suite run
in CI and by `agentbuilder doctor`. System prerequisite is just `bwrap` ≥ 0.5 (plus unprivileged
user namespaces).

### D10 — Agents are released as immutable versioned bundles into a local registry
**Status:** accepted, storage part superseded by D11 · 2026-10-19
**Context:** Users will accumulate many agents and need to list, run, edit and share them.
**Decision:** A build session releases a deterministic zip bundle (`manifest.json`, code,
`uv.lock`, evals, eval report, optional git history) as `name@N` into
`~/.agentbuilder/registry`, indexed in SQLite + FTS5. Versions are immutable; editing opens a
new build session seeded from a version and releases `N+1` behind a regression gate. Released
agents run in the sandbox under a stricter run policy derived from the manifest
(read-only code/env, writable `io/out` only, approved network allowlist, declared secrets).
**Consequences:** The manifest's `interface` (typed inputs/output) becomes part of the
generated-agent contract, enabling a generic run form/CLI and front-desk routing to existing
agents. Bundles are the source of truth; the index is rebuildable. Imported bundles are treated
as untrusted and re-evaluated locally.

### D11 — Registry storage is git; files only at export/import
**Status:** accepted · 2026-10-19 · supersedes the zip-bundle storage in D10
**Context:** Zip bundles had to be unpacked for every run, diff and edit, duplicated content
across versions, and needed an embedded git bundle to keep history — while the build
workspace is already a git repository.
**Decision:** Each agent is a bare git repository in `registry/agents/<name>.git`; a released
version is an immutable annotated tag `vN` on `main`, and the index records its commit SHA.
Runs use read-only cached checkouts of tags. Export produces a `git bundle` (default), a plain
project directory, or a tar.gz; import accepts the same and goes through a quarantine repo with
fsck, schema validation and permission approval.
**Consequences:** Diff/edit/clone are native git operations; storage is deduplicated; no
checksum files (commit SHA is the content hash). All git invocations on untrusted repos must use
the hardened config (no hooks, filters, fsmonitor, submodules). Remote/team registries can later
be plain git remotes.

### D12 — Sandbox hardening from the srt review; implementation choices
**Status:** accepted · 2026-10-22 · refines D9
**Context:** Reviewing sandbox-runtime (commit `aceba12`) for the first implementation of
`agentbuilder_sandbox` showed gaps in the 04-sandbox plan:
- Blocking only `socket(AF_UNIX)` is not enough:
  - A command that creates a user namespace gets full capabilities over a copy of the mount
    tree, and can remove every read-only bind, i.e. every write deny.
  - `AF_VSOCK` is not fenced by the network namespace.
  - `io_uring` can create sockets without calling `socket()`.
- A bridge that is unfiltered, runs as the same uid and shares the command's PID namespace can
  be ptraced by the command. The command then gets the bridge's power to open Unix sockets.
- The dangerous-file list was shorter than srt's, and nothing stopped a protected directory
  from being renamed aside (`mv .git x`).

**Decision:**
- **seccomp filter.** One default-allow filter, hand-assembled. It returns:
  - `EPERM` for `socket(AF_UNIX|AF_VSOCK)` and `io_uring_*`;
  - `EPERM` for `unshare`/`clone` with `CLONE_NEWUSER|CLONE_NEWNS`, for `setns` and for the
    whole mount API;
  - `ENOSYS` for `clone3`.

  A foreign architecture or an x32 syscall kills the process. The host builds the BPF program
  and passes it to the bridge.
- **Bridge.** It mirrors srt's `apply-seccomp`. It forks a stub that creates a
  user+pid+mount namespace. Its pid 1 mounts a fresh `/proc`, sets `max_user_namespaces=0`,
  reaps children and forwards signals. The command runs below it with `no_new_privs` and the
  filter. The bridge and the stubs are non-dumpable.
- **Proxy per exec.** Each exec gets its own filtering proxy on a fresh Unix socket. HTTP and
  SOCKS5 share that socket and are told apart by the first byte. The socket is bound only into
  that exec's sandbox, so attribution needs no proxy credentials. Per-exec allowlists cannot
  leak between execs.
- **Mount plan.** It follows srt:
  - mounts are applied shallow-first;
  - ancestor directories of write-denied paths are pinned;
  - absent deny paths get placeholders, which are removed once no exec runs;
  - a write deny inside a read-denied region is not re-bound.

  `scratch_write` roots (the sandbox's own HOME/TMPDIR) skip the dangerous-file protection.
- **Not taken from srt:**
  - inheriting the host environment (we build it ourselves);
  - `socat` (we use the Python bridge instead);
  - globs, TLS termination, credential masking, parent proxies;
  - macOS and Windows.
- **Type checking.** pyright runs with `pythonPlatform = "Linux"`, the target platform.

**Consequences:**
- Runtimes that need nested user namespaces fail in the sandbox: Chromium's sandbox, rootless
  podman, `unshare -r`.
- So do runtimes that need AF_UNIX sockets, e.g. multiprocessing managers.
- `allow_nested_userns` and `block_unix_sockets` exist for trusted cases only.
- While an exec runs, the workspace shows empty read-only placeholders for protected names.

### D13 — Keep OpenTelemetry spans for every probe and run; builder agents can query them
**Status:** accepted · 2026-10-26
**Context:** The trace (`trace.json`) holds Pydantic AI's message history plus what it does not
record (probe/scenario ids, output, error). It has no timing or nesting, which diagnosis needs
("which tool was slow", "where did the retries happen"). Pydantic AI already emits
OpenTelemetry spans for agent runs, model requests and tool executions. Using spans *instead*
of the trace was rejected: spans lose unflushed data when a run is killed, carry no scenario
context, and their attributes vary by instrumentation version.
**Decision:**
- The probe runner (and the run entry point) records the agent's spans to
  `<probe_id>.spans.jsonl` next to the trace, via a file exporter inside the sandbox.
- Traces and spans are stored host-side per session (`probes/it-<n>/`) and per registry run,
  kept across releases, and pruned only by retention.
- Builder agents read them through read-only telemetry tools (`list_probes`, `read_trace`,
  `read_spans`, `compare_probes`) during evaluation and debugging.

**Consequences:**
- `agentbuilder_runtime` depends on `opentelemetry-sdk` (installed into target venvs).
- Telemetry is written by the process that runs agent code, so it is informative, not
  authoritative: spend comes from the model proxy, and holdout probes stay hidden from the
  engineer and diagnostician.
- Span content is untrusted data in builder prompts.
- Storage grows with iterations; a retention policy is needed (M7).
