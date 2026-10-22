"""Process sandbox for untrusted code: bubblewrap + seccomp + filtering proxies.

A Python re-implementation of the model of Anthropic's sandbox-runtime (see
design/04-sandbox.md). No dependency on the rest of AgentBuilder.
"""

from agentbuilder_sandbox.net.filter import ApprovalCallback, NetworkRequest
from agentbuilder_sandbox.policy import (
    FilesystemPolicy,
    NetworkPolicy,
    ResourceLimits,
    SandboxPolicy,
)
from agentbuilder_sandbox.runtime import ExecResult, Sandbox, SandboxUnavailable, check_host
from agentbuilder_sandbox.violations import Violation

__all__ = [
    "ApprovalCallback",
    "ExecResult",
    "FilesystemPolicy",
    "NetworkPolicy",
    "NetworkRequest",
    "ResourceLimits",
    "Sandbox",
    "SandboxPolicy",
    "SandboxUnavailable",
    "Violation",
    "check_host",
]
