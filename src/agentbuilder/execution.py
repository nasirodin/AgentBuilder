"""Sandboxed command execution for builder agents (design/04-sandbox.md, "Session policy").

A session directory holds the `workspace` (the agent's working tree, a plain host directory)
and `tmp` (HOME/TMPDIR inside the sandbox). Commands only ever run through
`agentbuilder_sandbox.Sandbox`; where it is unavailable (native Windows, no bubblewrap) the
tools say so and run nothing.
"""

import asyncio
import os
import secrets
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic_ai.toolsets import FunctionToolset

from agentbuilder_sandbox import (
    ExecResult,
    FilesystemPolicy,
    NetworkPolicy,
    Sandbox,
    SandboxPolicy,
    SandboxUnavailable,
    check_host,
)

MAX_TIMEOUT_S = 600


def new_session_dir(data_dir: Path) -> Path:
    """A fresh session path; created on first use."""
    return data_dir / "sessions" / (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2))


def _host_secret_paths() -> list[str]:
    """Regions hidden from every sandboxed command."""
    paths = [str(Path.home()), f"/run/user/{os.getuid()}", "/var/run/docker.sock"]
    if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists():
        # WSL: Windows drives are mounted at /mnt/<letter>; hide all of them
        paths += [str(p) for p in Path("/mnt").glob("?") if p.is_dir()]
    return paths


class SandboxExecutor:
    """A session workspace plus a lazily started sandbox to run commands in."""

    def __init__(self, session_dir: Path, allowed_domains: Sequence[str] = ()) -> None:
        self.session_dir = session_dir
        self.workspace = session_dir / "workspace"
        self.tmp = session_dir / "tmp"
        self.allowed_domains = list(allowed_domains)
        self._sandbox: Sandbox | None = None
        self._lock = asyncio.Lock()

    def policy(self) -> SandboxPolicy:
        ws, tmp = str(self.workspace), str(self.tmp)
        return SandboxPolicy(
            filesystem=FilesystemPolicy(
                deny_read=_host_secret_paths(),
                allow_read=[str(self.session_dir)],
                allow_write=[ws],
                scratch_write=[tmp],
                deny_write=[str(self.workspace / ".git")],
            ),
            network=NetworkPolicy(allowed_domains=self.allowed_domains),
            env={"HOME": tmp, "TMPDIR": tmp, "LANG": "C.UTF-8", "TERM": "dumb"},
        )

    async def _get(self) -> Sandbox:
        async with self._lock:
            if self._sandbox is None:
                if errors := check_host(SandboxPolicy()):  # before any Linux-only call
                    raise SandboxUnavailable("; ".join(errors))
                self.session_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
                self.workspace.mkdir(exist_ok=True)
                self.tmp.mkdir(exist_ok=True)
                sandbox = Sandbox(self.policy(), self.session_dir / "sandbox")
                await sandbox.start()  # raises SandboxUnavailable
                self._sandbox = sandbox
            return self._sandbox

    async def run(
        self, cmd: list[str], *, stdin: bytes | None = None, timeout_s: float = 60
    ) -> ExecResult:
        sandbox = await self._get()
        timeout_s = min(max(timeout_s, 1), MAX_TIMEOUT_S)
        return await sandbox.exec(cmd, cwd=str(self.workspace), stdin=stdin, timeout_s=timeout_s)

    async def aclose(self) -> None:
        if self._sandbox is not None:
            await self._sandbox.stop()


def format_result(result: ExecResult) -> str:
    """Render an exec result for the model."""
    head = f"exit code: {result.exit_code} ({result.duration_s:.1f}s)"
    if result.timed_out:
        head += " - TIMED OUT, the command was killed"
    parts = [head]
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout.rstrip()}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr.rstrip()}")
    if result.violations:
        parts.append(
            "sandbox violations:\n"
            + "\n".join(f"- {v.kind}: {v.detail}" for v in result.violations)
        )
    return "\n\n".join(parts)


def sandbox_toolset(executor: SandboxExecutor) -> FunctionToolset[None]:
    toolset: FunctionToolset[None] = FunctionToolset()

    @toolset.tool_plain
    async def run_bash(command: str, timeout_s: int = 60) -> str:
        """Run a bash command in the sandboxed workspace and return its exit code and output.

        The working directory is the session workspace, which persists between calls; it is
        the only writable place besides $HOME/$TMPDIR. The rest of the filesystem is
        read-only and the user's home directory is hidden. Network access is blocked except
        for allow-listed domains (blocked attempts are reported as sandbox violations).
        Empty read-only placeholders for protected names (.bashrc, .vscode, .git/hooks, ...)
        may appear in the workspace while a command runs. Long output is truncated in the
        middle.

        Run Python with `python3 script.py`, or inline with a quoted heredoc so the shell
        leaves the code alone:

            python3 - <<'EOF'
            print("hello")
            EOF

        Args:
            command: A bash script, e.g. `ls -la` or `python3 -m pytest -q`.
            timeout_s: Seconds before the command is killed (max 600).
        """
        try:
            result = await executor.run(["bash", "-c", command], timeout_s=timeout_s)
        except SandboxUnavailable as e:
            return f"The sandbox is unavailable, nothing was run: {e}"
        return format_result(result)

    return toolset
