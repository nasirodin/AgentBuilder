"""`Sandbox`: runs commands as bubble-wrapped processes (see design/04-sandbox.md "API").

Per exec: a filtering proxy on a fresh Unix socket, a mount plan, and
`bwrap ... -- python -I -S bridge.py <spec>`; the bridge relays loopback ports 3128 (HTTP)
and 1080 (SOCKS5) plus one per service to host sockets and runs the command isolated and
filtered. Linux only; on other platforms `start()` raises `SandboxUnavailable`.
"""

import asyncio
import base64
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from agentbuilder_sandbox.linux.bwrap import MountPlan, bwrap_argv, plan_mounts
from agentbuilder_sandbox.linux.seccomp import build_filter, host_arch
from agentbuilder_sandbox.net.filter import AddressGuard, ApprovalCallback, parse_network
from agentbuilder_sandbox.net.proxy import ConnectionRecord, FilteringProxy
from agentbuilder_sandbox.policy import ResourceLimits, SandboxPolicy
from agentbuilder_sandbox.violations import Violation, network_violations, stderr_violations

HTTP_PORT = 3128
SOCKS_PORT = 1080
SERVICE_PORT_BASE = 3200
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
MIN_BWRAP = (0, 5, 0)
BRIDGE = Path(__file__).with_name("bridge.py")


class SandboxUnavailable(RuntimeError):
    """The host cannot run the sandbox (wrong platform, missing bubblewrap, ...)."""


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    """Some output was dropped (see `ResourceLimits.max_output_bytes`)."""
    violations: list[Violation]
    duration_s: float
    timed_out: bool


def check_host(policy: SandboxPolicy) -> list[str]:
    """Reasons the sandbox cannot run here; empty when it can."""
    if not sys.platform.startswith("linux"):
        return [f"the sandbox needs Linux (or WSL2); this is {sys.platform}"]
    errors: list[str] = []
    bwrap = policy.bwrap_path or shutil.which("bwrap")
    if not bwrap or not os.access(bwrap, os.X_OK):
        errors.append("bubblewrap (bwrap) is not installed: apt-get install bubblewrap")
    else:
        out = subprocess.run([bwrap, "--version"], capture_output=True, text=True).stdout
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
        if not m or tuple(map(int, m.groups())) < MIN_BWRAP:
            errors.append(f"bubblewrap >= 0.5 required, found {out.strip() or 'unknown'}")
    try:
        if int(Path("/proc/sys/user/max_user_namespaces").read_text()) == 0:
            errors.append("unprivileged user namespaces are disabled")
    except OSError, ValueError:
        pass
    if (policy.block_unix_sockets or not policy.allow_nested_userns) and host_arch() is None:
        errors.append("no seccomp filter for this CPU architecture")
    return errors


