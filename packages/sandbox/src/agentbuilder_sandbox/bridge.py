"""In-sandbox bridge: runs INSIDE the sandbox as `python -I -S bridge.py <spec>`.

Trusted code, standard library only, no imports from this package (it is run by path with
`-I`). The process tree mirrors srt's `apply-seccomp`:

    bwrap init
      bridge            relays 127.0.0.1:<port> <-> host Unix sockets; unfiltered, non-dumpable
        stub            makes a user+pid+mount namespace, waits, propagates the exit status
          init (pid 1)  fresh /proc, no further user namespaces, reaps, forwards signals
            command     no_new_privs + seccomp filter, then exec

The command lives in a PID namespace of its own with its own /proc, so it cannot see, signal
or ptrace the unfiltered bridge (which can create AF_UNIX sockets). If the bridge dies the
command only loses its own network access.

Spec (base64 JSON): {"listen": [[port, socket_path], ...], "seccomp": "<hex BPF>",
"nested_userns": bool, "argv": [...]}.
"""

import asyncio
import base64
import contextlib
import ctypes
import json
import os
import signal
import socket
import sys

PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_REC, MS_PRIVATE = 2, 4, 8, 16384, 1 << 18
FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1)

_libc = ctypes.CDLL(None, use_errno=True)


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.c_void_p))


def _check(rc: int, what: str) -> None:
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")


def _prctl(option: int, arg: int | ctypes.c_void_p = 0) -> None:
    _check(_libc.prctl(option, arg, 0, 0, 0), f"prctl({option})")


def _die(msg: str, code: int = 126) -> None:
    os.write(2, f"sandbox: {msg}\n".encode(errors="replace"))
    os._exit(code)


def _exit_code(status: int) -> int:
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


# --- command side (forked before any thread or event loop exists) ---------------------------


def _stub(spec: dict) -> None:
    uid, gid = os.geteuid(), os.getegid()
    created_userns = False
    try:
        os.unshare(os.CLONE_NEWPID | os.CLONE_NEWNS)
    except PermissionError:  # no capabilities under bwrap --cap-drop ALL: own a userns first
        os.unshare(os.CLONE_NEWUSER)
        for name, data in (("setgroups", "deny"), ("uid_map", f"{uid} {uid} 1"),
                           ("gid_map", f"{gid} {gid} 1")):  # fmt: skip
            with open(f"/proc/self/{name}", "w") as f:
                f.write(data)
        os.unshare(os.CLONE_NEWPID | os.CLONE_NEWNS)
        created_userns = True
    _prctl(PR_SET_DUMPABLE, 0)
    child = os.fork()  # becomes pid 1 of the new pid namespace
    if child == 0:
        _init(spec, created_userns)
    for sig in FORWARDED:
        signal.signal(sig, lambda s, _f: os.kill(child, s))
    _, status = os.waitpid(child, 0)
    os._exit(_exit_code(status))


def _init(spec: dict, created_userns: bool) -> None:
    _libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None)
    # A fresh /proc shows only this pid namespace. Refused where bwrap's /proc has read-only
    # covers; then non-dumpable ancestors and the separate pid namespace still keep the
    # command away from the bridge.
    _libc.mount(b"proc", b"/proc", b"proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, None)
    if created_userns and not spec["nested_userns"]:
        with contextlib.suppress(OSError), open("/proc/sys/user/max_user_namespaces", "w") as f:
            f.write("0")
    _prctl(PR_SET_DUMPABLE, 0)
    worker = os.fork()
    if worker == 0:
        _exec_command(spec)
    for sig in FORWARDED:
        signal.signal(sig, lambda s, _f: os.kill(worker, s))
    while True:  # pid 1 reaps every orphan; its exit tears the namespace down
        pid, status = os.wait()
        if pid == worker:
            os._exit(_exit_code(status))


def _exec_command(spec: dict) -> None:
    for sig in FORWARDED:
        signal.signal(sig, signal.SIG_DFL)
    try:
        _prctl(PR_SET_NO_NEW_PRIVS, 1)
        bpf = bytes.fromhex(spec["seccomp"])
        if bpf:
            buf = ctypes.create_string_buffer(bpf, len(bpf))
            prog = _SockFprog(len(bpf) // 8, ctypes.addressof(buf))
            _check(
                _libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0),
                "seccomp",
            )
    except OSError as e:
        _die(f"cannot apply restrictions: {e}")
    argv = spec["argv"]
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        _die(f"{argv[0]}: command not found", 127)
    except OSError as e:
        _die(f"{argv[0]}: {e.strerror}", 126)


# --- relays ------------------------------------------------------------------------------


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except OSError:
        pass


async def _relay(path: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        up_reader, up_writer = await asyncio.open_unix_connection(path)
    except OSError:
        writer.close()
        return
    try:
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    finally:
        up_writer.close()
        writer.close()


async def _serve(listeners: list[tuple[socket.socket, str]], child: int) -> int:
    loop = asyncio.get_running_loop()
    servers = [
        await asyncio.start_server(lambda r, w, p=path: _relay(p, r, w), sock=sock)
        for sock, path in listeners
    ]
    for sig in FORWARDED:
        loop.add_signal_handler(sig, os.kill, child, sig)
    # A thread is safe here: every fork has happened. (os.pidfd_open is missing from some builds.)
    _, status = await loop.run_in_executor(None, os.waitpid, child, 0)
    for s in servers:
        s.close()
    return _exit_code(status)


def main() -> None:
    spec = json.loads(base64.b64decode(sys.argv[1]))
    listeners = []
    for port, path in spec["listen"]:  # listening before the fork: no startup race
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(256)
        listeners.append((s, path))
    child = os.fork()
    if child == 0:
        for s, _ in listeners:
            s.close()
        try:
            _stub(spec)
        except BaseException as e:  # noqa: BLE001 - never fall back to running unisolated
            _die(f"cannot isolate command: {e}")
    _prctl(PR_SET_DUMPABLE, 0)
    sys.exit(asyncio.run(_serve(listeners, child)))


if __name__ == "__main__":
    main()
