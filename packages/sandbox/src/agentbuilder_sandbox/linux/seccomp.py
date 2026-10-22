"""Hand-assembled classic-BPF seccomp filter (no libseccomp), after srt's
`vendor/seccomp-src/seccomp-unix-block.c`.

Two groups of rules, both on a default-ALLOW filter:

- unix: `socket(AF_UNIX | AF_VSOCK, ...)` -> EPERM (the network namespace fences neither;
  pathname sockets are reachable through any readable path), and `io_uring_*` -> EPERM
  (`IORING_OP_SOCKET` would create sockets without calling socket()). `socketpair` stays
  allowed: asyncio and many runtimes need it.
- namespaces: `unshare`/`clone` with CLONE_NEWUSER or CLONE_NEWNS, `setns`, `mount` and every
  other mount-API call -> EPERM; `clone3` -> ENOSYS so libc falls back to `clone`, whose flags
  we can inspect. A new user namespace would give the command a full capability set over a
  copy of the mount tree, from which every read-only bind (all write denies) can be removed.

The architecture is checked first and a foreign one (e.g. i386 syscalls via `int 0x80` on
x86-64) kills the process; on x86-64 the x32 syscall range is killed too.
"""

import platform
import struct
from dataclasses import dataclass

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7

RET_KILL_PROCESS = 0x80000000
RET_ALLOW = 0x7FFF0000
RET_ERRNO = 0x00050000
EPERM = 1
ENOSYS = 38

AF_UNIX = 1
AF_VSOCK = 40
CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
UNSHARE_EMPTY_MNTNS = 0x00100000

# classic BPF opcodes
LD_W_ABS = 0x20
JEQ_K = 0x15
JGE_K = 0x35
JSET_K = 0x45
RET_K = 0x06

# offsets into struct seccomp_data (little-endian; low word of each argument)
OFF_NR = 0
OFF_ARCH = 4
OFF_ARG0 = 16


@dataclass(frozen=True)
class Arch:
    audit: int
    syscalls: dict[str, int]
    x32_bit: bool = False


_COMMON = {
    "io_uring_setup": 425,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    "open_tree": 428,
    "move_mount": 429,
    "fsopen": 430,
    "fsconfig": 431,
    "fsmount": 432,
    "fspick": 433,
    "clone3": 435,
    "mount_setattr": 442,
    "open_tree_attr": 467,
}

ARCHES: dict[str, Arch] = {
    "x86_64": Arch(
        AUDIT_ARCH_X86_64,
        _COMMON
        | {
            "socket": 41,
            "clone": 56,
            "pivot_root": 155,
            "mount": 165,
            "umount2": 166,
            "unshare": 272,
            "setns": 308,
        },
        x32_bit=True,
    ),
    "aarch64": Arch(
        AUDIT_ARCH_AARCH64,
        _COMMON
        | {
            "umount2": 39,
            "mount": 40,
            "pivot_root": 41,
            "unshare": 97,
            "socket": 198,
            "clone": 220,
            "setns": 268,
        },
    ),
}
ARCHES["amd64"] = ARCHES["x86_64"]
ARCHES["arm64"] = ARCHES["aarch64"]


def host_arch() -> str | None:
    m = platform.machine().lower()
    return m if m in ARCHES else None


class _Asm:
    """Tiny assembler: jumps name labels, resolved to relative offsets at the end."""

    def __init__(self) -> None:
        self.ins: list[tuple[int, int | str, int | str, int]] = []
        self.labels: dict[str, int] = {}

    def label(self, name: str) -> None:
        self.labels[name] = len(self.ins)

    def ld(self, off: int) -> None:
        self.ins.append((LD_W_ABS, 0, 0, off))

    def jmp(self, op: int, k: int, jt: int | str, jf: int | str = 0) -> None:
        self.ins.append((op, jt, jf, k))

    def ret(self, k: int) -> None:
        self.ins.append((RET_K, 0, 0, k))

    def assemble(self) -> bytes:
        out = bytearray()
        for i, (code, jt, jf, k) in enumerate(self.ins):
            rel = [self.labels[j] - i - 1 if isinstance(j, str) else j for j in (jt, jf)]
            if not all(0 <= r <= 255 for r in rel):
                raise ValueError("jump out of range")
            out += struct.pack("<HBBI", code, rel[0], rel[1], k)
        return bytes(out)


def build_filter(
    arch: str, *, block_unix_sockets: bool = True, block_namespaces: bool = True
) -> bytes:
    """The filter program as raw `struct sock_filter` bytes (8 bytes per instruction)."""
    a = ARCHES[arch]
    nr = a.syscalls
    eperm, enosys = RET_ERRNO | EPERM, RET_ERRNO | ENOSYS

    asm = _Asm()
    asm.ld(OFF_ARCH)
    asm.jmp(JEQ_K, a.audit, 1, 0)
    asm.ret(RET_KILL_PROCESS)
    asm.ld(OFF_NR)
    if a.x32_bit:
        asm.jmp(JGE_K, 0x40000000, "kill")

    deny: list[str] = []
    if block_unix_sockets:
        asm.jmp(JEQ_K, nr["socket"], "socket")
        deny += ["io_uring_setup", "io_uring_enter", "io_uring_register"]
    if block_namespaces:
        asm.jmp(JEQ_K, nr["unshare"], "unshare")
        asm.jmp(JEQ_K, nr["clone"], "clone")
        asm.jmp(JEQ_K, nr["clone3"], "enosys")
        deny += ["setns", "mount", "umount2", "pivot_root", "open_tree", "move_mount"]
        deny += ["fsopen", "fsconfig", "fsmount", "fspick", "mount_setattr", "open_tree_attr"]
    for name in deny:
        asm.jmp(JEQ_K, nr[name], "eperm")
    asm.ret(RET_ALLOW)

    if block_unix_sockets:
        asm.label("socket")
        asm.ld(OFF_ARG0)  # low 32 bits of the domain, as srt's MASKED_EQ 0xffffffff
        asm.jmp(JEQ_K, AF_UNIX, "eperm")
        asm.jmp(JEQ_K, AF_VSOCK, "eperm")
        asm.ret(RET_ALLOW)
    if block_namespaces:
        asm.label("unshare")
        asm.ld(OFF_ARG0)
        asm.jmp(JSET_K, CLONE_NEWUSER | CLONE_NEWNS | UNSHARE_EMPTY_MNTNS, "eperm")
        asm.ret(RET_ALLOW)
        asm.label("clone")
        asm.ld(OFF_ARG0)  # clone(flags, ...) on both x86-64 and aarch64
        asm.jmp(JSET_K, CLONE_NEWUSER | CLONE_NEWNS, "eperm")
        asm.ret(RET_ALLOW)
        asm.label("enosys")
        asm.ret(enosys)
    asm.label("eperm")
    asm.ret(eperm)
    asm.label("kill")
    asm.ret(RET_KILL_PROCESS)
    return asm.assemble()
