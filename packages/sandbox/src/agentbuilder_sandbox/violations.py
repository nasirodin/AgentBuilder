"""Violations reported with an exec result.

Network violations are exact (the proxy's decisions). Filesystem and socket ones are guessed
from stderr: bubblewrap reports nothing, and precise auditing (fanotify/eBPF) is out of scope.
"""

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel

from agentbuilder_sandbox.net.filter import sanitize
from agentbuilder_sandbox.net.proxy import ConnectionRecord

MAX_VIOLATIONS = 20


class Violation(BaseModel):
    kind: Literal["network", "filesystem", "unix_socket"]
    detail: str


def network_violations(records: Iterable[ConnectionRecord]) -> list[Violation]:
    return [
        Violation(kind="network", detail=f"deny {r.protocol} {r.host}:{r.port} ({r.reason})")
        for r in records
        if not r.allowed
    ]


def stderr_violations(stderr: str, protected: Iterable[str] = ()) -> list[Violation]:
    protected = list(protected)
    out: list[Violation] = []
    lines = stderr.splitlines()
    for i, line in enumerate(lines):
        low = line.lower()
        context = "\n".join(lines[max(0, i - 2) : i + 1]).lower()  # tracebacks split it up
        if "read-only file system" in low or "errno 30" in low:
            out.append(Violation(kind="filesystem", detail=sanitize(line)))
        elif "operation not permitted" in low or "errno 1]" in low:
            if "socket" in context or "connect" in context:
                out.append(Violation(kind="unix_socket", detail=sanitize(line)))
            elif any(p in line for p in protected):
                out.append(Violation(kind="filesystem", detail=sanitize(line)))
        if len(out) >= MAX_VIOLATIONS:
            break
    return out