class Sandbox:
    def __init__(
        self,
        policy: SandboxPolicy,
        session_dir: Path,
        approve: ApprovalCallback | None = None,
    ) -> None:
        self.policy = policy
        self.session_dir = session_dir.resolve()
        self.approve = approve
        self._started = False
        self._active = 0
        self._placeholders: set[str] = set()
        self._procs: set[asyncio.subprocess.Process] = set()

    @property
    def sock_dir(self) -> Path:
        return self.session_dir / "sock"

    @property
    def empty_dir(self) -> Path:
        return self.session_dir / "empty"

    async def start(self) -> None:
        if errors := check_host(self.policy):
            raise SandboxUnavailable("; ".join(errors))
        arch = host_arch()
        assert arch is not None
        self._bwrap = self.policy.bwrap_path or shutil.which("bwrap") or "bwrap"
        self._bpf = build_filter(
            arch,
            block_unix_sockets=self.policy.block_unix_sockets,
            block_namespaces=not self.policy.allow_nested_userns,
        ).hex()
        for d in (self.session_dir, self.sock_dir, self.empty_dir):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(str(self.sock_dir / ("x" * 16 + ".sock"))) > 100:
            raise SandboxUnavailable(f"session dir path too long for Unix sockets: {self.sock_dir}")
        self._python = os.path.realpath(sys.executable)
        self._runtime_read = [
            os.path.realpath(sys.base_prefix),
            os.path.dirname(self._python),
            str(BRIDGE.parent.resolve()),
        ]
        net = self.policy.network
        self._allowed, self._denied = net.allowed_rules(), net.denied_rules()
        self._guard = AddressGuard(
            extra=net.denied_resolved_addresses,
            deny_private=net.deny_private_addresses,
            allowed_literals=self._allowed,
        )
        self._sem = asyncio.Semaphore(self.policy.limits.max_concurrent)
        self._started = True

    async def stop(self) -> None:
        for proc in list(self._procs):
            _kill(proc)
        self._started = False

    async def __aenter__(self) -> Sandbox:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def exec(
        self,
        cmd: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float = 120,
        stdin: bytes | None = None,
        extra_allowed_domains: Sequence[str] = (),
    ) -> ExecResult:
        if not self._started:
            raise RuntimeError("Sandbox.start() has not been called")
        if not cmd:
            raise ValueError("empty command")
        async with self._sem:
            return await self._exec(list(cmd), cwd, env, timeout_s, stdin, extra_allowed_domains)

    async def _exec(
        self,
        cmd: list[str],
        cwd: str | None,
        env: dict[str, str] | None,
        timeout_s: float,
        stdin: bytes | None,
        extra_allowed: Sequence[str],
    ) -> ExecResult:
        started = time.monotonic()
        exec_id = secrets.token_hex(8)
        sock = str(self.sock_dir / f"{exec_id}.sock")
        records: list[ConnectionRecord] = []
        proxy = FilteringProxy(
            sock,
            exec_id=exec_id,
            allowed=self._allowed,
            denied=self._denied,
            extra_allowed=parse_network(extra_allowed, allow_star=False),
            guard=self._guard,
            approve=self.approve,
            on_record=records.append,
        )
        await proxy.start()
        os.chmod(sock, 0o600)
        self._active += 1
        try:
            plan, argv = self._prepare(cmd, cwd, env, sock)
            out = await _run(argv, stdin, timeout_s, self.policy.limits, self._procs)
        finally:
            await proxy.close()
            Path(sock).unlink(missing_ok=True)
            self._active -= 1
            if self._active == 0:
                self._remove_placeholders()
        exit_code, stdout, stderr, truncated, timed_out = out
        violations = network_violations(records) + stderr_violations(stderr, plan.protected)
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            violations=violations,
            duration_s=round(time.monotonic() - started, 3),
            timed_out=timed_out,
        )

    def _prepare(
        self, cmd: list[str], cwd: str | None, env: dict[str, str] | None, sock: str
    ) -> tuple[MountPlan, list[str]]:
        net = self.policy.network
        listen: list[tuple[int, str]] = [(HTTP_PORT, sock), (SOCKS_PORT, sock)]
        full_env = {"PATH": DEFAULT_PATH, **self.policy.env}
        for i, (name, path) in enumerate(sorted(net.services.items())):
            port = SERVICE_PORT_BASE + i
            listen.append((port, path))
            full_env[f"SANDBOX_SVC_{name.upper()}_URL"] = f"http://127.0.0.1:{port}"
        http_url, socks_url = f"http://127.0.0.1:{HTTP_PORT}", f"socks5h://127.0.0.1:{SOCKS_PORT}"
        full_env |= {
            "SANDBOX_RUNTIME": "1",
            "HTTP_PROXY": http_url,
            "HTTPS_PROXY": http_url,
            "http_proxy": http_url,
            "https_proxy": http_url,
            "ALL_PROXY": socks_url,
            "all_proxy": socks_url,
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
        }
        full_env |= env or {}  # per-exec values last; proxy variables grant nothing anyway
        plan = plan_mounts(
            self.policy.filesystem,
            extra_read=self._runtime_read,
            socket_binds=[sock, *net.services.values()],
            empty_dir=str(self.empty_dir),
        )
        self._placeholders.update(plan.placeholders)
        spec = {
            "listen": listen,
            "seccomp": self._bpf,
            "nested_userns": self.policy.allow_nested_userns,
            "argv": cmd,
        }
        encoded = base64.b64encode(json.dumps(spec).encode()).decode()
        workdir = cwd or (plan.write_roots[0] if plan.write_roots else "/")
        argv = bwrap_argv(
            self._bwrap,
            plan,
            env=full_env,
            cwd=workdir,
            command=[self._python, "-I", "-S", str(BRIDGE.resolve()), encoded],
        )
        return plan, argv

    def _remove_placeholders(self) -> None:
        """Remove mountpoints bubblewrap created, once no sandbox can still be using them
        (unlinking a mountpoint on the host detaches it inside a running sandbox)."""
        for p in sorted(self._placeholders, key=len, reverse=True):
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    os.rmdir(p)  # only if still empty
                elif os.path.isfile(p) and os.path.getsize(p) == 0:
                    os.unlink(p)
            except OSError:
                pass
        self._placeholders.clear()


