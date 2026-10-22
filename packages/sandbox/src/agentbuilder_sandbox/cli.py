"""`abx-sandbox [options] -- <cmd...>`: run one command in the sandbox, for manual testing.

Without `--policy`, the current directory is writable, `$HOME` is hidden except for the
current directory, and there is no network.
"""

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from agentbuilder_sandbox.policy import FilesystemPolicy, NetworkPolicy, SandboxPolicy
from agentbuilder_sandbox.runtime import Sandbox, SandboxUnavailable


def _default_policy(args: argparse.Namespace) -> SandboxPolicy:
    cwd = os.getcwd()
    return SandboxPolicy(
        filesystem=FilesystemPolicy(
            deny_read=[str(Path.home()), *args.deny_read],
            allow_read=[cwd],
            allow_write=[cwd, *args.allow_write],
        ),
        network=NetworkPolicy(allowed_domains=args.allow_domain),
        env={"HOME": "/tmp", "LANG": os.environ.get("LANG", "C.UTF-8")},
    )


async def _main(args: argparse.Namespace) -> int:
    if args.policy:
        policy = SandboxPolicy.model_validate_json(Path(args.policy).read_text())
    else:
        policy = _default_policy(args)
    with tempfile.TemporaryDirectory(prefix="abx-") as session:
        async with Sandbox(policy, Path(session)) as sandbox:
            result = await sandbox.exec(args.cmd, timeout_s=args.timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    for v in result.violations:
        print(f"[sandbox] {v.kind}: {v.detail}", file=sys.stderr)
    if result.timed_out:
        print(f"[sandbox] timed out after {args.timeout}s", file=sys.stderr)
    return result.exit_code


def main() -> None:
    p = argparse.ArgumentParser(prog="abx-sandbox", description=__doc__)
    p.add_argument("--policy", help="SandboxPolicy JSON file (overrides the other options)")
    p.add_argument("--allow-write", action="append", default=[], metavar="PATH")
    p.add_argument("--deny-read", action="append", default=[], metavar="PATH")
    p.add_argument("--allow-domain", action="append", default=[], metavar="DOMAIN")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("cmd", nargs="+")
    args = p.parse_args()
    try:
        sys.exit(asyncio.run(_main(args)))
    except SandboxUnavailable as e:
        sys.exit(f"abx-sandbox: {e}")


if __name__ == "__main__":
    main()
