"""Sandbox policy models (see design/04-sandbox.md, "Policy model").

Read is allow-by-default with `deny_read` regions and `allow_read` re-openings; write is
deny-by-default with `allow_write` regions and `deny_write` carve-outs. The more specific
(deeper) rule wins between a read deny and a read allow; a write deny always wins over a
write allow at or below it. All paths are absolute and literal: globs are not supported,
because a deny glob that silently matches nothing is a hole.
"""

import ipaddress
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentbuilder_sandbox.net.filter import DomainRule, parse_network


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _absolute(paths: list[str]) -> list[str]:
    for p in paths:
        if not p.startswith("/") or "\0" in p:
            raise ValueError(f"path must be absolute: {p!r}")
        if any(c in p for c in "*?["):
            raise ValueError(f"globs are not supported, list literal paths: {p!r}")
    return [str(PurePosixPath(p)) for p in paths]


class FilesystemPolicy(_Strict):
    deny_read: list[str] = []
    """Regions hidden from the sandbox (an empty tmpfs over directories, /dev/null over files)."""
    allow_read: list[str] = []
    """Re-opened read-only inside `deny_read` regions."""
    allow_write: list[str] = []
    """Writable; everything else is read-only."""
    deny_write: list[str] = []
    """Read-only even inside `allow_write` (deny wins)."""
    scratch_write: list[str] = []
    """Writable like `allow_write` but without dangerous-file protection: scratch areas no
    host tool acts on (the sandbox's own HOME/TMPDIR)."""
    protect_dangerous: bool = True
    """Also deny-write the dangerous files in every write root (see `dangerous.py`)."""

    @field_validator("deny_read", "allow_read", "allow_write", "deny_write", "scratch_write")
    @classmethod
    def _paths(cls, v: list[str]) -> list[str]:
        return _absolute(v)


class NetworkPolicy(_Strict):
    allowed_domains: list[str] = []
    """`pypi.org`, `*.pythonhosted.org`, `api.x.com:443`, `[::1]:8080`, `127.0.0.1:3000`."""
    denied_domains: list[str] = []
    """Checked first. Also accepts `*` and `*:<port>`."""
    denied_resolved_addresses: list[str] = []
    """Extra CIDRs allowed hostnames must not resolve to (on top of the built-in set)."""
    deny_private_addresses: bool = True
    """Also refuse hostnames resolving to RFC 1918, CGNAT and ULA space."""
    services: dict[str, str] = {}
    """Name -> host Unix socket, bridged to a loopback port inside and exposed as
    `SANDBOX_SVC_<NAME>_URL`."""

    @field_validator("allowed_domains")
    @classmethod
    def _allowed(cls, v: list[str]) -> list[str]:
        parse_network(v, allow_star=False)
        return v

    @field_validator("denied_domains")
    @classmethod
    def _denied(cls, v: list[str]) -> list[str]:
        parse_network(v, allow_star=True)
        return v

    @field_validator("denied_resolved_addresses")
    @classmethod
    def _cidrs(cls, v: list[str]) -> list[str]:
        for c in v:
            ipaddress.ip_network(c, strict=False)
        return v

    @field_validator("services")
    @classmethod
    def _services(cls, v: dict[str, str]) -> dict[str, str]:
        for name, path in v.items():
            if not name.isidentifier() or not name.isascii():
                raise ValueError(f"service name must be an identifier: {name!r}")
            _absolute([path])
        return v

    def allowed_rules(self) -> list[DomainRule]:
        return parse_network(self.allowed_domains, allow_star=False)

    def denied_rules(self) -> list[DomainRule]:
        return parse_network(self.denied_domains, allow_star=True)


class ResourceLimits(_Strict):
    memory_bytes: int | None = None
    """RLIMIT_AS. Off by default: runtimes that reserve address space (Go, JVM) break under it."""
    cpu_seconds: int | None = None
    file_size_bytes: int | None = 1 << 30
    open_files: int | None = 1024
    max_output_bytes: int = Field(default=256 * 1024, ge=1024)
    """Per stream; the middle of longer output is dropped."""
    max_concurrent: int = Field(default=4, ge=1)


class SandboxPolicy(_Strict):
    filesystem: FilesystemPolicy = FilesystemPolicy()
    network: NetworkPolicy = NetworkPolicy()
    limits: ResourceLimits = ResourceLimits()
    env: dict[str, str] = {}
    """The complete environment of the command; nothing is inherited from the host. Proxy
    variables and service URLs are added on top."""
    block_unix_sockets: bool = True
    """seccomp: refuse socket(AF_UNIX / AF_VSOCK) and io_uring."""
    allow_nested_userns: bool = False
    """Let the command create user/mount namespaces. Undoes every write deny; do not enable
    for untrusted code."""
    bwrap_path: str | None = None
