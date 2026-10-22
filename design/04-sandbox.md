# 04 — Sandbox

All target-agent code is LLM-written and must be treated as **untrusted**. The sandbox is where
that code is installed, tested and executed. The host never imports or executes it.

## Approach

We follow the model of Anthropic's
[sandbox-runtime](https://github.com/anthropics/sandbox-runtime) (`srt`) — every command runs as
an **OS-level sandboxed ("bubble-wrapped") process** with kernel-enforced filesystem and network
restrictions, and all egress goes through host-side filtering proxies — but we **re-implement it
in Python** as our own package. No Node, no `srt` dependency.

Why rewrite instead of wrap:
- One language and toolchain (no Node runtime as a prerequisite).
- Direct integration with the orchestrator: async API, typed policies, structured violations,
  approval callbacks for "allow this domain?" without an RPC sidecar.
- `srt` is a beta preview whose formats change; we only need a subset (Linux first).

What we borrow from `srt` (semantics, not code):
- Read = allow-by-default with `deny_read` regions and `allow_read` re-openings;
  write = deny-by-default with `allow_write` regions and `deny_write` carve-outs.
- Network namespace removed; HTTP(S) and SOCKS5 proxies on the host reached through Unix
  sockets bind-mounted into the sandbox and bridged to loopback ports inside it.
- Domain allow/deny lists with wildcards and ports; resolved-address filtering against
  loopback, link-local, metadata endpoints (DNS-rebinding defence).
- seccomp filter blocking `AF_UNIX`/`AF_VSOCK` sockets, `io_uring`, new user/mount
  namespaces and the mount API (D12).
- A mandatory deny-write list for dangerous files inside writable areas, with ancestor
  directories pinned so they cannot be renamed aside.

## Platforms

| Platform | Mechanism | v1? |
|----------|-----------|-----|
| Linux, incl. **WSL2** | `bubblewrap` (mount/pid/net/ipc/uts namespaces) + seccomp + host proxies | **Yes** — primary |
| macOS | `sandbox-exec` with a generated Seatbelt profile; network allowed only to the host proxies' loopback ports | Later (M6+) |
| Windows native | — | No. Windows users run AgentBuilder inside WSL2. |

## Package

The runtime is a **standalone package** inside this repo with no dependency on the rest of
AgentBuilder, so it can be tested and reused on its own:

```
packages/sandbox/src/agentbuilder_sandbox/
  policy.py         # SandboxPolicy, FilesystemPolicy, NetworkPolicy, ResourceLimits (Pydantic)
  runtime.py        # Sandbox: start/stop/exec, ExecResult, check_host, rlimits, timeouts
  linux/
    bwrap.py        # policy -> bubblewrap argv (mount plan)
    seccomp.py      # classic-BPF filter assembled by hand (x86-64, aarch64)
  net/
    filter.py       # host canonicalisation, domain rules, decisions, resolved-address guard
    proxy.py        # asyncio HTTP (CONNECT + absolute URI) and SOCKS5 on one Unix socket
  bridge.py         # runs INSIDE the sandbox: loopback ports <-> host sockets; isolation
  violations.py     # violations (network: exact; filesystem/socket: stderr heuristics)
  dangerous.py      # mandatory deny-write list
  cli.py            # `abx-sandbox [--policy file] -- <cmd>` for manual testing
```

Dependencies: Python stdlib only (+ Pydantic for policy models). System: `bwrap` ≥ 0.5.

## Policy model

```python
class FilesystemPolicy(BaseModel):
    deny_read: list[Path] = []      # regions hidden (empty tmpfs mounted over them)
    allow_read: list[Path] = []     # re-opened read-only inside deny_read regions
    allow_write: list[Path] = []    # writable; everything else read-only
    deny_write: list[Path] = []     # read-only even inside allow_write (deny wins)
    scratch_write: list[Path] = []  # writable without dangerous-file protection (sandbox HOME)

class NetworkPolicy(BaseModel):
    allowed_domains: list[str] = []         # "pypi.org", "*.pythonhosted.org", "api.x.com:443"
    denied_domains: list[str] = []          # checked first
    denied_resolved_addresses: list[str] = []   # extra CIDRs on top of built-ins
    services: dict[str, Path] = {}          # name -> host Unix socket, bridged to loopback
                                            # inside; exposed as SANDBOX_SVC_<NAME>_URL

class SandboxPolicy(BaseModel):
    filesystem: FilesystemPolicy
    network: NetworkPolicy
    limits: ResourceLimits                  # memory, pids, cpu, file size, wall clock
    env: dict[str, str]                     # complete env; nothing inherited from host
    block_unix_sockets: bool = True
```

All paths are absolute and literal (no globs) — globs in deny rules silently failing to match
is a classic hole. Network rules are evaluated: `denied_domains` → `allowed_domains` →
per-exec extra allowlist → approval callback (if set) → deny.

## Linux execution plan

For each `exec`, the runtime builds a `bwrap` invocation. **Mount order matters** (later mounts
shadow earlier ones):

```
bwrap
  --die-with-parent --new-session            # no orphaned sandboxes; no TIOCSTI on our tty
  --unshare-user --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup-try
  --unshare-net                              # no network; only loopback inside
  --cap-drop ALL
  --ro-bind / /                              # 1. whole fs read-only
  --dev /dev --proc /proc --tmpfs /tmp       # 2. fresh /dev, /proc, /tmp
  --tmpfs <deny_read...>                     # 3. hide regions (e.g. $HOME, /run/user, /var/run/docker.sock dir)
  --ro-bind <allow_read...> <same>           # 4. re-open what the build needs
  --bind <allow_write...> <same>             # 5. writable areas
  --ro-bind <deny_write...> <same>           # 6. carve-outs incl. dangerous files
  --bind <session>/sock <session>/sock       # 7. proxy Unix sockets
  --chdir <workspace>
  --clearenv --setenv ...                    # explicit env only
  -- python -m agentbuilder_sandbox.bridge --spec <fd> -- <cmd...>
```

- The `python` used for the bridge is the host interpreter, visible read-only.

As built (D12):
- Pins (directories above write-denied paths) follow `--ro-bind / /`.
- Rule mounts (3–6) are emitted shallow-first, so the more specific rule wins.
- A `deny_write` target that does not exist gets a placeholder (an empty file, or a directory
  for a missing parent) that bubblewrap creates and the runtime removes once no exec is running.
- The proxy socket is per exec: `<session>/sock/<exec_id>.sock`.
- The bridge is run as `python -I -S bridge.py <base64 spec>`.

### In-sandbox bridge

Inside the new network namespace there is only loopback. `bridge.py` (trusted code, stdlib only):

1. Listens on `127.0.0.1:<http_port>`, `<socks_port>` and one port per policy service (e.g.
   the model proxy) and forwards each connection to the matching host Unix socket.
2. Forks the target command with `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` pointing at the
   loopback ports, `NO_PROXY=127.0.0.1`, and `SANDBOX_SVC_<NAME>_URL=http://127.0.0.1:<port>`
   per service.
3. The child creates a user+pid+mount namespace, after srt's `apply-seccomp`.
   - Its pid 1 mounts a fresh `/proc`, sets `max_user_namespaces=0` and forks the command.
   - The command sets `PR_SET_NO_NEW_PRIVS` and installs the seccomp filter before `exec`.
   - The bridge itself is not filtered, but it is non-dumpable and invisible to the command.
   - `socketpair` stays allowed (asyncio and many runtimes need it).
4. Propagates exit code and signals; exits when the command exits.

Even if the command kills the bridge, it only loses its own network access.

### seccomp

The filter (D12, after srt's `seccomp-unix-block.c`):

| Syscall | Result |
|---------|--------|
| any syscall from a foreign architecture, or an x32 syscall | kill |
| `socket(AF_UNIX \| AF_VSOCK)`, `io_uring_*` | `EPERM` |
| `unshare` / `clone` with `CLONE_NEWUSER \| CLONE_NEWNS` | `EPERM` |
| `setns`, `mount`, `umount2`, `pivot_root`, the new mount API | `EPERM` |
| `clone3` | `ENOSYS` |
| everything else | allow |

The host hand-assembles the classic-BPF program with `struct` for x86-64 and aarch64, with no
libseccomp. The bridge installs it via `ctypes` `prctl`. On an unknown architecture the sandbox
refuses to start. Tests run the program in a small BPF interpreter.

Why it matters even with `--unshare-net`: pathname sockets live on the filesystem, so
something like the user's D-Bus session socket or a Docker socket would be reachable through
any readable path. We hide `/run/user/<uid>` and known socket dirs via `deny_read` too —
defence in depth.

## Host-side network proxies

Per exec (D12), the runtime starts on the host (asyncio, in AgentBuilder's process):

| Proxy | Listens on | Does |
|-------|------------|------|
| HTTP + SOCKS5 | `<session>/sock/<exec_id>.sock`; a first byte of 0x05 means SOCKS5 | HTTP: `CONNECT host:port` tunnels, and absolute `http://` requests (one per connection). SOCKS5: `CONNECT` only, no BIND/UDP. Both filter by host and port. |
| Model *(service, provided by AgentBuilder, not the sandbox package)* | `<session>/sock/model.sock` | Reverse proxy to LLM providers; injects real API keys; meters tokens/cost; enforces model allowlist and budget. |

Filtering (`net/filter.py`):
- Host matching: exact, `*.suffix` wildcard, optional `:port`; IDNA-normalised, lowercased.
- After allow: resolve once, drop addresses that are loopback, link-local, multicast,
  unspecified, RFC1918/ULA (configurable), cloud metadata (`169.254.169.254`, `fd00:ec2::254`),
  the host's own interface addresses, or in `denied_resolved_addresses` — then **dial the
  vetted IP** (not the name again) to prevent rebinding.
- Every decision is logged as a structured event (`NetworkDecision(host, port, allowed,
  reason, exec_id)`), surfaced to the TUI and to the diagnostician.
- Optional async **approval callback**: unknown domain → orchestrator asks the user
  ("target agent wants `api.weather.com:443` — allow once / always / deny"). Default: deny.

No TLS interception: decisions use the `CONNECT` target (or SOCKS destination) host and port only.

## Session policy (what AgentBuilder generates)

| Rule | Paths |
|------|-------|
| `deny_read` | `$HOME`, `/run/user/<uid>`, `/var/run/docker.sock`, `/mnt/c/Users` (WSL2: Windows profile), other mounted Windows drives except toolchain paths |
| `allow_read` | workspace, session dir, uv binary, uv-managed Python dir, uv cache, AgentBuilder's interpreter + `agentbuilder_sandbox` package |
| `allow_write` | workspace, `<session>/tmp`, `<session>/uv-cache` |
| `deny_write` | `<workspace>/.git`, `<workspace>/evals`, `<workspace>/agent.spec.yaml`, + dangerous files |
| network (default profile) | no domains; model proxy only |
| network (install profile) | `pypi.org`, `files.pythonhosted.org` (or configured index) — used only for `uv sync` / `uv add` |
| network (agent profile) | spec `permissions.allowlist`, only after user approval |
| services | `model` → `<session>/sock/model.sock` (→ `SANDBOX_SVC_MODEL_URL`) |
| env | `PATH`, `HOME=<session>/tmp`, `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_NO_MANAGED_PYTHON_DOWNLOADS`, proxy vars, service URLs. Nothing inherited. |

**Dangerous files** (always deny-write inside writable areas, because the *user's* tools on the
host may act on them):
- `.git/hooks` and `.git/config`, or a worktree's `.git` file
- `.vscode/`, `.idea/`, `.claude/commands|agents`
- `.envrc`, `.mcp.json`, `.gitconfig`, `.gitmodules`, `.ripgreprc`, `.pre-commit-config.yaml`
- shell rc files
- `sitecustomize.py` / `usercustomize.py`

At a write root they are protected even when absent, using placeholders. Below the root they
are protected where they exist, up to 3 levels deep. Roots listed in `scratch_write` are
exempt. The list lives in `dangerous.py`.

## Resource limits & process control

`bwrap` restricts *what* is reachable, not *how much* is consumed. The plan (built so far, in
`runtime.py`: rlimits without NPROC, which counts all of the user's processes; timeout by
SIGKILL to bwrap's process group; head+tail output caps; a concurrency semaphore):

- **rlimits** via `resource.setrlimit` in the `preexec_fn` before `bwrap` starts:
  `RLIMIT_AS`, `RLIMIT_NPROC`, `RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`, `RLIMIT_CORE=0`.
- **cgroups** when available: wrap in `systemd-run --user --scope -p MemoryMax=… -p
  TasksMax=… -p CPUQuota=…` for real memory/pids caps (often unavailable in WSL2 → rlimits).
- **Wall-clock timeout**: `bwrap --die-with-parent` + we start it in its own session; on
  timeout `SIGTERM` → grace → `SIGKILL` to the process group. PID namespace means killing
  the namespace init kills everything inside.
- **Disk quota**: workspace size checked after each exec; fail fast above limit.
- **Output caps**: stdout/stderr bounded in memory; full output streamed to an artifact file.
- **Concurrency**: semaphore on parallel sandboxed processes (probes run in parallel).

## Violations

| Kind | Source | Precision |
|------|--------|-----------|
| Network | Proxy decisions | Exact (host, port, reason, exec id) |
| Unix sockets | seccomp `EPERM` | Heuristic (stderr patterns) |
| Filesystem | `EROFS` / `EACCES` / missing paths under hidden regions in stderr/tracebacks | Heuristic |

`ExecResult.violations` carries them; the diagnostician gets them as evidence ("the tool tried
to write outside the workspace"). Precise filesystem auditing (fanotify/eBPF) is out of scope.

## API

```python
class ExecResult(BaseModel):
    exit_code: int
    stdout: str                  # truncated
    stderr: str                  # truncated
    violations: list[Violation]
    duration_s: float
    timed_out: bool
    artifact_id: str | None      # full output stored host-side

class Sandbox:
    def __init__(self, policy: SandboxPolicy, session_dir: Path,
                 approve: ApprovalCallback | None = None) -> None: ...

    async def start(self) -> None: ...   # self-check, start proxies, prepare mounts
    async def stop(self) -> None: ...    # kill process groups, stop proxies, remove sockets

    async def exec(self, cmd: list[str], *, cwd: str = ".", env: dict[str, str] | None = None,
                   timeout_s: float = 120, network: str = "default",
                   stdin: bytes | None = None) -> ExecResult: ...

    async def exec_stream(self, cmd: list[str], **kw) -> AsyncIterator[ExecEvent]: ...
```

File operations are **not** part of the sandbox: the workspace is a plain host directory, read
and written directly by trusted builder tools (see below). The sandbox only *executes*.

## Host-side file access

Generated code can plant traps for host tooling, so builder file tools must:

- Resolve paths and reject anything outside the workspace **after** resolving symlinks
  (`workspace/x -> ~/.ssh/id_rsa` is harmless inside the sandbox but not to the host);
  open with `O_NOFOLLOW`.
- Enforce the same protected paths as `deny_write` for agents not allowed to touch them.
- Never execute workspace content on the host. **git runs on the host** with
  `-c core.hooksPath=/dev/null -c core.fsmonitor=false`, and `.git` is read-only inside the
  sandbox.

## Toolchain inside the sandbox

- `uv` and a uv-managed Python (pinned per workspace) are readable, not writable.
- Target venv at `<workspace>/.venv`, created by `uv sync` under the install profile.
- `agentbuilder_runtime` (probe runner, trace schema) installed into that venv from a wheel
  shipped with AgentBuilder — no network needed.
- `ruff`, type checker, `pytest` are dev-dependencies of the target project.

## Self-test (`agentbuilder doctor`)

Checks `bwrap` version and unprivileged user namespaces (Ubuntu 24.04+ AppArmor restriction),
seccomp support for the arch, cgroup availability, then runs escape probes that **must fail**:
read `~/.ssh`, write outside workspace, write `.git/hooks/x`, `curl` a non-allowed domain,
connect to `169.254.169.254`, create an `AF_UNIX` socket, fork bomb under limits. Also part of
the sandbox package's test suite in CI.

## Threat model (summary)

| Threat | Mitigation |
|--------|------------|
| Read user secrets / other repos | `deny_read` over `$HOME`, runtime dirs, Windows drives (WSL2); allowlist re-opening. |
| Write outside workspace / persist on host | Read-only root, writable binds only for workspace + scratch. |
| Plant hooks / editor configs / rc files | Dangerous-file deny-write list; host git with hooks disabled. |
| Tamper with criteria or history | `deny_write` on `evals/`, spec, `.git`; same rules in host file tools. |
| API key exfiltration | Keys only in host model proxy; env is constructed, never inherited. |
| Data exfiltration over network | No netns; only filtered proxies; approval-gated allowlists; IP vetting. |
| Reach host services via sockets | Separate netns (abstract sockets isolated); seccomp blocks `AF_UNIX`; socket dirs hidden. |
| Malicious dependency | Install profile limited to package index; dependency policy + approval. |
| Resource exhaustion | rlimits / cgroups, pid namespace, timeouts, disk + output caps. |
| Privilege escalation | User namespace, `--cap-drop ALL`, `no_new_privs`. |
| Kernel exploit / escape | Residual risk of process-level isolation (shared kernel). Stronger backend (microVM) possible later behind the same API. |
| Prompt injection steering the builder | Builder has no host tools beyond workspace I/O; policy-widening needs user approval. |
