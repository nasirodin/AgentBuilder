"""Run the assembled filter in a small classic-BPF interpreter against synthetic syscalls."""

import struct

import pytest

from agentbuilder_sandbox.linux import seccomp as sc

EPERM = sc.RET_ERRNO | sc.EPERM
ENOSYS = sc.RET_ERRNO | sc.ENOSYS


def run(prog: bytes, arch: str, name: str | int, *args: int, audit: int | None = None) -> int:
    a = sc.ARCHES[arch]
    nr = a.syscalls[name] if isinstance(name, str) else name
    data = struct.pack("<iI", nr, audit if audit is not None else a.audit) + struct.pack("<Q", 0)
    data += b"".join(struct.pack("<Q", x) for x in (*args, *[0] * (6 - len(args))))
    ins = [struct.unpack("<HBBI", prog[i : i + 8]) for i in range(0, len(prog), 8)]
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = ins[pc]
        if code == sc.LD_W_ABS:
            (acc,) = struct.unpack_from("<I", data, k)
        elif code == sc.RET_K:
            return k
        else:
            cond = {sc.JEQ_K: acc == k, sc.JGE_K: acc >= k, sc.JSET_K: bool(acc & k)}[code]
            pc += jt if cond else jf
        pc += 1


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_unix_and_namespace_rules(arch: str) -> None:
    prog = sc.build_filter(arch)
    assert run(prog, arch, "socket", sc.AF_UNIX) == EPERM
    assert run(prog, arch, "socket", sc.AF_VSOCK) == EPERM
    assert run(prog, arch, "socket", (7 << 32) | sc.AF_UNIX) == EPERM  # high bits ignored
    assert run(prog, arch, "socket", 2) == sc.RET_ALLOW  # AF_INET
    assert run(prog, arch, "io_uring_setup") == EPERM
    assert run(prog, arch, "unshare", sc.CLONE_NEWUSER) == EPERM
    assert run(prog, arch, "unshare", sc.CLONE_NEWNS | 0x40000000) == EPERM
    assert run(prog, arch, "unshare", 0x40000000) == sc.RET_ALLOW  # CLONE_NEWNET
    assert run(prog, arch, "clone", sc.CLONE_NEWUSER | 17) == EPERM
    assert run(prog, arch, "clone", 0x01200011) == sc.RET_ALLOW  # fork()
    assert run(prog, arch, "clone3") == ENOSYS
    for name in ("mount", "umount2", "pivot_root", "setns", "fsopen", "open_tree_attr"):
        assert run(prog, arch, name) == EPERM
    assert run(prog, arch, 0) == sc.RET_ALLOW
    assert run(prog, arch, 0, audit=0x40000003) == sc.RET_KILL_PROCESS  # i386


def test_x32_is_killed() -> None:
    prog = sc.build_filter("x86_64")
    assert run(prog, "x86_64", 0x40000000 + 41, sc.AF_UNIX) == sc.RET_KILL_PROCESS


def test_rules_can_be_disabled() -> None:
    prog = sc.build_filter("x86_64", block_unix_sockets=False, block_namespaces=False)
    assert run(prog, "x86_64", "socket", sc.AF_UNIX) == sc.RET_ALLOW
    assert run(prog, "x86_64", "unshare", sc.CLONE_NEWUSER) == sc.RET_ALLOW
    nested = sc.build_filter("x86_64", block_namespaces=False)
    assert run(nested, "x86_64", "socket", sc.AF_UNIX) == EPERM
    assert run(nested, "x86_64", "mount") == sc.RET_ALLOW