class _Capture:
    """Keeps the head and tail of a stream within `limit` bytes."""

    def __init__(self, limit: int) -> None:
        self.half = limit // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def feed(self, data: bytes) -> None:
        room = self.half - len(self.head)
        if room > 0:
            self.head += data[:room]
            data = data[room:]
        self.tail += data
        if len(self.tail) > self.half:
            cut = len(self.tail) - self.half
            self.dropped += cut
            del self.tail[:cut]

    def text(self) -> str:
        if not self.dropped:
            return (self.head + self.tail).decode(errors="replace")
        return (
            self.head.decode(errors="replace")
            + f"\n[... {self.dropped} bytes omitted ...]\n"
            + self.tail.decode(errors="replace")
        )


async def _read(stream: asyncio.StreamReader | None, cap: _Capture) -> None:
    if stream is None:
        return
    while data := await stream.read(65536):
        cap.feed(data)


async def _write(stream: asyncio.StreamWriter | None, data: bytes | None) -> None:
    if stream is None or data is None:
        return
    try:
        stream.write(data)
        await stream.drain()
    except BrokenPipeError, ConnectionResetError:
        pass
    finally:
        stream.close()


def _rlimits(limits: ResourceLimits):
    def apply() -> None:
        import resource

        for res, value in (
            (resource.RLIMIT_AS, limits.memory_bytes),
            (resource.RLIMIT_CPU, limits.cpu_seconds),
            (resource.RLIMIT_FSIZE, limits.file_size_bytes),
            (resource.RLIMIT_NOFILE, limits.open_files),
            (resource.RLIMIT_CORE, 0),
        ):
            if value is not None:
                resource.setrlimit(res, (value, value))

    return apply


def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill bwrap's process group; --die-with-parent then takes the whole sandbox down."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


async def _run(
    argv: list[str],
    stdin: bytes | None,
    timeout_s: float,
    limits: ResourceLimits,
    procs: set[asyncio.subprocess.Process],
) -> tuple[int, str, str, bool, bool]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        preexec_fn=_rlimits(limits),
        env={"PATH": os.environ.get("PATH", DEFAULT_PATH)},
    )
    procs.add(proc)
    out, err = _Capture(limits.max_output_bytes), _Capture(limits.max_output_bytes)
    readers = [
        asyncio.create_task(_read(proc.stdout, out)),
        asyncio.create_task(_read(proc.stderr, err)),
    ]
    # Fed concurrently so a command that never reads stdin can't stall us past the deadline.
    feeder = asyncio.create_task(_write(proc.stdin, stdin))
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
    except TimeoutError:
        timed_out = True
        _kill(proc)
        await proc.wait()
    except asyncio.CancelledError:
        _kill(proc)
        raise
    finally:
        procs.discard(proc)
        feeder.cancel()
        if proc.stdin is not None:
            proc.stdin.close()
    _, pending = await asyncio.wait(readers, timeout=5)
    for t in pending:
        t.cancel()
    code = proc.returncode if proc.returncode is not None else -1
    if code < 0:
        code = 128 - code
    return code, out.text(), err.text(), bool(out.dropped or err.dropped), timed_out
