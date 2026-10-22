"""Policy -> bubblewrap argv (the mount plan). See design/04-sandbox.md "Linux execution plan".

Mount order matters, later mounts shadow earlier ones:

1. `--ro-bind / /`: the whole host read-only.
2. Pins: every existing directory between a write root and a write-denied path inside it is
   bound onto itself. The later bind of the write root buries them, so lookups never see
   them, but they stay mountpoints: `mv .git x` or `rmdir` of a pinned directory fails with
   EBUSY, which stops "move the protected directory aside and make a fresh one" (srt's
   ancestor pins).
3. Fresh `/dev`, `/proc` and an empty `/tmp`.
4. Rule mounts, shallow paths first so the more specific rule wins; for one path in the
   order deny_read, allow_read, allow_write, deny_write:
   - deny_read: tmpfs over a directory, /dev/null over a file;
   - allow_read: read-only bind, only where a deny_read region would otherwise hide it;
   - allow_write: read-write bind;
   - deny_write: read-only bind of the path itself, or of a placeholder at its first missing
     component (an empty file for the leaf, an empty directory above it) so it cannot be
     created; skipped where a deny_read region already hides it.
5. Service/proxy socket files, then the placeholder source directory read-only.

Every path is resolved (symlinks followed) before use: bubblewrap mounts on what a name leads
to, and a bind source must be the real location.
"""

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from agentbuilder_sandbox.dangerous import dangerous_paths
from agentbuilder_sandbox.policy import FilesystemPolicy

DENY_READ, ALLOW_READ, ALLOW_WRITE, DENY_WRITE = range(4)


@dataclass
class MountPlan:
    args: list[str]
    write_roots: list[str]
    placeholders: list[str] = field(default_factory=list)
    """Paths bubblewrap will create on the host as mountpoints; remove after the exec."""
    protected: list[str] = field(default_factory=list)
    """Write-denied paths inside write roots (for violation messages)."""


def _depth(p: str) -> int:
    return len(PurePosixPath(p).parts)


def _under(p: str, root: str) -> bool:
    return p == root or p.startswith(root.rstrip("/") + "/")


def _resolve(p: str) -> str:
    return os.path.realpath(p)


def _is_dir(p: str) -> bool:
    try:
        return stat.S_ISDIR(os.stat(p).st_mode)
    except OSError:
        return False


class _Rules:
    def __init__(self, deny_read: list[str], allow_read: list[str], allow_write: list[str]):
        self.kinds = [(DENY_READ, deny_read), (ALLOW_READ, allow_read), (ALLOW_WRITE, allow_write)]

    def hidden(self, p: str, *, strict: bool) -> bool:
        """Is `p` inside a deny_read region not re-opened by a deeper allow?"""
        best: tuple[int, int] | None = None  # (depth, kind); later kind wins at equal depth
        for kind, paths in self.kinds:
            for r in paths:
                if _under(p, r) and not (strict and r == p):
                    cand = (_depth(r), kind)
                    if best is None or cand > best:
                        best = cand
        return best is not None and best[1] == DENY_READ


def plan_mounts(
    fs: FilesystemPolicy,
    *,
    extra_read: Sequence[str] = (),
    socket_binds: Sequence[str] = (),
    empty_dir: str,
) -> MountPlan:
    deny_read = _existing(fs.deny_read)
    allow_read = _existing([*fs.allow_read, *extra_read])
    deny_write_cfg = [_resolve(p) for p in fs.deny_write]
    # a write deny wins over a write allow at or below it
    allow_write = [
        p
        for p in _existing([*fs.allow_write, *fs.scratch_write])
        if not any(_under(p, d) for d in deny_write_cfg)
    ]
    scratch = set(_existing(fs.scratch_write))
    if "/" in allow_write:
        raise ValueError("allow_write must not contain /")
    rules = _Rules(deny_read, allow_read, allow_write)

    deny_write = list(deny_write_cfg)
    if fs.protect_dangerous:
        for root in allow_write:
            if _is_dir(root) and root not in scratch:
                deny_write += [_resolve(p) for p in dangerous_paths(root)]

    mounts: list[tuple[int, int, list[str]]] = []  # (depth, kind, args)
    pins: set[str] = set()
    placeholders: list[str] = []
    protected: list[str] = []
    seen: set[str] = set()
    for d in dict.fromkeys(deny_write):
        root = _covering(d, allow_write)
        if root is None or d in seen:
            continue  # outside every write root: already read-only via `/`
        if rules.hidden(d, strict=False):
            continue  # hidden by a deny_read tmpfs; binding it would re-expose it
        if os.path.lexists(d):
            target, src = d, d
        else:
            target = _first_missing(d)
            if target in seen or not _under(target, root) or target == root:
                continue
            src = "/dev/null" if target == d else empty_dir
            placeholders.append(target)
        seen.add(target)
        protected.append(d)
        mounts.append((_depth(target), DENY_WRITE, ["--ro-bind", src, target]))
        parent = os.path.dirname(target)
        while parent != root and _under(parent, root):
            if _is_dir(parent):
                pins.add(parent)
            parent = os.path.dirname(parent)

    for p in deny_read:
        if _is_dir(p):
            mounts.append((_depth(p), DENY_READ, ["--tmpfs", p]))
        else:
            mounts.append((_depth(p), DENY_READ, ["--ro-bind", "/dev/null", p]))
    for p in allow_read:
        if rules.hidden(p, strict=True):
            mounts.append((_depth(p), ALLOW_READ, ["--ro-bind", p, p]))
    for p in allow_write:
        mounts.append((_depth(p), ALLOW_WRITE, ["--bind", p, p]))

    args = ["--ro-bind", "/", "/"]
    for pin in sorted(pins, key=_depth):
        args += ["--ro-bind", pin, pin]
    args += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for _, _, a in sorted(mounts, key=lambda m: (m[0], m[1])):
        args += a
    for s in socket_binds:
        args += ["--bind", s, s]
    args += ["--ro-bind", empty_dir, empty_dir]
    return MountPlan(args, allow_write, placeholders, protected)


def _existing(paths: Sequence[str]) -> list[str]:
    out = [_resolve(p) for p in paths]
    return list(dict.fromkeys(p for p in out if os.path.exists(p)))


def _covering(p: str, roots: list[str]) -> str | None:
    best = None
    for r in roots:
        if _under(p, r) and (best is None or _depth(r) > _depth(best)):
            best = r
    return best


def _first_missing(p: str) -> str:
    cur = p
    while not os.path.lexists(os.path.dirname(cur)):
        cur = os.path.dirname(cur)
    return cur


def bwrap_argv(
    bwrap: str,
    plan: MountPlan,
    *,
    env: dict[str, str],
    cwd: str,
    command: list[str],
) -> list[str]:
    argv = [
        bwrap,
        "--new-session",  # no TIOCSTI into our terminal
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--unshare-net",  # only loopback inside; egress through the bridged proxies
        "--hostname",
        "sandbox",
        "--cap-drop",
        "ALL",
        *plan.args,
        "--clearenv",
    ]
    for k, v in env.items():
        argv += ["--setenv", k, v]
    return [*argv, "--chdir", cwd, "--", *command]
